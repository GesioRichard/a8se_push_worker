from datetime import timedelta

import pytest

from a8se_push import fcm, feed
from a8se_push.fcm import DryRunSender, FcmSender, SafetyError, build_message

from .conftest import at, news_item, rss

NOW = at("2026-09-30 10:00")


def item(**kw):
    kw.setdefault("description", "Aparelhos foram localizados em uma residência no bairro São Conrado")
    (it,) = feed.parse(rss([news_item(1, "policia", NOW, title="Três celulares roubados são recuperados em Aracaju", **kw)]))
    return it


def test_message_matches_spec_4_2_exactly():
    it = item()
    msg = build_message(it, "policia", urgent=False, mode="production", now=NOW, ttl_hours=4)
    assert msg == {
        "condition": "'news_policia' in topics",
        "notification": {
            "title": "Três celulares roubados são recuperados em Aracaju",
            "body": "Aparelhos foram localizados em uma residência no bairro São Conrado",
            "image": it.image,
        },
        "data": {"type": "news", "guid": it.guid, "url": it.url, "category": "policia"},
        "android": {
            "priority": "high",
            "ttl": "14400s",
            "notification": {"channel_id": "news", "tag": "news_policia"},
        },
        "apns": {
            "headers": {
                "apns-collapse-id": "news_policia",
                "apns-expiration": str(int((NOW + timedelta(hours=4)).timestamp())),
            },
            "payload": {"aps": {"mutable-content": 1, "sound": "default"}},
        },
    }


def test_data_values_are_all_strings():
    msg = build_message(item(), "policia", urgent=True, mode="production", now=NOW, ttl_hours=4)
    assert all(isinstance(v, str) for v in msg["data"].values())
    assert msg["data"]["url"].startswith("https://a8se.com/")


def test_urgent_uses_two_topics_in_production():
    msg = build_message(item(), "policia", urgent=True, mode="production", now=NOW, ttl_hours=4)
    assert msg["condition"] == "'news_policia' in topics || 'news_urgente' in topics"
    assert msg["android"]["notification"]["tag"] == "news_policia"


@pytest.mark.parametrize("mode", ["test", "dry-run"])
@pytest.mark.parametrize("urgent", [False, True])
def test_non_production_modes_only_target_news_teste(mode, urgent):
    msg = build_message(item(), "policia", urgent=urgent, mode=mode, now=NOW, ttl_hours=4)
    assert msg["condition"] == "'news_teste' in topics"
    # tag continua sendo a editoria: o teste valida a substituição na bandeja
    assert msg["android"]["notification"]["tag"] == "news_policia"


def test_no_image_key_when_avif():
    it = item(image="https://img.dhost.cloud/x/https://cdn.a8se.com/images/a/b.avif")
    msg = build_message(it, "policia", urgent=False, mode="production", now=NOW, ttl_hours=4)
    assert "image" not in msg["notification"]


def test_ttl_from_config():
    msg = build_message(item(), "policia", urgent=False, mode="production", now=NOW, ttl_hours=2)
    assert msg["android"]["ttl"] == "7200s"


def test_topic_name_sanitized():
    msg = build_message(item(), "saúde", urgent=False, mode="production", now=NOW, ttl_hours=4)
    assert msg["condition"] == "'news_sa-de' in topics"


# --- corpo -----------------------------------------------------------------------------


def test_truncate_keeps_short_text():
    assert fcm.truncate("curto") == "curto"


def test_truncate_cuts_on_word_boundary_with_ellipsis():
    text = ("palavra " * 40).strip()
    out = fcm.truncate(text)
    assert len(out) <= 160
    assert out.endswith("…")
    assert out[:-1].split(" ")[-1] == "palavra"  # sem palavra cortada ao meio


def test_truncate_exact_limit_is_untouched():
    text = "a" * 160
    assert fcm.truncate(text) == text


# --- senders / modos ---------------------------------------------------------------------


def test_dry_run_sender_never_touches_firebase(monkeypatch):
    import firebase_admin.messaging as messaging

    def boom(*a, **k):
        raise AssertionError("dry-run chamou o FCM")

    monkeypatch.setattr(messaging, "send", boom)
    sender = fcm.make_sender("dry-run", "app-radios-d4371")
    assert isinstance(sender, DryRunSender)
    res = sender.send(build_message(item(), "policia", urgent=False, mode="dry-run", now=NOW, ttl_hours=4))
    assert res.ok and res.message_id is None


def test_fcm_sender_refuses_dry_run_mode():
    with pytest.raises(SafetyError):
        FcmSender("dry-run", "app-radios-d4371")


def test_test_mode_refuses_real_topics(monkeypatch):
    import firebase_admin.messaging as messaging

    monkeypatch.setattr(messaging, "send", lambda *a, **k: pytest.fail("não devia enviar"))
    sender = FcmSender("test", "app-radios-d4371", app=object())
    prod_msg = build_message(item(), "policia", urgent=True, mode="production", now=NOW, ttl_hours=4)
    with pytest.raises(SafetyError):
        sender.send(prod_msg)


def test_test_mode_sends_to_news_teste(monkeypatch):
    import firebase_admin.messaging as messaging

    captured = {}

    def fake_send(message, app=None):
        captured["message"] = message
        return "projects/app-radios-d4371/messages/123"

    monkeypatch.setattr(messaging, "send", fake_send)
    sender = FcmSender("test", "app-radios-d4371", app=object())
    res = sender.send(build_message(item(), "policia", urgent=False, mode="test", now=NOW, ttl_hours=4))
    assert res.ok and res.message_id.endswith("/123")
    m = captured["message"]
    assert m.condition == "'news_teste' in topics"
    assert m.topic is None and m.token is None
    assert m.android.notification.channel_id == "news"
    assert m.android.notification.tag == "news_policia"
    assert m.android.ttl == timedelta(hours=4)
    assert m.apns.headers["apns-collapse-id"] == "news_policia"
    assert m.apns.payload.aps.mutable_content is True
    assert m.data["type"] == "news"


def test_admin_message_encodes_like_spec():
    """O encoder do Admin SDK gera o JSON da API v1 igual ao da SPEC."""
    from firebase_admin import _messaging_encoder

    msg = build_message(item(), "policia", urgent=False, mode="production", now=NOW, ttl_hours=4)
    encoded = _messaging_encoder.MessageEncoder().default(fcm.to_admin_message(msg))
    assert encoded["condition"] == msg["condition"]
    assert encoded["data"] == msg["data"]
    assert encoded["android"]["ttl"] == "14400s"
    assert encoded["android"]["priority"] == "high"
    assert encoded["android"]["notification"] == {"channel_id": "news", "tag": "news_policia"}
    assert encoded["apns"]["payload"]["aps"] == {"mutable-content": 1, "sound": "default"}
    assert encoded["notification"]["image"] == msg["notification"]["image"]


@pytest.mark.parametrize(
    "exc_name, kind",
    [
        ("InvalidArgumentError", fcm.PERMANENT),
        ("UnavailableError", fcm.TEMPORARY),
        ("InternalError", fcm.TEMPORARY),
        ("DeadlineExceededError", fcm.TEMPORARY),
    ],
)
def test_error_classification(exc_name, kind):
    from firebase_admin import exceptions as fx

    exc = getattr(fx, exc_name)("x")
    assert fcm.classify_error(exc) == kind


def test_error_classification_generic_network_error_is_temporary():
    assert fcm.classify_error(TimeoutError("t")) == fcm.TEMPORARY
