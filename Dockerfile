# syntax=docker/dockerfile:1
# Two stages: build the virtualenv with uv, then copy only the venv and runtime files
# into a clean image that runs as a non-root user.

FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# Dependencies first: this layer stays cached until pyproject.toml or uv.lock changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY README.md ./
COPY mock_snow ./mock_snow
COPY triage ./triage
RUN uv sync --locked --no-dev --no-editable

FROM python:3.12-slim
RUN useradd --create-home --uid 1000 app \
    && mkdir -p /app/data /app/runs && chown app:app /app/data /app/runs
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY kb ./kb
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 HOST=0.0.0.0 PORT=8000
USER app
EXPOSE 8000
CMD ["triage-api"]
