"""Download e parse do RSS do a8se.com (SPEC §3, §4.1, §6).

Tudo aqui, exceto `fetch`, é puro: recebe bytes/strings e devolve dados.
"""

from __future__ import annotations

import html
import json
import re
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import httpx

NS = {
    "media": "http://search.yahoo.com/mrss/",
    "content": "http://purl.org/rss/1.0/modules/content/",
}

KIND_NEWS = "news"  # /noticias/<slug>/...
KIND_TV = "tv"  # /tv-atalaia/<programa>/...
KIND_OTHER = "other"

# Padrão do app quando o parâmetro news_categories não está publicado (SPEC §4.1).
DEFAULT_ALIASES: dict[str, str] = {"esporte": "esportes"}

_TOPIC_UNSAFE = re.compile(r"[^a-zA-Z0-9\-_.~%]")
_TAG = re.compile(r"<[^>]+>")
_SPACES = re.compile(r"\s+")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


class FeedError(Exception):
    pass


@dataclass(frozen=True)
class FeedItem:
    guid: str
    url: str
    title: str
    description: str
    category_label: str
    kind: str
    raw_slug: str  # slug antes de aplicar apelidos
    has_body: bool
    image: str | None  # nunca AVIF
    published_at: datetime | None  # UTC, com fuso


# --- texto -----------------------------------------------------------------


def unescape_all(text: str) -> str:
    """Aplica html.unescape até o texto parar de mudar (o feed codifica entidades em dobro)."""
    for _ in range(10):
        new = html.unescape(text)
        if new == text:
            break
        text = new
    return text


def clean_text(text: str | None) -> str:
    if not text:
        return ""
    text = unescape_all(text)
    text = _TAG.sub(" ", text)
    text = unescape_all(text)
    return _SPACES.sub(" ", text).strip()


def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def slugify(text: str) -> str:
    """Mesma regra do app: sem acentos, minúsculo, '-' no lugar de espaços e pontuação."""
    return _NON_ALNUM.sub("-", strip_accents(text).lower()).strip("-")


def topic_safe(name: str) -> str:
    """Nome de tópico FCM só aceita [a-zA-Z0-9-_.~%]."""
    return _TOPIC_UNSAFE.sub("-", name)


def normalize_title(title: str) -> str:
    """Chave de deduplicação: minúsculo, sem acento e sem pontuação."""
    return " ".join(_NON_ALNUM.sub(" ", strip_accents(clean_text(title)).lower()).split())


# --- URL / editoria --------------------------------------------------------


def is_a8se_https(url: str) -> bool:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and (host == "a8se.com" or host.endswith(".a8se.com"))


def url_kind_and_slug(url: str) -> tuple[str, str]:
    segments = [s for s in urlsplit(url).path.split("/") if s]
    if len(segments) >= 2 and segments[0] == "noticias":
        return KIND_NEWS, segments[1].lower()
    if segments and segments[0] == "tv-atalaia":
        return KIND_TV, ""
    return KIND_OTHER, ""


def canonical_slug(raw_slug: str, aliases: dict[str, str]) -> str:
    return aliases.get(raw_slug, raw_slug)


def parse_aliases(value: str | None) -> dict[str, str]:
    """Converte o JSON de `news_categories` (Remote Config) em {apelido: slug}.

    Cada entrada é {"slug": "...", "aliases": ["..."]}. Levanta ValueError se
    o JSON for inválido, para quem chama decidir o fallback.
    """
    if value is None:
        return dict(DEFAULT_ALIASES)
    data = json.loads(value)
    if not isinstance(data, list):
        raise ValueError("news_categories deve ser uma lista JSON")
    mapping: dict[str, str] = {}
    for entry in data:
        if not isinstance(entry, dict) or not entry.get("slug"):
            continue
        slug = str(entry["slug"])
        for alias in entry.get("aliases") or []:
            alias = str(alias)
            if alias and alias != slug:
                mapping[alias] = slug
    return mapping


def pick_image(url: str | None) -> str | None:
    if not url:
        return None
    url = url.strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return None
    if parts.path.lower().endswith(".avif"):
        return None  # Android não exibe AVIF em notificação (SPEC §3)
    return url


def parse_pubdate(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value.strip())
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(timezone.utc)


# --- parse -----------------------------------------------------------------


def parse(xml_bytes: bytes) -> list[FeedItem]:
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise FeedError(f"XML inválido: {exc}") from exc
    channel = root.find("channel")
    if channel is None:
        raise FeedError("RSS sem <channel>")

    items: list[FeedItem] = []
    for node in channel.findall("item"):
        url = (node.findtext("link") or "").strip()
        guid = (node.findtext("guid") or "").strip() or url
        if not guid or not url:
            continue
        category_label = clean_text(node.findtext("category"))
        kind, slug = url_kind_and_slug(url)
        if kind != KIND_NEWS:
            slug = slugify(category_label)
        body = node.findtext("content:encoded", namespaces=NS) or ""
        media = node.find("media:content", NS)
        items.append(
            FeedItem(
                guid=guid,
                url=url,
                title=clean_text(node.findtext("title")),
                description=clean_text(node.findtext("description")),
                category_label=category_label,
                kind=kind,
                raw_slug=slug,
                has_body=bool(clean_text(body)),
                image=pick_image(media.get("url") if media is not None else None),
                published_at=parse_pubdate(node.findtext("pubDate")),
            )
        )
    return items


def fetch(url: str, timeout: float, attempts: int) -> bytes:
    """Baixa o RSS. Levanta FeedError depois de `attempts` tentativas."""
    last: Exception | None = None
    for _ in range(max(1, attempts)):
        try:
            resp = httpx.get(
                url,
                timeout=timeout,
                follow_redirects=True,
                headers={"User-Agent": "a8se-push/1 (+https://a8se.com)"},
            )
            resp.raise_for_status()
            return resp.content
        except httpx.HTTPError as exc:
            last = exc
    raise FeedError(f"falha ao baixar {url}: {last!r}")
