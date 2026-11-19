"""Entrypoint for the Monthly Whisky Hunter Agent."""

from __future__ import annotations
import argparse
import logging
import os
from pathlib import Path
from typing import Sequence

from .config import format_date_list_human, parse_target_dates
from .direct_checkers import run_all_direct_checks
from .gemini_hunter import GEMINI_DEFAULT_MODEL, run_gemini_discovery
from .matcher import MatchFinding, normalize_url
from .notifier import dispatch_notifications, format_markdown_report, send_telegram_test_ping
from .state import SeenStateStore

logger = logging.getLogger("whisky_hunter")


def deduplicate_findings(findings: Sequence[MatchFinding]) -> list[MatchFinding]:
    """Deduplicate findings across Layer 1 and Layer 2 by normalized URL."""
    merged: dict[str, MatchFinding] = {}
    for item in findings:
        norm = normalize_url(item.url)
        if norm not in merged:
            merged[norm] = item
    return list(merged.values())


def write_github_step_summary(
    new_matches: Sequence[MatchFinding],
    all_matches: Sequence[MatchFinding],
    target_dates: Sequence[str],
) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY", "").strip()
    if not summary_path:
        return
    if new_matches:
        _, md = format_markdown_report(new_matches, len(all_matches), target_dates=target_dates)
    else:
        dates_prose = format_date_list_human(list(target_dates))
        md = (
            "### 🥃 Monthly Whisky Hunter — No New Matches\n\n"
            f"- **Target Dates:** {dates_prose} (`{', '.join(target_dates)}`)\n"
            "- Checked direct watchlist URLs, preferred retailers, and Gemini Search discovery.\n"
            f"- Active previously-seen matches still live: **{len(all_matches)}**\n"
            "- No notification sent (nothing new since last run).\n"
        )
    try:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write(md + "\n")
    except OSError as exc:
        logger.warning("Could not write GITHUB_STEP_SUMMARY: %s", exc)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Monthly Whisky Hunter for exact distillation/bottling dates."
    )
    parser.add_argument(
        "--dates",
        default=None,
        help=(
            "Comma-separated ISO dates (YYYY-MM-DD) to hunt for. "
            "Defaults to TARGET_DATES env var or '2012-06-07,2014-11-08'."
        ),
    )
    parser.add_argument(
        "--state-file",
        default="state/seen_matches.json",
        help="Path to JSON file tracking previously alerted URLs.",
    )
    parser.add_argument(
        "--force-notify",
        action="store_true",
        help="Alert on all active matches even if previously seen (useful for testing).",
    )
    parser.add_argument(
        "--send-test-ping",
        action="store_true",
        help="Send a Telegram connection test message even if 0 new matches are found.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print report without creating GitHub Issues, sending messages, or mutating state.",
    )
    parser.add_argument(
        "--skip-direct",
        action="store_true",
        help="Skip Layer 1 direct HTTP retailer checks.",
    )
    parser.add_argument(
        "--skip-gemini",
        action="store_true",
        help="Skip Layer 2 Gemini Search Grounding discovery.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    try:
        target_dates = parse_target_dates(args.dates)
    except ValueError as exc:
        logger.error("Invalid date in --dates / TARGET_DATES (expected YYYY-MM-DD): %s", exc)
        return 2

    logger.info(
        "Hunting for target date(s): %s (%s)",
        format_date_list_human(list(target_dates)),
        ", ".join(target_dates),
    )

    force_notify = args.force_notify or (
        os.environ.get("FORCE_NOTIFY", "").strip().lower() in ("1", "true", "yes")
    )
    test_ping = args.send_test_ping or (
        os.environ.get("SEND_TEST_PING", "").strip().lower() in ("1", "true", "yes")
    )
    one_off_override = bool(
        args.dates or os.environ.get("MANUAL_DATES_OVERRIDE", "").strip()
    )

    direct_findings: list[MatchFinding] = []
    if not args.skip_direct:
        logger.info("Running Layer 1: Direct checks on watched URLs & preferred stores...")
        direct_findings = run_all_direct_checks(target_dates=target_dates)
        logger.info("Layer 1 found %d active match(es).", len(direct_findings))

    gemini_findings: list[MatchFinding] = []
    if not args.skip_gemini:
        api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        model = os.environ.get("GEMINI_MODEL", GEMINI_DEFAULT_MODEL).strip() or GEMINI_DEFAULT_MODEL
        logger.info("Running Layer 2: Gemini Search Grounding (%s)...", model)
        gemini_findings = run_gemini_discovery(
            api_key=api_key, model=model, target_dates=target_dates
        )
        logger.info("Layer 2 found %d verified match(es).", len(gemini_findings))

    all_active = deduplicate_findings([*direct_findings, *gemini_findings])
    active_direct_norms = {normalize_url(m.url) for m in direct_findings}

    store = SeenStateStore(Path(args.state_file))
    if force_notify:
        to_notify = all_active
    else:
        to_notify = store.filter_new(
            all_active,
            active_direct_urls=None if (args.skip_direct or one_off_override) else active_direct_norms,
            active_target_dates=None if one_off_override else set(target_dates),
        )

    write_github_step_summary(to_notify, all_active, target_dates)

    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    tg_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    delivered = True

    if to_notify:
        logger.info("Dispatching alert for %d match(es)...", len(to_notify))
        channels = dispatch_notifications(
            to_notify,
            all_active_count=len(all_active),
            target_dates=target_dates,
            dry_run=args.dry_run,
        )
        logger.info("Notification channels triggered: %s", channels)
        if not channels and not args.dry_run:
            delivered = False
            logger.warning(
                "Matches were found, but NO notification channels are configured! "
                "Add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in GitHub Settings -> Secrets and variables -> Actions. "
                "Skipping state update so these matches will still be sent once Telegram is configured."
            )
    else:
        logger.info("No new matching bottles found this month — staying quiet.")
        if test_ping and not args.dry_run:
            if tg_token and tg_chat_id:
                send_telegram_test_ping(
                    tg_token,
                    tg_chat_id,
                    active_count=len(all_active),
                    target_dates=target_dates,
                )
                logger.info("Sent Telegram test ping to chat %s", tg_chat_id)
            else:
                logger.error(
                    "SEND_TEST_PING was requested, but TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is not set "
                    "in GitHub Settings -> Secrets and variables -> Actions!"
                )
                print(
                    "::error::Telegram secrets missing! Go to Settings -> Secrets and variables -> Actions "
                    "and add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID."
                )
                return 1

    if test_ping and not args.dry_run and not (tg_token and tg_chat_id):
        print(
            "::error::Telegram secrets missing! Go to Settings -> Secrets and variables -> Actions "
            "and add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID."
        )
        return 1

    if not args.dry_run and not one_off_override and delivered:
        store.mark_seen(all_active)
        store.save()
        logger.info("Updated state file at %s", args.state_file)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
