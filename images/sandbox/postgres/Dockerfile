# syntax=docker/dockerfile:1.7
# Postgres sandbox image: PostgreSQL 16 server binaries, Python with psycopg, and the Fixloop
# SQL executor. The executor starts a private server per run on a copy of the prepared data
# directory, listening only on a Unix socket, and runs the candidate as a read-only role.
FROM postgres:16-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-pip \
 && python3 -m pip install --no-cache-dir --break-system-packages "psycopg[binary]>=3.2,<4" \
 && apt-get purge -y python3-pip && apt-get autoremove -y && rm -rf /var/lib/apt/lists/* \
 && groupadd --gid 10001 sandbox \
 && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /tmp --shell /usr/sbin/nologin sandbox \
 && mkdir -p /workspace /out /env /opt/venv /opt/fixloop/lib \
 && chown 10001:10001 /workspace /out /env /opt/venv \
 && chmod 0755 /workspace /out /env /opt/venv

COPY packages/fixloop-sqlexec/src/fixloop_sqlexec /opt/fixloop/lib/fixloop_sqlexec
RUN chmod -R a+rX /opt/fixloop

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/opt/fixloop/lib \
    FIXLOOP_PG_BIN=/usr/lib/postgresql/16/bin
# The base image's entrypoint would start a server as postgres; sandboxes run their own command.
ENTRYPOINT []
USER 10001:10001
WORKDIR /workspace
