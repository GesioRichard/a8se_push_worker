"""Estado em SQLite (SPEC §8).

Datas são gravadas como ISO 8601 UTC no formato `YYYY-MM-DDTHH:MM:SSZ`, que
ordena corretamente como texto.

Além das tabelas da SPEC há `meta` (chave/valor) para contadores operacionais,
como falhas seguidas de leitura do RSS.

Envios em `dry-run` também são registrados em `sends` (mode = 'dry-run'):
sem isso a política não teria como simular o ritmo do dia. Toda consulta de
envios filtra pelo modo atual, então trocar de modo recomeça as contagens.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .policy import PENDING, Pending, SendRecord

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
  guid           TEXT PRIMARY KEY,
  url            TEXT NOT NULL,
  title          TEXT NOT NULL,
  title_key      TEXT NOT NULL,
  slug           TEXT NOT NULL,
  published_at   TEXT,
  first_seen_at  TEXT NOT NULL,
  status         TEXT NOT NULL,
  reason         TEXT
);
CREATE TABLE IF NOT EXISTS sends (
  id             INTEGER PRIMARY KEY,
  guid           TEXT NOT NULL,
  slug           TEXT NOT NULL,
  urgent         INTEGER NOT NULL,
  sent_at        TEXT NOT NULL,
  fcm_message_id TEXT,
  mode           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_items_status ON items(status);
CREATE INDEX IF NOT EXISTS idx_sends_sent_at ON sends(sent_at);
CREATE TABLE IF NOT EXISTS meta (
  key            TEXT PRIMARY KEY,
  value          TEXT NOT NULL
);
"""


def to_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class ItemRow:
    guid: str
    url: str
    title: str
    title_key: str
    slug: str
    published_at: datetime | None
    first_seen_at: datetime
    status: str
    reason: str | None


class Store:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # --- items ---------------------------------------------------------------

    def is_empty(self) -> bool:
        return self.conn.execute("SELECT 1 FROM items LIMIT 1").fetchone() is None

    def known_guids(self, guids: list[str]) -> set[str]:
        known: set[str] = set()
        for i in range(0, len(guids), 500):
            chunk = guids[i : i + 500]
            marks = ",".join("?" * len(chunk))
            rows = self.conn.execute(f"SELECT guid FROM items WHERE guid IN ({marks})", chunk)
            known.update(r["guid"] for r in rows)
        return known

    def insert_item(
        self,
        *,
        guid: str,
        url: str,
        title: str,
        title_key: str,
        slug: str,
        published_at: datetime | None,
        first_seen_at: datetime,
        status: str,
        reason: str | None = None,
    ) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO items (guid, url, title, title_key, slug, published_at,"
                " first_seen_at, status, reason) VALUES (?,?,?,?,?,?,?,?,?)",
                (guid, url, title, title_key, slug, to_iso(published_at),
                 to_iso(first_seen_at), status, reason),
            )

    def get_item(self, guid: str) -> ItemRow | None:
        row = self.conn.execute("SELECT * FROM items WHERE guid = ?", (guid,)).fetchone()
        return self._item(row) if row else None

    def set_status(self, guid: str, status: str, reason: str | None = None) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE items SET status = ?, reason = ? WHERE guid = ?", (status, reason, guid)
            )

    def pending(self) -> list[Pending]:
        rows = self.conn.execute(
            "SELECT guid, slug, title_key, published_at FROM items WHERE status = ?", (PENDING,)
        )
        return [
            Pending(r["guid"], r["slug"], r["title_key"], from_iso(r["published_at"]))
            for r in rows
        ]

    def purge_items(self, before: datetime) -> int:
        with self.conn:
            cur = self.conn.execute("DELETE FROM items WHERE first_seen_at < ?", (to_iso(before),))
        return cur.rowcount

    def status_counts(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT status, COUNT(*) AS n FROM items GROUP BY status")
        return {r["status"]: r["n"] for r in rows}

    # --- sends ---------------------------------------------------------------

    def record_send(
        self,
        *,
        guid: str,
        slug: str,
        urgent: bool,
        sent_at: datetime,
        fcm_message_id: str | None,
        mode: str,
    ) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO sends (guid, slug, urgent, sent_at, fcm_message_id, mode)"
                " VALUES (?,?,?,?,?,?)",
                (guid, slug, int(urgent), to_iso(sent_at), fcm_message_id, mode),
            )

    def sends_since(self, since: datetime, mode: str) -> list[SendRecord]:
        rows = self.conn.execute(
            "SELECT guid, slug, urgent, sent_at FROM sends WHERE sent_at >= ? AND mode = ?"
            " ORDER BY sent_at",
            (to_iso(since), mode),
        )
        return [SendRecord(r["guid"], r["slug"], bool(r["urgent"]), from_iso(r["sent_at"])) for r in rows]

    def sent_title_keys_since(self, since: datetime, mode: str) -> set[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT i.title_key FROM sends s JOIN items i ON i.guid = s.guid"
            " WHERE s.sent_at >= ? AND s.mode = ?",
            (to_iso(since), mode),
        )
        return {r["title_key"] for r in rows}

    def send_log(self, since: datetime, mode: str) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT s.*, i.title FROM sends s LEFT JOIN items i ON i.guid = s.guid"
                " WHERE s.sent_at >= ? AND s.mode = ? ORDER BY s.sent_at",
                (to_iso(since), mode),
            )
        )

    def last_send(self, mode: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT s.*, i.title FROM sends s LEFT JOIN items i ON i.guid = s.guid"
            " WHERE s.mode = ? ORDER BY s.sent_at DESC, s.id DESC LIMIT 1",
            (mode,),
        ).fetchone()

    # --- meta ----------------------------------------------------------------

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    @staticmethod
    def _item(row: sqlite3.Row) -> ItemRow:
        return ItemRow(
            guid=row["guid"],
            url=row["url"],
            title=row["title"],
            title_key=row["title_key"],
            slug=row["slug"],
            published_at=from_iso(row["published_at"]),
            first_seen_at=from_iso(row["first_seen_at"]),
            status=row["status"],
            reason=row["reason"],
        )
