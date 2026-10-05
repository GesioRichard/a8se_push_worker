from datetime import time

import pytest

from a8se_push import feed, policy
from a8se_push.policy import Pending, SendRecord

from .conftest import TZ, at, make_cfg, minutes, news_item, rss


def item(n, slug, published, **kw):
    (it,) = feed.parse(rss([news_item(n, slug, published, **kw)]))
    return it


def normal(slug, when, guid="g"):
    return SendRecord(guid=guid, slug=slug, urgent=False, sent_at=when)


def urgent(slug, when, guid="u"):
    return SendRecord(guid=guid, slug=slug, urgent=True, sent_at=when)


# --- horário de silêncio e dia local ---------------------------------------------


@pytest.mark.parametrize(
    "local, quiet",
    [
        ("2026-09-30 21:59", False),
        ("2026-09-30 22:00", True),
        ("2026-09-30 23:30", True),
        ("2026-10-01 00:00", True),
        ("2026-10-01 06:59", True),
        ("2026-10-01 07:00", False),
        ("2026-10-01 12:00", False),
    ],
)
def test_quiet_hours_wrap_midnight(local, quiet):
    assert policy.in_quiet_hours(at(local), TZ, time(22), time(7)) is quiet


def test_quiet_hours_same_day_window():
    assert policy.in_quiet_hours(at("2026-09-30 13:00"), TZ, time(12), time(14))
    assert not policy.in_quiet_hours(at("2026-09-30 14:00"), TZ, time(12), time(14))


def test_day_start_is_local_midnight():
    assert policy.day_start(at("2026-09-30 01:30"), TZ) == at("2026-09-30 00:00")
    # 23:30 em Maceió já é dia seguinte em UTC, mas o "dia" é o local
    assert policy.day_start(at("2026-09-30 23:30"), TZ) == at("2026-09-30 00:00")


# --- classificação -------------------------------------------------------------------


NOW = at("2026-09-30 10:00")


def classify(it, slug=None, cfg=None, keys=frozenset()):
    cfg = cfg or make_cfg()
    slug = slug or it.raw_slug
    return policy.classify(it, slug, NOW, cfg, set(keys), feed.normalize_title(it.title))


def test_fresh_news_is_candidate():
    assert classify(item(1, "policia", NOW - minutes(5))) == ("pending", None)


def test_tv_is_never_candidate():
    it = item(1, "jornal-do-estado", NOW - minutes(5), path="tv-atalaia", body=False)
    assert classify(it) == ("skipped", "tv")


def test_tv_with_body_is_still_skipped():
    it = item(1, "cidade-alerta", NOW - minutes(5), path="tv-atalaia", body=True)
    assert classify(it) == ("skipped", "tv")


def test_news_without_body_is_skipped():
    assert classify(item(1, "policia", NOW - minutes(5), body=False)) == ("skipped", "no_body")


def test_candidate_age_limit_is_60_minutes():
    assert classify(item(1, "policia", NOW - minutes(60)))[0] == "pending"
    assert classify(item(1, "policia", NOW - minutes(61))) == ("skipped", "old")


def test_future_tolerance_is_10_minutes():
    assert classify(item(1, "policia", NOW + minutes(10)))[0] == "pending"
    assert classify(item(1, "policia", NOW + minutes(11))) == ("skipped", "future")


def test_category_disabled():
    cfg = make_cfg(enabled=["sergipe"])
    assert classify(item(1, "policia", NOW), cfg=cfg) == ("skipped", "category_disabled")
    assert classify(item(2, "sergipe", NOW), cfg=cfg)[0] == "pending"


def test_duplicate_title_of_recent_send():
    it = item(1, "policia", NOW, title="Três celulares roubados são recuperados!")
    keys = {feed.normalize_title("TRÊS CELULARES ROUBADOS SÃO RECUPERADOS")}
    assert classify(it, keys=keys) == ("skipped", "duplicate_title")


def test_age_limit_comes_from_config():
    cfg = make_cfg(policy={"candidate_max_age_minutes": 30})
    assert classify(item(1, "policia", NOW - minutes(31)), cfg=cfg) == ("skipped", "old")


# --- portão global -----------------------------------------------------------------


def test_global_gate_open_with_no_sends():
    assert policy.global_block_reason(NOW, [], make_cfg()) is None


def test_global_gate_quiet_hours():
    assert policy.global_block_reason(at("2026-09-30 22:10"), [], make_cfg()) == policy.QUIET_HOURS


def test_global_min_gap_60_minutes():
    cfg = make_cfg()
    sends = [normal("policia", NOW - minutes(59))]
    assert policy.global_block_reason(NOW, sends, cfg) == policy.MIN_GAP
    sends = [normal("policia", NOW - minutes(60))]
    assert policy.global_block_reason(NOW, sends, cfg) is None


def test_urgent_send_also_counts_for_min_gap():
    sends = [urgent("policia", NOW - minutes(10))]
    assert policy.global_block_reason(NOW, sends, make_cfg()) == policy.MIN_GAP


def eight_sends(day="2026-09-30"):
    return [normal("x", at(f"{day} {h:02d}:00")) for h in range(7, 22, 2)]  # 07h, 09h, ..., 21h


def test_global_daily_cap_8_counts_only_today_and_only_normal():
    cfg = make_cfg()
    today = eight_sends()
    assert len(today) == 8
    assert policy.global_block_reason(at("2026-09-30 21:59"), today[:-1] + [normal("x", at("2026-09-30 20:30"))], cfg) == policy.DAILY_CAP
    seven = today[:7] + [urgent("x", at("2026-09-30 08:00"))]
    assert policy.global_block_reason(at("2026-09-30 21:30"), seven, cfg) is None
    # no dia seguinte zera
    assert policy.global_block_reason(at("2026-10-01 09:00"), today, cfg) is None


def test_global_cap_from_config():
    cfg = make_cfg(policy={"max_per_day": 2, "period_caps": []})
    sends = [normal("x", at("2026-09-30 07:00")), normal("y", at("2026-09-30 08:00"))]
    assert policy.global_block_reason(NOW, sends, cfg) == policy.DAILY_CAP


# --- cotas por período ----------------------------------------------------------------


def test_morning_quota_3_until_noon():
    cfg = make_cfg()
    morning = [normal("x", at(f"2026-09-30 0{h}:00")) for h in (7, 8, 9)]
    assert policy.global_block_reason(at("2026-09-30 11:58"), morning, cfg) == policy.PERIOD_CAP
    assert policy.global_block_reason(at("2026-09-30 12:00"), morning, cfg) is None


def test_afternoon_quota_is_cumulative_6_until_18h():
    cfg = make_cfg()
    six = [normal("x", at(f"2026-09-30 {h:02d}:00")) for h in (7, 8, 9, 12, 14, 16)]
    assert policy.global_block_reason(at("2026-09-30 17:30"), six, cfg) == policy.PERIOD_CAP
    assert policy.global_block_reason(at("2026-09-30 18:00"), six, cfg) is None


def test_unused_morning_quota_rolls_over_to_afternoon():
    cfg = make_cfg()
    afternoon = [normal("x", at(f"2026-09-30 {h:02d}:00")) for h in (12, 13, 14, 15, 16)]  # manhã vazia
    assert policy.global_block_reason(at("2026-09-30 17:00"), afternoon, cfg) is None
    assert policy.global_block_reason(at("2026-09-30 17:30"), afternoon + [normal("x", at("2026-09-30 16:30"))], cfg) == policy.PERIOD_CAP


def test_urgent_does_not_consume_period_quota():
    cfg = make_cfg()
    sends = [normal("x", at("2026-09-30 07:00")), normal("x", at("2026-09-30 08:00")), urgent("x", at("2026-09-30 09:00"))]
    assert policy.global_block_reason(at("2026-09-30 10:30"), sends, cfg) is None


def test_period_caps_can_be_disabled():
    cfg = make_cfg(policy={"period_caps": []})
    morning = [normal("x", at(f"2026-09-30 0{h}:00")) for h in (7, 8, 9)]
    assert policy.global_block_reason(at("2026-09-30 11:00"), morning, cfg) is None


# --- portão por editoria -------------------------------------------------------------


def test_category_min_gap_2h():
    cfg = make_cfg()
    sends = [normal("policia", NOW - minutes(119))]
    assert policy.category_block_reason("policia", NOW, sends, cfg) == policy.CATEGORY_MIN_GAP
    assert policy.category_block_reason("sergipe", NOW, sends, cfg) is None
    sends = [normal("policia", NOW - minutes(120))]
    assert policy.category_block_reason("policia", NOW, sends, cfg) is None


def test_category_daily_cap_3():
    cfg = make_cfg()
    sends = [normal("policia", at(f"2026-09-30 0{h}:00")) for h in (1, 3, 5)]
    assert policy.category_block_reason("policia", at("2026-09-30 12:00"), sends, cfg) == policy.CATEGORY_DAILY_CAP
    assert policy.category_block_reason("policia", at("2026-10-01 12:00"), sends, cfg) is None


# --- urgente -------------------------------------------------------------------------


def test_urgent_cap_2_per_day_and_ignores_quiet_and_gap():
    cfg = make_cfg()
    night = at("2026-09-30 23:00")
    assert policy.urgent_block_reason(night, [normal("x", night - minutes(1))], cfg) is None
    sends = [urgent("x", at("2026-09-30 10:00")), urgent("y", at("2026-09-30 11:00"))]
    assert policy.urgent_block_reason(night, sends, cfg) == policy.URGENT_DAILY_CAP
    assert policy.urgent_block_reason(at("2026-10-01 08:00"), sends, cfg) is None


# --- escolha ---------------------------------------------------------------------------


def pending(guid, slug, published):
    return Pending(guid=guid, slug=slug, title_key=guid, published_at=published)


def test_rank_by_priority_then_most_recent():
    cfg = make_cfg()
    cands = [
        pending("brasil-new", "brasil", NOW - minutes(1)),
        pending("policia-old", "policia", NOW - minutes(30)),
        pending("policia-new", "policia", NOW - minutes(10)),
        pending("sergipe", "sergipe", NOW - minutes(50)),
        pending("desconhecida", "concursos", NOW),
    ]
    ranked = [c.guid for c in policy.rank_candidates(cands, NOW, [], cfg)]
    assert ranked == ["sergipe", "policia-new", "policia-old", "brasil-new", "desconhecida"]


def test_rank_excludes_blocked_categories_and_expired():
    cfg = make_cfg()
    cands = [
        pending("sergipe", "sergipe", NOW - minutes(5)),
        pending("velha", "policia", NOW - minutes(61)),
        pending("brasil", "brasil", NOW - minutes(5)),
    ]
    sends = [normal("sergipe", NOW - minutes(60))]  # sergipe em intervalo de 2 h
    assert [c.guid for c in policy.rank_candidates(cands, NOW, sends, cfg)] == ["brasil"]
