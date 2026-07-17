#!/bin/bash

# ---------------------------------------------------------------------------
# EXAMPLE — anonymized single-target rsync job.
#
# This is the kind of script whose log output the dashboard parses. Every
# host, IP, and server name below is a placeholder (192.0.2.x is reserved
# for documentation, RFC 5737) — replace them with your own before use.
#
# The dashboard only ever READS the rsync_*.log files this produces; it does
# not run this script and cannot trigger backups. The `rsync_notify.sh`
# helper referenced below is optional — if it's missing, notifications are
# silently skipped and the rest still works.
# ---------------------------------------------------------------------------

# ####################################################################
# ### ⬇️ CONFIGURATION: EDIT THIS BLOCK FOR EACH NEW SCRIPT ⬇️ ###
# ####################################################################

# --- 1. Job Identity ---
JOB_NAME="MainServer to BackupServer - ISO"   # Human-friendly name for Notifications
LOG_SERVER_NAME="BackupServer"                # Folder name for Logs
LOG_JOB_CATEGORY="ISO"                        # Subfolder for Logs

# --- 2. Source & Destination ---
UNRAID_SOURCE_DIR="/mnt/user/ISO/"       # Local Source (Keep trailing slash)
REMOTE_DEST_DIR="/mnt/user/ISO/"         # Remote Destination (Keep trailing slash)

# --- 3. Remote Connection Details ---
REMOTE_HOST="192.0.2.10"                 # Remote Server IP (placeholder)
REMOTE_USER="root"                       # SSH Username
SSH_PORT="22"

# ####################################################################
# ### ⬆️ END CONFIGURATION: DO NOT EDIT BELOW THIS LINE ⬆️ ###
# ####################################################################

# --- Static Settings ---
LOG_DIR="/mnt/user/appdata/rsync_logs/${LOG_SERVER_NAME}/${LOG_JOB_CATEGORY}"
NOTIFY_SCRIPT="/boot/config/scripts/rsync_notify.sh"
RSYNC_BIN_PATH="/usr/bin/rsync"

# --- Dynamic Variables ---
TIMESTAMP=$(date +\%Y\%m\%d_\%H\%M\%S)
LOG_FILE="$LOG_DIR/rsync_$(echo "$JOB_NAME" | tr -s ' /' '_' | tr -cd 'A-Za-z0-9_-')_$TIMESTAMP.log"

# --- Sanitize text for Telegram ---
sanitize_for_telegram() {
    local text="$1"
    if [ -z "$text" ]; then return; fi
    sed -e 's/_/\\_/g' -e 's/\*/\\*/g' -e 's/\[/\\[/g' -e 's/\]/\\]/g' \
        -e 's/(/\\(/g' -e 's/)/\\)/g' -e 's/~/\\~/g' -e 's/`/\\`/g' \
        -e 's/>/\\>/g' -e 's/#/\\#/g' -e 's/+/\\+/g' -e 's/-/\\-/g' \
        -e 's/=/\\=/g' -e 's/|/\\|/g' -e 's/{/\\{/g' -e 's/}/\\}/g' \
        -e 's/\./\\./g' -e 's/!/\\!/g' <<< "$text"
}

JOB_NAME_SAFE=$(sanitize_for_telegram "$JOB_NAME")

# --- Rsync Flags (Optimized for Existing Files) ---
RSYNC_FLAGS=(
    -avh
    --no-o      # Don't map Users
    --no-g      # Don't map Groups
    --no-p      # Don't map Permissions
    --stats
    --delete
    # --- Standard Excludes ---
    --exclude ".bzvol"
    --exclude "@eaDir"
    # --- SSH Options ---
    -e "ssh -p $SSH_PORT -o UserKnownHostsFile=/dev/null -o StrictHostKeyChecking=no"
)

# --- Ensure Log Directory Exists ---
mkdir -p "$LOG_DIR"

# --- Notification Function ---
send_notification() {
    local service="$1"
    local status="$2"
    local job_name="$3"
    local message="$4"
    if [ ! -f "$NOTIFY_SCRIPT" ]; then
        echo "Error: Notification script not found at $NOTIFY_SCRIPT."
        return
    fi
    bash "$NOTIFY_SCRIPT" "$service" "$status" "$job_name" "$message"
}

# --- LOCK FILE SECTION ---
LOCK_DIR="/tmp/rsync_locks"
LOCK_FILE="$LOCK_DIR/$(echo "$JOB_NAME" | tr -s ' /' '_' | tr -cd 'A-Za-z0-9_-').lock"
mkdir -p "$LOCK_DIR"

if [ -e "$LOCK_FILE" ]; then
    LOCK_FILE_SAFE=$(sanitize_for_telegram "$LOCK_FILE")
    HOST_SAFE=$(sanitize_for_telegram "$(hostname)")
    SKIP_MESSAGE_TELEGRAM=$(printf "%b" "⚠️ *Sync Skipped ($JOB_NAME_SAFE)*\nPrevious job running.\n*Host:* \`$HOST_SAFE\`\n*Lock:* \`$LOCK_FILE_SAFE\`")
    SKIP_MESSAGE_DISCORD=$(printf "%b" "Skipped: Previous job running.\nHost: \`$HOST_SAFE\`")
    echo "[$(date)] - SKIP: Lock file exists." >> "$LOG_DIR/skipped_runs.log"
    send_notification "telegram" "99" "$JOB_NAME" "$SKIP_MESSAGE_TELEGRAM"
    send_notification "discord" "99" "$JOB_NAME" "$SKIP_MESSAGE_DISCORD"
    exit 1
fi
touch "$LOCK_FILE"
trap 'rm -f "$LOCK_FILE"' EXIT HUP INT QUIT PIPE TERM

# --- Initial Notifications ---
HOST_SAFE=$(sanitize_for_telegram "$(hostname)")
SOURCE_SAFE=$(sanitize_for_telegram "$UNRAID_SOURCE_DIR")
DEST_SAFE=$(sanitize_for_telegram "$REMOTE_HOST:$REMOTE_DEST_DIR")

START_MESSAGE_DATA="Host: \`$HOST_SAFE\`
Source: \`$SOURCE_SAFE\`
Destination: \`$DEST_SAFE\`"

START_TELEGRAM_MESSAGE=$(printf "🚀 *Rsync Started \\($JOB_NAME_SAFE\\)*\n%s" "$START_MESSAGE_DATA")
START_DISCORD_MESSAGE="$START_MESSAGE_DATA"

# --- CONDITIONAL START NOTIFICATION ---
# Only send the "Started" message if we are NOT running in a batch
if [ -z "$AGGREGATE_REPORT" ]; then
    send_notification "telegram" "start" "$JOB_NAME" "$START_TELEGRAM_MESSAGE"
    send_notification "discord" "start" "$JOB_NAME" "$START_DISCORD_MESSAGE"
fi

echo "--- Starting rsync at $(date) ---" > "$LOG_FILE"

# --- RSYNC COMMAND ---
START_SECONDS=$(date +%s) # <--- 1. Start Timer

"$RSYNC_BIN_PATH" "${RSYNC_FLAGS[@]}" "$UNRAID_SOURCE_DIR" "$REMOTE_USER@$REMOTE_HOST:$REMOTE_DEST_DIR" 2>&1 | tee -a "$LOG_FILE"

RSYNC_EXIT_CODE=${PIPESTATUS[0]}
END_SECONDS=$(date +%s)   # <--- 2. Stop Timer
echo "--- rsync finished at $(date) with exit code $RSYNC_EXIT_CODE ---" >> "$LOG_FILE"

# --- Final Notifications ---
if [ "$RSYNC_EXIT_CODE" -eq 0 ]; then
    STATUS_EMOJI="✅"
    STATUS_TEXT="*Success \\($JOB_NAME_SAFE\\)*"
else
    STATUS_EMOJI="❌"
    STATUS_TEXT="*Failed \\($JOB_NAME_SAFE\\)* \\(Exit Code: $RSYNC_EXIT_CODE\\)"
fi

# --- PARSE RSYNC STATS (Fixed to handle 'reg' text) ---
if [ "$RSYNC_EXIT_CODE" -eq 0 ]; then
    NUM_FILES=$(grep "Number of files:" "$LOG_FILE" | awk -F: '{print $2}' | awk '{print $1}' | tr -d ',')
    FILES_TRANSFERRED=$(grep "Number of regular files transferred:" "$LOG_FILE" | awk -F: '{print $2}' | tr -d ' ,')
    SIZE_TRANSFERRED=$(grep "Total transferred file size:" "$LOG_FILE" | awk -F: '{print $2}' | xargs)
    TOTAL_SENT=$(grep "Total bytes sent:" "$LOG_FILE" | awk -F: '{print $2}' | xargs)

    SUMMARY_TEXT=""
    if [ "$FILES_TRANSFERRED" == "0" ]; then
        SUMMARY_TEXT="No new files to transfer.\n"
        SUMMARY_TEXT="${SUMMARY_TEXT}$(printf "%-20s %s" "Total Files Scanned:" "$NUM_FILES")"
    else
        SUMMARY_TEXT="${SUMMARY_TEXT}$(printf "%-20s %s" "Files Transferred:" "$FILES_TRANSFERRED")\n"
        SUMMARY_TEXT="${SUMMARY_TEXT}$(printf "%-20s %s" "Size Transferred:" "$SIZE_TRANSFERRED")\n"
        SUMMARY_TEXT="${SUMMARY_TEXT}$(printf "%-20s %s" "Total Network Sent:" "$TOTAL_SENT")"
    fi
else
    LAST_ERRORS=$(grep -i "rsync error" "$LOG_FILE" | tail -n 5)
    SUMMARY_TEXT="Rsync encountered errors.\n\nError Log:\n$LAST_ERRORS"
fi

FINAL_TELEGRAM_MESSAGE=$(printf "%b" "$STATUS_EMOJI *Sync* $STATUS_TEXT

*Host:* \`$HOST_SAFE\`
*Source:* \`$SOURCE_SAFE\`
*Destination:* \`$DEST_SAFE\`

📊 *Summary:*
\`\`\`
$SUMMARY_TEXT
\`\`\`

*Full log:* \`$(sanitize_for_telegram "$LOG_FILE")\`")

FINAL_DISCORD_MESSAGE=$(printf "%b" "Host: \`$HOST_SAFE\`
Source: \`$SOURCE_SAFE\`
Destination: \`$DEST_SAFE\`

Summary:
\`\`\`
$SUMMARY_TEXT
\`\`\`

Full log: \`$(sanitize_for_telegram "$LOG_FILE")\`")

# --- SMART NOTIFICATION LOGIC (With Stats) ---

# Calculate Duration
DURATION_SECONDS=$((END_SECONDS - START_SECONDS))
if [ "$DURATION_SECONDS" -gt 3600 ]; then
    DURATION_FMT=$(printf "%dh %dm %ds" $((DURATION_SECONDS/3600)) $((DURATION_SECONDS%3600/60)) $((DURATION_SECONDS%60)))
else
    DURATION_FMT=$(printf "%dm %ds" $((DURATION_SECONDS/60)) $((DURATION_SECONDS%60)))
fi

if [ -n "$AGGREGATE_REPORT" ] && [ "$RSYNC_EXIT_CODE" -eq 0 ]; then
    # SILENT SUCCESS: Write stats + time to the report file
    {
        echo "✅ **$JOB_NAME**"
        echo "Time:               $DURATION_FMT"
        echo "Files Transferred:  $FILES_TRANSFERRED"
        echo "Size Transferred:   $SIZE_TRANSFERRED"
        echo "Total Network Sent: $TOTAL_SENT"
        echo ""
    } >> "$AGGREGATE_REPORT"
else
    # LOUD FAILURE (or Manual Run)
    send_notification "telegram" "$RSYNC_EXIT_CODE" "$JOB_NAME" "$FINAL_TELEGRAM_MESSAGE"
    send_notification "discord" "$RSYNC_EXIT_CODE" "$JOB_NAME" "$FINAL_DISCORD_MESSAGE"

    if [ -n "$AGGREGATE_REPORT" ]; then
         echo "❌ **$JOB_NAME**: Failed (Exit Code: $RSYNC_EXIT_CODE)" >> "$AGGREGATE_REPORT"
    fi
fi

# --- Clean up old logs ---
find "$LOG_DIR" -name "rsync_*.log" -mtime +7 -delete
