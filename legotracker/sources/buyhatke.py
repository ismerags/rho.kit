"""BuyHatke aggregator adapter.

BuyHatke (buyhatke.com) is a public, free price-tracking site that already
watches Amazon, Flipkart, Hamleys, Ajio and others. Two of its routes matter
here, and neither one requires an account or API key:

  1. ``/search?product=<query>``       — a keyword search across BuyHatke's own
     tracked catalogue. Cheap and broad: one request surfaces dozens of LEGO
     sets across every retailer BuyHatke already watches.
  2. ``/<any Amazon or Flipkart product URL>`` — paste-a-link lookup. BuyHatke
     resolves the pasted URL itself (server-side, from its own infrastructure)
     and returns that product's current price, a small cross-retailer price
     comparison, AND its full historical price series — often stretching back
     a year or more, which is far more history than this project could ever
     collect on its own.

Why this exists: Amazon and Flipkart bot-wall aggressive/parallel traffic (see
``amazon_in.py`` / ``flipkart.py``), which caps how much of the catalogue this
project can safely check directly. BuyHatke already did that work legitimately
(it is a public, paste-your-own-link tool, not a captcha bypass) and is happy
to hand the answer back in one polite request.

Both routes server-render their data into the HTML — no headless browser
needed — but in two different shapes:

  * The product-detail route embeds one JS object literal:
    ``data:{siteName:"Amazon",...,productData:{...},predictedData:{history:
    [...]}}``. Almost JSON — the only wrinkle is unquoted object keys — so
    ``_js_object_to_json`` below fixes that up and hands it to ``json.loads``.

  * The search route is compiled SvelteKit output: each result is a run of
    flat ``x.field=value;`` assignment statements (``x`` a short, arbitrary,
    per-item identifier). It is not a JSON blob at all, but the fields are
    always simple scalars in a fixed set, so ``_parse_search_blocks`` reads it
    with one regex rather than pulling in a JS engine.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Iterator, Optional
from urllib.parse import quote_plus, urlparse

from .base import Offer, Source

log = logging.getLogger(__name__)

BASE_URL = "https://buyhatke.com"

# BuyHatke's own display name for a retailer -> this project's retailer name.
# Only sites this project already has an adapter for are worth mapping: a
# mapped deal gets folded into that retailer's own price history (see
# collect.py:_absorb_deals), not just parked under "buyhatke".
SITE_NAME_TO_RETAILER = {
    "amazon": "amazon_in",
    "flipkart": "flipkart",
    "hamleys": "hamleys",
}

#: Same idea, keyed by the real vendor URL's domain instead of BuyHatke's own
#: "site_name" label -- needed for the /search route, whose result items carry
#: a `link` but no explicit site name. A domain not listed here (Ajio,
#: Snapdeal, ...) is left tagged "buyhatke" rather than inventing a retailer
#: this project has no page or label for.
DOMAIN_TO_RETAILER = {
    "amazon.in": "amazon_in",
    "flipkart.com": "flipkart",
    "hamleys.in": "hamleys",
    "firstcry.com": "firstcry",
}

#: Retailers queried directly instead of through BuyHatke (see
#: sources/__init__.py) precisely because BuyHatke does not track them.
#: An offer with one of these as its retailer was never something BuyHatke
#: could resolve, so `search._deep_enrich` skips it rather than spending a
#: guaranteed-to-fail request per catalogue set on it.
NOT_TRACKED_BY_BUYHATKE = frozenset({"toycra", "jaimantoys"})


def retailer_for_link(link: Optional[str]) -> str:
    """Who a shopper would actually pay, going only by the listing's own URL."""
    if not link:
        return "buyhatke"
    host = (urlparse(link).netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    for domain, retailer in DOMAIN_TO_RETAILER.items():
        if host == domain or host.endswith("." + domain):
            return retailer
    return "buyhatke"


def proxy_url(real_url: str) -> str:
    """The BuyHatke paste-link form of a real vendor URL: re-fetching this
    gets that exact listing's current price, cross-retailer deals, and full
    price history in one request."""
    return f"{BASE_URL}/{real_url}"


# --------------------------------------------------------------------------
# shared: pull a JS object literal out of server-rendered HTML
# --------------------------------------------------------------------------

def _extract_balanced_object(html: str, marker: str) -> Optional[str]:
    """Return the ``{...}`` object that starts right after `marker`.

    String-aware brace matching (same technique as flipkart._extract_state):
    a regex cannot safely find the matching close-brace when the blob contains
    braces inside string literals (image URLs, product titles with "{" in
    them is rare but not impossible — safety costs nothing here).
    """
    idx = html.find(marker)
    if idx == -1:
        return None
    start = idx + len(marker) - 1  # the "{" itself is the marker's last char
    if html[start] != "{":
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
                return html[start:i + 1]
    return None


def _js_object_to_json(blob: str) -> Optional[dict]:
    """Turn a JS object literal with unquoted keys into JSON and parse it.

    Walks the string once, tracking whether we are inside a "..." literal.
    Outside a string, a bare identifier immediately followed by ':' is an
    unquoted key — wrap it in quotes. Everything else (numbers, true/false/
    null, already-quoted strings) is already valid JSON and passes through
    untouched. This is deliberately narrower than a real JS parser: it does
    not need to be, since this input is machine-generated and never contains
    functions, comments, or single-quoted strings.
    """
    import json

    out: list[str] = []
    i = 0
    n = len(blob)
    ident_start_re = re.compile(r"[A-Za-z_$]")
    ident_re = re.compile(r"[A-Za-z0-9_$]*")
    in_str = False
    escape = False

    while i < n:
        ch = blob[i]
        if in_str:
            out.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            i += 1
            continue

        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue

        if ident_start_re.match(ch):
            m = ident_re.match(blob, i + 1)
            end = m.end() if m else i + 1
            ident = blob[i:end]
            j = end
            while j < n and blob[j] in " \t\n\r":
                j += 1
            if j < n and blob[j] == ":":
                out.append(f'"{ident}"')
            else:
                # bareword that is NOT a key: true/false/null pass through
                # unchanged (valid JSON already); anything else (there
                # shouldn't be anything else) is left as-is and will fail
                # json.loads loudly rather than silently mis-parsing.
                out.append(ident)
            i = end
            continue

        out.append(ch)
        i += 1

    try:
        return json.loads("".join(out))
    except ValueError as exc:
        log.warning("[buyhatke] could not parse object literal: %s", exc)
        return None


def fetch_detail(session, url: str) -> Optional[dict]:
    """Fetch a BuyHatke product page (canonical or '/<pasted-link>' form) and
    return its parsed page data: {siteName, dealsData, productData,
    predictedData, ...}, or None if the page didn't have one (BuyHatke
    doesn't track this product, or the layout changed)."""
    try:
        html = session.get(url, referer=BASE_URL)
    except Exception as exc:  # noqa: BLE001
        log.warning("[buyhatke] fetch %s failed: %s", url, exc)
        return None

    blob = _extract_balanced_object(html, "data:{siteName:")
    if blob is None:
        return None
    return _js_object_to_json(blob)


def offer_from_product_data(url: str, data: dict) -> Optional[Offer]:
    pd = data.get("productData") or {}
    name = pd.get("name")
    if not name:
        return None
    price = pd.get("cur_price")
    mrp = pd.get("mrpFloat")
    real_url = pd.get("link") or url
    site = str(pd.get("site_name") or "").strip().lower()
    retailer = SITE_NAME_TO_RETAILER.get(site) or retailer_for_link(real_url)
    return Offer(
        retailer=retailer,
        url=real_url,
        title=name,
        price_inr=float(price) if isinstance(price, (int, float)) else None,
        mrp_inr=(float(mrp) if isinstance(mrp, (int, float))
                and price and mrp >= price else None),
        in_stock=bool(pd.get("inStock", 1)),
        sku=pd.get("pid"),
        rating=pd.get("rating") if isinstance(pd.get("rating"), (int, float)) else None,
        review_count=pd.get("ratingCount") if isinstance(pd.get("ratingCount"), (int, float)) else None,
        brand="LEGO" if "lego" in name.lower() else None,
    )


def history_points(data: dict) -> list[tuple[str, float]]:
    """(observed_at, price) pairs from predictedData.history, oldest first.

    Each history entry is a validity range {from, to, price} — a price that
    held from `from` to `to`. We record it under `from`: that is the moment
    BuyHatke actually observed the change, which is what "a change happened
    on this date" means for our own append-only price_points table.
    """
    hist = ((data.get("predictedData") or {}).get("history")) or []
    out: list[tuple[str, float]] = []
    for pt in hist:
        at = pt.get("from")
        price = pt.get("price")
        if at and isinstance(price, (int, float)):
            out.append((str(at), float(price)))
    out.sort(key=lambda t: t[0])
    return out


def deal_list(data: dict) -> list[dict]:
    """Cross-retailer offers BuyHatke already has for this same product."""
    return ((data.get("dealsData") or {}).get("dealsList")) or []


# --------------------------------------------------------------------------
# the search route: flat x.field=value; statements, not JSON
# --------------------------------------------------------------------------

_FIELD_RE = re.compile(
    r'\.(?P<key>[A-Za-z_$][A-Za-z0-9_$]*)='
    r'(?P<val>"(?:[^"\\]|\\.)*"|-?\d+(?:\.\d+)?|true|false|null)\s*;'
)


def _coerce(raw: str):
    if raw == "true":
        return True
    if raw == "false":
        return False
    if raw == "null":
        return None
    if raw[:1] == '"':
        # cheap unescape: these are simple machine-generated strings
        return raw[1:-1].encode().decode("unicode_escape")
    try:
        if "." in raw:
            return float(raw)
        return int(raw)
    except ValueError:
        return raw


def _parse_search_blocks(html: str) -> Iterator[dict]:
    """Yield one dict per product card in a /search?product=... response.

    A new item starts at every `.prod=` assignment (confirmed against live
    markup: it is always the first field written for each item). Everything
    between one `.prod=` and the next belongs to that item.
    """
    starts = [m.start() for m in re.finditer(r'\.prod="', html)]
    if not starts:
        return
    starts.append(len(html))
    for a, b in zip(starts, starts[1:]):
        block = html[a:b]
        item: dict = {}
        for m in _FIELD_RE.finditer(block):
            item[m.group("key")] = _coerce(m.group("val"))
        if item.get("prod"):
            yield item


def _offer_from_search_item(item: dict) -> Optional[Offer]:
    title = item.get("prod")
    if not title:
        return None
    link = item.get("link") or ""
    if not link:
        return None
    price = item.get("price")
    mrp = item.get("mrp")
    return Offer(
        retailer=retailer_for_link(link),
        url=link,
        title=title,
        price_inr=float(price) if isinstance(price, (int, float)) else None,
        mrp_inr=(float(mrp) if isinstance(mrp, (int, float))
                and price and mrp >= price else None),
        in_stock=bool(item.get("isActive", 1)),
        sku=item.get("pid"),
        rating=item.get("rating") or None,
        review_count=item.get("rating_count") or None,
        brand="LEGO" if "lego" in title.lower() else None,
    )


class BuyHatke(Source):
    name = "buyhatke"
    label = "BuyHatke (aggregated)"
    base_url = BASE_URL

    def search(self, query: str, limit: int = 40) -> list[Offer]:
        url = f"{self.base_url}/search?product={quote_plus(query)}"
        try:
            html = self.session.get(url, referer=self.base_url)
        except Exception as exc:  # noqa: BLE001
            log.warning("[buyhatke] search '%s' failed: %s", query, exc)
            self.last_error = str(exc)
            return []

        offers: list[Offer] = []
        seen: set[str] = set()
        for item in _parse_search_blocks(html):
            offer = _offer_from_search_item(item)
            if offer is None or offer.url in seen:
                continue
            seen.add(offer.url)
            offers.append(offer)
            if len(offers) >= limit:
                break

        log.info("[buyhatke] '%s' -> %d offers", query, len(offers))
        return offers

    def fetch_offer(self, url: str) -> Optional[Offer]:
        target = url if url.startswith(BASE_URL) else proxy_url(url)
        data = fetch_detail(self.session, target)
        if data is None:
            return None
        return offer_from_product_data(target, data)
