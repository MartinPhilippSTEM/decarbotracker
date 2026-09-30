import json

import pytest
from conftest import NOW, make_source, read_fixture

from decarbotracker.fetch.feeds import (
    ParseError,
    detect_html_instead_of_feed,
    parse_feed_bytes,
    parse_wp_json_bytes,
)


def test_rss2_real_fixture():
    items = parse_feed_bytes(read_fixture("feeds", "rss2_stem.xml"), make_source(region="CZ", language="cs"), now=NOW)
    assert len(items) == 3
    for it in items:
        assert it.title and it.url.startswith("https://")
        assert it.published_at is not None and it.published_at.tzinfo is not None
        assert it.published_at.utcoffset().total_seconds() == 0
        assert len(it.summary_raw) <= 1501
        assert "<" not in it.summary_raw


def test_atom_real_fixture():
    items = parse_feed_bytes(read_fixture("feeds", "atom_faktaoklimatu.xml"), make_source(), now=NOW)
    assert len(items) == 3
    assert all(i.published_at for i in items)


def test_rdf_rss10_real_fixture():
    items = parse_feed_bytes(read_fixture("feeds", "rdf_nclimate.xml"), make_source(source_type="journal"), now=NOW)
    assert len(items) == 3
    assert all(i.url.startswith("https://www.nature.com/") for i in items)
    assert all(i.published_at for i in items)


def test_wp_json_custom_shape_rmi():
    items = parse_wp_json_bytes(read_fixture("feeds", "wp_rmi.json"), make_source(type="wp_json"), now=NOW)
    assert len(items) == 3
    assert items[0].title == "Different Minerals, Different Strategies"
    assert items[0].published_at.year == 2026
    assert items[0].authors == ["Laurie Stone"]


def test_wp_json_standard_shape():
    data = [{
        "id": 1, "date": "2026-09-24T10:00:00", "date_gmt": "2026-09-24T08:00:00",
        "link": "https://example.org/post-1/?utm_source=x",
        "title": {"rendered": "Nová studie o ETS2 &amp; domácnostech"},
        "excerpt": {"rendered": "<p>Krátký <b>výtah</b>.</p>"},
    }]
    items = parse_wp_json_bytes(json.dumps(data).encode(), make_source(type="wp_json"), now=NOW)
    assert items[0].title == "Nová studie o ETS2 & domácnostech"
    assert items[0].summary_raw == "Krátký výtah ."
    assert items[0].published_at.hour == 8


def test_wp_json_error_object():
    with pytest.raises(ParseError):
        parse_wp_json_bytes(b'{"code": "rest_no_route", "message": "x"}', make_source(type="wp_json"))


CLOUDFLARE = b"""<!DOCTYPE html><html lang="en-US"><head><title>Just a moment...</title>
<meta http-equiv="refresh" content="390"></head><body><div id="cf-browser-verification"></div></body></html>"""
PLAIN_HTML = b"<!doctype html><html><head><title>Blog</title></head><body><p>Hi</p></body></html>"


def test_detect_cloudflare():
    assert detect_html_instead_of_feed(CLOUDFLARE, "text/html") == "cloudflare"
    with pytest.raises(ParseError) as exc:
        parse_feed_bytes(CLOUDFLARE, make_source())
    assert exc.value.kind == "cloudflare"


def test_detect_html_instead_of_feed():
    assert detect_html_instead_of_feed(PLAIN_HTML, "text/html; charset=utf-8") == "html_instead_of_feed"
    with pytest.raises(ParseError) as exc:
        parse_feed_bytes(PLAIN_HTML, make_source(), content_type="text/html")
    assert exc.value.kind == "html_instead_of_feed"


def test_real_feed_not_flagged_as_html():
    assert detect_html_instead_of_feed(read_fixture("feeds", "rss2_stem.xml"), "application/rss+xml") is None


def test_bozo_fatal_only_without_items():
    broken_with_items = """<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>
    <item><title>Klima &amp; emise</title><link>https://example.org/1</link><pubDate>Wed, 23 Sep 2026 10:00:00 GMT</pubDate></item>
    <item><title>Neuzavřený & znak</title><link>https://example.org/2</link></item></channel></rss>""".encode()
    items = parse_feed_bytes(broken_with_items, make_source(), now=NOW)
    assert len(items) >= 1
    with pytest.raises(ParseError):
        parse_feed_bytes(b"\x00\x01 garbage, not xml at all {", make_source())


def test_empty_valid_feed_returns_empty_list():
    empty = b'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title></channel></rss>'
    assert parse_feed_bytes(empty, make_source()) == []
