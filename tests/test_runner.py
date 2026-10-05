from datetime import timedelta

import pytest

from a8se_push import fcm, feed
from a8se_push.fcm import SendResult
from a8se_push.runner import FEED_FAILURES_KEY, ManualSendError, run_once, send_manual

from .conftest import FakeWorld, at, make_cfg, minutes, news_item


def world(mode="dry-run", **kw):
    return FakeWorld(make_cfg(mode=mode, **kw))


def run(w, now):
    return run_once(w.cfg, w.store, now, w.deps)


def status(w, guid):
    return w.store.get_item(guid).status


def guid_of(d):
    return d["link"]


def seeded_world(now, mode="dry-run", **kw):
    """Mundo já com seed feito (um item antigo), pronto para receber novidades."""
    w = world(mode, **kw)
    w.feed_items = [news_item(0, "brasil", now - timedelta(days=1))]
    assert run(w, now - minutes(2))["result"] == "seeded"
    return w


# --- seed -------------------------------------------------------------------------


def test_first_run_seeds_and_sends_nothing_even_with_fresh_news(real_feed):
    w = world()
    w.feed_bytes = real_feed
    # 5 min depois da matéria mais recente do feed real (14:20): seria candidata
    out = run(w, at("2026-09-30 14:25"))
    assert out["result"] == "seeded"
    assert w.sender.sent == []
    assert w.store.status_counts() == {"seeded": 200}
    assert w.pings == [""]


def test_real_feed_second_run_with_new_article(real_feed):
    w = world()
    items = feed.parse(real_feed)
    newest = items[0]  # saúde, 14:20
    # seed sem a matéria mais recente
    w.feed_bytes = real_feed.replace(newest.guid.encode(), b"https://a8se.com/noticias/x/removido")
    run(w, at("2026-09-30 14:21"))
    w.feed_bytes = real_feed
    out = run(w, at("2026-09-30 14:25"))
    assert out["result"] == "sent"
    assert out["sent"] == newest.guid
    (msg,) = w.sender.sent
    assert msg["condition"] == "'news_teste' in topics"  # dry-run nunca mira tópico real
    assert msg["data"]["category"] == "saude"
    assert status(w, newest.guid) == "sent"


# --- fluxo normal --------------------------------------------------------------------


def test_new_fresh_article_is_sent_and_recorded():
    now = at("2026-09-30 10:00")
    w = seeded_world(now)
    new = news_item(1, "policia", now - minutes(5))
    w.feed_items.append(new)
    out = run(w, now)
    assert out["result"] == "sent"
    assert status(w, guid_of(new)) == "sent"
    (send,) = w.store.sends_since(now - timedelta(days=1), "dry-run")
    assert send.slug == "policia" and not send.urgent
    assert w.log.events("would_send")[0]["guid"] == guid_of(new)
    assert w.pings[-1] == ""


def test_tv_items_are_never_sent():
    now = at("2026-09-30 10:00")
    w = seeded_world(now)
    tv = news_item(1, "jornal-do-estado", now - minutes(1), path="tv-atalaia", body=False)
    w.feed_items.append(tv)
    assert run(w, now)["result"] == "no_candidate"
    assert w.sender.sent == []
    assert w.store.get_item(guid_of(tv)).reason == "tv"


def test_only_one_send_per_run_highest_priority_first():
    now = at("2026-09-30 10:00")
    w = seeded_world(now)
    brasil = news_item(1, "brasil", now - minutes(1))
    sergipe = news_item(2, "sergipe", now - minutes(20))
    policia = news_item(3, "policia", now - minutes(2))
    w.feed_items += [brasil, sergipe, policia]
    run(w, now)
    assert [m["data"]["guid"] for m in w.sender.sent] == [guid_of(sergipe)]
    assert status(w, guid_of(policia)) == "pending"
    assert status(w, guid_of(brasil)) == "pending"


def test_pending_waits_for_gap_then_goes_or_expires():
    now = at("2026-09-30 10:00")
    w = seeded_world(now)
    gap = w.cfg.policy.min_gap_minutes
    a = news_item(1, "sergipe", now)
    b = news_item(2, "policia", now)
    c = news_item(3, "brasil", now)
    w.feed_items += [a, b, c]
    run(w, now)  # envia sergipe
    out = run(w, now + minutes(gap - 2))
    assert out["result"] == "min_gap"
    run(w, now + minutes(gap))  # envia policia
    assert status(w, guid_of(b)) == "sent"
    run(w, now + minutes(62))  # brasil já tem 62 min: expira
    assert status(w, guid_of(c)) == "expired"
    assert len(w.sender.sent) == 2


def test_quiet_hours_sends_nothing_and_items_expire():
    night = at("2026-09-30 23:00")
    w = seeded_world(night)
    it = news_item(1, "sergipe", night - minutes(1))
    w.feed_items.append(it)
    assert run(w, night)["result"] == "quiet_hours"
    assert status(w, guid_of(it)) == "pending"
    run(w, at("2026-10-01 07:00"))
    assert status(w, guid_of(it)) == "expired"
    assert w.sender.sent == []


def test_republished_title_is_skipped():
    now = at("2026-09-30 10:00")
    w = seeded_world(now)
    w.feed_items.append(news_item(1, "policia", now - minutes(1), title="Homem é preso em Aracaju"))
    run(w, now)
    w.feed_items.append(news_item(2, "sergipe", now + minutes(50), title="Homem é preso em Aracaju!"))
    run(w, now + minutes(55))
    assert w.store.get_item(guid_of(w.feed_items[-1])).reason == "duplicate_title"
    assert len(w.sender.sent) == 1


def test_same_title_twice_in_same_fetch_sends_once():
    now = at("2026-09-30 10:00")
    w = seeded_world(now)
    a = news_item(1, "policia", now, title="Mesmo título")
    b = news_item(2, "sergipe", now, title="Mesmo título")
    w.feed_items += [a, b]
    run(w, now)
    run(w, now + minutes(w.cfg.policy.min_gap_minutes))
    assert len(w.sender.sent) == 1
    assert {status(w, guid_of(a)), status(w, guid_of(b))} == {"sent", "skipped"}


def test_category_disabled_is_not_sent():
    now = at("2026-09-30 10:00")
    w = seeded_world(now, enabled=["sergipe"])
    it = news_item(1, "policia", now - minutes(1))
    w.feed_items.append(it)
    run(w, now)
    assert w.store.get_item(guid_of(it)).reason == "category_disabled"


def test_alias_publishes_on_canonical_topic():
    now = at("2026-09-30 10:00")
    w = seeded_world(now, mode="production")
    w.sender = fcm.DryRunSender(mode="production")  # captura sem chamar o FCM
    w.feed_items.append(news_item(1, "esporte", now - minutes(1)))
    run(w, now)
    (msg,) = w.sender.sent
    assert msg["condition"] == "'news_esportes' in topics"
    assert msg["data"]["category"] == "esportes"


def test_item_gone_from_feed_is_not_sent():
    now = at("2026-09-30 10:00")
    w = seeded_world(now)
    it = news_item(1, "policia", now - minutes(1))
    w.feed_items.append(it)
    w.store.record_send(guid="x", slug="brasil", urgent=False, sent_at=now - minutes(10), fcm_message_id=None, mode="dry-run")
    run(w, now)  # bloqueado pelo intervalo, fica pending
    w.feed_items.remove(it)
    run(w, now + minutes(w.cfg.policy.min_gap_minutes - 10))
    assert w.store.get_item(guid_of(it)).reason == "gone_from_feed"
    assert w.sender.sent == []


# --- erros ------------------------------------------------------------------------------


class ScriptedSender:
    mode = "test"

    def __init__(self, *results):
        self.results = list(results)
        self.sent = []

    def send(self, message):
        self.sent.append(message)
        return self.results.pop(0)


def test_temporary_error_keeps_pending_and_retries():
    now = at("2026-09-30 10:00")
    w = seeded_world(now, mode="test")
    w.sender = ScriptedSender(
        SendResult(ok=False, error=fcm.TEMPORARY, detail="UnavailableError"),
        SendResult(ok=True, message_id="m1"),
    )
    it = news_item(1, "policia", now - minutes(1))
    w.feed_items.append(it)
    assert run(w, now)["result"] == "send_retry_later"
    assert status(w, guid_of(it)) == "pending"
    assert run(w, now + minutes(2))["result"] == "sent"
    (s,) = w.store.sends_since(now - timedelta(days=1), "test")
    assert s.guid == guid_of(it)


def test_permanent_error_marks_failed_and_tries_next():
    now = at("2026-09-30 10:00")
    w = seeded_world(now, mode="test")
    w.sender = ScriptedSender(
        SendResult(ok=False, error=fcm.PERMANENT, detail="InvalidArgumentError: x"),
        SendResult(ok=True, message_id="m2"),
    )
    a = news_item(1, "sergipe", now - minutes(1))
    b = news_item(2, "policia", now - minutes(1))
    w.feed_items += [a, b]
    run(w, now)
    assert status(w, guid_of(a)) == "failed"
    assert status(w, guid_of(b)) == "sent"


def test_feed_failure_exits_quietly_and_alerts_after_threshold():
    now = at("2026-09-30 10:00")
    w = seeded_world(now)
    w.feed_error = feed.FeedError("timeout")
    for i in range(4):
        assert run(w, now + minutes(2 * i))["result"] == "feed_failed"
    assert "/fail" not in w.pings
    run(w, now + minutes(10))
    assert w.pings[-1] == "/fail"
    w.feed_error = None
    run(w, now + minutes(12))
    assert w.store.get_meta(FEED_FAILURES_KEY) == "0"
    assert w.pings[-1] == ""


# --- dia simulado -------------------------------------------------------------------------


def test_simulated_busy_day_respects_all_limits():
    """Uma matéria a cada 10 min, das 06:00 às 23:50, execução a cada 2 min."""
    start = at("2026-09-30 05:50")
    w = seeded_world(start)
    slugs = ["policia", "sergipe", "brasil", "saude", "politica", "esportes", "aracaju"]
    t = at("2026-09-30 06:00")
    n = 0
    end = at("2026-10-01 00:00")
    while t < end:
        if (t - at("2026-09-30 06:00")).total_seconds() % 600 == 0:
            n += 1
            w.feed_items.append(news_item(n, slugs[n % len(slugs)], t))
        run(w, t)
        t += minutes(2)

    sends = w.store.sends_since(at("2026-09-30 00:00"), "dry-run")
    cfg = w.cfg
    assert len(sends) == cfg.policy.max_per_day
    for a, b in zip(sends, sends[1:]):
        assert b.sent_at - a.sent_at >= minutes(cfg.policy.min_gap_minutes)
    for s in sends:
        local = s.sent_at.astimezone(cfg.tz).time()
        assert local.hour >= 7 and local.hour < 22
    # cotas: no máximo 3 antes das 12h e 6 antes das 18h; a noite também recebe
    hours = [s.sent_at.astimezone(cfg.tz).hour for s in sends]
    assert sum(h < 12 for h in hours) == 3
    assert sum(12 <= h < 18 for h in hours) == 3
    assert sum(h >= 18 for h in hours) == 2
    by_slug = {}
    for s in sends:
        by_slug.setdefault(s.slug, []).append(s.sent_at)
    for times in by_slug.values():
        assert len(times) <= cfg.policy.per_category_max_per_day
        for a, b in zip(times, times[1:]):
            assert b - a >= minutes(cfg.policy.per_category_min_gap_minutes)


# --- envio manual ---------------------------------------------------------------------------


def manual(w, now, url, urgent=False, force=False, confirm=lambda q: True):
    return send_manual(w.cfg, w.store, now, w.deps, url, urgent=urgent, force=force, confirm=confirm)


def test_manual_send_refused_before_seed():
    now = at("2026-09-30 10:00")
    w = world()
    it = news_item(1, "policia", now)
    w.feed_items.append(it)
    with pytest.raises(ManualSendError, match="seed"):
        manual(w, now, guid_of(it))


def test_manual_urgent_ignores_quiet_hours_but_respects_cap():
    night = at("2026-09-30 23:30")
    w = seeded_world(night, mode="production")
    w.sender = fcm.DryRunSender(mode="production")
    items = [news_item(i, "policia", night - timedelta(hours=3)) for i in (1, 2, 3)]
    w.feed_items += items
    manual(w, night, guid_of(items[0]), urgent=True)
    manual(w, night + minutes(1), guid_of(items[1]), urgent=True)
    with pytest.raises(ManualSendError, match="urgent_daily_cap"):
        manual(w, night + minutes(2), guid_of(items[2]), urgent=True, force=True)
    assert w.sender.sent[0]["condition"] == "'news_policia' in topics || 'news_urgente' in topics"
    assert len(w.sender.sent) == 2


def test_manual_urgent_in_test_mode_goes_only_to_news_teste():
    now = at("2026-09-30 10:00")
    w = seeded_world(now, mode="test")
    w.sender = fcm.DryRunSender(mode="test")
    it = news_item(1, "policia", now)
    w.feed_items.append(it)
    manual(w, now, guid_of(it), urgent=True)
    assert w.sender.sent[0]["condition"] == "'news_teste' in topics"


def test_manual_normal_respects_policy_unless_forced():
    night = at("2026-09-30 23:00")
    w = seeded_world(night)
    it = news_item(1, "policia", night)
    w.feed_items.append(it)
    with pytest.raises(ManualSendError, match="quiet_hours"):
        manual(w, night, guid_of(it))
    manual(w, night, guid_of(it), force=True)
    assert len(w.sender.sent) == 1


def test_manual_never_sends_tv_even_forced():
    now = at("2026-09-30 10:00")
    w = seeded_world(now)
    tv = news_item(1, "cidade-alerta", now, path="tv-atalaia", body=False)
    w.feed_items.append(tv)
    with pytest.raises(ManualSendError, match="noticias"):
        manual(w, now, guid_of(tv), urgent=True, force=True)


def test_manual_requires_article_in_feed():
    now = at("2026-09-30 10:00")
    w = seeded_world(now)
    with pytest.raises(ManualSendError, match="não está no RSS"):
        manual(w, now, "https://a8se.com/noticias/policia/2026/09/30/1-nao-existe")


def test_manual_production_asks_confirmation():
    now = at("2026-09-30 10:00")
    w = seeded_world(now, mode="production")
    w.sender = fcm.DryRunSender(mode="production")
    it = news_item(1, "policia", now)
    w.feed_items.append(it)
    asked = []
    with pytest.raises(ManualSendError, match="cancelado"):
        manual(w, now, guid_of(it), confirm=lambda q: asked.append(q) or False)
    assert asked and w.sender.sent == []


def test_manual_item_does_not_enter_automatic_queue_on_failure():
    now = at("2026-09-30 10:00")
    w = seeded_world(now, mode="test")
    w.sender = ScriptedSender(SendResult(ok=False, error=fcm.TEMPORARY, detail="x"))
    it = news_item(1, "policia", now)
    w.feed_items.append(it)
    with pytest.raises(ManualSendError):
        manual(w, now, guid_of(it))
    assert status(w, guid_of(it)) == "skipped"
