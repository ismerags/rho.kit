"""Command line entry point:  python -m legotracker <command>"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
import webbrowser
from pathlib import Path

from . import collect as collector
from .analytics import compute_stats, score_deal
from .db import Database
from .http_client import PoliteSession
from .matching import normalise_set_num
from .report import render
from .sources import RETAILER_LABELS, SOURCE_CLASSES, build_sources

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "lego.sqlite3"
DEFAULT_DASH = ROOT / "dashboard.html"
CACHE_DIR = ROOT / "data" / "cache"


def _log(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _session(args) -> PoliteSession:
    return PoliteSession(
        min_interval=args.delay,
        jitter=args.delay * 0.6,
        cache_dir=CACHE_DIR if args.cache else None,
        cache_ttl=args.cache_ttl,
    )


# ---------------------------------------------------------------- commands

def cmd_discover(args, db: Database) -> int:
    rep = collector.discover(
        db, _session(args),
        sources=args.sources,
        queries=args.query or None,
        firstcry_pages=args.pages,
    )
    print(f"\nSaw {rep.listings_seen} listings; registered "
          f"{len(rep.sets_touched)} distinct sets.")
    if rep.errors:
        print(f"{len(rep.errors)} retailer error(s):")
        for e in rep.errors[:8]:
            print("  -", e)
    print("\nNext: python -m legotracker collect")
    return 0 if rep.status != "failed" else 1


def cmd_collect(args, db: Database) -> int:
    rep = collector.refresh(db, _session(args), sources=args.sources,
                            only_sets=args.set or None)
    print(f"\nSaved {rep.prices_saved} price points across "
          f"{len(rep.sets_touched)} sets. {rep.alerts} alert(s) raised.")
    if rep.errors:
        print(f"{len(rep.errors)} error(s) — first few:")
        for e in rep.errors[:8]:
            print("  -", e)
    if not args.no_dashboard:
        path = render(db, args.out)
        print(f"Dashboard written to {path}")
    return 0 if rep.status != "failed" else 1


def cmd_hunt(args, db: Database) -> int:
    session = _session(args)
    total = 0
    for raw in args.set_num:
        n = collector.hunt_set(db, session, raw, sources=args.sources)
        print(f"{normalise_set_num(raw)}: {n} listing(s) found")
        total += n
    if total:
        print("\nNow run: python -m legotracker collect")
    return 0


def cmd_search(args, db: Database) -> int:
    from .search import search as run_search

    session = PoliteSession(min_interval=args.delay, jitter=args.delay * 0.4,
                            cache_dir=CACHE_DIR if args.cache else None,
                            cache_ttl=args.cache_ttl)
    outcome = run_search(" ".join(args.query), session=session,
                         sources=args.sources, refine=not args.no_refine,
                         db=None if args.no_log else db)

    print()
    for st in outcome.statuses:
        mark = "!" if st.error else (" " if st.matched else "-")
        detail = st.error[:52] if st.error else f"{st.returned} hits, {st.matched} matched"
        print(f"  {mark} {st.label:<12} {detail:<40} {st.ms:>5}ms")

    if not outcome.sets:
        print(f"\nNo confident match for {outcome.query!r}. "
              f"{outcome.rejected} fuzzy result(s) rejected as the wrong set.")
        return 1

    for res in outcome.sets:
        print(f"\n  {res.set_num}  {res.name[:66]}")
        if res.spread is not None:
            print(f"  {'':6}  {res.spread:.0f}% spread between cheapest and dearest")
        print(f"  {'':6}  {'-' * 60}")
        best = res.best
        for o in res.offers:
            tag = "  <-- cheapest" if best and o.url == best.url else ""
            stock = "" if o.in_stock is not False else "  (out of stock)"
            price = f"₹{o.price_inr:,.0f}" if o.price_inr else "no price"
            label = RETAILER_LABELS.get(o.retailer, o.retailer)
            print(f"  {'':6}  {label:<13} {price:>10}{stock}{tag}")
            print(f"  {'':6}  {'':13} {o.url[:70]}")

    print(f"\n  {outcome.elapsed_ms/1000:.1f}s · {outcome.rejected} "
          f"fuzzy result(s) rejected as the wrong set")
    return 0


def cmd_serve(args, db: Database) -> int:
    from .serve import serve
    db.close()      # the server opens its own connection
    serve(port=args.port, db_path=None if args.no_log else args.db,
          open_browser=not args.no_open, delay=args.delay)
    return 0


def cmd_catalog(args, db: Database) -> int:
    from . import catalog as catalog_mod

    if args.show:
        items = catalog_mod.load(db)
        if not items:
            print("Catalogue is empty. Run: python -m legotracker catalog")
            return 1
        print(f"{len(items)} sets · last refreshed {db.catalog_age()}")
        for it in items[:args.limit]:
            price = f"₹{it['price']:,.0f}" if it["price"] else "—"
            stock = "" if it["in_stock"] else "  (out of stock)"
            print(f"  {it['set_num']:>8}  {price:>10}  "
                  f"{(it['theme'] or ''):<16} {it['name'][:44]}{stock}")
        return 0

    session = _session(args)
    n = catalog_mod.refresh(db, session=session, max_pages=args.pages)
    print(f"\nCatalogue refreshed: {n} sets cached (with thumbnails and prices).")
    print("Browse them at: python -m legotracker serve  ->  Browse catalogue tab")
    return 0 if n else 1


def cmd_priceall(args, db: Database) -> int:
    """Price every catalogue set across every retailer — the slow sweep that
    makes the browse grid show real cross-retailer lows."""
    from . import catalog as catalog_mod
    import time as _t

    items = db.catalog_items()
    if not items:
        print("Catalogue is empty. Run: python -m legotracker catalog")
        return 1

    started = _t.monotonic()

    def progress(n, total, set_num, found, stats):
        elapsed = _t.monotonic() - started
        rate = elapsed / max(1, n)
        left = (total - n) * rate
        print(f"  [{n:>4}/{total}] {set_num:<8} {found} retailer(s)   "
              f"~{left/60:.0f} min left", flush=True)

    print(f"Pricing {len(items)} catalogue sets across every retailer.")
    print("Resumable — sets priced recently are skipped, so you can stop "
          "with Ctrl+C and re-run.\n")
    stats = catalog_mod.price_all(db, session=_session(args),
                                  stale_days=args.stale_days,
                                  limit=args.limit, on_progress=progress)
    mins = (_t.monotonic() - started) / 60
    print(f"\nDone in {mins:.0f} min. "
          f"{stats['priced']} sets priced, {stats['no_match']} found nowhere, "
          f"{stats['skipped']} already fresh, {stats['errors']} errors.")
    return 0


def cmd_dashboard(args, db: Database) -> int:
    path = render(db, args.out)
    print(f"Dashboard written to {path}")
    if args.open:
        webbrowser.open(path.resolve().as_uri())
    return 0


def cmd_watch(args, db: Database) -> int:
    for raw in args.set_num:
        num = normalise_set_num(raw)
        db.upsert_set(num)
        db.set_watched(num, not args.remove)
        if args.target:
            db.set_target(num, args.target)
        state = "removed from" if args.remove else "added to"
        extra = f" (target ₹{args.target:,.0f})" if args.target else ""
        print(f"{num} {state} the watchlist{extra}")
    return 0


def cmd_list(args, db: Database) -> int:
    rows = db.all_sets() if args.all else db.watched_sets()
    if not rows:
        print("Nothing tracked yet. Run: python -m legotracker discover")
        return 0
    targets = db.targets()
    print(f"{'SET':>8}  {'BEST':>10}  {'RETAILER':<12} {'VERDICT':<12} NAME")
    print("-" * 88)
    for row in rows:
        best = None
        for listing in db.listings_for(row["set_num"]):
            stats = compute_stats([dict(h) for h in db.history(listing["id"])])
            if stats.current is None:
                continue
            v = score_deal(stats, target=targets.get(row["set_num"]))
            if best is None or stats.current < best[0]:
                best = (stats.current, listing["retailer"], v.label)
        if best:
            print(f"{row['set_num']:>8}  {best[0]:>10,.0f}  {best[1]:<12} "
                  f"{best[2]:<12} {(row['name'] or '')[:38]}")
        else:
            print(f"{row['set_num']:>8}  {'—':>10}  {'—':<12} {'no price':<12} "
                  f"{(row['name'] or '')[:38]}")
    return 0


def cmd_export(args, db: Database) -> int:
    out = Path(args.out_csv)
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["set_num", "name", "retailer", "observed_at",
                    "price_inr", "mrp_inr", "in_stock", "url"])
        n = 0
        for s in db.all_sets():
            for listing in db.listings_for(s["set_num"]):
                for h in db.history(listing["id"], limit=100000):
                    w.writerow([s["set_num"], s["name"] or "", listing["retailer"],
                                h["observed_at"], h["price_inr"], "",
                                h["in_stock"], listing["url"]])
                    n += 1
    print(f"Wrote {n} rows to {out}")
    return 0


def cmd_publish(args, db: Database) -> int:
    """Export the database to text files, then build the public website.

    This is the one command to run after a collection sweep. Everything it
    touches is inside the repository, so the follow-up is `git add -A &&
    git commit && git push` — and GitHub rebuilds and serves the site.
    """
    from .export import export_all
    from .site import build_site

    data_dir = Path(args.data)
    meta = export_all(db, data_dir)
    print(f"Exported {meta['catalog_items']} catalogue rows and "
          f"{meta['observations']} observations → {data_dir}")

    if meta["observations"] == 0:
        print("\n  No price observations yet. The site will build, but every "
              "set will say 'not checked'.\n  Run `priceall` first for a "
              "useful site.", file=sys.stderr)

    summary = build_site(data_dir, Path(args.content), Path(args.out),
                         base_url=args.base_url)
    print(f"Built {summary['pages']} pages for {summary['sets']} sets "
          f"→ {summary['out_dir']}")
    print(f"  {summary['with_prices']} sets have a comparable price")
    print(f"  {summary['with_history']} have enough history for a chart")
    print(f"  {summary['builds']} build posts")

    if args.open:
        webbrowser.open(f"file://{Path(args.out).resolve()}/index.html")
    else:
        print(f"\nPreview it:  python -m http.server -d {args.out} 8000")
    print("Publish it:  git add -A && git commit -m 'Update prices' && git push")
    return 0


def cmd_diagnose(args, db: Database) -> int:
    """Prove, retailer by retailer, whether scraping still works today."""
    session = _session(args)
    probe = args.probe
    print(f"Probing each retailer with query: {probe!r}\n")
    ok = True

    for adapter in build_sources(session, args.sources):
        name = adapter.label
        try:
            if adapter.name == "firstcry":
                offers = adapter.browse_brand(max_pages=1)
            elif adapter.name == "lego_in":
                offers = adapter.search("ninjago", limit=15)
            else:
                offers = adapter.search(probe, limit=12)
        except Exception as exc:  # noqa: BLE001
            print(f"  {name:<14} FAIL   {exc}")
            ok = False
            continue

        if not offers:
            if adapter.last_error:
                print(f"  {name:<14} UNREACHABLE  {adapter.last_error[:90]}")
                print(f"  {'':<14}              → network/DNS/proxy problem, "
                      f"not a parser problem")
            else:
                print(f"  {name:<14} EMPTY        reached the site but parsed "
                      f"nothing (layout change or bot wall)")
            ok = False
            continue

        priced = [o for o in offers if o.price_inr]
        expects_prices = not getattr(adapter, "catalog_only", False)
        status = "OK" if (priced or not expects_prices) else "NO PRICES"
        if status != "OK":
            ok = False
        print(f"  {name:<14} {status:<6} {len(offers)} listings, "
              f"{len(priced)} priced")
        for o in offers[:3]:
            money = f"₹{o.price_inr:,.0f}" if o.price_inr else "no price"
            print(f"                        {money:>12}  {o.title[:58]}")
        print()

    print("All retailers healthy." if ok else
          "Some retailers need attention — see above.")
    return 0 if ok else 1


# -------------------------------------------------------------------- main

def _source_list(value: str) -> list[str]:
    names = [v.strip() for v in value.split(",") if v.strip()]
    unknown = [n for n in names if n not in SOURCE_CLASSES]
    if unknown:
        raise argparse.ArgumentTypeError(
            f"unknown retailer(s): {', '.join(unknown)}. "
            f"Choose from: {', '.join(SOURCE_CLASSES)}")
    return names


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m legotracker",
        description="Track LEGO prices across Indian retailers.")
    p.add_argument("--db", default=str(DEFAULT_DB), help="SQLite file path")
    p.add_argument("--delay", type=float, default=1.5,
                   help="minimum seconds between hits on the SAME host "
                        "(default 1.5; different hosts run in parallel)")
    p.add_argument("--cache", action="store_true",
                   help="cache raw HTML on disk (useful while debugging parsers)")
    p.add_argument("--cache-ttl", type=int, default=3600,
                   help="cache lifetime in seconds (default 3600)")
    # Comma-separated, not nargs="+": with nargs the subcommand gets swallowed
    # as another value ("--sources amazon_in flipkart diagnose" fails).
    p.add_argument("--sources", type=_source_list, metavar="A,B",
                   help="limit to these retailers, comma-separated: "
                        + ",".join(SOURCE_CLASSES))
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", help="live price search across every retailer")
    s.add_argument("query", nargs="+",
                   help="set number (71806) or name (millennium falcon)")
    s.add_argument("--no-refine", action="store_true",
                   help="skip the targeted retry of retailers that found nothing")
    s.add_argument("--no-log", action="store_true",
                   help="do not record what was seen into the price history")
    s.set_defaults(fn=cmd_search)

    v = sub.add_parser("serve", help="open the local search page in your browser")
    v.add_argument("--port", type=int, default=8765)
    v.add_argument("--no-open", action="store_true")
    v.add_argument("--no-log", action="store_true",
                   help="do not record searches into the price history")
    v.set_defaults(fn=cmd_serve)

    k = sub.add_parser("catalog",
                       help="refresh the browsable catalogue (sets + thumbnails)")
    k.add_argument("--pages", type=int, default=8,
                   help="storefront pages to walk, 250 products each")
    k.add_argument("--show", action="store_true", help="print the cached catalogue")
    k.add_argument("--limit", type=int, default=40, help="rows to print with --show")
    k.set_defaults(fn=cmd_catalog)

    a = sub.add_parser("priceall",
                       help="price every catalogue set at every retailer (~45 min, resumable)")
    a.add_argument("--stale-days", type=int, default=7,
                   help="skip sets priced within this many days (default 7)")
    a.add_argument("--limit", type=int, help="stop after this many sets")
    a.set_defaults(fn=cmd_priceall)

    d = sub.add_parser("discover", help="sweep retailers and register sets")
    d.add_argument("--query", nargs="+", help="override the default search sweep")
    d.add_argument("--pages", type=int, default=8,
                   help="FirstCry brand-listing pages to walk (default 8)")
    d.set_defaults(fn=cmd_discover)

    c = sub.add_parser("collect", help="re-price every tracked listing (run daily)")
    c.add_argument("--set", nargs="+", help="only these set numbers")
    c.add_argument("--out", default=str(DEFAULT_DASH))
    c.add_argument("--no-dashboard", action="store_true")
    c.set_defaults(fn=cmd_collect)

    h = sub.add_parser("hunt", help="search every retailer for specific set numbers")
    h.add_argument("set_num", nargs="+")
    h.set_defaults(fn=cmd_hunt)

    w = sub.add_parser("watch", help="add sets to the watchlist, optionally with a target price")
    w.add_argument("set_num", nargs="+")
    w.add_argument("--target", type=float, help="alert me at or below this rupee price")
    w.add_argument("--remove", action="store_true")
    w.set_defaults(fn=cmd_watch)

    l = sub.add_parser("list", help="show tracked sets and their best prices")
    l.add_argument("--all", action="store_true", help="include unwatched sets")
    l.set_defaults(fn=cmd_list)

    b = sub.add_parser("dashboard", help="rebuild dashboard.html from the database")
    b.add_argument("--out", default=str(DEFAULT_DASH))
    b.add_argument("--open", action="store_true")
    b.set_defaults(fn=cmd_dashboard)

    e = sub.add_parser("export", help="dump the full price history to CSV")
    e.add_argument("out_csv", nargs="?", default="lego_prices.csv")
    e.set_defaults(fn=cmd_export)

    u = sub.add_parser("publish",
                       help="export data and rebuild the public website")
    u.add_argument("--data", default=str(ROOT / "data"),
                   help="where catalog.json / observations.csv are written")
    u.add_argument("--content", default=str(ROOT / "content"),
                   help="folder holding site.md and builds/")
    u.add_argument("--out", default=str(ROOT / "site"),
                   help="where the generated website is written")
    u.add_argument("--base-url", default="",
                   help="public URL of the site, for sitemap.xml")
    u.add_argument("--open", action="store_true", help="open the result locally")
    u.set_defaults(fn=cmd_publish)

    g = sub.add_parser("diagnose", help="check whether each retailer still parses")
    g.add_argument("--probe", default="lego ninjago")
    g.set_defaults(fn=cmd_diagnose)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _log(args.verbose)
    db = Database(args.db)
    try:
        return args.fn(args, db)
    except KeyboardInterrupt:
        print("\nInterrupted — partial results are saved.", file=sys.stderr)
        return 130
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
