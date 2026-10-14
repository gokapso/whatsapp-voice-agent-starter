# Pipecat Cloud image for this starter. Build from the repository root:
#   docker build -f examples/pipecat-cloud/Dockerfile -t kapso-voice-agent-cloud .
# The base image runs Pipecat Cloud's session server (/app/app.py) from /app/.venv and imports
# bot() from /app/bot.py. This file adds the starter's locked dependencies to that environment
# instead of replacing it, so the base image's own packages (pipecatcloud) stay installed.
FROM dailyco/pipecat-base:0.2.5-py3.12

COPY pyproject.toml uv.lock README.md LICENSE /starter/
RUN cd /starter \
    && uv export --frozen --no-dev --no-emit-project -o /tmp/requirements.txt \
    && uv pip install --python /app/.venv/bin/python -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

COPY src /starter/src
COPY agent /starter/agent
RUN uv pip install --python /app/.venv/bin/python --no-deps /starter

COPY examples/pipecat-cloud/bot.py /app/bot.py
# Sessions are short-lived: DATA_DIR is removed when a session ends (see README, "Limits").
ENV AGENT_CONFIG_PATH=/starter/agent/agent.toml DATA_DIR=/tmp/kapso-voice-agent PYTHONUNBUFFERED=1
