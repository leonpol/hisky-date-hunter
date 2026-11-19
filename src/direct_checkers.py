"""Layer 1: Direct HTTP checks on watched product URLs and preferred/Israel-shipping stores."""

from __future__ import annotations
from datetime import date
import json
import logging
from typing import Callable, Optional, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus, urljoin
from urllib.request import Request, urlopen

from .config import (
    DEFAULT_TARGET_DATES,
    KNOWN_BOTTLE_QUERIES,
    SHOPIFY_SEARCH_STORES,
    WATCHED_PRODUCTS,
    WatchedProduct,
)
from .matcher import (
    MatchFinding,
    detect_target_date,
    is_blocked_url,
    parse_stock_and_price_from_html,
)

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

Fetcher = Callable[[str], tuple[int, str]]


def default_http_fetch(url: str, timeout: float = 20.0) -> tuple[int, str]:
    req = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/json,application/xhtml+xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,de;q=0.7",
        },
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", 200)
            body_bytes = resp.read(1_500_000)
            charset = resp.headers.get_content_charset() or "utf-8"
            return status, body_bytes.decode(charset, errors="replace")
    except HTTPError as exc:
        return exc.code, ""
    except (URLError, TimeoutError, OSError) as exc:
        logger.warning("HTTP fetch failed for %s: %s", url, exc)
        return 0, ""


def check_watched_product(
    product: WatchedProduct,
    fetch: Fetcher = default_http_fetch,
) -> Optional[MatchFinding]:
    """Check a single known exact-date product page (and Shopify JSON if available)."""
    if product.shopify_json_url:
        status, raw_json = fetch(product.shopify_json_url)
        if status == 200 and raw_json:
            try:
                pdata = json.loads(raw_json).get("product", {})
                variants = pdata.get("variants") or []
                in_stock = any(bool(v.get("available")) for v in variants if isinstance(v, dict))
                if not in_stock:
                    if any("available" in v for v in variants if isinstance(v, dict)):
                        return None
                if in_stock:
                    first_price = variants[0].get("price", "") if variants else ""
                    return MatchFinding(
                        title=product.name,
                        url=product.url,
                        target_date=product.target_date,
                        event_type=product.event_type,
                        price=f"£{first_price}" if first_price else "See listing",
                        retailer=product.retailer,
                        ships_to_israel=product.ships_to_israel,
                        source_layer="Direct Check",
                        evidence="Shopify variant available=true on watched exact-date product",
                        caveat=product.caveat,
                    )
            except json.JSONDecodeError:
                pass

    status, html = fetch(product.url)
    if status != 200 or not html:
        logger.info("Watched URL %s returned status %s (skipping)", product.url, status)
        return None

    in_stock, price_str, _ = parse_stock_and_price_from_html(html)
    if in_stock is True:
        return MatchFinding(
            title=product.name,
            url=product.url,
            target_date=product.target_date,
            event_type=product.event_type,
            price=price_str or "See listing",
            retailer=product.retailer,
            ships_to_israel=product.ships_to_israel,
            source_layer="Direct Check",
            evidence="Live product page detected InStock",
            caveat=product.caveat,
        )
    return None


def check_shopify_predictive_searches(
    fetch: Fetcher = default_http_fetch,
    target_dates: Sequence[str] = DEFAULT_TARGET_DATES,
) -> list[MatchFinding]:
    """Query Shopify predictive search API on Israel-shipping stores for known exact bottles."""
    active_set = set(target_dates)
    active_queries = [spec for spec in KNOWN_BOTTLE_QUERIES if spec["target_date"] in active_set]
    if not active_queries:
        return []

    results: list[MatchFinding] = []
    for store in SHOPIFY_SEARCH_STORES:
        base = store["base_url"].rstrip("/")
        for spec in active_queries:
            q = quote_plus(spec["query"])
            api_url = (
                f"{base}/search/suggest.json?q={q}"
                "&resources[type]=product&resources[limit]=5"
            )
            status, body = fetch(api_url)
            if status != 200 or not body:
                continue
            try:
                payload = json.loads(body)
                products = (
                    payload.get("resources", {})
                    .get("results", {})
                    .get("products", [])
                )
            except (json.JSONDecodeError, AttributeError):
                continue

            for prod in products:
                if not isinstance(prod, dict):
                    continue
                if prod.get("available") is False:
                    continue
                title = str(prod.get("title", "")).strip()
                body_text = f"{title} {prod.get('body', '')}"
                low = body_text.lower()
                if spec["must_contain"] not in low:
                    continue
                if spec["must_contain_2"] and spec["must_contain_2"] not in low:
                    continue

                prod_url = urljoin(base + "/", str(prod.get("url", "")).split("?")[0])
                if is_blocked_url(prod_url):
                    continue

                if prod.get("available") is not True:
                    p_status, p_html = fetch(prod_url)
                    if p_status == 200 and p_html:
                        in_stock, _, _ = parse_stock_and_price_from_html(p_html)
                        if in_stock is not True:
                            continue
                    else:
                        continue

                price = str(prod.get("price", "")).strip()
                results.append(
                    MatchFinding(
                        title=title,
                        url=prod_url,
                        target_date=spec["target_date"],
                        event_type=spec["event_type"],
                        price=price or "See listing",
                        retailer=store["name"],
                        ships_to_israel=store["ships_to_israel"],
                        source_layer="Direct Check",
                        evidence=f"Shopify predictive search match for '{spec['query']}' (available=true)",
                        caveat=spec.get("caveat") or None,
                    )
                )
    return results


def _build_german_store_queries(target_dates: Sequence[str]) -> list[str]:
    queries: list[str] = []
    for iso in target_dates:
        d = date.fromisoformat(iso)
        dot_q = f"{d.day:02d}.{d.month:02d}.{d.year}"
        if dot_q not in queries:
            queries.append(dot_q)
    if "2012-06-07" in target_dates:
        queries.append("93.184")
    return queries


def check_whizita_and_home_of_malts(
    fetch: Fetcher = default_http_fetch,
    target_dates: Sequence[str] = DEFAULT_TARGET_DATES,
) -> list[MatchFinding]:
    """Search preferred German stores (Whizita & Home of Malts) for exact date strings."""
    findings: list[MatchFinding] = []
    search_endpoints = [
        (
            "Whizita (Germany — Preferred)",
            "https://www.whizita.de/en/search?page=search&keywords={q}",
            "Preferred — Ships to Israel via DHL (€45 up to 5 kg)",
        ),
        (
            "Home of Malts (Germany — Preferred)",
            "https://www.homeofmalts.com/search?search={q}",
            "Preferred — Ships to Israel via DHL (~€40 for 1 bottle, ex-VAT)",
        ),
    ]
    date_queries = _build_german_store_queries(target_dates)
    for retailer_name, tmpl, shipping_note in search_endpoints:
        for q in date_queries:
            url = tmpl.format(q=quote_plus(q))
            status, html = fetch(url)
            if status != 200 or not html:
                continue
            sanitized = (
                html.replace(f'value="{q}"', "")
                .replace(f"value='{q}'", "")
                .replace(f"«{q}»", "")
                .replace(f'"{q}"', "")
                .replace(f"'{q}'", "")
            )
            match = detect_target_date(sanitized, url=url, target_dates=target_dates)
            if match:
                target_date, event_type, evidence = match
                in_stock, price_str, title = parse_stock_and_price_from_html(html)
                if in_stock is not False:
                    findings.append(
                        MatchFinding(
                            title=title or f"{retailer_name} search hit for {q}",
                            url=url,
                            target_date=target_date,
                            event_type=event_type,
                            price=price_str or "See search results",
                            retailer=retailer_name,
                            ships_to_israel=shipping_note,
                            source_layer="Direct Check",
                            evidence=evidence,
                        )
                    )
    return findings


def run_all_direct_checks(
    fetch: Fetcher = default_http_fetch,
    target_dates: Sequence[str] = DEFAULT_TARGET_DATES,
) -> list[MatchFinding]:
    """Execute all Layer 1 direct checks filtered to `target_dates`."""
    active_set = set(target_dates)
    findings: list[MatchFinding] = []
    for product in WATCHED_PRODUCTS:
        if product.target_date not in active_set:
            continue
        hit = check_watched_product(product, fetch=fetch)
        if hit is not None:
            findings.append(hit)

    findings.extend(check_shopify_predictive_searches(fetch=fetch, target_dates=target_dates))
    findings.extend(check_whizita_and_home_of_malts(fetch=fetch, target_dates=target_dates))
    return findings
