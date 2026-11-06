# Debian slim rather than Alpine: every dependency ships ready-made wheels for
# it on amd64 and arm64, so nothing compiles during the build.
FROM python:3.13-slim

LABEL org.opencontainers.image.source="https://github.com/dmmedakin/printstash-mcp" \
      org.opencontainers.image.description="MCP server for PrintStash, the self-hosted 3D print library" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8000

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt \
 && useradd --no-create-home --uid 10001 mcp
COPY server.py api_catalog.json /app/

USER mcp
EXPOSE 8000

# /healthz answers 200 only while PrintStash itself is reachable.
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=5 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8000') + '/healthz', timeout=5)"

CMD ["python", "/app/server.py"]
