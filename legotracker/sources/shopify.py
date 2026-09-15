"""Shopify storefronts (Toycra, Jaiman Toys).

Both run on Shopify, which exposes a documented public JSON search endpoint:

    /search/suggest.json?q=<query>&resources[type]=product&resources[limit]=N

That returns clean structured data — title, price, compare_at_price, available,
vendor, url — with no HTML parsing at all. It is the same endpoint the site's own
search box uses, so it is stable, fast (~300ms) and far less likely to break than
a scraper.

Caveat: Shopify search is fuzzy. Querying Toycra for "71806" returns 71814,
71800 and 71786 — none of them the set asked for. The strict matcher upstream is
what makes these results trustworthy.

Prices come back as decimal strings in rupees ("18499.00"). Note this differs
from Shopify's Ajax cart API, which uses paise — getting that wrong would inflate
every price 100x, so the two are never mixed here.
"""

from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import quote_plus, urljoin

from .base import Offer, Source, parse_inr

log = logging.getLogger(__name__)


class ShopifyStore(Source):
    """Base class. Subclasses only set name/label/base_url."""

    def search(self, query: str, limit: int = 20) -> list[Offer]:
        url = (f"{self.base_url}/search/suggest.json?q={quote_plus(query)}"
               f"&resources[type]=product&resources[limit]={min(limit, 10)}")
        try:
            data = self.session.get_json(url, referer=self.base_url)
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] search '%s' failed: %s", self.name, query, exc)
            self.last_error = str(exc)
            return []

        try:
            products = data["resources"]["results"]["products"]
        except (KeyError, TypeError):
            log.warning("[%s] unexpected suggest.json shape", self.name)
            return []

        offers = [o for o in (self._to_offer(p) for p in products[:limit]) if o]
        log.info("[%s] '%s' -> %d offers", self.name, query, len(offers))
        return offers

    def _to_offer(self, p: dict) -> Optional[Offer]:
        title = (p.get("title") or "").strip()
        if not title:
            return None

        price = parse_inr(p.get("price"))
        # compare_at_price_max is Shopify's "was" price; it is 0 or absent when
        # the item is not discounted.
        mrp = parse_inr(p.get("compare_at_price_max")
                        or p.get("compare_at_price"))

        raw_url = (p.get("url") or "").split("?")[0]   # strip search tracking
        if not raw_url:
            handle = p.get("handle")
            if not handle:
                return None
            raw_url = f"/products/{handle}"

        image = p.get("image")
        if isinstance(image, dict):
            image = image.get("url") or image.get("src")
        if not isinstance(image, str):
            feat = p.get("featured_image")
            image = feat.get("url") if isinstance(feat, dict) else (
                feat if isinstance(feat, str) else None)

        return Offer(
            retailer=self.name,
            url=urljoin(self.base_url, raw_url),
            title=title,
            price_inr=price,
            mrp_inr=mrp if (mrp and price and mrp > price) else None,
            in_stock=bool(p.get("available")),
            sku=str(p.get("id")) if p.get("id") else None,
            brand=p.get("vendor"),
            image=image.split("?")[0] if isinstance(image, str) else None,
        )

    # ------------------------------------------------------------- catalogue

    def browse_brand(self, vendor: str = "LEGO", collection: str = "lego",
                     max_pages: int = 40) -> list[dict]:
        """Every product from one brand, via the storefront's JSON listing.

        Tries the brand collection first (/collections/lego/products.json —
        Toycra's LEGO range in a few requests). If that is empty — Jaiman Toys
        has a "lego" collection that returns nothing — it walks the whole store
        (/products.json) instead. Either way only products whose vendor is the
        brand are kept, because shop collections also hold display cases and
        look-alike bricks.
        """
        rows = self.browse_catalog(max_pages=max_pages,
                                   path=f"/collections/{collection}/products.json")
        source = f"collection '{collection}'"
        if not rows:
            self.last_error = None
            rows = self.browse_catalog(max_pages=max_pages)
            source = "full store"
        want = vendor.lower()
        kept = [r for r in rows if want in (r.get("vendor") or "").lower()]
        log.info("[%s] %s: %d products, %d by %s",
                 self.name, source, len(rows), len(kept), vendor)
        return kept

    def browse_catalog(self, max_pages: int = 8,
                       per_page: int = 250,
                       path: str = "/products.json") -> list[dict]:
        """Enumerate the whole storefront via /products.json.

        Returns raw-ish dicts rather than Offers because a catalogue row wants
        things an Offer does not model — image, piece count, the vendor's own
        SKU. On this storefront `variants[0].sku` IS the LEGO set number, which
        is far more trustworthy than parsing it out of the title.
        """
        items: list[dict] = []
        seen: set[str] = set()

        for page in range(1, max_pages + 1):
            url = f"{self.base_url}{path}?limit={per_page}&page={page}"
            try:
                data = self.session.get_json(url, referer=self.base_url)
            except Exception as exc:  # noqa: BLE001
                log.warning("[%s] catalogue page %d failed: %s",
                            self.name, page, exc)
                self.last_error = str(exc)
                break

            products = data.get("products") or []
            if not products:
                break

            for p in products:
                handle = p.get("handle")
                if not handle or handle in seen:
                    continue
                seen.add(handle)
                variants = p.get("variants") or []
                v = variants[0] if variants else {}
                images = p.get("images") or []
                items.append({
                    "retailer": self.name,
                    "title": (p.get("title") or "").strip(),
                    "handle": handle,
                    "url": f"{self.base_url}/products/{handle}",
                    "sku": (v.get("sku") or "").strip() or None,
                    "price": parse_inr(v.get("price")),
                    "compare_at": parse_inr(v.get("compare_at_price")),
                    "in_stock": bool(v.get("available")),
                    "image": (images[0].get("src") or "").split("?")[0]
                             if images else None,
                    "vendor": p.get("vendor"),
                    # The title often omits the piece count ("Krusty Burger
                    # Building Set for Adults") even though the storefront's
                    # own description states it plainly ("Set contains 490
                    # pieces") -- catalog.py falls back to this when the
                    # title alone doesn't have it.
                    "body_html": p.get("body_html") or "",
                })

            if len(products) < per_page:
                break

        log.info("[%s] catalogue -> %d products", self.name, len(items))
        return items

    def fetch_offer(self, url: str) -> Optional[Offer]:
        """Shopify serves a single product as JSON at <product-url>.js."""
        try:
            data = self.session.get_json(f"{url.rstrip('/')}.js",
                                         referer=self.base_url)
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] fetch %s failed: %s", self.name, url, exc)
            self.last_error = str(exc)
            return None

        title = (data.get("title") or "").strip()
        if not title:
            return None

        variants = data.get("variants") or []
        available = any(v.get("available") for v in variants) if variants \
            else bool(data.get("available"))

        # The product .js endpoint reports paise, unlike suggest.json.
        def rupees(v):
            return round(v / 100.0, 2) if isinstance(v, (int, float)) else None

        price = rupees(data.get("price"))
        mrp = rupees(data.get("compare_at_price"))

        return Offer(
            retailer=self.name,
            url=url,
            title=title,
            price_inr=price,
            mrp_inr=mrp if (mrp and price and mrp > price) else None,
            in_stock=available,
            sku=str(data.get("id")) if data.get("id") else None,
            brand=data.get("vendor"),
        )


class Toycra(ShopifyStore):
    name = "toycra"
    label = "Toycra"
    base_url = "https://toycra.com"


class JaimanToys(ShopifyStore):
    name = "jaimantoys"
    label = "Jaiman Toys"
    base_url = "https://jaimantoys.com"


class MyBrickHouse(ShopifyStore):
    """The LEGO Certified Store for India (shop id `legoin.myshopify.com`).

    LEGO does not sell direct here — lego.com/en-in shows no prices at all, even
    in a real browser (`meta[product:price:amount]` is literally "0"). Indian
    buyers are routed to Certified Stores, so this is the closest thing to
    LEGO's own Indian retail price, and it doubles as the catalogue source:
    ~900 products with set numbers, prices, stock and images.
    """
    name = "mybrickhouse"
    label = "LEGO Certified Store"
    base_url = "https://lego.mybrickhouse.com"
