# manualagerie
Manualagerie is a site where you upload all the random manuals in your house, and then you can ask an AI about them whenever you need them.

Ask *"What size is the hose connector on my dishwasher?"*. The assistant finds the right manual, searches inside it, and answers with page citations that open the PDF at that page. See [PLAN.md](PLAN.md) for the architecture.

## Run locally (Docker)

```bash
cp .env.example .env               # set APP_PASSWORD (for the first account) and OPENROUTER_API_KEY
docker build -t manualagerie .
docker run -d --name manualagerie --env-file .env -e OPENROUTER_API_KEY \
  -p 127.0.0.1:8765:8080 -v "$PWD/data:/data" manualagerie
open http://localhost:8765
```

## Develop without Docker

```bash
uv sync
DATA_DIR=./data APP_PASSWORD=dev uv run uvicorn app.main:app --reload --port 8765
uv run pytest
```

Scanned pages need `tesseract` on your PATH. It is already included in the Docker image.

Hovering or focusing a citation chip shows a thumbnail of the cited page, rendered with PyMuPDF on first use and cached under `/data/thumbs`.

## LLM backend: OpenRouter

Manualagerie has no model of its own. Every chat answer and the metadata extraction at upload time go through [OpenRouter](https://openrouter.ai), using its OpenAI-compatible API, so any model OpenRouter hosts can be the backend.

1. Create an API key at [openrouter.ai/keys](https://openrouter.ai/keys) and set it as `OPENROUTER_API_KEY`. Without it the app still starts and serves your library, but chat and metadata extraction are disabled (a warning is logged at startup).
2. Optionally pick a model with `OPENROUTER_MODEL`, using the `vendor/model` slug from [openrouter.ai/models](https://openrouter.ai/models). The default is `deepseek/deepseek-v4.1-flash-1017`. Choose one that supports **tool calling**, since the assistant uses tools to list manuals and search inside them.
3. Optionally set `OPENROUTER_METADATA_MODEL` to use a different (e.g. cheaper) model for extracting a manual's name, device and summary on upload. It defaults to `OPENROUTER_MODEL`.

If your model doesn't support tool calling, set `LLM_TOOLS=false` to use the simpler retrieve-then-answer pipeline instead. To point at a different OpenAI-compatible endpoint, set `OPENROUTER_BASE_URL` (default `https://openrouter.ai/api/v1`).

## Deploy

- Mount a persistent volume at `/data`. It holds the SQLite DB, the original PDFs, nightly backups and the auto-generated cookie secret.
- Run **exactly one replica**: SQLite on a volume can't be shared between replicas.
- Required env: `OPENROUTER_API_KEY` (see [LLM backend](#llm-backend-openrouter)), and `APP_PASSWORD` until the first account exists. The app refuses to start without `APP_PASSWORD` while there are no accounts. After that it isn't needed (see [Accounts](#accounts)). Set `APP_URL=https://…` so session cookies are marked Secure.
- After deploying (or upgrading from the shared-password version), open the site and create the first (admin) account with `APP_PASSWORD`, then add everyone else under **Users**.
- If a reverse proxy sits in front, make sure it sets `X-Forwarded-For`: login rate limiting is keyed on that header's first address.
- Health check: `GET /healthz`.

All settings are listed in `.env.example`.

## Accounts

Manualagerie is one household's app: the library, notes, maintenance tasks and shares belong to everyone. Each person
still signs in with their own account, so the app knows who asked what and who did what.

**First run.** While no account exists, the login page shows *Create the first (admin) account*. Creating it asks for
the household password, `APP_PASSWORD`, so a stranger who finds the site can't claim it. Then pick a username, a
display name and your own password. Upgrading an existing install works the same way: deploy, open the site, and
create the first account with your current `APP_PASSWORD`. Sessions from before accounts stop working, so everyone
signs in once more.

**`APP_PASSWORD` after that.** It is only used for that first account. Once any account exists, the app starts and
runs without it, and nobody can sign in with it. You can remove it from the environment, or keep it in case you ever
reset the database.

**Signing in** uses username and password. Usernames are case-insensitive. Failed attempts are limited to 10 per
15 minutes from each IP, and also to 10 per 15 minutes against each username from any IP.

**Users page** (`/settings/users`, admins only; *Users* in the nav). Here an admin can add people, reset their
passwords, disable or re-enable them, and make or unmake admins. The last active admin can't be disabled or demoted.
Disabling someone, or resetting their password, signs them out on their next request. Everyone can change their own
password from the page their name links to in the nav (`/settings/password`). Doing so signs out their other devices.

**Who did what.**

- Each conversation belongs to the person who started it. The sidebar shows **Mine** by default, and **Everyone's**
  lists the whole household's with the asker's name. Anyone can open any conversation. Conversation search follows
  the same toggle.
- Shares show *Shared by …* on the Shares page (the public `/s/<token>` page never names anyone), notes show
  *Added by …*, and the Maintenance page shows who last marked a task done.
- When the assistant creates a share or logs maintenance, it is credited to the person who started the conversation.
- Anything from before accounts shows as *unknown*.

**Security notes.** Passwords are hashed with scrypt (N=2^14, r=8, p=1) and a random 16-byte salt per user, and
compared in constant time. An unknown username costs the same hash work as a wrong password. The signed session
cookie holds the user id and a per-user session generation, and both are checked against the database on every
request. Disabling a user or changing their password bumps the generation, which kills every existing cookie for that
user. Logging in always issues a fresh cookie. Forms rely on the session cookie being `SameSite=Lax` for CSRF
protection, as the rest of the app does, so a cross-site POST arrives without a session. `/login`, `/login/setup`,
`/healthz`, `/static/…` and exactly `/s/<token>` are the only routes reachable while signed out.

`AUTH_DISABLED=true` (local development only) skips sign-in entirely. Everything is then treated as everyone's, and
the Users page is open.

## Photos and links

Photos (including iPhone HEIC/HEIF, via `pillow-heif`, whose wheels bundle libheif so no extra system packages are needed) are turned into PDFs and OCRed. On the Manuals page you can also paste a link to a manufacturer PDF or photo: the server downloads it (http/https only, private and internal addresses refused on every redirect, size-capped, type checked by Content-Type and file contents) and then treats it exactly like an upload, remembering the source URL.

## Reading scans and diagrams with vision

Tesseract mangles tables, wiring diagrams and exploded parts views. So after a manual's pages are extracted, the ingest worker can send the trickiest page images to the LLM (any OpenRouter model that accepts image input, the default `deepseek/deepseek-v4.1-flash-1017` included). The model returns a markdown transcription: tables come back as markdown tables, and each diagram is described with every label, part number, wire colour and measurement on it, so you can search for them and the assistant can cite them.

`VISION_OCR` sets when the worker uses vision:

| Value | Pages sent to the vision model |
|---|---|
| `off` | none: Tesseract only |
| `fallback` (default) | scanned or photographed pages where Tesseract found nothing or very little text, had low confidence, or found only a few words on a mostly-picture page; also text-layer pages that are mostly an embedded image with a short caption |
| `always` | every page without a text layer (all scans and photos), plus the `fallback` pages |

- Pages are rendered with PyMuPDF at 150 DPI, with the longest side capped at 2000 px.
- `OPENROUTER_VISION_MODEL` picks the model. If it is unset, `OPENROUTER_METADATA_MODEL` is used, then `OPENROUTER_MODEL`. `VISION_CONCURRENCY` (default 2) caps the number of requests in flight.
- Requests use the shared OpenRouter client, so 429 and 5xx errors are retried with backoff. If a manual is still rate limited after those retries, its remaining pages keep their Tesseract text. Token usage is logged per page and per manual.
- If a request fails, the page keeps its Tesseract text. A vision failure never fails the ingest. Without `OPENROUTER_API_KEY`, vision is skipped silently.
- Each page records which method produced its text (`text`, `tesseract` or `vision`) in `pages.ocr_method`, and the manual page shows e.g. "12 pages read with vision". Click **Re-index** to re-read an existing manual.

## Tags

Every manual has a free-form **Tags** field next to its metadata: `downstairs AC, shed, rental, Dana's`. Tags are the household's own words for a device, so when you ask *"how often should I clean the filter on my downstairs AC?"* and the library holds two air-conditioner manuals, the assistant matches your wording against the tags (and the device name, room and notes) instead of asking which one you mean.

Tags show as chips in the library list, appear in every catalog entry the model sees, and `list_manuals` ranks tag matches above words that merely show up in a summary.

## Notes

Not everything worth remembering is in a manual. The **Notes** page holds short facts the household writes down itself: *"Upstairs AC filters: 16x25x1, MERV 11, two of them"*, the paint colour in the hallway, where the water shut-off is. Each note has a title, free text and optional tags.

The assistant sees every note next to the manual catalog, so *"what size are my upstairs AC filters?"* is answered straight from the note, with a 📝 chip that links back to it. When the notes outgrow the prompt budget (`NOTES_INLINE_CHARS`, 20k characters by default), only their titles are inlined and the model reads the rest through a `search_notes` tool.

## Maintenance

The **Maintenance** page tracks recurring chores ("Change furnace filter, every 90 days"). Each task has an interval, a last-done date, optional notes and an optional link to a manual (the task then also shows on that manual's page; deleting the manual just unlinks it). Tasks are sorted by due date (last done + interval; a never-done task counts from the day it was added) and badged **Overdue**, **Due soon** (within 14 days) or **OK**. **Mark done** logs today's date to the history and updates last-done. The nav shows an "N overdue" badge when anything is past due.

The assistant sees a summary of every task (title, status, last done), so *"when did I last change the furnace filter?"* works. Saying *"I just changed the filter"* makes it call the `log_maintenance` tool (task title or id, optional date) and confirm what it logged.

## Searching conversations

The search box above the conversation list finds past chats by what you or the assistant said (tool calls and tool output are not indexed). Matches show a short highlighted snippet; click one to open that conversation.

## Sharing

Sometimes an answer is for someone outside the house: the plumber who needs the drain hose size, a sitter who needs to know how to reset the furnace. Click **🔗 Share** under any answer, or ask the assistant *"make me a link I can send the plumber"*, and you get a public, read-only page at `/s/<token>` that opens without signing in.

A share is a snapshot: it doesn't change if the conversation does. Manual citations turn into plain-text sources (*"Bosch dishwasher manual, p. 12"*) because the PDFs themselves stay private, and the page has no links back into the site. The assistant only creates a share when asked, writes it to make sense on its own, and leaves household notes and other private details out unless you ask for them. Pages are marked `noindex`.

The **Shares** page lists every live link with a copy button and a **Revoke** button; a revoked link returns 404. Set `APP_URL` so the links are absolute and point at your public hostname.

## Themes

`app/static/themes.css` owns every colour; `app/static/app.css` only reads tokens, so a theme is a single token block. Light, dark, sepia, parchment (warm paper, oxblood ink, serif) and blueprint ship today, and the picker in the top bar (also on the login screen) stores the choice in a `theme` cookie — `auto` follows the OS.

To add a theme:

1. add a `[data-theme="<id>"]` block to `app/static/themes.css` (copy the light block; every colour token is required, and `--bg-image`/`--bg-size` optionally add a page texture)
2. add `("<id>", "Label")` to `THEMES` in `app/themes.py`

The picker, the cookie and the pre-paint resolution in `app/static/theme.js` are generic. `tests/test_themes.py` fails if a registered theme has no token block, or if `app.css` grows a hard-coded colour.

## Evaluating answer quality

`tests/evals/questions.yaml` holds questions with the manual and page that should answer each one. The shipped entries are **examples**; replace them with about 20 real questions about your own manuals. The file's header documents every field:

- `question`: phrased as you would ask it in chat.
- `manual`: a substring of the manual's title or original filename.
- `pages`: optional; one page, a range (`"12-14"`) or a list. These are PDF page numbers, as shown in citations.
- `answer_must_contain`: optional strings the answer must include (`"5/8"`, `"20 psi"`).
- `expect_not_found: true`: for questions your manuals do not answer; the assistant should say so rather than guess.

Run it against your real library (`DATA_DIR`, e.g. `DATA_DIR=./data` locally):

```bash
uv run python scripts/eval.py --retrieval-only   # search only: no LLM, no cost
uv run python scripts/eval.py                    # also runs the real agent (needs OPENROUTER_API_KEY)
uv run python scripts/eval.py --limit 5 --k 8    # first 5 questions, top-8 retrieval
```

Reading the results:

- **Retrieval hit rate**: the share of questions where the expected manual (and page, if given) appears in the top-k `search_manuals` results for the question text. A miss means the right passage never reached the model.
- **Answer pass rate**: the share of questions where the agent's citations include the expected manual/page, every `answer_must_contain` string appears, and `expect_not_found` questions get a "not found" style answer. Retrieval hit but answer fail points at the prompt or model; retrieval miss points at chunking or search.
- A table is printed and the full JSON (answers, citations, per-question detail) is saved to `{DATA_DIR}/evals/eval-<timestamp>.json` so you can compare runs. Answer checks run in throwaway conversations that are deleted afterwards.

Tuning loop: record a baseline, change one thing, and re-run.

- **Chunk size**: `TARGET_WORDS` in `app/ingest/chunk.py`. Re-ingest your manuals after changing it, then run `--retrieval-only`. If answers are split across chunks, go larger; if the right page is buried in a long chunk, go smaller.
- **Heading boost**: the `bm25(chunks_fts, 3.0, 1.0)` weights in `app/search/fts.py` (heading, then text). Raise the first number if section titles should matter more, lower it if generic headings crowd out real matches. No re-ingest needed.
- **Prompt**: edit `app/llm/prompts.py` and run the full eval; watch for failures where retrieval hit (wrong citation, missing value, guessing instead of "not found").
