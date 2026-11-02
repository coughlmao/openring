"""Tiny SQLite document store for assistants, phone numbers, calls, tools and campaigns.

Each record is stored as a JSON document so the API can stay schema-compatible
with Vapi without a migration for every new field.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

COLLECTIONS = ("assistants", "phone_numbers", "calls", "tools", "campaigns", "routes", "files", "knowledge_bases",
               "squads", "credentials")
PROTECTED = {"id", "createdAt"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _merge(current: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    doc = {**current, **{k: v for k, v in patch.items() if k not in PROTECTED}}
    doc["updatedAt"] = now_iso()
    return doc


class Store:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            for name in COLLECTIONS:
                self._db.execute(
                    f"CREATE TABLE IF NOT EXISTS {name} ("
                    "id TEXT PRIMARY KEY, created_at TEXT NOT NULL, doc TEXT NOT NULL)"
                )
            # Server-wide values (the dashboard password hash) and dashboard login sessions.
            self._db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, expires_at REAL NOT NULL)"
            )
            # The passages of knowledge base files (knowledge.py). Python builds without FTS5 are rare; uploads say so.
            try:
                self._db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS file_chunks USING fts5("
                                 "file_id UNINDEXED, content, tokenize='unicode61 remove_diacritics 2')")
                self.can_search = True
            except sqlite3.OperationalError:
                self.can_search = False
            self._db.commit()

    def index_file(self, file_id: str, passages: list[str]) -> None:
        with self._lock:
            self._db.executemany("INSERT INTO file_chunks (file_id, content) VALUES (?, ?)",
                                 [(file_id, passage) for passage in passages])
            self._db.commit()

    def search_chunks(self, file_ids: list[str], match: str, limit: int) -> list[str]:
        """The passages of these files that best match a full-text query, best first."""
        marks = ",".join("?" * len(file_ids))
        with self._lock:
            rows = self._db.execute(
                f"SELECT content FROM file_chunks WHERE file_chunks MATCH ? AND file_id IN ({marks}) "
                "ORDER BY bm25(file_chunks) LIMIT ?", (match, *file_ids, limit),
            ).fetchall()
        return [r[0] for r in rows]

    def first_chunks(self, file_ids: list[str], limit: int) -> list[str]:
        marks = ",".join("?" * len(file_ids))
        with self._lock:
            rows = self._db.execute(f"SELECT content FROM file_chunks WHERE file_id IN ({marks}) LIMIT ?",
                                    (*file_ids, limit)).fetchall()
        return [r[0] for r in rows]

    def forget_file(self, file_id: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM file_chunks WHERE file_id = ?", (file_id,))
            self._db.commit()

    def get_setting(self, key: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_setting(self, key: str, value: str | None) -> None:
        with self._lock:
            if value is None:
                self._db.execute("DELETE FROM settings WHERE key = ?", (key,))
            else:
                self._db.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )
            self._db.commit()

    def add_session(self, token_hash: str, expires_at: float) -> None:
        with self._lock:
            self._db.execute("INSERT INTO sessions (token_hash, expires_at) VALUES (?, ?)", (token_hash, expires_at))
            self._db.commit()

    def session_expiry(self, token_hash: str) -> float | None:
        with self._lock:
            row = self._db.execute("SELECT expires_at FROM sessions WHERE token_hash = ?", (token_hash,)).fetchone()
        return row[0] if row else None

    def delete_sessions(self, token_hash: str | None = None) -> None:
        """Ends one session, or every session when no token is given."""
        with self._lock:
            if token_hash is None:
                self._db.execute("DELETE FROM sessions")
            else:
                self._db.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
            self._db.commit()

    def _check(self, collection: str) -> None:
        if collection not in COLLECTIONS:
            raise ValueError(f"unknown collection {collection}")

    def create(self, collection: str, data: dict[str, Any]) -> dict[str, Any]:
        self._check(collection)
        ts = now_iso()
        doc = {**data, "id": str(uuid.uuid4()), "createdAt": ts, "updatedAt": ts}
        with self._lock:
            self._db.execute(
                f"INSERT INTO {collection} (id, created_at, doc) VALUES (?, ?, ?)",
                (doc["id"], ts, json.dumps(doc)),
            )
            self._db.commit()
        return doc

    def get(self, collection: str, item_id: str) -> dict[str, Any] | None:
        self._check(collection)
        with self._lock:
            row = self._db.execute(
                f"SELECT doc FROM {collection} WHERE id = ?", (item_id,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def list(
             self,
             collection: str,
             limit: int = 100,
             created_at_gt:str | None = None,
             created_at_lt:str | None = None,
             created_at_ge:str | None = None,
             created_at_le:str | None = None,
             ) -> list[dict[str, Any]]:
        self._check(collection)

        conditions = []
        params:list[Any] = []

        if created_at_gt is not None:
            conditions.append("created_at > ?")
            params.append(created_at_gt)

        if created_at_lt is not None:
            conditions.append("created_at < ?")
            params.append(created_at_lt)

        if created_at_ge is not None:
            conditions.append("created_at >= ?")
            params.append(created_at_ge)

        if created_at_le is not None:
            conditions.append("created_at <= ?")
            params.append(created_at_le)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        with self._lock:
            rows = self._db.execute(
                f"SELECT doc FROM {collection} {where} ORDER BY created_at DESC, rowid DESC LIMIT ?", (*params,limit,)
            ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def update(self, collection: str, item_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
        """Merge `patch` into the document. Reading and writing under one lock keeps two writers (a campaign's
        dialer and someone cancelling it) from undoing each other's change."""
        self._check(collection)
        with self._lock:
            row = self._db.execute(f"SELECT doc FROM {collection} WHERE id = ?", (item_id,)).fetchone()
            if row is None:
                return None
            doc = _merge(json.loads(row[0]), patch)
            self._db.execute(
                f"UPDATE {collection} SET doc = ? WHERE id = ?", (json.dumps(doc), item_id)
            )
            self._db.commit()
        return doc

    def claim(
        self, collection: str, item_id: str, from_status: str, patch: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Apply `patch` only while the document's status is `from_status`.

        The check and the write happen under one lock, so when several callers
        race for the same document exactly one gets it back; the rest get None.
        """
        self._check(collection)
        with self._lock:
            row = self._db.execute(
                f"SELECT doc FROM {collection} WHERE id = ?", (item_id,)
            ).fetchone()
            current = json.loads(row[0]) if row else None
            if current is None or current.get("status") != from_status:
                return None
            doc = _merge(current, patch)
            self._db.execute(
                f"UPDATE {collection} SET doc = ? WHERE id = ?", (json.dumps(doc), item_id)
            )
            self._db.commit()
        return doc

    def delete(self, collection: str, item_id: str) -> dict[str, Any] | None:
        doc = self.get(collection, item_id)
        if doc is None:
            return None
        with self._lock:
            self._db.execute(f"DELETE FROM {collection} WHERE id = ?", (item_id,))
            self._db.commit()
        return doc

    def find_phone_number(self, number: str) -> dict[str, Any] | None:
        wanted = _normalize_number(number)
        if not wanted:  # no digits (an anonymous caller, a typo): never matches a number
            return None
        for doc in self.list("phone_numbers", limit=10_000):
            if _normalize_number(doc.get("number", "")) == wanted:
                return doc
        return None


def is_phone_number(number: Any) -> bool:
    """A phone number with its country code: 7 to 15 digits, written with spaces, dashes, dots or brackets."""
    text = str(number or "").strip()
    digits = _normalize_number(text)
    return bool(re.fullmatch(r"\+?[\d\s().-]+", text)) and 7 <= len(digits) <= 15


def _normalize_number(number: str) -> str:
    return "".join(ch for ch in number if ch.isdigit())
