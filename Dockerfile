# hunter — Music acquisition orchestrator, library console & Navidrome playlist mirror.
# Tiny on purpose: python-alpine + requests (+ PySocks for optional SOCKS proxies).
FROM python:3.12-alpine

LABEL org.opencontainers.image.title="Hunter" \
      org.opencontainers.image.description="Self-hosted music acquisition orchestrator, beets web console, and Navidrome playlist mirror" \
      org.opencontainers.image.source="https://github.com/J-Reed700/hunter" \
      org.opencontainers.image.licenses="MIT"

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN apk add --no-cache tzdata \
    && pip install --no-cache-dir --root-user-action=ignore -r /app/requirements.txt

COPY hunter/ /app/hunter/

# `docker exec hunter hunter <cmd>` — the operator CLI.
RUN printf '#!/bin/sh\nexec python3 -m hunter.cli "$@"\n' > /usr/local/bin/hunter \
    && chmod +x /usr/local/bin/hunter

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=15s --start-period=30s --retries=3 \
    CMD ["hunter", "health"]

ENTRYPOINT ["python3", "-m", "hunter.scheduler"]
