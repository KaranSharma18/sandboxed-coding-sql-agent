# syntax=docker/dockerfile:1.7
# Python sandbox image: the interpreter, uv, the Fixloop pytest plugin and a pytest wheelhouse.
# Nothing else. Project dependencies are installed per task into a separate environment volume
# (mounted at /opt/venv) during the prepare step, wheels only, through the package proxy.
ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv

# A fixed unprivileged user. Every sandbox runs as 10001:10001; root is never used.
RUN groupadd --gid 10001 sandbox \
 && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /tmp --shell /usr/sbin/nologin sandbox \
 && mkdir -p /workspace /out /opt/venv /env /opt/fixloop/lib /opt/fixloop/wheelhouse \
 && chown 10001:10001 /workspace /out /opt/venv /env \
 && chmod 0755 /workspace /out /opt/venv /env

# pytest and its dependencies as wheels: projects without third-party dependencies prepare offline.
RUN python -m pip download --no-cache-dir --only-binary :all: --dest /opt/fixloop/wheelhouse "pytest>=8,<10" \
 && chmod -R a+rX /opt/fixloop

# The reporting plugin is put on PYTHONPATH by the runner, so it works with any interpreter.
COPY packages/fixloop-pytest/src/fixloop_pytest /opt/fixloop/lib/fixloop_pytest

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH=/usr/local/bin:/usr/bin:/bin

LABEL org.opencontainers.image.title="fixloop sandbox (python)" \
      org.opencontainers.image.source="https://github.com/KaranSharma18/sandboxed-coding-sql-agent"
USER 10001:10001
WORKDIR /workspace
