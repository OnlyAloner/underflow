FROM python:3.12-slim

# CLAUDE_CLI=1 ставит Claude Code внутрь образа: библиотекарь через подписку (UNDERFLOW_LIBRARIAN=cli)
ARG CLAUDE_CLI=0
RUN if [ "$CLAUDE_CLI" = "1" ]; then \
      apt-get update && apt-get install -y --no-install-recommends nodejs npm && \
      npm install -g @anthropic-ai/claude-code && rm -rf /var/lib/apt/lists/* ; \
    fi

WORKDIR /app
COPY pyproject.toml README.en.md ./
COPY underflow ./underflow
RUN pip install --no-cache-dir . && useradd -m -u 10001 uf && mkdir /data && chown uf /data
COPY seed ./seed
USER uf
ENV UNDERFLOW_DB=/data/underflow.db UNDERFLOW_HOST=0.0.0.0 UNDERFLOW_PORT=8800
EXPOSE 8800
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8800/health', timeout=4).status == 200 else 1)"
CMD ["underflow", "serve"]
