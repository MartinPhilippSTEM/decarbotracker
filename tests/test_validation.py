import csv

from decarbotracker.storage import write_json
from decarbotracker.validation import agreement, cohen_kappa, export_sample


def test_cohen_kappa_known_values():
    assert cohen_kappa(["1", "0", "1", "0"], ["1", "0", "1", "0"]) == 1.0
    # shoda v 8 z 10 (po = 0,8), náhodná shoda pe = 0,5 → kappa 0,6
    a = ["1"] * 5 + ["0"] * 5
    b = ["1", "1", "1", "1", "0", "0", "0", "0", "1", "0"]
    assert cohen_kappa(a, b) == 0.6
    assert cohen_kappa(["", ""], ["1", "0"]) is None


def _fake_scores(tmp_path):
    rows = [{"id": f"id{i}", "title": f"Titulek {i}", "url": f"https://x/{i}", "source_id": "s",
             "source_type": "media", "has_summary": True, "scored_by": "llm", "final_score": 10 - i,
             "selected": i < 10, "score": {"relevance": 8 if i < 10 else 2, "geo_focus": "CZ" if i % 2 else "EU"}}
            for i in range(40)]
    write_json(tmp_path / "data" / "scores" / "2026-W48.json",
               {"week": "2026-W48", "model_scoring": "claude-haiku-5-5", "prompt_scoring": "2.0", "items": rows})
    write_json(tmp_path / "data" / "items" / "2026-W48.json",
               {"items": [{"id": r["id"], "summary_raw": "Výtah"} for r in rows]})


def test_export_is_stratified_and_hides_ai_scores(tmp_path):
    _fake_scores(tmp_path)
    out = export_sample("2026-W48", 20, tmp_path / "v.csv")
    with open(out, encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh, delimiter=";"))
    assert len(rows) == 20
    assert "relevance" not in rows[0] and rows[0]["A_relevantni"] == ""
    assert sum(1 for r in rows if int(r["id"][2:]) < 10) == 10  # polovina vybraných
    assert (tmp_path / "v_ai.csv").exists()


def test_agreement_between_coders_and_ai(tmp_path):
    _fake_scores(tmp_path)
    out = export_sample("2026-W48", 20, tmp_path / "v.csv")
    with open(out, encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh, delimiter=";"))
    for r in rows:
        rel = "ano" if int(r["id"][2:]) < 10 else "ne"
        geo = "CZ" if int(r["id"][2:]) % 2 else "EU"
        r.update(A_relevantni=rel, B_relevantni=rel, A_geo=geo, B_geo=geo)
    with open(out, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]), delimiter=";")
        w.writeheader()
        w.writerows(rows)
    res = agreement(out)
    assert res["n"] == 20
    assert res["kappa_relevance_A_B"] == 1.0 and res["kappa_relevance_A_AI"] == 1.0 and res["kappa_geo_B_AI"] == 1.0
