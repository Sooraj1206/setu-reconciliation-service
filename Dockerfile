FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY scripts/ ./scripts/

# Default to SQLite so the container runs standalone; override DATABASE_URL
# in your platform's env vars to point at managed Postgres in production.
ENV DATABASE_URL=sqlite:///./reconciliation.db
ENV PORT=8000

EXPOSE 8000

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
