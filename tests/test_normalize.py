import time
from datetime import UTC, datetime, timedelta, timezone

from conftest import NOW, make_source

from decarbotracker.normalize import (
    build_item,
    clean_text,
    from_struct_time,
    make_id,
    normalize_doi,
    normalize_url,
    parse_date,
    to_utc,
)


def test_normalize_url_strips_tracking_fragment_slash_and_scheme():
    a = normalize_url("http://Example.org/clanek/?utm_source=rss&utm_medium=feed&id=5#komentare")
    b = normalize_url("https://example.org/clanek?id=5")
    assert a == b == "https://example.org/clanek?id=5"
    assert normalize_url("https://example.org/") == "https://example.org"
    assert make_id("http://example.org/x/") == make_id("https://example.org/x?utm_campaign=y")


def test_normalize_doi():
    assert normalize_doi("https://doi.org/10.1016/J.ERSS.2026.1") == "10.1016/j.erss.2026.1"
    assert normalize_doi("doi:10.1/X") == "10.1/x"
    assert normalize_doi(None) is None


def test_struct_time_to_utc():
    t = time.strptime("2026-09-23 10:30:00", "%Y-%m-%d %H:%M:%S")
    dt = from_struct_time(t)
    assert dt == datetime(2026, 9, 23, 10, 30, tzinfo=UTC)
    assert from_struct_time(None) is None


def test_naive_becomes_utc_and_aware_converted():
    naive = datetime(2026, 9, 23, 10, 0)
    assert to_utc(naive).tzinfo == UTC
    prague = datetime(2026, 9, 23, 12, 0, tzinfo=timezone(timedelta(hours=2)))
    assert to_utc(prague) == datetime(2026, 9, 23, 10, 0, tzinfo=UTC)
    # aware a naive jsou po normalizaci porovnatelné
    assert to_utc(naive) < to_utc(prague) or to_utc(naive) == to_utc(prague)


def test_parse_date_formats():
    assert parse_date("Wed, 23 Sep 2026 10:00:00 +0200") == datetime(2026, 9, 23, 8, 0, tzinfo=UTC)
    assert parse_date("2026-09-23") == datetime(2026, 9, 23, tzinfo=UTC)
    assert parse_date("30. září 2026") == datetime(2026, 9, 30, tzinfo=UTC)
    assert parse_date("19. září 2025").month == 9
    assert parse_date(" 27. 7. 2026") == datetime(2026, 7, 27, tzinfo=UTC)
    assert parse_date("22 września 2026") == datetime(2026, 9, 22, tzinfo=UTC)
    assert parse_date("Fuel report — 11 September 2026") == datetime(2026, 9, 11, tzinfo=UTC)
    assert parse_date("") is None
    assert parse_date(None) is None
    assert parse_date("bez data") is None


def test_missing_date_uses_first_seen():
    it = build_item(make_source(), title="Titulek", url="https://example.org/1", published=None, now=NOW)
    assert it.published_at is None
    assert it.date_is_first_seen
    assert it.effective_date == NOW


def test_future_date_clamped():
    it = build_item(make_source(), title="T", url="https://example.org/2",
                    published=NOW + timedelta(days=3), now=NOW)
    assert it.published_at == NOW


def test_clean_text_strips_html_and_truncates():
    raw = "<p>Ahoj&nbsp;<b>světe</b></p>" + " slovo" * 600
    out = clean_text(raw, limit=100)
    assert out.startswith("Ahoj světe")
    assert len(out) <= 101 and out.endswith("…")
    assert clean_text("Text. The post Foo appeared first on Bar.") == "Text."


def test_build_item_rejects_missing_title_or_url():
    assert build_item(make_source(), title="", url="https://x.org", published=None) is None
    assert build_item(make_source(), title="T", url="/relative", published=None) is None
