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

## Themes

`app/static/themes.css` owns every colour; `app/static/app.css` only reads tokens, so a theme is a single token block. Light, dark, sepia, parchment (warm paper, oxblood ink, serif) and blueprint ship today, and the picker in the top bar (also on the login screen) stores the choice in a `theme` cookie — `auto` follows the OS.

To add a theme:

1. add a `[data-theme="<id>"]` block to `app/static/themes.css` (copy the light block; every colour token is required, and `--bg-image`/`--bg-size` optionally add a page texture)
2. add `("<id>", "Label")` to `THEMES` in `app/themes.py`

The picker, the cookie and the pre-paint resolution in `app/static/theme.js` are generic. `tests/test_themes.py` fails if a registered theme has no token block, or if `app.css` grows a hard-coded colour.

## Tags

Every manual has a free-form **Tags** field next to its metadata: `downstairs AC, shed, rental, Dana's`. Tags are the household's own words for a device, so when you ask *"how often should I clean the filter on my downstairs AC?"* and the library holds two air-conditioner manuals, the assistant matches your wording against the tags (and the device name, room and notes) instead of asking which one you mean.

Tags show as chips in the library list, appear in every catalog entry the model sees, and `list_manuals` ranks tag matches above words that merely show up in a summary.

## Photos and links

Photos (including iPhone HEIC/HEIF, via `pillow-heif`, whose wheels bundle libheif so no extra system packages are needed) are turned into PDFs and OCRed. On the Manuals page you can also paste a link to a manufacturer PDF or photo: the server downloads it (http/https only, private and internal addresses refused on every redirect, size-capped, type checked by Content-Type and file contents) and then treats it exactly like an upload, remembering the source URL.

## Searching conversations

The search box above the conversation list finds past chats by what you or the assistant said (tool calls and tool output are not indexed). Matches show a short highlighted snippet; click one to open that conversation.

## Notes

Not everything worth remembering is in a manual. The **Notes** page holds short facts the household writes down itself: *"Upstairs AC filters: 16x25x1, MERV 11, two of them"*, the paint colour in the hallway, where the water shut-off is. Each note has a title, free text and optional tags.

The assistant sees every note next to the manual catalog, so *"what size are my upstairs AC filters?"* is answered straight from the note, with a 📝 chip that links back to it. When the notes outgrow the prompt budget (`NOTES_INLINE_CHARS`, 20k characters by default), only their titles are inlined and the model reads the rest through a `search_notes` tool.

## Sharing

Sometimes an answer is for someone outside the house: the plumber who needs the drain hose size, a sitter who needs to know how to reset the furnace. Click **🔗 Share** under any answer, or ask the assistant *"make me a link I can send the plumber"*, and you get a public, read-only page at `/s/<token>` that opens without the household password.

A share is a snapshot: it doesn't change if the conversation does. Manual citations turn into plain-text sources (*"Bosch dishwasher manual, p. 12"*) because the PDFs themselves stay private, and the page has no links back into the site. The assistant only creates a share when asked, writes it to make sense on its own, and leaves household notes and other private details out unless you ask for them. Pages are marked `noindex`.

The **Shares** page lists every live link with a copy button and a **Revoke** button; a revoked link returns 404. Set `APP_URL` so the links are absolute and point at your public hostname.
