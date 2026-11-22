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

## Deploy

- Mount a persistent volume at `/data`. It holds the SQLite DB, the original PDFs, nightly backups and the auto-generated cookie secret.
- Run **exactly one replica**: SQLite on a volume can't be shared between replicas.
- Required env: `OPENROUTER_API_KEY` and `APP_PASSWORD`. Set `APP_URL=https://…` so session cookies are marked Secure.
- Health check: `GET /healthz`.

All settings are listed in `.env.example`.
