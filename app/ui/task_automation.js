const form = document.getElementById("automation-form");
const queryInput = document.getElementById("automation-query");
const thread = document.getElementById("automation-thread");

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function appendUserMessage(text) {
  const markup = `<article class="message user-message"><p>${escapeHtml(text)}</p></article>`;
  thread.insertAdjacentHTML("beforeend", markup);
  thread.scrollTop = thread.scrollHeight;
}

function appendAssistantHtml(html) {
  const markup = `<article class="message assistant-message">${html}</article>`;
  thread.insertAdjacentHTML("beforeend", markup);
  thread.scrollTop = thread.scrollHeight;
}

function renderResult(payload) {
  const plan = payload?.plan;
  const output = payload?.output;
  const logs = payload?.execution?.logs || [];
  const results = output?.results || [];

  const planHtml = plan?.steps
    ? `<div class="automation-card" style="margin-bottom:12px;">
         <h3>Generated Action Plan</h3>
         <div class="stack">
           ${plan.steps
             .map(
               (s) => `
             <div style="display:flex; gap:12px; margin-bottom:6px; font-size:0.95em;">
               <span style="color:var(--primary); font-weight:600; min-width:24px;">${s.step}.</span>
               <span>${escapeHtml(formatActionSentence(s, plan))}</span>
             </div>`
             )
             .join("")}
         </div>
       </div>`
    : "";

  function formatActionSentence(step, plan) {
    const { action, params } = step;
    switch (action) {
      case "search":
        const idx = params.query_index ?? 0;
        const q = plan.search_queries?.[idx]?.text || "relevant items";
        return `Search the web for "${q}"`;
      case "open_result":
        if (params.url) return `Navigate to ${params.url}`;
        return `Open search result #${(params.result_index ?? 0) + 1}`;
      case "extract_list":
        return `Extract structured information from the page content`;
      case "extract_detail":
        return `Analyze the page for deep details and evidence`;
      case "click":
        return `Interact with the page by clicking "${params.selector}"`;
      case "type":
        return `Enter "${params.text}" into the "${params.selector}" field`;
      case "rank":
        return `Process and rank the collected results for accuracy`;
      case "stop":
        return `Task successfully completed`;
      default:
        return action;
    }
  }

  const logsHtml = logs.length
    ? `<div class="automation-card" style="margin-bottom:12px;">
         <h3>Execution Log</h3>
         <div class="code-like">${escapeHtml(
           logs
             .map((l) => `#${l.step} ${l.action} ${l.ok ? "OK" : "FAIL"} — ${l.message}`)
             .join("\n")
         )}</div>
       </div>`
    : "";

  const resultsHtml = results.length
    ? `<div class="automation-card">
         <h3>Top Results</h3>
         <div class="stack">
           ${results
             .map(
               (r) => `
             <div class="stack-item" style="border-bottom: 1px solid var(--outline-variant); padding-bottom: 12px; margin-bottom: 12px;">
              <p><strong>${escapeHtml(r.name || r.source_domain || "Website")}</strong></p>
              ${r.source_domain ? `<p style="font-size:0.82em;color:var(--on-surface-variant); margin-top:2px;">Source: ${escapeHtml(r.source_domain)}</p>` : ""}
              ${(r.price != null || r.rating != null)
                ? `<p style="font-size:0.85em; margin-top:6px;">
                    ${r.price != null ? `<span>Price: ${escapeHtml(r.price)}</span>` : ""}
                    ${r.rating != null ? `<span>${r.price != null ? " | " : ""}Rating: ${escapeHtml(r.rating)}</span>` : ""}
                   </p>`
                : ""
              }
              ${r.snippet ? `<p style="font-size:0.85em;color:var(--on-surface-variant); margin-top:6px;">${escapeHtml(String(r.snippet).slice(0, 220))}</p>` : ""}
              ${ (r.link) ? `<a class="source-link" href="${escapeHtml(r.link)}" target="_blank">Open website</a>` : "" }
             </div>`
             )
             .join("")}
         </div>
       </div>`
    : `<div class="automation-card"><h3>Top Results</h3><p>No structured items extracted (site may block automation or require login).</p></div>`;

  const summaryHtml = output?.summary
    ? `<div class="automation-card" style="margin-bottom:12px;">
         <h3>Summary</h3>
         <p>${escapeHtml(output.summary)}</p>
         <p style="font-size:0.9em;color:var(--on-surface-variant)">${escapeHtml(output.reasoning || "")}</p>
       </div>`
    : "";

  appendAssistantHtml(summaryHtml + planHtml + logsHtml + resultsHtml);
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const query = queryInput.value.trim();
  if (!query) return;

  appendUserMessage(query);
  queryInput.value = "";
  queryInput.style.height = "auto";

  appendAssistantHtml(`<div class="automation-card"><p>Running task automation…</p></div>`);

  try {
    const response = await fetch("/api/automation/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query }),
    });
    const payload = await response.json();
    if (!response.ok) {
      appendAssistantHtml(
        `<div class="automation-card"><h3>Error</h3><p>${escapeHtml(payload?.detail || "Request failed")}</p></div>`
      );
      return;
    }
    renderResult(payload);
    saveToHistory(query, payload);
  } catch (e) {
    appendAssistantHtml(`<div class="automation-card"><h3>Error</h3><p>Network error.</p></div>`);
  }
});

function saveToHistory(query, payload) {
  const history = JSON.parse(localStorage.getItem("automation_history") || "[]");
  const entry = {
    id: "auto-" + Date.now(),
    type: "automation",
    query: query,
    timestamp: new Date().toISOString(),
    payload: payload
  };
  history.unshift(entry);
  localStorage.setItem("automation_history", JSON.stringify(history.slice(0, 50)));
}

// ── History loading ───────────────────────────────────────────────────
function handleHistoryLoad() {
  const urlParams = new URLSearchParams(window.location.search);
  const historyId = urlParams.get("historyId");
  if (!historyId) return;

  const history = JSON.parse(localStorage.getItem("automation_history") || "[]");
  const entry = history.find(h => h.id === historyId);
  if (entry) {
    appendUserMessage(entry.query);
    renderResult(entry.payload);
  }
}

handleHistoryLoad();

document.getElementById("new-automation-chat").addEventListener("click", () => {
  thread.innerHTML = `
    <article class="message assistant-message">
      <p>Describe a task (shopping, flights, comparisons). I will generate an action plan and attempt to execute it safely.</p>
    </article>
  `;
  queryInput.value = "";
  queryInput.focus();
});

