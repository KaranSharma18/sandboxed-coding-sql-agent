# syntax=docker/dockerfile:1.7
# DuckDB sandbox image: Python, DuckDB and the Fixloop SQL executor. Queries run against
# database files built from the customer's DDL and seeds, opened read-only.
FROM python:3.12-slim-bookworm

RUN groupadd --gid 10001 sandbox \
 && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /tmp --shell /usr/sbin/nologin sandbox \
 && mkdir -p /workspace /out /env /opt/venv /opt/fixloop/lib \
 && chown 10001:10001 /workspace /out /env /opt/venv \
 && chmod 0755 /workspace /out /env /opt/venv \
 && python -m pip install --no-cache-dir "duckdb>=1.1,<3"

COPY packages/fixloop-sqlexec/src/fixloop_sqlexec /opt/fixloop/lib/fixloop_sqlexec
RUN chmod -R a+rX /opt/fixloop

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/opt/fixloop/lib
USER 10001:10001
WORKDIR /workspace
