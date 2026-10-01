"""Odeslání e-mailu přes Resend (https://resend.com). Klíč a adresát jen z prostředí (GitHub Secrets / .env)."""

from __future__ import annotations

import logging
import os

import httpx
from jinja2 import Environment, FileSystemLoader, select_autoescape

from decarbotracker.config import CODE_ROOT
from decarbotracker.models import Digest

log = logging.getLogger(__name__)

RESEND_URL = "https://api.resend.com/emails"
# Bez ověřené domény smí Resend posílat z této adresy jen na e-mail, kterým byl účet založen.
DEFAULT_FROM = "decarbotracker <onboarding@resend.dev>"


class MailError(Exception):
    pass


def render_email(digest: Digest) -> tuple[str, str, str]:
    """Vrátí (předmět, HTML, prostý text)."""
    env = Environment(loader=FileSystemLoader(str(CODE_ROOT / "templates")), autoescape=select_autoescape(["html"]))
    from decarbotracker.digest import x_length

    html = env.get_template("email.html").render(d=digest, x_length=x_length)
    lines = [f"decarbotracker – týden {digest.week}", digest.headline_cs, "", "TŘI NEJZAJÍMAVĚJŠÍ ZJIŠTĚNÍ", ""]
    for n, f in enumerate(digest.findings, 1):
        lines += [f"{n}. [{f.geo}] {f.title_cs}", f"   {f.text_cs}", f"   Zdroj: {f.source_name} – {f.url}", ""]
    lines += [f"Celý přehled: {digest.report_url}", "", "NÁVRH VLÁKNA NA X (zkopírujte po příspěvcích)", ""]
    for n, p in enumerate(digest.x_thread, 1):
        lines += [f"--- {n}/{len(digest.x_thread)} ({x_length(p)} znaků) ---", p, ""]
    subject = f"decarbotracker {digest.week}: {digest.headline_cs}"[:150]
    return subject, html, "\n".join(lines)


def send_email(digest: Digest) -> str:
    """Odešle e-mail; vrátí ID zprávy. Vyhodí MailError, když chybí nastavení nebo selže API."""
    api_key = (os.environ.get("RESEND_API_KEY") or "").strip()
    to = [a.strip() for a in (os.environ.get("MAIL_TO") or "").split(",") if a.strip()]
    sender = (os.environ.get("MAIL_FROM") or "").strip() or DEFAULT_FROM
    if not api_key:
        raise MailError("Chybí RESEND_API_KEY (GitHub Secrets / .env) – e-mail se neposlal")
    if not to:
        raise MailError("Chybí MAIL_TO (adresa příjemce) – e-mail se neposlal")
    subject, html, text = render_email(digest)
    try:
        resp = httpx.post(RESEND_URL, headers={"Authorization": f"Bearer {api_key}"}, timeout=30,
                          json={"from": sender, "to": to, "subject": subject, "html": html, "text": text})
    except httpx.HTTPError as exc:
        raise MailError(f"Síťová chyba při odesílání e-mailu: {exc}") from exc
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("message", resp.text)
        except ValueError:
            detail = resp.text
        raise MailError(f"Resend odmítl e-mail (HTTP {resp.status_code}): {detail[:300]}")
    msg_id = (resp.json() or {}).get("id", "")
    log.info("E-mail odeslán (%d příjemců, id %s)", len(to), msg_id)
    return msg_id
