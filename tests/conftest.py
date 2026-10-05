from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

import pytest

from a8se_push import config
from a8se_push.fcm import DryRunSender
from a8se_push.log import MemoryLog
from a8se_push.runner import Deps
from a8se_push.store import Store

FIXTURES = Path(__file__).parent / "fixtures"
REAL_FEED = FIXTURES / "rss_2026-09-30.xml"  # baixado de https://a8se.com/rss.xml em 30/09/2026 15:08
TZ = ZoneInfo("America/Maceio")

PRIORITY = {
    "sergipe": 3, "policia": 2, "politica": 2, "eleicoes": 2,
    "saude": 2, "economia": 2, "brasil": 1, "esportes": 1,
}


def at(local: str) -> datetime:
    """'2026-09-30 10:00' em America/Maceio → datetime UTC."""
    return datetime.fromisoformat(local).replace(tzinfo=TZ).astimezone(timezone.utc)


def make_cfg(mode: str = "dry-run", policy: dict | None = None, **top) -> config.Config:
    raw = {
        "mode": mode,
        "state_db": ":memory:",
        "policy": policy or {},
        "categories": {"enabled": [], "priority": PRIORITY},
    }
    enabled = top.pop("enabled", None)
    if enabled is not None:
        raw["categories"]["enabled"] = enabled
    raw.update(top)
    return config.from_dict(raw)


def news_item(
    n: int,
    slug: str,
    published: datetime,
    *,
    title: str | None = None,
    body: bool = True,
    image: str | None = "https://img.dhost.cloud/x/1200x630/smart/https://cdn.a8se.com/images/a/{n}.jpeg",
    description: str = "Linha fina da matéria",
    category: str | None = None,
    path: str = "noticias",
) -> dict:
    link = f"https://a8se.com/{path}/{slug}/2026/09/30/{245000 + n}-materia-{n}"
    return {
        "title": title or f"Matéria número {n} de {slug}",
        "link": link,
        "pubDate": published,
        "category": category or slug.capitalize(),
        "description": description,
        "body": body,
        "image": image.format(n=n) if image else None,
    }


def rss(items: list[dict]) -> bytes:
    """Monta um RSS no mesmo formato do a8se.com (namespaces, CDATA, media:content)."""
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/"'
        ' xmlns:media="http://search.yahoo.com/mrss/"><channel><title>a8se.com</title>',
    ]
    for it in items:
        parts.append("<item>")
        parts.append(f"<title>{escape(it['title'])}</title>")
        parts.append(f"<link>{escape(it['link'])}</link>")
        parts.append(f"<pubDate>{format_datetime(it['pubDate'].astimezone(TZ))}</pubDate>")
        parts.append(f"<category>{escape(it['category'])}</category>")
        parts.append(f"<guid>{escape(it['link'])}</guid>")
        if it.get("body"):
            parts.append(f"<description><![CDATA[{it['description']}]]></description>")
            parts.append("<content:encoded><![CDATA[<p>Texto completo.</p>]]></content:encoded>")
        if it.get("image"):
            parts.append(f'<media:content url="{escape(it["image"])}" medium="image" width="1200" height="630"/>')
        parts.append("</item>")
    parts.append("</channel></rss>")
    return "".join(parts).encode("utf-8")


class FakeWorld:
    """Dependências falsas para o runner: feed mutável, pings e log em memória."""

    def __init__(self, cfg: config.Config, sender=None, aliases=None):
        self.cfg = cfg
        self.store = Store(":memory:")
        self.feed_items: list[dict] = []
        self.feed_bytes: bytes | None = None
        self.feed_error: Exception | None = None
        self.pings: list[str] = []
        self.log = MemoryLog()
        self.sender = sender or DryRunSender()
        self.aliases = aliases if aliases is not None else {"esporte": "esportes"}

    def _fetch(self) -> bytes:
        if self.feed_error:
            raise self.feed_error
        return self.feed_bytes if self.feed_bytes is not None else rss(self.feed_items)

    @property
    def deps(self) -> Deps:
        return Deps(
            fetch_feed=self._fetch,
            load_aliases=lambda now: dict(self.aliases),
            sender=self.sender,
            ping=self.pings.append,
            log=self.log,
        )


@pytest.fixture
def real_feed() -> bytes:
    return REAL_FEED.read_bytes()


@pytest.fixture
def cfg() -> config.Config:
    return make_cfg()


def minutes(n: float) -> timedelta:
    return timedelta(minutes=n)
