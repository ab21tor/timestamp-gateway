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
