"""Date matching, false-positive filtering, and stock-status detection."""

from __future__ import annotations
from dataclasses import asdict, dataclass
import json
import re
from typing import Any, Optional
from urllib.parse import urlparse, urlunparse

from .config import (
    BLOCKED_DOMAINS,
    DEFAULT_TARGET_DATES,
    ISRAEL_SHIPPING_NOTES,
    KNOWN_FALSE_POSITIVE_PATTERNS,
    US_SLASH_DATE_DOMAINS,
    format_human_date,
)


@dataclass
class MatchFinding:
    """Represents a verified in-stock or live-auction whisky matching the target dates."""

    title: str
    url: str
    target_date: str  # "2012-06-07" or "2014-11-08"
    event_type: str  # "Distilled", "Bottled", or "Bottled (Verify Code)"
    price: str
    retailer: str
    ships_to_israel: str
    source_layer: str  # "Direct Check" or "Gemini Search"
    evidence: str
    caveat: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_url(url: str) -> str:
    """Strip tracking query params/fragments for stable deduplication."""
    parsed = urlparse(url.strip())
    scheme = (parsed.scheme or "https").lower()
    netloc = parsed.netloc.lower()
    if netloc.startswith("www."):
        netloc_clean = netloc[4:]
    else:
        netloc_clean = netloc
    path = parsed.path.rstrip("/") or "/"
    # Preserve item/lot query params if present, drop srsltid/utm_*
    kept_query = ""
    if parsed.query:
        parts = [
            p
            for p in parsed.query.split("&")
            if not p.lower().startswith(("utm_", "srsltid=", "fbclid=", "gclid="))
        ]
        kept_query = "&".join(parts)
    return urlunparse((scheme, netloc_clean, path, "", kept_query, ""))


def extract_domain(url: str) -> str:
    netloc = urlparse(url.strip()).netloc.lower()
    return netloc[4:] if netloc.startswith("www.") else netloc


def is_blocked_url(url: str) -> bool:
    """Return True if URL belongs to a known scam/scraper domain."""
    domain = extract_domain(url)
    if not domain:
        return True
    return any(domain == d or domain.endswith("." + d) for d in BLOCKED_DOMAINS)


def lookup_israel_shipping(url: str, fallback: str = "Verify shipping to Israel at checkout") -> str:
    domain = extract_domain(url)
    for key, note in ISRAEL_SHIPPING_NOTES.items():
        if domain == key or domain.endswith("." + key):
            return note
    if domain.endswith(".co.il"):
        return "Local Israeli store (.co.il)"
    return fallback


from datetime import date
from functools import lru_cache
from typing import Sequence

# Page-level detector proving slash dates on the page are US MM/DD/YYYY (e.g. 07/19/1995)
_US_MMDD_PROOF_RE = re.compile(r"\b(?:0?[1-9]|1[0-2])/(?:1[3-9]|2\d|3[01])/(?:19|20)\d{2}\b")
# Guard against laser timestamps like "L21 08/11 14:24"
_LASER_CLOCK_TRAP_RE = re.compile(r"\bL\d{2}\s+\d{2}/\d{2}\s+\d{2}:\d{2}\b")

_MONTH_ALIASES: dict[int, str] = {
    1: "January|Januar|Janvier|Gennaio|Jan",
    2: "February|Februar|Février|Fevrier|Febbraio|Feb",
    3: "March|März|Maerz|Mars|Marzo|Mar",
    4: "April|Avril|Aprile|Apr",
    5: "May|Mai|Maggio",
    6: "June|Juni|Juin|Giugno|Jun",
    7: "July|Juli|Juillet|Luglio|Jul",
    8: "August|Août|Aout|Agosto|Aug",
    9: "September|Septembre|Settembre|Sept|Sep",
    10: "October|Oktober|Octobre|Ottobre|Oct|Okt",
    11: "November|Novembre|Nov",
    12: "December|Dezember|Décembre|Decembre|Dicembre|Dec|Dez",
}


@lru_cache(maxsize=64)
def _build_date_patterns(
    iso_date: str,
) -> tuple[tuple[tuple[re.Pattern[str], str], ...], re.Pattern[str]]:
    """Build (unambiguous_patterns, uk_slash_pattern) for any YYYY-MM-DD date."""
    d = date.fromisoformat(iso_date)
    y = d.year
    yy = f"{y % 100:02d}"
    dd = f"{d.day:02d}"
    mm = f"{d.month:02d}"
    day_re = f"0?{d.day}" if d.day < 10 else f"{d.day}"
    month_opt_re = f"0?{d.month}" if d.month < 10 else f"{d.month}"
    m_aliases = _MONTH_ALIASES[d.month]
    human = format_human_date(iso_date)

    unambiguous: list[tuple[re.Pattern[str], str]] = [
        (
            re.compile(
                rf"\b{day_re}(?:\.|th|st|nd|rd)?\s+(?:of\s+|de\s+|di\s+)?(?:{m_aliases}),?\s+{y}\b",
                re.I,
            ),
            f"{d.day} {human.split()[0]} {y}",
        ),
        (
            re.compile(rf"\b(?:{m_aliases})\s+{day_re}(?:th|st|nd|rd)?,?\s+{y}\b", re.I),
            human,
        ),
        (re.compile(rf"\b{dd}\.{mm}\.{y}\b"), f"{dd}.{mm}.{y}"),
        (re.compile(rf"\b{dd}\.{mm}\.{yy}\b(?!\s*:\d{{2}})"), f"{dd}.{mm}.{yy}"),
        (re.compile(rf"\b{y}[-.]{mm}[-.]{dd}\b"), iso_date),
        (re.compile(rf"\b[A-Z]{yy}{mm}{dd}[A-Z0-9]*\b"), f"Kavalan cask {yy}{mm}{dd} ({iso_date})"),
        (
            re.compile(
                rf"\b(?:dump(?:ed)?(?:\s+date|\s+on)?[:\s#]*){month_opt_re}[-/]{day_re}[-/](?:19|20)?{yy}\b",
                re.I,
            ),
            f"Dump date {d.month}-{d.day}-{yy} ({human})",
        ),
    ]

    if iso_date == "2012-06-07":
        unambiguous.append(
            (re.compile(r"\bL12\s*158\b", re.I), "Ardbeg code L12 158 (7 June 2012)")
        )

    # When day > 12 (13..31), both DD/MM/YYYY and US MM/DD/YYYY are calendar-unambiguous!
    if d.day > 12:
        unambiguous.append(
            (
                re.compile(rf"(?<!L\d{{2}}\s)\b{dd}/{mm}/(?:{y}|{yy})\b(?!\s*:\d{{2}})"),
                f"DD/MM/YYYY {dd}/{mm}/{y}",
            )
        )
        unambiguous.append(
            (
                re.compile(rf"\b{month_opt_re}/{dd}/{y}\b"),
                f"MM/DD/YYYY {mm}/{dd}/{y}",
            )
        )

    uk_slash = re.compile(rf"(?<!L\d{{2}}\s)\b{dd}/{mm}/(?:{y}|{yy})\b(?!\s*:\d{{2}})")
    return (tuple(unambiguous), uk_slash)


def detect_target_date(
    text: str,
    url: str = "",
    target_dates: Sequence[str] = DEFAULT_TARGET_DATES,
) -> Optional[tuple[str, str, str]]:
    """
    Inspect text + URL for a verified target date in `target_dates`.

    Returns (target_date_iso, event_type, evidence_str) or None if no valid date match.
    """
    lowered = text.lower()
    for bad in KNOWN_FALSE_POSITIVE_PATTERNS:
        if bad in lowered:
            return None

    # Strip out laser clock codes like "L21 08/11 14:24" before checking slash dates
    cleaned_text = _LASER_CLOCK_TRAP_RE.sub(" ", text)

    for iso_date in target_dates:
        unambiguous_patterns, _ = _build_date_patterns(iso_date)
        for pattern, label in unambiguous_patterns:
            m = pattern.search(cleaned_text)
            if m:
                event_type = _infer_event_type(cleaned_text, m.start(), m.end())
                return (iso_date, event_type, f"Matched {label}: '{m.group(0)}'")

    # Check UK/EU DD/MM/YYYY slash dates (for days 1..12) only if page is NOT a US MM/DD/YYYY source
    domain = extract_domain(url) if url else ""
    is_us_source = any(domain == d or domain.endswith("." + d) for d in US_SLASH_DATE_DOMAINS)
    if not is_us_source and not _US_MMDD_PROOF_RE.search(cleaned_text):
        for iso_date in target_dates:
            _, uk_slash = _build_date_patterns(iso_date)
            m_slash = uk_slash.search(cleaned_text)
            if m_slash:
                event_type = _infer_event_type(cleaned_text, m_slash.start(), m_slash.end())
                return (iso_date, event_type, f"Matched DD/MM/YYYY '{m_slash.group(0)}'")

    return None


def _infer_event_type(text: str, start: int, end: int) -> str:
    bottled_kws = (
        "bottled",
        "abgefüllt",
        "abfüll",
        "imbottigliat",
        "mis en bouteille",
        "bottle code",
        "dump",
        "l12 158",
    )
    distilled_kws = (
        "distilled",
        "destilliert",
        "distillazione",
        "riempita",
        "cask filled",
        "filled",
    )

    # Check matched string itself first (e.g. L12 158 or Dump date)
    matched_low = text[start:end].lower()
    if any(kw in matched_low for kw in bottled_kws):
        return "Bottled"
    if any(kw in matched_low for kw in distilled_kws):
        return "Distilled"

    # Check the text immediately preceding the date: pick whichever keyword appears closest before `start`
    prefix = text[max(0, start - 50) : start].lower()
    last_bot = max((prefix.rfind(kw) for kw in bottled_kws), default=-1)
    last_dist = max((prefix.rfind(kw) for kw in distilled_kws), default=-1)
    if last_bot != -1 or last_dist != -1:
        return "Bottled" if last_bot > last_dist else "Distilled"

    # Fallback: check immediately after the date (e.g. "07.06.2012 (bottled)")
    suffix = text[end : min(len(text), end + 25)].lower()
    first_bots = [suffix.find(kw) for kw in bottled_kws if suffix.find(kw) != -1]
    first_dists = [suffix.find(kw) for kw in distilled_kws if suffix.find(kw) != -1]
    if first_bots or first_dists:
        min_bot = min(first_bots) if first_bots else 999
        min_dist = min(first_dists) if first_dists else 999
        return "Bottled" if min_bot < min_dist else "Distilled"

    return "Distilled / Bottled"


_JSONLD_SCRIPT_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.I | re.S,
)


def parse_stock_and_price_from_html(html: str) -> tuple[Optional[bool], str, str]:
    """
    Determine (in_stock, price_str, title_str) from HTML using Schema.org JSON-LD first,
    then falling back to page markers.

    in_stock:
      - True: verified purchasable / active lot
      - False: explicitly out of stock, discontinued, or past auction result
      - None: inconclusive
    """
    title = ""
    title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    if title_match:
        title = re.sub(r"\s+", " ", title_match.group(1)).strip()

    # Past auction result guard (e.g. WhiskyAuction archive lots)
    if re.search(r"Result:\s*\d+\s*EUR\s+in\s+Auction", html, re.I):
        return (False, "", title)

    # 1. Inspect JSON-LD Product / Offer blocks
    for raw_block in _JSONLD_SCRIPT_RE.findall(html):
        try:
            data = json.loads(raw_block.strip())
        except json.JSONDecodeError:
            continue
        items = data if isinstance(data, list) else [data]
        expanded: list[dict[str, Any]] = []
        for item in items:
            if isinstance(item, dict):
                expanded.append(item)
                if isinstance(item.get("@graph"), list):
                    expanded.extend(x for x in item["@graph"] if isinstance(x, dict))

        for node in expanded:
            node_type = str(node.get("@type", "")).lower()
            if "product" in node_type:
                if not title and node.get("name"):
                    title = str(node["name"]).strip()
                offers = node.get("offers")
                offer_list = offers if isinstance(offers, list) else ([offers] if isinstance(offers, dict) else [])
                for offer in offer_list:
                    avail = str(offer.get("availability", "")).lower()
                    price_val = offer.get("price") or offer.get("lowPrice") or ""
                    currency = offer.get("priceCurrency") or ""
                    price_str = f"{currency} {price_val}".strip() if price_val else ""
                    if any(k in avail for k in ("instock", "limitedavailability", "onlineonly")):
                        return (True, price_str, title)
                    if any(k in avail for k in ("outofstock", "soldout", "discontinued", "backorder")):
                        return (False, price_str, title)

    # 2. Heuristic text fallback on stripped main markup
    # Extract price if visible
    price_str = ""
    m_price = re.search(r"(?:£|€|\$)\s?\d{2,4}(?:[.,]\d{2})?|\d{2,4}[.,]\d{2}\s?€", html)
    if m_price:
        price_str = m_price.group(0).strip()

    lowered = html.lower()
    negative_markers = (
        "this product is currently out of stock",
        "this product has been discontinued",
        "notify me when back in stock",
        "out of stock",
        "sold out",
        "ausverkauft",
        "nicht mehr verfügbar",
        "esaurito",
    )
    positive_markers = (
        "add to cart",
        "add to basket",
        "in den warenkorb",
        "sofort verfügbar",
        "aggiungi al carrello",
    )

    # Note: pages often have "related products" with "add to basket" or "sold out".
    # Check primary product markers carefully:
    if "this product is currently out of stock" in lowered or "this product has been discontinued" in lowered:
        return (False, price_str, title)
    if "sofort verfügbar" in lowered or "aggiungi al carrello" in lowered:
        return (True, price_str, title)

    has_neg = any(m in lowered for m in negative_markers)
    has_pos = any(m in lowered for m in positive_markers)
    if has_pos and not has_neg:
        return (True, price_str, title)
    if has_neg and not has_pos:
        return (False, price_str, title)

    return (None, price_str, title)
