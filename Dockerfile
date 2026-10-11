# Stage 1: build the React frontend
FROM node:22-slim AS frontend
WORKDIR /fe
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# Stage 2: the API, serving the built frontend at /
FROM python:3.11-slim
WORKDIR /srv
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HF_HOME=/srv/.cache/huggingface
# OpenCV (used by the OCR engine) needs these shared libraries on slim images
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 curl && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Download the embedding model at build time so the container starts without network access to Hugging Face.
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-small-en-v1.5')"
# ...and the reranker used to pick the passages each answer is based on.
RUN python -c "from sentence_transformers import CrossEncoder; CrossEncoder('cross-encoder/ms-marco-MiniLM-L-6-v2')"

COPY . .
COPY --from=frontend /fe/dist ./frontend/dist
RUN useradd --create-home --uid 1000 app && mkdir -p /srv/var /srv/uploads /srv/kb && chown -R app:app /srv
USER app

# Everything that changes at runtime lives on two volumes, so it survives rebuilds and redeploys:
#   /srv/var  databases (accounts, chats, usage, audit), KB index + OCR cache, attachments, ad images, uploads
#   /srv/kb   the Urban Money knowledge-base folder (admins upload into it; it is not baked into the image)
ENV APP_ENV=production AUDIT_DB=/srv/var/audit.db UPLOAD_DIR=/srv/var/uploads ATTACHMENT_DIR=/srv/var/attachments \
    KB_INDEX_DB=/srv/var/kb_index.db KB_CACHE_DIR=/srv/var/kb_cache URBAN_MONEY_DIR=/srv/kb
VOLUME ["/srv/var", "/srv/kb"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s CMD curl -fs http://127.0.0.1:8000/api/health || exit 1
# One process only: rate limits, the knowledge-base job queue and SQLite are in-process.
# --proxy-headers: behind the reverse proxy, take the user's IP from X-Forwarded-For (per-user rate limits).
# Only the proxy can reach port 8000 (docker-compose does not publish it), so trusting its headers is safe.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", \
     "--proxy-headers", "--forwarded-allow-ips", "*", "--timeout-keep-alive", "75"]
