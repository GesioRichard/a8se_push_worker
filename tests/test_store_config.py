import json
from datetime import time, timedelta
from pathlib import Path

import pytest

from a8se_push import config, remote_config
from a8se_push.log import MemoryLog
from a8se_push.store import Store

from .conftest import at

ROOT = Path(__file__).resolve().parents[1]


# --- store ---------------------------------------------------------------------------


def test_schema_matches_spec_tables(tmp_path):
    s = Store(tmp_path / "state.db")
    cols = {r["name"] for r in s.conn.execute("PRAGMA table_info(items)")}
    assert cols == {"guid", "url", "title", "title_key", "slug", "published_at", "first_seen_at", "status", "reason"}
    cols = {r["name"] for r in s.conn.execute("PRAGMA table_info(sends)")}
    assert cols == {"id", "guid", "slug", "urgent", "sent_at", "fcm_message_id", "mode"}
    idx = {r["name"] for r in s.conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"idx_items_status", "idx_sends_sent_at"} <= idx


def _insert(s, guid, seen, status="pending"):
    s.insert_item(guid=guid, url=guid, title="t", title_key="t", slug="x",
                  published_at=seen, first_seen_at=seen, status=status)


def test_purge_removes_items_older_than_30_days_but_keeps_sends():
    s = Store(":memory:")
    now = at("2026-09-30 10:00")
    _insert(s, "old", now - timedelta(days=31))
    _insert(s, "new", now - timedelta(days=1))
    s.record_send(guid="old", slug="x", urgent=False, sent_at=now - timedelta(days=31), fcm_message_id="m", mode="test")
    assert s.purge_items(now - timedelta(days=30)) == 1
    assert s.known_guids(["old", "new"]) == {"new"}
    assert s.conn.execute("SELECT COUNT(*) FROM sends").fetchone()[0] == 1


def test_sends_are_filtered_by_mode():
    s = Store(":memory:")
    now = at("2026-09-30 10:00")
    s.record_send(guid="a", slug="x", urgent=False, sent_at=now, fcm_message_id=None, mode="dry-run")
    s.record_send(guid="b", slug="x", urgent=True, sent_at=now, fcm_message_id="m", mode="test")
    assert [r.guid for r in s.sends_since(now - timedelta(hours=1), "test")] == ["b"]
    assert s.sends_since(now - timedelta(hours=1), "production") == []


def test_dates_are_iso_utc(tmp_path):
    s = Store(":memory:")
    _insert(s, "g", at("2026-09-30 10:00"))
    row = s.conn.execute("SELECT published_at FROM items").fetchone()
    assert row[0] == "2026-09-30T13:00:00Z"


# --- config ----------------------------------------------------------------------------


def test_example_config_is_valid_and_dry_run():
    cfg = config.load(ROOT / "deploy" / "config.example.toml")
    assert cfg.mode == "dry-run"
    p = cfg.policy
    assert (p.max_per_day, p.min_gap_minutes, p.per_category_max_per_day, p.per_category_min_gap_minutes) == (8, 60, 3, 120)
    assert p.period_caps == ((time(12, 0), 3), (time(18, 0), 6))
    assert (p.quiet_start.hour, p.quiet_end.hour) == (22, 7)
    assert (p.candidate_max_age_minutes, p.urgent_max_per_day, p.ttl_hours) == (60, 2, 4)
    assert cfg.timezone == "America/Maceio"
    assert cfg.categories.priority["sergipe"] == 3


def test_invalid_mode_is_rejected():
    with pytest.raises(config.ConfigError):
        config.from_dict({"mode": "prod"})


def test_unknown_policy_key_is_rejected():
    with pytest.raises(config.ConfigError):
        config.from_dict({"policy": {"max_por_dia": 6}})


@pytest.mark.parametrize(
    "caps",
    [
        [{"until": "18:00", "max": 6}, {"until": "12:00", "max": 3}],  # fora de ordem
        [{"until": "12:00", "max": 5}, {"until": "18:00", "max": 4}],  # máximo decrescente
        [{"until": "12:00", "max": 9}],  # acima de max_per_day
        [{"until": "12:00"}],
        "12:00=3",
    ],
)
def test_invalid_period_caps_are_rejected(caps):
    with pytest.raises(config.ConfigError):
        config.from_dict({"policy": {"period_caps": caps}})


def test_default_mode_is_dry_run():
    assert config.from_dict({}).mode == "dry-run"


# --- apelidos (Remote Config) ----------------------------------------------------------------


def test_aliases_fetched_then_cached(tmp_path):
    calls = []
    fetcher = lambda: calls.append(1) or '[{"slug": "esportes", "aliases": ["esporte", "futebol"]}]'
    cache = tmp_path / "aliases.json"
    now = at("2026-09-30 10:00")
    a = remote_config.load_aliases(cache, now, 60, fetcher, MemoryLog())
    b = remote_config.load_aliases(cache, now + timedelta(minutes=59), 60, fetcher, MemoryLog())
    assert a == b == {"esporte": "esportes", "futebol": "esportes"}
    assert len(calls) == 1
    remote_config.load_aliases(cache, now + timedelta(minutes=61), 60, fetcher, MemoryLog())
    assert len(calls) == 2


def test_aliases_fall_back_to_stale_cache_then_default(tmp_path):
    def broken():
        raise RuntimeError("sem rede")

    cache = tmp_path / "aliases.json"
    now = at("2026-09-30 10:00")
    log = MemoryLog()
    assert remote_config.load_aliases(cache, now, 60, broken, log) == {"esporte": "esportes"}
    cache.write_text(json.dumps({"fetched_at": "2026-09-29T10:00:00Z", "aliases": {"futebol": "esportes"}}))
    assert remote_config.load_aliases(cache, now, 60, broken, log) == {"futebol": "esportes"}
    assert log.events("aliases_fetch_failed")
