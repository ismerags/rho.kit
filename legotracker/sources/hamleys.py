"""Hamleys India (hamleys.in).

Hamleys runs on the Fynd commerce platform, which exposes a REST catalog API:

    /api/service/application/catalog/v1.0/products/?q=<query>&page_size=N

It needs an `Authorization: Bearer <token>` header. That token is **not a
secret** — it is the public storefront credential (base64 of
`application_id:application_token`) that Hamleys' own JavaScript sends from every
visitor's browser. Using it is exactly what the site does.

It does rotate, though, so hardcoding it guarantees a future breakage. Instead we
lift it from Hamleys' own homepage, where it is server-rendered as
`"authorizationHeader":"Bearer ..."`, and cache it for the process lifetime.
When they rotate it, the next run picks up the new one by itself.
"""

from __future__ import annotations

import logging
import re
from typing import Optional
from urllib.parse import quote_plus, urljoin

from .base import Offer, Source

log = logging.getLogger(__name__)

TOKEN_RE = re.compile(r'"authorizationHeader"\s*:\s*"(Bearer\s+[A-Za-z0-9+/=]+)"')
# Fallback: any base64 blob that decodes to "<24 hex>:<token>".
TOKEN_FALLBACK_RE = re.compile(r'"(Bearer\s+[A-Za-z0-9+/]{30,}={0,2})"')


class Hamleys(Source):
    name = "hamleys"
    label = "Hamleys"
    base_url = "https://hamleys.in"

    _token: Optional[str] = None      # cached per process

    def _auth(self) -> Optional[str]:
        if Hamleys._token:
            return Hamleys._token
        try:
            html = self.session.get(self.base_url + "/", use_cache=False)
        except Exception as exc:  # noqa: BLE001
            log.warning("[hamleys] could not load homepage for token: %s", exc)
            self.last_error = str(exc)
            return None

        m = TOKEN_RE.search(html) or TOKEN_FALLBACK_RE.search(html)
        if not m:
            log.warning("[hamleys] storefront token not found in homepage — "
                        "their frontend changed")
            return None
        Hamleys._token = m.group(1)
        log.info("[hamleys] storefront token acquired")
        return Hamleys._token

    # ------------------------------------------------------------------ search

    def search(self, query: str, limit: int = 20) -> list[Offer]:
        token = self._auth()
        if not token:
            return []

        url = (f"{self.base_url}/api/service/application/catalog/v1.0/products/"
               f"?q={quote_plus(query)}&page_size={min(limit, 30)}")
        try:
            data = self.session.get_json(
                url, referer=self.base_url,
                extra_headers={"Authorization": token,
                               "x-ordering-source": "storefront",
                               "Accept": "application/json"})
        except Exception as exc:  # noqa: BLE001
            log.warning("[hamleys] search '%s' failed: %s", query, exc)
            self.last_error = str(exc)
            # A rotated token shows up as a 401; drop it so the next call refetches.
            if "401" in str(exc):
                Hamleys._token = None
            return []

        items = data.get("items") or []
        offers = [o for o in (self._to_offer(i) for i in items[:limit]) if o]
        log.info("[hamleys] '%s' -> %d offers", query, len(offers))
        return offers

    def _to_offer(self, item: dict) -> Optional[Offer]:
        name = (item.get("name") or "").strip()
        if not name:
            return None

        price = item.get("price") or {}
        effective = (price.get("effective") or {}).get("min")
        marked = (price.get("marked") or {}).get("min")

        slug = item.get("slug")
        url = urljoin(self.base_url, f"/product/{slug}") if slug else self.base_url

        brand = (item.get("brand") or {}).get("name")

        return Offer(
            retailer=self.name,
            url=url,
            title=name,
            price_inr=float(effective) if isinstance(effective, (int, float)) else None,
            mrp_inr=(float(marked)
                     if isinstance(marked, (int, float))
                     and isinstance(effective, (int, float))
                     and marked > effective else None),
            in_stock=bool(item.get("sellable")),
            sku=item.get("item_code") or (str(item["uid"]) if item.get("uid") else None),
            brand=brand,
        )

    # ----------------------------------------------------------------- refresh

    def fetch_offer(self, url: str) -> Optional[Offer]:
        """Re-price a known product.

        Fynd's single-product endpoint (`/products/<slug>/`) returns the product
        but **no price** — pricing lives behind a separate per-size call. Rather
        than chase a two-hop API, we search for the set number in the slug: the
        search endpoint already returns prices, and it is one request.
        """
        slug = url.rstrip("/").rsplit("/", 1)[-1]
        nums = re.findall(r"(?<!\d)(\d{4,7})(?!\d)", slug)
        query = nums[0] if nums else slug.replace("-", " ")

        for offer in self.search(f"LEGO {query}", limit=20):
            if offer.url.rstrip("/") == url.rstrip("/"):
                return offer
        return None
