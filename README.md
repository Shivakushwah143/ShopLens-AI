# 🧠 MultiAgent Browser AI System

A state-of-the-art, production-ready AI agent designed to autonomously navigate the web, synthesize information, and provide evidence-backed answers. Built with a "human-in-the-loop" transparency mindset, this agent doesn't just give answers—it shows you exactly how it found them.

---

## 🌟 Overview

The **General Browser AI Agent** is a multi-modal agentic system that leverages Playwright for web automation and Gemini for advanced reasoning. Unlike traditional chatbots that rely on pre-trained knowledge, this agent uses live browsing to access real-time data, verify facts across multiple sources, and produce comprehensive research reports.

### The "Research" Vertical
This version of the agent is specialized in **Deep Research**. It is optimized to:
- Navigate complex web layouts and bypass common bot-detection.
- Synthesize long-form reports (1000+ words) from multiple disparate sources.
- Retrieve high-quality imagery and video recommendations related to the query.
- Maintain a persistent "Library" of past research for future reference.

### The "Task Automation" Vertical
Focused on **Executing Actions**. It takes a high-level query, plans a sequence of browser actions, and executes them to achieve a goal.

### The "Deals Tracker" Vertical
Specialized in **E-commerce & Price Analysis**. It compares prices across multiple Indian retailers, filters out junk/accessories, and tracks price history.

---

## 🚀 How It Works: The Agentic Process

The agent follows a sophisticated, self-correcting loop to ensure accuracy and depth:

1.  **Intake & Intent Analysis**: The agent analyzes the user's query to determine the "Research Mode" (Web, Video, or Mixed) and generates a structured research plan.
2.  **Parallel Search & Discovery**:
    *   **Search APIs**: Hits Google (CSE) or Bing API for high-quality structured data.
    *   **Fallback Scraping**: If APIs fail, it uses a stealth Playwright worker to scrape search engines like Startpage or DuckDuckGo.
    *   **Image/Video Search**: Performs dedicated searches for relevant visual media.
3.  **Autonomous Browsing**: The agent opens multiple browser tabs in parallel to visit top-ranked sources. It extracts text, structured tables, and metadata while providing a **Live Stream** of its progress back to the user.
4.  **Evidence Extraction**: It filters out "noise" (ads, sidebars) and keeps only high-confidence evidence snippets related to the core query.
5.  **Ranking & Verification**: A dedicated ranking node scores sources based on authority, relevance, and completeness. Any conflicting information is flagged.
6.  **Synthesis (The Answerer)**: Finally, the agent composes a detailed response with full citations, embedded tables, relevant images, and video recommendations.

---

## ✨ Key Features

- **📺 Live Agent Browsing Stream**: Watch the agent work in real-time. A "magic" blue-glow interface displays the active browser frames as the agent visits sources.
- **🖼️ Dedicated Image Discovery**: Unlike basic scrapers, the agent uses dedicated image search logic to find high-relevance visuals and flashcards.
- **📚 Research Library**: Automatically saves every task, screenshot, and result, allowing you to build a personal knowledge base.
- **⚡ Parallel Execution**: Utilizes asynchronous workers to search and browse multiple sites simultaneously, drastically reducing research time without sacrificing quality.
- **🛡️ Evidence-First Logic**: Every statement made by the agent is backed by a clickable citation and extracted evidence snippet.
- **🔍 Multi-Engine Support**: Integrated support for Google Custom Search, Bing Search API, and multiple privacy-focused scraping fallbacks.

---

## 🛠️ Run Locally

### 1. Prerequisites & Environment
**Important**: Change directory to `Browser-Agent` before running any command.

```bash
# Create a virtual environment
py -3.11 -m venv .venv
.venv\Scripts\activate

# Install dependencies in editable mode
pip install -e .[dev]

# Install Playwright browsers
playwright install chromium
```

### 2. Configuration
Copy `.env.example` to `.env` and fill in your keys:
- `GEMINI_API_KEY`: Required for the brain (Planning & Synthesis).
- `GOOGLE_API_KEY` & `GOOGLE_CSE_ID`: Recommended for premium search results.
- `BING_API_KEY`: Optional alternative for search.

### 3. Start the Application

```bash
# Standard run (recommended for Windows & Database stability)
$env:APP_ENV='production'; .venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --app-dir .

# Alternative with auto-reload (for developers)
$env:APP_ENV='production'; .venv\Scripts\python.exe -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000 --app-dir .
```

4. Open `http://localhost:8000/` in your browser.

---

## 🏗️ Architecture

- **`app/agent`**: The "Brain" containing the LangGraph orchestrator, planning logic, and answer synthesis.
- **`app/agent/task_automation`**: The "Task Automation Agent" vertical for action-oriented workflows.
- **`app/agent/deals_tracker`**: The "Deals & Price Tracker" vertical for price extraction, comparison, and alerting.
- **`app/browser`**: The "Hands" managing Playwright instances, screencasting, and search execution.
- **`app/extraction`**: Specialized logic for pulling clean data from messy HTML and YouTube.
- **`app/ranking`**: Multi-dimensional scoring for sources and media relevance.
- **`app/ui`**: A modern, responsive frontend built for real-time observation.
- **`app/storage`**: SQLModel/SQLite persistence for tasks, actions, prices, and alerts.

---

## 💡 Developer Notes

*   **Models**: We recommend `Gemini 3 Flash` for high-speed planning and `Gemini 3 Pro` for complex final synthesis and reasoning.
*   **Database**: Uses SQLite by default. For production scaling, switch to PostgreSQL via the `DATABASE_URL` environment variable.
*   **Stealth**: The browser worker uses standard stealth headers to ensure high success rates on enterprise sites.

---

## ⚡ Task Automation Agent (New Vertical)

This repo includes a second vertical: **Task Automation Agent** — focused on *executing multi-step browser workflows* (not long-form research).

### UI
- **Research Agent**: open `http://localhost:8000/` (default)
- **Task Automation Agent**: open `http://localhost:8000/ui/task_automation.html`
  - You can also navigate to it from the left sidebar in the Research UI.

### API
Run a task automation workflow via:
- `POST /api/automation/run`

Example payload:

```json
{ "query": "Find laptops under 80000 with 16GB RAM" }
```

---

## 🏷️ Deals & Price Tracker Agent (New Vertical)

The **Deals & Price Tracker** is a specialized vertical designed to find the best prices across major e-commerce platforms (Amazon, Flipkart, Croma, etc.) and track price history.

### Key Features
- **URL-Based Extraction**: Paste a direct product URL to extract clean name, price, and rating.
- **Price Comparison**: Automatically finds the same product on other stores to compare prices.
- **Smart Filtering**: Built-in logic to strictly filter out accessories (covers, cables, etc.) and focus on the primary product.
- **Final Verdict**: Provides a "BEST DEAL" badge or suggests a better source with potential savings.
- **Price Tracking & Alerts**: Set a target price and get notified when it drops below your threshold.

### UI
- **Deals Tracker**: open `http://localhost:8000/ui/deals.html`
  - Accessible via the "Deals Tracker" link in the sidebar.

### API
Search or extract from URL via:
- `POST /api/deals/search`

Example payload (Query):
```json
{ "query": "iphone 15 under 70000" }
```

Example payload (URL):
```json
{ "url": "https://www.amazon.in/dp/B0CHX1W1XY" }
```
