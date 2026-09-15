"""The browsable catalogue: ~900 LEGO sets with thumbnails, cached locally.

Where it comes from
-------------------
The LEGO Certified Store for India (lego.mybrickhouse.com) runs on Shopify and
exposes `/products.json`, which returns the entire storefront — title, price,
stock, SKU and image URLs — in four requests. That single endpoint gives us the
three things a browse page needs and would otherwise be hard to get:

  * an authoritative list of what LEGO actually sells in India
  * a set number per product (`variants[0].sku`), not guessed from the title
  * a product image, which no other retailer exposes in bulk

lego.com/en-in cannot do this job: it publishes no prices at all, even to a real
browser, because LEGO sells into India through Certified Stores rather than
direct.

What is stored
--------------
A cache table, refreshed on demand. It is deliberately separate from `listings`:
a catalogue row is "this set exists and the Certified Store wants X for it",
whereas a listing is "we are tracking this URL's price over time". Refreshing
the catalogue also records the Certified Store's price as a normal observation,
so browsing feeds the price history like everything else.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from .db import Database, now_iso
from .http_client import PoliteSession
from .matching import normalise_set_num
from .sources import CATALOG_SOURCE, build_sources

log = logging.getLogger(__name__)

SETNUM_RE = re.compile(r"(?<!\d)(\d{4,7})(?!\d)")
PIECES_RE = re.compile(r"(\d[\d,]{1,6})\s*(?:pieces|pcs|piece)\b", re.I)

#: Theme is not in the Shopify data — `product_type` is blank and `tags` are
#: marketing labels like "new_arrivals_24_08_2026". So we read it off the title,
#: longest name first so "Star Wars" wins before "Star", and "Speed Champions"
#: before "Speed".
THEMES = [
    "Star Wars", "Harry Potter", "Speed Champions", "Jurassic World",
    "Super Mario", "Animal Crossing", "Sonic the Hedgehog", "Lord of the Rings",
    "Creator 3in1", "Creator Expert", "Architecture", "Botanicals", "BrickHeadz",
    "Minifigures", "Technic", "NINJAGO", "DUPLO", "Friends", "Minecraft",
    "Marvel", "Batman", "DC", "Disney", "Icons", "City", "Classic", "Ideas",
    "DREAMZzz", "Monkie Kid", "Wicked", "One Piece", "Formula 1", "F1",
    "Despicable Me", "Avatar", "Fortnite", "Art", "Education", "Gabby's Dollhouse",
]


@dataclass
class CatalogItem:
    set_num: str
    name: str
    theme: Optional[str] = None
    pieces: Optional[int] = None
    image: Optional[str] = None
    url: Optional[str] = None
    price: Optional[float] = None
    mrp: Optional[float] = None
    in_stock: bool = True

    def as_json(self) -> dict:
        return {
            "set_num": self.set_num, "name": self.name, "theme": self.theme,
            "pieces": self.pieces, "image": self.image, "url": self.url,
            "price": self.price, "mrp": self.mrp, "in_stock": self.in_stock,
        }


def theme_of(title: str) -> Optional[str]:
    low = title.lower()
    for theme in sorted(THEMES, key=len, reverse=True):
        if theme.lower() in low:
            return theme
    return None


def pieces_of(title: str) -> Optional[int]:
    m = PIECES_RE.search(title)
    if not m:
        return None
    try:
        n = int(m.group(1).replace(",", ""))
    except ValueError:
        return None
    return n if 1 <= n <= 20000 else None


def set_num_of(raw_sku: Optional[str], title: str) -> Optional[str]:
    """Prefer the storefront's own SKU; fall back to the title.

    The SKU is the LEGO set number on this store ("75453"), which avoids the
    usual trap of picking a piece count out of the title instead.
    """
    if raw_sku:
        sku = normalise_set_num(raw_sku)
        if SETNUM_RE.fullmatch(sku):
            return sku

    cleaned = PIECES_RE.sub(" ", title)
    candidates = [m.group(1) for m in SETNUM_RE.finditer(cleaned)]
    # Drop things that read like years rather than set numbers.
    candidates = [c for c in candidates
                  if not (len(c) == 4 and 1990 <= int(c) <= 2035)]
    if not candidates:
        return None
    candidates.sort(key=lambda c: (0 if len(c) in (5, 6) else 1))
    return candidates[0]


def to_item(row: dict) -> Optional[CatalogItem]:
    title = (row.get("title") or "").strip()
    if not title:
        return None
    set_num = set_num_of(row.get("sku"), title)
    if not set_num:
        return None      # merch, apparel, gift cards — nothing to price-match

    price = row.get("price")
    compare = row.get("compare_at")
    return CatalogItem(
        set_num=set_num,
        name=title,
        theme=theme_of(title),
        pieces=pieces_of(title),
        image=row.get("image"),
        url=row.get("url"),
        price=price,
        mrp=compare if (compare and price and compare > price) else None,
        in_stock=bool(row.get("in_stock")),
    )


# --------------------------------------------------------------------------

def refresh(db: Database, session: Optional[PoliteSession] = None,
            max_pages: int = 8) -> int:
    """Re-read the storefront and replace the cached catalogue."""
    session = session or PoliteSession(min_interval=1.0, jitter=0.4,
                                       timeout=20, max_retries=2,
                                       backoff_cap=5.0)
    adapter = build_sources(session, [CATALOG_SOURCE])[0]
    rows = adapter.browse_catalog(max_pages=max_pages)

    # A page that fails half-way returns the pages before it. Replacing a
    # 900-set catalogue with the first 250 would silently shrink Browse and
    # the daily snapshot, so a clearly partial read is refused instead.
    existing = len(db.catalog_items())
    if adapter.last_error and existing and len(rows) < 0.8 * existing:
        raise RuntimeError(
            f"catalogue read stopped early ({len(rows)} products vs {existing} "
            f"cached): {adapter.last_error[:120]}")

    items: dict[str, CatalogItem] = {}
    for row in rows:
        item = to_item(row)
        if not item:
            continue
        # If the store lists a set twice, keep the cheaper in-stock one.
        prev = items.get(item.set_num)
        if prev and not (item.in_stock and (
                not prev.in_stock or (item.price or 1e9) < (prev.price or 1e9))):
            continue
        items[item.set_num] = item

    db.replace_catalog([i.as_json() for i in items.values()])

    # Browsing should feed the price history too, exactly like a search does.
    saved = 0
    for item in items.values():
        if item.price is None:
            continue
        try:
            db.upsert_set(item.set_num, name=item.name, theme=item.theme,
                          pieces=item.pieces, image_url=item.image)
            listing_id = db.upsert_listing(
                set_num=item.set_num, retailer=CATALOG_SOURCE,
                url=item.url or "", title=item.name, confidence=0.99)
            db.record_price(listing_id, item.price, mrp_inr=item.mrp,
                            in_stock=item.in_stock)
            saved += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("catalogue -> history failed for %s: %s",
                        item.set_num, exc)

    log.info("catalogue: %d raw rows -> %d sets cached, %d prices recorded",
             len(rows), len(items), saved)
    return len(items)


def clean_name(name: str, set_num: str) -> str:
    """Drop the set number from a title so the tile starts at the product name.

    Only ever removes the number that *matches* this set — "Lego 72537 Kpop
    Demon Hunters Derpy Tiger" must lose the 72537 but keep every other number,
    because a piece count or a "2 Fast 2 Furious" is part of the name. The set
    number is already displayed as #72537 above the title, so repeating it is
    pure noise.
    """
    if not name:
        return name
    out = name
    # "... Set 75453", "... - 60312", "... (42176)" and bare occurrences.
    out = re.sub(rf"[\s\-–—,:(\[]*\b(?:set|item|no\.?)?\s*{re.escape(set_num)}\b[)\]]*",
                 " ", out, flags=re.I)
    out = re.sub(r"\s{2,}", " ", out)
    out = out.strip(" -–—,:•|")
    # A leading "LEGO®" is kept (it is part of how the set is known), but a
    # dangling separator left behind by the removal is not.
    return out or name


def enrich(db: Database, items: list[dict]) -> list[dict]:
    """Attach the best price we actually know for each set, from any retailer.

    The catalogue feed only carries the Certified Store's price. Everything
    else comes from observations already in the database — recorded by searches,
    by `collect`, or by a `priceall` sweep. Nothing is fetched here, so browsing
    stays instant.
    """
    known = db.latest_prices_by_set()
    out: list[dict] = []

    for item in items:
        row = dict(item)
        row["display_name"] = clean_name(row.get("name") or "", row["set_num"])

        seen = known.get(row["set_num"], [])
        in_stock = [o for o in seen if o["in_stock"] is not False]
        pool = in_stock or seen

        if pool:
            best = min(pool, key=lambda o: o["price"])
            row["best"] = {
                "price": best["price"], "mrp": best["mrp"],
                "retailer": best["retailer"], "url": best["url"],
                "at": best["observed_at"], "in_stock": best["in_stock"],
                "n": len(seen),
            }
            # "Out everywhere" only counts retailers we have actually checked;
            # an unpriced set is unknown, not unavailable.
            row["out_everywhere"] = bool(seen) and not in_stock
        else:
            row["best"] = None
            row["out_everywhere"] = False

        # Fall back to the catalogue's own price when nothing is known yet.
        price = (row["best"] or {}).get("price") or row.get("price")
        mrp = (row["best"] or {}).get("mrp") or row.get("mrp")
        row["show_price"] = price
        row["show_mrp"] = mrp if (mrp and price and mrp > price) else None
        row["discount_pct"] = (round((mrp - price) / mrp * 100)
                               if row["show_mrp"] else None)
        row["per_piece"] = (round(price / row["pieces"], 2)
                            if price and row.get("pieces") else None)
        out.append(row)
    return out


def load(db: Database, with_prices: bool = True) -> list[dict]:
    items = db.catalog_items()
    return enrich(db, items) if with_prices else items


def price_all(db: Database, session: Optional[PoliteSession] = None,
              stale_days: int = 7, limit: Optional[int] = None,
              on_progress=None) -> dict:
    """Walk the catalogue and price every set across every retailer.

    This is the slow job that makes the browse grid genuinely useful: without
    it, a tile can only show the Certified Store's price. Roughly 2-3 seconds
    per set (seven retailers in parallel, politely throttled), so the full
    catalogue takes about 45 minutes. It is resumable by design — sets priced
    within `stale_days` are skipped, so re-running after an interruption picks
    up where it stopped.
    """
    from datetime import datetime, timedelta, timezone
    from .search import search as run_search

    session = session or PoliteSession(min_interval=1.5, jitter=0.6,
                                       timeout=15, max_retries=2,
                                       backoff_cap=4.0)
    items = db.catalog_items()
    known = db.latest_prices_by_set()
    cutoff = datetime.now(timezone.utc) - timedelta(days=stale_days)

    def is_fresh(set_num: str) -> bool:
        rows = known.get(set_num) or []
        # Fresh only if something other than the catalogue feed itself is recent
        # — the Certified Store price alone is what we are trying to improve on.
        for r in rows:
            if r["retailer"] == CATALOG_SOURCE:
                continue
            try:
                ts = datetime.fromisoformat(r["observed_at"])
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if ts >= cutoff:
                return True
        return False

    todo = [i for i in items if not is_fresh(i["set_num"])]
    if limit:
        todo = todo[:limit]

    stats = {"total": len(items), "skipped": len(items) - len(todo),
             "priced": 0, "no_match": 0, "errors": 0}

    for n, item in enumerate(todo, 1):
        set_num = item["set_num"]
        try:
            # refine=True: a retailer's first-pass query coming up empty is
            # not evidence it lacks the set (see search.py's pass 2). Skipping
            # that retry here was trading real coverage for a faster sweep —
            # the retry only re-queries retailers whose targeted query would
            # differ from what pass 1 already sent, so it does not double
            # Amazon/Flipkart's request volume for sets they were always
            # going to search precisely anyway.
            # deep=True: this is background collection, not someone
            # waiting on a page — worth the extra BuyHatke request per
            # offer to also pull cross-retailer deals and full history.
            outcome = run_search(set_num, session=session, refine=True,
                                 db=db, deep=True)
            hit = next((s for s in outcome.sets if s.set_num == set_num), None)
            found = len(hit.offers) if hit else 0
            if found:
                stats["priced"] += 1
            else:
                stats["no_match"] += 1
        except Exception as exc:  # noqa: BLE001
            stats["errors"] += 1
            found = 0
            log.warning("priceall %s failed: %s", set_num, exc)
        if on_progress:
            on_progress(n, len(todo), set_num, found, stats)

    log.info("priceall: %s", stats)
    return stats


def themes(items: list[dict]) -> list[str]:
    counts: dict[str, int] = {}
    for it in items:
        t = it.get("theme")
        if t:
            counts[t] = counts.get(t, 0) + 1
    return [t for t, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]
