"""Outbound email via Resend.

With no `RESEND_API_KEY` the link is logged instead of sent. That is not a stub: it means the
whole auth flow, including expiry, single use and reuse detection, is testable on a laptop
with no third-party account, and a demo does not fail because a mail provider is having a bad
morning.

The magic-link email deliberately carries no branding beyond the name, no tracking pixel and
no click wrapper. Link-scanning proxies in corporate mail follow wrapped URLs, and a scanner
that follows a single-use login link consumes it before the person clicks.
"""

from __future__ import annotations

import logging

import httpx

from ..config import settings

log = logging.getLogger("haazir.mail")

RESEND_ENDPOINT = "https://api.resend.com/emails"
_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


async def send(to: str, subject: str, text_body: str, html_body: str | None = None) -> bool:
    """Returns True if the provider accepted it. Never raises: a failed send must not turn
    `POST /auth/request-link` into a 500, because the response is 202 either way (§5 rule 8)
    and a different status for a failed send would itself leak whether the address exists."""
    if not settings.resend_api_key:
        log.warning("RESEND_API_KEY not set. Email not sent.\n--- to: %s\n--- %s\n%s",
                    to, subject, text_body)
        return False

    payload = {
        "from": settings.mail_from,
        "to": [to],
        "subject": subject,
        "text": text_body,
    }
    if html_body:
        payload["html"] = html_body

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.post(
                RESEND_ENDPOINT,
                json=payload,
                headers={"Authorization": f"Bearer {settings.resend_api_key}"},
            )
        if r.status_code >= 400:
            log.error("resend rejected the message: %s %s", r.status_code, r.text[:300])
            return False
        return True
    except httpx.HTTPError as exc:
        log.error("resend unreachable: %s", exc)
        return False


def magic_link_email(link: str, minutes: int) -> tuple[str, str, str]:
    subject = "Your HAAZIR sign-in link"
    text_body = (
        "Sign in to HAAZIR:\n\n"
        f"{link}\n\n"
        f"This link works once and expires in {minutes} minutes.\n"
        "If you did not ask for it, ignore this email. Nothing has changed on your account.\n"
    )
    html_body = (
        '<div style="font-family:system-ui,-apple-system,Segoe UI,sans-serif;'
        'font-size:15px;line-height:1.6;color:#14120D">'
        "<p>Sign in to HAAZIR:</p>"
        f'<p><a href="{link}" style="color:#A9530B">{link}</a></p>'
        f"<p>This link works once and expires in {minutes} minutes.</p>"
        "<p>If you did not ask for it, ignore this email. "
        "Nothing has changed on your account.</p>"
        "</div>"
    )
    return subject, text_body, html_body
