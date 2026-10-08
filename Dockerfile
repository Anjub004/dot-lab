FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY dashboard ./dashboard
COPY examples ./examples
COPY pyproject.toml README.md ./

# Run as an unprivileged user; data/ is the only writable directory.
RUN useradd --create-home --uid 1000 dotlab \
    && mkdir -p /app/data/workspace \
    && chown -R dotlab:dotlab /app/data
USER dotlab

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
