"""Layer 2: Gemini API with Google Search grounding to discover newly listed single casks & lots."""

from __future__ import annotations
from datetime import date, datetime, timezone
import json
import logging
import re
from typing import Any, Callable, Optional, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import (
    DEFAULT_TARGET_DATES,
    KNOWN_FALSE_POSITIVE_PATTERNS,
    format_date_list_human,
    format_human_date,
)
from .direct_checkers import Fetcher, default_http_fetch
from .matcher import (
    MatchFinding,
    is_blocked_url,
    lookup_israel_shipping,
    parse_stock_and_price_from_html,
)

logger = logging.getLogger(__name__)

GEMINI_DEFAULT_MODEL = "gemini-2.5-flash"


def build_system_prompt(target_dates: Sequence[str] = DEFAULT_TARGET_DATES) -> str:
    """Construct the Gemini system instruction dynamically for the configured target dates."""
    date_bullets: list[str] = []
    years: list[str] = []
    for idx, iso in enumerate(target_dates, start=1):
        d = date.fromisoformat(iso)
        yy = f"{d.year % 100:02d}"
        dd = f"{d.day:02d}"
        mm = f"{d.month:02d}"
        human = format_human_date(iso)
        day_month_year = f"{d.day} {human.split()[0]} {d.year}"
        extra = ""
        if iso == "2012-06-07":
            extra = ", Ardbeg Galileo 750ml/75cl or 50ml code L12 158"
        date_bullets.append(
            f"{idx}. {human} ({dd}.{mm}.{d.year}, {dd}/{mm}/{d.year} in UK/EU DD/MM/YYYY, "
            f"{day_month_year}, {iso}, Kavalan cask {yy}{mm}{dd}, "
            f"Blanton's dump date {d.month}-{d.day}-{yy}{extra})"
        )
        if str(d.year) not in years:
            years.append(str(d.year))

    known_notes: list[str] = []
    if "2012-06-07" in target_dates:
        known_notes.extend(
            [
                "  * An Orkney Distillery (Highland Park) 2012 11yo Single Malts of Scotland Cask #6 (Kirsch Import, distilled 07.06.2012)",
                "  * SMWS 93.184 Glen Scotia 2012 9yo (distilled 7 June 2012)",
                "  * Laphroaig 1996 15yo Signatory Vintage Cask Strength #8511 (bottled 07/06/2012)",
                "  * Craigellachie 1993-2012 Gordon & MacPhail Connoisseurs Choice (bottle code 07/06/12 AB/JEIE)",
                "  * Ardbeg Galileo 1999 12yo — CRITICAL RULE: ONLY 750ml/75cl (US market) and 50ml mini carry code L12 158 (7 June 2012). Standard 70cl UK (L12 162 = 11 June 2012) and 70cl EU (L12 163 = 12 June 2012) DO NOT match unless L12 158 is explicitly confirmed!",
            ]
        )
    if "2014-11-08" in target_dates:
        known_notes.extend(
            [
                "  * Ardnamurchan 2014 10yo AD/11:14 CK.342 (Whisky Club Italia, distilled 8 November 2014)",
                "  * Kalkwijck Eastmoor Batch III (distilled 8 November 2014)",
            ]
        )

    known_section = (
        "- Known exact bottles for these dates:\n" + "\n".join(known_notes) + "\n"
        if known_notes
        else ""
    )
    years_str = "/".join(years)
    allowed_iso_str = " | ".join(target_dates)

    return (
        "You are a precision whisky-hunting research agent.\n"
        "Your task is to use Google Search to find whiskies that are CURRENTLY IN STOCK at an online store OR in a LIVE/UPCOMING AUCTION, matching ANY of these exact dates (distilled OR bottled/dumped):\n"
        + "\n".join(date_bullets)
        + "\n\nPRIORITY RETAILERS & AUCTIONS (must ship to Israel, be in Israel, or clearly state shipping):\n"
        "- Preferred: whizita.de, homeofmalts.com, masterofmalt.com, Israeli (.co.il) liquor stores\n"
        "- Other Israel-shipping retailers: thewhiskyexchange.com, hedonism.co.uk, htfw.com, finedrams.com, deliawhisky.de, deinwhisky.de, thewhiskybarrel.com\n"
        "- Israel-shipping auctions: whiskyauction.com, whiskyauctioneer.com, scotchwhiskyauctions.com\n\n"
        "SINGLE CASKS & BOTTLERS TO CHECK:\n"
        f"- Newly bottled or archived {years_str} single casks from Signatory Vintage (Cask Strength / Un-Chillfiltered / 100 Proof), Gordon & MacPhail (Connoisseurs Choice), SMWS, Single Malts of Scotland, Cadenhead's, Adelphi, Berry Bros., Glenfarclas Family Casks, Balvenie Single Barrel, Kavalan Solist, Ardnamurchan, Blanton's Single Barrel.\n"
        + known_section
        + "\nSTRICT ANTI-HALLUCINATION & TRAP RULES:\n"
        "- DO NOT include sold-out, discontinued, or past/closed auction lots.\n"
        "- DO NOT confuse US MM/DD/YYYY with UK DD/MM/YYYY! On US sites, slash dates are Month/Day/Year; on UK/EU sites, slash dates are Day/Month/Year.\n"
        "- DO NOT confuse laser clock timestamps like 'L21 08/11 14:24' with a calendar year!\n"
        "- DO NOT include scam/scraper sites (corretticatering.com, matbim.be, tiptop-tents.com, nixfertig.de, pdms-group.com, winspiresolutions.com).\n"
        "- Every URL you return MUST be a real URL from your Google Search results.\n\n"
        "OUTPUT FORMAT:\n"
        "Respond with a brief summary followed by a single ```json ... ``` code block containing:\n"
        "{\n"
        '  "matches": [\n'
        "    {\n"
        '      "title": "Full whisky name & cask/code",\n'
        '      "url": "https://...",\n'
        f'      "target_date": "{allowed_iso_str}",\n'
        '      "event_type": "Distilled or Bottled",\n'
        '      "price": "Price with currency",\n'
        '      "retailer": "Store or auction house name",\n'
        '      "ships_to_israel": "Direct / EU-only / Verify",\n'
        '      "evidence": "Exact date text or bottle code shown on listing",\n'
        '      "caveat": "Optional batch/shipping note or null"\n'
        "    }\n"
        "  ]\n"
        "}\n"
        'If nothing new/in-stock is found, return {"matches": []}.\n'
    )


def _extract_json_object(text: str) -> dict[str, Any]:
    """Extract JSON object from fenced ```json block or raw braces."""
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S | re.I)
    candidate = fenced.group(1) if fenced else ""
    if not candidate:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            candidate = text[start : end + 1]
    if not candidate:
        return {"matches": []}
    try:
        parsed = json.loads(candidate)
        return parsed if isinstance(parsed, dict) else {"matches": []}
    except json.JSONDecodeError:
        logger.warning("Could not parse JSON block from Gemini output")
        return {"matches": []}


def call_gemini_search_api(
    api_key: str,
    model: str = GEMINI_DEFAULT_MODEL,
    target_dates: Sequence[str] = DEFAULT_TARGET_DATES,
    timeout: float = 90.0,
) -> dict[str, Any]:
    """Invoke Gemini generateContent REST endpoint with Google Search grounding."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    dates_prose = format_date_list_human(list(target_dates))
    user_prompt = (
        f"Today's date is {today}. Run thorough Google Searches across preferred retailers "
        "(Whizita, Home of Malts, Master of Malt, Israeli .co.il stores), UK/EU stores shipping to Israel "
        "(The Whisky Exchange, Hedonism Wines, HTFW, Fine Drams, Deliawhisky, DeinWhisky), and live whisky "
        "auctions (WhiskyAuction.com, Whisky Auctioneer, Scotch Whisky Auctions) for any IN-STOCK or "
        f"LIVE-AUCTION whisky distilled OR bottled on {dates_prose}. Return only verified live listings."
    )

    payload = {
        "system_instruction": {"parts": [{"text": build_system_prompt(target_dates)}]},
        "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"temperature": 0.1},
    }

    endpoint = (
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    )
    req = Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        },
        method="POST",
    )
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def parse_and_verify_gemini_response(
    raw_response: dict[str, Any],
    fetch: Optional[Fetcher] = default_http_fetch,
    target_dates: Sequence[str] = DEFAULT_TARGET_DATES,
) -> list[MatchFinding]:
    """Parse Gemini response JSON and verify candidate URLs are not blocked or explicitly out of stock."""
    candidates = raw_response.get("candidates") or []
    if not candidates:
        return []

    first = candidates[0]
    parts = first.get("content", {}).get("parts") or []
    full_text = "\n".join(str(p.get("text", "")) for p in parts if isinstance(p, dict))
    parsed = _extract_json_object(full_text)
    raw_matches = parsed.get("matches") or []
    if not isinstance(raw_matches, list):
        return []

    allowed_dates = set(target_dates)
    verified: list[MatchFinding] = []
    for item in raw_matches:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url", "")).strip()
        title = str(item.get("title", "")).strip()
        target_date = str(item.get("target_date", "")).strip()
        if not url.startswith(("http://", "https://")) or not title:
            continue
        if target_date not in allowed_dates:
            continue
        if is_blocked_url(url):
            logger.info("Blocked scam/scraper URL from Gemini: %s", url)
            continue

        combined_low = f"{title} {item.get('evidence', '')} {item.get('caveat', '')}".lower()
        if any(bad in combined_low for bad in KNOWN_FALSE_POSITIVE_PATTERNS):
            logger.info("Filtered known false positive from Gemini: %s", title)
            continue

        if fetch is not None:
            status, html = fetch(url)
            if status == 404 or status == 410:
                logger.info("Rejected dead URL (%s): %s", status, url)
                continue
            if status == 200 and html:
                in_stock, _, _ = parse_stock_and_price_from_html(html)
                if in_stock is False:
                    logger.info("Rejected out-of-stock URL from Gemini: %s", url)
                    continue

        ships = str(item.get("ships_to_israel", "")).strip()
        if not ships or ships.lower() == "verify":
            ships = lookup_israel_shipping(url)

        verified.append(
            MatchFinding(
                title=title,
                url=url,
                target_date=target_date,
                event_type=str(item.get("event_type", "Distilled / Bottled")).strip(),
                price=str(item.get("price", "See listing")).strip() or "See listing",
                retailer=str(item.get("retailer", "")).strip() or url,
                ships_to_israel=ships,
                source_layer="Gemini Search",
                evidence=str(item.get("evidence", "")).strip(),
                caveat=str(item.get("caveat") or "").strip() or None,
            )
        )
    return verified


def run_gemini_discovery(
    api_key: str,
    model: str = GEMINI_DEFAULT_MODEL,
    target_dates: Sequence[str] = DEFAULT_TARGET_DATES,
    fetch: Optional[Fetcher] = default_http_fetch,
    api_caller: Callable[..., dict[str, Any]] = call_gemini_search_api,
) -> list[MatchFinding]:
    """Run Layer 2 Gemini Search Grounding discovery with graceful error handling."""
    if not api_key:
        logger.info("GEMINI_API_KEY not set — skipping Layer 2 Gemini Search discovery.")
        return []
    try:
        raw = api_caller(api_key, model, target_dates)
        return parse_and_verify_gemini_response(raw, fetch=fetch, target_dates=target_dates)
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        logger.error("Gemini API discovery failed (continuing with Layer 1 results): %s", exc)
        return []
