# ===== Stage 1: Build React frontend =====
FROM node:22-alpine AS frontend-builder
WORKDIR /frontend

# Allow building behind restrictive networks (e.g. an npm mirror).
ARG NPM_REGISTRY=https://registry.npmjs.org
RUN npm config set registry ${NPM_REGISTRY}

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
RUN npm run build


# ===== Stage 2: Python runtime with SPA =====
FROM python:3.12-slim
WORKDIR /app

# Allow building behind restrictive networks (e.g. a PyPI mirror) without
# changing the default behaviour.
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN pip install --no-cache-dir -i ${PIP_INDEX_URL} \
    "fastapi>=0.115.0" \
    "uvicorn[standard]>=0.34.0" \
    "pyjwt>=2.9.0" \
    "cryptography>=44.0.0" \
    "bcrypt>=4.2.0" \
    "httpx>=0.28.0" \
    "python-multipart>=0.0.32"

COPY auth-server/auth_server/ ./auth_server/
COPY --from=frontend-builder /frontend/dist/ ./frontend/dist/
COPY scripts/backup.py ./scripts/backup.py

RUN mkdir -p /app/keys /app/data

EXPOSE 8091

CMD ["uvicorn", "auth_server.main:app", "--host", "0.0.0.0", "--port", "8091"]
