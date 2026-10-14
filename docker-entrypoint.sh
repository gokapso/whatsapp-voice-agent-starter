#!/bin/sh
# Container entrypoint. Started as the unprivileged `app` user (Docker Compose, docker run), it
# only runs the command. Started as root, for example on Railway, which mounts volumes owned by
# root and needs RAILWAY_RUN_UID=0 for that, it gives DATA_DIR to `app` and drops to that user
# before the server starts, so the server still never runs as root.
set -eu
if [ "$(id -u)" = "0" ]; then
  mkdir -p "$DATA_DIR"
  chown -R -h app:app "$DATA_DIR"
  exec setpriv --reuid=app --regid=app --init-groups --no-new-privs "$@"
fi
exec "$@"
