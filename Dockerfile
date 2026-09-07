FROM python:3.12-slim AS runtime

FROM node:24-bookworm-slim AS frontend
WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY index.html vite.config.js ./
COPY frontend ./frontend
RUN npm run build

FROM runtime
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
COPY requirements.txt ./
RUN pip install --no-cache-dir --prefer-binary -r requirements.txt
COPY app.py ./
COPY scripts ./scripts
COPY --from=frontend /build/frontend/dist ./frontend/dist
RUN mkdir -p data/assets
EXPOSE 8910
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.getenv('PORT', '8910') + '/api/health', timeout=3)"
ENTRYPOINT ["sh", "-c", "exec gunicorn --bind 0.0.0.0:${PORT:-8910} --workers 2 --threads 4 app:app"]