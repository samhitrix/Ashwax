# AshwaX — one image runs the whole stack through launcher.py (the only supported
# entry point): API + orchestrator + React dashboard. Paper mode unless config says otherwise.
FROM python:3.13-slim-bookworm

# Node is only needed for the dashboard; copy it from the official image instead of apt.
COPY --from=node:22-bookworm-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=node:22-bookworm-slim /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Kolkata \
    VITE_HOST=0.0.0.0

WORKDIR /app

# Dependencies first so code edits don't re-download them.
COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY dashboard_react/package.json dashboard_react/package-lock.json ./dashboard_react/
RUN cd dashboard_react && npm ci

COPY . .

# Only the dashboard port is published; the API stays on 127.0.0.1:3000 inside the
# container and is reached through the dashboard's /api proxy.
EXPOSE 5173

HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 \
  CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:3000/api/agent_status', timeout=8)" || exit 1

CMD ["python", "launcher.py"]
