FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" DATA_DIR=/data

# OCR for scanned manuals. Add more language packs (e.g. tesseract-ocr-fra) and set OCR_LANGS=eng+fra.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng curl \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.7.8 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

RUN useradd --create-home --uid 1000 app && mkdir -p /data && chown app:app /data

# Semantic search: bake the embedding model (BAAI/bge-small-en-v1.5, ~67 MB quantized ONNX) into
# the image so the first start needs no network. With fastembed/onnxruntime this adds roughly
# 230 MB to the image. If the download fails (offline build) the build still succeeds and the app
# downloads the model into /data/models on first start, or stays keyword-only.
# Build with --build-arg PRELOAD_EMBED_MODEL=0 to skip it.
ARG PRELOAD_EMBED_MODEL=1
ENV EMBED_BUNDLED_DIR=/opt/models
RUN mkdir -p /opt/models \
 && if [ "$PRELOAD_EMBED_MODEL" = "1" ]; then \
      python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5', cache_dir='/opt/models')" \
      || echo "WARNING: embedding model not pre-downloaded; it will be fetched at runtime"; \
    fi \
 && chown -R app:app /opt/models

COPY app ./app
USER app
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s CMD curl -fsS http://localhost:8080/healthz || exit 1

# Single worker on purpose: the ingest thread and SQLite writer live in this process.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]
