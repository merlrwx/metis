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

# Copy the environment and application files
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app app.py /app/app.py
COPY --chown=app:app api_client.py /app/api_client.py
COPY --chown=app:app cookie_component /app/cookie_component

# Run the application
WORKDIR /app
USER app
EXPOSE 8501
CMD ["/app/.venv/bin/streamlit", "run", "--server.address", "0.0.0.0", "/app/app.py"]
