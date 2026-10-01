FROM python:3.13.15-alpine3.24 AS builder

# Install uv
COPY --from=ghcr.io/astral-sh/uv:0.12.18 /uv /uvx /bin/

# Change the working directory to the `app` directory
WORKDIR /app
ENV UV_LINK_MODE=copy

# Install dependencies in a cache layer. This speeds up build time
RUN --mount=type=cache,target=/root/.cache/uv \
  --mount=type=bind,source=uv.lock,target=uv.lock \
  --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
  uv sync --locked --no-install-project --no-editable --no-dev

# Copy the project into the intermediate image
COPY . /app

# Sync the project and install it, now that we have access to the source code
RUN --mount=type=cache,target=/root/.cache/uv \
  uv sync --locked --no-editable --no-dev

FROM python:3.13.15-alpine3.24

# Create a non-root user and group with specific IDs
RUN addgroup -S -g 1000 app && adduser -S -u 1000 -G app app
RUN mkdir -p /data/metis-objects && chown app:app /data/metis-objects

# Copy the environment, but not the source code
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --from=builder --chown=app:app /app/alembic.ini /app/alembic.ini
COPY --from=builder --chown=app:app /app/migrations /app/migrations
COPY --chown=app:app start-api.sh /app/start-api.sh

WORKDIR /app
USER app
EXPOSE 8000

# Run the application
CMD ["/app/start-api.sh"]
