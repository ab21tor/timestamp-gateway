#!/usr/bin/env bash
# verify-obligations-snapshot.sh <obligations.db copy> [--expect-payment-hash HASH]
#
# Says whether a copy of the gateway's obligation log is usable: the file
# opens, PRAGMA integrity_check answers ok, the obligations table exists,
# and — when a hash is given — the named obligation row is present (the
# known-record check: the backup writes the newest row's payment hash into
# its metadata before snapshotting, and the restore checklist asks for it
# back). Exit 0 and "usable: ..." only when every check holds; otherwise
# exit 1 and "unusable: <reason>". Read-only: the copy is opened with
# query_only set and nothing is written. Used by ops/backup-live-state.sh
# on every snapshot it takes, and by hand after a restore
# (ops/BACKUP-RECOVERY.md, "Post-restore checks").
set -u

SNAPSHOT="${1:-}"
EXPECT=""
if [ "${2:-}" = "--expect-payment-hash" ]; then
  EXPECT="${3:-}"
fi
if [ -z "$SNAPSHOT" ]; then
  echo "usage: $0 <obligations.db copy> [--expect-payment-hash HASH]" >&2
  exit 2
fi
if [ ! -f "$SNAPSHOT" ]; then
  echo "unusable: file not found: $SNAPSHOT"
  exit 1
fi

python3 - "$SNAPSHOT" "$EXPECT" <<'PY'
import sqlite3
import sys

path, expect = sys.argv[1], sys.argv[2]


def unusable(reason):
    print("unusable: " + reason)
    sys.exit(1)


try:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA query_only = 1")
    verdict = conn.execute("PRAGMA integrity_check").fetchone()[0]
except sqlite3.Error as exc:
    unusable("cannot open as SQLite (%s)" % type(exc).__name__)
if verdict != "ok":
    unusable("integrity_check: " + str(verdict)[:200])
tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
if "obligations" not in tables:
    unusable("obligations table missing (tables: %s)" % ", ".join(sorted(tables)) if tables else "obligations table missing (no tables)")
count = conn.execute("SELECT COUNT(*) FROM obligations").fetchone()[0]
if expect:
    found = conn.execute("SELECT 1 FROM obligations WHERE payment_hash = ?", (expect,)).fetchone()
    if found is None:
        unusable("known obligation row %s... is missing (the copy predates it or is torn)" % expect[:8])
print("usable: integrity ok, obligations=%d%s" % (count, ", known row present" if expect else ""))
PY
