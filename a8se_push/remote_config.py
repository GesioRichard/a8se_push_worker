"""Apelidos de editoria vindos do Remote Config (SPEC §4.1), com cache local.

O parâmetro `news_categories` está no template de **cliente** do Remote Config.
O módulo `firebase_admin.remote_config` só lê templates de servidor, então o
template é lido pela API REST (`GET /v1/projects/<id>/remoteConfig`) com a
mesma credencial do Admin SDK (papel Firebase Remote Config Viewer).

Ordem de preferência: cache com menos de `cache_minutes` → API → cache antigo
→ padrão do app (`esporte → esportes`).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

import httpx

from .feed import DEFAULT_ALIASES, parse_aliases
from .store import from_iso, to_iso

API = "https://firebaseremoteconfig.googleapis.com/v1/projects/{project}/remoteConfig"

# Devolve o valor padrão (string JSON) do parâmetro, ou None se não publicado.
ParamFetcher = Callable[[], "str | None"]


def fetch_parameter(project_id: str, parameter: str, timeout: float = 20.0) -> str | None:
    from firebase_admin import credentials

    token = credentials.ApplicationDefault().get_access_token().access_token
    resp = httpx.get(
        API.format(project=project_id),
        headers={"Authorization": f"Bearer {token}", "Accept-Encoding": "gzip"},
        timeout=timeout,
    )
    resp.raise_for_status()
    param = resp.json().get("parameters", {}).get(parameter)
    if not param:
        return None
    return (param.get("defaultValue") or {}).get("value")


def load_aliases(
    cache_path: Path,
    now: datetime,
    cache_minutes: int,
    fetcher: ParamFetcher,
    log: Callable[..., None],
) -> dict[str, str]:
    cached = _read_cache(cache_path)
    if cached is not None:
        fetched_at, aliases = cached
        if now - fetched_at < timedelta(minutes=cache_minutes):
            return aliases

    try:
        aliases = parse_aliases(fetcher())
    except Exception as exc:  # noqa: BLE001 - qualquer falha cai no fallback
        log("aliases_fetch_failed", level="warning", error=f"{type(exc).__name__}: {exc}",
            fallback="cache" if cached else "default")
        return cached[1] if cached else dict(DEFAULT_ALIASES)

    _write_cache(cache_path, now, aliases)
    log("aliases_loaded", aliases=aliases)
    return aliases


def _read_cache(path: Path) -> tuple[datetime, dict[str, str]] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return from_iso(data["fetched_at"]), {str(k): str(v) for k, v in data["aliases"].items()}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def _write_cache(path: Path, now: datetime, aliases: dict[str, str]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"fetched_at": to_iso(now), "aliases": aliases}), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass  # cache é otimização; a próxima execução tenta de novo
