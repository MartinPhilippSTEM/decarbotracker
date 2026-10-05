from conftest import make_item, make_scored

from decarbotracker.render import build_site
from decarbotracker.sentiment import label_for, summarize
from decarbotracker.storage import week_path, write_json
from decarbotracker.synthesis import synthesize


def _rows(values, region="CZ", scored_by="llm"):
    return [make_scored(make_item(i + (0 if region == "CZ" else 100), region=region), relevance=7, final=10.0,
                        sentiment=v, scored_by=scored_by) for i, v in enumerate(values)]


def test_too_few_items_gives_no_estimate():
    assert summarize(_rows([1, -1, 0]), set()) is None


def test_heuristic_scores_are_ignored():
    assert summarize(_rows([2] * 20, scored_by="heuristic"), set()) is None


def test_index_scale_and_shares():
    rows = _rows([2] * 4 + [-2] * 4 + [0] * 2)
    s = summarize(rows, set())
    assert s.index == 0 and s.label_cs == "přibližně vyrovnaná"
    assert abs(s.negative - 0.4) < 0.01 and abs(s.positive - 0.4) < 0.01
    s2 = summarize(_rows([2] * 10), set())
    assert s2.index == 100 and s2.label_cs == "spíše příznivá"


def test_weights_and_regions():
    rows = _rows([-1] * 6, "CZ") + _rows([1, 1], "EU")
    rows[0].final_score = 100.0  # důležitá zpráva váží víc
    s = summarize(rows, {rows[0].item.id, rows[-1].item.id})
    assert s.index == -38  # (6×(−1)·váhy 150 + 2×(+1)·20) / 170 → −0,76 → −38
    regs = {r.region: r for r in s.by_region}
    assert regs["CZ"].n == 6 and regs["EU"].n == 2
    assert [r.region for r in s.by_region] == ["CZ", "EU"]
    assert s.most_negative_id == rows[0].item.id and s.most_positive_id == rows[-1].item.id


def test_labels():
    assert label_for(-50) == "spíše nepříznivá" and label_for(-20) == "mírně nepříznivá"
    assert label_for(5) == "přibližně vyrovnaná" and label_for(25) == "mírně příznivá"


def test_mood_section_rendered_with_soft_wording(settings, tmp_path):
    sel = [make_scored(make_item(i, region="CZ" if i < 4 else "EU"), relevance=7, sentiment=(-1 if i % 3 else 1))
           for i in range(14)]
    rep = synthesize("2026-W39", sel, 40, settings, None)
    rep.sentiment = summarize(sel, {i.id for i in rep.items})
    write_json(week_path("2026-W39"), rep)
    html = (build_site(settings, out_dir=tmp_path / "site") / "index.html").read_text(encoding="utf-8")
    assert "Přibližná nálada diskurzu" in html and "(odhad)" in html
    assert "Orientační odhad" in html and "přibližný odhad z hodnocení AI" in html
    assert html.index('id="nalada"') < html.index('id="prehled"')
