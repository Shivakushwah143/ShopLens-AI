from app.agent.planner import ResearchPlanner
from app.agent.intake import QueryIntake
from app.models.schemas import QueryIntent, ResearchMode, SourceItem, SourceType, UserQuery
from app.ranking.source_ranker import SourceRanker


async def test_planner_builds_plan():
    planner = ResearchPlanner()
    plan = await planner.build_plan(
        UserQuery(text="What is retrieval augmented generation and when should it be used?"),
        QueryIntent(
            mode=ResearchMode.web,
            topic="retrieval augmented generation",
            subtopics=["benefits", "tradeoffs"],
            requires_youtube=False,
        ),
    )
    assert plan.search_queries
    assert plan.source_limit >= 1


def test_intake_normalizes_non_enum_mode_and_scalar_subtopics():
    intake = QueryIntake()
    payload = intake._normalize_intent_payload(
        {
            "mode": "comparison",
            "topic": "RAG vs long-context prompting",
            "subtopics": "tradeoffs; evaluation",
            "requires_youtube": "yes",
            "answer_format": "",
        },
        "Compare retrieval augmented generation and long-context prompting, then recommend one useful YouTube explainer.",
    )
    assert payload["mode"] == "web"
    assert payload["subtopics"] == ["tradeoffs", "evaluation"]
    assert payload["requires_youtube"] is True
    assert payload["answer_format"] == "direct_answer"


def test_planner_normalizes_string_fields_from_llm():
    planner = ResearchPlanner()
    payload = planner._normalize_plan_payload(
        {
            "objective": "Compare two approaches",
            "search_queries": "retrieval augmented generation vs long context prompting",
            "video_queries": "RAG explainer|long context prompting explainer",
            "subquestions": "When does each approach win?",
            "source_limit": "7",
            "stopping_criteria": "enough evidence;clarity and accuracy",
        }
    )
    assert isinstance(payload["search_queries"], list)
    assert payload["source_limit"] == 7
    assert payload["stopping_criteria"] == ["enough evidence", "clarity and accuracy"]


def test_ranker_prefers_authoritative_relevant_sources():
    ranker = SourceRanker()
    ranked = ranker.rank(
        "retrieval augmented generation",
        [
            SourceItem(
                task_id="1",
                source_type=SourceType.web,
                title="Wikipedia entry about cats",
                url="https://wikipedia.org/wiki/Cat",
                domain="wikipedia.org",
                snippet="Domestic cat overview",
            ),
            SourceItem(
                task_id="1",
                source_type=SourceType.web,
                title="What is retrieval augmented generation?",
                url="https://docs.example.edu/rag",
                domain="docs.example.edu",
                snippet="Retrieval augmented generation combines search with generation.",
            ),
        ],
    )
    assert ranked[0].title == "What is retrieval augmented generation?"
