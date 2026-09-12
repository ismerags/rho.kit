"""The daily job: sweep prices, find new lows, post a snapshot to Discord.

    python -m legotracker daily

What it does, in order — every step is independent, so one broken retailer
makes the run "partial", never "failed":

  1. LEGO Certified Store  whole catalogue, 4 requests   (catalog.refresh)
  2. Toycra, Jaiman Toys   whole LEGO range, a few requests each
  3. Your watchlist        each set on Amazon, Flipkart, FirstCry and Hamleys
  4. New lows              any set now cheaper than anything seen before
  5. Snapshot              cheapest price per set -> CSV + Discord message

Why the split between 1-2 and 3: the three Shopify stores publish their whole
price list as JSON, so ~900 sets cost a handful of requests. Amazon and Flipkart
only answer one search at a time, and they block scripts that ask too often —
830 searches a day there would get the Mac blocked within the hour. So the big
marketplaces are checked only for the sets you actually care about.
"""

from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from . import catalog as catalog_mod
from .analytics import MIN_MEANINGFUL_MOVE, MIN_MEANINGFUL_RUPEES
from .db import Database, now_iso
from .http_client import PoliteSession
from .matching import candidate_set_numbers, score_listing
from .search import plain_reason, search as run_search
from .sources import CATALOG_SOURCE, RETAILER_LABELS, build_sources

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT_DIR = ROOT / "data" / "snapshots"

SWEEP_STORES = ("toycra", "jaimantoys")
WATCH_SOURCES = ("amazon_in", "flipkart", "firstcry", "hamleys")

#: How recent a price must be to count as "today's" in the snapshot. Covers
#: this run plus anything a search picked up in the previous day.
FRESH_HOURS = 24
TOP_DEALS = 15
MAX_ALERTS_POSTED = 20


# --------------------------------------------------------------------------
# results
# --------------------------------------------------------------------------

@dataclass
class SourceHealth:
    retailer: str
    checked: int = 0            # sets / products successfully read
    errors: int = 0
    last_error: Optional[str] = None

    @property
    def label(self) -> str:
        return RETAILER_LABELS.get(self.retailer, self.retailer)

    @property
    def ok(self) -> bool:
        return self.errors == 0 and self.checked > 0


@dataclass
class SnapshotRow:
    set_num: str
    name: str
    theme: Optional[str]
    pieces: Optional[int]
    image: Optional[str]
    best_price: float
    best_retailer: str
    best_url: str
    shops: int                   # retailers with a fresh in-stock price
    official: Optional[float]    # LEGO Certified Store list price
    lowest_ever: Optional[float]

    @property
    def pct_below_official(self) -> Optional[float]:
        if not self.official or self.official <= 0:
            return None
        return round((self.official - self.best_price) / self.official * 100, 1)

    @property
    def per_piece(self) -> Optional[float]:
        return round(self.best_price / self.pieces, 2) if self.pieces else None


@dataclass
class NewLow:
    set_num: str
    name: str
    retailer: str
    url: str
    price: float
    prev_low: float
    official: Optional[float]
    image: Optional[str]
    listing_id: int

    @property
    def drop_pct(self) -> float:
        return round((self.prev_low - self.price) / self.prev_low * 100, 1)


@dataclass
class DailyReport:
    started_at: str
    health: dict[str, SourceHealth] = field(default_factory=dict)
    watchlist: list[str] = field(default_factory=list)
    snapshot: list[SnapshotRow] = field(default_factory=list)
    new_lows: list[NewLow] = field(default_factory=list)
    csv_path: Optional[Path] = None
    errors: list[str] = field(default_factory=list)

    def h(self, retailer: str) -> SourceHealth:
        return self.health.setdefault(retailer, SourceHealth(retailer))

    @property
    def status(self) -> str:
        if not self.snapshot:
            return "failed"
        return "partial" if any(not h.ok for h in self.health.values()) else "ok"


# --------------------------------------------------------------------------
# 1-2. whole-store sweeps
# --------------------------------------------------------------------------

SKU_PREFIX_RE = re.compile(r"(?i)^lego[\s\-_]*")


def identify(title: str, sku: Optional[str], vendor: Optional[str],
             price: Optional[float]) -> Optional[str]:
    """Set number for a Shopify product, or None if we are not sure.

    The shop's SKU is tried first ("75453", or Toycra's "Lego72537"), but only
    accepted if the same number is in the title — the strict matcher decides,
    exactly as it does for searches. A mislabelled SKU must not file a price
    against the wrong set.
    """
    tries: list[str] = []
    if sku:
        digits = SKU_PREFIX_RE.sub("", sku.strip())
        if re.fullmatch(r"\d{4,7}", digits):
            tries.append(digits)
    tries += [c for c in candidate_set_numbers(title)[:3] if c not in tries]
    for num in tries:
        res = score_listing(title, num, brand=vendor, price_inr=price)
        if res.ok:
            return num
    return None


def sweep_store(db: Database, session: PoliteSession, retailer: str,
                run_id: Optional[int], health: SourceHealth) -> int:
    adapter = build_sources(session, [retailer])[0]
    rows = adapter.browse_brand(vendor="LEGO")
    if not rows:
        health.errors += 1
        health.last_error = adapter.last_error or "store listing came back empty"
        return 0

    known = db.set_names()
    saved = 0
    for row in rows:
        title = row.get("title") or ""
        price = row.get("price")
        num = identify(title, row.get("sku"), row.get("vendor"), price)
        if not num or price is None:
            continue
        try:
            if num not in known:
                db.upsert_set(num, name=title, pieces=catalog_mod.pieces_of(title),
                              image_url=row.get("image"))
                known[num] = {"name": title}
            listing_id = db.upsert_listing(
                set_num=num, retailer=retailer, url=row["url"], title=title,
                retailer_sku=row.get("sku"), confidence=0.95)
            compare = row.get("compare_at")
            db.record_price(listing_id, price,
                            mrp_inr=compare if (compare and compare > price) else None,
                            in_stock=row.get("in_stock"), run_id=run_id)
            saved += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] could not record %s: %s", retailer, num, exc)
    health.checked = saved
    log.info("[%s] sweep: %d LEGO products, %d prices recorded",
             retailer, len(rows), saved)
    return saved


# --------------------------------------------------------------------------
# 3. watchlist on the marketplaces
# --------------------------------------------------------------------------

def check_watchlist(db: Database, session: PoliteSession, sets: list[str],
                    report: DailyReport) -> None:
    for set_num in sets:
        try:
            outcome = run_search(set_num, session=session,
                                 sources=list(WATCH_SOURCES), refine=False, db=db)
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"watchlist {set_num}: {exc}")
            continue
        for st in outcome.statuses:
            h = report.h(st.retailer)
            if st.error:
                h.errors += 1
                h.last_error = st.error
            else:
                h.checked += 1


# --------------------------------------------------------------------------
# 4. new lows
# --------------------------------------------------------------------------

def find_new_lows(db: Database, since: str) -> list[NewLow]:
    """Sets whose cheapest price in this run beats everything seen before it.

    Two guards against noise:
      * the move must be worth acting on — 2% and ₹50, same rule as alerts;
      * a listing's FIRST observation never counts. The first day Toycra is
        swept, it is cheaper than the Certified Store on hundreds of sets —
        that is a new source, not a price drop, and would bury the channel.
    """
    prior = db.lowest_ever_by_set(before=since)
    names = db.set_names()
    official = _official_prices(db)

    best_now: dict[str, dict] = {}
    for o in db.observations_since(since):
        if o["prior_n"] < 1:
            continue
        cur = best_now.get(o["set_num"])
        if cur is None or o["price_inr"] < cur["price_inr"]:
            best_now[o["set_num"]] = o

    out: list[NewLow] = []
    for set_num, o in best_now.items():
        p = prior.get(set_num)
        if not p or p["low"] is None:
            continue
        prev_low, price = float(p["low"]), float(o["price_inr"])
        if prev_low - price < max(MIN_MEANINGFUL_RUPEES, prev_low * MIN_MEANINGFUL_MOVE):
            continue
        meta = names.get(set_num) or {}
        out.append(NewLow(
            set_num=set_num,
            name=catalog_mod.clean_name(meta.get("name") or "", set_num) or set_num,
            retailer=o["retailer"], url=o["url"], price=price, prev_low=prev_low,
            official=official.get(set_num), image=meta.get("image_url"),
            listing_id=o["listing_id"]))
    out.sort(key=lambda n: -n.drop_pct)
    return out


# --------------------------------------------------------------------------
# 5. snapshot
# --------------------------------------------------------------------------

def _official_prices(db: Database) -> dict[str, float]:
    """LEGO's own Indian price per set: the Certified Store's list price
    (its crossed-out price during a sale, otherwise its price)."""
    out = {}
    for it in db.catalog_items():
        p = it.get("mrp") or it.get("price")
        if p:
            out[it["set_num"]] = float(p)
    return out


def build_snapshot(db: Database, fresh_since: str) -> list[SnapshotRow]:
    known = db.latest_prices_by_set()
    names = db.set_names()
    official = _official_prices(db)
    catalog = {c["set_num"]: c for c in db.catalog_items()}
    lows = db.lowest_ever_by_set()

    rows: list[SnapshotRow] = []
    for set_num, obs in known.items():
        fresh = [o for o in obs
                 if o["observed_at"] >= fresh_since and o["price"]
                 and o["in_stock"] is not False]
        if not fresh:
            continue
        best = min(fresh, key=lambda o: o["price"])
        meta = names.get(set_num) or {}
        cat = catalog.get(set_num) or {}
        raw_name = cat.get("name") or meta.get("name") or ""
        rows.append(SnapshotRow(
            set_num=set_num,
            name=catalog_mod.clean_name(raw_name, set_num) or set_num,
            theme=cat.get("theme") or meta.get("theme"),
            pieces=cat.get("pieces") or meta.get("pieces"),
            image=cat.get("image") or meta.get("image_url"),
            best_price=float(best["price"]),
            best_retailer=best["retailer"],
            best_url=best["url"],
            shops=len({o["retailer"] for o in fresh}),
            official=official.get(set_num),
            lowest_ever=(lows.get(set_num) or {}).get("low"),
        ))
    # Whole-percent buckets, then rupees saved: at "25% off" a ₹30,000 set is
    # a bigger deal than a ₹900 one and should be listed first.
    rows.sort(key=lambda r: (-round(r.pct_below_official)
                             if r.pct_below_official is not None else 1e9,
                             -((r.official or 0) - r.best_price), r.set_num))
    return rows


_NOISE = re.compile(r"[®™]|\(r\)", re.I)


def short_name(name: str) -> str:
    """'LEGO® Technic™ Aston Martin …' -> 'Technic Aston Martin …' for chat."""
    out = _NOISE.sub("", name or "")
    out = re.sub(r"^\s*lego\s+", "", out, flags=re.I)
    return re.sub(r"\s{2,}", " ", out).strip() or name


def snapshot_csv(rows: list[SnapshotRow]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["set_num", "name", "theme", "pieces", "cheapest_price_inr",
                "cheapest_at", "pct_below_official", "official_price_inr",
                "price_per_piece", "shops_with_price", "lowest_ever_inr", "url"])
    for r in rows:
        w.writerow([r.set_num, r.name, r.theme or "", r.pieces or "",
                    round(r.best_price), RETAILER_LABELS.get(r.best_retailer, r.best_retailer),
                    "" if r.pct_below_official is None else r.pct_below_official,
                    "" if r.official is None else round(r.official),
                    "" if r.per_piece is None else r.per_piece,
                    r.shops,
                    "" if r.lowest_ever is None else round(r.lowest_ever),
                    r.best_url])
    # BOM so Excel on a Mac opens the ₹ and names correctly.
    return ("\ufeff" + buf.getvalue()).encode("utf-8")


# --------------------------------------------------------------------------
# formatting for Discord
# --------------------------------------------------------------------------

def inr(value: Optional[float]) -> str:
    """Indian digit grouping: 103184 -> ₹1,03,184."""
    if value is None:
        return "—"
    n = int(round(value))
    s = str(abs(n))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts + [tail])
    return ("-₹" if n < 0 else "₹") + s


_MD = re.compile(r"([\\*_~`|\[\]()>#])")


def md(text: str, limit: int = 48) -> str:
    text = short_name(text)
    text = text if len(text) <= limit else text[:limit - 1].rstrip() + "…"
    return _MD.sub(r"\\\1", text)


def label(retailer: str) -> str:
    return RETAILER_LABELS.get(retailer, retailer)


GREEN, AMBER, BLUE = 0x0CA30C, 0xFAB219, 0x2A78D6


def alert_embeds(lows: list[NewLow]) -> list[dict]:
    embeds = []
    for n in lows[:MAX_ALERTS_POSTED]:
        lines = [f"**{inr(n.price)}** at {label(n.retailer)} — lowest we've "
                 f"recorded anywhere",
                 f"Previous low {inr(n.prev_low)} · {n.drop_pct:g}% lower"]
        if n.official and n.official > n.price:
            off = (n.official - n.price) / n.official * 100
            lines.append(f"LEGO's price {inr(n.official)} · {off:.0f}% off")
        e = {"title": f"{n.set_num} · {short_name(n.name)}"[:250], "url": n.url,
             "description": "\n".join(lines), "color": GREEN}
        if n.image:
            e["thumbnail"] = {"url": n.image}
        embeds.append(e)
    return embeds


def snapshot_embeds(report: DailyReport, today: str) -> list[dict]:
    rows = report.snapshot
    deals = [r for r in rows if (r.pct_below_official or 0) > 0][:TOP_DEALS]
    sweeps = [report.health[s] for s in (CATALOG_SOURCE, *SWEEP_STORES)
              if s in report.health]
    head = (f"**{len(rows)} sets priced** in the last {FRESH_HOURS} h"
            + (" across " + ", ".join(f"{h.label} ({h.checked})" for h in sweeps)
               if sweeps else " (from recent searches)")
            + ".\n\n**Biggest discounts vs LEGO's own price**\n")
    lines = [f"`{r.set_num}` [{md(r.name, 40)}]({r.best_url}) — "
             f"**{inr(r.best_price)}** at {label(r.best_retailer)} · "
             f"{r.pct_below_official:.0f}% off {inr(r.official)}" for r in deals]
    desc = head + (_fit(lines, MAX_DESC - len(head)) if lines
                   else "_Nothing below LEGO's price today._")
    main = {"title": f"Cheapest LEGO prices in India — {today}",
            "description": desc, "color": BLUE,
            "footer": {"text": "Full list of every set in the attached CSV. "
                               "“LEGO's own price” = the LEGO Certified Store's list price."}}
    embeds = [main]

    if report.watchlist:
        by_set = {r.set_num: r for r in rows}
        wl = []
        for num in report.watchlist:
            r = by_set.get(num)
            if not r:
                wl.append(f"`{num}` no in-stock price found today")
                continue
            low = (f" · lowest ever {inr(r.lowest_ever)}"
                   if r.lowest_ever and r.lowest_ever < r.best_price - 1 else
                   " · **lowest ever**" if r.lowest_ever else "")
            wl.append(f"`{num}` [{md(r.name, 36)}]({r.best_url}) — "
                      f"**{inr(r.best_price)}** at {label(r.best_retailer)}{low}")
        embeds.append({"title": "Your watchlist", "color": BLUE,
                       "description": _fit(wl, MAX_DESC)})

    broken = [h for h in report.health.values() if not h.ok]
    if broken:
        text = "\n".join(
            f"**{h.label}** — couldn't check "
            f"({plain_reason(h.last_error) or 'no data'}); "
            f"{h.checked} ok, {h.errors} failed" for h in broken)
        embeds.append({"title": "Couldn't check everywhere", "color": AMBER,
                       "description": (text + "\n\nPrices from these shops are "
                                       "missing from today's snapshot — the "
                                       "real cheapest may be there.")[:MAX_DESC]})
    return embeds


MAX_DESC = 4000


def _fit(lines: list[str], budget: int) -> str:
    """Join whole lines until the budget runs out. Slicing a joined string
    could cut a [name](url) link in half and break the message's markdown."""
    out, used = [], 0
    for ln in lines:
        if used + len(ln) + 1 > budget:
            out.append(f"…and {len(lines) - len(out)} more in the CSV")
            break
        out.append(ln)
        used += len(ln) + 1
    return "\n".join(out)


def discord_messages(report: DailyReport, today: str,
                     csv_bytes: Optional[bytes]) -> list[dict]:
    from .notify import pack_embeds

    msgs: list[dict] = []
    if report.new_lows:
        n = len(report.new_lows)
        extra = (f" Showing the biggest {MAX_ALERTS_POSTED}; the rest are in "
                 f"today's CSV." if n > MAX_ALERTS_POSTED else "")
        groups = pack_embeds(alert_embeds(report.new_lows))
        for i, g in enumerate(groups):
            msgs.append({"content": (f"**{n} new lowest price{'s' if n != 1 else ''}**"
                                     f" today.{extra}") if i == 0 else None,
                         "embeds": g})
    groups = pack_embeds(snapshot_embeds(report, today))
    for i, g in enumerate(groups):
        m: dict = {"embeds": g}
        if i == len(groups) - 1 and csv_bytes:
            m["file"] = (f"lego-cheapest-{datetime.now().strftime('%Y-%m-%d')}.csv",
                         csv_bytes, "text/csv")
        msgs.append(m)
    return msgs


# --------------------------------------------------------------------------
# the job
# --------------------------------------------------------------------------

def run_daily(db: Database, session: Optional[PoliteSession] = None,
              post: bool = True, sweep: bool = True,
              watch: bool = True) -> DailyReport:
    session = session or PoliteSession(min_interval=1.5, jitter=0.8, timeout=20,
                                       max_retries=2, backoff_cap=8.0)
    started = now_iso()
    report = DailyReport(started_at=started)
    run_id = db.start_run()

    if sweep:
        # 1. Certified Store — also rebuilds the browse catalogue.
        h = report.h(CATALOG_SOURCE)
        try:
            h.checked = catalog_mod.refresh(db, session=session)
            if not h.checked:
                h.errors, h.last_error = 1, "catalogue came back empty"
        except Exception as exc:  # noqa: BLE001
            h.errors, h.last_error = 1, str(exc)
            report.errors.append(f"{CATALOG_SOURCE}: {exc}")

        # 2. Toycra and Jaiman Toys, whole LEGO range.
        for store in SWEEP_STORES:
            h = report.h(store)
            try:
                sweep_store(db, session, store, run_id, h)
            except Exception as exc:  # noqa: BLE001
                h.errors, h.last_error = h.errors + 1, str(exc)
                report.errors.append(f"{store}: {exc}")

    # 3. Watchlist on the marketplaces.
    report.watchlist = db.watchlist()
    if watch and report.watchlist:
        check_watchlist(db, session, report.watchlist, report)

    # 4 + 5.
    report.new_lows = find_new_lows(db, started)
    for n in report.new_lows:
        db.add_alert(set_num=n.set_num, kind="all_time_low",
                     listing_id=n.listing_id, price_inr=n.price,
                     prev_price=n.prev_low,
                     message=f"{n.set_num} new low {inr(n.price)} at "
                             f"{label(n.retailer)} (was {inr(n.prev_low)})")

    fresh_since = (datetime.fromisoformat(started)
                   - timedelta(hours=FRESH_HOURS)).isoformat(timespec="seconds")
    report.snapshot = build_snapshot(db, fresh_since)

    csv_bytes = snapshot_csv(report.snapshot)
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    report.csv_path = SNAPSHOT_DIR / f"cheapest-{datetime.now().strftime('%Y-%m-%d')}.csv"
    report.csv_path.write_bytes(csv_bytes)

    if post:
        from .notify import Discord, load_webhook
        url = load_webhook()
        if not url:
            report.errors.append("Discord webhook not set — run "
                                 "`python -m legotracker discord <webhook url>`")
        else:
            try:
                today = datetime.now().strftime("%a %d %b")
                Discord(url).post_many(discord_messages(report, today, csv_bytes))
            except Exception as exc:  # noqa: BLE001 — a failed post must not lose the run
                report.errors.append(f"Discord: {exc}")

    saved = sum(h.checked for h in report.health.values())
    db.finish_run(run_id, report.status, len(report.snapshot), saved,
                  notes="; ".join(report.errors[:5]))
    return report
