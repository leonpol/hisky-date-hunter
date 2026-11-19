"""Configuration, watchlist, shipping metadata, and blocklists for Whisky Hunter."""

from __future__ import annotations
from dataclasses import dataclass
from datetime import date
import os
import re
from typing import Optional

DEFAULT_TARGET_DATES: tuple[str, ...] = ("2012-06-07", "2014-11-08")
DEFAULT_DESTINATION_COUNTRY: str = "Israel"

_ENGLISH_MONTHS: tuple[str, ...] = (
    "",
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def parse_target_dates(raw: Optional[str] = None) -> tuple[str, ...]:
    """
    Parse comma/semicolon/whitespace-separated ISO dates (YYYY-MM-DD).
    Falls back to DEFAULT_TARGET_DATES ("2012-06-07", "2014-11-08") if empty or None.
    Raises ValueError if a provided date is not a valid YYYY-MM-DD calendar date.
    """
    source = raw if raw is not None else os.environ.get("TARGET_DATES", "")
    cleaned = source.strip()
    if not cleaned:
        return DEFAULT_TARGET_DATES

    tokens = [t.strip() for t in re.split(r"[,;\s]+", cleaned) if t.strip()]
    if not tokens:
        return DEFAULT_TARGET_DATES

    seen: list[str] = []
    for tok in tokens:
        parsed = date.fromisoformat(tok)
        iso = parsed.isoformat()
        if iso not in seen:
            seen.append(iso)
    return tuple(seen)


def format_human_date(iso_date: str) -> str:
    """Format '2012-06-07' -> 'June 7, 2012'."""
    try:
        d = date.fromisoformat(iso_date)
        return f"{_ENGLISH_MONTHS[d.month]} {d.day}, {d.year}"
    except ValueError:
        return iso_date


def format_date_list_human(target_dates: tuple[str, ...] | list[str]) -> str:
    """Format a list of ISO dates into prose like 'June 7, 2012 or November 8, 2014'."""
    labels = [format_human_date(d) for d in target_dates]
    if not labels:
        return ""
    if len(labels) == 1:
        return labels[0]
    if len(labels) == 2:
        return f"{labels[0]} or {labels[1]}"
    return ", ".join(labels[:-1]) + f", or {labels[-1]}"


@dataclass(frozen=True)
class WatchedProduct:
    """A known exact-date (or conditional-batch) whisky product page to monitor."""

    id: str
    name: str
    url: str
    retailer: str
    target_date: str  # ISO "YYYY-MM-DD"
    event_type: str  # "Distilled" or "Bottled"
    ships_to_israel: str
    caveat: Optional[str] = None
    shopify_json_url: Optional[str] = None


# Known scam / scraper domains that copy whisky listings and must never be reported.
BLOCKED_DOMAINS: frozenset[str] = frozenset(
    {
        "matbim.be",
        "corretticatering.com",
        "tiptop-tents.com",
        "ashanaink.com",
        "nixfertig.de",
        "6580.tw",
        "pdms-group.com",
        "winspiresolutions.com",
        "webberscience.com",
    }
)

# US domains where slash dates (e.g., 07/06/2012 or 08/11/2014) default to MM/DD/YYYY.
US_SLASH_DATE_DOMAINS: frozenset[str] = frozenset(
    {
        "bonhams.com",
        "klwines.com",
        " Astorwines.com".strip().lower(),
        "totalwine.com",
        "caskers.com",
        "flaviar.com",
        "unicornauctions.com",
        "sothebys.com",
        "ackerwines.com",
        "hartdavis-hart.com",
        "benchmarkwineandspirits.com",
        "cellar.com",
    }
)

# Known false-positive expressions to ignore automatically.
KNOWN_FALSE_POSITIVE_PATTERNS: tuple[str, ...] = (
    # Bonhams US catalog lists bottled 08/11/2014 in MM/DD/YYYY (= August 11, 2014)
    "si 19 013",
    # Ardbeg Galileo 70cl UK (June 11, 2012) & EU (June 12, 2012) laser codes
    "l12 162",
    "l12 163",
)

# Direct product URLs for verified exact-date whiskies.
WATCHED_PRODUCTS: tuple[WatchedProduct, ...] = (
    WatchedProduct(
        id="twe-ardbeg-galileo-75cl",
        name="Ardbeg 1999 Galileo 12yo (75cl US Market — Code L12 158)",
        url="https://www.thewhiskyexchange.com/p/20282/ardbeg-1999-galileo-12-year-old",
        retailer="The Whisky Exchange (UK)",
        target_date="2012-06-07",
        event_type="Bottled",
        ships_to_israel="Yes — direct courier shipping to Israel (ex-VAT pricing at checkout)",
        caveat="75cl/750ml US batch carries code L12 158 (bottled 7 June 2012). Confirm L12 158 before dispatch.",
    ),
    WatchedProduct(
        id="mom-ardbeg-galileo",
        name="Ardbeg Galileo 1999 12yo (Master of Malt)",
        url="https://www.masterofmalt.com/whiskies/ardbeg/ardbeg-galileo-whisky/",
        retailer="Master of Malt (UK — Preferred)",
        target_date="2012-06-07",
        event_type="Bottled (Verify Code)",
        ships_to_israel="Yes — ships directly to Israel",
        caveat=(
            "70cl UK/EU bottles are usually L12 162 (11 June 2012) or L12 163 (12 June 2012); "
            "ask support to check if laser code is L12 158 (7 June 2012)."
        ),
    ),
    WatchedProduct(
        id="twb-craigellachie-1993-cc",
        name="Craigellachie 1993–2012 Gordon & MacPhail Connoisseurs Choice (46%)",
        url="https://www.thewhiskybarrel.com/products/craigellachie-1993-connoisseurs-choice",
        shopify_json_url="https://www.thewhiskybarrel.com/products/craigellachie-1993-connoisseurs-choice.json",
        retailer="The Whisky Barrel (UK)",
        target_date="2012-06-07",
        event_type="Bottled",
        ships_to_israel="Yes — ships internationally (50+ countries; verify Israel rate at checkout)",
        caveat="Confirm bottle laser code reads 07/06/12 AB/JEIE before dispatch.",
    ),
    WatchedProduct(
        id="finedrams-kalkwijck-batch-iii",
        name="Kalkwijck Eastmoor Batch III Single Malt (49.7%)",
        url="https://www.finedrams.com/kalkwijck-eastmoor-batch-iii.html",
        retailer="Fine Drams (Denmark)",
        target_date="2014-11-08",
        event_type="Distilled",
        ships_to_israel="Yes — ships directly to Israel via DHL",
    ),
    WatchedProduct(
        id="whiskyclub-ardnamurchan-ck342",
        name="Ardnamurchan 2014 10yo AD/11:14 CK.342 (Whisky Club Italia Single Cask, 56%)",
        url="https://whiskyclub.it/prodotto/ardnamurchan-ad-1114-ck-342-70cl-56/",
        retailer="Whisky Club Italia (Italy)",
        target_date="2014-11-08",
        event_type="Distilled",
        ships_to_israel="EU checkout only — email dram@whiskyclub.it for Israel courier quote or use EU contact",
    ),
    WatchedProduct(
        id="wein-riegger-craigellachie-1993-cc",
        name="Craigellachie 1993–2012 Gordon & MacPhail Connoisseurs Choice (46%)",
        url="https://www.wein-riegger.de/Craigellachie/Craigellachie-1993-2012-Gordon-amp-MacPhail-Connoisseurs-Choice.html",
        retailer="Wein-Riegger (Germany)",
        target_date="2012-06-07",
        event_type="Bottled",
        ships_to_israel="Germany shipping only — requires German/EU contact + confirm code 07/06/12 AB/JEIE",
        caveat="Email shop@wein-riegger.de to verify laser code 07/06/12 AB/JEIE.",
    ),
    WatchedProduct(
        id="hedonism-caol-ila-2014-finn-thomson",
        name="Caol Ila 11yo 2014 Finn Thomson Single Cask (Refill Hogshead)",
        url="https://hedonism.co.uk/products/caol-ila-11-year-old-finn-thomson-2014",
        shopify_json_url="https://hedonism.co.uk/products/caol-ila-11-year-old-finn-thomson-2014.json",
        retailer="Hedonism Wines (UK)",
        target_date="2014-08-26",
        event_type="Distilled (Verify Label)",
        ships_to_israel="Yes — direct shipping to Israel via DHL (ILS ₪ supported, ex-VAT)",
        caveat=(
            "Description states 'distilled on the 26th of August 2014' (57.2% ABV, 164 bottles), "
            "whereas header lists 56.5% ABV (which matches Cask #301512 distilled 07.05.2014 on other shops). "
            "Ask Hedonism Wines (+44 20 7290 7870) to verify the physical back-label date on SKU HED04708 before ordering."
        ),
    ),
    WatchedProduct(
        id="finedrams-ben-nevis-1991-signatory-2911",
        name="Ben Nevis 1991 23yo Signatory Vintage Un-Chillfiltered Cask #2911 (46%)",
        url="https://www.finedrams.com/ben-nevis-23-year-old-1991-signatory-un-chillfiltered-whisky.html",
        retailer="Fine Drams (Denmark)",
        target_date="2014-08-26",
        event_type="Bottled",
        ships_to_israel="Yes — ships directly to Israel via DHL",
    ),
    WatchedProduct(
        id="htfw-macallan-speymalt-1970-41yo",
        name="The Macallan Speymalt 1970 41yo Gordon & MacPhail (43%, EAN 5020613029538)",
        url="https://www.htfw.com/the-macallan-speymalt-single-malt-scotch-1970-41-year-old-whisky",
        retailer="Hard To Find Whisky (UK)",
        target_date="2011-12-20",
        event_type="Bottled (Verify Batch)",
        ships_to_israel="Yes — free worldwide priority insured shipping to Israel on orders over £500",
        caveat=(
            "EAN 5020613029538 is documented as bottled 20th December 2011; "
            "confirm exact bottling date on label/box with sales@htfw.com before ordering."
        ),
    ),
    WatchedProduct(
        id="whiskyde-macallan-speymalt-1970-2011",
        name="The Macallan Speymalt 1970–2011 41yo Gordon & MacPhail (43%)",
        url="https://www.whisky.de/shop/en/product/Macallan-Speymalt-1970-2011.html",
        retailer="Whisky.de (Germany)",
        target_date="2011-12-20",
        event_type="Bottled (Verify Batch)",
        ships_to_israel="Germany/EU shipping only — requires EU contact + confirm 20.12.2011 bottling date",
        caveat="Ask Whisky.de to confirm the exact bottling date reads 20th December 2011 before ordering.",
    ),
)

# Shopify predictive-search stores that ship directly to Israel.
SHOPIFY_SEARCH_STORES: tuple[dict[str, str], ...] = (
    {
        "name": "Hedonism Wines (UK)",
        "base_url": "https://hedonism.co.uk",
        "ships_to_israel": "Yes — ships directly to Israel via DHL (ILS ₪ supported, ex-VAT)",
    },
    {
        "name": "The Whisky Barrel (UK)",
        "base_url": "https://www.thewhiskybarrel.com",
        "ships_to_israel": "Yes — international shipping",
    },
)

# Specific bottle queries for Shopify predictive search endpoints.
KNOWN_BOTTLE_QUERIES: tuple[dict[str, str], ...] = (
    {
        "query": "Craigellachie 1993 Connoisseurs Choice",
        "must_contain": "craigellachie",
        "must_contain_2": "1993",
        "target_date": "2012-06-07",
        "event_type": "Bottled",
        "caveat": "Confirm laser code 07/06/12 AB/JEIE (2012 bottling).",
    },
    {
        "query": "Laphroaig 1996 Signatory 8511",
        "must_contain": "laphroaig",
        "must_contain_2": "8511",
        "target_date": "2012-06-07",
        "event_type": "Bottled",
        "caveat": "Signatory Cask #8511 bottled 07/06/2012.",
    },
    {
        "query": "93.184",
        "must_contain": "93.184",
        "must_contain_2": "",
        "target_date": "2012-06-07",
        "event_type": "Distilled",
        "caveat": "SMWS 93.184 (Glen Scotia distilled 7 June 2012).",
    },
    {
        "query": "Kalkwijck Batch III",
        "must_contain": "kalkwijck",
        "must_contain_2": "iii",
        "target_date": "2014-11-08",
        "event_type": "Distilled",
        "caveat": "Kalkwijck Eastmoor Batch III distilled 8 November 2014.",
    },
    {
        "query": "Ben Nevis 1991 2911",
        "must_contain": "ben nevis",
        "must_contain_2": "2911",
        "target_date": "2014-08-26",
        "event_type": "Bottled",
        "caveat": "Signatory Vintage Cask #2911 bottled 26.08.2014.",
    },
    {
        "query": "Macallan Speymalt 1970",
        "must_contain": "macallan",
        "must_contain_2": "1970",
        "target_date": "2011-12-20",
        "event_type": "Bottled (Verify Batch)",
        "caveat": "Confirm Gordon & MacPhail Speymalt 1970–2011 batch bottled 20 December 2011 (EAN 5020613029538).",
    },
)

# Preferred & Israel-shipping retailer shipping reference for reports.
ISRAEL_SHIPPING_NOTES: dict[str, str] = {
    "whizita.de": "Preferred — Ships to Israel via DHL (€45 up to 5 kg, €55 up to 10 kg)",
    "homeofmalts.com": "Preferred — Ships to Israel via DHL (~€40 for 1 bottle, 19% German VAT deducted)",
    "masterofmalt.com": "Preferred — Ships directly to Israel",
    "thewhiskyexchange.com": "Ships directly to Israel (ex-VAT pricing)",
    "hedonism.co.uk": "Ships directly to Israel via DHL (supports ILS ₪, ex-VAT pricing)",
    "htfw.com": "Ships directly to Israel (supports ILS ₪, ex-VAT pricing)",
    "finedrams.com": "Ships directly to Israel via DHL",
    "deliawhisky.de": "Ships to Israel via DHL/UPS (€49 up to 5 kg)",
    "deinwhisky.de": "Ships to Israel",
    "whiskyauction.com": "Auction — Ships to Israel via DHL Express (€85 for 1 bottle + €3 packing + 10% buyer fee)",
    "whiskyauctioneer.com": "Auction — Ships to Israel via DHL (monthly auctions open around the 20th)",
}
