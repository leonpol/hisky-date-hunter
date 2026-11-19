"""Notification delivery: Telegram Bot (primary) + GitHub Issue + optional SMTP / Resend email."""

from __future__ import annotations
from datetime import datetime, timezone
from email.message import EmailMessage
import html
import json
import logging
import os
import re
import smtplib
from typing import Callable, Sequence
from urllib.request import Request, urlopen

from .config import DEFAULT_TARGET_DATES, format_date_list_human, format_human_date
from .matcher import MatchFinding

logger = logging.getLogger(__name__)

TELEGRAM_MAX_CHUNK = 3800


def format_markdown_report(
    new_matches: Sequence[MatchFinding],
    all_active_count: int,
    github_owner: str = "",
    target_dates: Sequence[str] = DEFAULT_TARGET_DATES,
) -> tuple[str, str]:
    """Return (subject_title, markdown_body) for GitHub Issue / email."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    subject = f"🥃 Whisky Hunter Match: {len(new_matches)} new bottle(s) found ({today})"
    dates_prose = format_date_list_human(list(target_dates))

    lines: list[str] = []
    if github_owner:
        lines.append(f"cc @{github_owner}\n")

    lines.append(
        f"Your monthly Whisky Hunter check (`{today}`) found **{len(new_matches)} new or restocked** "
        f"whisky listing(s) matching **{dates_prose}** (distilled or bottled):\n"
    )

    for idx, m in enumerate(new_matches, start=1):
        pretty_date = format_human_date(m.target_date)
        lines.append(f"### {idx}. [{m.title}]({m.url})")
        lines.append(f"- **Date Match:** {m.event_type} on **{pretty_date}** (`{m.target_date}`)")
        lines.append(f"- **Retailer / Auction:** {m.retailer}")
        lines.append(f"- **Price:** {m.price}")
        lines.append(f"- **Israel Shipping:** {m.ships_to_israel}")
        lines.append(f"- **Source / Evidence:** {m.source_layer} — {m.evidence}")
        if m.caveat:
            lines.append(f"- ⚠️ **Note:** {m.caveat}")
        lines.append("")

    lines.append("---")
    lines.append(
        f"*Total active tracked matches: {all_active_count}. "
        "Previously reported bottles are stored in `state/seen_matches.json` so you won't receive duplicate alerts.*"
    )
    return subject, "\n".join(lines)


def format_telegram_messages(
    new_matches: Sequence[MatchFinding],
    all_active_count: int,
    target_dates: Sequence[str] = DEFAULT_TARGET_DATES,
) -> list[str]:
    """
    Format matches into one or more Telegram HTML messages (each safely under 4096 chars).
    All dynamic strings are HTML-escaped so names like 'Gordon & MacPhail' never fail parsing.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    dates_prose = html.escape(format_date_list_human(list(target_dates)))
    header = (
        f"🥃 <b>Whisky Hunter Alert ({today})</b>\n"
        f"Found <b>{len(new_matches)}</b> new/restocked bottle(s) matching "
        f"<b>{dates_prose}</b>:\n"
    )

    blocks: list[str] = []
    for idx, m in enumerate(new_matches, start=1):
        pretty_date = html.escape(format_human_date(m.target_date))
        safe_url = html.escape(m.url, quote=True)
        safe_title = html.escape(m.title)
        safe_event = html.escape(m.event_type)
        safe_retailer = html.escape(m.retailer)
        safe_price = html.escape(m.price)
        safe_ship = html.escape(m.ships_to_israel)
        safe_ev = html.escape(m.evidence)

        item_lines = [
            f"<b>{idx}. <a href=\"{safe_url}\">{safe_title}</a></b>",
            f"• <b>Date:</b> {safe_event} on <b>{pretty_date}</b> (<code>{html.escape(m.target_date)}</code>)",
            f"• <b>Store / Auction:</b> {safe_retailer}",
            f"• <b>Price:</b> {safe_price}",
            f"• <b>Israel Shipping:</b> {safe_ship}",
            f"• <b>Evidence:</b> {safe_ev}",
        ]
        if m.caveat:
            item_lines.append(f"• ⚠️ <i>{html.escape(m.caveat)}</i>")
        blocks.append("\n".join(item_lines))

    footer = f"\n<i>Total active tracked matches: {all_active_count}</i>"

    chunks: list[str] = []
    current = header
    for block in blocks:
        addition = "\n" + block + "\n"
        if len(current) + len(addition) + len(footer) > TELEGRAM_MAX_CHUNK and current != header:
            chunks.append(current.rstrip())
            current = "🥃 <b>Whisky Hunter (continued)</b>\n\n" + block + "\n"
        else:
            current += addition

    current += footer
    chunks.append(current)
    return chunks


def _strip_html_tags(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def send_telegram_messages(
    bot_token: str,
    chat_id: str,
    messages: Sequence[str],
    *,
    post_json: Callable[[str, dict[str, object]], dict[str, object]] | None = None,
) -> int:
    """Send HTML-formatted message chunks via Telegram Bot API with plain-text fallback."""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    def _default_post(endpoint: str, payload: dict[str, object]) -> dict[str, object]:
        req = Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req, timeout=20.0) as resp:
            return json.loads(resp.read().decode("utf-8"))

    sender = post_json or _default_post
    sent_count = 0
    for chunk in messages:
        payload: dict[str, object] = {
            "chat_id": chat_id,
            "text": chunk,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        try:
            sender(url, payload)
            sent_count += 1
        except Exception as exc:
            logger.warning("Telegram HTML send failed (%s); retrying plain text...", exc)
            fallback_payload: dict[str, object] = {
                "chat_id": chat_id,
                "text": _strip_html_tags(chunk),
            }
            sender(url, fallback_payload)
            sent_count += 1
    return sent_count


def send_telegram_test_ping(
    bot_token: str,
    chat_id: str,
    active_count: int,
    target_dates: Sequence[str] = DEFAULT_TARGET_DATES,
) -> None:
    """Send a manual verification ping to confirm Telegram Bot credentials work."""
    dates_prose = html.escape(format_date_list_human(list(target_dates)))
    msg = (
        "✅ <b>Whisky Hunter Agent Connected!</b>\n\n"
        "Your monthly check is active (runs on the <b>21st of every month</b>) and will message you here "
        f"whenever a new or restocked whisky distilled or bottled on <b>{dates_prose}</b> is found.\n\n"
        f"<i>Active matches in current run: {active_count}</i>"
    )
    send_telegram_messages(bot_token, chat_id, [msg])


def create_github_issue(
    title: str,
    body: str,
    repo: str,
    token: str,
) -> str:
    """Create a GitHub Issue in `owner/repo`."""
    url = f"https://api.github.com/repos/{repo}/issues"
    payload = {
        "title": title,
        "body": body,
        "labels": ["whisky-match"],
    }
    req = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=20.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return str(data.get("html_url", ""))
    except Exception:
        payload.pop("labels", None)
        req_no_label = Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            method="POST",
        )
        with urlopen(req_no_label, timeout=20.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return str(data.get("html_url", ""))


def send_smtp_email(
    subject: str,
    markdown_body: str,
    *,
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    smtp_pass: str,
    to_email: str,
) -> None:
    """Send direct email via SMTP."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = to_email
    msg.set_content(markdown_body)

    with smtplib.SMTP(smtp_host, smtp_port, timeout=25) as server:
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.send_message(msg)


def send_resend_email(
    subject: str,
    markdown_body: str,
    *,
    api_key: str,
    to_email: str,
    from_email: str = "Whisky Hunter <onboarding@resend.dev>",
) -> None:
    """Send direct email via Resend HTTP API."""
    payload = {
        "from": from_email,
        "to": [to_email],
        "subject": subject,
        "text": markdown_body,
    }
    req = Request(
        "https://api.resend.com/emails",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urlopen(req, timeout=20.0) as resp:
        resp.read()


def dispatch_notifications(
    new_matches: Sequence[MatchFinding],
    all_active_count: int,
    *,
    target_dates: Sequence[str] = DEFAULT_TARGET_DATES,
    dry_run: bool = False,
) -> list[str]:
    """
    Send notifications for new matches via configured channels:
    1. Telegram Bot (`TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`) — Primary preferred channel
    2. GitHub Issue (`GITHUB_TOKEN` + `GITHUB_REPOSITORY`, disable with `ENABLE_GITHUB_ISSUE=false`)
    3. Optional direct email via SMTP or Resend
    """
    if not new_matches:
        return []

    owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "").strip()
    subject, body = format_markdown_report(
        new_matches, all_active_count, github_owner=owner, target_dates=target_dates
    )
    tg_chunks = format_telegram_messages(new_matches, all_active_count, target_dates=target_dates)
    channels_sent: list[str] = []

    if dry_run:
        logger.info("DRY RUN — Telegram message preview:\n%s", "\n---\n".join(tg_chunks))
        return ["dry-run"]

    # 1. Telegram Bot notification (Primary)
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    tg_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if tg_token and tg_chat_id:
        try:
            sent = send_telegram_messages(tg_token, tg_chat_id, tg_chunks)
            logger.info("Sent %d Telegram message(s) to chat %s", sent, tg_chat_id)
            channels_sent.append(f"telegram:{tg_chat_id}")
        except Exception as exc:
            logger.error("Failed to send Telegram notification: %s", exc)
    elif tg_token or tg_chat_id:
        logger.warning(
            "Partial Telegram configuration detected: set both TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID."
        )
    else:
        logger.warning(
            "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are not set in Repository Secrets — skipping Telegram send."
        )

    # 2. Optional GitHub Issue notification (disabled by default to avoid GitHub email spam; opt in with ENABLE_GITHUB_ISSUE=true)
    enable_gh_issue = os.environ.get("ENABLE_GITHUB_ISSUE", "false").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    gh_repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    gh_token = os.environ.get("GITHUB_TOKEN", "").strip()
    if enable_gh_issue and gh_repo and gh_token:
        try:
            issue_url = create_github_issue(subject, body, repo=gh_repo, token=gh_token)
            logger.info("Created GitHub Issue notification: %s", issue_url)
            channels_sent.append(f"github-issue:{issue_url}")
        except Exception as exc:
            logger.error("Failed to create GitHub Issue: %s", exc)

    # 3. Optional direct SMTP email
    notify_email = os.environ.get("NOTIFY_EMAIL", "").strip()
    smtp_host = os.environ.get("SMTP_HOST", "").strip()
    smtp_user = os.environ.get("SMTP_USER", "").strip()
    smtp_pass = os.environ.get("SMTP_PASS", "").strip()
    smtp_port = int(os.environ.get("SMTP_PORT", "587").strip() or "587")
    if notify_email and smtp_host and smtp_user and smtp_pass:
        try:
            send_smtp_email(
                subject,
                body,
                smtp_host=smtp_host,
                smtp_port=smtp_port,
                smtp_user=smtp_user,
                smtp_pass=smtp_pass,
                to_email=notify_email,
            )
            logger.info("Sent SMTP email to %s", notify_email)
            channels_sent.append(f"smtp:{notify_email}")
        except Exception as exc:
            logger.error("Failed to send SMTP email: %s", exc)

    # 4. Optional Resend API email
    resend_key = os.environ.get("RESEND_API_KEY", "").strip()
    if notify_email and resend_key:
        try:
            from_addr = os.environ.get("RESEND_FROM_EMAIL", "Whisky Hunter <onboarding@resend.dev>")
            send_resend_email(
                subject,
                body,
                api_key=resend_key,
                to_email=notify_email,
                from_email=from_addr,
            )
            logger.info("Sent Resend email to %s", notify_email)
            channels_sent.append(f"resend:{notify_email}")
        except Exception as exc:
            logger.error("Failed to send Resend email: %s", exc)

    return channels_sent
