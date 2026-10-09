#!/bin/sh
set -e

# Supports both ways of setting ownership:
#   PUID/PGID  - container starts as root, remaps the user, then drops down
#   user:      - compose starts the container as an unprivileged uid already
# The second cannot remap anything, so it just runs the command as given.
if [ "$(id -u)" != "0" ]; then
    mkdir -p /downloads /config/beets 2>/dev/null || true
    exec "$@"
fi

PUID=${PUID:-1000}
PGID=${PGID:-1000}

groupmod -o -g "$PGID" companion
usermod  -o -u "$PUID" companion

mkdir -p /config /config/beets /downloads

# The config volume is small and entirely ours, so claiming all of it is safe.
chown -R companion:companion /config

# The output volume is not: it may sit inside an existing music library owned
# by other users. Only the directories this app writes into are touched, and
# never recursively.
for dir in /downloads /music; do
    chown companion:companion "$dir" 2>/dev/null || true
done

exec gosu companion "$@"
