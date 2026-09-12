"""Amazon.in adapter.

Verified against the live site: the server-rendered HTML for /s?k=... does
contain `data-component-type="s-search-result"` cards with `a-price` blocks,
so a plain requests fetch is enough — no browser needed. The fragile parts are
(a) the title, which in the current layout is easiest to read off the image alt
text, and (b) Amazon's bot wall, which PoliteSession detects and reports.
"""

from __future__ import annotations

import logging
import re
from typing import Optional
from urllib.parse import quote_plus, urljoin

from bs4 import BeautifulSoup

from .base import Offer, Source, parse_inr

log = logging.getLogger(__name__)

ASIN_RE = re.compile(r"/(?:dp|gp/product)/([A-Z0-9]{10})")


class AmazonIN(Source):
    name = "amazon_in"
    label = "Amazon.in"
    base_url = "https://www.amazon.in"

    # ------------------------------------------------------------- discovery

    def search(self, query: str, limit: int = 24) -> list[Offer]:
        # No `&i=toys` department filter: it was never verified against the
        # live site, and a wrong department alias silently returns nothing.
        # The strict matcher already discards non-LEGO noise, so breadth is
        # worth more here than a narrower search.
        url = f"{self.base_url}/s?k={quote_plus(query)}"
        try:
            html = self.session.get(url, referer=self.base_url)
        except Exception as exc:  # noqa: BLE001
            log.warning("[amazon_in] search '%s' failed: %s", query, exc)
            self.last_error = str(exc)
            return []

        soup = BeautifulSoup(html, "lxml")
        cards = soup.select('div[data-component-type="s-search-result"]')
        offers: list[Offer] = []

        for card in cards[:limit]:
            offer = self._parse_card(card)
            if offer:
                offers.append(offer)

        log.info("[amazon_in] '%s' -> %d cards, %d parsed",
                 query, len(cards), len(offers))
        return offers

    def _parse_card(self, card) -> Optional[Offer]:
        asin = card.get("data-asin") or None
        if not asin:
            return None

        title = self._card_title(card)
        if not title:
            return None

        # Current price: first .a-price that is NOT the struck-through MRP.
        price = None
        for node in card.select(".a-price"):
            classes = node.get("class") or []
            if "a-text-price" in classes:
                continue
            off = node.select_one(".a-offscreen")
            price = parse_inr(off.get_text() if off else None)
            if price:
                break

        mrp_node = card.select_one(".a-price.a-text-price .a-offscreen")
        mrp = parse_inr(mrp_node.get_text() if mrp_node else None)

        href = None
        link = card.select_one("h2 a[href], a.a-link-normal.s-line-clamp-2[href]")
        if link:
            href = urljoin(self.base_url, link["href"].split("?")[0])
        if not href:
            href = f"{self.base_url}/dp/{asin}"

        rating, reviews = self._card_reviews(card)

        return Offer(
            retailer=self.name,
            url=href,
            title=title,
            price_inr=price,
            mrp_inr=mrp if (mrp and price and mrp >= price) else None,
            in_stock=price is not None,
            sku=asin,
            rating=rating,
            review_count=reviews,
            brand="LEGO" if "lego" in title.lower() else None,
        )

    @staticmethod
    def _card_title(card) -> Optional[str]:
        # The image alt carries the full product title in the current layout;
        # the <h2> is often truncated to the brand alone.
        img = card.select_one("img.s-image[alt]")
        if img and len(img["alt"].strip()) > 12:
            return img["alt"].strip()
        for sel in ("h2 a span", "h2 span", "h2 a"):
            node = card.select_one(sel)
            if node:
                txt = node.get_text(" ", strip=True)
                if len(txt) > 12:
                    return txt
        link = card.select_one("a[aria-label]")
        if link:
            return link["aria-label"].strip()
        return None

    @staticmethod
    def _card_reviews(card) -> tuple[Optional[float], Optional[int]]:
        rating = None
        node = card.select_one("span.a-icon-alt")
        if node:
            m = re.search(r"([\d.]+)\s+out of", node.get_text())
            if m:
                rating = float(m.group(1))
        if rating is None:
            # Current layout puts the rating only in the popover label.
            for el in card.select("[data-a-popover], [aria-label]"):
                blob = (el.get("data-a-popover") or "") + (el.get("aria-label") or "")
                m = re.search(r"([\d.]+) out of 5 stars", blob)
                if m:
                    rating = float(m.group(1))
                    break
        reviews = None
        rnode = card.select_one("span.a-size-base.s-underline-text, "
                                "a[href*='#customerReviews'] span")
        if rnode:
            digits = re.sub(r"[^\d]", "", rnode.get_text())
            if digits:
                reviews = int(digits)
        return rating, reviews

    # --------------------------------------------------------------- refresh

    def fetch_offer(self, url: str) -> Optional[Offer]:
        try:
            html = self.session.get(url, referer=self.base_url)
        except Exception as exc:  # noqa: BLE001
            log.warning("[amazon_in] fetch %s failed: %s", url, exc)
            self.last_error = str(exc)
            return None

        soup = BeautifulSoup(html, "lxml")
        tnode = soup.select_one("#productTitle")
        title = tnode.get_text(" ", strip=True) if tnode else ""
        if not title:
            return None

        price = None
        for sel in ("#corePriceDisplay_desktop_feature_div .a-price .a-offscreen",
                    "#corePrice_feature_div .a-price .a-offscreen",
                    "#priceblock_ourprice",
                    ".a-price .a-offscreen"):
            node = soup.select_one(sel)
            price = parse_inr(node.get_text() if node else None)
            if price:
                break

        mrp_node = soup.select_one(
            ".basisPrice .a-offscreen, span.a-price.a-text-price .a-offscreen")
        mrp = parse_inr(mrp_node.get_text() if mrp_node else None)

        avail_node = soup.select_one("#availability")
        avail = avail_node.get_text(" ", strip=True).lower() if avail_node else ""
        if avail:
            in_stock = not any(p in avail for p in
                               ("unavailable", "out of stock", "not available"))
        else:
            in_stock = price is not None

        seller_node = soup.select_one("#sellerProfileTriggerId, #merchant-info")
        seller = seller_node.get_text(" ", strip=True)[:120] if seller_node else None

        rating = None
        rnode = soup.select_one("#acrPopover")
        if rnode and rnode.get("title"):
            m = re.search(r"([\d.]+)", rnode["title"])
            if m:
                rating = float(m.group(1))

        reviews = None
        cnode = soup.select_one("#acrCustomerReviewText")
        if cnode:
            digits = re.sub(r"[^\d]", "", cnode.get_text())
            if digits:
                reviews = int(digits)

        m = ASIN_RE.search(url)
        return Offer(
            retailer=self.name,
            url=url,
            title=title,
            price_inr=price,
            mrp_inr=mrp if (mrp and price and mrp >= price) else None,
            in_stock=in_stock,
            seller=seller,
            sku=m.group(1) if m else None,
            rating=rating,
            review_count=reviews,
            brand="LEGO" if "lego" in title.lower() else None,
        )
