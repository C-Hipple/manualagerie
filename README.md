# manualagerie
Manualagerie is a site where you upload all the random manuals in your house, and then you can ask an AI about them whenever you need them.

Ask *"What size is the hose connector on my dishwasher?"*. The assistant finds the right manual, searches inside it, and answers with page citations that open the PDF at that page. See [PLAN.md](PLAN.md) for the architecture.

## Run locally (Docker)

```bash
cp .env.example .env               # set APP_PASSWORD and OPENROUTER_API_KEY (or export the key in your shell)
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
- Required env: `OPENROUTER_API_KEY` (see [LLM backend](#llm-backend-openrouter)) and `APP_PASSWORD`. Set `APP_URL=https://…` so session cookies are marked Secure.
- Health check: `GET /healthz`.

All settings are listed in `.env.example`.

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

## Asking with photos

Sometimes the quickest question is a picture: the rating plate behind the fridge, an `E24` blinking on the dishwasher, a part that fell off. Click **📎** next to the chat box, paste a screenshot (Ctrl/Cmd+V), or drag images onto the box. Up to 4 images per question; thumbnails with a × to remove them show before you send, and the images stay in your message bubble in the history. A photo with no text works too: the assistant describes what it sees, reads any model number or error code, and then searches the manuals for it.

- PNG, JPEG, WebP, GIF and HEIC/HEIF are accepted, identified by decoding them with Pillow (the extension and Content-Type are ignored); anything else is refused with a message. Each upload is capped at 20 MB.
- The server downscales each image to at most 1600 px on the long side, applies the EXIF rotation, and re-encodes it (screenshots as PNG, photos as JPEG, at most about 1.5 MB). Re-encoding drops EXIF, so GPS position and camera details are not kept.
- Files are stored in `{DATA_DIR}/chat_images/`, linked to the message in the `message_images` table, served only to signed-in users at `/api/chat-images/<id>`, and deleted with their conversation. Shared answers never include them: a share is a snapshot of the answer text only.
- Only the question you are sending carries its images to the model. Earlier turns are resent as text with an `[image previously attached]` marker in place of each image: images cost many tokens, the history keeps up to 10 turns, and the assistant's earlier answer already records what it read from the photo. To ask about a detail of an earlier photo, attach it again.
- With `LLM_TOOLS=false` the query planner sees the text only, and the images go to the final answer call.
- The model must accept image input; the default `deepseek/deepseek-v4.1-flash-1017` does. If the configured `OPENROUTER_MODEL` refuses images, the chat shows an inline error suggesting a vision-capable model. Set `CHAT_IMAGES=false` to hide the attach button and refuse image uploads.

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

Sometimes an answer is for someone outside the house: the plumber who needs the drain hose size, a sitter who needs to know how to reset the furnace. Click **🔗 Share** under any answer, or ask the assistant *"make me a link I can send the plumber"*, and you get a public, read-only page at `/s/<token>` that opens without the household password.

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
