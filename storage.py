"""Persistent (SQLite) storage for poll metadata and the last known vote per user.

Two tables:

* ``polls``  – one row per Telegram poll the bot has sent. This is the
  ``poll_id -> metadata`` map used to route ``poll_answer`` updates to the
  right sheet/row/column, and it survives a bot restart.
* ``votes``  – the last value received for a given (poll, user). A vote is
  stored here *before* it is written to the Google Sheet (``synced = 0``)
  and marked ``synced = 1`` once the sheet has it, so a vote whose sheet
  write failed is retried instead of lost. It also lets us tell "new vote"
  from "changed vote" in the logs.
"""

import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("qpoll.storage")

DB_PATH = Path(__file__).parent / "polls.db"

# sqlite3 connections are not safe to share across threads; the bot touches
# this from the asyncio loop thread and from scheduler jobs, so we serialise.
_lock = threading.Lock()


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def init_db():
    with _lock, _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS polls (
                poll_id            TEXT PRIMARY KEY,
                date               TEXT NOT NULL,   -- DD-MMM-YYYY, the sheet row key
                poll_type          TEXT NOT NULL,   -- 'own_service' | 'partner'
                chat_id            INTEGER NOT NULL,
                message_id         INTEGER NOT NULL,
                group_label        TEXT,
                project_name       TEXT,            -- set for poll_type='own_service'
                partner_key        TEXT,            -- set for poll_type='partner'
                partner_sheet_name TEXT,            -- set for poll_type='partner'
                question           TEXT,
                closed             INTEGER NOT NULL DEFAULT 0,
                created_at         TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS votes (
                poll_id    TEXT NOT NULL,
                user_id    INTEGER NOT NULL,
                value      INTEGER,                 -- NULL => vote retracted
                updated_at TEXT NOT NULL,
                PRIMARY KEY (poll_id, user_id)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_polls_date ON polls(date)")
        _add_column(conn, "votes", "synced", "INTEGER NOT NULL DEFAULT 1")
        _add_column(conn, "polls", "autofilled", "INTEGER NOT NULL DEFAULT 0")
    logger.info("SQLite storage ready at %s", DB_PATH)


def _add_column(conn, table, column, decl):
    """Add a column to a table created by an older version of this file."""
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(%s)" % table)}
    if column not in columns:
        conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, decl))


def save_poll(meta):
    """Persist metadata for a poll the bot just sent.

    ``meta`` keys: poll_id, date, poll_type, chat_id, message_id, group_label,
    project_name, partner_key, partner_sheet_name, question.
    """
    with _lock, _connect() as conn:
        conn.execute(
            """
            INSERT INTO polls (
                poll_id, date, poll_type, chat_id, message_id, group_label,
                project_name, partner_key, partner_sheet_name, question,
                closed, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
            ON CONFLICT(poll_id) DO UPDATE SET
                date               = excluded.date,
                poll_type          = excluded.poll_type,
                chat_id            = excluded.chat_id,
                message_id         = excluded.message_id,
                group_label        = excluded.group_label,
                project_name       = excluded.project_name,
                partner_key        = excluded.partner_key,
                partner_sheet_name = excluded.partner_sheet_name,
                question           = excluded.question,
                created_at         = excluded.created_at
            """,
            (
                meta["poll_id"],
                meta["date"],
                meta["poll_type"],
                meta["chat_id"],
                meta["message_id"],
                meta.get("group_label"),
                meta.get("project_name"),
                meta.get("partner_key"),
                meta.get("partner_sheet_name"),
                meta.get("question"),
                _now_iso(),
            ),
        )


def import_poll_log(rows):
    """Bulk-load poll metadata recovered from the sheet-backed poll log
    (SheetsClient.load_poll_log) into local storage. Used once at startup so
    that polls sent by a previous process instance -- one whose local
    storage.db was wiped by a redeploy on a host with no persistent disk,
    e.g. Render's free tier -- can still have their votes routed correctly.
    Existing local rows are left untouched (``INSERT OR IGNORE``).
    """
    if not rows:
        return
    with _lock, _connect() as conn:
        for r in rows:
            poll_id = str(r.get("poll_id") or "").strip()
            if not poll_id:
                continue
            conn.execute(
                """
                INSERT OR IGNORE INTO polls (
                    poll_id, date, poll_type, chat_id, message_id, group_label,
                    project_name, partner_key, partner_sheet_name, question,
                    closed, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    poll_id,
                    r.get("date"),
                    r.get("poll_type"),
                    int(r.get("chat_id") or 0),
                    int(r.get("message_id") or 0),
                    r.get("group_label") or None,
                    r.get("project_name") or None,
                    r.get("partner_key") or None,
                    r.get("partner_sheet_name") or None,
                    r.get("question") or None,
                    int(r.get("closed") or 0),
                    _now_iso(),
                ),
            )
    logger.info("Imported %d poll(s) from the sheet-backed poll log", len(rows))


def get_poll(poll_id):
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT * FROM polls WHERE poll_id = ?", (poll_id,)
        ).fetchone()
    return dict(row) if row else None


def polls_for_date(date_str):
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM polls WHERE date = ?", (date_str,)
        ).fetchall()
    return [dict(r) for r in rows]


def open_polls():
    with _lock, _connect() as conn:
        rows = conn.execute("SELECT * FROM polls WHERE closed = 0").fetchall()
    return [dict(r) for r in rows]


def mark_closed(poll_id):
    with _lock, _connect() as conn:
        conn.execute("UPDATE polls SET closed = 1 WHERE poll_id = ?", (poll_id,))


def get_previous_vote(poll_id, user_id):
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT value FROM votes WHERE poll_id = ? AND user_id = ?",
            (poll_id, user_id),
        ).fetchone()
    return row["value"] if row else None


def record_vote(poll_id, user_id, value, synced=True):
    """Store the latest vote. ``synced=False`` means it still has to be
    written to the sheet (see pending_votes / mark_vote_synced)."""
    with _lock, _connect() as conn:
        conn.execute(
            """
            INSERT INTO votes (poll_id, user_id, value, updated_at, synced)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(poll_id, user_id)
            DO UPDATE SET value = excluded.value,
                          updated_at = excluded.updated_at,
                          synced = excluded.synced
            """,
            (poll_id, user_id, value, _now_iso(), int(synced)),
        )


def has_vote(poll_id, user_id):
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM votes WHERE poll_id = ? AND user_id = ?",
            (poll_id, user_id),
        ).fetchone()
    return row is not None


def mark_vote_synced(poll_id, user_id, value):
    """Mark a vote as written to the sheet -- only if it is still ``value``,
    so a newer vote that arrived meanwhile stays pending."""
    with _lock, _connect() as conn:
        conn.execute(
            "UPDATE votes SET synced = 1 "
            "WHERE poll_id = ? AND user_id = ? AND value IS ?",
            (poll_id, user_id, value),
        )


def pending_votes():
    """Votes not yet written to the sheet, each with its poll's metadata."""
    with _lock, _connect() as conn:
        rows = conn.execute(
            """
            SELECT v.user_id, v.value, p.*
            FROM votes v JOIN polls p ON p.poll_id = v.poll_id
            WHERE v.synced = 0
            ORDER BY v.updated_at
            """
        ).fetchall()
    return [dict(r) for r in rows]


def get_pending_vote(poll_id, user_id):
    """(value,) if this vote is still waiting for its sheet write, else None."""
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT value FROM votes "
            "WHERE poll_id = ? AND user_id = ? AND synced = 0",
            (poll_id, user_id),
        ).fetchone()
    return (row["value"],) if row else None


def polls_to_autofill():
    """Own-service polls whose missed votes have not been autofilled yet."""
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM polls "
            "WHERE poll_type = 'own_service' AND autofilled = 0"
        ).fetchall()
    return [dict(r) for r in rows]


def mark_autofilled(poll_id):
    with _lock, _connect() as conn:
        conn.execute("UPDATE polls SET autofilled = 1 WHERE poll_id = ?",
                     (poll_id,))
