#!/bin/sh
# Starts as root, makes the state folder writable for the app user, then drops
# privileges. PUID/PGID default to 99/100 (unRAID's nobody:users) so a bind-
# mounted appdata folder is writable out of the box; override for other hosts.
set -e

PUID="${PUID:-99}"
PGID="${PGID:-100}"
STATE_DIR="${STATE_DIR:-/data/state}"

# Files the app creates (settings, alert memory) are private to its user.
umask 077

if [ "$(id -u)" = "0" ]; then
    # Never touch ownership of a whole share because of a mis-mapped folder.
    case "$STATE_DIR" in
        /|/mnt|/mnt/user|/mnt/user/appdata|/mnt/cache|/mnt/disk[0-9]|/mnt/disk[0-9][0-9]|/data|/data/logs|/app|/etc|/home|/root|/tmp|/var)
            echo "rsync-watch: refusing to use '$STATE_DIR' as the state folder" >&2
            exit 1;;
    esac

    # Re-map the built-in 'dashboard' user to the requested ids.
    groupmod -o -g "$PGID" dashboard 2>/dev/null || true
    usermod  -o -u "$PUID" -g "$PGID" dashboard 2>/dev/null || true

    mkdir -p "$STATE_DIR" 2>/dev/null || true
    # Only the folder and the files the app writes: nothing recursive.
    if ! chown "$PUID:$PGID" "$STATE_DIR" 2>/dev/null; then
        echo "rsync-watch: WARNING could not chown $STATE_DIR - alert settings may not be saveable" >&2
    fi
    for f in "$STATE_DIR/settings.json" "$STATE_DIR/alerts.json" \
             "$STATE_DIR/settings.json.tmp" "$STATE_DIR/alerts.json.tmp"; do
        if [ -e "$f" ]; then
            chown "$PUID:$PGID" "$f" 2>/dev/null || true
            chmod 600 "$f" 2>/dev/null || true
        fi
    done
    chmod 700 "$STATE_DIR" 2>/dev/null || true
    echo "rsync-watch: running as uid=$PUID gid=$PGID, state dir $STATE_DIR"
    # Drop root for good: no capabilities carried over, and nothing started
    # from here can regain privileges.
    exec setpriv --reuid="$PUID" --regid="$PGID" --init-groups --inh-caps=-all --no-new-privs "$@"
fi

exec "$@"
