"""Lidská validace hodnocení AI: vzorek k nezávislému kódování dvěma lidmi a výpočet shody (Cohenovo kappa).

Postup: `export-validation --week 2026-W48 --n 100` vytvoří CSV (vybrané i nevybrané položky, bez skóre AI),
dva kodéři nezávisle vyplní sloupce A_relevantni / B_relevantni (ano/ne) a A_geo / B_geo (CZ/EU/US/GLOBAL),
pak `validation-agreement soubor.csv` spočítá shodu mezi kodéry a mezi kodéry a AI.
"""

from __future__ import annotations

import csv
import random
from collections import Counter
from pathlib import Path

from decarbotracker.config import data_dir
from decarbotracker.storage import read_json

RELEVANT_FROM = 5  # relevance AI ≥ 5 = „relevantní“ (pro srovnání s binárním lidským kódováním)
CODER_COLUMNS = ["A_relevantni", "A_geo", "B_relevantni", "B_geo"]


def export_sample(week: str, n: int, out: Path, seed: int = 42) -> Path:
    """Stratifikovaný náhodný vzorek: polovina z vybraných, polovina z nevybraných kandidátů (bez skóre AI).
    Skóre AI se uloží do samostatného souboru *_ai.csv, aby kodéři nebyli ovlivněni."""
    data = read_json(data_dir() / "scores" / f"{week}.json", default=None)
    if not data:
        raise FileNotFoundError(f"Pro týden {week} nejsou uložená skóre (data/scores/{week}.json).")
    items = {d["id"]: d for d in (read_json(data_dir() / "items" / f"{week}.json", default={}) or {}).get("items", [])}
    rows = data["items"]
    rng = random.Random(seed)
    sel = [r for r in rows if r["selected"]]
    rest = [r for r in rows if not r["selected"]]
    k_sel = min(len(sel), n // 2)
    sample = rng.sample(sel, k_sel) + rng.sample(rest, min(len(rest), n - k_sel))
    rng.shuffle(sample)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["id", "titulek", "zdroj", "odkaz", "vytah", *CODER_COLUMNS])
        for r in sample:
            w.writerow([r["id"], r["title"], r["source_id"], r["url"],
                        (items.get(r["id"], {}).get("summary_raw") or "")[:500], "", "", "", ""])
    ai = out.with_name(out.stem + "_ai.csv")
    with open(ai, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["id", "relevance", "geo", "vybrano", "model", "verze_promptu"])
        for r in sample:
            w.writerow([r["id"], r["score"]["relevance"], r["score"]["geo_focus"], int(r["selected"]),
                        data.get("model_scoring", ""), data.get("prompt_scoring", "")])
    return out


def cohen_kappa(a: list[str], b: list[str]) -> float | None:
    """Cohenovo kappa pro dvě stejně dlouhé řady kategorií; None, když nelze spočítat."""
    pairs = [(x, y) for x, y in zip(a, b, strict=True) if x and y]
    if not pairs:
        return None
    n = len(pairs)
    po = sum(1 for x, y in pairs if x == y) / n
    ca, cb = Counter(x for x, _ in pairs), Counter(y for _, y in pairs)
    pe = sum(ca[k] * cb.get(k, 0) for k in ca) / (n * n)
    if pe >= 1:
        return 1.0 if po == 1 else None
    return round((po - pe) / (1 - pe), 3)


def _yes(v: str) -> str:
    v = (v or "").strip().lower()
    return {"ano": "1", "a": "1", "1": "1", "yes": "1", "ne": "0", "n": "0", "0": "0", "no": "0"}.get(v, "")


def agreement(path: Path) -> dict[str, float | int | None]:
    """Shoda kodérů A–B a kodérů s AI (relevance zjednodušená na relevantní/nerelevantní, geo)."""
    with open(path, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh, delimiter=";"))
    ai_path = path.with_name(path.stem + "_ai.csv")
    ai: dict[str, dict[str, str]] = {}
    if ai_path.exists():
        with open(ai_path, encoding="utf-8-sig", newline="") as fh:
            ai = {r["id"]: r for r in csv.DictReader(fh, delimiter=";")}
    ra, rb = [_yes(r.get("A_relevantni", "")) for r in rows], [_yes(r.get("B_relevantni", "")) for r in rows]
    ga = [(r.get("A_geo") or "").strip().upper() for r in rows]
    gb = [(r.get("B_geo") or "").strip().upper() for r in rows]
    r_ai = ["1" if int(ai[r["id"]]["relevance"]) >= RELEVANT_FROM else "0" if r["id"] in ai else "" for r in rows]
    g_ai = [ai[r["id"]]["geo"] if r["id"] in ai else "" for r in rows]
    return {
        "n": len(rows),
        "kappa_relevance_A_B": cohen_kappa(ra, rb),
        "kappa_geo_A_B": cohen_kappa(ga, gb),
        "kappa_relevance_A_AI": cohen_kappa(ra, r_ai),
        "kappa_relevance_B_AI": cohen_kappa(rb, r_ai),
        "kappa_geo_A_AI": cohen_kappa(ga, g_ai),
        "kappa_geo_B_AI": cohen_kappa(gb, g_ai),
    }
