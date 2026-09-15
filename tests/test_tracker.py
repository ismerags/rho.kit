"""Offline test suite.

Fixtures are genuine markup captured from the live sites in September 2026 and
trimmed to the structures the parsers actually read — so these tests exercise
the real shapes, not invented ones.

    python -m pytest tests -q        (or)     python tests/test_tracker.py
"""

from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bs4 import BeautifulSoup  # noqa: E402

from legotracker.analytics import compute_stats, detect_alerts, score_deal  # noqa: E402
from legotracker.db import Database  # noqa: E402
from legotracker.matching import (candidate_set_numbers, score_listing,  # noqa: E402
                                  normalise_set_num)
from legotracker.sources.amazon_in import AmazonIN  # noqa: E402
from legotracker.sources.buyhatke import (_extract_balanced_object,  # noqa: E402
                                          _js_object_to_json, offer_from_product_data,
                                          history_points, deal_list, _parse_search_blocks,
                                          _offer_from_search_item,
                                          proxy_url as buyhatke_proxy_url,
                                          retailer_for_link)
from legotracker.sources.base import parse_inr  # noqa: E402
from legotracker.sources.firstcry import FirstCry  # noqa: E402
from legotracker.sources.flipkart import _extract_state, _walk_products, _prices  # noqa: E402
from legotracker.sources.lego_in import PRODUCT_HREF_RE, LegoIN  # noqa: E402

FIX = Path(__file__).parent / "fixtures"
_fails: list[str] = []


def check(cond, label):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        _fails.append(label)


def eq(got, want, label):
    check(got == want, f"{label}  (got {got!r}, want {want!r})")


# ---------------------------------------------------------------- parse_inr

def test_parse_inr():
    print("\nparse_inr")
    eq(parse_inr("₹8,695.00"), 8695.0, "rupee with comma and paise")
    eq(parse_inr("Rs. 2,175"), 2175.0, "Rs. prefix")
    eq(parse_inr("2474.1"), 2474.1, "bare number")
    eq(parse_inr(None), None, "None")
    eq(parse_inr("4.8"), None, "a rating is not a price")
    eq(parse_inr("out of stock"), None, "prose is not a price")


# ---------------------------------------------------------------- matching

def test_set_number_extraction():
    print("\nset number extraction")
    eq(candidate_set_numbers("LEGO NINJAGO Cole's Elemental Earth Mech Toy 71806 ( 235 Pieces)"),
       ["71806"], "piece count is not mistaken for a set number")
    eq(candidate_set_numbers("LEGO Speed Champions Ferrari SF-24 F1 Race Car Driver Set 275 pieces- 77242"),
       ["77242"], "trailing set number after a piece count")
    eq(candidate_set_numbers("LEGO City Police Car Kit 94 Pieces - 60312"),
       ["60312"], "two-digit piece count")
    check("2024" not in candidate_set_numbers("LEGO Icons 2024 Edition 10345"),
          "a year is not treated as a set number")
    eq(normalise_set_num("75192-1"), "75192", "rebrickable suffix stripped")


def test_matching_rejects_junk():
    print("\nmatch scoring")
    good = score_listing(
        "LEGO® NINJAGO® Lloyd's Dragon Mech Battle Pack - 71862", "71862")
    check(good.ok and good.confidence >= 0.75, f"genuine set accepted ({good.confidence:.2f})")

    stand = score_listing(
        "Millennium Falcon Vertical Display Stand for Lego 75192 Starship Model",
        "75192")
    check(not stand.ok, "display stand rejected")

    lights = score_listing(
        "BRIKSMAX Led Lighting Kit for Lego 75192 Millennium Falcon", "75192")
    check(not lights.ok, "third-party light kit rejected")

    knock = score_listing(
        "Pureey Top StarLinks: Kids' Educational Interlocking Blocks 71806", "71806")
    check(not knock.ok, "knock-off without the LEGO brand rejected")

    wrong = score_listing("LEGO Star Wars Millennium Falcon Set 75375", "75192")
    check(not wrong.ok, "a different set number is not a match")

    cheap = score_listing("LEGO Star Wars Millennium Falcon 75192", "75192",
                          price_inr=1200, msrp_usd=849.99)
    check(not cheap.ok, "implausibly cheap listing rejected as a parts lot")


# ---------------------------------------------------------------- adapters

def test_amazon_parser():
    print("\nAmazon.in parser (genuine markup)")
    soup = BeautifulSoup((FIX / "amazon_search.html").read_text(), "lxml")
    cards = soup.select('div[data-component-type="s-search-result"]')
    eq(len(cards), 2, "two cards found")

    adapter = AmazonIN.__new__(AmazonIN)      # no network needed
    first = adapter._parse_card(cards[0])
    check(first is not None, "first card parsed")
    eq(first.price_inr, 995.0, "current price read from .a-price .a-offscreen")
    eq(first.mrp_inr, 1999.0, "MRP read from .a-price.a-text-price")
    eq(first.sku, "B0FPXFP95Z", "ASIN captured")
    eq(first.rating, 4.8, "rating from the popover label")
    check("71862" in first.title, "title comes from the image alt, not the truncated h2")
    check(first.url.endswith("/dp/B0FPXFP95Z") or "/dp/B0FPXFP95Z/" in first.url,
          f"clean product url ({first.url[-40:]})")

    second = adapter._parse_card(cards[1])
    res = score_listing(second.title, "75192", brand=second.brand,
                        price_inr=second.price_inr)
    check(not res.ok, "the accessory card is rejected downstream")


def test_flipkart_parser():
    print("\nFlipkart parser (genuine state shape)")
    html = (FIX / "flipkart_search.html").read_text()
    state = _extract_state(html)
    check(state is not None, "brace-matched __INITIAL_STATE__ extracted")

    products = list(_walk_products(state))
    eq(len(products), 3, "walker found every product regardless of nesting depth")

    cole = [p for p in products if "71806" in p["titles"]["title"]][0]
    eq(_prices(cole["pricing"]), (2175.0, 2290.0), "strikeOff shape -> (selling, mrp)")

    mclaren = [p for p in products if "42166" in p["titles"]["title"]][0]
    eq(_prices(mclaren["pricing"]), (2399.0, 2699.0), "finalPrice/mrp shape")
    eq(mclaren["availability"]["displayState"], "OUT_OF_STOCK", "stock state read")

    junk = [p for p in products if p["titles"]["title"].startswith("Pureey")][0]
    res = score_listing(junk["titles"]["title"], "71806")
    check(not res.ok, "Flipkart knock-off rejected")


def test_firstcry_parser():
    print("\nFirstCry parser (genuine markup)")
    soup = BeautifulSoup((FIX / "firstcry_listing.html").read_text(), "lxml")
    tiles = soup.select(".list_block")
    eq(len(tiles), 2, "two tiles found")

    adapter = FirstCry.__new__(FirstCry)
    a = adapter._parse_tile(tiles[0])
    eq(a.price_inr, 2474.1, "sale price from the aria-label")
    eq(a.mrp_inr, 2749.0, "MRP from the aria-label")
    eq(a.sku, "19967977", "product id from the listingpg- class")
    check(a.url.startswith("https://www.firstcry.com/"),
          "protocol-relative href absolutised")
    check("77242" in a.title, "aria-label title carries the set number")

    b = adapter._parse_tile(tiles[1])
    eq(b.price_inr, 998.0, "second tile price")
    eq(candidate_set_numbers(b.title), ["60312"], "set number extracted from title")


def test_shopify_parser():
    print("\nShopify parser (Toycra / Jaiman Toys, genuine suggest.json)")
    import json
    from legotracker.sources.shopify import Toycra
    data = json.loads((FIX / "toycra_suggest.json").read_text())
    products = data["resources"]["results"]["products"]

    adapter = Toycra.__new__(Toycra)
    offers = [adapter._to_offer(p) for p in products]
    eq(len(offers), 3, "three products parsed")

    a = offers[0]
    eq(a.price_inr, 18499.0, "suggest.json price is RUPEES, not paise")
    eq(a.mrp_inr, 21999.0, "compare_at_price_max used as MRP")
    eq(a.in_stock, True, "availability read")
    check("?_pos=" not in a.url, "search tracking params stripped from url")
    check(a.url == "https://toycra.com/products/"
                   "lego-71814-ninjago-tournament-temple-city-3489-pieces",
          f"absolute product url ({a.url})")

    b = offers[1]
    eq(b.in_stock, False, "out-of-stock product flagged")
    eq(b.mrp_inr, None, "compare_at_price of 0.00 is not treated as an MRP")

    # The accessory must not survive matching, even though its title has 71800.
    res = score_listing(offers[2].title, "71800", brand=offers[2].brand,
                        price_inr=offers[2].price_inr)
    check(not res.ok, "Shopify third-party display stand rejected")

    # And the real set at that number must survive.
    ok = score_listing(b.title, "71800", brand=b.brand)
    check(ok.ok, "genuine LEGO listing accepted")


def test_shopify_paise_conversion():
    print("\nShopify product .js paise conversion")
    from legotracker.sources.shopify import Toycra
    adapter = Toycra.__new__(Toycra)

    class FakeSession:
        def get_json(self, url, **kw):
            # Genuine shape: the product .js endpoint reports PAISE.
            return {"id": 9045096988870,
                    "title": "Lego 71814 Ninjago Tournament Temple City (3489 Pieces)",
                    "price": 1849900, "compare_at_price": 2199900,
                    "vendor": "LEGO",
                    "variants": [{"price": 1849900, "available": True}]}

    adapter.session = FakeSession()
    adapter.last_error = None
    o = adapter.fetch_offer("https://toycra.com/products/x")
    eq(o.price_inr, 18499.0, "paise divided by 100 (a 100x bug if missed)")
    eq(o.mrp_inr, 21999.0, "compare_at_price converted too")
    eq(o.in_stock, True, "availability from variants")


def test_hamleys_parser():
    print("\nHamleys parser (genuine Fynd catalog response)")
    import json
    from legotracker.sources.hamleys import Hamleys, TOKEN_RE
    data = json.loads((FIX / "hamleys_products.json").read_text())

    adapter = Hamleys.__new__(Hamleys)
    offers = [adapter._to_offer(i) for i in data["items"]]
    eq(len(offers), 3, "three items parsed")

    eq(offers[0].price_inr, 9099.0, "effective price read")
    eq(offers[0].mrp_inr, None, "marked == effective is not an MRP")
    eq(offers[1].price_inr, 6079.0, "discounted price read")
    eq(offers[1].mrp_inr, 6399.0, "marked > effective becomes MRP")
    eq(offers[2].in_stock, False, "sellable:false is out of stock")
    check(offers[0].url.endswith(
        "/product/lego-icons-blacktron-renegade-10355-building-blocks-for-kids-18y"),
        "product url built from slug")

    # Token extraction from the server-rendered homepage.
    html = ('...{"gqlClient":{"domain":"https://api.fynd.com",'
            '"authorizationHeader":"Bearer NjA1ODg0Zjg4OTI1MTMzMWZlODQ3ZDgwOkNmT1dMLUZ1QQ=="},'
            '"sectionU...')
    m = TOKEN_RE.search(html)
    check(m is not None, "storefront token found in homepage html")
    check(m.group(1).startswith("Bearer "), "token includes the Bearer prefix")

    eq(candidate_set_numbers(offers[0].title), ["10355"],
       "set number extracted despite '18Y+' age suffix")


def test_buyhatke_parser():
    print("\nBuyHatke parser (genuine captured page data, set 71813)")

    # ---- product-detail route: one near-JSON object literal --------------
    html = (FIX / "buyhatke_detail.html").read_text()
    blob = _extract_balanced_object(html, "data:{siteName:")
    check(blob is not None, "brace-matched data:{siteName:...} object extracted")

    data = _js_object_to_json(blob)
    check(data is not None, "unquoted-key object literal parsed as JSON")
    eq(data["siteName"], "Amazon", "siteName read")

    offer = offer_from_product_data("https://buyhatke.com/x", data)
    check(offer is not None, "offer built from productData")
    eq(offer.retailer, "amazon_in",
       "detail-route offer resolves to the real retailer, not 'buyhatke'")
    eq(offer.url, "http://www.amazon.in/gp/product/B0CQ3VY8X2",
       "detail-route offer keeps the real vendor link from productData.link")
    eq(offer.price_inr, 13499.0, "current price read")
    eq(offer.mrp_inr, None, "mrp(12999) < price(13499) discarded, matches base.py convention")
    eq(offer.sku, "B0CQ3VY8X2", "ASIN read from productData.pid")
    eq(offer.rating, 4.7, "rating read")
    check("71813" in offer.title, "title carries the set number")

    hist = history_points(data)
    eq(len(hist), 4, "four history points (oldest first)")
    eq(hist[0], ("2024-04-19 05:05:31", 33500.0), "oldest point, from 2024")
    eq(hist[-1], ("2026-09-14 07:00:24", 13499.0), "newest point matches current price")

    deals = deal_list(data)
    eq(len(deals), 3, "three cross-retailer deals")
    names = sorted(d["site_name"] for d in deals)
    eq(names, ["Ajio", "Amazon", "Hamleys"], "deal site names read")

    eq(retailer_for_link("https://www.flipkart.com/p/x"), "flipkart",
       "domain resolution: flipkart.com")
    eq(retailer_for_link("https://www.ajio.com/p/x"), "buyhatke",
       "an unmapped domain (Ajio) stays under the generic buyhatke bucket")
    eq(retailer_for_link(None), "buyhatke", "no link at all also falls back")

    # ---- search route: flat x.field=value; statements, not JSON ----------
    search_html = (FIX / "buyhatke_search.html").read_text()
    items = list(_parse_search_blocks(search_html))
    eq(len(items), 2, "two search result blocks found")
    eq(items[0]["price"], 2547, "first item's price read")
    eq(items[1]["pid"], "B0CQ3VY8X2", "second item's pid read")

    first = _offer_from_search_item(items[0])
    check(first is not None, "search item converted to an Offer")
    eq(first.url, "http://www.amazon.in/gp/product/B0FPXDXXYR",
       "offer keeps the real amazon.in link, not a buyhatke-proxied one")
    eq(first.retailer, "amazon_in",
       "retailer resolved from the link's own domain")
    eq(first.price_inr, 2547.0, "search item price")
    eq(first.mrp_inr, 2999.0, "search item mrp(2999) > price(2547) kept")
    check("77256" in first.title, "search item title carries the set number")

    second = _offer_from_search_item(items[1])
    eq(second.retailer, "amazon_in", "second item also resolves to amazon_in")
    eq(buyhatke_proxy_url(first.url),
       "https://buyhatke.com/http://www.amazon.in/gp/product/B0FPXDXXYR",
       "proxy_url reconstructs the paste-link form for re-fetching via BuyHatke")


def test_catalog_extraction():
    """The catalogue's set number comes from the storefront SKU, not the title."""
    print("\ncatalogue extraction")
    from legotracker.catalog import (CatalogItem, pieces_of, set_num_of,
                                     theme_of, to_item)

    eq(set_num_of("75453", "LEGO Star Wars Offworld Sandcrawler Set 75453"),
       "75453", "SKU used directly when it is a set number")
    eq(set_num_of(None, "Technic Porsche GT4 42176 Building Kit (834 Pieces)"),
       "42176", "falls back to the title, ignoring the piece count")
    eq(set_num_of("", "LEGO City Police Car Kit 94 Pieces - 60312"),
       "60312", "empty SKU falls back cleanly")
    eq(set_num_of("SHIRT-L", "LEGO T-Shirt Large"), None,
       "non-numeric SKU with no set number in the title is skipped")

    eq(pieces_of("Technic Porsche GT4 42176 Building Kit (834 Pieces)"), 834,
       "piece count parsed")
    eq(pieces_of("LEGO Art The Kiss 31221 (4,000 pieces)"), 4000,
       "comma-separated piece count parsed")
    eq(pieces_of("LEGO Keyring 854235"), None, "no piece count is fine")

    eq(theme_of("LEGO® Star Wars™ Offworld Sandcrawler Set 75453"), "Star Wars",
       "theme from title")
    eq(theme_of("Technic Mack LR Electric Garbage Truck 42167"), "Technic",
       "Technic detected")
    check(theme_of("LEGO Speed Champions Ferrari 77242") == "Speed Champions",
          "longest theme name wins over a shorter substring")

    item = to_item({"title": "Technic Porsche GT4 e-Performance 42176 (834 Pieces)",
                    "sku": "42176", "price": 15999.0, "compare_at": 17999.0,
                    "in_stock": True, "image": "https://cdn/x.png",
                    "url": "https://store/products/x"})
    eq(item.set_num, "42176", "item set number")
    eq(item.pieces, 834, "item piece count")
    eq(item.mrp, 17999.0, "compare_at above price becomes MRP")
    eq(item.theme, "Technic", "item theme")

    same = to_item({"title": "LEGO Keyring 854235", "sku": "854235",
                    "price": 699.0, "compare_at": 699.0, "in_stock": True})
    eq(same.mrp, None, "compare_at equal to price is not an MRP")

    eq(to_item({"title": "LEGO Gift Card", "sku": "GC", "price": 1000.0}), None,
       "items with no set number are excluded from the catalogue")


def test_catalog_storage():
    print("\ncatalogue storage")
    import tempfile
    from pathlib import Path as _P
    from legotracker.db import Database as _DB
    with tempfile.TemporaryDirectory() as tmp:
        db = _DB(_P(tmp) / "c.db")
        db.replace_catalog([
            {"set_num": "42176", "name": "Technic Porsche", "theme": "Technic",
             "pieces": 834, "image": "https://cdn/a.png", "url": "u1",
             "price": 15999.0, "mrp": 17999.0, "in_stock": True},
            {"set_num": "60312", "name": "City Police Car", "theme": "City",
             "pieces": 94, "image": None, "url": "u2",
             "price": 999.0, "mrp": None, "in_stock": False},
        ])
        items = db.catalog_items()
        eq(len(items), 2, "both rows stored")
        eq(items[0]["set_num"], "60312", "ordered by name")
        eq(items[1]["in_stock"], True, "stock flag round-trips as bool")
        eq(items[0]["in_stock"], False, "out-of-stock round-trips")
        check(db.catalog_age() is not None, "refresh timestamp recorded")

        # A refresh must replace, not merge — dropped sets should disappear.
        db.replace_catalog([{"set_num": "42176", "name": "Technic Porsche",
                             "theme": "Technic", "pieces": 834, "image": None,
                             "url": "u1", "price": 14999.0, "mrp": None,
                             "in_stock": True}])
        items = db.catalog_items()
        eq(len(items), 1, "stale sets are dropped on refresh")
        eq(items[0]["price"], 14999.0, "price updated")

        from legotracker.catalog import themes
        eq(themes(items), ["Technic"], "theme list derived from items")
        db.close()


def test_catalog_name_cleanup():
    """Only the set number is stripped — every other number is part of the name."""
    print("\ncatalogue name cleanup")
    from legotracker.catalog import clean_name

    eq(clean_name("Lego 72537 Kpop Demon Hunters Derpy Tiger", "72537"),
       "Lego Kpop Demon Hunters Derpy Tiger", "leading set number removed")
    eq(clean_name("LEGO® Star Wars™ Offworld Sandcrawler and Mudhorn Set 75453", "75453"),
       "LEGO® Star Wars™ Offworld Sandcrawler and Mudhorn",
       "trailing 'Set NNNNN' removed")
    eq(clean_name("LEGO City Police Car Kit 94 Pieces - 60312", "60312"),
       "LEGO City Police Car Kit 94 Pieces",
       "trailing '- NNNNN' removed, piece count kept")
    eq(clean_name("LEGO Technic 2 Fast 2 Furious Nissan Skyline 42210", "42210"),
       "LEGO Technic 2 Fast 2 Furious Nissan Skyline",
       "numbers that are part of the NAME survive")
    eq(clean_name("Technic Porsche 42176 Building Kit (834 Pieces)", "42176"),
       "Technic Porsche Building Kit (834 Pieces)",
       "mid-title set number removed, piece count kept")
    eq(clean_name("LEGO Something 12345", "99999"),
       "LEGO Something 12345", "a NON-matching number is never stripped")
    eq(clean_name("", "42176"), "", "empty title survives")


def test_catalog_enrichment():
    """Tiles show the best price known from ANY retailer, not just the
    catalogue's own store — and only demote sets actually checked."""
    print("\ncatalogue enrichment")
    import tempfile
    from pathlib import Path as _P
    from legotracker.catalog import enrich
    from legotracker.db import Database as _DB

    with tempfile.TemporaryDirectory() as tmp:
        db = _DB(_P(tmp) / "e.db")
        for sn, name in [("42176", "Technic Porsche 42176"),
                         ("60312", "City Police Car 60312"),
                         ("71806", "NINJAGO Mech 71806")]:
            db.upsert_set(sn, name=name)

        def obs(sn, retailer, price, stock=True, mrp=None):
            lid = db.upsert_listing(sn, retailer, f"https://{retailer}/{sn}",
                                    title=f"LEGO {sn}")
            db.record_price(lid, price, mrp_inr=mrp, in_stock=stock)

        # 42176 seen at three retailers; Toycra cheapest.
        obs("42176", "mybrickhouse", 15999.0, mrp=17999.0)
        obs("42176", "toycra", 14899.0, mrp=17999.0)
        obs("42176", "amazon_in", 15499.0)
        # 60312 checked twice, out of stock at both.
        obs("60312", "mybrickhouse", 999.0, stock=False)
        obs("60312", "firstcry", 949.0, stock=False)
        # 71806 never checked anywhere.

        items = [
            {"set_num": "42176", "name": "Technic Porsche 42176", "pieces": 834,
             "price": 15999.0, "mrp": 17999.0, "in_stock": True},
            {"set_num": "60312", "name": "City Police Car 60312", "pieces": 94,
             "price": 999.0, "mrp": None, "in_stock": False},
            {"set_num": "71806", "name": "NINJAGO Mech 71806", "pieces": 235,
             "price": 2290.0, "mrp": 2490.0, "in_stock": True},
        ]
        out = {r["set_num"]: r for r in enrich(db, items)}

        a = out["42176"]
        eq(a["best"]["retailer"], "toycra", "cheapest across retailers wins")
        eq(a["show_price"], 14899.0, "tile price is the cross-retailer low")
        eq(a["best"]["n"], 3, "reports how many retailers were checked")
        eq(a["discount_pct"], 17, "discount computed off the shown price")
        eq(a["per_piece"], 17.86, "per-piece uses the best price, not the store's")
        eq(a["out_everywhere"], False, "in stock somewhere")
        eq(a["display_name"], "Technic Porsche", "name cleaned for display")

        b = out["60312"]
        eq(b["out_everywhere"], True, "out of stock at every retailer checked")
        eq(b["show_price"], 949.0, "still shows the cheapest seen")

        c = out["71806"]
        eq(c["best"], None, "never checked -> no cross-retailer best")
        eq(c["show_price"], 2290.0, "falls back to the catalogue's own price")
        eq(c["out_everywhere"], False,
           "an unchecked set is UNKNOWN, not demoted as unavailable")
        eq(c["discount_pct"], 8, "fallback still shows the store's discount")
        db.close()


def test_lego_catalog_regex():
    print("\nLEGO.com IN catalog regex")
    sample = ('<a href="/en-in/product/ninjago-city-workshops-71837">x</a>'
              '<a href="/en-in/product/lloyd-vs-earth-monster-spinner-71850">y</a>'
              '<a href="/en-in/product/ninjago-stronger-together-wallet-5009548">z</a>')
    hits = [(m.group(1), m.group(2)) for m in PRODUCT_HREF_RE.finditer(sample)]
    eq(len(hits), 3, "all product links found")
    eq(hits[0][1], "71837", "set number is the trailing segment")
    eq(hits[1][0], "lloyd-vs-earth-monster-spinner", "name slug captured")
    check(getattr(LegoIN, "catalog_only", False),
          "LEGO.com is flagged catalog-only (it publishes no prices in HTML)")


# --------------------------------------------------------------- analytics

def _series(prices):
    now = datetime.now(timezone.utc)
    return [{"observed_at": (now - timedelta(days=len(prices) - 1 - i)).isoformat(),
             "price_inr": p} for i, p in enumerate(prices)]


def test_stats_and_scoring():
    print("\nanalytics")
    stats = compute_stats(_series([3000, 3000, 2900, 3100, 3000, 2400]))
    eq(stats.n, 6, "all observations counted")
    eq(stats.low, 2400.0, "all-time low")
    eq(stats.current, 2400.0, "latest is current")
    check(stats.median_30d == 3000.0, f"30-day median ({stats.median_30d})")

    v = score_deal(stats, mrp=4000)
    check(v.is_all_time_low, "all-time low flagged")
    check(v.score >= 80, f"a real drop scores as a strong buy ({v.score})")
    check(v.vs_typical_pct and v.vs_typical_pct > 15,
          f"~19% below typical ({v.vs_typical_pct})")

    flat = compute_stats(_series([2000, 2000, 2000, 2000, 2000]))
    fv = score_deal(flat, mrp=5000)
    check(fv.score < 70,
          f"a fake 60%-off-MRP with no history movement is not a strong buy ({fv.score})")
    check("off listed MRP" in " ".join(fv.reasons), "MRP still reported, just down-weighted")

    thin = score_deal(compute_stats(_series([1500, 1400])))
    eq(thin.label, "Watching", "thin history refuses to give a verdict")
    eq(thin.confidence, "low", "confidence reported as low")

    rising = score_deal(compute_stats(_series([2000, 2000, 2000, 2000, 2600])))
    check(rising.score < 45, f"a price rise scores badly ({rising.score})")

    tgt = score_deal(compute_stats(_series([3000, 3000, 3000, 2500])), target=2600)
    check(tgt.score >= 85, "user target overrides the model")


def test_score_curve():
    """Regression guard: the score must rise monotonically with the discount and
    must not call a trivial dip a strong buy (it did, before the percentile was
    changed to exclude today's own price)."""
    print("\nscore curve")
    import random
    random.seed(3)
    base = [round(3000 * random.uniform(0.97, 1.03)) for _ in range(40)]

    def s(today):
        return score_deal(compute_stats(_series(base + [today])), mrp=4499)

    scores = [s(t).score for t in (3600, 3300, 3000, 2800, 2600, 2400, 2100)]
    check(all(a <= b for a, b in zip(scores, scores[1:])),
          f"score rises monotonically as price falls {scores}")
    check(s(3600).label == "Overpriced", "20% above typical is Overpriced")
    check(s(3000).label == "Fair", "at the median is Fair")
    check(s(2900).score < 80,
          f"a 3.6% dip is not a strong buy ({s(2900).score})")
    check(s(2600).label == "Strong buy",
          f"a 14% discount at a new floor is a strong buy ({s(2600).score})")
    check(len(set(scores)) > 4, "the score discriminates rather than saturating")


def test_alerts():
    print("\nalerts")
    stats = compute_stats(_series([3000, 3000, 3000, 3000, 2100]))
    alerts = detect_alerts("71806", 1, "Amazon.in", stats, prev_price=3000,
                           target=2500)
    kinds = {a["kind"] for a in alerts}
    check("all_time_low" in kinds, "all-time-low alert raised")
    check("drop" in kinds, "30% drop alert raised")
    check("target_hit" in kinds, "target alert raised")

    quiet = detect_alerts("71806", 1, "Amazon.in",
                          compute_stats(_series([3000, 3000, 3000, 2990])),
                          prev_price=3000)
    eq(len(quiet), 0, "a trivial wobble raises nothing")


# ---------------------------------------------------------------- database

def test_parallel_throttle():
    """The per-host rate limit must not serialise different hosts.

    Holding the lock across the sleep made a six-retailer fan-out run one host
    at a time — a 3-second search became a 20-second one.
    """
    print("\nparallel throttle")
    import time as _t
    from concurrent.futures import ThreadPoolExecutor
    from legotracker.http_client import PoliteSession

    s = PoliteSession(min_interval=1.0, jitter=0.0)
    hosts = [f"host{i}.example" for i in range(6)]

    t0 = _t.monotonic()
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(s._wait_turn, hosts))
    elapsed = _t.monotonic() - t0
    check(elapsed < 0.5,
          f"six different hosts pass through concurrently ({elapsed:.2f}s)")

    # The same host still has to wait its turn.
    t0 = _t.monotonic()
    s._wait_turn("host0.example")
    same_host = _t.monotonic() - t0
    check(same_host >= 0.8,
          f"a repeat hit on one host is still throttled ({same_host:.2f}s)")


def test_accept_encoding_matches_decoders():
    """Regression: we must never advertise a Content-Encoding we cannot decode.

    Hardcoding 'gzip, deflate, br' without the optional `brotli` package made
    urllib3 hand back UNDECODED brotli bytes. Nothing raised — BeautifulSoup
    found zero products and json.loads failed, so every retailer silently
    reported 'no match' and the whole app looked unable to fetch data.
    """
    print("\nAccept-Encoding vs installed decoders")
    import requests
    from urllib3.response import HTTPResponse
    from legotracker.http_client import BASE_HEADERS, PoliteSession

    check("Accept-Encoding" not in BASE_HEADERS,
          "we do not override Accept-Encoding by hand")

    advertised = {e.strip().lower().split(";")[0]
                  for e in PoliteSession().session.headers
                  .get("Accept-Encoding", "").split(",") if e.strip()}
    decodable = {d.lower() for d in getattr(HTTPResponse, "CONTENT_DECODERS", [])}
    decodable |= {"identity", "x-gzip"}
    unsupported = advertised - decodable
    check(not unsupported,
          f"every advertised encoding is decodable (advertised={sorted(advertised)}, "
          f"unsupported={sorted(unsupported)})")


def test_undecoded_body_is_loud():
    print("\nundecoded body detection")
    from legotracker.http_client import PoliteSession

    class Resp:
        def __init__(self, enc, content):
            self.headers = {"Content-Encoding": enc} if enc else {}
            self.content = content

    s = PoliteSession()
    # Raw brotli bytes still labelled 'br' -> must be reported, not parsed.
    eq(s._undecoded_encoding(Resp("br", b"\x1b\x8b\x10\x00rest")), "br",
       "still-compressed brotli body detected")
    eq(s._undecoded_encoding(Resp("zstd", b"\x28\xb5\x2f\xfdrest")), "zstd",
       "still-compressed zstd body detected")
    eq(s._undecoded_encoding(Resp("gzip", b"<html>")), None,
       "gzip is always decodable")
    eq(s._undecoded_encoding(Resp("", b"<html>")), None,
       "no encoding header is fine")


def test_session_is_per_thread():
    """requests.Session is not thread-safe and the search uses six threads."""
    print("\nthread-local HTTP sessions")
    from concurrent.futures import ThreadPoolExecutor
    from legotracker.http_client import PoliteSession

    import threading
    s = PoliteSession()
    # A barrier forces all six to be in flight at once; pool.map alone reuses
    # idle threads and would not prove anything.
    barrier = threading.Barrier(6)

    def grab(_):
        barrier.wait(timeout=5)
        return id(s.session)

    with ThreadPoolExecutor(max_workers=6) as pool:
        ids = list(pool.map(grab, range(6)))
    check(len(set(ids)) == 6,
          f"six concurrent threads get six distinct Sessions ({len(set(ids))}/6)")

    uas = {s.session.headers["User-Agent"]}
    with ThreadPoolExecutor(max_workers=3) as pool:
        uas |= set(pool.map(lambda _: s.session.headers["User-Agent"], range(3)))
    eq(len(uas), 1, "all threads share one User-Agent")


def test_amazon_search_url():
    print("\nAmazon search URL")
    import inspect
    from legotracker.sources.amazon_in import AmazonIN
    # Check the URL the code actually builds, not the comments around it.
    src = inspect.getsource(AmazonIN.search)
    url_lines = [ln for ln in src.splitlines()
                 if "url = " in ln and not ln.strip().startswith("#")]
    eq(len(url_lines), 1, "one search URL is constructed")
    check("i=toys" not in url_lines[0],
          f"no unverified department filter in the URL ({url_lines[0].strip()})")
    check("/s?k=" in url_lines[0],
          "uses the search endpoint verified against the live site")


def test_search_matching():
    """The whole point of the search engine: fuzzy retailer results must not
    become a tidy comparison table of six different products."""
    print("\nsearch matching")
    from legotracker.search import _absorb, _identify, SetResult
    from legotracker.sources.base import Offer

    def mk(retailer, title, price):
        return Offer(retailer=retailer, url=f"https://{retailer}/x/{price}",
                     title=title, price_inr=price, in_stock=True,
                     brand="LEGO" if "lego" in title.lower() else None)

    # What the six retailers actually returned for the query "71806".
    hits = [
        mk("toycra", "Lego 71814 Ninjago Tournament Temple City (3489 Pieces)", 18499),
        mk("hamleys", "LEGO Icons Blacktron Renegade 10355, Building Blocks 18Y+", 9099),
        mk("firstcry", "LEGO NINJAGO Dragon Spinjitzu Battle Pack 186 Pieces - 71826", 1499),
        mk("amazon_in", "LEGO NINJAGO Cole's Elemental Earth Mech Toy 71806 (235 Pieces)", 2175),
        mk("flipkart", "LEGO NINJAGO Cole's Elemental Earth Mech Toy 71806 ( 235 Pieces)", 2290),
    ]

    by_set: dict = {}
    kept = _absorb(hits, "71806", by_set)
    eq(kept, 2, "only the two genuine 71806 listings kept")
    eq(sorted(by_set), ["71806"], "no other set was filed")
    eq(len(by_set["71806"].offers), 2, "both retailers carrying it are present")

    # Free-text mode groups everything it can confidently identify.
    by_set2: dict = {}
    kept2 = _absorb(hits, None, by_set2)
    eq(kept2, 5, "text mode identifies every genuine LEGO listing")
    eq(len(by_set2), 4, "grouped into four distinct sets")

    # An accessory carrying a set number is never filed.
    junk = [mk("toycra", "Acrylic Display Stand for Lego 71806 Earth Mech", 899)]
    by_set3: dict = {}
    eq(_absorb(junk, "71806", by_set3), 0, "display stand rejected in search too")


def test_search_deadline():
    """One hanging retailer must not hold the whole page.

    Before the deadline existed, a retailer hitting a bot wall would retry with
    escalating backoff — around two minutes of spinner with nothing rendered.
    """
    print("\nsearch deadline")
    import time as _t
    from legotracker.sources import SOURCE_CLASSES
    from legotracker.sources.base import Offer
    from legotracker import search as search_mod

    originals = {n: c.search for n, c in SOURCE_CLASSES.items()}

    def stub(name, secs):
        def _s(self, q, limit=24):
            _t.sleep(secs)
            return [Offer(retailer=name, url=f"https://{name}/p",
                          title="LEGO Technic Mercedes-AMG F1 W14 42171",
                          price_inr=19999, in_stock=True, brand="LEGO")]
        return _s

    try:
        for n in SOURCE_CLASSES:
            SOURCE_CLASSES[n].search = stub(n, 60 if n == "amazon_in" else 0.2)

        t0 = _t.monotonic()
        # BuyHatke, Toycra and Jaiman Toys are the default sources now (see
        # sources/__init__.py), so this test explicitly asks for every
        # registered adapter -- the deadline/cooldown mechanism being tested
        # here is generic over however many sources are active, default or
        # not.
        out = search_mod.search("42171", deadline=4.0, refine=True,
                                sources=list(SOURCE_CLASSES))
        elapsed = _t.monotonic() - t0

        check(elapsed < 7.0,
              f"returns near the deadline, not the slow retailer ({elapsed:.1f}s)")
        eq(len(out.sets), 1, "the set is still resolved from the retailers that answered")
        fast_ok = [s for s in out.statuses if s.matched]
        check(len(fast_ok) >= 4,
              f"the retailers that answered are all reported ({len(fast_ok)})")
        stalled = [s for s in out.statuses if s.retailer == "amazon_in"]
        check(stalled and stalled[0].error and "timed out" in stalled[0].error,
              "the stalled retailer is reported as timed out, not as 'no match'")
    finally:
        for n, fn in originals.items():
            SOURCE_CLASSES[n].search = fn


def test_search_result_shape():
    print("\nsearch result shape")
    from legotracker.search import SetResult
    from legotracker.sources.base import Offer

    def mk(r, price, stock=True):
        return Offer(retailer=r, url=f"https://{r}/p", title="LEGO X 71806",
                     price_inr=price, in_stock=stock)

    res = SetResult("71806", "LEGO X 71806", [
        mk("amazon_in", 2175), mk("flipkart", 2290),
        mk("toycra", 1999, stock=False), mk("hamleys", 2899)])
    eq(res.best.retailer, "amazon_in",
       "cheapest IN-STOCK offer wins (the ₹1,999 one is out of stock)")
    eq(res.spread, 33.3, "spread ignores out-of-stock offers")

    lone = SetResult("71806", "x", [mk("amazon_in", 2175)])
    eq(lone.spread, None, "a single offer has no spread")

    nothing = SetResult("71806", "x", [mk("amazon_in", None, stock=True)])
    eq(nothing.best, None, "an unpriced offer is not a best price")


def test_database_roundtrip():
    print("\ndatabase")
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "t.sqlite3")
        db.upsert_set("71806", name="Cole's Elemental Earth Mech", pieces=235)
        db.upsert_set("71806", theme="NINJAGO")            # enrich, don't clobber
        row = db.all_sets()[0]
        eq(row["name"], "Cole's Elemental Earth Mech", "name preserved across upserts")
        eq(row["theme"], "NINJAGO", "theme added by the second upsert")

        db.upsert_set("71806", name=None)
        eq(db.all_sets()[0]["name"], "Cole's Elemental Earth Mech",
           "a NULL never overwrites a real value")

        lid = db.upsert_listing("71806", "amazon_in", "https://x/dp/A1",
                                title="LEGO NINJAGO ... 71806")
        lid2 = db.upsert_listing("71806", "amazon_in", "https://x/dp/A1",
                                 title="LEGO NINJAGO ... 71806")
        eq(lid, lid2, "same url upserts to the same listing row")

        db.record_price(lid, 2175.0, mrp_inr=2290.0, in_stock=True)
        db.record_price(lid, 1999.0, mrp_inr=2290.0, in_stock=True)
        eq(len(db.history(lid)), 2, "history is append-only")
        eq(db.latest_price(lid)["price_inr"], 1999.0, "latest price is the newest row")

        db.set_target("71806", 2000)
        eq(db.targets(), {"71806": 2000.0}, "target stored")

        # Regression: the threaded search server opens the connection on the
        # main thread and writes from request workers.
        from concurrent.futures import ThreadPoolExecutor
        errors: list[str] = []

        def write(i):
            try:
                db.record_price(lid, 1000.0 + i, in_stock=True)
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))

        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(write, range(12)))
        check(not errors, f"writes from other threads succeed ({errors[:1]})")
        eq(len(db.history(lid)), 14, "every threaded write landed")

        rid = db.start_run()
        db.finish_run(rid, "ok", 1, 2)
        eq(db.recent_runs()[0]["status"], "ok", "run recorded")
        db.close()


def test_deep_enrich_and_backfill():
    """The composition logic behind `deep=True` (catalog.py's background
    sweep): one BuyHatke detail fetch per offer should (a) fold in other
    retailers' prices for the same product as new offers, deduped against
    what is already there, and (b) backfill that product's price history
    once listings exist to attach it to.
    """
    print("\ndeep enrich + history backfill")
    from legotracker import search as search_mod
    from legotracker.search import SearchOutcome, SetResult
    from legotracker.sources.base import Offer

    amazon_url = "http://www.amazon.in/gp/product/B0CQ3VY8X2"
    outcome = SearchOutcome(query="71813", kind="set_number", sets=[
        SetResult("71813", "LEGO NINJAGO Wolf Mask Shadow Dojo 71813", [
            Offer(retailer="amazon_in", url=amazon_url,
                  title="LEGO NINJAGO Wolf Mask Shadow Dojo 71813",
                  price_inr=13499.0, in_stock=True, brand="LEGO"),
        ]),
    ])

    detail_data = {
        "dealsData": {"dealsList": [
            # A genuinely new retailer for this product -- should become a
            # new offer on the same SetResult.
            {"site_name": "Flipkart", "link": "https://www.flipkart.com/p/x",
             "price": 12999, "mrpFloat": 13999, "prod": "LEGO X flipkart"},
            # An unmapped site (see DOMAIN_TO_RETAILER) -- must be skipped,
            # not invented as a new retailer identity.
            {"site_name": "Ajio", "link": "https://www.ajio.com/p/x",
             "price": 11999, "mrpFloat": 12999, "prod": "LEGO X ajio"},
            # Same URL as the offer already on this SetResult -- must not
            # be duplicated.
            {"site_name": "Amazon", "link": amazon_url,
             "price": 13499, "mrpFloat": 13999, "prod": "dup"},
        ]},
        "predictedData": {"history": [
            {"from": "2024-04-19 05:05:31", "to": "2024-04-19 05:05:31", "price": 33500},
            {"from": "2026-09-14 07:00:24", "to": "2026-09-14 07:00:24", "price": 13499},
        ]},
    }

    calls = []

    def fake_fetch_detail(session, url):
        calls.append(url)
        return detail_data

    original = search_mod.buyhatke_source.fetch_detail
    search_mod.buyhatke_source.fetch_detail = fake_fetch_detail
    try:
        detail_by_url = search_mod._deep_enrich(session=None, outcome=outcome)
    finally:
        search_mod.buyhatke_source.fetch_detail = original

    eq(len(calls), 1, "one detail fetch for the one offer that was present")
    eq(list(detail_by_url), [amazon_url], "keyed by the real vendor URL")

    res = outcome.sets[0]
    eq(len(res.offers), 2, "flipkart's deal was folded in; Ajio and the duplicate were not")
    added = res.offers[1]
    eq(added.retailer, "flipkart", "new offer resolved to the real retailer")
    eq(added.url, "https://www.flipkart.com/p/x", "new offer keeps the real vendor link")
    eq(added.price_inr, 12999.0, "new offer's price carried over")
    eq(added.mrp_inr, 13999.0, "new offer's mrp carried over")

    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "t.sqlite3")
        search_mod._persist(db, outcome)
        search_mod._backfill_all_history(db, outcome, detail_by_url)

        amazon_lid = db.upsert_listing("71813", "amazon_in", amazon_url)
        rows = db.history(amazon_lid)
        eq(len(rows), 3, "today's persisted price plus two backfilled history points")
        prices = sorted(r["price_inr"] for r in rows)
        eq(prices, [13499.0, 13499.0, 33500.0], "backfilled prices match BuyHatke's history")

        flipkart_lid = db.upsert_listing("71813", "flipkart", "https://www.flipkart.com/p/x")
        eq(len(db.history(flipkart_lid)), 1,
           "flipkart's own detail page was never fetched, so it has no history to backfill")

        # Backfilling again must not duplicate rows already recorded.
        search_mod._backfill_all_history(db, outcome, detail_by_url)
        eq(len(db.history(amazon_lid)), 3, "re-running the backfill is a no-op (deduped by timestamp)")
        db.close()


def test_deep_enrich_skips_direct_sources_and_respects_cooldown():
    """Two failure modes seen in a real GitHub Actions run, both from the
    same cause: `_deep_enrich` tried to ask BuyHatke about offers it was
    never going to know about.

    1. Toycra/Jaiman Toys offers got proxied through buyhatke.com/<url> --
       a guaranteed 403 on every single catalogue set, since BuyHatke
       doesn't track either store.
    2. BuyHatke's own search was blocked (HTTP 403) but that wasn't treated
       as a block worth cooling down for, so it kept retrying and failing on
       every one of 900+ sets instead of pausing.
    """
    print("\ndeep enrich skips toycra/jaimantoys and respects cooldown")
    from legotracker import search as search_mod
    from legotracker.search import SearchOutcome, SetResult, cooling_down, reset_retailer_state, start_cooldown
    from legotracker.sources.base import Offer
    from legotracker.sources.buyhatke import BuyHatke

    eq(search_mod.is_block_error("HTTP 403 for https://buyhatke.com/search?product=x"), True,
       "a 403 is treated as a block, not a one-off error -- it always means a bot wall here")

    reset_retailer_state()
    try:
        # 1. A toycra offer must never be proxied through BuyHatke.
        outcome = SearchOutcome(query="30724", kind="set_number", sets=[
            SetResult("30724", "LEGO X 30724", [
                Offer(retailer="toycra", url="https://toycra.com/products/x",
                      title="LEGO X", price_inr=499.0, in_stock=True),
            ]),
        ])
        calls = []
        original_fetch = search_mod.buyhatke_source.fetch_detail
        search_mod.buyhatke_source.fetch_detail = lambda session, url: calls.append(url) or {}
        try:
            detail_by_url = search_mod._deep_enrich(session=None, outcome=outcome)
        finally:
            search_mod.buyhatke_source.fetch_detail = original_fetch
        eq(calls, [], "a toycra offer is never looked up on BuyHatke")
        eq(detail_by_url, {}, "nothing to enrich from")

        # 2. Once BuyHatke is cooling down, no further detail fetch is even
        #    attempted -- not for the set that tripped it, nor any after.
        start_cooldown(BuyHatke.name, "HTTP 403 for https://buyhatke.com/search?product=x")
        check(cooling_down(BuyHatke.name) is not None, "cooldown is active")

        outcome2 = SearchOutcome(query="71813", kind="set_number", sets=[
            SetResult("71813", "LEGO X 71813", [
                Offer(retailer="amazon_in", url="http://www.amazon.in/gp/product/X",
                      title="LEGO X", price_inr=1999.0, in_stock=True),
            ]),
        ])
        calls2 = []
        original_fetch2 = search_mod.buyhatke_source.fetch_detail
        search_mod.buyhatke_source.fetch_detail = lambda session, url: calls2.append(url) or {}
        try:
            detail_by_url2 = search_mod._deep_enrich(session=None, outcome=outcome2)
        finally:
            search_mod.buyhatke_source.fetch_detail = original_fetch2
        eq(calls2, [], "no detail fetch is attempted while BuyHatke is cooling down")
        eq(detail_by_url2, {}, "nothing to enrich while cooling down")
    finally:
        reset_retailer_state()


def test_dashboard_renders():
    print("\ndashboard")
    from legotracker.report import render
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "t.sqlite3")
        db.upsert_set("71806", name="Cole's Elemental Earth Mech",
                      theme="NINJAGO", pieces=235)
        lid = db.upsert_listing("71806", "amazon_in", "https://x/dp/A1",
                                title="LEGO NINJAGO 71806")
        for p in (2400, 2300, 2290, 2175, 1999):
            db.record_price(lid, float(p), mrp_inr=2290.0, in_stock=True)
        out = render(db, Path(tmp) / "d.html")
        html = out.read_text()
        check(out.exists() and len(html) > 8000, "dashboard file written")
        check("__PAYLOAD__" not in html, "payload substituted")
        check('"set_num": "71806"' in html, "set present in payload")
        check("Cole&#x27;s" in html or "Cole's" in html, "name present")
        check(html.count("<script") == 1, "single self-contained script block")
        db.close()



# =========================================================== static site ===
#
# The public website is generated, published and then unmaintained until
# something breaks. These tests cover the two categories of failure that would
# be worst in that setting: claims the data does not support, and untrusted
# text reaching a page unescaped.


def _site_fixture(tmp, *, days=12, with_prices=True, catalog_price=True):
    """Build a small site into `tmp` and return (out_dir, items, observations)."""
    from legotracker.export import export_all
    from legotracker.site import build_site

    db = Database(Path(tmp) / "t.sqlite3")
    now = datetime.now(timezone.utc)
    db.replace_catalog([
        {"set_num": "42176", "name": "LEGO Technic Porsche GT4 42176 (834 Pieces)",
         "theme": "Technic", "pieces": 834,
         "image": "https://cdn.shopify.com/x/porsche.jpg",
         "url": "https://lego.mybrickhouse.com/products/42176",
         "price": 15999.0 if catalog_price else None, "mrp": 17999.0,
         "in_stock": True},
        {"set_num": "60312", "name": '<script>alert("xss")</script> & Co 60312',
         "theme": "City", "pieces": 94,
         "image": "javascript:alert(1)",
         "url": "https://evil.example.com/steal",
         "price": 999.0 if catalog_price else None, "mrp": 1099.0,
         "in_stock": True},
        {"set_num": "99999", "name": "LEGO Never Priced 99999",
         "theme": "Icons", "pieces": 100, "image": None, "url": None,
         "price": None, "mrp": None, "in_stock": True},
    ])

    if with_prices:
        db.upsert_set("42176")
        for retailer, base in (("amazon_in", 15499.0), ("toycra", 14899.0)):
            lid = db.upsert_listing("42176", retailer,
                                    f"https://{retailer.replace('_', '.')}/p/42176",
                                    title="LEGO 42176")
            for d in range(days, -1, -1):
                with db.tx() as c:
                    c.execute(
                        """INSERT INTO price_points
                             (listing_id, observed_at, price_inr, mrp_inr, in_stock)
                           VALUES (?,?,?,?,?)""",
                        (lid, (now - timedelta(days=d)).isoformat(),
                         base - d * 5, 17999.0, 1))

    data_dir = Path(tmp) / "data"
    export_all(db, data_dir)
    out = Path(tmp) / "site"
    build_site(data_dir, Path(tmp) / "content", out)
    db.close()
    return out


def test_site_money_and_freshness():
    """Indian digit grouping, and staleness that is never silently hidden."""
    print("\nsite formatting")
    from legotracker.site import freshness, rupees

    eq(rupees(999), "\u20b9999", "under a thousand")
    eq(rupees(15999), "\u20b915,999", "thousands")
    eq(rupees(129999), "\u20b91,29,999", "lakh grouping, not 129,999")
    eq(rupees(12345678), "\u20b91,23,45,678", "crore grouping")
    eq(rupees(None), "\u2014", "missing price is a dash, never zero")
    eq(rupees(18.1, decimals=True), "\u20b918.10", "per-piece keeps paise")

    now = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
    def at(days=0, hours=0, minutes=0):
        return freshness(
            (now - timedelta(days=days, hours=hours, minutes=minutes)).isoformat(), now)

    eq(at(hours=1)["level"], "fresh", "an hour old is fresh")
    eq(at(minutes=30)["text"], "30 minutes ago", "under 90 minutes counts in minutes")
    eq(at(minutes=100)["text"], "1 hour ago", "singular hour, not '1 hours'")
    eq(at(hours=5)["text"], "5 hours ago", "plural hours")
    eq(at(days=1)["text"], "yesterday", "one day reads as yesterday")
    eq(at(days=9)["level"], "stale", "nine days is flagged stale")
    eq(at(days=40)["level"], "old", "forty days is flagged old")
    eq(freshness(None, now)["text"], "never checked", "no timestamp says so")
    eq(freshness("not a date", now)["level"], "unknown", "garbage date is survivable")


def test_site_link_and_image_allowlist():
    """A poisoned database row must not become a link on a public page."""
    print("\nsite link safety")
    from legotracker.site import safe_image, safe_url

    check(safe_url("https://www.amazon.in/dp/B0C") != "", "known retailer allowed")
    check(safe_url("https://lego.mybrickhouse.com/products/x") != "",
          "subdomain of an allowed host")
    eq(safe_url("https://evil.example.com/x"), "", "unknown host rejected")
    eq(safe_url("https://amazon.in.evil.com/x"), "",
       "suffix lookalike rejected, not matched as amazon.in")
    eq(safe_url("http://www.amazon.in/x"), "", "plain http rejected")
    eq(safe_url("javascript:alert(1)"), "", "javascript: scheme rejected")
    eq(safe_url(None), "", "None is survivable")
    eq(safe_image("javascript:alert(1)"), "", "javascript: image rejected")
    eq(safe_image('https://x/y" onerror="alert(1)'), "",
       "quote-breaking image URL rejected")
    check(safe_image("https://cdn.shopify.com/a/b.jpg") != "", "https image allowed")


def test_site_escapes_untrusted_text():
    """Set names come from retailer listings. They are not ours and not trusted."""
    print("\nsite escaping")
    with tempfile.TemporaryDirectory() as tmp:
        out = _site_fixture(tmp)
        page = (out / "set" / "60312" / "index.html").read_text()
        check("<script>alert(" not in page, "script tag from a set name is escaped")
        check("&lt;script&gt;" in page, "…and appears as visible text instead")
        check("&amp;" in page, "ampersand escaped")
        check("evil.example.com" not in page,
              "catalogue URL outside the allowlist never reaches the page")
        check("javascript:" not in page, "javascript: image src never emitted")

        ld = page.split('application/ld+json">')[1].split("</script>")[0] if \
            "application/ld+json" in page else "{}"
        check("<" not in ld and ">" not in ld,
              "angle brackets inside JSON-LD are unicode-escaped")
        import json as _json
        _json.loads(ld)
        check(True, "…and the result is still valid JSON")

        # The search index is served as a standalone JSON file and rendered with
        # textContent, so escaping it would be theatre. The property that
        # actually matters is that the script never builds HTML from it.
        js = (out / "assets" / "site.js").read_text()
        check("innerHTML" not in js, "site.js never assigns innerHTML")
        _json.loads((out / "search-index.json").read_text())
        check(True, "search index is valid JSON")


def test_site_never_claims_history_it_lacks():
    print("\nsite honesty")
    with tempfile.TemporaryDirectory() as tmp:
        out = _site_fixture(tmp, days=1)          # 2 days — below the threshold
        page = (out / "set" / "42176" / "index.html").read_text()
        check("<svg" not in page, "no chart when there isn't enough history for one")
        check("all-time" not in page.lower(), "…and claims no all-time low")
        check("Not enough data yet" in page,
              "the set page says why, instead of just omitting the section")

    with tempfile.TemporaryDirectory() as tmp:
        out = _site_fixture(tmp, days=40)
        page = (out / "set" / "42176" / "index.html").read_text()
        # Wired in for real now that BuyHatke's own multi-year series gives
        # most tracked sets ample history from day one (see history_block's
        # docstring) — a set page with enough history shows the chart.
        check("<svg" in page,
              "price history chart is shown once there is enough history")
        check("Lowest since we started tracking" in page,
              "the all-time-low fact is worded as since-we-started")
        check("all-time low" not in page.lower(),
              "…and never claims a true all-time low")

    from datetime import datetime as _dt, timedelta as _td
    from legotracker.site import history_block, analyse_set
    # The function itself still works correctly — it's just not wired in.
    start = _dt(2026, 1, 1)
    obs = [{"retailer": "amazon_in",
            "observed_at": (start + _td(days=d)).isoformat(),
            "price_inr": 15000.0 + d, "mrp_inr": 17999.0, "in_stock": True}
           for d in range(40)]
    item = {"set_num": "42176", "pieces": 3696}
    analysis = analyse_set(item, obs)
    block = history_block(analysis)
    check("<svg" in block, "history_block itself still draws a chart when called")
    check("Lowest since we started tracking" in block,
          "all-time low is worded as since-we-started")
    check("all-time low" not in block.lower(),
          "…and never as a true all-time low")


def test_site_distinguishes_unchecked_from_unavailable():
    """The core promise: 'nobody has it' and 'we didn't look' are different."""
    print("\nsite unchecked vs unavailable")
    with tempfile.TemporaryDirectory() as tmp:
        out = _site_fixture(tmp, with_prices=False, catalog_price=False)
        page = (out / "set" / "42176" / "index.html").read_text()
        check("We haven&#x27;t priced this set yet" in page
              or "We haven't priced this set yet" in page,
              "never-checked set says so")
        check("No retailer we checked has it in stock" not in page,
              "…and does not claim it is unavailable")
        check("Not checked yet" not in page,
              "unchecked retailers are left out of the table, not listed as such")
        check("No retailer has been checked for this set yet" in page,
              "…and the empty table says so plainly instead")

    with tempfile.TemporaryDirectory() as tmp:
        out = _site_fixture(tmp, with_prices=False, catalog_price=True)
        page = (out / "set" / "42176" / "index.html").read_text()
        check("LEGO Certified Store" in page and "15,999" in page,
              "catalogue price is shown rather than silently dropped")


def test_site_structure_and_relative_links():
    print("\nsite structure")
    with tempfile.TemporaryDirectory() as tmp:
        out = _site_fixture(tmp)
        for path in ("index.html", "browse/index.html", "builds/index.html",
                     "about/index.html", "404.html", "robots.txt",
                     "search-index.json", ".nojekyll",
                     "assets/site.css", "assets/site.js",
                     "set/42176/index.html"):
            check((out / path).exists(), f"emitted {path}")

        home = (out / "index.html").read_text()
        deep = (out / "set" / "42176" / "index.html").read_text()
        check('href="browse/"' in home, "home links to browse relatively")
        check('href="../../browse/"' in deep, "set page walks back up two levels")
        check('href="/browse/"' not in deep,
              "no root-absolute link — those break on a project Pages site")
        check('href="../../assets/site.css"' in deep, "stylesheet path is relative")

        import json as _json
        rows = _json.loads((out / "search-index.json").read_text())
        eq(len(rows), 3, "every set is searchable")
        check(all(set(r) == {"n", "t", "h", "p", "r"} for r in rows),
              "search index shape is stable")


def test_site_json_ld_only_when_backed_by_an_offer():
    print("\nsite structured data")
    with tempfile.TemporaryDirectory() as tmp:
        out = _site_fixture(tmp)
        priced = (out / "set" / "42176" / "index.html").read_text()
        check("application/ld+json" in priced, "priced set emits Product data")
        check('"priceCurrency": "INR"' in priced, "currency is rupees")

        unpriced = (out / "set" / "99999" / "index.html").read_text()
        check("application/ld+json" not in unpriced,
              "a set with no real offer claims no offer to Google")


def test_site_retailer_labels_match_the_adapters():
    """site.py duplicates the retailer labels to stay free of `requests`.

    That duplication is only safe while something checks it, which is this.
    """
    print("\nsite retailer table")
    from legotracker.site import RETAILERS
    from legotracker.sources import RETAILER_LABELS

    eq(set(RETAILERS), set(RETAILER_LABELS), "same retailers on both sides")
    for key, label in RETAILER_LABELS.items():
        eq(RETAILERS[key]["label"], label, f"{key} label matches its adapter")


def test_site_generator_imports_no_third_party():
    """The deploy workflow has no pip install. Keep it that way.

    The static check below is not enough on its own: it looks at four module
    files, and the thing that actually broke was `legotracker/__init__.py`
    importing requests, which every one of them triggers just by existing
    inside the package. So the real test is the runtime one — import the
    generator with third-party packages made unimportable and see if it
    survives.
    """
    print("\nsite dependency budget")
    import builtins
    import importlib

    blocked_rt = {"requests", "bs4", "lxml", "urllib3", "charset_normalizer"}
    for mod in [m for m in list(sys.modules)
                if m.split(".")[0] in {"legotracker"} | blocked_rt]:
        del sys.modules[mod]

    real_import = builtins.__import__
    hit = []

    def guard(name, *a, **k):
        if name.split(".")[0] in blocked_rt:
            hit.append(name)
            raise ImportError(f"{name} is not installed in the deploy workflow")
        return real_import(name, *a, **k)

    builtins.__import__ = guard
    try:
        importlib.import_module("legotracker.site")
        ok = True
    except Exception:
        ok = False
    finally:
        builtins.__import__ = real_import
        for mod in [m for m in list(sys.modules) if m.startswith("legotracker")]:
            del sys.modules[mod]

    check(ok, f"legotracker.site imports with no third-party packages "
              f"(tripped on: {hit[0] if hit else 'nothing'})")

    import ast
    blocked = {"requests", "bs4", "lxml", "urllib3"}
    for name in ("site", "content", "export", "analytics", "__init__"):
        tree = ast.parse((Path(__file__).resolve().parent.parent /
                          "legotracker" / f"{name}.py").read_text())
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                found.add(node.module.split(".")[0])
        eq(sorted(found & blocked), [], f"{name}.py imports nothing third-party")


def test_content_parser_is_forgiving():
    """A build post is edited in a browser at 11pm. It must fail softly."""
    print("\ncontent parsing")
    from legotracker.content import parse_build, parse_fields

    fields, body = parse_fields(
        "title: A Build\r\nstatus: COMPLETE\r\nphotos:\r\n  a.jpg\r\n"
        "  b.jpg\r\n---\r\nBody text.\r\n")
    eq(fields["title"], "A Build", "windows line endings survive")
    eq(fields["photos"], ["a.jpg", "b.jpg"], "indented lines become a list")
    eq(body, "Body text.", "body is separated at the divider")

    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        (d / "ok.md").write_text("title: Fine\nstatus: nonsense\nprogress: 900\n"
                                 "set: banana\n---\nhi")
        b = parse_build(d / "ok.md")
        eq(b.status, "building", "unknown status falls back instead of crashing")
        eq(b.progress, 100, "progress is clamped to 100")
        eq(b.set_num, None, "a non-numeric set number is dropped, not rendered")

        (d / "no-title.md").write_text("status: complete\n---\nbody")
        eq(parse_build(d / "no-title.md"), None, "a post with no title is skipped")

        (d / "no-divider.md").write_text("title: Still Works\nJust prose here.")
        b2 = parse_build(d / "no-divider.md")
        eq(b2.title, "Still Works", "a missing --- divider is not fatal")
        eq(b2.status, "building", "…and defaults are applied")


def test_export_roundtrip_is_stable():
    """Re-exporting unchanged data must produce an identical file.

    Otherwise every weekly `git commit` churns the whole history file and the
    repository grows for no reason.
    """
    print("\nexport stability")
    from legotracker.export import export_all, load_catalog, load_observations

    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "e.sqlite3")
        db.replace_catalog([{"set_num": "42176", "name": "LEGO Technic 42176",
                             "theme": "Technic", "pieces": 834,
                             "image": None, "url": None,
                             "price": 15999.0, "mrp": 17999.0, "in_stock": True}])
        db.upsert_set("42176")
        lid = db.upsert_listing("42176", "toycra", "https://toycra.com/p/42176")
        db.record_price(lid, 14899.0, mrp_inr=17999.0, in_stock=True)
        db.record_price(lid, None, in_stock=False)

        a, b = Path(tmp) / "a", Path(tmp) / "b"
        export_all(db, a)
        export_all(db, b)
        eq((a / "observations.csv").read_bytes(),
           (b / "observations.csv").read_bytes(), "observations export is byte-stable")
        eq((a / "catalog.json").read_bytes(),
           (b / "catalog.json").read_bytes(), "catalogue export is byte-stable")

        obs = load_observations(a)
        eq(len(obs["42176"]), 2, "both observations survive the round trip")
        priced = [o for o in obs["42176"] if o["price_inr"] is not None]
        eq(len(priced), 1, "the out-of-stock row is kept, with no price")
        eq(load_catalog(a)[0]["display_name"], "LEGO Technic",
           "display name is cleaned during export")
        db.close()


def test_daily_best_collapses_correctly():
    print("\ndaily best price")
    from legotracker.site import daily_best

    obs = [
        {"observed_at": "2026-09-01T10:00:00+00:00", "price_inr": 200.0, "in_stock": True},
        {"observed_at": "2026-09-01T18:00:00+00:00", "price_inr": 150.0, "in_stock": True},
        {"observed_at": "2026-09-01T19:00:00+00:00", "price_inr": 90.0,  "in_stock": False},
        {"observed_at": "2026-09-02T10:00:00+00:00", "price_inr": None,  "in_stock": True},
        {"observed_at": "2026-09-03T10:00:00+00:00", "price_inr": 175.0, "in_stock": True},
    ]
    series = daily_best(obs)
    eq([s["observed_at"] for s in series], ["2026-09-01", "2026-09-03"],
       "a day with no usable price produces no point — no interpolation")
    eq(series[0]["price_inr"], 150.0,
       "cheapest in-stock wins; the cheaper out-of-stock row is not a buyable price")


def main() -> int:
    for fn in (test_parse_inr, test_set_number_extraction, test_matching_rejects_junk,
               test_amazon_parser, test_flipkart_parser, test_firstcry_parser,
               test_shopify_parser, test_shopify_paise_conversion,
               test_hamleys_parser, test_buyhatke_parser, test_catalog_extraction,
               test_catalog_storage, test_catalog_name_cleanup,
               test_catalog_enrichment, test_lego_catalog_regex,
               test_stats_and_scoring, test_score_curve, test_alerts,
               test_parallel_throttle, test_accept_encoding_matches_decoders,
               test_undecoded_body_is_loud, test_session_is_per_thread,
               test_amazon_search_url, test_search_matching,
               test_search_deadline, test_search_result_shape,
               test_database_roundtrip, test_deep_enrich_and_backfill, test_deep_enrich_skips_direct_sources_and_respects_cooldown,
               test_dashboard_renders,
               test_site_money_and_freshness, test_site_link_and_image_allowlist,
               test_site_escapes_untrusted_text,
               test_site_never_claims_history_it_lacks,
               test_site_distinguishes_unchecked_from_unavailable,
               test_site_structure_and_relative_links,
               test_site_json_ld_only_when_backed_by_an_offer,
               test_site_retailer_labels_match_the_adapters,
               test_site_generator_imports_no_third_party,
               test_content_parser_is_forgiving,
               test_export_roundtrip_is_stable,
               test_daily_best_collapses_correctly):
        fn()
    print("\n" + "=" * 60)
    if _fails:
        print(f"{len(_fails)} FAILURE(S):")
        for f in _fails:
            print("  -", f)
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
