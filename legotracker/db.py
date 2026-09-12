"""SQLite storage: catalog, listings, and the append-only price history.

The price table is deliberately append-only. Every collection run writes a new
row per listing it saw, even when the price is unchanged, because "the price was
still X on day N" is itself data you need for a median. Storage is trivial:
75 sets x 4 retailers x 365 days is ~110k rows, a few MB.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Optional

log = logging.getLogger(__name__)

SCHEMA = """
PRAGMA foreign_keys = ON;

-- The LEGO catalog: one row per set, independent of who sells it.
CREATE TABLE IF NOT EXISTS sets (
    set_num      TEXT PRIMARY KEY,        -- "75375", "10307"
    name         TEXT,
    theme        TEXT,
    year         INTEGER,
    pieces       INTEGER,
    minifigs     INTEGER,
    msrp_usd     REAL,                    -- reference for import/arbitrage sanity
    retired      INTEGER DEFAULT 0,
    image_url    TEXT,
    watched      INTEGER DEFAULT 1,       -- 0 = discovered but not tracked
    added_at     TEXT NOT NULL
);

-- One row per (set, retailer, product page). A set can have several listings at
-- one retailer (different sellers / duplicate pages); we keep them all and let
-- the analytics layer pick the cheapest credible one.
CREATE TABLE IF NOT EXISTS listings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    set_num      TEXT NOT NULL REFERENCES sets(set_num) ON DELETE CASCADE,
    retailer     TEXT NOT NULL,           -- amazon_in | flipkart | firstcry | lego_in
    retailer_sku TEXT,                    -- ASIN / Flipkart pid / product id
    url          TEXT NOT NULL,
    title        TEXT,
    seller       TEXT,
    confidence   REAL DEFAULT 1.0,        -- how sure we are this page IS this set
    active       INTEGER DEFAULT 1,
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    UNIQUE (retailer, url)
);

-- Append-only observations.
CREATE TABLE IF NOT EXISTS price_points (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id   INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    observed_at  TEXT NOT NULL,
    price_inr    REAL,                    -- NULL when out of stock / unpriced
    mrp_inr      REAL,
    in_stock     INTEGER,
    rating       REAL,
    review_count INTEGER,
    run_id       INTEGER REFERENCES runs(id)
);

CREATE INDEX IF NOT EXISTS idx_pp_listing_time
    ON price_points (listing_id, observed_at);
CREATE INDEX IF NOT EXISTS idx_listings_set
    ON listings (set_num, retailer);

CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT,                    -- running | ok | partial | failed
    sets_seen    INTEGER DEFAULT 0,
    prices_saved INTEGER DEFAULT 0,
    notes        TEXT
);

CREATE TABLE IF NOT EXISTS alerts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT NOT NULL,
    set_num      TEXT NOT NULL,
    listing_id   INTEGER,
    kind         TEXT NOT NULL,           -- all_time_low | drop | target_hit | back_in_stock
    price_inr    REAL,
    prev_price   REAL,
    message      TEXT,
    seen         INTEGER DEFAULT 0
);

-- Cached browsable catalogue (the LEGO Certified Store's storefront).
-- Separate from `listings` on purpose: this is "what exists and what the
-- official store charges", not "a URL whose price we track over time".
CREATE TABLE IF NOT EXISTS catalog_items (
    set_num      TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    theme        TEXT,
    pieces       INTEGER,
    image        TEXT,
    url          TEXT,
    price_inr    REAL,
    mrp_inr      REAL,
    in_stock     INTEGER DEFAULT 1,
    refreshed_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_catalog_theme ON catalog_items (theme);

-- The sets YOU picked. Separate from sets.watched, which defaults to 1 and so
-- ends up set on every one of the ~900 catalogue sets: it cannot tell "I care
-- about this" from "this exists". The daily job checks watchlist sets on every
-- retailer, including the ones that block bulk traffic (Amazon, Flipkart).
CREATE TABLE IF NOT EXISTS watchlist (
    set_num      TEXT PRIMARY KEY,
    added_at     TEXT NOT NULL,
    note         TEXT
);

-- User's price targets, kept in the DB so the dashboard can edit them later.
CREATE TABLE IF NOT EXISTS targets (
    set_num      TEXT PRIMARY KEY REFERENCES sets(set_num) ON DELETE CASCADE,
    target_inr   REAL NOT NULL,
    note         TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    """Thread-safe enough for this app's needs.

    sqlite3 connections are bound to their creating thread by default, which
    breaks the threaded search server (the connection is opened on the main
    thread and used by request workers). `check_same_thread=False` lifts that
    restriction, and every write goes through `tx()`, which holds a lock — so
    exactly one writer runs at a time, which is what SQLite wants anyway.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False,
                                    timeout=15.0)
        self.conn.row_factory = sqlite3.Row
        self._write_lock = threading.RLock()
        self.journal_mode = self._set_journal_mode()
        with self._write_lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    def _set_journal_mode(self) -> str:
        """WAL is faster and allows readers during writes, but it needs real
        file locking and shared memory. That is missing on some mounts — network
        shares, iCloud Drive, and the virtualised folder mounts used by remote
        desktop sessions — where enabling it fails outright with 'disk I/O
        error'. Fall back to the portable rollback journal rather than refusing
        to open the database at all.
        """
        for mode in ("WAL", "TRUNCATE", "DELETE"):
            try:
                row = self.conn.execute(
                    f"PRAGMA journal_mode = {mode}").fetchone()
            except sqlite3.DatabaseError:
                continue
            if row and str(row[0]).upper() == mode:
                if mode != "WAL":
                    log.info("sqlite journal_mode=%s (WAL unavailable on %s)",
                             mode, self.path.parent)
                return mode
        return "unknown"

    # ------------------------------------------------------------------ utils

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._write_lock:
            try:
                yield self.conn
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    def close(self) -> None:
        self.conn.close()

    # ---------------------------------------------------------------- catalog

    def upsert_set(self, set_num: str, **fields) -> None:
        """Insert a set, or fill in any columns that are still NULL.

        Never overwrites a non-null value with a null one — enrichment from a
        thin source must not erase good data from a richer one.
        """
        cols = {k: v for k, v in fields.items() if v is not None}
        with self.tx() as c:
            c.execute(
                "INSERT OR IGNORE INTO sets (set_num, added_at) VALUES (?, ?)",
                (set_num, now_iso()),
            )
            if cols:
                # COALESCE(?, col) keeps the existing value when the new one is NULL
                sql = "UPDATE sets SET " + ", ".join(
                    f"{k} = COALESCE(?, {k})" for k in cols
                ) + " WHERE set_num = ?"
                c.execute(sql, [*cols.values(), set_num])

    def set_watched(self, set_num: str, watched: bool) -> None:
        with self.tx() as c:
            c.execute("UPDATE sets SET watched = ? WHERE set_num = ?",
                      (1 if watched else 0, set_num))

    def watched_sets(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM sets WHERE watched = 1 ORDER BY set_num"
        ).fetchall()

    def all_sets(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM sets ORDER BY set_num").fetchall()

    # --------------------------------------------------------------- listings

    def upsert_listing(self, set_num: str, retailer: str, url: str,
                       title: Optional[str] = None,
                       retailer_sku: Optional[str] = None,
                       seller: Optional[str] = None,
                       confidence: float = 1.0) -> int:
        ts = now_iso()
        with self.tx() as c:
            c.execute(
                """INSERT INTO listings
                     (set_num, retailer, retailer_sku, url, title, seller,
                      confidence, first_seen, last_seen)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(retailer, url) DO UPDATE SET
                     title      = COALESCE(excluded.title, listings.title),
                     seller     = COALESCE(excluded.seller, listings.seller),
                     confidence = excluded.confidence,
                     set_num    = excluded.set_num,
                     active     = 1,
                     last_seen  = excluded.last_seen""",
                (set_num, retailer, retailer_sku, url, title, seller,
                 confidence, ts, ts),
            )
            row = c.execute(
                "SELECT id FROM listings WHERE retailer = ? AND url = ?",
                (retailer, url),
            ).fetchone()
        return int(row["id"])

    def listings_for(self, set_num: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM listings WHERE set_num = ? AND active = 1",
            (set_num,),
        ).fetchall()

    def all_active_listings(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT l.* FROM listings l
               JOIN sets s ON s.set_num = l.set_num
               WHERE l.active = 1 AND s.watched = 1
               ORDER BY l.set_num, l.retailer"""
        ).fetchall()

    # ----------------------------------------------------------------- prices

    def record_price(self, listing_id: int, price_inr: Optional[float],
                     mrp_inr: Optional[float] = None,
                     in_stock: Optional[bool] = None,
                     rating: Optional[float] = None,
                     review_count: Optional[int] = None,
                     run_id: Optional[int] = None) -> None:
        with self.tx() as c:
            c.execute(
                """INSERT INTO price_points
                     (listing_id, observed_at, price_inr, mrp_inr, in_stock,
                      rating, review_count, run_id)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (listing_id, now_iso(), price_inr, mrp_inr,
                 None if in_stock is None else int(in_stock),
                 rating, review_count, run_id),
            )

    def history(self, listing_id: int, limit: int = 5000) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT observed_at, price_inr, in_stock FROM price_points
               WHERE listing_id = ? ORDER BY observed_at DESC LIMIT ?""",
            (listing_id, limit),
        ).fetchall()

    def latest_price(self, listing_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            """SELECT * FROM price_points WHERE listing_id = ?
               ORDER BY observed_at DESC LIMIT 1""",
            (listing_id,),
        ).fetchone()

    # ------------------------------------------------------------------- runs

    def start_run(self) -> int:
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO runs (started_at, status) VALUES (?, 'running')",
                (now_iso(),),
            )
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str, sets_seen: int,
                   prices_saved: int, notes: str = "") -> None:
        with self.tx() as c:
            c.execute(
                """UPDATE runs SET finished_at = ?, status = ?, sets_seen = ?,
                       prices_saved = ?, notes = ? WHERE id = ?""",
                (now_iso(), status, sets_seen, prices_saved, notes, run_id),
            )

    def recent_runs(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    # ----------------------------------------------------------------- alerts

    def add_alert(self, set_num: str, kind: str, message: str,
                  listing_id: Optional[int] = None,
                  price_inr: Optional[float] = None,
                  prev_price: Optional[float] = None) -> None:
        with self.tx() as c:
            c.execute(
                """INSERT INTO alerts
                     (created_at, set_num, listing_id, kind, price_inr,
                      prev_price, message)
                   VALUES (?,?,?,?,?,?,?)""",
                (now_iso(), set_num, listing_id, kind, price_inr,
                 prev_price, message),
            )

    def open_alerts(self, limit: int = 100) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    # -------------------------------------------------------------- catalogue

    def replace_catalog(self, items: list[dict]) -> None:
        """Swap in a fresh catalogue atomically.

        Replaced wholesale rather than merged: a set the store has dropped
        should disappear from browse, and a stale row is worse than a missing
        one. The delete and the insert share one transaction so a failed
        refresh can never leave you with an empty catalogue.
        """
        ts = now_iso()
        with self.tx() as c:
            c.execute("DELETE FROM catalog_items")
            c.executemany(
                """INSERT INTO catalog_items
                     (set_num, name, theme, pieces, image, url,
                      price_inr, mrp_inr, in_stock, refreshed_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                [(i["set_num"], i["name"], i.get("theme"), i.get("pieces"),
                  i.get("image"), i.get("url"), i.get("price"), i.get("mrp"),
                  1 if i.get("in_stock", True) else 0, ts)
                 for i in items],
            )

    def catalog_items(self) -> list[dict]:
        rows = self.conn.execute(
            """SELECT set_num, name, theme, pieces, image, url,
                      price_inr, mrp_inr, in_stock, refreshed_at
               FROM catalog_items ORDER BY name"""
        ).fetchall()
        return [{"set_num": r["set_num"], "name": r["name"],
                 "theme": r["theme"], "pieces": r["pieces"],
                 "image": r["image"], "url": r["url"],
                 "price": r["price_inr"], "mrp": r["mrp_inr"],
                 "in_stock": bool(r["in_stock"]),
                 "refreshed_at": r["refreshed_at"]} for r in rows]

    def latest_prices_by_set(self) -> dict[str, list[dict]]:
        """The most recent observation for every listing, grouped by set.

        Deliberately avoids window functions so it runs on older SQLite builds:
        it self-joins against MAX(observed_at) per listing instead. One query for
        the whole catalogue — doing this per tile would be ~830 queries.
        """
        rows = self.conn.execute(
            """SELECT l.set_num, l.retailer, p.price_inr, p.mrp_inr,
                      p.in_stock, p.observed_at, l.url
               FROM price_points p
               JOIN listings l ON l.id = p.listing_id
               JOIN (SELECT listing_id, MAX(observed_at) mx
                     FROM price_points GROUP BY listing_id) m
                 ON m.listing_id = p.listing_id AND m.mx = p.observed_at
               WHERE p.price_inr IS NOT NULL"""
        ).fetchall()

        out: dict[str, list[dict]] = {}
        for r in rows:
            out.setdefault(r["set_num"], []).append({
                "retailer": r["retailer"], "price": r["price_inr"],
                "mrp": r["mrp_inr"],
                "in_stock": None if r["in_stock"] is None else bool(r["in_stock"]),
                "observed_at": r["observed_at"], "url": r["url"],
            })
        return out

    def catalog_age(self) -> Optional[str]:
        row = self.conn.execute(
            "SELECT max(refreshed_at) t FROM catalog_items").fetchone()
        return row["t"] if row else None

    # -------------------------------------------------------------- watchlist

    def add_to_watchlist(self, set_num: str, note: str = "") -> None:
        with self.tx() as c:
            c.execute(
                """INSERT INTO watchlist (set_num, added_at, note) VALUES (?,?,?)
                   ON CONFLICT(set_num) DO UPDATE SET
                     note = COALESCE(NULLIF(excluded.note, ''), watchlist.note)""",
                (set_num, now_iso(), note))

    def remove_from_watchlist(self, set_num: str) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM watchlist WHERE set_num = ?", (set_num,))

    def watchlist(self) -> list[str]:
        return [r["set_num"] for r in self.conn.execute(
            "SELECT set_num FROM watchlist ORDER BY added_at, set_num")]

    # ------------------------------------------------------- daily snapshots

    def set_names(self) -> dict[str, dict]:
        """name / theme / pieces / image for every known set, one query."""
        return {r["set_num"]: dict(r) for r in self.conn.execute(
            "SELECT set_num, name, theme, pieces, image_url FROM sets")}

    def lowest_ever_by_set(self, before: Optional[str] = None) -> dict[str, dict]:
        """Lowest in-stock price per set across every retailer, optionally only
        from observations strictly before `before` (an ISO timestamp).

        An observation with unknown stock counts as in stock: several retailers
        never say, and excluding them would hide most of the history.
        """
        sql = """SELECT l.set_num, MIN(p.price_inr) low, COUNT(*) n
                 FROM price_points p JOIN listings l ON l.id = p.listing_id
                 WHERE p.price_inr IS NOT NULL AND COALESCE(p.in_stock, 1) = 1"""
        args: list = []
        if before:
            sql += " AND p.observed_at < ?"
            args.append(before)
        sql += " GROUP BY l.set_num"
        return {r["set_num"]: {"low": r["low"], "n": r["n"]}
                for r in self.conn.execute(sql, args)}

    def observations_since(self, since: str) -> list[dict]:
        """Every in-stock priced observation at or after `since`, with the
        number of earlier observations its listing already had."""
        rows = self.conn.execute(
            """SELECT l.id listing_id, l.set_num, l.retailer, l.url,
                      p.price_inr, p.mrp_inr, p.observed_at,
                      (SELECT COUNT(*) FROM price_points q
                        WHERE q.listing_id = l.id AND q.observed_at < ?
                          AND q.price_inr IS NOT NULL) prior_n
               FROM price_points p JOIN listings l ON l.id = p.listing_id
               WHERE p.observed_at >= ? AND p.price_inr IS NOT NULL
                 AND COALESCE(p.in_stock, 1) = 1""",
            (since, since)).fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------------- targets

    def set_target(self, set_num: str, target_inr: float,
                   note: str = "") -> None:
        with self.tx() as c:
            c.execute(
                """INSERT INTO targets (set_num, target_inr, note)
                   VALUES (?,?,?)
                   ON CONFLICT(set_num) DO UPDATE SET
                     target_inr = excluded.target_inr, note = excluded.note""",
                (set_num, target_inr, note),
            )

    def targets(self) -> dict[str, float]:
        return {r["set_num"]: r["target_inr"]
                for r in self.conn.execute("SELECT * FROM targets")}
