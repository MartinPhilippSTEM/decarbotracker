"""Příkazová řádka: python -m decarbotracker <příkaz>."""

from __future__ import annotations

import argparse
import functools
import http.server
import logging
import os
import subprocess
import sys
from datetime import UTC, datetime

from decarbotracker.config import load_settings, normalize_base_url, project_root
from decarbotracker.weeks import last_completed_week, parse_week

log = logging.getLogger("decarbotracker")


class _SecretFilter(logging.Filter):
    """Pojistka: nikdy nevypisuj API klíče do logu."""

    def __init__(self) -> None:
        super().__init__()
        self.secrets = [v for k in ("ANTHROPIC_API_KEY", "OPENALEX_API_KEY", "RESEND_API_KEY") if (v := os.environ.get(k)) and len(v) > 8]

    def filter(self, record: logging.LogRecord) -> bool:
        if self.secrets:
            msg = record.getMessage()
            for s in self.secrets:
                if s in msg:
                    record.msg = msg.replace(s, "***")
                    record.args = ()
        return True


def setup_logging(verbose: bool) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    load_settings()  # načte .env, aby filtr znal klíče
    handler.addFilter(_SecretFilter())
    for noisy in ("httpx", "httpcore", "httpx2", "httpcore2", "anthropic", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# --------------------------------------------------------------------------- příkazy


def cmd_run(args: argparse.Namespace) -> int:
    from decarbotracker.pipeline import run_pipeline
    from decarbotracker.render import build_site

    settings = load_settings()
    week = args.week or last_completed_week()
    parse_week(week)
    report = run_pipeline(week, settings, dry_run=args.dry_run, reuse_items=args.reuse_items)
    out = build_site(settings)
    print(f"\nHotovo: týden {week}, stav {report.status}, {report.items_selected} položek v přehledu.")
    print(f"Data: data/weeks/{week}.json · web: {out}")
    if report.usage.calls:
        print(f"Odhad nákladů LLM: ${report.usage.cost_usd:.4f} ({report.usage.calls} volání)")
    github_output(week=week, status=report.status)
    maybe_deploy(args, f"data: týden {week}")
    return 0


def github_output(**values: str) -> None:
    """V GitHub Actions předá hodnoty dalším krokům (např. týden do commit zprávy)."""
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            for k, v in values.items():
                fh.write(f"{k}={v}\n")


def cmd_check_sources(args: argparse.Namespace) -> int:
    from decarbotracker.config import load_sources
    from decarbotracker.fetch.academic import fetch_academic
    from decarbotracker.sources import disabled_health, fetch_all, format_health_table, save_health

    settings = load_settings()
    sources = load_sources()
    if args.only:
        wanted = {s.strip() for s in args.only.split(",")}
        sources = [s for s in sources if s.id in wanted]
    results = fetch_all(settings, sources)
    healths = [r.health for r in results]
    if args.academic and not args.only:
        _, acad = fetch_academic(settings)
        healths += acad
    disabled = [h for h in disabled_health() if not args.only or h.source_id in {s.id for s in sources}]
    print()
    print(format_health_table(healths + disabled))
    save_health(healths, extra=disabled)
    print("\nUloženo do data/source_health.json")
    return 0


def cmd_check_key(args: argparse.Namespace) -> int:
    """Ověří ANTHROPIC_API_KEY bez vypsání jeho hodnoty."""
    import anthropic

    from decarbotracker.llm import describe_key

    settings = load_settings()
    key = os.environ.get("ANTHROPIC_API_KEY")
    print(f"ANTHROPIC_API_KEY: {describe_key(key)}")
    if not key:
        return 1
    from decarbotracker.llm import WORKSPACE_HINT, make_anthropic_client

    ws = settings.anthropic_workspace_id
    print(f"ANTHROPIC_WORKSPACE_ID: {'nastaveno (' + ws[:10] + '…)' if ws else 'nenastaveno'}")
    try:
        client = make_anthropic_client(settings, max_retries=1, timeout=30)
        models = [m.id for m in client.models.list(limit=50)]
        wanted = [settings.llm.model_scoring, settings.llm.model_synthesis]
        print("Klíč je PLATNÝ. Dostupné potřebné modely:",
              ", ".join(f"{m} {'✓' if any(x == m or x.startswith(m + '-') for x in models) else '✗ (nedostupný)'}"
                        for m in wanted))
        return check_schemas(client, settings)
    except anthropic.AuthenticationError as exc:
        print(f"Klíč je NEPLATNÝ (401): {exc.message}")
    except anthropic.PermissionDeniedError as exc:
        print(f"Klíč nemá oprávnění (403): {exc.message}")
    except anthropic.APIError as exc:
        print(WORKSPACE_HINT if "workspace" in str(exc).lower() else f"Ověření se nezdařilo: {exc}")
    return 1


def cmd_notify(args: argparse.Namespace) -> int:
    """E-mail se 3 zjištěními a návrhem vlákna na X pro hotový týdenní přehled."""
    from datetime import UTC, datetime

    from decarbotracker.digest import build_digest
    from decarbotracker.llm import ClaudeClient, LLMError, UsageTracker
    from decarbotracker.mailer import MailError, render_email, send_email
    from decarbotracker.models import Digest, WeeklyReport
    from decarbotracker.storage import digest_path, read_json, week_path, write_json

    settings = load_settings()
    week = args.week
    if not week:
        weeks = sorted(p.stem for p in (project_root() / "data" / "weeks").glob("*.json"))
        if not weeks:
            print("Žádný týdenní přehled k odeslání.")
            return 1
        week = weeks[-1]
    report = WeeklyReport.model_validate(read_json(week_path(week)))
    existing = read_json(digest_path(week), default=None)
    digest = Digest.model_validate(existing) if existing else None
    if digest is None or digest.generated_at < report.generated_at or args.regenerate:
        client = None
        if not args.dry_run:
            try:
                client = ClaudeClient(settings, UsageTracker(settings.llm))
            except LLMError as exc:
                print(f"({exc} – zjištění se vyberou bez AI)")
        digest = build_digest(report, settings, client)
        if not args.dry_run:
            write_json(digest_path(week), digest)
    subject, html, text = render_email(digest)
    if args.dry_run or args.no_send:
        preview = project_root() / "email-preview.html"
        with open(preview, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(html)
        print(text)
        print(f"\n(Neodesláno. Náhled e-mailu: {preview})")
        return 0
    if digest.sent_at and not args.force:
        print(f"E-mail za týden {week} už byl odeslán {digest.sent_at:%d. %m. %Y %H:%M} UTC (znovu: --force).")
        return 0
    try:
        send_email(digest)
    except MailError as exc:
        print(f"E-mail se nepodařilo odeslat: {exc}")
        return 1
    digest.sent_at = datetime.now(UTC)
    write_json(digest_path(week), digest)
    print(f"E-mail za týden {week} odeslán: {subject}")
    return 0


def check_schemas(client, settings) -> int:
    """Levná zkouška (max_tokens=16), že API přijme schémata strukturovaného výstupu (gramatika není moc velká)."""
    import anthropic

    from decarbotracker.models import BriefDraft, DigestDraft, OpportunityBatch, ReportDraft, ScoreBatch

    status = 0
    for model_id, schema in ((settings.llm.model_scoring, ScoreBatch), (settings.llm.model_synthesis, ReportDraft),
                             (settings.llm.model_synthesis, BriefDraft),
                             (settings.llm.model_synthesis, OpportunityBatch),
                             (settings.llm.model_synthesis, DigestDraft)):
        try:
            client.messages.create(
                model=model_id, max_tokens=16, messages=[{"role": "user", "content": "Test schématu, odpověz krátce."}],
                output_config={"format": {"type": "json_schema", "schema": anthropic.transform_schema(schema)}},
            )
            print(f"Schéma {schema.__name__} ({model_id}): OK")
        except anthropic.BadRequestError as exc:
            print(f"Schéma {schema.__name__} ({model_id}): CHYBA – {exc.message}")
            status = 1
    return status


def cmd_build(args: argparse.Namespace) -> int:
    from decarbotracker.render import build_site

    out = build_site(load_settings(), base_url=args.base_url)
    print(f"Web vygenerován do {out}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from decarbotracker.render import build_site

    base = normalize_base_url(args.base_url)
    out = build_site(load_settings(), base_url=base)
    prefix = base.rstrip("/")

    class Handler(http.server.SimpleHTTPRequestHandler):
        def translate_path(self, path: str) -> str:
            if prefix and path.startswith(prefix):
                path = path[len(prefix):] or "/"
            return super().translate_path(path)

        def log_message(self, format: str, *a) -> None:  # noqa: A002
            pass

    handler = functools.partial(Handler, directory=str(out))
    http.server.ThreadingHTTPServer.allow_reuse_address = True
    with http.server.ThreadingHTTPServer(("127.0.0.1", args.port), handler) as httpd:
        print(f"Náhled webu: http://localhost:{args.port}{base}  (ukončíte Ctrl+C)")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nUkončeno.")
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    from decarbotracker.ask import run_ask
    from decarbotracker.render import build_site

    settings = load_settings()
    brief = run_ask(args.topic, settings, days=args.days, dry_run=args.dry_run)
    build_site(settings)
    print(f"\n{brief.headline_cs}\n")
    if brief.summary_cs:
        print(brief.summary_cs, "\n")
    for f in brief.key_findings:
        print(f" • [{f.geo}] {f.text_cs}")
    print(f"\nUloženo: data/briefs/{brief.slug}.json · stránka: dotazy/{brief.slug}/")
    if brief.usage.calls:
        print(f"Odhad nákladů LLM: ${brief.usage.cost_usd:.4f}")
    github_output(slug=brief.slug, status=brief.status)
    maybe_deploy(args, f"data: dotaz {brief.slug}")
    return 0


# --------------------------------------------------------------------------- nasazení z lokálního PC


def maybe_deploy(args: argparse.Namespace, message: str) -> None:
    """Lokálně: commit data/ a push → GitHub Actions web znovu sestaví a nasadí. V CI dělá commit workflow."""
    if getattr(args, "no_deploy", False) or getattr(args, "dry_run", False) or os.environ.get("GITHUB_ACTIONS"):
        return
    root = project_root()
    if not (root / ".git").exists():
        print("(Nasazení přeskočeno: složka není git repozitář. Použijte --no-deploy, nebo viz README.)")
        return
    try:
        subprocess.run(["git", "add", "data/"], cwd=root, check=True)
        staged = subprocess.run(["git", "diff", "--staged", "--quiet"], cwd=root)
        if staged.returncode == 0:
            print("(Žádné změny dat k odeslání.)")
            return
        subprocess.run(["git", "commit", "-m", message], cwd=root, check=True)
        subprocess.run(["git", "pull", "--rebase"], cwd=root, check=True)
        subprocess.run(["git", "push"], cwd=root, check=True)
        print("Data odeslána na GitHub – web se během pár minut aktualizuje (Actions → weekly).")
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        print(f"(Nasazení se nezdařilo: {exc}. Data zůstala lokálně; zkuste 'git push' ručně.)")


# --------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m decarbotracker",
                                description="Týdenní přehled analýz, studií a průzkumů o dekarbonizaci.")
    p.add_argument("-v", "--verbose", action="store_true", help="podrobný log")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="celá pipeline: fetch → filtr → skórování → syntéza → render")
    r.add_argument("--week", help="ISO týden YYYY-Www (výchozí: týden, který právě skončil)")
    r.add_argument("--dry-run", action="store_true", help="bez volání LLM, deterministický ukázkový výstup")
    r.add_argument("--no-deploy", action="store_true", help="neposílat data na GitHub (lokálně)")
    r.add_argument("--reuse-items", action="store_true", help="nestahovat znovu, použít uložené položky týdne")
    r.set_defaults(func=cmd_run)

    c = sub.add_parser("check-sources", help="otestuje všechny zdroje a vypíše tabulku stavu")
    c.add_argument("--only", help="jen vybrané zdroje (id oddělená čárkou)")
    c.add_argument("--no-academic", dest="academic", action="store_false", help="bez Crossref/OpenAlex")
    c.set_defaults(func=cmd_check_sources)

    n = sub.add_parser("notify", help="e-mail se 3 zjištěními a návrhem vlákna na X")
    n.add_argument("--week", help="týden (výchozí: poslední přehled)")
    n.add_argument("--dry-run", action="store_true", help="bez AI a bez odeslání, jen náhled")
    n.add_argument("--no-send", action="store_true", help="připravit, ale neodeslat (náhled email-preview.html)")
    n.add_argument("--regenerate", action="store_true", help="znovu vybrat zjištění (stojí ~0,02 USD)")
    n.add_argument("--force", action="store_true", help="odeslat znovu, i když už jednou odešlo")
    n.set_defaults(func=cmd_notify)

    k = sub.add_parser("check-key", help="ověří ANTHROPIC_API_KEY (hodnotu nevypisuje)")
    k.set_defaults(func=cmd_check_key)

    b = sub.add_parser("build", help="jen přegeneruje web z uložených JSON dat")
    b.add_argument("--base-url", help="přepíše base_url (např. /decarbotracker/)")
    b.set_defaults(func=cmd_build)

    s = sub.add_parser("serve", help="lokální náhled webu na http://localhost:8000")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--base-url", default="/", help="např. /decarbotracker/ pro test cest jako na GitHub Pages")
    s.set_defaults(func=cmd_serve)

    a = sub.add_parser("ask", help="dotaz na aktuální zjištění k tématu")
    a.add_argument("topic", help="téma, např. \"tepelná čerpadla\"")
    a.add_argument("--days", type=int, help="kolik dní zpět hledat (výchozí 14)")
    a.add_argument("--dry-run", action="store_true", help="bez volání LLM")
    a.add_argument("--no-deploy", action="store_true", help="neposílat výsledek na GitHub")
    a.set_defaults(func=cmd_ask)
    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    started = datetime.now(UTC)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    finally:
        log.debug("Doba běhu: %s", datetime.now(UTC) - started)


if __name__ == "__main__":
    raise SystemExit(main())
