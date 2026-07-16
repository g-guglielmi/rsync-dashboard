"""
Parses rsync log files produced by the user's unRAID backup scripts into
structured run records. Designed to be resilient to minor script variations:
- single-target scripts  ("--- rsync finished at ... with exit code N ---")
- multi-folder batch scripts ("--- All transfers finished at ... ---")

No log format is assumed beyond what's already emitted by the scripts;
nothing here requires modifying them.
"""
import os
import re
from datetime import datetime

LOG_FILENAME_RE = re.compile(r"_(\d{8})_(\d{6})\.log$")
START_LINE_RE = re.compile(r"--- Starting(?: Batch)? Rsync at (.+?) ---", re.I)
END_SINGLE_RE = re.compile(r"--- rsync finished at (.+?) with exit code (\d+) ---")
END_BATCH_RE = re.compile(r"--- All transfers finished at (.+?) ---")
BASH_DATE_RE = re.compile(
    r"^\w{3} (\w{3}) +(\d{1,2}) (\d{2}:\d{2}:\d{2}) \S+ (\d{4})$"
)

SIZE_RE = r"([\d,]+(?:\.\d+)?)\s*([KMGT]?)"
STAT_FIELDS = {
    "files_scanned": (r"Number of files:\s*" + SIZE_RE, False),
    "files_created": (r"Number of created files:\s*" + SIZE_RE, False),
    "files_deleted": (r"Number of deleted files:\s*" + SIZE_RE, False),
    "files_transferred": (r"Number of regular files transferred:\s*" + SIZE_RE, False),
    "size_transferred": (r"Total transferred file size:\s*" + SIZE_RE, True),
    "bytes_sent": (r"Total bytes sent:\s*" + SIZE_RE, True),
}

# The scripts run rsync with a single -h (human-readable level 2), which
# prints size suffixes in units of 1000, not 1024.
SIZE_MULT = {"": 1, "K": 1000, "M": 1000**2, "G": 1000**3, "T": 1000**4}

# Only treat a log with no completion marker as "still running" if it was
# touched recently; otherwise assume the job crashed / the container restarted.
RUNNING_GRACE_SECONDS = 6 * 3600

# rsync exit codes that mean "completed, but with caveats" rather than a real
# failure. 24 = "some files vanished before they could be transferred", which
# is routine when syncing a live filesystem.
WARNING_EXIT_CODES = {24}

# Completed runs never change on disk, so their parse result can be reused as
# long as (mtime, size) is unchanged. Keyed by absolute path.
_parse_cache = {}


def parse_bash_date(s):
    """Parses bash `date` default output, e.g. 'Sat Jul 11 03:00:01 CEST 2026'."""
    s = s.strip()
    m = BASH_DATE_RE.match(s)
    if not m:
        return None
    mon, day, time_, year = m.groups()
    try:
        return datetime.strptime(f"{mon} {day} {time_} {year}", "%b %d %H:%M:%S %Y")
    except ValueError:
        return None


def parse_size_token(value, suffix):
    value = float(value.replace(",", ""))
    return int(value * SIZE_MULT.get(suffix, 1))


def sum_field(content, pattern, is_size):
    total = 0
    found = False
    for m in re.finditer(pattern, content):
        found = True
        val, suffix = m.group(1), m.group(2)
        total += parse_size_token(val, suffix) if is_size else int(float(val.replace(",", "")))
    return total, found


def extract_stats(content):
    stats = {}
    for key, (pattern, is_size) in STAT_FIELDS.items():
        total, found = sum_field(content, pattern, is_size)
        stats[key] = total if found else 0
    return stats


def extract_errors(content, limit=10):
    lines = []
    for line in content.splitlines():
        if re.search(r"rsync error|rsync:.*failed|rsync warning|❌", line, re.I):
            lines.append(line.strip())
    return lines[-limit:]


def determine_status(content, single_m, batch_m):
    has_warning_text = bool(re.search(r"rsync warning", content, re.I))

    if single_m:
        code = int(single_m.group(2))
        if code == 0:
            return "warning" if has_warning_text else "success"
        if code in WARNING_EXIT_CODES:
            return "warning"
        return "failed"

    if batch_m:
        ok = len(re.findall(r"✅ Finished", content))
        bad = len(re.findall(r"❌ Error syncing", content))
        if bad == 0:
            return "warning" if has_warning_text else "success"
        if ok == 0:
            return "failed"
        return "warning"

    return None  # no completion marker -> caller decides running vs interrupted


def parse_log_file(path):
    try:
        st = os.stat(path)
    except OSError:
        return None

    cache_key = (st.st_mtime, st.st_size)
    cached = _parse_cache.get(path)
    if cached is not None and cached[0] == cache_key:
        return cached[1]

    result = _parse_log_content(path)
    # Only cache final results: a marker-less log must be re-evaluated each
    # time so "running" can age into "interrupted" even if the file is idle.
    if result and result["status"] in ("success", "warning", "failed"):
        _parse_cache[path] = (cache_key, result)
    return result


def prune_parse_cache():
    """Drops cache entries for log files that were deleted (e.g. by the
    scripts' own 7-day cleanup)."""
    for path in list(_parse_cache):
        if not os.path.exists(path):
            _parse_cache.pop(path, None)


def _parse_log_content(path):
    try:
        # Logs are written by bash scripts in UTF-8; don't depend on the
        # platform default encoding (the ✅/❌ markers break otherwise).
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError:
        return None

    filename = os.path.basename(path)

    start_time = None
    m = START_LINE_RE.search(content)
    if m:
        start_time = parse_bash_date(m.group(1))
    if start_time is None:
        fm = LOG_FILENAME_RE.search(filename)
        if fm:
            try:
                start_time = datetime.strptime(fm.group(1) + fm.group(2), "%Y%m%d%H%M%S")
            except ValueError:
                start_time = None

    single_m = END_SINGLE_RE.search(content)
    batch_m = None if single_m else END_BATCH_RE.search(content)

    end_time = None
    end_m = single_m or batch_m
    if end_m:
        end_time = parse_bash_date(end_m.group(1))

    status = determine_status(content, single_m, batch_m)
    if status is None:
        mtime = os.path.getmtime(path)
        age = datetime.now().timestamp() - mtime
        status = "running" if age < RUNNING_GRACE_SECONDS else "interrupted"

    duration_seconds = None
    if start_time and end_time:
        duration_seconds = max(0, int((end_time - start_time).total_seconds()))

    stats = extract_stats(content)
    errors = extract_errors(content) if status in ("failed", "warning", "interrupted") else []

    return {
        "filename": filename,
        "status": status,
        "start_time": start_time.isoformat() if start_time else None,
        "end_time": end_time.isoformat() if end_time else None,
        "duration_seconds": duration_seconds,
        "files_scanned": stats["files_scanned"],
        "files_created": stats["files_created"],
        "files_deleted": stats["files_deleted"],
        "files_transferred": stats["files_transferred"],
        "size_transferred_bytes": stats["size_transferred"],
        "bytes_sent": stats["bytes_sent"],
        "errors": errors,
    }


def discover_jobs(logs_root):
    """Returns [{server, category, path}] for every leaf job folder under logs_root."""
    jobs = []
    if not os.path.isdir(logs_root):
        return jobs
    for server in sorted(os.listdir(logs_root)):
        server_path = os.path.join(logs_root, server)
        if not os.path.isdir(server_path):
            continue
        for category in sorted(os.listdir(server_path)):
            category_path = os.path.join(server_path, category)
            if not os.path.isdir(category_path):
                continue
            jobs.append({"server": server, "category": category, "path": category_path})
    return jobs


def get_job_runs(job_path, limit=20):
    """Returns parsed runs for a job folder, most recent first.

    Only files matching the timestamped run-log pattern (rsync_..._YYYYMMDD_HHMMSS.log)
    are treated as runs. This deliberately excludes side files the scripts also write,
    such as skipped_runs.log.
    """
    if not os.path.isdir(job_path):
        return []
    files = [f for f in os.listdir(job_path) if LOG_FILENAME_RE.search(f)]

    def sort_key(fname):
        m = LOG_FILENAME_RE.search(fname)
        return m.group(1) + m.group(2)

    files.sort(key=sort_key, reverse=True)
    runs = []
    for fname in files[:limit]:
        parsed = parse_log_file(os.path.join(job_path, fname))
        if parsed:
            runs.append(parsed)
    return runs


def get_dashboard_data(logs_root, history_limit=15):
    prune_parse_cache()
    jobs = discover_jobs(logs_root)
    job_results = []
    for job in jobs:
        runs = get_job_runs(job["path"], limit=history_limit)
        job_results.append({
            "server": job["server"],
            "category": job["category"],
            "runs": runs,
            "latest": runs[0] if runs else None,
        })

    overview = {
        "success": 0, "warning": 0, "failed": 0, "running": 0, "interrupted": 0, "no_data": 0,
        "total_transferred_bytes": 0, "total_deleted_files": 0,
    }
    for j in job_results:
        latest = j["latest"]
        if not latest:
            overview["no_data"] += 1
            continue
        overview[latest["status"]] = overview.get(latest["status"], 0) + 1
        overview["total_transferred_bytes"] += latest["size_transferred_bytes"]
        overview["total_deleted_files"] += latest["files_deleted"]

    return {"jobs": job_results, "overview": overview}
