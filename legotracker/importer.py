"""Rebuild the local SQLite working database from the git-committed exports.

Why this exists: GitHub Actions runners are ephemeral — nothing on disk
survives between runs except what gets checked out from the repo. The durable
state lives in ``data/catalog.json`` and ``data/observations.csv`` (see
``export.py``); this module is the mirror image of that one, turning the text
back into a working `Database` so `collect`/`priceall` have real "what did we
see last time" state to compare against — exactly as if they were running on
rb's own Mac, where the sqlite file never goes away between runs.

Safe to run against a database that already has data. The catalogue import is
a full replace either way (see `Database.replace_catalog`); the price-history
import is careful to skip any (listing, observed_at) pair already present, so
running this against an already-populated database — or running it twice in
one workflow — adds nothing twice. Price history is append-only by design
(see db.py's own docstring), so that guard is not optional.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from pathlib import Path

from .db import Database
from .export import load_catalog, load_observations

log = logging.getLogger(__name__)


def rebuild_from_exports(db: Database, data_dir: Path) -> dict:
    data_dir = Path(data_dir)
    stats = {"catalog_items": 0, "sets": 0, "listings": 0,
             "observations": 0, "observations_skipped": 0}

    # ---- catalogue (the browsable storefront cache) ------------------------
    catalog_rows = load_catalog(data_dir)
    if catalog_rows:
        db.replace_catalog(catalog_rows)
        stats["catalog_items"] = len(catalog_rows)
        for row in catalog_rows:
            # Also register these as tracked `sets` — collect()/priceall()
            # key off `sets` + `listings`, not `catalog_items`.
            db.upsert_set(row["set_num"], name=row.get("name"))

    # ---- price history (this is what makes change-detection possible) ------
    by_set = load_observations(data_dir)
    for set_num, rows in by_set.items():
        db.upsert_set(set_num)

        grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for row in rows:
            retailer, url = row.get("retailer"), row.get("url")
            if retailer and url:
                grouped[(retailer, url)].append(row)

        for (retailer, url), group_rows in grouped.items():
            listing_id = db.upsert_listing(set_num=set_num, retailer=retailer, url=url)
            stats["listings"] += 1
            existing = db.observed_timestamps(listing_id)
            for row in group_rows:
                observed_at = row.get("observed_at")
                if not observed_at or observed_at in existing:
                    stats["observations_skipped"] += 1
                    continue
                db.backfill_price_point(
                    listing_id, observed_at,
                    price_inr=row.get("price_inr"), mrp_inr=row.get("mrp_inr"),
                    in_stock=row.get("in_stock"))
                existing.add(observed_at)
                stats["observations"] += 1

    stats["sets"] = len(db.all_sets())
    log.info("rebuild: %d catalog items, %d sets, %d listings, "
             "%d observations imported (%d already present)",
             stats["catalog_items"], stats["sets"], stats["listings"],
             stats["observations"], stats["observations_skipped"])
    return stats
