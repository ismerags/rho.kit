"""Orchestration: discover what's on sale, then keep re-pricing it.

Two verbs:

  discover()  Sweep the retailers broadly, extract set numbers from titles, and
              register (set, retailer, url) triples. Run this occasionally.

  refresh()   Re-read every registered listing and append today's price.
              Run this daily — it is the thing that builds price history.

Both are written so a single broken retailer degrades the run to "partial"
rather than killing it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterable, Optional

from .analytics import compute_stats, detect_alerts
from .db import Database
from .matching import candidate_set_numbers, normalise_set_num, score_listing
from .sources import Offer, RETAILER_LABELS, Source, build_sources
from .sources import buyhatke as buyhatke_source
from .http_client import PoliteSession

log = logging.getLogger(__name__)

#: Broad sweeps used by discover(). Amazon/Flipkart need text queries;
#: FirstCry and LEGO.com have their own enumerable listings.
DEFAULT_QUERIES = [
    "lego star wars", "lego technic", "lego city", "lego creator",
    "lego icons", "lego harry potter", "lego ninjago", "lego friends",
    "lego marvel", "lego minecraft", "lego architecture", "lego speed champions",
    "lego duplo", "lego classic", "lego botanical", "lego jurassic world",
    "lego super mario", "lego disney",
]

MIN_CONFIDENCE = 0.55


@dataclass
class RunReport:
    listings_seen: int = 0
    prices_saved: int = 0
    sets_touched: set[str] = field(default_factory=set)
    alerts: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.errors and self.prices_saved == 0:
            return "failed"
        return "partial" if self.errors else "ok"


# --------------------------------------------------------------------------
# discovery
# --------------------------------------------------------------------------

def _register_offer(db: Database, offer: Offer,
                    report: RunReport) -> Optional[str]:
    """Work out which set an offer is, and register it. Returns the set number."""
    cands = candidate_set_numbers(offer.title)
    if not cands:
        return None

    best_num, best_conf, best_reason = None, 0.0, ""
    for num in cands[:3]:
        res = score_listing(offer.title, num, brand=offer.brand,
                            price_inr=offer.price_inr)
        if res.ok and res.confidence > best_conf:
            best_num, best_conf, best_reason = num, res.confidence, res.reason

    if best_num is None or best_conf < MIN_CONFIDENCE:
        return None

    db.upsert_set(best_num)
    db.upsert_listing(
        set_num=best_num,
        retailer=offer.retailer,
        url=offer.url,
        title=offer.title,
        retailer_sku=offer.sku,
        seller=offer.seller,
        confidence=best_conf,
    )
    report.sets_touched.add(best_num)
    log.debug("registered %s <- [%s] %s (%.2f: %s)",
              best_num, offer.retailer, offer.title[:60], best_conf, best_reason)
    return best_num


def discover(db: Database, session: PoliteSession,
             sources: Optional[list[str]] = None,
             queries: Optional[list[str]] = None,
             firstcry_pages: int = 8,
             lego_themes: Optional[list[str]] = None) -> RunReport:
    report = RunReport()
    adapters = build_sources(session, sources)

    for adapter in adapters:
        try:
            offers: list[Offer] = []

            if adapter.name == "firstcry":
                offers = adapter.browse_brand(max_pages=firstcry_pages)
            elif adapter.name == "lego_in":
                offers = adapter.browse_catalog(themes=lego_themes)
            else:
                for query in (queries or DEFAULT_QUERIES):
                    offers.extend(adapter.search(query, limit=30))

            for offer in offers:
                report.listings_seen += 1
                _register_offer(db, offer, report)

        except Exception as exc:  # noqa: BLE001
            msg = f"{adapter.name}: {exc}"
            log.error("discovery failed for %s", msg)
            report.errors.append(msg)

    log.info("discovery: %d offers seen, %d sets registered",
             report.listings_seen, len(report.sets_touched))
    return report


# --------------------------------------------------------------------------
# refresh
# --------------------------------------------------------------------------

def _absorb_deals(db: Database, run_id: int, set_num: str,
                  deals: list[dict], report: RunReport) -> None:
    """Fold BuyHatke's cross-retailer comparison into OUR OWN retailers'
    price history — e.g. its "Amazon" deal becomes a real amazon_in listing,
    priced without ever asking amazon.in directly. Skipped for any site we
    don't already have an adapter for; there is nowhere sensible to put it."""
    for deal in deals:
        site = str(deal.get("site_name") or "").strip().lower()
        retailer = buyhatke_source.SITE_NAME_TO_RETAILER.get(site)
        if not retailer or retailer == buyhatke_source.BuyHatke.name:
            continue
        link = deal.get("link")
        price = deal.get("price")
        if not link or not isinstance(price, (int, float)):
            continue
        mrp = deal.get("mrpFloat")
        listing_id = db.upsert_listing(
            set_num=set_num, retailer=retailer, url=link,
            title=deal.get("prod"), confidence=0.8)
        db.record_price(
            listing_id, price_inr=float(price),
            mrp_inr=(float(mrp) if isinstance(mrp, (int, float))
                    and mrp >= price else None),
            in_stock=True, run_id=run_id)
        report.prices_saved += 1
        report.sets_touched.add(set_num)


def _backfill_history(db: Database, listing_id: int,
                      points: list[tuple[str, float]]) -> int:
    """Import BuyHatke's own historical price series for one listing.

    Runs every refresh but only ever adds what is missing: BuyHatke's past
    is immutable (only new points appear at the tail), so anything already
    present is left alone rather than re-inserted.
    """
    if not points:
        return 0
    existing = db.observed_timestamps(listing_id)
    inserted = 0
    for at, price in points:
        # BuyHatke's own format ("2024-04-19 05:05:31") normalised to match
        # this project's ISO-with-offset convention, so a listing's history
        # sorts correctly whether it came from a live scrape or a backfill.
        # BuyHatke does not publish a timezone for these; treated as UTC —
        # close enough for a "when did the price move" signal.
        observed_at = at.replace(" ", "T") + "+00:00"
        if observed_at in existing:
            continue
        db.backfill_price_point(listing_id, observed_at, price_inr=price)
        existing.add(observed_at)
        inserted += 1
    return inserted


def refresh(db: Database, session: PoliteSession,
            sources: Optional[list[str]] = None,
            only_sets: Optional[Iterable[str]] = None) -> RunReport:
    """Re-price every already-registered listing.

    Not on the production path any more — `priceall` (catalog.py::price_all,
    via search.py's deep search) is what scripts/weekly.sh and the GitHub
    Actions workflow actually run, and it does the discover-and-price-in-one
    sweep that made this two-step discover()/refresh() pair mostly redundant.
    Kept working for anyone using `discover`/`collect` by hand: BuyHatke can
    re-fetch ANY listing's stored real vendor URL through its paste-link
    route regardless of which retailer it is labelled as, so it refreshes
    everything by default; a dormant per-site adapter only gets used when
    named explicitly via --sources.
    """
    report = RunReport()
    run_id = db.start_run()

    adapters = {a.name: a for a in build_sources(session, sources)}
    buyhatke_adapter = adapters.get(buyhatke_source.BuyHatke.name)
    targets = db.targets()
    wanted = {normalise_set_num(s) for s in only_sets} if only_sets else None

    listings = [l for l in db.all_active_listings()
                if (l["retailer"] in adapters or buyhatke_adapter is not None)
                and (wanted is None or l["set_num"] in wanted)]

    log.info("refreshing %d listings across %d retailers",
             len(listings), len(adapters))

    for listing in listings:
        # A listing's own retailer wins if that adapter was explicitly named
        # (a dormant per-site scraper someone asked for via --sources);
        # otherwise BuyHatke fetches it via the paste-link route, whatever
        # retailer it is labelled as.
        use_own_adapter = (listing["retailer"] in adapters
                          and listing["retailer"] != buyhatke_source.BuyHatke.name)
        adapter: Optional[Source] = adapters.get(listing["retailer"])
        report.listings_seen += 1

        prev = db.latest_price(listing["id"])
        prev_price = prev["price_inr"] if prev else None

        bh_data = None
        try:
            if use_own_adapter:
                offer = adapter.fetch_offer(listing["url"])
            else:
                # Fetched once here so the cross-vendor deals and the full
                # price history below can reuse it instead of hitting
                # BuyHatke a second time for the same page.
                bh_data = buyhatke_source.fetch_detail(
                    session, buyhatke_source.proxy_url(listing["url"]))
                offer = (buyhatke_source.offer_from_product_data(listing["url"], bh_data)
                        if bh_data else None)
        except Exception as exc:  # noqa: BLE001
            log.warning("refresh %s %s failed: %s",
                        listing["retailer"], listing["set_num"], exc)
            report.errors.append(f"{listing['retailer']}/{listing['set_num']}: {exc}")
            continue

        if offer is None:
            continue

        # Guard: never write a price against the wrong set. Retailers do
        # recycle URLs, and a stale URL can 302 to a different product.
        if offer.title:
            res = score_listing(offer.title, listing["set_num"],
                                brand=offer.brand, price_inr=offer.price_inr)
            if not res.ok:
                log.warning("skipping %s %s — page no longer matches (%s)",
                            listing["retailer"], listing["set_num"], res.reason)
                continue

        if getattr(adapter, "catalog_only", False):
            # Availability only; a NULL price row still records "we looked".
            db.record_price(listing["id"], None, in_stock=offer.in_stock,
                            run_id=run_id)
            continue

        db.record_price(
            listing["id"],
            price_inr=offer.price_inr,
            mrp_inr=offer.mrp_inr,
            in_stock=offer.in_stock,
            rating=offer.rating,
            review_count=offer.review_count,
            run_id=run_id,
        )
        report.prices_saved += 1
        report.sets_touched.add(listing["set_num"])

        if bh_data is not None:
            _absorb_deals(db, run_id, listing["set_num"],
                         buyhatke_source.deal_list(bh_data), report)
            _backfill_history(db, listing["id"],
                              buyhatke_source.history_points(bh_data))

        # ---- alerts --------------------------------------------------------
        history = [dict(r) for r in db.history(listing["id"])]
        stats = compute_stats(history)
        for alert in detect_alerts(
            set_num=listing["set_num"],
            listing_id=listing["id"],
            retailer=RETAILER_LABELS.get(listing["retailer"], listing["retailer"]),
            stats=stats,
            prev_price=prev_price,
            target=targets.get(listing["set_num"]),
        ):
            db.add_alert(set_num=listing["set_num"],
                         listing_id=listing["id"], **alert)
            report.alerts += 1

    db.finish_run(run_id, report.status, len(report.sets_touched),
                  report.prices_saved,
                  notes="; ".join(report.errors[:5]))

    log.info("refresh done: %d prices saved, %d alerts, %d errors",
             report.prices_saved, report.alerts, len(report.errors))
    return report


# --------------------------------------------------------------------------
# targeted search (fills gaps for a set nobody discovered yet)
# --------------------------------------------------------------------------

def hunt_set(db: Database, session: PoliteSession, set_num: str,
             sources: Optional[list[str]] = None) -> int:
    """Search every retailer specifically for one set number."""
    set_num = normalise_set_num(set_num)
    report = RunReport()
    found = 0

    for adapter in build_sources(session, sources):
        try:
            offers = adapter.search(f"LEGO {set_num}", limit=20)
        except Exception as exc:  # noqa: BLE001
            log.warning("hunt %s on %s failed: %s", set_num, adapter.name, exc)
            continue

        for offer in offers:
            res = score_listing(offer.title, set_num, brand=offer.brand,
                                price_inr=offer.price_inr)
            if not res.ok:
                continue
            db.upsert_set(set_num)
            db.upsert_listing(set_num=set_num, retailer=offer.retailer,
                              url=offer.url, title=offer.title,
                              retailer_sku=offer.sku, confidence=res.confidence)
            found += 1

    db.set_watched(set_num, True)
    log.info("hunt %s -> %d listings", set_num, found)
    return found
