FROM python:3.12-slim

# Tini for proper signal handling in containers
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install system deps (only if needed by lxml/numpy wheels — slim image is fine for now)
RUN apt-get update && apt-get install -y --no-install-recommends \
    tini \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps first for layer caching
COPY requirements.txt .
RUN pip install -r requirements.txt

# Copy app code
COPY core ./core
COPY api ./api
COPY db ./db
COPY mcp_server ./mcp_server
COPY scripts ./scripts

# Default port for the API; Railway/Render override $PORT
ENV PORT=8000
EXPOSE 8000

# Tini handles signals so SIGTERM is forwarded to uvicorn cleanly
ENTRYPOINT ["/usr/bin/tini", "--"]

# Default to running the FastAPI service. Override CMD to run the
# snapshot job or the MCP server in a different container/cron.
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]