# Manualagerie — Architecture & Execution Plan

Upload every manual in the house once, then ask questions like *"What size is the hose connector on my dishwasher?"*. The assistant picks the right manual, searches inside it, and answers with page citations you can click.

---

## 1. Constraints & key decisions

| Concern | Decision | Why |
|---|---|---|
| Hosting | One container, one persistent volume mounted at `/data` | What the host provides |
| Database | SQLite file at `/data/manualagerie.db`, WAL mode | No managed DB. Data is small: a household has maybe 50–300 manuals |
| Search | SQLite **FTS5** (BM25), with **sqlite-vec** + local embeddings added in Phase 4 | Keyword search handles model numbers, part numbers and units ("3/4 in", "GHT") well. Vectors add recall on paraphrases later without any new service |
| LLM | OpenRouter through its OpenAI-compatible API; default model `deepseek-flash-4.1-1017`, overridable by env | As requested |
| Retrieval style | **Agentic tool loop**: the model sees the manual catalog, then calls `search_manuals` / `read_pages` | Matches the "find the dishwasher manual, then look inside it" flow, and lets the model retry with different wording |
| Backend | Python 3.12 + FastAPI + uvicorn, managed with `uv` | Python has the strongest PDF/OCR tooling (PyMuPDF, Tesseract) |
| Frontend | Server-rendered Jinja templates + HTMX + a small vanilla-JS chat client using SSE streaming | No separate SPA build. One process, one image |
| Background work | In-process worker thread that reads a `jobs` table in SQLite | No Redis or queue service. Jobs survive restarts because they live in the DB |
| Auth | Per-person accounts within one household (scrypt hashes, signed session cookie); `APP_PASSWORD` only bootstraps the first admin | The site is on the public internet. Data stays shared by the household; accounts record who asked and who did what |
| Scale | **Exactly one replica** | SQLite on a volume can't be shared across replicas. Note this in the deploy docs |

> ⚠️ **Model slug:** OpenRouter IDs are usually `vendor/model`, e.g. `deepseek/...`. Check the exact slug for `deepseek-flash-4.1-1017` on openrouter.ai/models, and confirm the model supports **tool calling** there. If it doesn't, the app falls back to the non-agentic pipeline described in §5.4.

---

## 2. System overview

```
                ┌────────────────────────── container ──────────────────────────┐
 Browser ──────►│ FastAPI (uvicorn, 1 worker)                                    │
  - Library     │   ├─ /auth        password login → signed cookie               │
  - Upload      │   ├─ /manuals     upload, list, edit metadata, view PDF@page   │
  - Chat (SSE)  │   ├─ /chat        conversations, SSE stream of agent loop      │
                │   └─ ingest worker thread  (polls jobs table)                  │
                │         extract text → OCR fallback → chunk → FTS index        │
                │         → LLM metadata extraction                              │
                │                                                                │
                │  /data (persistent volume)                                     │
                │   ├─ manualagerie.db   (SQLite: metadata, pages, chunks, FTS,  │
                │   │                     chats, jobs)                            │
                │   ├─ files/<sha256>.pdf (original uploads)                     │
                │   └─ backups/          (nightly VACUUM INTO snapshots)         │
                └───────────────────────────────┬────────────────────────────────┘
                                                │ HTTPS
                                                ▼
                                      OpenRouter (chat + tools)
```

---

## 3. Data model (SQLite)

```sql
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE manuals (
  id            INTEGER PRIMARY KEY,
  sha256        TEXT NOT NULL UNIQUE,          -- de-dupe uploads
  filename      TEXT NOT NULL,
  title         TEXT,                          -- "Bosch 800 Series Dishwasher – Installation Instructions"
  device_name   TEXT,                          -- user-facing grouping: "Kitchen dishwasher"
  brand         TEXT,
  model_numbers TEXT,                          -- comma-separated, e.g. "SHPM88Z75N, SHP88PZ55N"
  category      TEXT,                          -- dishwasher, furnace, water heater, router, ...
  doc_type      TEXT,                          -- user_manual | installation | quick_start | parts | warranty | other
  room          TEXT,                          -- optional, user-entered
  tags          TEXT,                          -- free-form, comma-separated: "downstairs AC, shed, rental"
  summary       TEXT,                          -- 2–3 sentence LLM summary
  notes         TEXT,                          -- free-form user notes ("bought 2023, Costco")
  page_count    INTEGER,
  status        TEXT NOT NULL DEFAULT 'queued',-- queued | processing | ready | error
  error         TEXT,
  created_at    TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE pages (
  manual_id  INTEGER NOT NULL REFERENCES manuals(id) ON DELETE CASCADE,
  page_no    INTEGER NOT NULL,                 -- 1-based, matches PDF viewer #page=N
  text       TEXT NOT NULL,
  lang       TEXT,                             -- detected language; multilingual manuals are common
  ocr        INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (manual_id, page_no)
);

CREATE TABLE chunks (
  id         INTEGER PRIMARY KEY,
  manual_id  INTEGER NOT NULL REFERENCES manuals(id) ON DELETE CASCADE,
  page_start INTEGER NOT NULL,
  page_end   INTEGER NOT NULL,
  heading    TEXT,                             -- nearest section heading, when one can be detected
  lang       TEXT,
  text       TEXT NOT NULL
);

CREATE VIRTUAL TABLE chunks_fts USING fts5(
  heading, text,
  content='chunks', content_rowid='id',
  tokenize = 'porter unicode61 remove_diacritics 2'  -- "3/4", "GHT-3" are matched as phrase queries
);
-- plus triggers that keep chunks_fts in sync with chunks on insert/delete/update

CREATE TABLE jobs (
  id         INTEGER PRIMARY KEY,
  manual_id  INTEGER NOT NULL REFERENCES manuals(id) ON DELETE CASCADE,
  kind       TEXT NOT NULL,                    -- ingest | reindex | embed
  status     TEXT NOT NULL DEFAULT 'queued',   -- queued | running | done | error
  attempts   INTEGER NOT NULL DEFAULT 0,
  error      TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE conversations (
  id         INTEGER PRIMARY KEY,
  title      TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE messages (
  id              INTEGER PRIMARY KEY,
  conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  role            TEXT NOT NULL,               -- user | assistant | tool
  content         TEXT,
  tool_calls      TEXT,                        -- JSON from the model
  tool_call_id    TEXT,
  citations       TEXT,                        -- JSON [{manual_id, page}] for the UI
  model           TEXT,
  created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE notes (                           -- household facts that aren't in any manual
  id         INTEGER PRIMARY KEY,
  title      TEXT NOT NULL,                    -- "Upstairs AC filters"
  body       TEXT,                             -- "16x25x1, MERV 11. Two of them."
  tags       TEXT,                             -- same free-form tags as manuals
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Phase 4
-- CREATE VIRTUAL TABLE chunks_vec USING vec0(chunk_id INTEGER PRIMARY KEY, embedding float[384]);
```

Schema migrations: numbered `.sql` files in `app/migrations/`, applied at startup and tracked with `PRAGMA user_version`. No ORM. Use plain `sqlite3` behind a thin repository module.

---

## 4. Ingestion pipeline

Triggered by an upload. Runs on the worker thread, one job at a time (fine for a household).

1. **Receive upload** (`POST /manuals`, multipart, several files at once). Stream the file to `/data/tmp`, compute sha256, and move it to `/data/files/<sha256>.pdf`. If that sha256 already exists, return the existing manual. Insert a `manuals` row (`queued`) and a `jobs` row.
   - Accepted inputs: PDF. Phase 3 adds JPG/PNG/HEIC photos of paper manuals, converted into a single PDF with `img2pdf`.
   - Size limit set by `MAX_UPLOAD_MB` (default 100).
2. **Extract text per page** with PyMuPDF (`page.get_text("text", sort=True)`). Keep layout order so tables and spec lists stay readable.
3. **OCR fallback:** if a page yields fewer than ~40 characters of text, render it at 300 dpi and OCR it with Tesseract (`pytesseract`, `eng` plus whatever languages `OCR_LANGS` lists). Set `pages.ocr = 1`.
4. **Language tag** each page with `langdetect`/`lingua`. Many manuals repeat everything in EN/FR/ES. With `STRIP_OTHER_LANGS` (default on), paragraphs confidently detected as outside `PREFERRED_LANGS` (default `en`) are dropped before chunking, so results aren't three copies of the same paragraph and the LLM doesn't read translations. Page numbers are unchanged, and the original PDF keeps everything. A manual with no preferred-language text is kept whole. Search still ranks `PREFERRED_LANGS` first for whatever remains.
5. **Chunk:** split by page, then by heading-like lines (ALL CAPS, numbered sections, larger font size taken from PyMuPDF `dict` spans), aiming for ~250–500 words with ~15% overlap. A chunk may span at most 2 pages. Record `page_start`, `page_end` and `heading`.
6. **Index:** insert the chunks; triggers fill FTS5.
7. **Metadata extraction (LLM):** send the filename, the first ~4 pages and any table-of-contents text to the model, with a JSON-schema response format, to get `{title, brand, model_numbers[], category, doc_type, device_name_suggestion, summary}`. Pre-fill the manual's fields with the result; the user can edit them in the UI. If the call fails, the manual is still `ready`, just with sparse metadata.
8. Mark the manual `ready`, or `error` with a message; the UI offers a Retry button.

The worker resets `running` jobs back to `queued` on startup (crash recovery) and caps `attempts` at 3.

---

## 5. Chat / agent design

### 5.1 System prompt (built per request)

- Role: "You answer questions about the user's home appliances and devices using ONLY their uploaded manuals. Always cite manual and page. If the manuals don't say, say so — do not guess specs."
- A **compact catalog** of every `ready` manual, one line each, grouped by `device_name`:
  `[12] Kitchen dishwasher — Bosch SHPM88Z75N — Installation Instructions (48 pp) (tags: under the counter, rental) — summary…`
  The user's tags, device name, room and notes are how the model maps everyday wording ("my downstairs AC") onto the right manual when two are similar, so they lead the line rather than trailing the summary.
  At about 300 manuals this is ~15k tokens; past that, swap the inline catalog for a `list_manuals(query)` tool (the code path exists from day one and is switched by catalog size).
- The **household notes**, in full, listed before the catalog: `[note #3] Upstairs AC filters (tags: upstairs AC)` followed by the indented body. They are the household's own facts, so the model checks them first, cites them as `[note #3]`, and prefers them over a manual's generic options for what is actually installed. Past `NOTES_INLINE_CHARS` (20k) only titles are inlined and `search_notes` returns the bodies.

### 5.2 Tools exposed to the model

| Tool | Args | Returns |
|---|---|---|
| `search_manuals` | `query: str`, `manual_ids?: int[]`, `limit?: int=8` | Top chunks: `{chunk_id, manual_id, manual_title, pages, heading, snippet}`. FTS5 BM25 with a heading boost; Phase 4 merges in vector results with RRF |
| `read_pages` | `manual_id: int`, `start_page: int`, `end_page: int` (max 4 pages) | Full page text. Used for spec tables and diagrams' captions that chunking split up |
| `list_manuals` | `query?: str` | Catalog rows. Only needed when the catalog isn't inlined |
| `search_notes` | `query: str` | Full text of household notes matching by tags, title or body. Only needed when the notes are inlined as titles only |

FTS query building: the model's query is sanitized (FTS5 syntax characters are escaped), then run as an OR of terms with phrase boosts. Model numbers are matched exactly.

### 5.3 Loop

1. Append the user message and call OpenRouter with `stream=true` and the tool list.
2. If the model returns tool calls, execute them locally, append the results, and call again. Cap at **6 tool rounds**; at the cap, force a final answer by calling with `tool_choice="none"`.
3. Stream final-answer tokens to the browser over SSE. Also send `tool` status events so the UI can show "Searching *Bosch Installation Instructions* for 'drain hose diameter'…".
4. Parse `[#12 p.34]`-style citations from the answer into `messages.citations`. The UI renders each one as a link to `/manuals/12/file#page=34`, which opens the stored PDF at that page.
5. Persist every message, tool calls included, so a conversation can be continued.

Example trace for *"What is the hose size connector on my dishwasher?"*:
catalog shows `[12] Kitchen dishwasher – Installation` and `[13] Kitchen dishwasher – Use & Care` → `search_manuals("drain hose connection diameter inlet fitting", [12,13])` → hit on p.21 → `read_pages(12, 21, 22)` → answer: "Water inlet takes a 3/8" compression fitting; drain hose fits 5/8"–1" connections [#12 p.21]".

### 5.4 Fallback for models without tool calling

When `LLM_TOOLS=false`, or OpenRouter reports the model doesn't support tools:
(a) a short LLM call rewrites the question into search queries and picks likely manual IDs from the catalog → (b) run `search_manuals` → (c) one answer call with the top ~10 chunks in context. The same citations and UI apply.

### 5.5 OpenRouter client

- `openai` Python SDK with `base_url="https://openrouter.ai/api/v1"`, headers `HTTP-Referer` (`APP_URL`) and `X-Title: Manualagerie`.
- Timeouts and 2 retries with backoff on 429/5xx. Log token usage per message.
- Models are set by `OPENROUTER_MODEL` (chat) and `OPENROUTER_METADATA_MODEL` (ingest metadata; defaults to the chat model).

---

## 6. Web UI

| Page | Contents |
|---|---|
| `/login` | Password field |
| `/` (Chat) | Conversation list on the left, chat pane on the right, streaming answers, citation chips, "searching…" tool status lines |
| `/manuals` | Drag-and-drop multi-upload; table of manuals with status badges (queued/processing/ready/error) refreshed by HTMX polling; filter by device/category |
| `/notes` | Household notes (title, text, tags) that aren't tied to a manual: add, filter, edit, delete. The chat cites them as `[note #N]` |
| `/manuals/{id}` | Editable metadata (device name, brand, model numbers, room, tags, notes), page count, Re-index / Delete buttons, embedded PDF viewer (browser native `<embed>` / `<iframe>` with `#page=N`) |

Mobile-friendly layout, since the phone is what you'll have in hand standing in front of the dishwasher.

---

## 7. Configuration (env vars)

| Var | Default | Notes |
|---|---|---|
| `OPENROUTER_API_KEY` | — (required) | |
| `OPENROUTER_MODEL` | `deepseek/deepseek-v4.1-flash-1017` | Chat model |
| `OPENROUTER_METADATA_MODEL` | = `OPENROUTER_MODEL` | |
| `LLM_TOOLS` | `true` | `false` forces the fallback pipeline |
| `APP_PASSWORD` | — (required) | Household login |
| `SECRET_KEY` | — (required) | Cookie signing |
| `DATA_DIR` | `/data` | Volume mount |
| `APP_URL` | — | OpenRouter referer header, Secure cookies, absolute share links |
| `MAX_UPLOAD_MB` | `100` | |
| `OCR_LANGS` | `eng` | Tesseract language packs; the image must include them |
| `PREFERRED_LANGS` | `en` | Search language filter; languages kept at ingest |
| `STRIP_OTHER_LANGS` | `true` | Drop other-language paragraphs at ingest |
| `BACKUP_KEEP` | `7` | Nightly snapshots kept |

---

## 8. Repository layout

```
manualagerie/
├─ pyproject.toml / uv.lock
├─ Dockerfile
├─ .env.example
├─ app/
│  ├─ main.py              # FastAPI app factory, startup: migrations, worker, backup scheduler
│  ├─ config.py            # pydantic-settings
│  ├─ db.py                # connection mgmt (WAL, busy_timeout), migrations runner
│  ├─ migrations/0001_init.sql
│  ├─ auth.py
│  ├─ repo/                # manuals.py, chunks.py, chat.py — SQL lives here
│  ├─ ingest/
│  │  ├─ worker.py         # job loop
│  │  ├─ extract.py        # PyMuPDF + OCR fallback
│  │  ├─ chunk.py
│  │  ├─ lang.py
│  │  └─ metadata.py       # LLM metadata extraction
│  ├─ search/fts.py        # query sanitizing + BM25 search (+ vector.py in Phase 4)
│  ├─ llm/
│  │  ├─ client.py         # OpenRouter wrapper, streaming, retries
│  │  ├─ prompts.py
│  │  ├─ tools.py          # tool schemas + dispatch
│  │  └─ agent.py          # tool loop, fallback pipeline, citation parsing
│  ├─ routes/              # auth.py, manuals.py, chat.py (SSE), health.py
│  ├─ templates/           # Jinja + HTMX
│  └─ static/              # chat.js, app.css
└─ tests/
   ├─ fixtures/            # small sample PDFs: text-based, scanned, multilingual
   ├─ test_extract.py, test_chunk.py, test_fts.py, test_agent.py (mocked LLM)
   └─ evals/questions.yaml # real-manual Q&A pairs for manual/CI-optional eval
```

---

## 9. Container & deployment

- **Image:** `python:3.12-slim` + apt `tesseract-ocr tesseract-ocr-eng` (+ other language packs as needed). `uv sync --frozen`. Run as a non-root user that owns `/data`.
- **Command:** `uvicorn app.main:app --host 0.0.0.0 --port 8080 --workers 1`. One worker, because the ingest thread and SQLite writer live in that process.
- **Volume:** mount at `/data`. Create `files/`, `tmp/` and `backups/` on startup.
- **Health:** `GET /healthz` checks that the DB opens and the data dir is writable.
- **Backups:** at startup and every 24 h, `VACUUM INTO /data/backups/manualagerie-YYYYMMDD.db` and prune down to `BACKUP_KEEP`. The originals in `files/` can always rebuild the index (`reindex` job), so the DB is the only thing that needs snapshots. Optionally sync `/data` off-box later.
- **Replicas:** 1. Rolling deploys must stop the old container before the new one mounts the volume, or at minimum never run two writers against it at once.
- **SQLite settings:** `journal_mode=WAL`, `busy_timeout=5000`, `synchronous=NORMAL`, `foreign_keys=ON`. Open one connection per request/thread; the worker gets its own.

---

## 10. Execution plan (phased)

Each phase ends with something runnable and its own acceptance check.

### Phase 0 — Skeleton (½ day)
- [ ] `uv init`; add deps: fastapi, uvicorn, jinja2, python-multipart, pydantic-settings, itsdangerous, pymupdf, pytesseract, pillow, openai, langdetect, pytest, httpx.
- [ ] `config.py`, `db.py` with the migrations runner, `0001_init.sql` (§3) including the FTS triggers.
- [ ] Password login + session middleware; `/healthz`.
- [ ] Dockerfile + `.env.example`; `docker run -v $(pwd)/data:/data` boots.
- **Done when:** the container starts, you can log in, and the DB file appears on the volume with the schema applied.

### Phase 1 — Ingestion & library (1–2 days)
- [ ] Upload route (multi-file, sha256 dedupe, size limit) → file stored, manual and job rows created.
- [ ] Worker thread: extract → OCR fallback → language tag → chunk → FTS index → status updates; crash recovery and retry.
- [ ] LLM metadata extraction with JSON output and graceful failure.
- [ ] `/manuals` list with live status, `/manuals/{id}` detail with edit / delete / re-index and the PDF viewer at `#page=N`.
- [ ] Tests: extraction on text and scanned fixtures, chunk page ranges, FTS triggers, dedupe.
- **Done when:** uploading 5 real manuals (including one scanned) brings all of them to `ready`, with sensible auto-filled metadata, and the PDFs open at a chosen page.

### Phase 2 — Search & agentic chat (1–2 days)
- [ ] `search/fts.py`: query sanitizing, BM25 with heading weight, `manual_ids` and language filters, snippets.
- [ ] Debug page `/search?q=` for checking retrieval quality without the LLM.
- [ ] `llm/client.py` (streaming, retries) and `llm/tools.py` (`search_manuals`, `read_pages`, `list_manuals`).
- [ ] `llm/agent.py`: system prompt with catalog, tool loop (max 6 rounds), citation parsing, message persistence.
- [ ] SSE chat endpoint and `chat.js`: token streaming, tool-status lines, citation chips linking to the PDF page.
- [ ] Conversation list, new / continue / delete conversation.
- [ ] Tests: agent loop against a scripted fake LLM (tool call → tool result → answer), citation parsing.
- **Done when:** "what is the hose size connector on my dishwasher" returns a correct, cited answer from your real manual, and clicking the citation opens the right page.

### Phase 3 — Hardening & quality (1 day)
- [ ] Fallback non-tool pipeline (§5.4), selected automatically or by `LLM_TOOLS=false`.
- [ ] Eval harness: `tests/evals/questions.yaml` (~20 real questions with expected manual and page); a script reports hit rate and answer correctness. Use it to tune chunk size, the heading boost and the prompt.
- [ ] Photo upload: images → PDF → OCR.
- [ ] Nightly backup task; startup cleanup of `/data/tmp`.
- [ ] Error UX: OpenRouter failures shown inline, upload errors, retry buttons.
- [ ] Basic login rate limiting; request logging with token usage.
- **Done when:** the eval set scores ≥ 85% on finding the right manual and page, and a backup file appears daily.

### Phase 4 — Semantic search (optional, ~1 day)
Do this only if the Phase 3 evals show misses caused by vocabulary mismatch (for example "hose size" vs "drain line outside diameter").
- [ ] Add `sqlite-vec` and `fastembed` (local ONNX `BAAI/bge-small-en-v1.5`, 384-dim, CPU, no extra API) and migration `0002_vec.sql`.
- [ ] `embed` job kind; backfill existing chunks.
- [ ] Hybrid search: FTS top-30 + vector top-30 merged with Reciprocal Rank Fusion.
- **Done when:** the eval hit rate goes up with no regressions.

### Later / nice-to-haves
- Show page thumbnails in citations (render PNG with PyMuPDF and cache it in `/data/thumbs`).
- "Add manual from URL" (fetch a manufacturer PDF).
- Per-device notes and maintenance reminders ("filter changed 2026-03").
- Export / import of the whole library (zip of `/data`).

---

## 11. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Default model slug wrong, or no tool support on OpenRouter | Verify in Phase 0; model is env-configurable; fallback pipeline in Phase 3 |
| Specs live in tables or diagrams that text extraction mangles | `read_pages` gives the model whole pages; layout-sorted extraction; citations let the user check the PDF visually |
| Multilingual manuals flood results with duplicates | Per-page language tags and the `PREFERRED_LANGS` filter |
| Model invents specs | Prompt says to use only the manuals, citations are required, and "not found in your manuals" is an allowed answer; the eval set checks for it |
| Volume loss / DB corruption | WAL, nightly `VACUUM INTO` snapshots, and a full rebuild possible from the original PDFs |
| Running two containers against one SQLite volume | Documented single-replica deploy; stop-then-start deploy strategy |
| Big scanned manuals make OCR slow | Background worker with progress (pages done / total) shown in the UI |
