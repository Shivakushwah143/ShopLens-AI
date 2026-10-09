"""LangGraph-powered research orchestrator.

Replaces the fixed pipeline with an adaptive state graph that can loop back
to gather more evidence when coverage is thin, and self-review its answer.

Public interface is identical to the previous orchestrator so routes.py and
the rest of the codebase remain untouched.
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph
from sqlmodel.ext.asyncio.session import AsyncSession

from app.agent.answerer import AnswerComposer
from app.agent.intake import QueryIntake
from app.agent.planner import ResearchPlanner
from app.agent.verification import Verifier
from app.browser.worker import BrowserWorker
from app.core.config import get_settings
from app.core.llm import llm_client
from app.core.stream import screencast_stream
from app.extraction.web import WebEvidenceExtractor
from app.extraction.youtube import YouTubeExtractor
from app.models.schemas import (
    BrowserAction,
    BrowserActionType,
    ConfidenceScore,
    ErrorEvent,
    FinalAnswer,
    ImageItem,
    QueryIntent,
    ResearchPlan,
    SourceItem,
    SourceType,
    TaskStatus,
    UserQuery,
    VideoItem,
)
from app.ranking.source_ranker import SourceRanker
from app.storage.repositories import TaskRepository
from app.utils.text import guess_domain, keyword_overlap_score

logger = logging.getLogger(__name__)

# Max browser tabs to open simultaneously during source visiting
_VISIT_CONCURRENCY = 4

# ── Helpers ──────────────────────────────────────────────────────────────

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _should_skip_result(query: str, title: str, snippet: str, domain: str,
                        blocked_domains: set[str] | None = None) -> bool:
    blocked = {
        "dictionary", "dictionaries", "wiktionary.org", "thesaurus.com",
        "vocabulary.com", "pinterest.com", "quora.com", "facebook.com",
        "instagram.com", "twitter.com", "tiktok.com", "linkedin.com",
        "reddit.com",  # often requires JS / login
    }
    d = domain.lower()
    if any(h in d for h in blocked):
        return True
    # Skip domains previously found to be blocked/unusable
    if blocked_domains and d in blocked_domains:
        return True
    t = title.lower()
    if any(w in t for w in {" noun ", "definition", "pronunciation"}):
        return True
    return keyword_overlap_score(query, f"{title} {snippet or ''}") < 0.10


# ── LangGraph State ─────────────────────────────────────────────────────

class ResearchState(TypedDict, total=False):
    # Identifiers
    task_id: str
    session: Any  # AsyncSession (not serialisable, kept in-memory only)
    # Query analysis
    query: UserQuery
    intent: QueryIntent
    plan: ResearchPlan
    # Collected data
    sources: list[SourceItem]
    evidence: list
    videos: list[VideoItem]
    images: list[ImageItem]
    # Tracking
    search_iteration: int
    review_iteration: int
    used_queries: list[str]
    domain_counter: dict[str, int]
    blocked_domains: set[str]  # Domains that repeatedly fail access
    # Results
    ranked_sources: list[SourceItem]
    confidence: ConfidenceScore
    answer: FinalAnswer
    # Browser (kept open across nodes)
    browser: Any


# ── Graph Node Functions ─────────────────────────────────────────────────

async def analyze_query(state: ResearchState) -> dict:
    repo = TaskRepository(state["session"])
    task = await repo.get_task(state["task_id"])
    await repo.update_task(task, status=TaskStatus.planning, current_step="Analyzing query with LLM")
    intake = QueryIntake()
    query, intent = await intake.analyze(task.query_text)
    return {"query": query, "intent": intent}


async def build_plan(state: ResearchState) -> dict:
    repo = TaskRepository(state["session"])
    task = await repo.get_task(state["task_id"])
    planner = ResearchPlanner()
    plan = await planner.build_plan(state["query"], state["intent"])
    await repo.update_task(task, status=TaskStatus.planning, current_step="Plan generated", plan=plan)
    await repo.add_action(BrowserAction(
        task_id=state["task_id"], action_type=BrowserActionType.search,
        description=f"Generated research plan with {len(plan.search_queries)} queries",
        created_at=_utcnow(), metadata=plan.model_dump(),
    ))
    return {
        "plan": plan,
        "sources": [], "evidence": [], "videos": [], "images": [],
        "search_iteration": 0, "review_iteration": 0,
        "used_queries": [], "domain_counter": {},
        "blocked_domains": set(),
    }


async def search_and_collect(state: ResearchState) -> dict:
    """Core browsing node — searches queries in parallel, then visits pages in parallel.

    Behaviour is identical to the previous sequential version; only the I/O execution
    strategy has changed:
      • Phase 1 – all web_search calls for the current batch run concurrently.
      • Phase 2 – LLM filtering of each search result set runs concurrently.
      • Phase 3 – up to _VISIT_CONCURRENCY pages are opened in separate browser tabs
                   simultaneously, each using capture_page_on_new_tab so they don't
                   share the main self.page object.
    All existing domain caps, blocked-domain tracking, evidence extraction, and error
    recording are preserved unchanged.
    """
    repo = TaskRepository(state["session"])
    task = await repo.get_task(state["task_id"])
    settings = get_settings()
    await repo.update_task(task, status=TaskStatus.researching, current_step="Browsing sources")

    plan = state["plan"]
    sources = list(state.get("sources") or [])
    evidence = list(state.get("evidence") or [])
    videos = list(state.get("videos") or [])
    iteration = state.get("search_iteration", 0)
    used_queries = list(state.get("used_queries") or [])
    domain_counter: dict[str, int] = dict(state.get("domain_counter") or {})
    blocked_domains: set[str] = set(state.get("blocked_domains") or set())
    extractor = WebEvidenceExtractor()
    youtube_extractor = YouTubeExtractor()

    target_sources = max(10, min(plan.source_limit, 15))
    per_query_limit = 8
    max_per_domain = 2
    max_access_failures = 5

    # Pick queries not yet used
    remaining_queries = [q for q in plan.search_queries if q not in used_queries]
    batch = remaining_queries[:4] if iteration == 0 else remaining_queries[:3]
    for q in batch:
        used_queries.append(q)

    browser: BrowserWorker = state["browser"]

    # ── Phase 1: Parallel Web Search ──────────────────────────────────────
    # Log each query as an action (same as before) then fire all searches at once.
    await repo.update_task(task, current_step=f"Searching {len(batch)} queries in parallel…")
    for q in batch:
        await repo.add_action(BrowserAction(
            task_id=state["task_id"], action_type=BrowserActionType.search,
            description=f"Searching: {q}", created_at=_utcnow(), metadata={},
        ))

    raw_results_list = await asyncio.gather(
        *[browser.web_search(q, limit=per_query_limit) for q in batch],
        return_exceptions=True,
    )

    # ── Phase 2: Parallel LLM Filtering ───────────────────────────────────
    # Filter each search result set concurrently; skip any that errored.
    valid_raw = [
        r for r in raw_results_list
        if not isinstance(r, Exception) and r
    ]
    filtered_batches = await asyncio.gather(
        *[_llm_filter_results(state["query"].text, raw) for raw in valid_raw],
        return_exceptions=True,
    )

    # Merge all candidates, deduplicate by URL
    seen_urls: set[str] = set()
    candidates = []
    for filtered in filtered_batches:
        if isinstance(filtered, Exception) or not filtered:
            continue
        for result in filtered[:per_query_limit]:
            if result.url not in seen_urls:
                seen_urls.add(result.url)
                candidates.append(result)

    # Apply the same domain / skip-result pre-filter as before so we only
    # actually open tabs for URLs that pass all quality gates.
    pre_filtered = []
    for result in candidates:
        if len(sources) + len(pre_filtered) >= target_sources:
            break
        cd = guess_domain(result.url)
        if _should_skip_result(state["query"].text, result.title, result.snippet, cd,
                               blocked_domains=blocked_domains):
            continue
        if domain_counter.get(cd, 0) >= max_per_domain:
            continue
        pre_filtered.append(result)

    # ── Phase 3: Parallel Page Visits ─────────────────────────────────────
    # Each URL gets its own browser tab (capture_page_on_new_tab), with a
    # semaphore limiting concurrent tabs to _VISIT_CONCURRENCY.
    await repo.update_task(
        task, current_step=f"Visiting {len(pre_filtered)} sources in parallel…"
    )
    semaphore = asyncio.Semaphore(_VISIT_CONCURRENCY)

    async def _visit_one(result, idx: int):
        """Open one tab, capture it, return (result, domain, page_or_None)."""
        async with semaphore:
            cd = guess_domain(result.url)
            ss = settings.screenshot_dir / f"{state['task_id']}_{len(sources) + idx}.png"
            try:
                page = await browser.capture_page_on_new_tab(
                    result.url, screenshot_file=ss,
                    blocked_domains=blocked_domains,
                )
                return (result, cd, page)
            except Exception as exc:
                return (result, cd, exc)

    visit_results = await asyncio.gather(
        *[_visit_one(r, i) for i, r in enumerate(pre_filtered)],
        return_exceptions=True,
    )

    # Process visit results sequentially so shared state (sources, domain_counter,
    # blocked_domains) is updated safely — asyncio is cooperative so no locks needed.
    access_failures = 0
    for vr in visit_results:
        if len(sources) >= target_sources:
            break
        if access_failures >= max_access_failures:
            logger.warning("Too many consecutive access failures, skipping remaining results")
            break
        if isinstance(vr, Exception):
            access_failures += 1
            continue

        result, cd, page_or_exc = vr
        if isinstance(page_or_exc, Exception):
            access_failures += 1
            blocked_domains.add(cd)
            await repo.add_error(ErrorEvent(
                task_id=state["task_id"],
                message=f"Failed: {result.url}: {page_or_exc}",
                recoverable=True, context={"url": result.url}, created_at=_utcnow(),
            ))
            continue

        page = page_or_exc
        if page is None:
            access_failures += 1
            blocked_domains.add(cd)
            await repo.add_error(ErrorEvent(
                task_id=state["task_id"],
                message=f"Page unusable (blocked/empty): {result.url}",
                recoverable=True,
                context={"url": result.url, "domain": cd, "reason": "access_failed"},
                created_at=_utcnow(),
            ))
            continue

        # Successful visit — update shared state
        access_failures = 0
        src = SourceItem(
            task_id=state["task_id"], source_type=SourceType.web,
            title=page.title or result.title, url=page.url,
            domain=guess_domain(page.url),
            snippet=result.snippet or page.text[:300],
            author=page.metadata.get("author"),
            published_at=page.metadata.get("published_time"),
            metadata=page.metadata,
        )
        sources.append(src)
        domain_counter[src.domain] = domain_counter.get(src.domain, 0) + 1
        evidence.extend(extractor.extract(
            task_id=state["task_id"], query=state["query"].text,
            url=page.url, html=page.html, text=page.text,
        ))
        await repo.add_action(BrowserAction(
            task_id=state["task_id"], action_type=BrowserActionType.navigate,
            description=f"Visited source {len(sources)}: {src.title}",
            url=page.url, screenshot_path=page.screenshot_path,
            created_at=_utcnow(), metadata={"domain": src.domain},
        ))
        await repo.update_task(task,
            current_step=f"Collected {len(sources)}/{target_sources} sources",
            latest_screenshot=page.screenshot_path)

    # ── Images (Dedicated Image Search) ──────────────────────────────────
    images = list(state.get("images") or [])
    if iteration == 0:
        await repo.update_task(task, current_step="Searching for relevant images")
        await repo.add_action(BrowserAction(
            task_id=state["task_id"], action_type=BrowserActionType.search,
            description="Searching for dedicated images", created_at=_utcnow(), metadata={},
        ))
        image_results = await browser.image_search(state["query"].text, limit=8)
        images.extend(image_results)

    # ── YouTube (only on first iteration) ─────────────────────────────────
    # Unchanged — sequential is fine here since we only call it once.
    if iteration == 0 and state["intent"].requires_youtube:
        await repo.update_task(task, current_step="Searching YouTube")
        await repo.add_action(BrowserAction(
            task_id=state["task_id"], action_type=BrowserActionType.search,
            description="Searching YouTube", created_at=_utcnow(), metadata={},
        ))
        candidates_yt = []
        for vq in plan.video_queries[:3]:
            for r in await browser.youtube_search(vq, limit=settings.max_video_results):
                item = youtube_extractor.enrich(task_id=state["task_id"], title=r.title, url=r.url)
                item = youtube_extractor.score(state["query"].text, item)
                candidates_yt.append(item)
        if candidates_yt:
            unique = youtube_extractor.deduplicate(candidates_yt)
            videos = sorted(unique, key=lambda v: v.rank_score, reverse=True)[:3]

    return {
        "sources": sources, "evidence": evidence, "videos": videos, "images": images,
        "search_iteration": iteration + 1,
        "used_queries": used_queries, "domain_counter": domain_counter,
        "blocked_domains": blocked_domains,
    }


async def evaluate_coverage(state: ResearchState) -> dict:
    """LLM judges if we have enough quality evidence or need more searching."""
    repo = TaskRepository(state["session"])
    task = await repo.get_task(state["task_id"])
    await repo.update_task(task, current_step="Evaluating evidence coverage")

    sources = state.get("sources") or []
    evidence = state.get("evidence") or []
    text_evidence = [e for e in evidence if e.evidence_type.value == "text"]
    high_conf = [e for e in text_evidence if (e.confidence or 0) > 0.6]

    # If we already have good coverage, skip the LLM call
    if len(sources) >= 8 and len(high_conf) >= 4:
        return {}

    # Ask LLM if there are gaps
    settings = get_settings()
    snippets = "\n".join(f"- {s.title}: {(s.snippet or '')[:100]}" for s in sources[:10])
    try:
        result = await llm_client.json_completion(
            model=settings.llm_model_worker,
            system_prompt=(
                "You evaluate research coverage. Given a query and collected sources, decide if "
                "more searching is needed. Return JSON: {\"sufficient\": true/false, "
                "\"gaps\": [\"missing topic 1\", ...], \"extra_queries\": [\"search query\", ...]}"
            ),
            user_prompt=(
                f"Query: {state['query'].text}\n"
                f"Sources collected ({len(sources)}):\n{snippets}\n"
                f"High-confidence evidence items: {len(high_conf)}"
            ),
        )
        if result and not result.get("sufficient", True):
            extras = result.get("extra_queries", [])
            if extras and isinstance(extras, list):
                plan = state["plan"]
                current_queries = list(plan.search_queries)
                for eq in extras[:3]:
                    if str(eq).strip() and str(eq) not in current_queries:
                        current_queries.append(str(eq))
                new_plan = plan.model_copy(update={"search_queries": current_queries})
                await repo.add_action(BrowserAction(
                    task_id=state["task_id"], action_type=BrowserActionType.search,
                    description=f"LLM found gaps: {', '.join(result.get('gaps', [])[:3])}",
                    created_at=_utcnow(), metadata={"gaps": result.get("gaps", [])},
                ))
                return {"plan": new_plan}
    except Exception:
        pass
    return {}


async def rank_sources(state: ResearchState) -> dict:
    repo = TaskRepository(state["session"])
    task = await repo.get_task(state["task_id"])
    await repo.update_task(task, status=TaskStatus.ranking, current_step="LLM ranking sources")
    ranker = SourceRanker()
    settings = get_settings()
    ranked = await ranker.llm_rank(state["query"].text, state.get("sources") or [])
    limit = max(settings.max_web_sources, 8)
    ranked = ranked[:limit]
    await repo.replace_sources(state["task_id"], ranked)
    await repo.replace_evidence(state["task_id"], state.get("evidence") or [])
    return {"ranked_sources": ranked}


async def verify_evidence(state: ResearchState) -> dict:
    repo = TaskRepository(state["session"])
    task = await repo.get_task(state["task_id"])
    await repo.update_task(task, status=TaskStatus.verifying, current_step="Verifying evidence")
    verifier = Verifier()
    confidence = verifier.verify(state["ranked_sources"], state.get("evidence") or [])
    await repo.add_action(BrowserAction(
        task_id=state["task_id"], action_type=BrowserActionType.verify,
        description=f"Confidence: {confidence.overall:.0%}",
        created_at=_utcnow(), metadata=confidence.model_dump(),
    ))
    return {"confidence": confidence}


async def compose_answer(state: ResearchState) -> dict:
    repo = TaskRepository(state["session"])
    task = await repo.get_task(state["task_id"])
    await repo.update_task(task, status=TaskStatus.composing,
        current_step="LLM composing research report")
    composer = AnswerComposer()
    answer = await composer.compose(
        query=state["query"].text,
        sources=state["ranked_sources"],
        evidence=state.get("evidence") or [],
        confidence=state["confidence"],
        videos=state.get("videos"),
        images=state.get("images"),
    )
    return {"answer": answer}


async def review_answer(state: ResearchState) -> dict:
    """LLM self-reviews the answer for quality and completeness."""
    repo = TaskRepository(state["session"])
    task = await repo.get_task(state["task_id"])
    await repo.update_task(task, current_step="LLM reviewing answer quality")
    settings = get_settings()
    answer = state["answer"]
    answer_preview = answer.direct_answer[:3000] if answer.direct_answer else ""

    try:
        result = await llm_client.json_completion(
            model=settings.llm_model_worker,
            system_prompt=(
                "Review a research answer for quality. Return JSON: "
                "{\"quality\": \"good\"|\"needs_improvement\", "
                "\"issues\": [\"issue 1\", ...], \"extra_queries\": [\"query\", ...]}"
            ),
            user_prompt=(
                f"Original query: {state['query'].text}\n"
                f"Answer preview (first 3000 chars):\n{answer_preview}\n"
                f"Supporting points: {len(answer.supporting_points)}\n"
                f"Citations: {len(answer.citations)}\n"
                f"Confidence: {state['confidence'].overall:.0%}"
            ),
        )
        if result and result.get("quality") == "needs_improvement":
            extras = result.get("extra_queries", [])
            if extras and isinstance(extras, list):
                plan = state["plan"]
                current_queries = list(plan.search_queries)
                for eq in extras[:2]:
                    if str(eq).strip() and str(eq) not in current_queries:
                        current_queries.append(str(eq))
                new_plan = plan.model_copy(update={"search_queries": current_queries})
                await repo.add_action(BrowserAction(
                    task_id=state["task_id"], action_type=BrowserActionType.compose,
                    description=f"Answer review: needs improvement — {', '.join(result.get('issues', [])[:2])}",
                    created_at=_utcnow(), metadata=result,
                ))
                return {"plan": new_plan, "review_iteration": state.get("review_iteration", 0) + 1}
    except Exception:
        pass

    return {"review_iteration": state.get("review_iteration", 0) + 1}


async def finalize(state: ResearchState) -> dict:
    repo = TaskRepository(state["session"])
    task = await repo.get_task(state["task_id"])
    answer = state["answer"]
    await repo.update_task(task, status=TaskStatus.completed, current_step="Completed", answer=answer)
    await repo.add_action(BrowserAction(
        task_id=state["task_id"], action_type=BrowserActionType.compose,
        description="Final research report composed",
        created_at=_utcnow(), metadata={"citations": answer.citations},
    ))
    return {}


# ── Conditional Edges ────────────────────────────────────────────────────

def should_search_more(state: ResearchState) -> str:
    """After evaluate_coverage: loop back to search or proceed to rank."""
    iteration = state.get("search_iteration", 0)
    sources = state.get("sources") or []
    evidence = state.get("evidence") or []

    # Hard stop: max iterations or enough sources
    if iteration >= 3 or len(sources) >= 12:
        return "rank_sources"

    # Sufficient evidence threshold: 6+ sources with 4+ high-quality evidence items
    text_evidence = [e for e in evidence if e.evidence_type.value == "text"]
    high_conf = [e for e in text_evidence if (e.confidence or 0) > 0.5]
    if len(sources) >= 8 and len(high_conf) >= 4:
        return "rank_sources"  # We have enough good content

    # If the plan got new queries from evaluate_coverage, search more
    used = set(state.get("used_queries") or [])
    remaining = [q for q in state["plan"].search_queries if q not in used]
    if remaining:
        return "search_and_collect"
    return "rank_sources"


def should_revise(state: ResearchState) -> str:
    """After review_answer: loop back to search more or finalize."""
    review_iter = state.get("review_iteration", 0)
    if review_iter >= 2:
        return "finalize"
    # If review added new queries, go back for more research
    used = set(state.get("used_queries") or [])
    remaining = [q for q in state["plan"].search_queries if q not in used]
    if remaining and state.get("search_iteration", 0) < 3:
        return "search_and_collect"
    return "finalize"


# ── Build the Graph ──────────────────────────────────────────────────────

def build_research_graph() -> StateGraph:
    g = StateGraph(ResearchState)
    g.add_node("analyze_query", analyze_query)
    g.add_node("build_plan", build_plan)
    g.add_node("search_and_collect", search_and_collect)
    g.add_node("evaluate_coverage", evaluate_coverage)
    g.add_node("rank_sources", rank_sources)
    g.add_node("verify_evidence", verify_evidence)
    g.add_node("compose_answer", compose_answer)
    g.add_node("review_answer", review_answer)
    g.add_node("finalize", finalize)

    g.set_entry_point("analyze_query")
    g.add_edge("analyze_query", "build_plan")
    g.add_edge("build_plan", "search_and_collect")
    g.add_edge("search_and_collect", "evaluate_coverage")
    g.add_conditional_edges("evaluate_coverage", should_search_more,
        {"search_and_collect": "search_and_collect", "rank_sources": "rank_sources"})
    g.add_edge("rank_sources", "verify_evidence")
    g.add_edge("verify_evidence", "compose_answer")
    g.add_edge("compose_answer", "review_answer")
    g.add_conditional_edges("review_answer", should_revise,
        {"search_and_collect": "search_and_collect", "finalize": "finalize"})
    g.add_edge("finalize", END)

    return g


_compiled_graph = build_research_graph().compile()


# ── LLM filter helper (used inside search node) ─────────────────────────

async def _llm_filter_results(query: str, results) -> list:
    if not results:
        return []
    settings = get_settings()
    summaries = [f"[{i}] {r.title} — {(r.snippet or '')[:150]}" for i, r in enumerate(results)]
    try:
        llm_result = await llm_client.json_completion(
            model=settings.llm_model_worker,
            system_prompt=(
                "Given a research query and search results, return JSON with key 'keep': "
                "array of result indices worth visiting. Exclude paywalled, shallow, or irrelevant results."
            ),
            user_prompt=f"Query: {query}\n\nResults:\n" + "\n".join(summaries),
        )
        if llm_result and isinstance(llm_result.get("keep"), list):
            filtered = [results[int(i)] for i in llm_result["keep"]
                        if isinstance(i, int) and 0 <= i < len(results)]
            if filtered:
                return filtered
    except Exception:
        pass
    return list(results)


# ── Public Interface (unchanged) ─────────────────────────────────────────

class ResearchOrchestrator:
    """Drop-in replacement — same run(session, task_id) signature."""

    def __init__(self) -> None:
        self.settings = get_settings()

    async def run(self, session: AsyncSession, task_id: str) -> None:
        repo = TaskRepository(session)
        task = await repo.get_task(task_id)
        if not task:
            return

        browser = None
        try:
            browser = BrowserWorker(task_id=task_id)
            await browser.__aenter__()

            initial_state: ResearchState = {
                "task_id": task_id,
                "session": session,
                "browser": browser,
                "search_iteration": 0,
                "review_iteration": 0,
                "blocked_domains": set(),
            }
            await _compiled_graph.ainvoke(initial_state)

        except Exception as exc:
            logger.exception("Task failed")
            task = await repo.get_task(task_id)
            if task:
                await repo.update_task(task, status=TaskStatus.failed, current_step="Failed")
                await repo.add_error(ErrorEvent(
                    task_id=task_id, message=str(exc), recoverable=True,
                    context={}, created_at=_utcnow(),
                ))
        finally:
            screencast_stream.clear(task_id)
            if browser:
                await browser.__aexit__(None, None, None)
            await session.close()


class TaskRunner:
    def __init__(self) -> None:
        self._running: dict[str, asyncio.Task] = {}

    def track(self, task_id: str, task: asyncio.Task) -> None:
        self._running[task_id] = task
        task.add_done_callback(lambda _: self._running.pop(task_id, None))


task_runner = TaskRunner()
