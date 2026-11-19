"""Unit tests for the Monthly Whisky Hunter Agent."""

from __future__ import annotations
import json
from pathlib import Path
import tempfile
import unittest

from src.config import WATCHED_PRODUCTS
from src.direct_checkers import check_shopify_predictive_searches, check_watched_product
from src.gemini_hunter import parse_and_verify_gemini_response
from src.matcher import (
    MatchFinding,
    detect_target_date,
    is_blocked_url,
    normalize_url,
    parse_stock_and_price_from_html,
)
from src.notifier import (
    TELEGRAM_MAX_CHUNK,
    format_markdown_report,
    format_telegram_messages,
    send_telegram_messages,
)
from src.state import SeenStateStore


class TestDateMatcherAndTraps(unittest.TestCase):
    def test_valid_june_7_2012_formats(self) -> None:
        cases = [
            ("Distilled: 07.06.2012 | Bottled: 15.01.2024", "https://deliawhisky.de/x", "Distilled"),
            ("Distilled: 26.11.1996 | Bottled: 07.06.2012", "https://deliawhisky.de/y", "Bottled"),
            ("Distilled on 7 June 2012, Cask 93.184", "https://smws.com/x", "Distilled"),
            ("Signatory Vintage Cask #8511 Bottled on: 07/06/2012", "https://whiskyauctioneer.com/lot/1", "Bottled"),
            ("Bottle code: 07/06/12 AB/JEIE", "https://whiskyauction.com/item/2348674", "Bottled"),
            ("Ardbeg Galileo 750ml laser code L12 158 16:11", "https://whiskyauctioneer.com/lot/2", "Bottled"),
            ("Kavalan Solist Sherry Cask S120607A", "https://whizita.de/kavalan", "Distilled / Bottled"),
            ("Blanton's Single Barrel Dumped on 6-7-12", "https://unicornauctions.com/lot/9", "Bottled"),
        ]
        for text, url, expected_event in cases:
            with self.subTest(text=text):
                res = detect_target_date(text, url=url)
                self.assertIsNotNone(res)
                assert res is not None
                self.assertEqual(res[0], "2012-06-07")
                self.assertEqual(res[1], expected_event)

    def test_valid_nov_8_2014_formats(self) -> None:
        cases = [
            ("Botte riempita l'8 Novembre 2014", "https://whiskyclub.it/p", "Distilled"),
            ("Distilled: 8 November 2014 | Kalkwijck Batch III", "https://finedrams.com/k", "Distilled"),
            ("Destilliert am 08.11.2014", "https://homeofmalts.com/x", "Distilled"),
            ("Kavalan Solist Vinho B141108A", "https://masterofmalt.com/k", "Distilled / Bottled"),
        ]
        for text, url, expected_event in cases:
            with self.subTest(text=text):
                res = detect_target_date(text, url=url)
                self.assertIsNotNone(res)
                assert res is not None
                self.assertEqual(res[0], "2014-11-08")
                self.assertEqual(res[1], expected_event)

    def test_rejects_us_mm_dd_yyyy_bonhams_trap(self) -> None:
        bonhams_text = (
            "Glen Keith distilled 07/19/1995, bottled 08/12/2014. "
            "Strathisla Chivas Brothers Distilled 01/10/1995, bottled 08/11/2014, batch no. SI 19 013."
        )
        self.assertIsNone(detect_target_date(bonhams_text, url="https://www.bonhams.com/auction/33081/lot/203/"))
        # Even on an unknown domain, presence of 07/19/1995 proves MM/DD/YYYY
        generic_us_text = "Distilled 07/19/1995, bottled 08/11/2014"
        self.assertIsNone(detect_target_date(generic_us_text, url="https://example.org/whisky"))

    def test_rejects_laser_clock_timestamp_trap(self) -> None:
        # Glenrothes 1998 #13662 bottled in 2021 (L21) on 08/11 at 14:24
        glenrothes_text = "Bottle code: L0627A L21 08/11 14:24 8"
        self.assertIsNone(detect_target_date(glenrothes_text, url="https://whiskyauction.com/item/4845763"))

    def test_rejects_ardbeg_galileo_uk_eu_70cl_codes(self) -> None:
        self.assertIsNone(detect_target_date("Ardbeg Galileo 70cl L12 162 12:43", url="https://htfw.com/g"))
        self.assertIsNone(detect_target_date("Ardbeg Galileo 70cl L12 163 08:03", url="https://whizita.de/g"))

    def test_blocks_scam_domains(self) -> None:
        self.assertTrue(is_blocked_url("https://corretticatering.com/listing/craigellachie-1993"))
        self.assertTrue(is_blocked_url("https://www.winspiresolutions.com/products/laphroaig-8511"))
        self.assertFalse(is_blocked_url("https://www.whizita.de/en/whisky"))


class TestStockAndDirectCheckers(unittest.TestCase):
    def test_jsonld_stock_parsing(self) -> None:
        in_stock_html = """
        <html><head><title>Orkney 2012</title>
        <script type="application/ld+json">
        {"@type": "Product", "name": "Orkney 2012 #6", "offers": {"availability": "https://schema.org/InStock", "price": "89.90", "priceCurrency": "EUR"}}
        </script></head><body></body></html>
        """
        in_stock, price, title = parse_stock_and_price_from_html(in_stock_html)
        self.assertTrue(in_stock)
        self.assertEqual(price, "EUR 89.90")
        self.assertEqual(title, "Orkney 2012")

        out_html = in_stock_html.replace("InStock", "OutOfStock")
        in_stock2, _, _ = parse_stock_and_price_from_html(out_html)
        self.assertFalse(in_stock2)

    def test_past_auction_result_marked_not_in_stock(self) -> None:
        archive_html = "<html><title>#2348674 - Craigellachie</title><body>Result: 84 EUR in Auction 2023-10</body></html>"
        in_stock, _, _ = parse_stock_and_price_from_html(archive_html)
        self.assertFalse(in_stock)

    def test_watched_product_shopify_json_and_predictive_search(self) -> None:
        twb_prod = next(p for p in WATCHED_PRODUCTS if p.id == "twb-craigellachie-1993-cc")

        def mock_fetch(url: str) -> tuple[int, str]:
            if url.endswith(".json") and "craigellachie-1993" in url:
                return 200, json.dumps({"product": {"variants": [{"available": True, "price": "119.99"}]}})
            if "hedonism.co.uk/search/suggest.json" in url and "93.184" in url:
                return 200, json.dumps(
                    {
                        "resources": {
                            "results": {
                                "products": [
                                    {
                                        "title": "SMWS 93.184 Glen Scotia 2012",
                                        "url": "/products/smws-93-184?variant=123",
                                        "available": True,
                                        "price": "£125.00",
                                    }
                                ]
                            }
                        }
                    }
                )
            return 200, json.dumps({"resources": {"results": {"products": []}}})

        hit = check_watched_product(twb_prod, fetch=mock_fetch)
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertEqual(hit.price, "£119.99")

        search_hits = check_shopify_predictive_searches(fetch=mock_fetch)
        self.assertEqual(len(search_hits), 1)
        self.assertEqual(search_hits[0].url, "https://hedonism.co.uk/products/smws-93-184")


class TestGeminiVerificationAndState(unittest.TestCase):
    def test_gemini_parser_filters_scams_and_sold_out(self) -> None:
        fake_gemini = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": "Here are the results:\n```json\n"
                                + json.dumps(
                                    {
                                        "matches": [
                                            {
                                                "title": "Laphroaig 1996 15yo Signatory #8511",
                                                "url": "https://whiskyauctioneer.com/lot/999999/laphroaig-1996-8511",
                                                "target_date": "2012-06-07",
                                                "event_type": "Bottled",
                                                "price": "£190 current bid",
                                                "retailer": "Whisky Auctioneer",
                                                "ships_to_israel": "Verify",
                                                "evidence": "Bottled on 07/06/2012 on tin",
                                            },
                                            {
                                                "title": "Scam Listing Craigellachie",
                                                "url": "https://corretticatering.com/listing/craigellachie",
                                                "target_date": "2012-06-07",
                                                "event_type": "Bottled",
                                                "price": "$90",
                                                "retailer": "Corretti",
                                                "ships_to_israel": "Yes",
                                                "evidence": "07/06/12",
                                            },
                                            {
                                                "title": "Sold Out Galileo",
                                                "url": "https://www.thewhiskyexchange.com/p/20282/ardbeg-1999-galileo-12-year-old",
                                                "target_date": "2012-06-07",
                                                "event_type": "Bottled",
                                                "price": "£500",
                                                "retailer": "TWE",
                                                "ships_to_israel": "Yes",
                                                "evidence": "75cl L12 158",
                                            },
                                        ]
                                    }
                                )
                                + "\n```"
                            }
                        ]
                    }
                }
            ]
        }

        def mock_fetch(url: str) -> tuple[int, str]:
            if "20282" in url:
                return 200, "<html><body>This product is currently out of stock</body></html>"
            return 200, "<html><body>Current Bid: £190 - Place Bid</body></html>"

        verified = parse_and_verify_gemini_response(fake_gemini, fetch=mock_fetch)
        self.assertEqual(len(verified), 1)
        self.assertIn("whiskyauctioneer.com", verified[0].url)
        self.assertIn("DHL", verified[0].ships_to_israel)

    def test_state_deduplication_and_restock_pruning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "seen_matches.json"
            store = SeenStateStore(state_path)

            m1 = MatchFinding(
                title="Ardbeg Galileo 75cl",
                url="https://www.thewhiskyexchange.com/p/20282/ardbeg-1999-galileo-12-year-old?utm_source=x",
                target_date="2012-06-07",
                event_type="Bottled",
                price="£480",
                retailer="The Whisky Exchange",
                ships_to_israel="Yes",
                source_layer="Direct Check",
                evidence="InStock",
            )

            # Month 1: new match -> reported
            new_m1 = store.filter_new([m1], active_direct_urls={normalize_url(m1.url)})
            self.assertEqual(len(new_m1), 1)
            store.mark_seen([m1])
            store.save()

            # Month 2: still in stock -> NOT reported again
            store2 = SeenStateStore(state_path)
            new_m2 = store2.filter_new([m1], active_direct_urls={normalize_url(m1.url)})
            self.assertEqual(len(new_m2), 0)

            # Month 3: goes out of stock -> pruned from seen
            new_m3 = store2.filter_new([], active_direct_urls=set())
            self.assertEqual(len(new_m3), 0)
            store2.mark_seen([])
            store2.save()

            # Month 4: restocked -> reported again!
            store3 = SeenStateStore(state_path)
            new_m4 = store3.filter_new([m1], active_direct_urls={normalize_url(m1.url)})
            self.assertEqual(len(new_m4), 1)

            subj, body = format_markdown_report(new_m4, 1, github_owner="alice")
            self.assertIn("1 new bottle(s)", subj)
            self.assertIn("cc @alice", body)


class TestTelegramNotifications(unittest.TestCase):
    def test_telegram_html_escaping_and_chunking(self) -> None:
        items = [
            MatchFinding(
                title=f"Craigellachie 1993–2012 Gordon & MacPhail <Batch #{i}>",
                url=f"https://www.thewhiskybarrel.com/products/craigellachie-{i}?a=1&b=2",
                target_date="2012-06-07",
                event_type="Bottled",
                price="£119.99",
                retailer="The Whisky Barrel & Co.",
                ships_to_israel="Yes (DHL)",
                source_layer="Direct Check",
                evidence="Bottle code 07/06/12 AB/JEIE & verified",
                caveat="Check <75cl> vs 70cl" if i == 1 else "",
            )
            for i in range(1, 14)
        ]

        chunks = format_telegram_messages(items, all_active_count=14)
        self.assertGreaterEqual(len(chunks), 2)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), TELEGRAM_MAX_CHUNK + 120)

        first = chunks[0]
        self.assertIn("Gordon &amp; MacPhail &lt;Batch #1&gt;", first)
        self.assertIn("a=1&amp;b=2", first)
        self.assertIn("Check &lt;75cl&gt; vs 70cl", first)

    def test_send_telegram_messages_and_plaintext_fallback(self) -> None:
        calls: list[tuple[str, dict[str, object]]] = []

        def mock_post(endpoint: str, payload: dict[str, object]) -> dict[str, object]:
            calls.append((endpoint, payload))
            if payload.get("parse_mode") == "HTML":
                raise RuntimeError("Simulated HTML parse error")
            return {"ok": True}

        sent = send_telegram_messages(
            "12345:TOKEN",
            "987654321",
            ["🥃 <b>Whisky &amp; Co.</b>\n<a href=\"https://whizita.de\">Link</a>"],
            post_json=mock_post,
        )
        self.assertEqual(sent, 1)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][1].get("parse_mode"), "HTML")
        self.assertIsNone(calls[1][1].get("parse_mode"))
        self.assertIn("Whisky & Co.", str(calls[1][1].get("text")))
        self.assertNotIn("<b>", str(calls[1][1].get("text")))


class TestCustomDatesForFriends(unittest.TestCase):
    def test_parse_custom_target_dates(self) -> None:
        from src.config import DEFAULT_TARGET_DATES, parse_target_dates

        self.assertEqual(parse_target_dates(""), DEFAULT_TARGET_DATES)
        self.assertEqual(
            parse_target_dates("2016-03-15, 2019-10-04; 2016-03-15"),
            ("2016-03-15", "2019-10-04"),
        )
        with self.assertRaises(ValueError):
            parse_target_dates("15-03-2016")

    def test_custom_dates_across_matcher_gemini_and_fork_state(self) -> None:
        from src.direct_checkers import check_whizita_and_home_of_malts, run_all_direct_checks
        from src.gemini_hunter import build_system_prompt

        friend_dates = ("2016-03-15", "2019-10-04")

        # Unambiguous German DD.MM.YYYY, English month, Kavalan cask, Blanton's dump, and UK DD/MM/YYYY
        self.assertEqual(
            detect_target_date("Destilliert am 15.03.2016", target_dates=friend_dates)[0],  # type: ignore[index]
            "2016-03-15",
        )
        self.assertEqual(
            detect_target_date("Kavalan Solist Sherry S191004A", target_dates=friend_dates)[0],  # type: ignore[index]
            "2019-10-04",
        )
        self.assertEqual(
            detect_target_date("Blanton's Dumped on 3-15-16", target_dates=friend_dates)[0],  # type: ignore[index]
            "2016-03-15",
        )
        self.assertEqual(
            detect_target_date("Bottled: 04/10/2019", url="https://whiskyauctioneer.com/lot/5", target_dates=friend_dates)[0],  # type: ignore[index]
            "2019-10-04",
        )
        # Default June 7, 2012 should NOT match when friend_dates are active
        self.assertIsNone(
            detect_target_date("Distilled: 07.06.2012", target_dates=friend_dates)
        )
        # US MM/DD/YYYY trap still rejected for day <= 12 (04/10/2019 on Bonhams = April 10)
        self.assertIsNone(
            detect_target_date("Bottled 04/10/2019", url="https://www.bonhams.com/lot/1", target_dates=friend_dates)
        )

        # Direct checks skip 2012/2014 watchlist items and search Whizita/HoM for 15.03.2016 & 04.10.2019
        fetched_urls: list[str] = []

        def mock_fetch(url: str) -> tuple[int, str]:
            fetched_urls.append(url)
            if "15.03.2016" in url and "whizita" in url:
                return (
                    200,
                    "<html><title>Signatory 2016</title><body>Distilled 15.03.2016 - Add to cart €79.90</body></html>",
                )
            return (200, "<html><body>No results</body></html>")

        hits = run_all_direct_checks(fetch=mock_fetch, target_dates=friend_dates)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].target_date, "2016-03-15")
        self.assertTrue(all("thewhiskyexchange" not in u for u in fetched_urls))

        # Dynamic Gemini system prompt mentions friend's dates & omits 2012 Galileo note
        prompt = build_system_prompt(friend_dates)
        self.assertIn("March 15, 2016", prompt)
        self.assertIn("160315", prompt)
        self.assertNotIn("Ardbeg Galileo", prompt)

        # Fork state cleanup: old 2012-06-07 entries are pruned when friend runs with friend_dates
        with tempfile.TemporaryDirectory() as tmp:
            sp = Path(tmp) / "seen_matches.json"
            sp.write_text(
                json.dumps(
                    {
                        "last_run_utc": "2026-01-01T00:00:00Z",
                        "seen": {
                            "https://whizita.de/old": {
                                "title": "Old 2012 Bottle",
                                "target_date": "2012-06-07",
                                "source_layer": "Gemini Search",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            store = SeenStateStore(sp)
            new_hits = store.filter_new(hits, active_target_dates=set(friend_dates))
            self.assertEqual(len(new_hits), 1)
            self.assertNotIn("https://whizita.de/old", store.seen)

        tg_msg = format_telegram_messages(hits, all_active_count=1, target_dates=friend_dates)[0]
        self.assertIn("March 15, 2016 or October 4, 2019", tg_msg)

    def test_aug_26_2014_and_dec_20_2011_formats_and_springbank_trap(self) -> None:
        friend_dates_2 = ("2014-08-26", "2011-12-20")
        self.assertEqual(
            detect_target_date(
                "distilled on the 26th of August 2014 and matured exclusively in a refill hogshead",
                url="https://hedonism.co.uk/products/caol-ila-11-year-old-finn-thomson-2014",
                target_dates=friend_dates_2,
            ),
            ("2014-08-26", "Distilled", "Matched 26 August 2014: '26th of August 2014'"),
        )
        self.assertEqual(
            detect_target_date(
                "Year bottled: 20th December 2011",
                url="https://www.amshop.de/en/macallan-speymalt-1970.html",
                target_dates=friend_dates_2,
            ),
            ("2011-12-20", "Bottled", "Matched 20 December 2011: '20th December 2011'"),
        )
        # Springbank/Hazelburn batch code '20/12 11 | Bottle code: 27.01.20 11' must NOT match 2011-12-20
        self.assertIsNone(
            detect_target_date(
                "Filling code: 20/12 11 | Bottle code: 27.01.20 11",
                url="https://whiskyauction.com/item/4509899",
                target_dates=friend_dates_2,
            )
        )


if __name__ == "__main__":
    unittest.main()
