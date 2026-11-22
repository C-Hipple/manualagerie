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

## Semantic search

Keyword search (SQLite FTS5) is great at model numbers and units, but misses paraphrases: *"outlet tubing width"* shares no word with *"drain hose diameter"*. With `SEMANTIC_SEARCH=true` (the default) every chunk also gets a 384-dim embedding from the local ONNX model `BAAI/bge-small-en-v1.5` (via `fastembed`, CPU only, no API calls), stored in a `sqlite-vec` table (`chunks_vec`, migration `0011_vec.sql`).

- **Search:** `search_manuals` and the `/search` page take the FTS top 30 and the vector top 30 and merge them with Reciprocal Rank Fusion (k = 60), keeping the manual filter, the preferred-language ordering and the highlighted snippets. `/search` labels each hit *keyword*, *semantic* or both, with its rank in each list.
- **Indexing:** after a manual is chunked (upload or re-index) the worker runs an `embed` job in batches of 32. On startup, once the model has loaded, a backfill queues `embed` jobs for any ready manual with chunks missing vectors, so existing libraries are upgraded automatically. Deleting or re-indexing a manual deletes its vectors.
- **Model cache:** the model lives in `$DATA_DIR/models` so it survives restarts. The Docker image pre-downloads it to `/opt/models` at build time (about +230 MB for the model, `onnxruntime` and friends; skip with `--build-arg PRELOAD_EMBED_MODEL=0`).
- **Graceful degradation:** the model loads in the background and search stays keyword-only until it is ready. If `fastembed`, the model (e.g. no network to HuggingFace) or the `sqlite-vec` extension is unavailable, a warning is logged and everything keeps working on FTS alone. Set `SEMANTIC_SEARCH=false` to skip it entirely. The `/search` page shows the current state.

## Deploy

- Mount a persistent volume at `/data`. It holds the SQLite DB, the original PDFs, nightly backups and the auto-generated cookie secret.
- Run **exactly one replica**: SQLite on a volume can't be shared between replicas.
- Required env: `OPENROUTER_API_KEY` and `APP_PASSWORD`. Set `APP_URL=https://…` so session cookies are marked Secure.
- Health check: `GET /healthz`.

All settings are listed in `.env.example`.
