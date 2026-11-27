FROM node:22-bookworm-slim

ARG CODEX_VERSION=0.172.0
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 ca-certificates tini \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @openai/codex@${CODEX_VERSION} \
    && npm cache clean --force

# Bothost монтирует исходники поверх /app. Программа живёт отдельно.
WORKDIR /opt/koresh
COPY bot.py persona.txt response.schema.json ./
ENV PYTHONUNBUFFERED=1 \
    OWNER_ID=379721932 \
    DATA_DIR=/app/data \
    CODEX_HOME=/app/data/codex \
    PORT=8080
EXPOSE 8080
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python3", "/opt/koresh/bot.py"]
