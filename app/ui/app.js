/* ===================================================================
   AI Research Agent — app.js (v6)
   Multi-task, Library, Image cards, Single-stream with blue glow
   =================================================================== */

const form = document.getElementById("query-form");
const queryInput = document.getElementById("query");
const chatThread = document.getElementById("chat-thread");

// ── Multi-task state ──────────────────────────────────────────────────
let currentChatTaskIds = [];   // task IDs in the current chat thread
let activeWebSockets = new Map(); // taskId -> WebSocket
let liveTasks = new Set();        // taskId set — currently streaming
let pollHandles = new Map();      // taskId -> intervalId
let allChats = [];                // Array of { id, label, taskIds, html }
let currentChatId = null;

// ── Startup ───────────────────────────────────────────────────────────
initNewChat(true); // first launch — creates a blank chat
loadLibraryFromServer();

// ── View switching (sidebar nav) ──────────────────────────────────────
function switchView(view) {
  document.querySelectorAll(".nav-item[data-view]").forEach(n => n.classList.remove("active"));
  const navItem = document.querySelector(`.nav-item[data-view="${view}"]`);
  if (navItem) navItem.classList.add("active");
  document.getElementById("view-research").style.display = (view === "research") ? "flex" : "none";
  document.getElementById("view-library").style.display  = (view === "library")  ? "flex" : "none";
  if (view === "library") renderLibrary();
}

document.querySelectorAll(".nav-item[data-view]").forEach(item => {
  item.addEventListener("click", () => {
    switchView(item.dataset.view);
  });
});

// Handle query params for view switching
const urlParams = new URLSearchParams(window.location.search);
const initialView = urlParams.get("view") || "research";
switchView(initialView);

// ── New Chat button ───────────────────────────────────────────────────
document.getElementById("new-chat-btn").addEventListener("click", () => {
  // Save current chat before starting new one
  saveCurrentChat();
  initNewChat(false);
  // Switch to research view
  document.querySelectorAll(".nav-item[data-view]").forEach(n => n.classList.remove("active"));
  document.getElementById("nav-research").classList.add("active");
  document.getElementById("view-research").style.display = "flex";
  document.getElementById("view-library").style.display  = "none";
});

// ── Form submit ───────────────────────────────────────────────────────
form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const query = queryInput.value.trim();
  if (!query) return;

  appendUserMessage(query);

  const response = await fetch("/api/tasks", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query }),
  });
  const payload = await response.json();
  const taskId = payload.task_id;
  currentChatTaskIds.push(taskId);

  const taskHtml = buildTaskArticle(taskId);
  chatThread.insertAdjacentHTML("beforeend", taskHtml);

  queryInput.value = "";
  queryInput.style.height = "auto";

  startPolling(taskId);
  startScreencast(taskId);
  chatThread.scrollTop = chatThread.scrollHeight;
});

// ── Build task article HTML ───────────────────────────────────────────
function buildTaskArticle(taskId) {
  return `
    <article class="message assistant-message" id="task-msg-${taskId}">
      <div class="status-container" style="margin-bottom: 12px; display: flex; align-items: center; gap: 10px;">
        <span class="badge" id="status-badge-${taskId}">queued</span>
        <span id="current-step-${taskId}" style="font-size: 0.9em; color: var(--on-surface-variant);">Task created</span>
      </div>

      <div id="progress-log-${taskId}" class="feed" style="margin-bottom: 12px; font-size: 0.85em;"></div>
      <div id="error-box-${taskId}" class="error-box" hidden></div>

      <div id="browser-container-${taskId}" style="display:block; margin-bottom: 16px;">
        <div class="stream-header">
          <span class="material-symbols-outlined" style="font-size:18px;">cast</span>
          <span>Live Agent Browsing Stream:</span>
        </div>
        <div id="preview-grid-${taskId}" class="preview-grid">
          <div id="preview-empty-${taskId}" class="empty">
            <div class="loading-spinner"></div>
            <p>Initializing live browser stream...</p>
          </div>
        </div>
      </div>

      <div id="sources-list-${taskId}" class="stack" style="margin-bottom: 20px;"></div>
      <div id="answer-box-${taskId}" class="answer-box" style="margin-bottom: 20px;"></div>
      <div id="image-card-${taskId}" class="answer-box" style="margin-bottom: 20px;"></div>
      <div id="video-card-${taskId}" class="answer-box" style="margin-bottom: 20px;"></div>
      <div id="evidence-list-${taskId}" class="stack" style="margin-bottom: 20px;"></div>
    </article>
  `;
}

// ── Screencast (WebSocket) ────────────────────────────────────────────
function startScreencast(taskId) {
  if (activeWebSockets.has(taskId)) {
    const oldWs = activeWebSockets.get(taskId);
    oldWs.onclose = null;
    oldWs.close();
  }
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const wsUrl = `${protocol}//${window.location.host}/api/tasks/${taskId}/screencast`;
  const ws = new WebSocket(wsUrl);
  activeWebSockets.set(taskId, ws);

  ws.onopen = () => console.log("WS connected:", taskId);

  ws.onmessage = (event) => {
    try {
      const { page_id, data } = JSON.parse(event.data);
      if (!liveTasks.has(taskId)) {
        liveTasks.add(taskId);
      }
      const grid = document.getElementById(`preview-grid-${taskId}`);
      if (!grid) return;

      // Single-stream display: update the one visible frame
      let img = grid.querySelector(".stream-frame");
      if (!img) {
        const empty = document.getElementById(`preview-empty-${taskId}`);
        if (empty) empty.style.display = "none";
        img = document.createElement("img");
        img.className = "stream-frame";
        img.alt = "Live Browser Stream";
        grid.appendChild(img);
      }
      img.src = data;
      img.style.display = "block";
      // Ensure grid and container are visible
      grid.style.display = "flex";
      const container = document.getElementById(`browser-container-${taskId}`);
      if (container) container.style.display = "block";
    } catch (e) {
      console.error("Screencast error:", e);
    }
  };

  ws.onclose = () => {
    activeWebSockets.delete(taskId);
  };
}

// ── Polling ───────────────────────────────────────────────────────────
function startPolling(taskId) {
  if (pollHandles.has(taskId)) clearInterval(pollHandles.get(taskId));
  fetchTask(taskId);
  const handle = setInterval(() => fetchTask(taskId), 2500);
  pollHandles.set(taskId, handle);
}

async function fetchTask(taskId) {
  try {
    const response = await fetch(`/api/tasks/${taskId}`);
    if (!response.ok) return;
    const task = await response.json();
    renderTask(task.id, task);
    if (task.status === "completed" || task.status === "failed") {
      clearInterval(pollHandles.get(taskId));
      pollHandles.delete(taskId);
      liveTasks.delete(task.id);
      renderTask(task.id, task); // one last render
      saveCurrentChat(); // auto-save on completion
    }
  } catch (e) {
    // Network error — retry on next interval
  }
}

// ── Render Task ───────────────────────────────────────────────────────
function renderTask(taskId, task) {
  const statusBadge = document.getElementById(`status-badge-${taskId}`);
  const currentStep = document.getElementById(`current-step-${taskId}`);
  if (statusBadge) statusBadge.textContent = task.status;
  if (currentStep) currentStep.textContent = task.current_step;

  renderActions(taskId, task.actions || []);
  renderErrors(taskId, task.errors || []);
  renderPreview(taskId, task.actions || [], task.latest_screenshot);
  renderAnswer(taskId, task.answer);
  renderSources(taskId, task.sources || []);
  renderEvidence(taskId, task.evidence || []);
  renderVideo(taskId, task.answer?.videos || []);
  renderImages(taskId, task.answer?.images || []);
}

function renderActions(taskId, actions) {
  const progressLog = document.getElementById(`progress-log-${taskId}`);
  if (!progressLog) return;
  progressLog.innerHTML = actions
    .slice()
    .reverse()
    .map(
      (action) => `
        <div class="feed-item" style="padding: 8px 12px; margin-bottom: 4px;">
          <p><strong>${escapeHtml(action.action_type)}</strong> - ${escapeHtml(action.description)}</p>
        </div>
      `
    )
    .join("");
}

function renderPreview(taskId, actions, latestScreenshotPath) {
  const screenshots = Array.from(
    new Set(
      actions
        .map((action) => action.screenshot_path)
        .filter(Boolean)
        .map(normalizePath)
    )
  );

  const grid = document.getElementById(`preview-grid-${taskId}`);
  const container = document.getElementById(`browser-container-${taskId}`);
  const previewEmpty = document.getElementById(`preview-empty-${taskId}`);

  if (!screenshots.length && !latestScreenshotPath) {
    if (grid) grid.style.display = "none";
    if (previewEmpty) previewEmpty.style.display = "block";
    if (container) container.style.display = "block";
    return;
  }

  const isLive = liveTasks.has(taskId);
  const hasLiveFrames = grid && grid.querySelector(".stream-frame");

  if (isLive && hasLiveFrames) {
    if (grid) grid.style.display = "grid";
    if (container) container.style.display = "block";
    return;
  }

  // Historical fallback: show the latest screenshot
  if (grid) {
    grid.style.display = "grid";
    const latest = screenshots.at(-1) || normalizePath(latestScreenshotPath);
    if (latest) {
      let img = grid.querySelector(".stream-frame");
      if (!img) {
        img = document.createElement("img");
        img.className = "stream-frame screenshot-fallback";
        grid.appendChild(img);
      }
      if (!isLive || img.classList.contains("screenshot-fallback")) {
        img.src = `${latest}?t=${Date.now()}`;
        img.classList.add("screenshot-fallback");
        img.style.display = "block";
      }
      if (previewEmpty) previewEmpty.style.display = "none";
    }
  }
  if (container) container.style.display = "block";
}

function renderErrors(taskId, errors) {
  const errorBox = document.getElementById(`error-box-${taskId}`);
  if (!errorBox) return;
  if (!errors.length) {
    errorBox.hidden = true;
    errorBox.innerHTML = "";
    return;
  }
  const latest = errors[errors.length - 1];
  errorBox.hidden = false;
  errorBox.innerHTML = `
    <p><strong>Error Details</strong></p>
    <p>${escapeHtml(latest.message)}</p>
  `;
}

function renderAnswer(taskId, answer) {
  const answerBox = document.getElementById(`answer-box-${taskId}`);
  if (!answerBox) return;
  if (!answer) {
    answerBox.innerHTML = "";
    return;
  }
  const points = (answer.supporting_points || []).slice(0, 6);
  const citations = (answer.citations || []).slice(0, 8);
  answerBox.innerHTML = `
    <div class="direct-answer-html" style="font-size: 1.05em; margin-bottom: 16px;">${answer.direct_answer}</div>
    ${points.length
      ? `<ul style="margin-bottom: 16px;">${points.map((point) => `<li>${escapeHtml(point)}</li>`).join("")}</ul>`
      : ""
    }
    <p style="margin-bottom: 12px;"><strong>Confidence:</strong> ${Math.round((answer.confidence.overall || 0) * 100)}%</p>
    ${citations.length
      ? `<p style="font-size: 0.9em; line-height: 1.4;">${citations
        .map((citation) => `<a class="source-link" href="${citation}" target="_blank">${escapeHtml(citation)}</a>`)
        .join("<br/>")}</p>`
      : ""
    }
  `;
}

function renderSources(taskId, sources) {
  const sourcesList = document.getElementById(`sources-list-${taskId}`);
  if (!sourcesList) return;
  if (!sources.length) {
    sourcesList.innerHTML = "";
    return;
  }
  sourcesList.innerHTML = `
    <h3 style="margin-bottom: 8px; font-size: 1.1em; color: var(--primary);">Ranked Sources</h3>
    ` + sources
      .map(
        (source) => `
            <div class="stack-item" style="border-bottom: 1px solid var(--outline-variant); padding-bottom: 12px; margin-bottom: 12px; background: transparent; border-radius: 0;">
              <p><strong>${escapeHtml(source.title)}</strong></p>
              <p style="font-size: 0.85em;">${escapeHtml(source.domain)} | score ${Number(source.rank_score).toFixed(2)}</p>
              <p style="font-size: 0.9em; margin-top: 6px;">${escapeHtml(source.snippet || "")}</p>
              <a class="source-link" href="${source.url}" target="_blank">${escapeHtml(source.url)}</a>
            </div>
          `
      )
      .join("");
}

function renderEvidence(taskId, evidence) {
  const evidenceList = document.getElementById(`evidence-list-${taskId}`);
  if (!evidenceList) return;
  if (!evidence.length) {
    evidenceList.innerHTML = "";
    return;
  }
  evidenceList.innerHTML = `
    <h3 style="margin-bottom: 8px; font-size: 1.1em; color: var(--primary);">Evidence</h3>
    ` + evidence
      .slice(0, 10)
      .map(
        (item) => `
            <div class="stack-item" style="border-bottom: 1px solid var(--outline-variant); padding-bottom: 12px; margin-bottom: 12px; background: transparent; border-radius: 0;">
              <p style="font-size: 0.85em;"><strong>${escapeHtml(item.evidence_type)}</strong> | confidence ${Math.round((item.confidence || 0) * 100)}%</p>
              <p style="font-size: 0.9em; margin-top: 6px;">${escapeHtml(item.excerpt || item.content.slice(0, 300))}</p>
            </div>
          `
      )
      .join("");
}

function renderVideo(taskId, videos) {
  const videoCard = document.getElementById(`video-card-${taskId}`);
  if (!videoCard) return;
  if (!videos || !videos.length) {
    videoCard.innerHTML = "";
    return;
  }
  videoCard.innerHTML = `
    <h3 style="margin-bottom: 8px; font-size: 1.1em; color: var(--primary);">YouTube Recommendations</h3>
    ` + videos.map(video => {
    let videoId = "";
    try {
      const urlObj = new URL(video.url);
      if (urlObj.hostname.includes("youtube.com")) {
        videoId = urlObj.searchParams.get("v");
      } else if (urlObj.hostname.includes("youtu.be")) {
        videoId = urlObj.pathname.slice(1);
      }
    } catch (e) { }

    const imgTag = videoId ? `<img src="https://img.youtube.com/vi/${videoId}/hqdefault.jpg" alt="Video thumbnail" style="width: 120px; height: 90px; object-fit: cover; border-radius: 8px; margin-right: 12px; flex-shrink: 0;">` : '';

    return `
      <div class="stack-item" style="border-bottom: 1px solid var(--outline-variant); padding-bottom: 12px; margin-bottom: 12px; background: transparent; border-radius: 0; display: flex; align-items: flex-start;">
        ${imgTag}
        <div>
          <p><strong><a class="source-link" href="${video.url}" target="_blank" style="text-decoration: none;">${escapeHtml(video.title)}</a></strong></p>
          <p style="font-size: 0.85em;">${escapeHtml((video.reasons || []).join(" | "))}</p>
          ${video.transcript_excerpt ? `<p style="font-size: 0.9em; margin-top: 6px;">${escapeHtml(video.transcript_excerpt.slice(0, 320))}</p>` : ""}
        </div>
      </div>
      `;
  }).join("");
}

// ── Image Cards ───────────────────────────────────────────────────────
function renderImages(taskId, images) {
  const imageCard = document.getElementById(`image-card-${taskId}`);
  if (!imageCard) return;
  if (!images || !images.length) {
    imageCard.innerHTML = "";
    return;
  }
  imageCard.innerHTML = `
    <h3 style="margin-bottom: 12px; font-size: 1.1em; color: var(--primary);">Related Images</h3>
    <div class="image-grid">
      ${images.map(img => `
        <div class="image-card">
          <div class="image-card-thumb">
            <img src="${escapeHtml(img.src)}" alt="${escapeHtml(img.alt)}" loading="lazy"
                 onerror="this.parentElement.parentElement.style.display='none'" />
          </div>
          <div class="image-card-info">
            <p class="image-card-alt">${escapeHtml(img.alt)}</p>
            <a class="source-link" href="${escapeHtml(img.source_url)}" target="_blank" style="font-size: 0.75em;">
              ${escapeHtml(img.source_title || img.source_url)}
            </a>
          </div>
        </div>
      `).join("")}
    </div>
  `;
}

// ── Chat Management ───────────────────────────────────────────────────
function initNewChat(isFirst) {
  const id = "chat-" + Date.now();
  currentChatId = id;
  currentChatTaskIds = [];
  if (!isFirst) {
    // Clear thread
    chatThread.innerHTML = `
      <article class="message assistant-message">
        <p>Ask a research question to begin. I will plan, browse, verify, and answer with evidence.</p>
      </article>
    `;
  }
  queryInput.value = "";
  queryInput.style.height = "auto";
}

function saveCurrentChat() {
  if (!currentChatId) return;
  // Extract the first user message as label
  const firstUser = chatThread.querySelector(".user-message p");
  const label = firstUser ? firstUser.textContent.slice(0, 60) : "Untitled Research";
  // Check if this chat is already saved
  const existing = allChats.find(c => c.id === currentChatId);
  if (existing) {
    existing.html = chatThread.innerHTML;
    existing.label = label;
    existing.taskIds = [...currentChatTaskIds];
  } else if (currentChatTaskIds.length > 0) {
    allChats.unshift({ id: currentChatId, label, taskIds: [...currentChatTaskIds], html: chatThread.innerHTML });
  }
}

function loadChat(chatId) {
  const chat = allChats.find(c => c.id === chatId);
  if (!chat) return;
  // Save the current one first
  saveCurrentChat();
  // Restore
  currentChatId = chat.id;
  currentChatTaskIds = [...chat.taskIds];
  chatThread.innerHTML = chat.html;
  // Re-attach polling for any non-completed tasks
  for (const taskId of currentChatTaskIds) {
    if (!pollHandles.has(taskId)) {
      // Check if task is still running
      fetchTask(taskId).then(() => {
        // If still running, it'll auto-resume polling via fetchTask
      });
    }
  }
  // Switch to research view
  document.querySelectorAll(".nav-item[data-view]").forEach(n => n.classList.remove("active"));
  document.getElementById("nav-research").classList.add("active");
  document.getElementById("view-research").style.display = "flex";
  document.getElementById("view-library").style.display  = "none";
}

// ── Library View ──────────────────────────────────────────────────────
async function loadLibraryFromServer() {
  try {
    const resp = await fetch("/api/tasks");
    if (!resp.ok) return;
    const tasks = await resp.json();
    // Build library entries from server-side task records
    for (const t of tasks) {
      const existsInChats = allChats.some(c => c.taskIds.includes(t.id));
      if (!existsInChats) {
        allChats.push({
          id: "server-" + t.id,
          label: t.query.slice(0, 60),
          taskIds: [t.id],
          html: null, // will be rebuilt on click
          serverTask: t,
        });
      }
    }
  } catch (e) {
    // Startup error — not critical
  }
}

function renderLibrary() {
  const listEl = document.getElementById("library-list");
  if (!listEl) return;
  
  // Merge in-memory chats + server tasks + automation history + deals history
  const automationHistory = JSON.parse(localStorage.getItem("automation_history") || "[]");
  const dealsHistory = JSON.parse(localStorage.getItem("deals_history") || "[]");
  const entries = [
    ...allChats,
    ...automationHistory.map(h => ({
      id: h.id,
      label: h.query.slice(0, 60),
      type: "automation",
      timestamp: h.timestamp,
      payload: h.payload
    })),
    ...dealsHistory.map(h => ({
      id: h.id,
      label: h.query.slice(0, 60),
      type: "deals",
      timestamp: h.timestamp,
      payload: h.payload
    }))
  ];

  if (!entries.length) {
    listEl.innerHTML = `
      <div class="library-empty">
        <span class="material-symbols-outlined" style="font-size:48px;color:var(--muted);">auto_stories</span>
        <p>No past research or automation sessions yet.</p>
      </div>
    `;
    return;
  }

  // Sort by timestamp if available, else original order
  entries.sort((a, b) => {
    const tA = a.timestamp || 0;
    const tB = b.timestamp || 0;
    return tB > tA ? 1 : -1;
  });

  listEl.innerHTML = entries.map(chat => {
    let statusIcon = "history";
    let statusColor = "var(--muted)";
    
    if (chat.type === "automation") {
      statusIcon = "bolt";
      statusColor = "var(--primary)";
    } else if (chat.type === "deals") {
      statusIcon = "local_offer";
      statusColor = "var(--secondary)";
    } else if (chat.serverTask) {
      statusIcon = chat.serverTask.status === "completed" ? "check_circle" : chat.serverTask.status === "failed" ? "error" : "pending";
      statusColor = chat.serverTask.status === "completed" ? "var(--secondary)" : chat.serverTask.status === "failed" ? "var(--error)" : "var(--primary)";
    }

    return `
      <div class="library-card" onclick="loadChatOrTask('${chat.id}')">
        <div class="library-card-icon">
          <span class="material-symbols-outlined" style="color:${statusColor}">${statusIcon}</span>
        </div>
        <div class="library-card-info">
          <p class="library-card-title">${escapeHtml(chat.label)}</p>
          <p class="library-card-meta">${chat.type === "automation" ? "Task Automation" : chat.type === "deals" ? "Deals Tracker" : (chat.taskIds.length + " task(s)")}</p>
        </div>
        <span class="material-symbols-outlined" style="color:var(--muted);font-size:18px;">chevron_right</span>
      </div>
    `;
  }).join("");
}

// Make loadChatOrTask global for onclick
window.loadChatOrTask = function(chatId) {
  // Check deals history
  const dealsHistory = JSON.parse(localStorage.getItem("deals_history") || "[]");
  const dealsEntry = dealsHistory.find(h => h.id === chatId);
  if (dealsEntry) {
    window.location.href = `/ui/deals.html?historyId=${chatId}`;
    return;
  }

  // Check automation history
  const automationHistory = JSON.parse(localStorage.getItem("automation_history") || "[]");
  const autoEntry = automationHistory.find(h => h.id === chatId);
  if (autoEntry) {
    window.location.href = `/ui/task_automation.html?historyId=${chatId}`;
    return;
  }

  const chat = allChats.find(c => c.id === chatId);
  if (!chat) return;
  if (chat.html) {
    loadChat(chatId);
  } else if (chat.serverTask) {
    // Rebuild the chat from server data
    saveCurrentChat();
    currentChatId = chat.id;
    currentChatTaskIds = [...chat.taskIds];
    chatThread.innerHTML = `
      <article class="message user-message"><p>${escapeHtml(chat.serverTask.query)}</p></article>
      ${buildTaskArticle(chat.serverTask.id)}
    `;
    // Fetch and render the task data
    startPolling(chat.serverTask.id);
    // Switch to research view
    document.querySelectorAll(".nav-item[data-view]").forEach(n => n.classList.remove("active"));
    document.getElementById("nav-research").classList.add("active");
    document.getElementById("view-research").style.display = "flex";
    document.getElementById("view-library").style.display  = "none";
  }
};

// ── Utilities ─────────────────────────────────────────────────────────
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
  chatThread.insertAdjacentHTML("beforeend", markup);
  chatThread.scrollTop = chatThread.scrollHeight;
}

function normalizePath(rawPath) {
  if (!rawPath) return "";
  return rawPath.startsWith("/") ? rawPath : `/${rawPath.replaceAll("\\", "/")}`;
}
