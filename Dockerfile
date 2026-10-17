FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# Install dependencies first so application changes keep the cache warm.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .
RUN uv sync --frozen --no-dev \
    && uv run python manage.py collectstatic --noinput \
    && mkdir -p /data \
    && useradd --create-home --uid 10001 bridge \
    && chown -R bridge:bridge /app /data

USER bridge

EXPOSE 8000

CMD ["sh", "-c", ".venv/bin/python manage.py migrate --noinput && .venv/bin/uvicorn cc_switch.asgi:application --host 0.0.0.0 --port 8000 --workers 1"]
