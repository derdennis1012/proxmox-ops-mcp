# syntax=docker/dockerfile:1

# ---- build stage: resolve dependencies and build a virtualenv with uv -------------------
FROM python:3.13-slim@sha256:b3575dcf816cdacc5292df77a6e3e59b8941631078373a045efa2ac1e9c5c3b2 AS build

COPY --from=ghcr.io/astral-sh/uv:0.11@sha256:77280f2f771df71f90786c314fe1bbc1e023feac652969bbf139c280babf2eb7 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /src
# Dependencies first, so this layer is cached across source changes.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev --no-install-project

COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# ---- runtime stage: no compiler, no uv, unprivileged user -------------------------------
FROM python:3.13-slim@sha256:b3575dcf816cdacc5292df77a6e3e59b8941631078373a045efa2ac1e9c5c3b2

LABEL org.opencontainers.image.title="proxmox-ops-mcp" \
      org.opencontainers.image.description="MCP server for operating Proxmox VE" \
      org.opencontainers.image.source="https://github.com/derdennis1012/proxmox-ops-mcp" \
      org.opencontainers.image.licenses="MIT" \
      io.modelcontextprotocol.server.name="io.github.derdennis1012/proxmox-ops-mcp"

RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin mcp \
    && install -d -o mcp -g mcp /var/log/proxmox-ops-mcp
COPY --from=build --chown=root:root /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER mcp
ENTRYPOINT ["proxmox-ops-mcp"]
CMD ["serve"]
