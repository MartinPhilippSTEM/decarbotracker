# decarbotracker

Týdenní přehled nových analýz, studií, průzkumů a odborných článků o energetice, dekarbonizaci a změně klimatu. Přehled je česky a obsahuje SWOT analýzu z pohledu **dekarbonizace ČR a podpory veřejnosti pro ni**.

## Co aplikace dělá

- **Každé pondělí ráno** (kolem 6:17 letního času) sama:
  1. stáhne novinky z ~70 zdrojů (česká i zahraniční média, think tanky, agentury pro výzkum veřejného mínění, EU instituce, odborné časopisy přes Crossref a OpenAlex),
  2. vybere položky z uplynulého týdne, odstraní duplicity a odfiltruje témata mimo obor,
  3. levnějším modelem Claude ohodnotí relevanci každé položky (s důrazem na **ČR** a na **postoje veřejnosti a komunikaci**),
  4. silnějším modelem z ~40 nejlepších položek napíše přehled: hlavní poselství, shrnutí, SWOT, nejdůležitější položky, průzkumy a komunikační doporučení, prognózy, přehled podle regionů a co sledovat příští týden,
  5. výsledek zveřejní jako web na GitHub Pages a nabídne ho i jako RSS (`feed.xml`).
- Na začátku přehledu je sekce **Na první pohled**: 5 nejdůležitějších analýz, **Události a termíny** a **Příležitosti**. Příležitosti jsou výzvy a granty z TA ČR, EUKI a EU Funding & Tenders Portal (jen SSH výzvy ke klimatu a energetice). AI vybírá jen ty, které se hodí pro sociologický, politologický nebo ekonomický výzkum, a vynechává technologické dotace. U každé výzvy uvádí min./max. částku, termíny a dvě věty o obsahu. Každá výzva se zobrazí jen jednou. Nastavení najdete v `config/settings.yaml` v sekci `opportunities`.
- **Kdykoliv během týdne** se můžete doptat na konkrétní téma, např. „tepelná čerpadla“ nebo „postoje k jádru“ (viz [Dotaz na téma](#dotaz-na-téma)).
- Váš počítač nemusí být zapnutý, všechno běží v GitHub Actions.

Každý bod SWOT i každé doporučení musí odkazovat na konkrétní zdroj. Program to po vygenerování kontroluje a body bez doložení vyřadí. Text přesto generuje AI, takže **zjištění před použitím ověřte u původního zdroje**.

---

## Instalace na Windows (krok za krokem)

Potřebujete jen jednou. Příkazy pište do **PowerShellu** (Start → napište „PowerShell“).

1. **Python 3.12**: stáhněte z <https://www.python.org/downloads/windows/> („Windows installer (64-bit)“, verze 3.12.x). Na první obrazovce instalátoru **zaškrtněte „Add python.exe to PATH“** a klikněte na *Install Now*.
2. **Git**: stáhněte z <https://git-scm.com/download/win> a nainstalujte s výchozím nastavením.
3. Zavřete a znovu otevřete PowerShell. Ověřte instalaci:
   ```powershell
   py --version
   git --version
   ```
4. Stáhněte si projekt (adresu repozitáře uvidíte na GitHubu pod zeleným tlačítkem *Code*):
   ```powershell
   cd $HOME\Documents
   git clone https://github.com/VAS-UCET/decarbotracker.git
   cd decarbotracker
   ```
5. Vytvořte virtuální prostředí a aktivujte ho:
   ```powershell
   py -3.12 -m venv .venv
   .venv\Scripts\Activate.ps1
   ```
   Pokud se objeví chyba *„running scripts is disabled on this system“*, povolte spouštění skriptů jen pro svůj účet a aktivaci zopakujte:
   ```powershell
   Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
   .venv\Scripts\Activate.ps1
   ```
   Po aktivaci je na začátku řádku `(.venv)`.
6. Nainstalujte knihovny:
   ```powershell
   pip install -r requirements.txt
   ```
7. Zkopírujte vzorový soubor s klíči a otevřete ho v Poznámkovém bloku:
   ```powershell
   Copy-Item .env.example .env
   notepad .env
   ```
   Vložte klíč za `ANTHROPIC_API_KEY=` (bez uvozovek a mezer) a soubor uložte. Soubor `.env` se na GitHub nikdy nenahraje.

> **Tip:** Projekt raději nedávejte do složky synchronizované OneDrivem. Složky `.venv` a `site` mají tisíce malých souborů a OneDrive je někdy zamyká. Nejlepší je například `C:\Users\<vy>\Documents\decarbotracker` bez synchronizace.

### Vyzkoušení bez klíče

```powershell
python -m decarbotracker run --dry-run
python -m decarbotracker serve
```
Pak otevřete <http://localhost:8000>. `--dry-run` stáhne skutečná data, ale místo AI vytvoří ukázkový text, takže nic nestojí. Server ukončíte klávesami `Ctrl+C`.

---

## Kde získat klíče

### Anthropic (povinné pro ostrý běh)
1. Otevřete <https://platform.claude.com> a přihlaste se nebo si založte účet.
2. **Settings → Billing**: dobijte kredit. Na desítky týdnů stačí 5–10 USD (viz [náklady](#očekávané-náklady)).
3. **Settings → API Keys → Create Key**: pojmenujte klíč např. „decarbotracker“ a zkopírujte ho. Zobrazí se jen jednou.
4. Doporučení: v **Settings → Limits** si nastavte měsíční limit útraty, např. 10 USD.

### OpenAlex (volitelné)
Bez klíče je denní rozpočet OpenAlex sdílený podle IP adresy a bývá vyčerpaný. Odborné články pak zajišťuje jen Crossref, což stačí. S klíčem přibudou abstrakty a tematické vyhledávání:
1. <https://openalex.org/settings/api>: přihlaste se a vytvořte klíč (zdarma).
2. Vložte ho do `.env` jako `OPENALEX_API_KEY=` a na GitHubu jako secret stejného jména.

---

## Nasazení na GitHub (web běží sám)

1. Na <https://github.com> si založte účet, pokud ho ještě nemáte.
2. **Vytvořte repozitář**: vpravo nahoře **+ → New repository** → název `decarbotracker` → **Public** → *Create repository*. (Nejspíš už je vytvořený, pokud vám s nasazením pomáhal Claude.)
3. Nahrajte kód, pokud ještě není na GitHubu:
   ```powershell
   git remote add origin https://github.com/VAS-UCET/decarbotracker.git
   git push -u origin main
   ```
4. **Klíče do GitHubu**: v repozitáři **Settings → Secrets and variables → Actions → New repository secret**:
   - `ANTHROPIC_API_KEY`: váš klíč (povinné),
   - `OPENALEX_API_KEY`: volitelně,
   - `CONTACT_EMAIL`: volitelně, váš e-mail pro Crossref.
5. **Zapněte Pages**: **Settings → Pages → Build and deployment → Source: GitHub Actions**.
6. **První spuštění**: **Actions** → vlevo **weekly** → **Run workflow** → **Run workflow**. Pokud GitHub ukáže žluté upozornění, že workflow jsou vypnuté, klikněte nejdřív na *I understand my workflows, go ahead and enable them*.
7. Po 5–10 minutách poběží web na `https://VAS-UCET.github.io/decarbotracker/`. Odkaz najdete i v **Settings → Pages**.

Od té doby se web obnovuje každé pondělí sám. Když něco selže, GitHub vám pošle e-mail a web zůstane v posledním funkčním stavu.

---

## Dotaz na téma

**Z prohlížeče (doporučeno):** **Actions → ask → Run workflow**, do pole *Téma dotazu* napište například `ETS2 domácnosti` a volitelně změňte počet dní (výchozí 14). Za pár minut najdete výsledek na webu v sekci **Dotazy**.

**Lokálně:**
```powershell
python -m decarbotracker ask "tepelná čerpadla"
python -m decarbotracker ask "postoje k jádru" --days 30
python -m decarbotracker ask "energetická chudoba" --no-deploy   # jen u vás, bez odeslání na GitHub
```
Výsledek se vypíše do okna a uloží do `data/briefs/`. Bez `--no-deploy` se pošle na GitHub a web se aktualizuje. Jeden dotaz stojí zhruba 0,05–0,15 USD.

---

## Příkazy

| Příkaz | Co dělá |
|---|---|
| `python -m decarbotracker run` | celá pipeline za týden, který právě skončil, a sestavení webu; lokálně pak data pošle na GitHub |
| `python -m decarbotracker run --week 2026-W39` | konkrétní týden |
| `python -m decarbotracker run --dry-run` | bez AI (zdarma), ukázkový výstup |
| `python -m decarbotracker run --no-deploy` | neposílat výsledek na GitHub |
| `python -m decarbotracker run --reuse-items` | nestahovat znovu, jen znovu ohodnotit a shrnout uložené položky |
| `python -m decarbotracker check-sources` | otestuje všechny zdroje a vypíše tabulku stavu (uloží `data/source_health.json`) |
| `python -m decarbotracker build` | jen přegeneruje web z uložených dat |
| `python -m decarbotracker serve` | náhled webu na <http://localhost:8000> |
| `python -m decarbotracker serve --base-url /decarbotracker/` | náhled se stejnými cestami jako na GitHub Pages |
| `python -m decarbotracker ask "téma"` | dotaz na téma |

---

## Úpravy

### Přidání nebo odebrání zdroje (`config/sources.yaml`)
Každý zdroj je blok, například:
```yaml
  - id: muj-zdroj              # unikátní, jen malá písmena bez diakritiky a pomlčky
    name: Můj zdroj
    type: rss                  # rss / wp_json / scrape
    url: "https://example.org/feed/"
    region: CZ                 # CZ / EU / US / GLOBAL
    source_type: think_tank    # think_tank / research / polling / government / media / journal / ngo / industry
    language: cs
    topic_filter: true         # true = položka musí obsahovat klíčové slovo (u obecných médií vždy true)
    weight: 1.0                # 0.5–1.5, důležitost zdroje
    enabled: true
    notes: ""
```
- Zdroj **dočasně vypnete** nastavením `enabled: false`. Do `notes` napište proč, poznámka se zobrazí na stránce Metodika.
- Adresu feedu často najdete jako `/feed/`, `/rss.xml`, `?feed=rss2`, u webů na Joomla `?format=feed&type=rss`.
- Weby bez feedu lze **scrapovat** (`type: scrape`) pomocí CSS selektorů. Inspiraci najdete u existujících zdrojů Ember, IEA a Agora. Takový zdroj je křehký: když web změní vzhled, `check-sources` ukáže „chyba parsování“.
- Po úpravě spusťte `python -m decarbotracker check-sources --only muj-zdroj`.

### Klíčová slova (`config/keywords.yaml`)
Píšou se **bez diakritiky a jako kmeny**, protože program porovnává text bez háčků a čárek a musí zachytit i skloňování. Příklady: `tepeln cerpad` zachytí „tepelná čerpadla“ i „tepelných čerpadel“, `energetick chudob` zachytí „energetické chudoby“. Pozor na vkladné e (čerpadlo → čerpadel): kmen proto musí končit před ním. Zkratky (ETS, CBAM, OZE) patří do seznamu `exact`, kde se hledají jako celé slovo.

### Váhy, modely a limity (`config/settings.yaml`)
- `selection.geo_weights`: váhy regionů (CZ 1.0, EU 0.7, US 0.4, GLOBAL 0.5),
- `selection.source_type_weights`, `bonus_attitudes` (+30 %), `bonus_original_research` (+15 %),
- `selection.max_items_synthesis` (40), `min_cz` (5), `min_attitudes` (5),
- `llm.model_scoring` (`claude-haiku-4-5`), `llm.model_synthesis` (`claude-sonnet-5-5`) a jejich zálohy,
- `llm.max_cost_usd`: **tvrdý limit nákladů na jeden běh** (výchozí 1 USD).

Modely ověřujte v dokumentaci Anthropic (*Models overview* a *Model deprecations*). Haiku 4.5 má plánované vyřazení „nejdříve 15. 10. 2026“. Když přestane fungovat, program automaticky přepne skórování na `model_scoring_fallback`.

### Prompty (`prompts/*.md`)
Instrukce pro AI jsou v samostatných souborech `scoring.md`, `synthesis.md` a `ask.md`. Úpravy se verzují v gitu jako ostatní kód.

---

## Očekávané náklady

Odhad z reálných dat týdne 2026-W39 (336 položek v okně, 157 po předfiltru, 40 do syntézy):

| Krok | Model | Tokeny (odhad) | Cena |
|---|---|---|---|
| skórování (7 dávek × 25 položek) | claude-haiku-4-5 | ~27 000 vstup, ~20 000 výstup | ~0,13 USD |
| týdenní syntéza | claude-sonnet-5-5 | ~12 000 vstup, 8 000–16 000 výstup (vč. „přemýšlení“) | ~0,10–0,18 USD |
| **celkem za týden** | | | **~0,25–0,35 USD** (≈ 1–1,5 USD měsíčně) |
| dotaz na téma | claude-sonnet-5-5 | | ~0,05–0,15 USD |

Skutečnou spotřebu po každém běhu najdete v logu v Actions a v `data/weeks/<týden>.json` (pole `usage`).

**Jak náklady snížit:** v `settings.yaml` snižte `llm.max_items_scoring` (např. na 150), `selection.max_items_synthesis` (např. na 25) nebo `llm.synthesis_effort` na `low`. Tvrdý strop dává `llm.max_cost_usd`: pokud by ho odhad (spočítaný přes `count_tokens`) překročil, program zkrátí vstupy. Dalším pojistkou je měsíční limit v Anthropic Console.

---

## Řešení častých problémů

**Zdroj vrací 403 (chyba HTTP).** Web blokuje automatické stahování. Program zkouší i prohlížečový User-Agent a záložní stažení přes `urllib`. Když nepomůže ani to (typicky Cloudflare „Just a moment…“), nastavte zdroji `enabled: false` a do `notes` napište důvod. Některé weby blokují jen určité sítě: ze serverů GitHubu může zdroj fungovat, i když z vašeho počítače ne, a naopak. Stav z posledního běhu v Actions ukazuje stránka Metodika.

**Workflow se nespustil.**
- Zkontrolujte **Actions**: nahoře může být upozornění, že jsou workflow vypnuté. Klikněte na *Enable workflow*.
- **GitHub u veřejných repozitářů vypíná plánované workflow po 60 dnech bez aktivity.** Týdenní commit dat tomu normálně brání, ale pokud se to stane: **Actions → weekly → Enable workflow** (nebo „…“ → *Enable workflow*) a pak **Run workflow**.
- Plánované spuštění se někdy zpozdí o desítky minut, to je normální.

**Web ukazuje starou verzi.** Počkejte pár minut po doběhnutí workflow a obnovte stránku s `Ctrl+F5`. V **Actions** ověřte, že poslední běh je zelený. Když syntéza selže, web zobrazí týden bez SWOT s upozorněním; když selže celý běh, zůstane předchozí verze.

**Chyba API klíče** (v logu „Neplatný ANTHROPIC_API_KEY“, „Chybí ANTHROPIC_API_KEY“ nebo „došel kredit“):
- lokálně zkontrolujte `.env` (žádné mezery ani uvozovky kolem klíče),
- na GitHubu zkontrolujte secret `ANTHROPIC_API_KEY` (**Settings → Secrets and variables → Actions**). Klíč můžete přepsat přes *Update*,
- ověřte kredit v Anthropic Console → Billing.
Bez platného klíče se týden stejně zveřejní, jen bez AI shrnutí, se seznamem nejlépe hodnocených položek.

**Vypnutý plánovaný workflow**: viz „Workflow se nespustil“ výše.

**„running scripts is disabled“ při aktivaci `.venv`**: viz krok 5 instalace.

---

## Jak to funguje uvnitř (pro zájemce)

```
decarbotracker/
  cli.py           příkazy (run, check-sources, build, serve, ask)
  pipeline.py      celý týdenní běh
  sources.py       stahování všech zdrojů + health report
  fetch/           http.py (timeouty, retry), feeds.py (RSS/Atom/RDF/WP JSON), scrape.py, academic.py
  normalize.py     jednotný model položky, data vždy v UTC, normalizace URL
  dedup.py         deduplikace (URL, DOI, podobnost titulků), týdenní okno, data/seen.json
  filter.py        předfiltr klíčových slov
  scoring.py       LLM skórování, váhy, kvóty
  synthesis.py     týdenní syntéza, validace doložení, záložní výstup
  ask.py           dotaz na téma
  llm.py           Claude API: strukturovaný výstup, stop_reason, fallbacky, ceny
  render.py        statický web (Jinja2)
config/            sources.yaml, keywords.yaml, settings.yaml
prompts/           instrukce pro AI
templates/, static/  vzhled webu a logo
data/              items/ (položky po týdnech), weeks/ (výstupy), briefs/ (dotazy), seen.json, source_health.json
tests/             testy s uloženými ukázkami feedů a stránek (bez sítě)
```

Kontrola kódu: `ruff check .` a `python -m pytest`. Obojí běží automaticky na GitHubu (workflow **ci**).

Web ukládá a zobrazuje jen metadata, krátké výtahy a vlastní shrnutí, nikdy plné texty článků.
