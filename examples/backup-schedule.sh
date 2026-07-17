#!/bin/bash

# ---------------------------------------------------------------------------
# EXAMPLE — anonymized master wrapper that runs each per-job script
# (like rsync-job.sh) in sequence.
#
# Two things here matter to the dashboard:
#   1. If the remote is unreachable it bails early — so it writes a short
#      "failed" log per job, otherwise the dashboard would keep showing the
#      previous run's status forever (see the failsafe block below).
#   2. IPs / server names are placeholders (192.0.2.x, RFC 5737). Replace
#      them, and adjust JOB_CATEGORIES + the child-script paths to match yours.
# ---------------------------------------------------------------------------

# 1. Create a shared variable for the report file
export AGGREGATE_REPORT="/tmp/backup_report_$(date +%s).txt"
NOTIFY_SCRIPT="/boot/config/scripts/rsync_notify.sh"

# 2. Header for the report
echo "**Backup Batch Run: $(date)**" > "$AGGREGATE_REPORT"

# --- Send "Batch Started" Notification (Blue) ---
START_MSG="🚀 **Backup Batch Started**
📅 Date: $(date)"

bash "$NOTIFY_SCRIPT" "telegram" "batch" "Batch Started" "$START_MSG"
bash "$NOTIFY_SCRIPT" "discord" "batch" "Batch Started" "$START_MSG"

# --- Failsafe: Check if Remote Server is Online ---
REMOTE_IP="192.0.2.10"   # placeholder — your backup server's IP
# Must match the LOG_SERVER_NAME / LOG_JOB_CATEGORY of the child scripts,
# so the dashboard files these under the right jobs.
LOG_ROOT="/mnt/user/appdata/rsync_logs/BackupServer"
JOB_CATEGORIES=("Backup" "Documents" "Immich" "ISO" "Media-Temp" "Media")

# Sends 1 packet (-c 1) and waits a maximum of 3 seconds (-W 3)
if ! ping -c 1 -W 3 "$REMOTE_IP" > /dev/null 2>&1; then
    echo -e "\n❌ **Critical Error**: Remote server ($REMOTE_IP) is offline. Skipping all backup tasks." >> "$AGGREGATE_REPORT"

    # Write a "failed" run log for every job, so the dashboard shows the
    # skipped night as failed instead of silently keeping the old status.
    TS=$(date +%Y%m%d_%H%M%S)
    for CATEGORY in "${JOB_CATEGORIES[@]}"; do
        JOB_LOG_DIR="$LOG_ROOT/$CATEGORY"
        mkdir -p "$JOB_LOG_DIR"
        {
            echo "--- Starting rsync at $(date) ---"
            echo "rsync error: remote server $REMOTE_IP is offline - all backup tasks skipped by BackupSchedule failsafe"
            echo "--- rsync finished at $(date) with exit code 255 ---"
        } > "$JOB_LOG_DIR/rsync_offline_$TS.log"
    done

    # Read the aborted report and send a single failure notification
    FINAL_REPORT=$(cat "$AGGREGATE_REPORT")
    bash "$NOTIFY_SCRIPT" "telegram" "batch_warning" "Batch Target Offline" "$FINAL_REPORT"
    bash "$NOTIFY_SCRIPT" "discord" "batch_warning" "Batch Target Offline" "$FINAL_REPORT"

    # Clean up and stop execution completely
    rm -f "$AGGREGATE_REPORT"
    exit 1
fi

# 3. Run Scripts Sequentially
# We use ';' instead of '&&' so ALL scripts run, even if one fails.
# (These paths point at your own per-job scripts — one per JOB_CATEGORY above.)
bash "/boot/config/plugins/user.scripts/scripts/00_BackupServer - Backup/script" ; \
bash "/boot/config/plugins/user.scripts/scripts/00_BackupServer - Documents/script" ; \
bash "/boot/config/plugins/user.scripts/scripts/00_BackupServer - Immich/script" ; \
bash "/boot/config/plugins/user.scripts/scripts/00_BackupServer - ISO/script" ; \
bash "/boot/config/plugins/user.scripts/scripts/00_BackupServer - Media-Temp/script" ; \
bash "/boot/config/plugins/user.scripts/scripts/00_BackupServer - Media/script"

# 4. Analyze Results and Send Summary
FINAL_REPORT=$(cat "$AGGREGATE_REPORT")

# Check if the report contains the specific failure emoji we used in the scripts
if grep -q "❌" "$AGGREGATE_REPORT"; then
    # At least one job failed
    BATCH_STATUS="batch_warning"
else
    # All jobs succeeded
    BATCH_STATUS="batch_success"
fi

bash "$NOTIFY_SCRIPT" "telegram" "$BATCH_STATUS" "Batch Summary" "$FINAL_REPORT"
bash "$NOTIFY_SCRIPT" "discord" "$BATCH_STATUS" "Batch Summary" "$FINAL_REPORT"

# 5. Cleanup
rm -f "$AGGREGATE_REPORT"
