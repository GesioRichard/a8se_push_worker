"""CLI: `a8se-push run | send | status` (SPEC §7, §10).

Único lugar que lê o relógio real; a lógica recebe `now` pronto.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

from . import config as config_mod
from . import feed, policy, remote_config
from .fcm import make_sender
from .log import JsonLog
from .runner import FEED_FAILURES_KEY, SENDS_LOOKBACK, Deps, ManualSendError, run_once, send_manual
from .store import Store, from_iso

LOCK_NAME = "a8se-push.lock"
ALIASES_CACHE_NAME = "aliases_cache.json"


# --- infraestrutura ------------------------------------------------------------


class Busy(Exception):
    pass


@contextlib.contextmanager
def state_lock(state_dir: Path, wait_seconds: float):
    """Lock exclusivo no diretório de estado: `run` e `send` nunca rodam juntos.

    Em sistemas sem fcntl (Windows, só desenvolvimento) o lock é ignorado.
    """
    try:
        import fcntl
    except ImportError:
        yield
        return
    import time as _time

    state_dir.mkdir(parents=True, exist_ok=True)
    with open(state_dir / LOCK_NAME, "w") as fh:
        deadline = _time.monotonic() + wait_seconds
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if _time.monotonic() >= deadline:
                    raise Busy() from None
                _time.sleep(0.5)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def make_pinger(url: str, log: JsonLog):
    def ping(suffix: str) -> None:
        if not url:
            return
        try:
            httpx.get(url.rstrip("/") + suffix, timeout=10)
        except httpx.HTTPError as exc:
            log("heartbeat_failed", level="warning", error=type(exc).__name__)

    return ping


def make_deps(cfg: config_mod.Config, log: JsonLog) -> Deps:
    def fetch_param():
        if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
            raise RuntimeError("GOOGLE_APPLICATION_CREDENTIALS não definido")
        return remote_config.fetch_parameter(cfg.firebase_project, cfg.remote_config.parameter)

    return Deps(
        fetch_feed=lambda: feed.fetch(cfg.feed_url, cfg.feed.timeout_seconds, cfg.feed.attempts),
        load_aliases=lambda now: remote_config.load_aliases(
            cfg.state_dir / ALIASES_CACHE_NAME, now, cfg.remote_config.cache_minutes, fetch_param, log
        ),
        sender=make_sender(cfg.mode, cfg.firebase_project),
        ping=make_pinger(cfg.healthcheck_url, log),
        log=log,
    )


def resolve_now(value: str | None, cfg: config_mod.Config) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    if cfg.mode == "production":
        raise SystemExit("--now não é permitido em production")
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=cfg.tz)
    return dt.astimezone(timezone.utc)


def ask_yes_no(question: str) -> bool:
    try:
        answer = input(f"{question} [s/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in ("s", "sim", "y", "yes")


# --- comandos ------------------------------------------------------------------


def cmd_run(cfg, args, log: JsonLog) -> int:
    now = resolve_now(args.now, cfg)
    try:
        with state_lock(cfg.state_dir, wait_seconds=0):
            store = Store(cfg.state_db)
            try:
                run_once(cfg, store, now, make_deps(cfg, log))
            finally:
                store.close()
    except Busy:
        log("already_running", level="warning")
    return 0


def cmd_send(cfg, args, log: JsonLog) -> int:
    now = resolve_now(args.now, cfg)
    try:
        with state_lock(cfg.state_dir, wait_seconds=60):
            store = Store(cfg.state_db)
            try:
                guid = send_manual(
                    cfg, store, now, make_deps(cfg, log), args.url,
                    urgent=args.urgent, force=args.force, confirm=ask_yes_no,
                )
            finally:
                store.close()
    except Busy:
        print("outra execução está em andamento; tente de novo em instantes", file=sys.stderr)
        return 1
    except ManualSendError as exc:
        print(f"não enviado: {exc}", file=sys.stderr)
        return 1
    verb = "simulado (dry-run)" if cfg.mode == "dry-run" else f"enviado ({cfg.mode})"
    print(f"{verb}: {guid}")
    return 0


def cmd_status(cfg, args, log: JsonLog) -> int:
    now = resolve_now(args.now, cfg)
    tz = cfg.tz
    store = Store(cfg.state_db)
    try:
        start = policy.day_start(now, tz)
        sends = store.sends_since(now - SENDS_LOOKBACK, cfg.mode)
        today = store.send_log(start, cfg.mode)
        normal = sum(1 for r in today if not r["urgent"])
        urgent = len(today) - normal
        p = cfg.policy

        def local(iso: str | None) -> str:
            dt = from_iso(iso)
            return dt.astimezone(tz).strftime("%d/%m %H:%M") if dt else "-"

        print(f"modo: {cfg.mode}    agora: {now.astimezone(tz):%d/%m/%Y %H:%M} ({cfg.timezone})")
        print(f"hoje: {normal}/{p.max_per_day} normais, {urgent}/{p.urgent_max_per_day} urgentes")
        until = next((u for u, _ in p.period_caps if now.astimezone(tz).time() < u), None)
        if until is not None:
            print(f"cota do período: {normal}/{policy.period_cap(now, cfg)} até {until:%H:%M}")
        for r in today:
            flag = " URGENTE" if r["urgent"] else ""
            print(f"  {local(r['sent_at'])}  news_{r['slug']:<12}{flag}  {r['title'] or r['guid']}")
        last = store.last_send(cfg.mode)
        if last:
            print(f"último envio: {local(last['sent_at'])}  news_{last['slug']}  {last['title'] or last['guid']}")
        else:
            print("último envio: nenhum")

        blocked = policy.global_block_reason(now, sends, cfg)
        print(f"próxima execução: {'bloqueada por ' + blocked if blocked else 'pode enviar'}")

        pending = store.pending()
        print(f"fila (pending): {len(pending)}")
        for c in sorted(pending, key=lambda c: c.published_at or now, reverse=True):
            age = int((now - c.published_at).total_seconds() // 60) if c.published_at else -1
            cat = policy.category_block_reason(c.slug, now, sends, cfg)
            item = store.get_item(c.guid)
            print(
                f"  {age:>3} min  prio {policy.priority_of(c.slug, cfg)}  news_{c.slug:<12}"
                f"{' [' + cat + ']' if cat else ''}  {item.title if item else c.guid}"
            )
        failures = store.get_meta(FEED_FAILURES_KEY, "0")
        print(f"falhas seguidas do RSS: {failures}")
        counts = store.status_counts()
        print("itens: " + (", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "nenhum"))
    finally:
        store.close()
    return 0


# --- entrada -------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="a8se-push", description=__doc__)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config",
        help=f"caminho do config.toml (padrão: ${config_mod.CONFIG_ENV_VAR} ou {config_mod.DEFAULT_CONFIG_PATH})",
    )
    common.add_argument("--now", help=argparse.SUPPRESS)  # simulação; recusado em production
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("run", parents=[common], help="uma execução (timer do systemd)")

    send = sub.add_parser("send", parents=[common], help="envio manual de uma matéria do feed")
    send.add_argument("--url", required=True, help="URL (ou guid) da matéria no RSS")
    send.add_argument("--urgent", action="store_true", help="plantão: também em news_urgente")
    send.add_argument("--force", action="store_true", help="ignora a política normal (não vale para --urgent)")

    sub.add_parser("status", parents=[common], help="envios do dia, fila e último envio")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    log = JsonLog()
    try:
        cfg = config_mod.load(args.config)
    except config_mod.ConfigError as exc:
        log("config_error", level="error", error=str(exc))
        return 2
    handler = {"run": cmd_run, "send": cmd_send, "status": cmd_status}[args.command]
    return handler(cfg, args, log)


if __name__ == "__main__":
    sys.exit(main())
