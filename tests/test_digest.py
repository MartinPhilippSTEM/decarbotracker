import pytest
from conftest import make_item, make_scored

from decarbotracker.digest import _enforce_mix, build_digest, fit_x, x_length
from decarbotracker.llm import UsageTracker
from decarbotracker.mailer import MailError, render_email, send_email
from decarbotracker.models import DigestDraft, DigestFinding
from decarbotracker.synthesis import synthesize


@pytest.fixture
def report(settings):
    sel = [make_scored(make_item(i, region="CZ" if i < 3 else "EU"), relevance=7) for i in range(12)]
    return synthesize("2026-W39", sel, 40, settings, None)


def test_x_length_counts_url_as_23():
    url = "https://martinphilippstem.github.io/decarbotracker/tydny/2026-w39/"
    assert x_length(f"Ahoj {url}") == 5 + 23
    long = "slovo " * 80 + url
    fitted = fit_x(long)
    assert x_length(fitted) <= 280 and fitted.endswith(url) and "…" in fitted


def test_deterministic_digest_has_cz_and_world(settings, report):
    d = build_digest(report, settings, None)
    assert len(d.findings) == 3
    geos = [f.geo for f in d.findings]
    assert "CZ" in geos and any(g != "CZ" for g in geos)
    assert geos[0] == "CZ"
    assert all(x_length(p) <= 280 for p in d.x_thread)
    assert any(d.report_url in p for p in d.x_thread)


def test_enforce_mix_adds_missing_world(report):
    cz_ids = [i.id for i in report.items if i.geo_focus == "CZ"]
    only_cz = [DigestFinding(item_id=i, geo="CZ", title_cs="T", text_cs="X") for i in cz_ids[:3]]
    notes = []
    out = _enforce_mix(only_cz, report, notes)
    assert len(out) == 3
    assert any(f.geo != "CZ" for f in out) and any(f.geo == "CZ" for f in out)
    assert any("ze světa" in n for n in notes)


def test_enforce_mix_drops_unknown_ids(report):
    notes = []
    out = _enforce_mix([DigestFinding(item_id="neexistuje", geo="CZ", title_cs="T", text_cs="X")], report, notes)
    assert len(out) == 3 and all(f.item_id != "neexistuje" for f in out)


class FakeClient:
    def __init__(self, settings, draft):
        self.tracker = UsageTracker(settings.llm)
        self.draft = draft

    def structured(self, **kw):
        return self.draft, None


def test_llm_digest_link_placeholder_replaced(settings, report):
    ids = [i.id for i in report.items]
    draft = DigestDraft(
        findings=[DigestFinding(item_id=ids[0], geo="CZ", title_cs="Česko", text_cs="A."),
                  DigestFinding(item_id=ids[5], geo="EU", title_cs="Evropa", text_cs="B."),
                  DigestFinding(item_id=ids[6], geo="EU", title_cs="Další", text_cs="C.")],
        x_thread=["🧵 Háček", "1/ jedna", "Celý přehled: {ODKAZ} #klima"])
    d = build_digest(report, settings, FakeClient(settings, draft))
    assert d.x_thread[-1].startswith("Celý přehled: https://") and "{ODKAZ}" not in "".join(d.x_thread)
    assert [f.title_cs for f in d.findings] == ["Česko", "Evropa", "Další"]


def test_render_email_contains_findings_and_thread(settings, report):
    d = build_digest(report, settings, None)
    subject, html, text = render_email(d)
    assert "2026-W39" in subject
    assert "Tři nejzajímavější zjištění" in html and "Návrh vlákna na X" in html
    assert d.findings[0].url in html and d.report_url in text


def test_send_email_requires_key_and_recipient(settings, report, monkeypatch):
    d = build_digest(report, settings, None)
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    with pytest.raises(MailError):
        send_email(d)
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.delenv("MAIL_TO", raising=False)
    with pytest.raises(MailError):
        send_email(d)


def test_cli_notify_dry_run(settings, report, tmp_path, capsys):
    from decarbotracker.cli import main
    from decarbotracker.storage import week_path, write_json

    write_json(week_path("2026-W39"), report)
    assert main(["notify", "--dry-run"]) == 0
    assert (tmp_path / "email-preview.html").exists()
    assert "NÁVRH VLÁKNA NA X" in capsys.readouterr().out
