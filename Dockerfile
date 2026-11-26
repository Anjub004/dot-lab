FROM python:3.12-slim

LABEL org.opencontainers.image.title="Dot Lab" \
      org.opencontainers.image.description="Experimental always-on AI agent workspace" \
      org.opencontainers.image.source="https://github.com/Anjub004/dot-lab" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Runtime dependencies only (no test or lint tools), read from pyproject.toml.
COPY pyproject.toml .
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml', 'rb'))['project']['dependencies']))" > /tmp/requirements.txt \
    && pip install -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

COPY app ./app
COPY dashboard ./dashboard
COPY examples ./examples
COPY README.md LICENSE alembic.ini .env.example ./

# Run as an unprivileged user; data/ is the only writable directory.
RUN useradd --create-home --uid 1000 dotlab \
    && mkdir -p /app/data/workspace \
    && chown -R dotlab:dotlab /app/data
USER dotlab

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
