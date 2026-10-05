"""Orquestra uma execução (SPEC §7) e o envio manual (SPEC §10).

Todas as dependências externas (feed, Remote Config, FCM, heartbeat, relógio)
entram por `Deps` e por `now`, para que os testes rodem sem rede.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from . import policy
from .config import Config
from .fcm import PERMANENT, Sender, build_message
from .feed import FeedError, FeedItem, KIND_NEWS, canonical_slug, is_a8se_https, normalize_title, parse
from .log import JsonLog
from .store import Store

FEED_FAILURES_KEY = "feed_consecutive_failures"
SENDS_LOOKBACK = timedelta(days=2)  # cobre o "dia" local + o maior intervalo configurável


@dataclass
class Deps:
    fetch_feed: Callable[[], bytes]
    load_aliases: Callable[[datetime], dict[str, str]]
    sender: Sender
    ping: Callable[[str], None]  # "" = sucesso, "/fail" = alerta
    log: JsonLog


# --- execução automática -----------------------------------------------------


def run_once(cfg: Config, store: Store, now: datetime, deps: Deps) -> dict:
    """Uma execução completa. Devolve um resumo (também registrado em log)."""
    log = deps.log
    mode = cfg.mode
    summary: dict = {"mode": mode, "new": 0, "sent": None}

    # 1. RSS
    try:
        items = parse(deps.fetch_feed())
    except FeedError as exc:
        failures = int(store.get_meta(FEED_FAILURES_KEY, "0")) + 1
        store.set_meta(FEED_FAILURES_KEY, str(failures))
        log("feed_failed", level="error", error=str(exc), consecutive_failures=failures)
        if failures >= cfg.feed.failure_alert_threshold:
            deps.ping("/fail")
        summary["result"] = "feed_failed"
        return summary
    store.set_meta(FEED_FAILURES_KEY, "0")

    # 2. apelidos
    aliases = deps.load_aliases(now)

    # 3–4. itens novos (seed na primeira execução)
    seed = store.is_empty()
    new_count = _record_new_items(cfg, store, now, items, aliases, seed, log)
    summary["new"] = new_count
    if seed:
        log("seeded", items=new_count)
        summary["result"] = "seeded"
        deps.ping("")
        return summary

    # 5. expiração e limpeza
    expired = _expire_pending(cfg, store, now, log)
    purged = store.purge_items(now - timedelta(days=cfg.policy.items_retention_days))
    if purged:
        log("items_purged", count=purged)
    summary["expired"] = expired

    # 6–8. escolha e envio
    by_guid = {it.guid: it for it in items}
    sent_count = 0
    while sent_count < cfg.policy.max_per_run:
        sends = store.sends_since(now - SENDS_LOOKBACK, mode)
        blocked = policy.global_block_reason(now, sends, cfg)
        if blocked:
            log("no_send", reason=blocked, pending=len(store.pending()))
            summary.setdefault("result", blocked)
            break
        choice = _choose(cfg, store, now, sends, by_guid, log)
        if choice is None:
            log("no_send", reason="no_candidate")
            summary.setdefault("result", "no_candidate")
            break
        pending_item, feed_item = choice
        ok, permanent = _send(cfg, store, now, deps, feed_item, pending_item.slug, urgent=False)
        if ok:
            sent_count += 1
            summary["sent"] = feed_item.guid
            summary["result"] = "sent"
        elif not permanent:
            summary["result"] = "send_retry_later"
            break  # erro temporário: o item fica pending para a próxima execução

    log("run_done", **summary)
    deps.ping("")
    return summary


def _record_new_items(
    cfg: Config,
    store: Store,
    now: datetime,
    items: list[FeedItem],
    aliases: dict[str, str],
    seed: bool,
    log: JsonLog,
) -> int:
    known = store.known_guids([it.guid for it in items])
    recent_keys = store.sent_title_keys_since(
        now - timedelta(hours=cfg.policy.duplicate_title_hours), cfg.mode
    )
    count = 0
    for item in items:
        if item.guid in known:
            continue
        known.add(item.guid)
        slug = canonical_slug(item.raw_slug, aliases)
        title_key = normalize_title(item.title)
        if seed:
            status, reason = policy.SEEDED, None
        else:
            status, reason = policy.classify(item, slug, now, cfg, recent_keys, title_key)
        store.insert_item(
            guid=item.guid,
            url=item.url,
            title=item.title,
            title_key=title_key,
            slug=slug,
            published_at=item.published_at,
            first_seen_at=now,
            status=status,
            reason=reason,
        )
        count += 1
        if not seed:
            log("item_new", guid=item.guid, slug=slug, status=status, reason=reason, title=item.title)
    return count


def _expire_pending(cfg: Config, store: Store, now: datetime, log: JsonLog) -> int:
    n = 0
    for p in store.pending():
        if policy.is_too_old(p.published_at, now, cfg.policy):
            store.set_status(p.guid, policy.EXPIRED)
            log("item_expired", guid=p.guid, slug=p.slug)
            n += 1
    return n


def _choose(cfg, store, now, sends, by_guid, log):
    recent_keys = store.sent_title_keys_since(
        now - timedelta(hours=cfg.policy.duplicate_title_hours), cfg.mode
    )
    for p in policy.rank_candidates(store.pending(), now, sends, cfg):
        if p.title_key in recent_keys:
            store.set_status(p.guid, policy.SKIPPED, "duplicate_title")
            log("item_skipped", guid=p.guid, reason="duplicate_title")
            continue
        feed_item = by_guid.get(p.guid)
        if feed_item is None:
            # o app abre a matéria a partir do feed: fora dele, não dá para notificar
            store.set_status(p.guid, policy.SKIPPED, "gone_from_feed")
            log("item_skipped", guid=p.guid, reason="gone_from_feed")
            continue
        return p, feed_item
    return None


def _send(
    cfg: Config, store: Store, now: datetime, deps: Deps, item: FeedItem, slug: str, *, urgent: bool
) -> tuple[bool, bool]:
    """Envia e registra. Devolve (sucesso, erro_permanente)."""
    message = build_message(
        item, slug, urgent=urgent, mode=cfg.mode, now=now, ttl_hours=cfg.policy.ttl_hours
    )
    result = deps.sender.send(message)
    if result.ok:
        store.set_status(item.guid, policy.SENT)
        store.record_send(
            guid=item.guid, slug=slug, urgent=urgent, sent_at=now,
            fcm_message_id=result.message_id, mode=cfg.mode,
        )
        deps.log(
            "would_send" if cfg.mode == "dry-run" else "sent",
            mode=cfg.mode, guid=item.guid, slug=slug, urgent=urgent,
            condition=message["condition"], title=item.title,
            local_time=now.astimezone(cfg.tz).strftime("%Y-%m-%d %H:%M"),
            fcm_message_id=result.message_id,
        )
        return True, False
    permanent = result.error == PERMANENT
    if permanent:
        store.set_status(item.guid, policy.FAILED, (result.detail or "")[:500])
    deps.log(
        "send_failed", level="error", guid=item.guid, slug=slug, urgent=urgent,
        error_kind=result.error, detail=result.detail,
    )
    return False, permanent


# --- envio manual (SPEC §10) -------------------------------------------------


class ManualSendError(Exception):
    pass


def send_manual(
    cfg: Config,
    store: Store,
    now: datetime,
    deps: Deps,
    url: str,
    *,
    urgent: bool,
    force: bool,
    confirm: Callable[[str], bool],
) -> str:
    """Envia uma matéria específica. Devolve o guid enviado; levanta ManualSendError."""
    if store.is_empty():
        # sem isso o envio manual "pularia" o seed e a próxima execução trataria o feed todo como novo
        raise ManualSendError("estado vazio: rode `a8se-push run` uma vez antes (seed)")
    try:
        items = parse(deps.fetch_feed())
    except FeedError as exc:
        raise ManualSendError(f"não consegui ler o RSS: {exc}") from exc
    url = url.strip()
    item = next((it for it in items if url in (it.url, it.guid)), None)
    if item is None:
        raise ManualSendError("a matéria não está no RSS; o app só abre matérias que estão no feed")

    # Regra absoluta (CLAUDE.md §2): nada de TV nem item sem texto, nem com --force.
    if item.kind != KIND_NEWS:
        raise ManualSendError("só matérias escritas (/noticias/) podem ser notificadas")
    if not item.has_body:
        raise ManualSendError("a matéria não tem content:encoded; o app não teria texto para mostrar")
    if not is_a8se_https(item.url):
        raise ManualSendError("URL precisa ser https do domínio a8se.com")

    slug = canonical_slug(item.raw_slug, deps.load_aliases(now))
    sends = store.sends_since(now - SENDS_LOOKBACK, cfg.mode)
    if urgent:
        blocked = policy.urgent_block_reason(now, sends, cfg)
        if blocked:
            raise ManualSendError(f"bloqueado: {blocked} (urgent_max_per_day não pode ser furado)")
    else:
        blocked = policy.normal_block_reason(slug, now, sends, cfg)
        if blocked and not force:
            raise ManualSendError(f"bloqueado pela política: {blocked} (use --force para ignorar)")
        if blocked:
            deps.log("manual_force", level="warning", guid=item.guid, reason=blocked)

    if cfg.mode == "production":
        target = "urgente" if urgent else "normal"
        if not confirm(f"Enviar {target} em PRODUÇÃO para news_{slug}: \"{item.title}\"?"):
            raise ManualSendError("cancelado")

    if store.get_item(item.guid) is None:
        store.insert_item(
            guid=item.guid, url=item.url, title=item.title, title_key=normalize_title(item.title),
            slug=slug, published_at=item.published_at, first_seen_at=now,
            status=policy.SKIPPED, reason="manual",  # nunca entra na fila automática
        )
    ok, _ = _send(cfg, store, now, deps, item, slug, urgent=urgent)
    if not ok:
        raise ManualSendError("falha no envio (veja o log)")
    return item.guid
