"""LEGO.com India (lego.com/en-in) — catalog source, NOT a price source.

Verified behaviour, September 2026:

  * Theme pages server-render product links:
        /en-in/product/ninjago-city-workshops-71837
    The trailing number is the official set number and the slug is the official
    set name, so a plain HTTP fetch gives a clean, authoritative catalog of what
    LEGO actually sells in India.

  * Prices are NOT in the HTML. `__NEXT_DATA__` carries `"price":0`, the rendered
    DOM node `[data-test="product-price-display-price"]` is empty until a
    client-side GraphQL call fills it, and that endpoint rejects replayed
    requests as CSRF unless you carry the browser's headers and a rotating
    persisted-query hash.

So this adapter deliberately returns offers with `price_inr = None`. It answers
"does LEGO officially sell this set in India, and what is it called" — which is
what makes the other three retailers' listings matchable in the first place.

If you later want official RRP too, the honest route is a headless browser
(Playwright) against the product page; see README, "Adding LEGO.com prices".
"""

from __future__ import annotations

import logging
import re
from typing import Optional
from urllib.parse import urljoin

from .base import Offer, Source

log = logging.getLogger(__name__)

PRODUCT_HREF_RE = re.compile(r"/en-in/product/([a-z0-9\-]+?)-(\d{4,7})(?![\d-])")

# Themes worth sweeping for an India catalog. Trimmed to what the IN store
# actually carries; unknown slugs simply return nothing and are skipped.
DEFAULT_THEMES = [
    "ninjago", "star-wars", "technic", "city", "creator-3-in-1", "icons",
    "harry-potter", "friends", "duplo", "classic", "speed-champions",
    "minecraft", "marvel", "disney", "architecture", "botanicals",
    "jurassic-world", "super-mario", "one-piece", "animal-crossing",
    "lego-dreamzzz", "sonic-the-hedgehog", "wicked", "f1",
]


def _titleise(slug: str) -> str:
    """'ninjago-city-workshops' -> 'Ninjago City Workshops'."""
    words = slug.replace("-", " ").split()
    small = {"of", "the", "and", "vs", "in", "a", "to"}
    out = []
    for i, w in enumerate(words):
        out.append(w if (i and w in small) else w.capitalize())
    return " ".join(out)


class LegoIN(Source):
    name = "lego_in"
    label = "LEGO.com IN"
    base_url = "https://www.lego.com"

    #: This source cannot report prices. The collector reads this flag so it
    #: records availability without polluting the price history with NULLs.
    catalog_only = True

    def search(self, query: str, limit: int = 40) -> list[Offer]:
        """`query` is treated as a theme slug (e.g. 'ninjago', 'star-wars')."""
        slug = query.strip().lower().replace(" ", "-")
        url = f"{self.base_url}/en-in/themes/{slug}"
        try:
            html = self.session.get(url, referer=f"{self.base_url}/en-in")
        except Exception as exc:  # noqa: BLE001
            log.warning("[lego_in] theme '%s' failed: %s", slug, exc)
            self.last_error = str(exc)
            return []

        offers: list[Offer] = []
        seen: set[str] = set()

        for match in PRODUCT_HREF_RE.finditer(html):
            name_slug, set_num = match.group(1), match.group(2)
            href = urljoin(self.base_url, match.group(0))
            if href in seen:
                continue
            seen.add(href)

            offers.append(Offer(
                retailer=self.name,
                url=href,
                # Reconstructing the title with the set number appended keeps it
                # compatible with the same matcher every other source uses.
                title=f"LEGO {_titleise(name_slug)} {set_num}",
                price_inr=None,
                in_stock=True,          # listed on the IN store = officially sold
                sku=set_num,
                brand="LEGO",
            ))
            if len(offers) >= limit:
                break

        log.info("[lego_in] theme '%s' -> %d products", slug, len(offers))
        return offers

    def browse_catalog(self, themes: Optional[list[str]] = None,
                       per_theme: int = 60) -> list[Offer]:
        """Sweep every theme and return the union of official IN products."""
        out: list[Offer] = []
        seen: set[str] = set()
        for theme in (themes or DEFAULT_THEMES):
            for offer in self.search(theme, limit=per_theme):
                if offer.url not in seen:
                    seen.add(offer.url)
                    out.append(offer)
        log.info("[lego_in] catalog sweep -> %d unique products", len(out))
        return out

    def fetch_offer(self, url: str) -> Optional[Offer]:
        """Availability only — see the module docstring for why there is no price."""
        try:
            html = self.session.get(url, referer=f"{self.base_url}/en-in")
        except Exception as exc:  # noqa: BLE001
            log.warning("[lego_in] fetch %s failed: %s", url, exc)
            self.last_error = str(exc)
            return None

        m = PRODUCT_HREF_RE.search(url)
        set_num = m.group(2) if m else None
        name = _titleise(m.group(1)) if m else url.rsplit("/", 1)[-1]

        low = html.lower()
        # These strings are server-rendered even though the price is not.
        sold_out = ("temporarily out of stock" in low
                    or "sold out" in low
                    or "coming soon" in low)

        return Offer(
            retailer=self.name,
            url=url,
            title=f"LEGO {name} {set_num}" if set_num else name,
            price_inr=None,
            in_stock=not sold_out,
            sku=set_num,
            brand="LEGO",
        )
