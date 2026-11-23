# hunter — Spotify→slskd orchestrator + Navidrome playlist mirror.
# Tiny on purpose: python-alpine + requests is the entire dependency tree.
FROM python:3.12-alpine

RUN apk add --no-cache tzdata \
    && pip install --no-cache-dir --root-user-action=ignore requests

WORKDIR /app
COPY hunter/ /app/hunter/

# `docker exec hunter hunter <cmd>` — the operator CLI.
RUN printf '#!/bin/sh\nexec python3 -m hunter.cli "$@"\n' > /usr/local/bin/hunter \
    && chmod +x /usr/local/bin/hunter

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

ENTRYPOINT ["python3", "-m", "hunter.scheduler"]
