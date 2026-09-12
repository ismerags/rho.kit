"""FirstCry adapter.

FirstCry is the friendliest of the four: fully server-rendered, 20 tiles a page,
and — usefully — it labels its own prices for screen readers:

    <a aria-label="Sale price RS 2474.1"> ... </a>
    <a aria-label="Regular price RS 2749"> ... </a>

Reading those aria-labels is much more stable than the CSS classes beside them
(`span.r1 B14_42`), which are generated and churn between deploys.

Its LEGO brand listing is a fixed URL, so we can enumerate the whole Indian LEGO
range here rather than guessing search terms:
    https://www.firstcry.com/lego/0/0/354?page=N
"""

from __future__ import annotations

import logging
import re
from typing import Optional
from urllib.parse import quote_plus, urljoin

from bs4 import BeautifulSoup

from .base import Offer, Source, parse_inr

log = logging.getLogger(__name__)

BRAND_LISTING = "https://www.firstcry.com/lego/0/0/354"
SALE_RE = re.compile(r"sale price\s*rs\.?\s*([\d,]+(?:\.\d+)?)", re.I)
REG_RE = re.compile(r"regular price\s*rs\.?\s*([\d,]+(?:\.\d+)?)", re.I)
PID_RE = re.compile(r"listingpg-(\d+)")


class FirstCry(Source):
    name = "firstcry"
    label = "FirstCry"
    base_url = "https://www.firstcry.com"

    # ------------------------------------------------------------- discovery

    def search(self, query: str, limit: int = 24) -> list[Offer]:
        """One request against FirstCry's server-rendered search.

        Note the endpoint: `/search/?q=` works and returns the same `.list_block`
        tiles as the brand listing. The more obvious-looking
        `/searchresult?searchstring=` renders client-side and comes back with
        zero tiles — it looks like "no results" rather than like a bug, which is
        exactly the kind of silent failure worth pinning down in a comment.
        """
        url = f"{self.base_url}/search/?q={quote_plus(query)}"
        try:
            html = self.session.get(url, referer=self.base_url)
        except Exception as exc:  # noqa: BLE001
            log.warning("[firstcry] search '%s' failed: %s", query, exc)
            self.last_error = str(exc)
            return []

        soup = BeautifulSoup(html, "lxml")
        tiles = soup.select(".list_block")
        offers: list[Offer] = []
        seen: set[str] = set()
        for tile in tiles:
            offer = self._parse_tile(tile)
            if offer and offer.url not in seen:
                seen.add(offer.url)
                offers.append(offer)
            if len(offers) >= limit:
                break

        log.info("[firstcry] '%s' -> %d tiles, %d offers",
                 query, len(tiles), len(offers))
        return offers

    def browse_brand(self, max_pages: int = 8) -> list[Offer]:
        """Walk the LEGO brand listing pages. This is the discovery workhorse."""
        offers: list[Offer] = []
        seen: set[str] = set()

        for page in range(1, max_pages + 1):
            url = BRAND_LISTING if page == 1 else f"{BRAND_LISTING}?page={page}"
            try:
                html = self.session.get(url, referer=self.base_url)
            except Exception as exc:  # noqa: BLE001
                log.warning("[firstcry] page %d failed: %s", page, exc)
                self.last_error = str(exc)
                break

            soup = BeautifulSoup(html, "lxml")
            tiles = soup.select(".list_block")
            if not tiles:
                log.info("[firstcry] page %d has no tiles, stopping", page)
                break

            new_on_page = 0
            for tile in tiles:
                offer = self._parse_tile(tile)
                if offer and offer.url not in seen:
                    seen.add(offer.url)
                    offers.append(offer)
                    new_on_page += 1

            log.info("[firstcry] page %d -> %d tiles, %d new",
                     page, len(tiles), new_on_page)
            if new_on_page == 0:
                break

        return offers

    def _parse_tile(self, tile) -> Optional[Offer]:
        inner = tile.select_one(".li_inner_block")
        title = None
        pid = None
        if inner:
            title = (inner.get("aria-label") or "").strip() or None
            classes = " ".join(inner.get("class") or [])
            m = PID_RE.search(classes)
            if m:
                pid = m.group(1)

        if not title:
            img = tile.select_one("img[alt]")
            title = img["alt"].strip() if img and img.get("alt") else None
        if not title:
            return None

        link = tile.select_one('a[href*="product-detail"]')
        if not link:
            return None
        href = link["href"]
        if href.startswith("//"):
            href = "https:" + href
        href = urljoin(self.base_url, href).split("?")[0]

        price = mrp = None
        for node in tile.select("a[aria-label], span[aria-label]"):
            label = node.get("aria-label") or ""
            if price is None:
                m = SALE_RE.search(label)
                if m:
                    price = parse_inr(m.group(1))
            if mrp is None:
                m = REG_RE.search(label)
                if m:
                    mrp = parse_inr(m.group(1))
            if price is not None and mrp is not None:
                break

        if price is None:
            # Layout fallback: the struck-through MRP sits in <del class="regular-price">
            del_node = tile.select_one("del.regular-price")
            if del_node:
                mrp = mrp or parse_inr(del_node.get_text())
            span = tile.select_one("span.r1")
            price = parse_inr(span.get_text()) if span else None

        text = tile.get_text(" ", strip=True).lower()
        out_of_stock = "out of stock" in text or "sold out" in text

        return Offer(
            retailer=self.name,
            url=href,
            title=title,
            price_inr=price,
            mrp_inr=mrp if (mrp and price and mrp >= price) else None,
            in_stock=(not out_of_stock) and price is not None,
            sku=pid,
            brand="LEGO" if title.lower().startswith("lego") else None,
        )

    # --------------------------------------------------------------- refresh

    def fetch_offer(self, url: str) -> Optional[Offer]:
        try:
            html = self.session.get(url, referer=BRAND_LISTING)
        except Exception as exc:  # noqa: BLE001
            log.warning("[firstcry] fetch %s failed: %s", url, exc)
            self.last_error = str(exc)
            return None

        soup = BeautifulSoup(html, "lxml")

        title = None
        h1 = soup.select_one("h1")
        if h1:
            title = h1.get_text(" ", strip=True)
        if not title:
            og = soup.select_one('meta[property="og:title"]')
            title = og["content"].strip() if og and og.get("content") else None
        if not title:
            return None

        price = mrp = None
        for node in soup.select("[aria-label]"):
            label = node.get("aria-label") or ""
            if price is None:
                m = SALE_RE.search(label)
                if m:
                    price = parse_inr(m.group(1))
            if mrp is None:
                m = REG_RE.search(label)
                if m:
                    mrp = parse_inr(m.group(1))

        if price is None:
            meta = soup.select_one('meta[property="product:price:amount"], '
                                   'meta[itemprop="price"]')
            if meta and meta.get("content"):
                price = parse_inr(meta["content"])

        body = soup.get_text(" ", strip=True).lower()
        in_stock = not ("out of stock" in body or "sold out" in body)

        return Offer(
            retailer=self.name,
            url=url,
            title=title,
            price_inr=price,
            mrp_inr=mrp if (mrp and price and mrp >= price) else None,
            in_stock=in_stock and price is not None,
            brand="LEGO" if "lego" in title.lower() else None,
        )
