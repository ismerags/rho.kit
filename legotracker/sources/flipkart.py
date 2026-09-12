"""Flipkart adapter.

Flipkart server-renders a full Redux store into the page as
`window.__INITIAL_STATE__ = {...};` and that blob contains every product's
title, url, availability and pricing. Parsing it is far more stable than
scraping Flipkart's obfuscated, frequently-rotated CSS class names.

The store's shape changes often, so instead of hard-coding a path like
`pageDataV4.page.data['10002'][i].widget.data.products[0]...` we walk the whole
tree and collect any object that has both `titles` and `pricing`. That survives
layout reshuffles.

Two pricing shapes exist in the wild:
    search results : pricing.prices = [{strikeOff:true, value:2290},
                                       {strikeOff:false, value:2175}]
    product pages  : pricing.finalPrice.value / pricing.mrp.value
Both are handled.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Iterator, Optional
from urllib.parse import quote_plus, urljoin

from .base import Offer, Source

log = logging.getLogger(__name__)

STATE_MARKER = "window.__INITIAL_STATE__"


def _extract_state(html: str) -> Optional[dict]:
    """Pull the JSON object after `window.__INITIAL_STATE__ =` by brace matching.

    A regex cannot do this safely: the blob is ~300KB of nested JSON containing
    braces inside string literals (review text, product names).
    """
    idx = html.find(STATE_MARKER)
    if idx == -1:
        return None
    start = html.find("{", idx)
    if start == -1:
        return None

    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(html)):
        ch = html[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                blob = html[start:i + 1]
                try:
                    return json.loads(blob)
                except json.JSONDecodeError as exc:
                    log.warning("[flipkart] state JSON decode failed: %s", exc)
                    return None
    return None


def _walk_products(node: Any, seen: Optional[set[int]] = None) -> Iterator[dict]:
    """Yield every dict in the tree that looks like a product card."""
    if seen is None:
        seen = set()
    if id(node) in seen:
        return
    if isinstance(node, dict):
        seen.add(id(node))
        titles = node.get("titles")
        if isinstance(titles, dict) and titles.get("title") and "pricing" in node:
            yield node
        else:
            for value in node.values():
                yield from _walk_products(value, seen)
    elif isinstance(node, list):
        seen.add(id(node))
        for item in node:
            yield from _walk_products(item, seen)


def _prices(pricing: Any) -> tuple[Optional[float], Optional[float]]:
    """Return (selling_price, mrp) from either pricing shape."""
    if not isinstance(pricing, dict):
        return None, None

    selling = mrp = None

    fp = pricing.get("finalPrice")
    if isinstance(fp, dict) and isinstance(fp.get("value"), (int, float)):
        selling = float(fp["value"])
    m = pricing.get("mrp")
    if isinstance(m, dict) and isinstance(m.get("value"), (int, float)):
        mrp = float(m["value"])
    elif isinstance(m, (int, float)):
        mrp = float(m)

    prices = pricing.get("prices")
    if isinstance(prices, list):
        for entry in prices:
            if not isinstance(entry, dict):
                continue
            val = entry.get("value")
            if not isinstance(val, (int, float)):
                continue
            if entry.get("strikeOff"):
                mrp = mrp or float(val)
            else:
                selling = selling or float(val)

    if selling and mrp and mrp < selling:
        selling, mrp = mrp, selling
    return selling, mrp


class Flipkart(Source):
    name = "flipkart"
    label = "Flipkart"
    base_url = "https://www.flipkart.com"

    def search(self, query: str, limit: int = 24) -> list[Offer]:
        url = f"{self.base_url}/search?q={quote_plus(query)}"
        try:
            html = self.session.get(url, referer=self.base_url)
        except Exception as exc:  # noqa: BLE001
            log.warning("[flipkart] search '%s' failed: %s", query, exc)
            self.last_error = str(exc)
            return []

        state = _extract_state(html)
        if state is None:
            log.warning("[flipkart] no __INITIAL_STATE__ for '%s' "
                        "(layout change or bot wall)", query)
            return []

        offers: list[Offer] = []
        seen_urls: set[str] = set()

        for node in _walk_products(state):
            if len(offers) >= limit:
                break
            titles = node.get("titles") or {}
            title = (titles.get("title") or "").strip()
            if not title:
                continue

            raw_url = node.get("smartUrl") or node.get("baseUrl") or ""
            raw_url = str(raw_url).split("?")[0]
            if not raw_url:
                continue
            full_url = urljoin(self.base_url, raw_url)
            if full_url in seen_urls:
                continue
            seen_urls.add(full_url)

            selling, mrp = _prices(node.get("pricing"))

            avail = node.get("availability") or {}
            state_txt = str(avail.get("displayState") or "").upper()
            if state_txt:
                in_stock = state_txt == "IN_STOCK"
            else:
                in_stock = selling is not None

            rating_obj = node.get("rating") or {}
            rating = rating_obj.get("average") if isinstance(rating_obj, dict) else None
            reviews = rating_obj.get("count") if isinstance(rating_obj, dict) else None

            offers.append(Offer(
                retailer=self.name,
                url=full_url,
                title=title,
                price_inr=selling,
                mrp_inr=mrp,
                in_stock=in_stock,
                sku=node.get("id"),
                rating=float(rating) if isinstance(rating, (int, float)) else None,
                review_count=int(reviews) if isinstance(reviews, (int, float)) else None,
                brand="LEGO" if title.lower().startswith("lego") else None,
            ))

        log.info("[flipkart] '%s' -> %d offers", query, len(offers))
        return offers

    def fetch_offer(self, url: str) -> Optional[Offer]:
        """Product pages carry the same state blob, so reuse the walker and
        take the entry whose url matches (or the first one, which is the
        page's own product)."""
        try:
            html = self.session.get(url, referer=self.base_url)
        except Exception as exc:  # noqa: BLE001
            log.warning("[flipkart] fetch %s failed: %s", url, exc)
            self.last_error = str(exc)
            return None

        state = _extract_state(html)
        if state is None:
            return None

        path = url.replace(self.base_url, "").split("?")[0]
        fallback: Optional[Offer] = None

        for node in _walk_products(state):
            titles = node.get("titles") or {}
            title = (titles.get("title") or "").strip()
            raw_url = str(node.get("smartUrl") or node.get("baseUrl") or "").split("?")[0]
            selling, mrp = _prices(node.get("pricing"))
            avail = node.get("availability") or {}
            state_txt = str(avail.get("displayState") or "").upper()

            offer = Offer(
                retailer=self.name,
                url=urljoin(self.base_url, raw_url) if raw_url else url,
                title=title,
                price_inr=selling,
                mrp_inr=mrp,
                in_stock=(state_txt == "IN_STOCK") if state_txt else (selling is not None),
                sku=node.get("id"),
                brand="LEGO" if title.lower().startswith("lego") else None,
            )
            if raw_url and raw_url == path:
                return offer
            if fallback is None and selling is not None:
                fallback = offer

        return fallback
