#!/bin/sh
# Starts as root, makes the state folder writable for the app user, then drops
# privileges. PUID/PGID default to 99/100 (unRAID's nobody:users) so a bind-
# mounted appdata folder is writable out of the box; override for other hosts.
set -e

PUID="${PUID:-99}"
PGID="${PGID:-100}"
STATE_DIR="${STATE_DIR:-/data/state}"

if [ "$(id -u)" = "0" ]; then
    # Re-map the built-in 'dashboard' user to the requested ids.
    groupmod -o -g "$PGID" dashboard 2>/dev/null || true
    usermod  -o -u "$PUID" -g "$PGID" dashboard 2>/dev/null || true

    mkdir -p "$STATE_DIR" 2>/dev/null || true
    if ! chown -R "$PUID:$PGID" "$STATE_DIR" 2>/dev/null; then
        echo "rsync-watch: WARNING could not chown $STATE_DIR — alert settings may not be saveable" >&2
    fi
    echo "rsync-watch: running as uid=$PUID gid=$PGID, state dir $STATE_DIR"
    exec setpriv --reuid="$PUID" --regid="$PGID" --init-groups "$@"
fi

exec "$@"
