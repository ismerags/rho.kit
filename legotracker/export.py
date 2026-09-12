"""Export the tracking database to plain text files that live in the repo.

Why this exists
---------------
The public website is a **static site**. Nothing runs on a server, so the
generator that builds it cannot reach the SQLite database sitting on rb's Mac.
The data has to travel to GitHub somehow, and there are only two honest ways:

1. commit ``data/lego.sqlite3`` — one opaque 3 MB blob, rewritten in full every
   week, so git stores a brand new copy of it every single time; or
2. commit plain text.

This module does (2), for three reasons that all point the same way:

* **Git likes text.** ``observations.csv`` only ever grows at the end. A weekly
  run appends ~5,800 lines (~350 KB); git stores that as a small delta instead
  of a fresh 3 MB blob. Over a year that is the difference between ~18 MB and
  ~156 MB of repository.
* **The site build needs no dependencies.** ``site.py`` reads these files with
  nothing but the standard library, so the GitHub Actions workflow has no
  ``pip install`` step to break.
* **A public repo should have readable data.** Anyone can open the CSV. That is
  the point of publishing it.

Everything that needs the retailer adapters (and therefore ``requests``)
happens *here*, on the machine that does the collecting. The generator gets
finished, enriched rows and only has to lay them out.

Determinism matters: rows are sorted before writing so that re-exporting
unchanged data produces a byte-identical file and an empty git diff.
"""

from __future__ import annotations

import csv
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:                      # type hints only — never imported at runtime
    from .db import Database


# NOTE: `catalog` and `Database` are imported lazily, inside the functions that
# need them.
#
# This module has two halves with very different homes. The `export_*` half
# runs on the collecting machine, where everything is installed. The `load_*`
# half runs inside GitHub Actions, where **nothing** is installed — and it is
# reached via `site.py`, which imports this module. A top-level
# `from .catalog import clean_name` would therefore pull `catalog` →
# `http_client` → `requests` into the deploy build and break it, several files
# away from anything that looks related.

log = logging.getLogger(__name__)

#: Written into meta.json so the site can say what produced it.
EXPORT_FORMAT = 1

OBSERVATION_COLUMNS = [
    "set_num", "retailer", "observed_at",
    "price_inr", "mrp_inr", "in_stock", "url",
]

CATALOG_COLUMNS = [
    "set_num", "name", "display_name", "theme", "pieces",
    "image", "url", "price", "mrp", "in_stock", "refreshed_at",
]


def _num(value) -> Optional[float]:
    """Round money to paise. Floats that differ in the 12th decimal place would
    otherwise produce a spurious git diff on every export."""
    if value is None:
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def export_catalog(db: "Database", path: Path) -> int:
    """Write the browsable catalogue as JSON.

    ``display_name`` is computed here rather than in the generator because
    ``clean_name`` lives in ``catalog.py``, which imports the retailer adapters.
    Doing it now keeps the generator free of third-party imports.
    """
    from .catalog import clean_name          # lazy: see the note at the top

    rows = []
    for item in db.catalog_items():
        set_num = item["set_num"]
        rows.append({
            "set_num": set_num,
            "name": item.get("name") or "",
            "display_name": clean_name(item.get("name") or "", set_num),
            "theme": item.get("theme"),
            "pieces": item.get("pieces"),
            "image": item.get("image"),
            "url": item.get("url"),
            "price": _num(item.get("price")),
            "mrp": _num(item.get("mrp")),
            "in_stock": bool(item.get("in_stock", True)),
            "refreshed_at": item.get("refreshed_at"),
        })

    rows.sort(key=lambda r: r["set_num"])
    path.parent.mkdir(parents=True, exist_ok=True)
    # sort_keys + a trailing newline so the file is stable across Python
    # versions and text editors; indent=1 keeps it diffable without bloating it.
    path.write_text(
        json.dumps(rows, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return len(rows)


def export_observations(db: "Database", path: Path) -> int:
    """Write every price observation ever recorded, as CSV.

    Append-only in spirit: the file is rewritten in full, but because the
    content is sorted and the database only ever gains rows, the result differs
    from last week's only by added lines.

    Observations with no price (out of stock, or a retailer that reached the
    page but found no figure) are kept. "Toycra had no price on 3 September" is
    a real fact and the freshness logic uses it.
    """
    sql = """
        SELECT l.set_num, l.retailer, p.observed_at,
               p.price_inr, p.mrp_inr, p.in_stock, l.url
        FROM price_points p
        JOIN listings l ON l.id = p.listing_id
        ORDER BY l.set_num, l.retailer, p.observed_at, p.id
    """
    rows = db.conn.execute(sql).fetchall()

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(OBSERVATION_COLUMNS)
        for r in rows:
            writer.writerow([
                r["set_num"],
                r["retailer"],
                r["observed_at"],
                "" if r["price_inr"] is None else _num(r["price_inr"]),
                "" if r["mrp_inr"] is None else _num(r["mrp_inr"]),
                "" if r["in_stock"] is None else int(bool(r["in_stock"])),
                r["url"] or "",
            ])
    return len(rows)


def export_meta(db: "Database", path: Path, *,
                catalog_rows: int, observation_rows: int) -> dict:
    """Small summary file — what the site shows in its 'data freshness' footer."""
    day_row = db.conn.execute(
        "SELECT COUNT(DISTINCT substr(observed_at, 1, 10)) d FROM price_points"
    ).fetchone()
    span = db.conn.execute(
        "SELECT MIN(observed_at) a, MAX(observed_at) b FROM price_points"
    ).fetchone()
    retailers = [r["retailer"] for r in db.conn.execute(
        "SELECT DISTINCT retailer FROM listings ORDER BY retailer")]

    from .db import now_iso                  # lazy: see the note at the top

    meta = {
        "format": EXPORT_FORMAT,
        "exported_at": now_iso(),
        "catalog_items": catalog_rows,
        "observations": observation_rows,
        "distinct_days": (day_row["d"] if day_row else 0) or 0,
        "first_observation": span["a"] if span else None,
        "last_observation": span["b"] if span else None,
        "retailers": retailers,
        "catalog_refreshed_at": db.catalog_age(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(meta, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return meta


def export_all(db: "Database", data_dir: Path) -> dict:
    """Write catalog.json, observations.csv and meta.json into `data_dir`."""
    data_dir = Path(data_dir)
    n_cat = export_catalog(db, data_dir / "catalog.json")
    n_obs = export_observations(db, data_dir / "observations.csv")
    meta = export_meta(db, data_dir / "meta.json",
                       catalog_rows=n_cat, observation_rows=n_obs)
    log.info("exported %d catalogue rows and %d observations to %s",
             n_cat, n_obs, data_dir)
    return meta


# --------------------------------------------------------------------- read

def load_catalog(data_dir: Path) -> list[dict]:
    path = Path(data_dir) / "catalog.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def load_observations(data_dir: Path) -> dict[str, list[dict]]:
    """Read observations.csv back, grouped by set number.

    Standard library only — this is the half that runs inside GitHub Actions.
    A malformed row is skipped rather than fatal: one bad line should not stop
    the whole site from publishing.
    """
    path = Path(data_dir) / "observations.csv"
    out: dict[str, list[dict]] = {}
    if not path.exists():
        return out

    with path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            set_num = (row.get("set_num") or "").strip()
            if not set_num:
                continue
            try:
                price = float(row["price_inr"]) if row.get("price_inr") else None
                mrp = float(row["mrp_inr"]) if row.get("mrp_inr") else None
            except ValueError:
                continue
            stock = row.get("in_stock")
            out.setdefault(set_num, []).append({
                "retailer": (row.get("retailer") or "").strip(),
                "observed_at": (row.get("observed_at") or "").strip(),
                "price_inr": price,
                "mrp_inr": mrp,
                "in_stock": None if stock in (None, "") else bool(int(stock)),
                "url": (row.get("url") or "").strip(),
            })
    return out


def load_meta(data_dir: Path) -> dict:
    path = Path(data_dir) / "meta.json"
    if not path.exists():
        return {"format": EXPORT_FORMAT, "observations": 0, "catalog_items": 0}
    return json.loads(path.read_text(encoding="utf-8"))
