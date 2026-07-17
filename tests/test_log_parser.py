import os
import time

import pytest

import log_parser
from log_parser import (
    daily_transfers,
    discover_jobs,
    get_dashboard_data,
    get_job_runs,
    parse_bash_date,
    parse_log_file,
    parse_size_token,
)


# Log lines below mirror exactly what the unRAID scripts + `rsync -avh --stats`
# produce (lowercase "Starting rsync", 1000-based K/M/G suffixes).
SINGLE_SUCCESS = """--- Starting rsync at Sat Jul 11 03:00:01 CEST 2026 ---
sending incremental file list
Number of files: 107,309 (reg: 88,275, dir: 19,034)
Number of created files: 12
Number of deleted files: 3
Number of regular files transferred: 42
Total transferred file size: 1,234,567 bytes
Total bytes sent: 987,654
--- rsync finished at Sat Jul 11 03:12:41 CEST 2026 with exit code 0 ---
"""

SINGLE_SUCCESS_SUFFIXED = """--- Starting rsync at Sat Jul 11 03:00:01 CEST 2026 ---
Number of files: 107,309 (reg: 88,275, dir: 19,034)
Number of regular files transferred: 42
Total transferred file size: 1.53G bytes
Total bytes sent: 8.79K
--- rsync finished at Sat Jul 11 03:12:41 CEST 2026 with exit code 0 ---
"""

SINGLE_FAILED = """--- Starting rsync at Sat Jul 11 03:00:01 CEST 2026 ---
rsync error: some files/attrs were not transferred (code 23) at main.c(1338)
--- rsync finished at Sat Jul 11 03:01:00 CEST 2026 with exit code 23 ---
"""

SINGLE_VANISHED = """--- Starting rsync at Sat Jul 11 03:00:01 CEST 2026 ---
file has vanished: "/mnt/user/data/tmpfile"
rsync warning: some files vanished before they could be transferred (code 24)
--- rsync finished at Sat Jul 11 03:05:00 CEST 2026 with exit code 24 ---
"""

BATCH_MIXED = """--- Starting Batch Rsync at Sat Jul 11 04:00:00 CEST 2026 ---
✅ Finished folder-a
❌ Error syncing folder-b (Code: 23)
--- All transfers finished at Sat Jul 11 04:30:00 CEST 2026 ---
"""

BATCH_ALL_FAILED = """--- Starting Batch Rsync at Sat Jul 11 04:00:00 CEST 2026 ---
❌ Error syncing folder-a (Code: 23)
❌ Error syncing folder-b (Code: 23)
--- All transfers finished at Sat Jul 11 04:30:00 CEST 2026 ---
"""

NO_MARKER = """--- Starting rsync at Sat Jul 11 03:00:01 CEST 2026 ---
sending incremental file list
"""

# Format written by docker-backup.sh (multi-VM docker pull backup) after it
# was aligned with the dashboard's batch markers.
DOCKER_VM_BATCH = """--- Starting Batch Rsync at Fri Jul 10 04:15:02 CEST 2026 ---
🚀 Triggering backup on vm-one...
   -> Pulling results from /home/backup-user/docker_backup_20260710_041503...
receiving incremental file list
docker_app.tar.gz
Number of files: 11 (reg: 10, dir: 1)
Number of regular files transferred: 10
Total transferred file size: 94.33M bytes
Total bytes sent: 229
   ✅ Finished vm-one
🚀 Triggering backup on vm-two...
receiving incremental file list
Number of files: 9 (reg: 8, dir: 1)
Number of regular files transferred: 8
Total transferred file size: 73.31M bytes
Total bytes sent: 210
   ✅ Finished vm-two
--- All transfers finished at Fri Jul 10 04:16:04 CEST 2026 ---
"""

DOCKER_VM_BATCH_ONE_FAILED = DOCKER_VM_BATCH.replace(
    "   ✅ Finished vm-two",
    "   ❌ Error syncing vm-two: rsync exited with code 12")


@pytest.fixture(autouse=True)
def clear_cache():
    log_parser._parse_cache.clear()
    yield
    log_parser._parse_cache.clear()


def write_log(dirpath, name, content, mtime=None):
    path = os.path.join(str(dirpath), name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def test_parse_bash_date():
    d = parse_bash_date("Sat Jul 11 03:00:01 CEST 2026")
    assert d is not None
    assert (d.year, d.month, d.day, d.hour, d.minute, d.second) == (2026, 7, 11, 3, 0, 1)
    assert parse_bash_date("not a date") is None


def test_parse_size_token():
    # rsync -h (single) uses 1000-based suffixes
    assert parse_size_token("1,234,567", "") == 1234567
    assert parse_size_token("1.5", "K") == 1500
    assert parse_size_token("2", "G") == 2 * 1000**3


def test_single_success(tmp_path):
    path = write_log(tmp_path, "rsync_media_20260711_030001.log", SINGLE_SUCCESS)
    run = parse_log_file(path)
    assert run["status"] == "success"
    assert run["start_time"] == "2026-07-11T03:00:01"
    assert run["end_time"] == "2026-07-11T03:12:41"
    assert run["duration_seconds"] == 760
    assert run["files_scanned"] == 107309
    assert run["files_created"] == 12
    assert run["files_deleted"] == 3
    assert run["files_transferred"] == 42
    assert run["size_transferred_bytes"] == 1234567
    assert run["bytes_sent"] == 987654
    assert run["errors"] == []


def test_single_success_with_suffixed_sizes(tmp_path):
    path = write_log(tmp_path, "rsync_media_20260711_030001.log", SINGLE_SUCCESS_SUFFIXED)
    run = parse_log_file(path)
    assert run["status"] == "success"
    assert run["size_transferred_bytes"] == 1_530_000_000
    assert run["bytes_sent"] == 8790


def test_single_failed_collects_errors(tmp_path):
    path = write_log(tmp_path, "rsync_media_20260711_030001.log", SINGLE_FAILED)
    run = parse_log_file(path)
    assert run["status"] == "failed"
    assert any("rsync error" in e for e in run["errors"])


def test_vanished_files_is_warning_not_failure(tmp_path):
    path = write_log(tmp_path, "rsync_media_20260711_030001.log", SINGLE_VANISHED)
    run = parse_log_file(path)
    assert run["status"] == "warning"


def test_batch_mixed_is_warning(tmp_path):
    path = write_log(tmp_path, "rsync_batch_20260711_040000.log", BATCH_MIXED)
    run = parse_log_file(path)
    assert run["status"] == "warning"
    assert run["end_time"] == "2026-07-11T04:30:00"


def test_batch_all_failed(tmp_path):
    path = write_log(tmp_path, "rsync_batch_20260711_040000.log", BATCH_ALL_FAILED)
    assert parse_log_file(path)["status"] == "failed"


def test_docker_vm_batch_success(tmp_path):
    path = write_log(tmp_path, "backup_20260710_041502.log", DOCKER_VM_BATCH)
    run = parse_log_file(path)
    assert run["status"] == "success"
    assert run["start_time"] == "2026-07-10T04:15:02"
    assert run["end_time"] == "2026-07-10T04:16:04"
    assert run["files_transferred"] == 18
    assert run["size_transferred_bytes"] == 94_330_000 + 73_310_000


def test_docker_vm_batch_partial_failure_is_warning(tmp_path):
    path = write_log(tmp_path, "backup_20260710_041502.log", DOCKER_VM_BATCH_ONE_FAILED)
    run = parse_log_file(path)
    assert run["status"] == "warning"
    assert any("vm-two" in e for e in run["errors"])


def test_no_marker_recent_is_running(tmp_path):
    path = write_log(tmp_path, "rsync_media_20260711_030001.log", NO_MARKER)
    assert parse_log_file(path)["status"] == "running"


def test_no_marker_old_is_interrupted(tmp_path):
    old = time.time() - log_parser.RUNNING_GRACE_SECONDS - 60
    path = write_log(tmp_path, "rsync_media_20260711_030001.log", NO_MARKER, mtime=old)
    assert parse_log_file(path)["status"] == "interrupted"


def test_start_time_falls_back_to_filename(tmp_path):
    path = write_log(tmp_path, "rsync_media_20260711_030001.log",
                     "no start line here\n--- rsync finished at Sat Jul 11 03:12:41 CEST 2026 with exit code 0 ---\n")
    run = parse_log_file(path)
    assert run["start_time"] == "2026-07-11T03:00:01"


def test_completed_runs_are_cached_and_invalidated(tmp_path):
    path = write_log(tmp_path, "rsync_media_20260711_030001.log", SINGLE_SUCCESS)
    first = parse_log_file(path)
    assert parse_log_file(path) is first  # served from cache

    # Change the file (size + mtime change) -> cache entry is refreshed
    write_log(tmp_path, "rsync_media_20260711_030001.log", SINGLE_FAILED,
              mtime=time.time() + 10)
    assert parse_log_file(path)["status"] == "failed"


def test_running_logs_are_not_cached(tmp_path):
    path = write_log(tmp_path, "rsync_media_20260711_030001.log", NO_MARKER)
    parse_log_file(path)
    assert path not in log_parser._parse_cache


def test_discover_jobs_and_runs(tmp_path):
    job_dir = tmp_path / "tower" / "media"
    job_dir.mkdir(parents=True)
    write_log(job_dir, "rsync_media_20260710_030001.log", SINGLE_SUCCESS)
    write_log(job_dir, "rsync_media_20260711_030001.log", SINGLE_FAILED)
    write_log(job_dir, "skipped_runs.log", "not a run log")

    jobs = discover_jobs(str(tmp_path))
    assert [(j["server"], j["category"]) for j in jobs] == [("tower", "media")]

    runs = get_job_runs(jobs[0]["path"])
    assert len(runs) == 2  # skipped_runs.log excluded
    assert runs[0]["filename"] == "rsync_media_20260711_030001.log"  # newest first
    assert runs[0]["status"] == "failed"


def test_dashboard_overview(tmp_path):
    for server, category, content in [
        ("tower", "media", SINGLE_SUCCESS),
        ("tower", "docs", SINGLE_FAILED),
    ]:
        d = tmp_path / server / category
        d.mkdir(parents=True, exist_ok=True)
        write_log(d, "rsync_x_20260711_030001.log", content)
    (tmp_path / "tower" / "empty").mkdir()

    data = get_dashboard_data(str(tmp_path))
    o = data["overview"]
    assert o["success"] == 1
    assert o["failed"] == 1
    assert o["no_data"] == 1
    assert o["total_transferred_bytes"] == 1234567


def test_daily_transfers():
    from datetime import date

    def run(day, size):
        return {"start_time": f"{day}T05:30:01", "size_transferred_bytes": size}

    jobs = [
        {"runs": [run("2026-07-16", 100), run("2026-07-15", 50), run("2026-07-01", 999)]},
        {"runs": [run("2026-07-16", 25)]},
        {"runs": []},
    ]
    result = daily_transfers(jobs, days=7, today=date(2026, 7, 16))

    assert len(result) == 7
    assert result[0]["date"] == "2026-07-10"   # oldest first, fixed 7-day window
    assert result[-1]["date"] == "2026-07-16"
    assert result[-1]["bytes"] == 125          # summed across jobs
    assert result[-2]["bytes"] == 50
    assert sum(r["bytes"] for r in result) == 175  # July 1 outside window, excluded


def test_dashboard_includes_daily_transfers(tmp_path):
    d = tmp_path / "tower" / "media"
    d.mkdir(parents=True)
    write_log(d, "rsync_media_20260711_030001.log", SINGLE_SUCCESS)
    data = get_dashboard_data(str(tmp_path))
    assert len(data["daily_transfers"]) == 7
    assert all("date" in e and "bytes" in e for e in data["daily_transfers"])
    # each job also carries its own per-job daily series
    for job in data["jobs"]:
        assert len(job["daily_transfers"]) == 7


def test_prune_parse_cache(tmp_path):
    path = write_log(tmp_path, "rsync_media_20260711_030001.log", SINGLE_SUCCESS)
    parse_log_file(path)
    assert path in log_parser._parse_cache
    os.remove(path)
    log_parser.prune_parse_cache()
    assert path not in log_parser._parse_cache
