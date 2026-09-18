"""ops/backup-live-state.sh against a configured OBLIGATIONS_DB_PATH
(2026-09-15/16 review F07): the snapshot, its check, the archive and the
metadata must all describe the database the gateway is configured to use,
never the default path beside it. The script runs for real with sqlite3
and tar; docker, systemctl and logger are stubs; the status probes are
stubs; the snapshot verifier is the real one."""
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import tarfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent

pytestmark = pytest.mark.skipif(shutil.which("sqlite3") is None or shutil.which("tar") is None,
                                reason="the backup needs sqlite3 and tar on the host")


def create_db(path, payment_hash):
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE obligations (payment_hash TEXT PRIMARY KEY, digest TEXT, status TEXT)")
        db.execute("INSERT INTO obligations VALUES (?, ?, ?)", (payment_hash, "ab" * 32, "settled"))


def stub(path, body="exit 0"):
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def run_backup(base, env_lines):
    fixture = base / "repo"
    (fixture / "ops").mkdir(parents=True)
    (fixture / ".env").write_text(env_lines)
    for name in ("status.sh", "phoenixd-status.sh", "otsd-status.sh", "list-proofs.sh"):
        stub(fixture / "ops" / name)
    os.symlink(REPO / "ops" / "verify-obligations-snapshot.sh", fixture / "ops" / "verify-obligations-snapshot.sh")
    (fixture / "ops" / "lib").mkdir()
    os.symlink(REPO / "ops" / "lib" / "env.sh", fixture / "ops" / "lib" / "env.sh")
    bins = base / "bin"
    bins.mkdir()
    for tool in ("docker", "systemctl", "logger"):
        stub(bins / tool, "exit 1")
    for name in ("phoenix", "calendar", "fork"):
        (base / name).mkdir()
    # A clean environment: the app's own tests export OBLIGATIONS_DB_PATH
    # (":memory:") and other gateway settings, and the script reads the
    # environment for anything .env does not set.
    env = {"PATH": str(bins) + ":" + os.environ["PATH"], "HOME": os.environ.get("HOME", str(base)),
           "LANG": "C", "REPO_DIR": str(fixture), "STATE_DIR": str(base / "state"),
           "PHOENIX_HOME": str(base / "phoenix"), "OTSD_CALENDAR_DIR": str(base / "calendar"),
           "OTSD_FORK_PATH": str(base / "fork"), "TOR_KEYS_DIR": "", "ANCHOR_RECEIPTS_DIR": "",
           "ARTIFACTS": "", "GATEWAY_UNIT": "", "PHOENIXD_UNIT": "", "SOCAT_UNIT": "",
           "BACKUP_REMOTE": "", "BACKUP_AGE_RECIPIENT": ""}
    # The script under test is the repository's own, not a copy in the fixture.
    return subprocess.run(["/bin/bash", str(REPO / "ops" / "backup-live-state.sh")],
                          env=env, capture_output=True, text=True, timeout=60)


def snapshot_rows(archive):
    with tarfile.open(archive) as tf:
        names = tf.getnames()
        member = next(n for n in names if n.endswith("/obligations.db.snapshot"))
        raw = tf.extractfile(member).read()
        metadata = next(n for n in names if n.endswith("/metadata.txt"))
        meta = tf.extractfile(metadata).read().decode()
    copy = archive.parent / "restored.db"
    copy.write_bytes(raw)
    with sqlite3.connect(copy) as db:
        rows = [r[0] for r in db.execute("SELECT payment_hash FROM obligations")]
    return names, rows, meta


def test_the_backup_snapshots_the_configured_obligations_database(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    live = tmp_path / "custom-live"
    live.mkdir()
    create_db(state / "obligations.db", "11" * 32)      # an old default-path file
    create_db(live / "actual.db", "22" * 32)            # the configured live database
    status = tmp_path / "backup-status"
    backups = tmp_path / "backups"
    run = run_backup(tmp_path, f"BACKUP_ROOT={backups}\nBACKUP_STATUS_PATH={status}\n"
                               f"OBLIGATIONS_DB_PATH={live / 'actual.db'}\n")
    assert run.returncode == 0, run.stdout + run.stderr
    report = json.loads(status.read_text())
    archive = next(backups.glob("*-live-state.tar.gz"))
    names, rows, meta = snapshot_rows(archive)
    assert rows == ["22" * 32], "the snapshot must be of the configured database"
    assert any(n.endswith("custom-live/actual.db") for n in names), "the configured database travels in the archive"
    assert f"obligations_db_path: {live / 'actual.db'}" in meta
    assert "obligations_newest_payment_hash: " + "22" * 32 in meta
    assert report["status"] == "local_only"


def test_the_default_path_is_used_when_nothing_is_configured(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    create_db(state / "obligations.db", "11" * 32)
    status = tmp_path / "backup-status"
    backups = tmp_path / "backups"
    run = run_backup(tmp_path, f"BACKUP_ROOT={backups}\nBACKUP_STATUS_PATH={status}\n")
    assert run.returncode == 0, run.stdout + run.stderr
    archive = next(backups.glob("*-live-state.tar.gz"))
    names, rows, meta = snapshot_rows(archive)
    assert rows == ["11" * 32]
    assert f"obligations_db_path: {state / 'obligations.db'}" in meta


# The calendar backup boundary (workflow five, gate ruling 3, 2026-09-18).
# A copy of the calendar directory taken while otsd writes is a hot copy:
# the calendar's own contract calls only a stopped copy a backup (fork
# docs/contracts.md, section 10, R1). The script now succeeds on the
# calendar member only at a boundary it established or verified; without
# one it refuses success and says why. Each test failed before the fix.

def run_backup_with(base, env_lines, systemctl_body="exit 1", docker_body="exit 1", **extra_env):
    """As run_backup, with the systemctl and docker stubs supplied by the
    test (they record their calls in base/calls.log) and extra environment."""
    fixture = base / "repo"
    (fixture / "ops").mkdir(parents=True, exist_ok=True)
    (fixture / ".env").write_text(env_lines)
    for name in ("status.sh", "phoenixd-status.sh", "otsd-status.sh", "list-proofs.sh"):
        stub(fixture / "ops" / name)
    if not (fixture / "ops" / "verify-obligations-snapshot.sh").exists():
        os.symlink(REPO / "ops" / "verify-obligations-snapshot.sh", fixture / "ops" / "verify-obligations-snapshot.sh")
        (fixture / "ops" / "lib").mkdir()
        os.symlink(REPO / "ops" / "lib" / "env.sh", fixture / "ops" / "lib" / "env.sh")
    bins = base / "bin"
    bins.mkdir(exist_ok=True)
    calls = base / "calls.log"
    stub(bins / "logger", "exit 1")
    stub(bins / "systemctl", f'echo "systemctl $*" >> "{calls}"\n' + systemctl_body)
    stub(bins / "docker", f'echo "docker $*" >> "{calls}"\n' + docker_body)
    for name in ("phoenix", "calendar", "fork"):
        (base / name).mkdir(exist_ok=True)
    (base / "calendar" / "journal").write_bytes(b"\x00" * 44)
    state = base / "state"
    state.mkdir(exist_ok=True)
    if not (state / "obligations.db").exists():
        create_db(state / "obligations.db", "11" * 32)
    env = {"PATH": str(bins) + ":" + os.environ["PATH"], "HOME": os.environ.get("HOME", str(base)),
           "LANG": "C", "REPO_DIR": str(fixture), "STATE_DIR": str(state),
           "PHOENIX_HOME": str(base / "phoenix"), "OTSD_CALENDAR_DIR": str(base / "calendar"),
           "OTSD_FORK_PATH": str(base / "fork"), "TOR_KEYS_DIR": "", "ANCHOR_RECEIPTS_DIR": "",
           "ARTIFACTS": "", "GATEWAY_UNIT": "", "PHOENIXD_UNIT": "", "SOCAT_UNIT": "",
           "BACKUP_REMOTE": "", "BACKUP_AGE_RECIPIENT": ""}
    env.update(extra_env)
    run = subprocess.run(["/bin/bash", str(REPO / "ops" / "backup-live-state.sh")],
                         env=env, capture_output=True, text=True, timeout=60)
    calls_text = calls.read_text() if calls.exists() else ""
    return run, calls_text


def _report_and_meta(tmp_path, backups, status):
    report = json.loads(status.read_text())
    archives = list(backups.glob("*-live-state.tar.gz"))
    meta = ""
    if archives:
        with tarfile.open(archives[0]) as tf:
            member = next(n for n in tf.getnames() if n.endswith("/metadata.txt"))
            meta = tf.extractfile(member).read().decode()
    return report, archives, meta


OTSD_RUNNING = 'case "$*" in *is-active*otsd*) exit 0 ;; *) exit 1 ;; esac'


def test_a_hot_copy_of_a_running_calendar_is_never_a_successful_backup(tmp_path):
    status, backups = tmp_path / "backup-status", tmp_path / "backups"
    run, calls = run_backup_with(tmp_path, f"BACKUP_ROOT={backups}\nBACKUP_STATUS_PATH={status}\n",
                                 systemctl_body=OTSD_RUNNING)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "systemctl is-active --quiet otsd" in calls, calls   # the writer was asked
    report, archives, meta = _report_and_meta(tmp_path, backups, status)
    assert report["status"] == "failed", report
    assert "hot copy" in report["detail"] and "CALENDAR_BACKUP_BOUNDARY" in report["detail"], report
    assert archives, "the degraded archive is still made"
    assert "calendar_backup_boundary: none" in meta, meta
    assert "systemctl stop" not in calls


def test_the_stop_boundary_stops_the_calendar_copies_it_and_restarts_it(tmp_path):
    status, backups = tmp_path / "backup-status", tmp_path / "backups"
    stopped = tmp_path / "otsd.stopped"
    body = (f'case "$*" in\n'
            f'  *is-active*otsd*) [ -e "{stopped}" ] && exit 3 || exit 0 ;;\n'
            f'  *stop*otsd*) touch "{stopped}"; exit 0 ;;\n'
            f'  *start*otsd*) rm -f "{stopped}"; exit 0 ;;\n'
            f'  *) exit 1 ;;\n'
            f'esac')
    run, calls = run_backup_with(tmp_path, f"BACKUP_ROOT={backups}\nBACKUP_STATUS_PATH={status}\n",
                                 systemctl_body=body, CALENDAR_BACKUP_BOUNDARY="stop")
    assert run.returncode == 0, run.stdout + run.stderr
    lines = [l for l in calls.splitlines() if "otsd" in l]
    assert any("stop otsd" in l for l in lines) and any("start otsd" in l for l in lines), calls
    assert lines.index(next(l for l in lines if "stop otsd" in l)) < lines.index(next(l for l in lines if "start otsd" in l))
    # Seen stopped before the copy, restarted after it, and the run says so in order.
    out = run.stdout
    assert out.index("calendar writer stopped and seen stopped") < out.index("=== creating sensitive archive ===") < out.index("calendar writer restarted")
    assert not stopped.exists(), "the writer was left stopped"
    report, archives, meta = _report_and_meta(tmp_path, backups, status)
    assert report["status"] == "local_only", report
    assert "calendar_backup_boundary: stop (" in meta, meta


def test_the_stop_boundary_that_cannot_stop_the_writer_is_a_failed_backup(tmp_path):
    status, backups = tmp_path / "backup-status", tmp_path / "backups"
    body = 'case "$*" in *is-active*otsd*) exit 0 ;; *stop*otsd*) exit 0 ;; *start*otsd*) exit 0 ;; *) exit 1 ;; esac'
    run, calls = run_backup_with(tmp_path, f"BACKUP_ROOT={backups}\nBACKUP_STATUS_PATH={status}\n",
                                 systemctl_body=body, CALENDAR_BACKUP_BOUNDARY="stop")
    assert run.returncode == 0, run.stdout + run.stderr
    assert "systemctl stop otsd" in calls, calls
    report, archives, meta = _report_and_meta(tmp_path, backups, status)
    assert report["status"] == "failed", report
    assert "did not stop" in report["detail"], report
    assert "calendar_backup_boundary: none" in meta, meta


def test_the_stopped_boundary_with_a_running_calendar_is_a_failed_backup(tmp_path):
    status, backups = tmp_path / "backup-status", tmp_path / "backups"
    run, calls = run_backup_with(tmp_path, f"BACKUP_ROOT={backups}\nBACKUP_STATUS_PATH={status}\n",
                                 systemctl_body=OTSD_RUNNING, CALENDAR_BACKUP_BOUNDARY="stopped")
    assert run.returncode == 0, run.stdout + run.stderr
    report, archives, meta = _report_and_meta(tmp_path, backups, status)
    assert report["status"] == "failed", report
    assert "CALENDAR_BACKUP_BOUNDARY=stopped" in report["detail"], report
    assert "systemctl stop" not in calls, "the stopped boundary never stops anything itself"


def test_no_calendar_writer_running_satisfies_the_stopped_boundary(tmp_path):
    status, backups = tmp_path / "backup-status", tmp_path / "backups"
    run, calls = run_backup_with(tmp_path, f"BACKUP_ROOT={backups}\nBACKUP_STATUS_PATH={status}\n")
    assert run.returncode == 0, run.stdout + run.stderr
    report, archives, meta = _report_and_meta(tmp_path, backups, status)
    assert report["status"] == "local_only", report
    assert "calendar_backup_boundary: stopped (no calendar writer was running)" in meta, meta


def test_a_calendar_that_does_not_restart_after_the_copy_is_a_failed_backup(tmp_path):
    status, backups = tmp_path / "backup-status", tmp_path / "backups"
    stopped = tmp_path / "otsd.stopped"
    body = (f'case "$*" in\n'
            f'  *is-active*otsd*) [ -e "{stopped}" ] && exit 3 || exit 0 ;;\n'
            f'  *stop*otsd*) touch "{stopped}"; exit 0 ;;\n'
            f'  *start*otsd*) exit 1 ;;\n'
            f'  *) exit 1 ;;\n'
            f'esac')
    run, calls = run_backup_with(tmp_path, f"BACKUP_ROOT={backups}\nBACKUP_STATUS_PATH={status}\n",
                                 systemctl_body=body, CALENDAR_BACKUP_BOUNDARY="stop")
    assert run.returncode == 0, run.stdout + run.stderr
    assert "systemctl start otsd" in calls, calls
    report, archives, meta = _report_and_meta(tmp_path, backups, status)
    assert report["status"] == "failed", report
    assert "did not restart" in report["detail"], report
    assert archives, "the copy taken at the boundary is still archived"
    assert "calendar_backup_boundary: stop (" in meta, meta


def test_the_snapshot_boundary_archives_the_declared_snapshot_in_place_of_the_live_directory(tmp_path):
    status, backups = tmp_path / "backup-status", tmp_path / "backups"
    snapshot = tmp_path / "snap"
    snapshot.mkdir()
    (snapshot / "journal").write_bytes(b"\x00" * 44)
    run, calls = run_backup_with(tmp_path, f"BACKUP_ROOT={backups}\nBACKUP_STATUS_PATH={status}\n",
                                 systemctl_body=OTSD_RUNNING, CALENDAR_BACKUP_BOUNDARY="snapshot",
                                 OTSD_CALENDAR_SNAPSHOT_DIR=str(snapshot))
    assert run.returncode == 0, run.stdout + run.stderr
    report, archives, meta = _report_and_meta(tmp_path, backups, status)
    assert report["status"] == "local_only", report
    assert f"calendar_backup_boundary: snapshot (operator-declared: {snapshot})" in meta, meta
    with tarfile.open(archives[0]) as tf:
        names = tf.getnames()
    assert any(n.endswith("/snap/journal") for n in names), names
    assert not any("/calendar/journal" in n for n in names), "the live directory was archived beside the snapshot"
    assert "systemctl stop" not in calls
    # The same declaration without a snapshot on disk is a failed run, and the
    # live directory is archived as the hot copy it is.
    run, calls = run_backup_with(tmp_path, f"BACKUP_ROOT={backups}\nBACKUP_STATUS_PATH={status}\n",
                                 systemctl_body=OTSD_RUNNING, CALENDAR_BACKUP_BOUNDARY="snapshot",
                                 OTSD_CALENDAR_SNAPSHOT_DIR=str(tmp_path / "no-such-snapshot"))
    report = json.loads(status.read_text())
    assert report["status"] == "failed" and "OTSD_CALENDAR_SNAPSHOT_DIR" in report["detail"], report


def test_an_unknown_boundary_value_is_refused_before_anything_is_stopped(tmp_path):
    status, backups = tmp_path / "backup-status", tmp_path / "backups"
    run, calls = run_backup_with(tmp_path, f"BACKUP_ROOT={backups}\nBACKUP_STATUS_PATH={status}\n",
                                 systemctl_body=OTSD_RUNNING, CALENDAR_BACKUP_BOUNDARY="maybe")
    assert run.returncode == 1, run.stdout + run.stderr
    assert "CALENDAR_BACKUP_BOUNDARY must be" in run.stdout + run.stderr
    assert "systemctl stop" not in calls
