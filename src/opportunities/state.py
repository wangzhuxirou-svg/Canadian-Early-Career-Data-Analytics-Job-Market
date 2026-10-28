"""Persistent local SQLite state for opportunity checks and snapshots."""

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STATE_PATH = ROOT / "data" / "opportunities" / "state.sqlite3"


class RunLockedError(RuntimeError):
    """Raised when another opportunity check owns the local run lock."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


class OpportunityState:
    def __init__(self, path: Path | None = None):
        self.path = path or DEFAULT_STATE_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    status TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    error TEXT
                );
                CREATE TABLE IF NOT EXISTS account_checks (
                    check_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    organizer_id TEXT NOT NULL,
                    attempted_at TEXT NOT NULL,
                    succeeded_at TEXT,
                    status TEXT NOT NULL,
                    coverage_status TEXT NOT NULL,
                    discovered_count INTEGER,
                    collected_count INTEGER,
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS account_checks_latest
                    ON account_checks(organizer_id, attempted_at DESC);
                CREATE TABLE IF NOT EXISTS current_posts (
                    source_post_id TEXT PRIMARY KEY,
                    organizer_id TEXT NOT NULL,
                    content_revision INTEGER NOT NULL,
                    content_hash TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    last_fetched_at TEXT NOT NULL,
                    collection_status TEXT NOT NULL,
                    snapshot_path TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS post_history (
                    history_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    source_post_id TEXT NOT NULL,
                    content_revision INTEGER NOT NULL,
                    content_hash TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    content_changed INTEGER NOT NULL,
                    collection_status TEXT NOT NULL,
                    snapshot_path TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS post_history_by_post
                    ON post_history(source_post_id, fetched_at);
                CREATE TABLE IF NOT EXISTS schedule_slots (
                    slot_id TEXT PRIMARY KEY,
                    scheduled_at TEXT NOT NULL,
                    run_id TEXT REFERENCES runs(run_id),
                    status TEXT NOT NULL,
                    error TEXT
                );
                CREATE TABLE IF NOT EXISTS run_lock (
                    lock_name TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    acquired_at TEXT NOT NULL
                );
                """
            )

    def create_run(self, mode: str = "scheduled", run_id: str | None = None) -> str:
        run_id = run_id or uuid.uuid4().hex
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO runs (run_id, started_at, status, mode) VALUES (?, ?, ?, ?)",
                (run_id, utc_now(), "running", mode),
            )
        return run_id

    def finish_run(self, run_id: str, status: str, error: str | None = None) -> None:
        if status not in {"complete", "partial", "failed", "dry_run"}:
            raise ValueError(f"Unsupported run status: {status}")
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE runs SET ended_at = ?, status = ?, error = ? WHERE run_id = ?",
                (utc_now(), status, error, run_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Unknown run ID: {run_id}")
            connection.execute("DELETE FROM run_lock WHERE run_id = ?", (run_id,))

    @contextmanager
    def run_lock(self, run_id: str):
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "INSERT INTO run_lock (lock_name, run_id, acquired_at) VALUES (?, ?, ?)",
                    ("opportunities", run_id, utc_now()),
                )
                connection.commit()
            except sqlite3.IntegrityError as error:
                connection.rollback()
                existing = self.get_lock()
                raise RunLockedError(
                    f"Opportunity run {existing['run_id']} already holds the run lock."
                    if existing
                    else "Opportunity run lock is already held."
                ) from error
        try:
            yield
        finally:
            with self._connection() as connection:
                connection.execute(
                    "DELETE FROM run_lock WHERE lock_name = ? AND run_id = ?",
                    ("opportunities", run_id),
                )

    def get_lock(self) -> dict | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT lock_name, run_id, acquired_at FROM run_lock WHERE lock_name = ?",
                ("opportunities",),
            ).fetchone()
        return dict(row) if row else None

    def recover_abandoned_lock(self, run_id: str) -> None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            lock = connection.execute(
                "SELECT run_id FROM run_lock WHERE lock_name = ?",
                ("opportunities",),
            ).fetchone()
            if not lock or lock["run_id"] != run_id:
                connection.rollback()
                raise ValueError("The supplied run ID does not own the active lock.")
            connection.execute(
                """UPDATE runs SET ended_at = ?, status = 'failed',
                   error = 'Lock manually recovered after an interrupted process.'
                   WHERE run_id = ? AND status = 'running'""",
                (utc_now(), run_id),
            )
            connection.execute(
                "DELETE FROM run_lock WHERE lock_name = ? AND run_id = ?",
                ("opportunities", run_id),
            )
            connection.commit()

    def record_account_attempt(
        self,
        run_id: str,
        organizer_id: str,
        status: str = "running",
        coverage_status: str = "unknown",
        error: str | None = None,
    ) -> int:
        with self._connection() as connection:
            cursor = connection.execute(
                """INSERT INTO account_checks
                   (run_id, organizer_id, attempted_at, status, coverage_status, error)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (run_id, organizer_id, utc_now(), status, coverage_status, error),
            )
            return int(cursor.lastrowid)

    def finish_account_attempt(
        self,
        check_id: int,
        status: str,
        coverage_status: str,
        discovered_count: int | None = None,
        collected_count: int | None = None,
        error: str | None = None,
    ) -> None:
        allowed_status = {
            "success",
            "partial",
            "failed",
            "blocked",
            "requires_auth",
            "skipped",
        }
        if status not in allowed_status:
            raise ValueError(f"Unsupported account check status: {status}")
        succeeded_at = utc_now() if status == "success" else None
        with self._connection() as connection:
            cursor = connection.execute(
                """UPDATE account_checks
                   SET succeeded_at = ?, status = ?, coverage_status = ?,
                       discovered_count = ?, collected_count = ?, error = ?
                   WHERE check_id = ?""",
                (
                    succeeded_at,
                    status,
                    coverage_status,
                    discovered_count,
                    collected_count,
                    error,
                    check_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Unknown account check ID: {check_id}")

    def record_snapshot(self, run_id: str, raw_post: dict, snapshot_path: Path) -> dict:
        source_post_id = raw_post["source_post_id"]
        organizer_id = raw_post["organizer_id"]
        content_hash = raw_post["content_hash"]
        fetched_at = raw_post["last_fetched_at"]
        collection_status = raw_post["collection_status"]
        snapshot_path_text = str(snapshot_path)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM current_posts WHERE source_post_id = ?",
                (source_post_id,),
            ).fetchone()
            if current is None:
                revision = 1
                first_seen_at = raw_post.get("first_seen_at") or fetched_at
                changed = True
            else:
                changed = current["content_hash"] != content_hash
                revision = current["content_revision"] + (1 if changed else 0)
                first_seen_at = current["first_seen_at"]
            last_seen_at = raw_post.get("last_seen_at") or fetched_at
            if collection_status == "complete":
                connection.execute(
                    """INSERT INTO current_posts
                       (source_post_id, organizer_id, content_revision, content_hash,
                        first_seen_at, last_seen_at, last_fetched_at, collection_status,
                        snapshot_path)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(source_post_id) DO UPDATE SET
                        organizer_id = excluded.organizer_id,
                        content_revision = excluded.content_revision,
                        content_hash = excluded.content_hash,
                        first_seen_at = excluded.first_seen_at,
                        last_seen_at = excluded.last_seen_at,
                        last_fetched_at = excluded.last_fetched_at,
                        collection_status = excluded.collection_status,
                        snapshot_path = excluded.snapshot_path""",
                    (
                        source_post_id,
                        organizer_id,
                        revision,
                        content_hash,
                        first_seen_at,
                        last_seen_at,
                        fetched_at,
                        collection_status,
                        snapshot_path_text,
                    ),
                )
            connection.execute(
                """INSERT INTO post_history
                   (run_id, source_post_id, content_revision, content_hash, fetched_at,
                    content_changed, collection_status, snapshot_path)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    source_post_id,
                    revision,
                    content_hash,
                    fetched_at,
                    int(changed),
                    collection_status,
                    snapshot_path_text,
                ),
            )
            connection.commit()
        return {
            "source_post_id": source_post_id,
            "content_revision": revision,
            "content_changed": changed,
            "first_seen_at": first_seen_at,
            "became_current": collection_status == "complete",
        }

    def report(self) -> dict:
        with self._connection() as connection:
            runs = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM runs ORDER BY started_at DESC LIMIT 20"
                )
            ]
            accounts = [
                dict(row)
                for row in connection.execute(
                    """SELECT c.* FROM account_checks c
                       JOIN (
                           SELECT organizer_id, MAX(check_id) AS check_id
                           FROM account_checks GROUP BY organizer_id
                       ) latest ON latest.check_id = c.check_id
                       ORDER BY c.organizer_id"""
                )
            ]
            successful_checks = [dict(row) for row in connection.execute(
                """SELECT organizer_id, succeeded_at FROM account_checks
                   WHERE status = 'success' AND coverage_status = 'complete'
                   AND succeeded_at IS NOT NULL ORDER BY check_id DESC"""
            )]
            posts = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM current_posts ORDER BY organizer_id, source_post_id"
                )
            ]
            lock = connection.execute(
                "SELECT * FROM run_lock WHERE lock_name = ?", ("opportunities",)
            ).fetchone()
        return {
            "generated_at": utc_now(),
            "runs": runs,
            "accounts": accounts,
            "successful_checks": successful_checks,
            "posts": posts,
            "lock": dict(lock) if lock else None,
        }

    def claim_schedule_slot(self, slot_id: str, scheduled_at: str) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                """INSERT OR IGNORE INTO schedule_slots
                   (slot_id, scheduled_at, status) VALUES (?, ?, 'pending')""",
                (slot_id, scheduled_at),
            )
            return cursor.rowcount == 1

    def finish_schedule_slot(
        self,
        slot_id: str,
        status: str,
        run_id: str | None = None,
        error: str | None = None,
    ) -> None:
        with self._connection() as connection:
            cursor = connection.execute(
                """UPDATE schedule_slots SET status = ?, run_id = ?, error = ?
                   WHERE slot_id = ?""",
                (status, run_id, error, slot_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Unknown schedule slot: {slot_id}")

    def failed_account_count(self) -> int:
        with self._connection() as connection:
            row = connection.execute(
                """SELECT COUNT(*) AS count FROM account_checks
                   WHERE status IN ('failed', 'blocked', 'requires_auth')"""
            ).fetchone()
        return int(row["count"])
