"""Integrační test: run --dry-run nad fixtures vyprodukuje kompletní web bez výjimek (bez sítě)."""

from datetime import UTC, datetime

from conftest import FIXTURES, make_source, read_fixture

from decarbotracker.cli import main
from decarbotracker.config import load_sources
from decarbotracker.fetch.feeds import parse_feed_bytes, parse_wp_json_bytes
from decarbotracker.fetch.scrape import parse_listing
from decarbotracker.models import WeeklyReport
from decarbotracker.pipeline import run_pipeline
from decarbotracker.render import build_site
from decarbotracker.sources import SourceHealth
from decarbotracker.storage import read_json, seen_path, week_path

NOW = datetime(2026, 9, 30, 8, 0, tzinfo=UTC)
WEEK = "2026-W39"


def fixture_collector(settings, now):
    items = []
    items += parse_feed_bytes(read_fixture("feeds", "rss2_stem.xml"),
                              make_source(id="stem", name="STEM", region="CZ", source_type="polling", language="cs"), now=now)
    items += parse_feed_bytes(read_fixture("feeds", "atom_faktaoklimatu.xml"),
                              make_source(id="fok", name="Fakta o klimatu", region="CZ", topic_filter=False), now=now)
    items += parse_feed_bytes(read_fixture("feeds", "rdf_nclimate.xml"),
                              make_source(id="ncc", name="Nature Climate Change", region="GLOBAL",
                                          source_type="journal", topic_filter=False), now=now)
    items += parse_wp_json_bytes(read_fixture("feeds", "wp_rmi.json"),
                                 make_source(id="rmi", name="RMI", region="US", topic_filter=False), now=now)
    for s in load_sources():
        f = FIXTURES / "html" / f"{s.id}.html"
        if s.type == "scrape" and f.exists():
            items += parse_listing(f.read_bytes(), s, now=now)
    healths = [SourceHealth(source_id="stem", name="STEM", type="rss", url="https://www.stem.cz/feed/", region="CZ",
                            status="ok", count=3, checked_at=now)]
    return items, healths


def test_dry_run_pipeline_produces_complete_site(settings, tmp_path):
    report = run_pipeline(WEEK, settings, dry_run=True, now=NOW, collector=fixture_collector)
    assert report.status == "dry_run"
    assert report.items_selected > 0
    saved = WeeklyReport.model_validate(read_json(week_path(WEEK)))
    valid = {i.id for i in saved.items}
    assert saved.swot is not None
    for q in ("strengths", "weaknesses", "opportunities", "threats"):
        for p in getattr(saved.swot, q):
            assert p.evidence_item_ids and set(p.evidence_item_ids) <= valid
    assert all(t.item_id in valid for t in saved.top_items)
    # dry-run nemění seen.json
    assert not seen_path().exists()
    out = build_site(settings, out_dir=tmp_path / "site")
    assert (out / "index.html").stat().st_size > 5000
    assert (out / "tydny" / "2026-w39" / "index.html").exists()


def test_rerun_same_week_same_input(settings):
    r1 = run_pipeline(WEEK, settings, dry_run=True, now=NOW, collector=fixture_collector)
    r2 = run_pipeline(WEEK, settings, dry_run=True, now=NOW, collector=fixture_collector)
    assert [i.id for i in r1.items] == [i.id for i in r2.items]


def test_run_without_api_key_publishes_fallback(settings):
    rep = run_pipeline(WEEK, settings, dry_run=False, now=NOW, collector=fixture_collector)
    assert rep.status == "fallback"
    assert rep.top_items
    assert seen_path().exists()


def test_failing_collector_source_does_not_crash(settings):
    def broken(settings, now):
        items, health = fixture_collector(settings, now)
        health.append(SourceHealth(source_id="x", name="X", type="rss", url="https://x", region="EU",
                                   status="http_error", detail="HTTP 500", checked_at=now))
        return items, health

    assert run_pipeline(WEEK, settings, dry_run=True, now=NOW, collector=broken).status == "dry_run"


def test_cli_build_command(settings, tmp_path):
    run_pipeline(WEEK, settings, dry_run=True, now=NOW, collector=fixture_collector)
    assert main(["build", "--base-url", "/decarbotracker/"]) == 0
    html = (tmp_path / "site" / "index.html").read_text(encoding="utf-8")
    assert "/decarbotracker/static/style.css" in html


def test_run_skip_if_done(settings, capsys):
    from decarbotracker.storage import week_path, write_json

    write_json(week_path("2026-W39"), {"week": "2026-W39", "status": "ok"})
    assert main(["run", "--week", "2026-W39", "--skip-if-done", "--no-deploy"]) == 0
    assert "už je hotový" in capsys.readouterr().out
