"""Leitura e validação do config.toml (SPEC §9).

Os valores padrão abaixo são os da SPEC. Eles existem para que um arquivo
incompleto continue seguro; em produção, o config.toml deve declarar tudo
(veja deploy/config.example.toml).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

MODES = ("dry-run", "test", "production")
DEFAULT_CONFIG_PATH = "/etc/a8se-push/config.toml"
CONFIG_ENV_VAR = "A8SE_PUSH_CONFIG"


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class PolicyConfig:
    max_per_day: int = 8
    min_gap_minutes: int = 60
    # Cotas acumuladas no dia: antes de `until`, no máximo `max` envios normais.
    # O que um período não usa passa para o seguinte; o último período vai até max_per_day.
    period_caps: tuple[tuple[time, int], ...] = ((time(12, 0), 3), (time(18, 0), 6))
    per_category_max_per_day: int = 3
    per_category_min_gap_minutes: int = 120
    quiet_start: time = time(22, 0)
    quiet_end: time = time(7, 0)
    candidate_max_age_minutes: int = 60
    urgent_max_per_day: int = 2
    ttl_hours: int = 4
    max_per_run: int = 1
    future_tolerance_minutes: int = 10
    duplicate_title_hours: int = 24
    items_retention_days: int = 30


@dataclass(frozen=True)
class FeedConfig:
    timeout_seconds: float = 20.0
    attempts: int = 2
    failure_alert_threshold: int = 5


@dataclass(frozen=True)
class RemoteConfigConfig:
    parameter: str = "news_categories"
    cache_minutes: int = 60


@dataclass(frozen=True)
class CategoriesConfig:
    enabled: tuple[str, ...] = ()  # vazio = todas as editorias de notícia
    priority: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class Config:
    mode: str = "dry-run"
    feed_url: str = "https://a8se.com/rss.xml"
    timezone: str = "America/Maceio"
    firebase_project: str = "app-radios-d4371"
    state_db: str = "/var/lib/a8se-push/state.db"
    healthcheck_url: str = ""
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    feed: FeedConfig = field(default_factory=FeedConfig)
    remote_config: RemoteConfigConfig = field(default_factory=RemoteConfigConfig)
    categories: CategoriesConfig = field(default_factory=CategoriesConfig)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def state_dir(self) -> Path:
        return Path(self.state_db).parent


def _parse_hhmm(value: str, key: str) -> time:
    try:
        hh, mm = value.split(":")
        return time(int(hh), int(mm))
    except (ValueError, AttributeError) as exc:
        raise ConfigError(f"{key}: esperado 'HH:MM', recebido {value!r}") from exc


def _parse_period_caps(value) -> tuple[tuple[time, int], ...]:
    """[{until = "12:00", max = 3}, ...] → ((time(12), 3), ...)."""
    if not isinstance(value, list):
        raise ConfigError('policy.period_caps: esperado [{until = "HH:MM", max = N}, ...]')
    caps = []
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != {"until", "max"}:
            raise ConfigError('policy.period_caps: cada item precisa de until = "HH:MM" e max = N')
        caps.append((_parse_hhmm(entry["until"], "policy.period_caps"), int(entry["max"])))
    return tuple(caps)


def _pick(section: dict, cls, key_map: dict[str, str] | None = None) -> dict:
    """Copia de `section` só as chaves conhecidas por `cls`; recusa chaves desconhecidas."""
    key_map = key_map or {}
    known = set(cls.__dataclass_fields__) | set(key_map)
    unknown = set(section) - known
    if unknown:
        raise ConfigError(f"chaves desconhecidas em [{cls.__name__}]: {sorted(unknown)}")
    return {key_map.get(k, k): v for k, v in section.items()}


def from_dict(raw: dict) -> Config:
    raw = dict(raw)
    policy_raw = dict(raw.pop("policy", {}))
    quiet = policy_raw.pop("quiet_hours", None)
    period_caps = policy_raw.pop("period_caps", None)
    policy_kwargs = _pick(policy_raw, PolicyConfig)
    if period_caps is not None:
        policy_kwargs["period_caps"] = _parse_period_caps(period_caps)
    if quiet is not None:
        if not (isinstance(quiet, list) and len(quiet) == 2):
            raise ConfigError("policy.quiet_hours: esperado [\"HH:MM\", \"HH:MM\"]")
        policy_kwargs["quiet_start"] = _parse_hhmm(quiet[0], "policy.quiet_hours")
        policy_kwargs["quiet_end"] = _parse_hhmm(quiet[1], "policy.quiet_hours")
    policy = PolicyConfig(**policy_kwargs)

    feed = FeedConfig(**_pick(raw.pop("feed", {}), FeedConfig))
    remote = RemoteConfigConfig(**_pick(raw.pop("remote_config", {}), RemoteConfigConfig))

    cats_raw = dict(raw.pop("categories", {}))
    enabled = tuple(str(s) for s in cats_raw.pop("enabled", []))
    priority = {str(k): int(v) for k, v in dict(cats_raw.pop("priority", {})).items()}
    if cats_raw:
        raise ConfigError(f"chaves desconhecidas em [categories]: {sorted(cats_raw)}")
    categories = CategoriesConfig(enabled=enabled, priority=priority)

    top = _pick(raw, Config)
    cfg = Config(**top, policy=policy, feed=feed, remote_config=remote, categories=categories)
    _validate(cfg)
    return cfg


def _validate(cfg: Config) -> None:
    if cfg.mode not in MODES:
        raise ConfigError(f"mode deve ser um de {MODES}, recebido {cfg.mode!r}")
    try:
        cfg.tz
    except Exception as exc:  # ZoneInfoNotFoundError e afins
        raise ConfigError(f"timezone inválido: {cfg.timezone!r}") from exc
    p = cfg.policy
    for name in (
        "max_per_day", "min_gap_minutes", "per_category_max_per_day",
        "per_category_min_gap_minutes", "candidate_max_age_minutes",
        "urgent_max_per_day", "ttl_hours", "max_per_run",
        "future_tolerance_minutes", "duplicate_title_hours", "items_retention_days",
    ):
        if getattr(p, name) < 0:
            raise ConfigError(f"policy.{name} não pode ser negativo")
    caps = p.period_caps
    if any(a[0] >= b[0] or a[1] > b[1] for a, b in zip(caps, caps[1:])):
        raise ConfigError("policy.period_caps: horários e máximos devem ser crescentes")
    if any(m < 0 or m > p.max_per_day for _, m in caps):
        raise ConfigError("policy.period_caps: cada max deve ficar entre 0 e max_per_day")
    if cfg.feed.attempts < 1:
        raise ConfigError("feed.attempts deve ser >= 1")


def load(path: str | os.PathLike | None = None) -> Config:
    path = Path(path or os.environ.get(CONFIG_ENV_VAR) or DEFAULT_CONFIG_PATH)
    try:
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
    except FileNotFoundError as exc:
        raise ConfigError(f"arquivo de configuração não encontrado: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: TOML inválido: {exc}") from exc
    return from_dict(raw)
