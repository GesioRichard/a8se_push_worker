"""Política de seleção e frequência (SPEC §5, §6 e §7). Só funções puras.

Nenhuma função aqui lê o relógio: `now` (datetime com fuso) sempre vem de fora.

Convenções:
- Tetos diários (global, cotas por período e por editoria) contam só envios **normais**; o
  urgente tem teto próprio (`urgent_max_per_day`).
- Intervalos mínimos (global e por editoria) contam **qualquer** envio,
  inclusive urgente ("intervalo mínimo entre dois envios quaisquer").
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Iterable, Sequence
from zoneinfo import ZoneInfo

from .config import Config, PolicyConfig
from .feed import KIND_NEWS, KIND_TV, FeedItem, is_a8se_https

PENDING = "pending"
SENT = "sent"
SKIPPED = "skipped"
EXPIRED = "expired"
FAILED = "failed"
SEEDED = "seeded"

# Motivos de bloqueio (log / status)
QUIET_HOURS = "quiet_hours"
DAILY_CAP = "daily_cap"
PERIOD_CAP = "period_cap"
MIN_GAP = "min_gap"
CATEGORY_DAILY_CAP = "category_daily_cap"
CATEGORY_MIN_GAP = "category_min_gap"
URGENT_DAILY_CAP = "urgent_daily_cap"


@dataclass(frozen=True)
class SendRecord:
    guid: str
    slug: str
    urgent: bool
    sent_at: datetime  # UTC


@dataclass(frozen=True)
class Pending:
    guid: str
    slug: str
    title_key: str
    published_at: datetime | None  # UTC


# --- tempo -----------------------------------------------------------------


def day_start(now: datetime, tz: ZoneInfo) -> datetime:
    """Início (UTC) do dia local de `now`. "Dia" = 00:00–23:59 no fuso configurado."""
    local = now.astimezone(tz)
    return datetime.combine(local.date(), time(0, 0), tzinfo=tz).astimezone(timezone.utc)


def in_quiet_hours(now: datetime, tz: ZoneInfo, start: time, end: time) -> bool:
    t = now.astimezone(tz).time()
    if start == end:
        return False
    if start < end:
        return start <= t < end
    return t >= start or t < end  # atravessa a meia-noite (22:00–07:00)


def is_too_old(published_at: datetime | None, now: datetime, policy: PolicyConfig) -> bool:
    if published_at is None:
        return True
    return now - published_at > timedelta(minutes=policy.candidate_max_age_minutes)


# --- classificação de itens novos (SPEC §6) --------------------------------


def classify(
    item: FeedItem,
    slug: str,
    now: datetime,
    cfg: Config,
    recent_sent_title_keys: set[str],
    title_key: str,
) -> tuple[str, str | None]:
    """Devolve (status, motivo) para um item nunca visto: pending ou skipped."""
    if item.kind == KIND_TV:
        return SKIPPED, "tv"
    if item.kind != KIND_NEWS:
        return SKIPPED, "not_news"
    if not item.has_body:
        return SKIPPED, "no_body"
    if not is_a8se_https(item.url):
        return SKIPPED, "invalid_url"
    if not item.title:
        return SKIPPED, "no_title"
    enabled = cfg.categories.enabled
    if enabled and slug not in enabled:
        return SKIPPED, "category_disabled"
    if item.published_at is None:
        return SKIPPED, "no_date"
    if item.published_at - now > timedelta(minutes=cfg.policy.future_tolerance_minutes):
        return SKIPPED, "future"
    if is_too_old(item.published_at, now, cfg.policy):
        return SKIPPED, "old"
    if title_key in recent_sent_title_keys:
        return SKIPPED, "duplicate_title"
    return PENDING, None


# --- portões de envio (SPEC §5, §7 passos 6–7) -------------------------------


def _normal_today(sends: Iterable[SendRecord], start: datetime) -> list[SendRecord]:
    return [s for s in sends if not s.urgent and s.sent_at >= start]


def period_cap(now: datetime, cfg: Config) -> int | None:
    """Teto acumulado do dia que vale agora (cotas por período), ou None no último período."""
    t = now.astimezone(cfg.tz).time()
    return next((m for until, m in cfg.policy.period_caps if t < until), None)


def global_block_reason(now: datetime, sends: Sequence[SendRecord], cfg: Config) -> str | None:
    p = cfg.policy
    if in_quiet_hours(now, cfg.tz, p.quiet_start, p.quiet_end):
        return QUIET_HOURS
    sent_today = len(_normal_today(sends, day_start(now, cfg.tz)))
    if sent_today >= p.max_per_day:
        return DAILY_CAP
    cap = period_cap(now, cfg)
    if cap is not None and sent_today >= cap:
        return PERIOD_CAP
    last = max((s.sent_at for s in sends), default=None)
    if last is not None and now - last < timedelta(minutes=p.min_gap_minutes):
        return MIN_GAP
    return None


def category_block_reason(
    slug: str, now: datetime, sends: Sequence[SendRecord], cfg: Config
) -> str | None:
    p = cfg.policy
    same = [s for s in sends if s.slug == slug]
    if len(_normal_today(same, day_start(now, cfg.tz))) >= p.per_category_max_per_day:
        return CATEGORY_DAILY_CAP
    last = max((s.sent_at for s in same), default=None)
    if last is not None and now - last < timedelta(minutes=p.per_category_min_gap_minutes):
        return CATEGORY_MIN_GAP
    return None


def urgent_block_reason(now: datetime, sends: Sequence[SendRecord], cfg: Config) -> str | None:
    """Urgente fura silêncio e intervalo global, mas não o próprio teto (SPEC §5)."""
    start = day_start(now, cfg.tz)
    today = [s for s in sends if s.urgent and s.sent_at >= start]
    if len(today) >= cfg.policy.urgent_max_per_day:
        return URGENT_DAILY_CAP
    return None


def normal_block_reason(
    slug: str, now: datetime, sends: Sequence[SendRecord], cfg: Config
) -> str | None:
    return global_block_reason(now, sends, cfg) or category_block_reason(slug, now, sends, cfg)


# --- escolha (SPEC §6 "Escolha") ---------------------------------------------


def priority_of(slug: str, cfg: Config) -> int:
    return cfg.categories.priority.get(slug, 0)


def rank_candidates(
    pending: Sequence[Pending], now: datetime, sends: Sequence[SendRecord], cfg: Config
) -> list[Pending]:
    """Pendentes elegíveis agora, na ordem de escolha: maior prioridade, depois o mais recente.

    Não aplica o portão global (quem chama verifica antes).
    """
    eligible = [
        c
        for c in pending
        if not is_too_old(c.published_at, now, cfg.policy)
        and category_block_reason(c.slug, now, sends, cfg) is None
    ]
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    return sorted(
        eligible,
        key=lambda c: (priority_of(c.slug, cfg), c.published_at or epoch, c.guid),
        reverse=True,
    )
