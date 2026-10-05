from datetime import datetime, timezone

import pytest

from a8se_push import feed
from a8se_push.feed import KIND_NEWS, KIND_OTHER, KIND_TV


@pytest.fixture
def items(real_feed):
    return feed.parse(real_feed)


def by_id(items, numeric_id: str):
    return next(it for it in items if f"/{numeric_id}-" in it.url)


# --- feed real ---------------------------------------------------------------


def test_parses_all_items_and_guid_equals_link(items):
    assert len(items) == 200
    assert all(it.guid == it.url for it in items)


def test_tv_items_are_flagged_and_have_no_body(items):
    tv = [it for it in items if it.kind == KIND_TV]
    assert len(tv) == 123
    assert all("/tv-atalaia/" in it.url for it in tv)
    assert not any(it.has_body for it in tv)


def test_news_items_have_body_and_slug_from_url(items):
    news = [it for it in items if it.kind == KIND_NEWS]
    assert len(news) == 77
    assert all(it.has_body for it in news)
    policia = by_id(items, "244782")
    assert policia.raw_slug == "policia"
    assert policia.category_label == "Polícia"  # acento preservado no rótulo
    saude = by_id(items, "244784")
    assert saude.raw_slug == "saude"
    assert saude.title == "Ministério da Saúde libera doação de sangue para pessoas com mais de 70 anos"
    assert saude.description == "A pasta também diminuiu o intervalo de tempo para doar em algumas situações"


def test_pubdate_is_converted_to_utc(items):
    saude = by_id(items, "244784")  # Wed, 30 Sep 2026 14:20:00 -0300
    assert saude.published_at == datetime(2026, 9, 30, 17, 20, tzinfo=timezone.utc)


def test_double_encoded_entities_are_fully_decoded(items):
    tv = by_id(items, "244638")  # <title>Colabore com a &amp;apos;Festa das Crianças&amp;apos; ...
    assert tv.title == "Colabore com a 'Festa das Crianças' fazendo doações"


def test_avif_image_is_dropped(items):
    esporte = by_id(items, "244759")  # media:content .avif
    assert esporte.image is None
    assert all(not (it.image or "").lower().endswith(".avif") for it in items)


def test_webp_and_jpeg_images_are_kept(items):
    assert by_id(items, "244784").image.endswith(".webp")


def test_real_esporte_slug_maps_to_esportes_by_default(items):
    esporte = by_id(items, "244759")
    assert esporte.raw_slug == "esporte"
    assert feed.canonical_slug(esporte.raw_slug, feed.DEFAULT_ALIASES) == "esportes"


# --- funções auxiliares --------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("&amp;apos;x&amp;apos;", "'x'"),
        ("A &amp;amp; B", "A & B"),
        ("&amp;amp;amp;quot;", '"'),
        ("<p>Olá <b>mundo</b></p>", "Olá mundo"),
        ("  muito   espaço \n aqui ", "muito espaço aqui"),
        (None, ""),
    ],
)
def test_clean_text(raw, expected):
    assert feed.clean_text(raw) == expected


@pytest.mark.parametrize(
    "label, slug",
    [("Polícia", "policia"), ("Eleições 2026", "eleicoes-2026"), ("Saúde & Bem-estar", "saude-bem-estar"),
     ("Jornal do Estado", "jornal-do-estado")],
)
def test_slugify_matches_app_rule(label, slug):
    assert feed.slugify(label) == slug


def test_topic_safe_replaces_invalid_chars():
    assert feed.topic_safe("news_saúde bem") == "news_sa-de-bem"
    assert feed.topic_safe("news_a-b_c.d~e%f") == "news_a-b_c.d~e%f"


def test_url_outside_noticias_uses_category_label(tmp_path):
    xml = (
        b'<rss xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><item>'
        b"<title>T</title><link>https://a8se.com/especiais/abc/1-x</link>"
        b"<category>Elei\xc3\xa7\xc3\xb5es</category>"
        b"<content:encoded><![CDATA[<p>x</p>]]></content:encoded></item></channel></rss>"
    )
    (it,) = feed.parse(xml)
    assert it.kind == KIND_OTHER
    assert it.raw_slug == "eleicoes"
    assert it.guid == it.url  # sem <guid>, usa o link


def test_parse_aliases_from_remote_config():
    value = '[{"slug": "esportes", "aliases": ["esporte", "futebol"]}, {"slug": "policia"}]'
    assert feed.parse_aliases(value) == {"esporte": "esportes", "futebol": "esportes"}


def test_parse_aliases_unpublished_uses_app_default():
    assert feed.parse_aliases(None) == {"esporte": "esportes"}


def test_parse_aliases_invalid_json_raises():
    with pytest.raises(ValueError):
        feed.parse_aliases("{nope")


def test_normalize_title_ignores_case_accents_and_punctuation():
    a = feed.normalize_title("Três celulares roubados são recuperados, em Aracaju!")
    b = feed.normalize_title("TRES CELULARES ROUBADOS SAO RECUPERADOS EM ARACAJU")
    assert a == b == "tres celulares roubados sao recuperados em aracaju"


@pytest.mark.parametrize(
    "url, ok",
    [
        ("https://a8se.com/noticias/x", True),
        ("https://www.a8se.com/noticias/x", True),
        ("http://a8se.com/noticias/x", False),
        ("https://a8se.com.evil.com/noticias/x", False),
        ("https://outro.com/noticias/x", False),
    ],
)
def test_is_a8se_https(url, ok):
    assert feed.is_a8se_https(url) is ok


def test_invalid_xml_raises_feed_error():
    with pytest.raises(feed.FeedError):
        feed.parse(b"<html>erro 502</html")


def test_bad_pubdate_is_none():
    assert feed.parse_pubdate("ontem") is None
    assert feed.parse_pubdate(None) is None
