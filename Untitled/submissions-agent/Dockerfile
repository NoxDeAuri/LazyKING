FROM python:3.12-slim

WORKDIR /app

# Install Python deps before Playwright so layer is cached separately
COPY pyproject.toml ./
COPY src/ ./src/

RUN pip install --no-cache-dir -e .

# Playwright + Chromium + system deps (~500 MB; cached as a separate layer)
RUN playwright install chromium --with-deps

# Persistent data directory — mount a Railway volume here
RUN mkdir -p /app/data

ENV DEPLOY=true

EXPOSE 8765

# Railway injects $PORT; fall back to 8765 for other platforms
CMD ["sh", "-c", "uvicorn src.web.app:app --host 0.0.0.0 --port ${PORT:-8765}"]
