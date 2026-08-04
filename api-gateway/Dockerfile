FROM python:3.12-slim
WORKDIR /app

RUN pip install --no-cache-dir \
    "fastapi>=0.115.0" \
    "uvicorn[standard]>=0.34.0" \
    "pyjwt>=2.9.0" \
    "cryptography>=44.0.0" \
    "httpx>=0.28.0" \
    "python-dotenv>=1.0.0" \
    "prometheus-client>=0.21.0"

COPY api-gateway/gateway/ ./gateway/

RUN mkdir -p /app/keys /app/data

EXPOSE 8092

CMD ["uvicorn", "gateway.main:app", "--host", "0.0.0.0", "--port", "8092"]
