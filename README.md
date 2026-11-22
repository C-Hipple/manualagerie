# manualagerie
Manualagerie is a site where you upload all the random manuals in your house, and then you can ask an AI about them whenever you need them.

Ask *"What size is the hose connector on my dishwasher?"*. The assistant finds the right manual, searches inside it, and answers with page citations that open the PDF at that page. See [PLAN.md](PLAN.md) for the architecture.

## Run locally (Docker)

```bash
cp .env.example .env               # set APP_PASSWORD; OPENROUTER_API_KEY can live here or in your shell
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

## Themes

`app/static/themes.css` owns every colour; `app/static/app.css` only reads tokens, so a theme is a single token block. Light, dark, sepia, parchment (warm paper, oxblood ink, serif) and blueprint ship today, and the picker in the top bar (also on the login screen) stores the choice in a `theme` cookie — `auto` follows the OS.

To add a theme:

1. add a `[data-theme="<id>"]` block to `app/static/themes.css` (copy the light block; every colour token is required, and `--bg-image`/`--bg-size` optionally add a page texture)
2. add `("<id>", "Label")` to `THEMES` in `app/themes.py`

The picker, the cookie and the pre-paint resolution in `app/static/theme.js` are generic. `tests/test_themes.py` fails if a registered theme has no token block, or if `app.css` grows a hard-coded colour.

## Tags

Every manual has a free-form **Tags** field next to its metadata: `downstairs AC, shed, rental, Dana's`. Tags are the household's own words for a device, so when you ask *"how often should I clean the filter on my downstairs AC?"* and the library holds two air-conditioner manuals, the assistant matches your wording against the tags (and the device name, room and notes) instead of asking which one you mean.

Tags show as chips in the library list, appear in every catalog entry the model sees, and `list_manuals` ranks tag matches above words that merely show up in a summary.

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

## Deploy

- Mount a persistent volume at `/data`. It holds the SQLite DB, the original PDFs, nightly backups and the auto-generated cookie secret.
- Run **exactly one replica**: SQLite on a volume can't be shared between replicas.
- Required env: `OPENROUTER_API_KEY` and `APP_PASSWORD`. Set `APP_URL=https://…` so session cookies are marked Secure.
- Health check: `GET /healthz`.

All settings are listed in `.env.example`.
