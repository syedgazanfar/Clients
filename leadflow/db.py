"""SQLite storage for leads and messages."""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phone TEXT NOT NULL UNIQUE,
    name TEXT,
    email TEXT,
    company TEXT,
    service TEXT,
    challenge TEXT,
    timeline TEXT,
    source TEXT NOT NULL DEFAULT 'website',
    consent INTEGER NOT NULL DEFAULT 0,
    opted_out INTEGER NOT NULL DEFAULT 0,
    stage TEXT NOT NULL DEFAULT 'new',
    status TEXT NOT NULL DEFAULT 'active',
    score INTEGER NOT NULL DEFAULT 0,
    followups_sent INTEGER NOT NULL DEFAULT 0,
    owner_notified INTEGER NOT NULL DEFAULT 0,
    last_inbound_at TEXT,
    last_outbound_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER NOT NULL REFERENCES leads(id),
    direction TEXT NOT NULL,
    body TEXT NOT NULL,
    provider_id TEXT,
    status TEXT NOT NULL DEFAULT 'ok',
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_provider_id
    ON messages(provider_id) WHERE provider_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_messages_lead ON messages(lead_id);
"""

LEAD_FIELDS = {
    "name", "email", "company", "service", "challenge", "timeline", "source",
    "consent", "opted_out", "stage", "status", "score", "followups_sent",
    "owner_notified", "last_inbound_at", "last_outbound_at",
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime | None = None) -> str:
    return (moment or utcnow()).astimezone(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def _one(self, sql: str, args: tuple = ()) -> dict | None:
        with self._lock:
            row = self._conn.execute(sql, args).fetchone()
        return dict(row) if row else None

    def _all(self, sql: str, args: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, args).fetchall()]

    # Leads -----------------------------------------------------------------

    def get_lead(self, lead_id: int) -> dict | None:
        return self._one("SELECT * FROM leads WHERE id = ?", (lead_id,))

    def get_lead_by_phone(self, phone: str) -> dict | None:
        return self._one("SELECT * FROM leads WHERE phone = ?", (phone,))

    def create_lead(self, phone: str, **fields) -> dict:
        unknown = set(fields) - LEAD_FIELDS
        if unknown:
            raise ValueError(f"unknown lead fields: {unknown}")
        now = iso()
        columns = ["phone", "created_at", "updated_at", *fields]
        values = [phone, now, now, *fields.values()]
        sql = "INSERT INTO leads ({}) VALUES ({})".format(
            ", ".join(columns), ", ".join("?" for _ in columns)
        )
        with self._lock:
            cursor = self._conn.execute(sql, values)
            self._conn.commit()
            lead_id = cursor.lastrowid
        return self.get_lead(lead_id)

    def update_lead(self, lead_id: int, **fields) -> dict:
        unknown = set(fields) - LEAD_FIELDS
        if unknown:
            raise ValueError(f"unknown lead fields: {unknown}")
        fields["updated_at"] = iso()
        sql = "UPDATE leads SET {} WHERE id = ?".format(
            ", ".join(f"{name} = ?" for name in fields)
        )
        with self._lock:
            self._conn.execute(sql, (*fields.values(), lead_id))
            self._conn.commit()
        return self.get_lead(lead_id)

    def list_leads(self) -> list[dict]:
        return self._all("SELECT * FROM leads ORDER BY score DESC, created_at DESC")

    def followup_candidates(self) -> list[dict]:
        return self._all(
            """SELECT * FROM leads
               WHERE consent = 1 AND opted_out = 0 AND status = 'active'
                 AND stage != 'done' AND last_outbound_at IS NOT NULL
                 AND (last_inbound_at IS NULL OR last_inbound_at < last_outbound_at)
               ORDER BY last_outbound_at"""
        )

    # Messages --------------------------------------------------------------

    def add_message(
        self,
        lead_id: int,
        direction: str,
        body: str,
        provider_id: str | None = None,
        status: str = "ok",
    ) -> bool:
        """Store a message. Returns False if provider_id was already seen."""
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO messages (lead_id, direction, body, provider_id, status,"
                    " created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (lead_id, direction, body, provider_id, status, iso()),
                )
                self._conn.commit()
        except sqlite3.IntegrityError:
            return False
        return True

    def message_seen(self, provider_id: str) -> bool:
        return self._one(
            "SELECT 1 AS x FROM messages WHERE provider_id = ?", (provider_id,)
        ) is not None

    def list_messages(self, lead_id: int) -> list[dict]:
        return self._all(
            "SELECT * FROM messages WHERE lead_id = ? ORDER BY id", (lead_id,)
        )
