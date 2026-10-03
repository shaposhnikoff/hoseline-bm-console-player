FROM python:3.14.2-slim-bookworm AS builder

COPY --from=ghcr.io/astral-sh/uv:0.11.21 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

FROM python:3.14.2-slim-bookworm

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app
RUN useradd --uid 10001 --create-home appuser \
    && mkdir -p /app/data /app/recordings \
    && chown appuser:appuser /app/recordings
COPY --from=builder /app/.venv /app/.venv
COPY hoseline_player.py hoseline_web.py ./
COPY web ./web
USER appuser
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=2).close()"
CMD ["python", "hoseline_web.py", "--host", "0.0.0.0"]
