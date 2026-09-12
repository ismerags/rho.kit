"""Builds the public static website from the exported data files.

This is the half that runs inside GitHub Actions, so it imports **nothing
outside the standard library** (``analytics`` is stdlib-only too). No requests,
no BeautifulSoup, no database driver. There is no ``pip install`` step in the
deploy workflow and therefore nothing in it that can break on a Tuesday because
an unrelated package published a bad release.

What it emits
-------------
    index.html              search + current build
    browse/index.html       the whole catalogue, filterable in the browser
    set/<number>/index.html one page per set — the canonical page
    builds/index.html       build history
    about/index.html        how it works, where the data comes from
    search-index.json       what the search box searches
    sitemap.xml, robots.txt, 404.html
    assets/site.css, assets/site.js

Every internal link is **relative**. GitHub Pages serves a project site from
``/<repo>/`` rather than ``/``, and absolute ``/browse/`` links are the classic
way to get a site that works perfectly on your laptop and 404s everywhere the
moment it is published. Relative links work at any base path, including
``file://``.

Honesty rules, enforced here rather than left to the templates
--------------------------------------------------------------
* Fewer than ``MIN_OBSERVATIONS_FOR_TYPICAL`` distinct days of history → no
  chart, no verdict, no "all-time low". The page says so.
* "All-time low" is always worded *lowest since we started tracking*. We have
  weeks of history, not years, and the page should never imply otherwise.
* An unchecked retailer and an out-of-stock retailer are different facts and
  are shown differently. Neither is silently omitted.
* No interpolation, no gap filling, no synthetic points.
"""

from __future__ import annotations

import html
import json
import logging
import re
import shutil
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

from .analytics import MIN_OBSERVATIONS_FOR_TYPICAL, compute_stats, score_deal
from .content import Build, SiteConfig, load_builds, load_site_config
from .export import load_catalog, load_meta, load_observations

log = logging.getLogger(__name__)

ASSET_DIR = Path(__file__).parent / "assets"

#: Display metadata for each retailer. Deliberately duplicated from the adapter
#: classes rather than imported: `sources/__init__` pulls in `requests`, and the
#: whole point of this module is to run without it. `tests/test_tracker.py`
#: asserts the two stay in agreement, so the duplication cannot silently rot.
RETAILERS: dict[str, dict] = {
    "mybrickhouse": {"label": "LEGO Certified Store", "domain": "lego.mybrickhouse.com"},
    "toycra":       {"label": "Toycra",               "domain": "toycra.com"},
    "jaimantoys":   {"label": "Jaiman Toys",          "domain": "jaimantoys.com"},
    "hamleys":      {"label": "Hamleys",              "domain": "hamleys.in"},
    "amazon_in":    {"label": "Amazon.in",            "domain": "amazon.in"},
    "flipkart":     {"label": "Flipkart",             "domain": "flipkart.com"},
    "firstcry":     {"label": "FirstCry",             "domain": "firstcry.com"},
    "lego_in":      {"label": "LEGO.com IN",          "domain": "lego.com"},
}

#: Retailers that can actually quote a rupee price. LEGO.com is excluded: it
#: renders prices client-side only and publishes none for India, so listing it
#: as "not checked yet" would imply a price is coming that never will. It stays
#: in RETAILERS because it is still a catalogue source, and the About page
#: explains its absence rather than leaving a silent gap.
PRICE_RETAILERS = {k: v for k, v in RETAILERS.items() if k != "lego_in"}

#: Only links to these hosts are ever written into a page. The database is
#: filled by parsers pointed at live retailer HTML; if one of those ever picked
#: up an attacker-controlled URL, this is what stops it becoming a link on a
#: public website. Cheap, and the failure mode is a missing button, not a
#: phishing hop.
ALLOWED_LINK_HOSTS = tuple(sorted(
    {r["domain"] for r in RETAILERS.values()} |
    {"lego.com", "instagram.com", "github.com"}
))

SET_NUM_RE = re.compile(r"^\d{4,7}$")
SAFE_IMAGE_RE = re.compile(r"^https://[\w.-]+/[^\s\"'<>]*$")

#: A set's catalogue `theme` is a single string, but plenty of real sets
#: straddle two of the browse filters at once — a Technic set that is also an
#: F1 car, an Icons set that is also a Star Wars build. Rather than force one
#: label, the browse page tags a set with every franchise its own name names,
#: in addition to its catalogue theme, so it turns up under both filters.
SECONDARY_THEME_KEYWORDS: dict[str, tuple[str, ...]] = {
    "F1": ("f1", "formula 1", "formula one"),
    "Speed Champions": ("speed champions",),
    "Star Wars": ("star wars",),
    "Marvel": ("marvel", "spider-man", "spiderman", "avengers"),
    "Harry Potter": ("harry potter", "hogwarts"),
    "Disney": ("disney",),
}


def theme_tags(item: dict) -> list[str]:
    """Every browse-filter bucket a set belongs in: its catalogue theme plus
    any franchise its own name mentions."""
    primary = (item.get("theme") or "").strip()
    name = (item.get("display_name") or item.get("name") or "").lower()
    tags = [primary] if primary else []
    for tag, keywords in SECONDARY_THEME_KEYWORDS.items():
        if tag == primary or tag in tags:
            continue
        if any(kw in name for kw in keywords):
            tags.append(tag)
    return tags


def compute_value_thresholds(values: Iterable[float]) -> tuple[float, float]:
    """Tertile cut points for per-piece price, used to rank a set as cheap,
    mid or expensive *relative to the rest of the catalogue* — never against
    a fixed rupee figure, which would go stale the moment prices move."""
    values = sorted(v for v in values if v and v > 0)
    if not values:
        return (0.0, 0.0)
    lo = values[int(len(values) * 0.33)]
    hi = values[min(len(values) - 1, int(len(values) * 0.67))]
    return (lo, hi)


def value_tier(per_piece: Optional[float], thresholds: tuple[float, float]) -> str:
    """'good' (cheap), 'warn' (mid) or 'bad' (expensive) — reusing the site's
    existing semantic colours, which already read as green / orange / red."""
    if not per_piece:
        return ""
    lo, hi = thresholds
    if hi <= lo:
        return ""
    if per_piece <= lo:
        return "good"
    if per_piece >= hi:
        return "bad"
    return "warn"


def pp_span(per_piece: Optional[float], thresholds: tuple[float, float]) -> str:
    """The italic, colour-coded '₹X.XX/piece' used on browse tiles and cards."""
    if not per_piece:
        return ""
    tier = value_tier(per_piece, thresholds)
    cls = f"pp pp-{tier}" if tier else "pp"
    return f'<span class="{cls}">{esc(rupees(per_piece, decimals=True))}/piece</span>'


# ------------------------------------------------------------------ helpers

def esc(value) -> str:
    """Escape for HTML text and double-quoted attributes.

    Every single piece of text that reaches a page goes through this. Set names
    come from retailer listings — text we did not write and do not control —
    and build posts come from a file edited in a browser. Neither is trusted.
    """
    return html.escape("" if value is None else str(value), quote=True)


def rupees(amount: Optional[float], *, decimals: bool = False) -> str:
    """Format money the way India actually writes it: ₹1,29,999, not ₹129,999.

    The last three digits group together, everything above them groups in twos.
    A site for Indian buyers that renders lakhs in the Western style looks
    immediately foreign, and it is a two-line fix.
    """
    if amount is None:
        return "—"
    negative = amount < 0
    amount = abs(float(amount))
    whole = int(amount)
    frac = f"{amount - whole:.2f}"[1:] if decimals else ""

    digits = str(whole)
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        digits = ",".join(parts + [tail])
    return f"{'-' if negative else ''}₹{digits}{frac}"


def parse_ts(value: str) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def freshness(observed_at: Optional[str], now: Optional[datetime] = None) -> dict:
    """Turn a timestamp into words plus a severity, so staleness is never hidden.

    A price with no date next to it is a claim the site cannot support. Past a
    week the label turns amber; past a month it says plainly that it may be
    wrong. Being visibly out of date is far better than being quietly wrong.
    """
    now = now or datetime.now(timezone.utc)
    dt = parse_ts(observed_at) if observed_at else None
    if dt is None:
        return {"text": "never checked", "level": "unknown", "iso": ""}

    delta = now - dt
    mins = delta.total_seconds() / 60
    iso = dt.isoformat()

    def plural(n: int, unit: str) -> str:
        return f"{n} {unit} ago" if n == 1 else f"{n} {unit}s ago"

    if mins < 90:
        text = "just now" if mins < 5 else plural(int(mins), "minute")
        level = "fresh"
    elif delta.days < 1:
        text = plural(int(mins // 60), "hour")
        level = "fresh"
    elif delta.days == 1:
        text, level = "yesterday", "fresh"
    elif delta.days <= 7:
        text, level = plural(delta.days, "day"), "fresh"
    elif delta.days <= 30:
        text, level = plural(delta.days, "day"), "stale"
    else:
        text, level = f"{dt:%-d %b %Y}", "old"
    return {"text": text, "level": level, "iso": iso}


def safe_url(url: Optional[str]) -> str:
    """Return `url` only if it is https and points somewhere we expect."""
    if not url or not isinstance(url, str):
        return ""
    if not url.startswith("https://"):
        return ""
    host = url.split("/", 3)[2].split("@")[-1].split(":")[0].lower()
    if any(host == d or host.endswith("." + d) for d in ALLOWED_LINK_HOSTS):
        return url
    return ""


def safe_image(url: Optional[str]) -> str:
    """Product images are hotlinked from the retailers' CDNs — free, and it keeps
    ~900 photos out of the repository. Only https URLs are accepted."""
    if not url or not isinstance(url, str):
        return ""
    return url if SAFE_IMAGE_RE.match(url) else ""


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")


# --------------------------------------------------------------- analysis

def daily_best(observations: Iterable[dict]) -> list[dict]:
    """Collapse every observation into one best in-stock price per day.

    The question a buyer asks is "what is the cheapest I could have got this
    for", not "what did Flipkart want on Tuesday". Taking the daily minimum
    across retailers answers the first question, and it is also what makes a
    median meaningful — otherwise a retailer that reports twice as often would
    quietly dominate the average.
    """
    by_day: dict[str, float] = {}
    for obs in observations:
        price = obs.get("price_inr")
        if price is None or price <= 0:
            continue
        if obs.get("in_stock") is False:
            continue
        day = (obs.get("observed_at") or "")[:10]
        if len(day) != 10:
            continue
        if day not in by_day or price < by_day[day]:
            by_day[day] = float(price)
    return [{"observed_at": d, "price_inr": p} for d, p in sorted(by_day.items())]


def window_low(series: list[dict], days: int,
               now: Optional[datetime] = None) -> Optional[float]:
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=days)).date().isoformat()
    values = [s["price_inr"] for s in series if s["observed_at"] >= cutoff]
    return min(values) if values else None


def analyse_set(item: dict, observations: list[dict],
                now: Optional[datetime] = None) -> dict:
    """Everything one set page needs, computed once.

    Returns per-retailer rows (including retailers we have never checked, which
    are a different and equally honest answer to "is it available"), the current
    best offer, the daily series, and a verdict that stays silent until there is
    enough history to justify one.
    """
    now = now or datetime.now(timezone.utc)

    latest: dict[str, dict] = {}
    for obs in observations:
        key = obs.get("retailer") or ""
        prev = latest.get(key)
        if prev is None or (obs.get("observed_at") or "") >= (prev.get("observed_at") or ""):
            latest[key] = obs

    # The catalogue feed carries the Certified Store's own price, and that is a
    # real observation — just one collected by the catalogue refresh rather than
    # by a price sweep. Without this a set that has never been swept shows
    # "not checked" here while the browse grid shows a price for it, which looks
    # like a bug and undermines both numbers.
    if "mybrickhouse" not in latest and item.get("price"):
        latest["mybrickhouse"] = {
            "retailer": "mybrickhouse",
            "observed_at": item.get("refreshed_at") or "",
            "price_inr": item.get("price"),
            "mrp_inr": item.get("mrp"),
            "in_stock": bool(item.get("in_stock", True)),
            "url": item.get("url") or "",
        }

    rows = []
    for key, meta in PRICE_RETAILERS.items():
        obs = latest.get(key)
        if obs is None:
            rows.append({"retailer": key, "label": meta["label"], "state": "unchecked",
                         "price": None, "url": "", "fresh": freshness(None, now)})
            continue
        price = obs.get("price_inr")
        in_stock = obs.get("in_stock")
        if price is None or in_stock is False:
            state = "out_of_stock" if in_stock is False else "no_price"
        else:
            state = "in_stock"
        rows.append({
            "retailer": key,
            "label": meta["label"],
            "state": state,
            "price": price if state == "in_stock" else None,
            "last_price": price,
            "mrp": obs.get("mrp_inr"),
            "url": safe_url(obs.get("url")),
            "fresh": freshness(obs.get("observed_at"), now),
        })

    # Cheapest first; anything without a price sinks, keeping its own order.
    priced = sorted([r for r in rows if r["price"] is not None],
                    key=lambda r: r["price"])
    unpriced = [r for r in rows if r["price"] is None]
    rows = priced + unpriced

    best = priced[0] if priced else None
    series = daily_best(observations)
    stats = compute_stats(series)

    pieces = item.get("pieces")
    best_price = best["price"] if best else None
    per_piece = (round(best_price / pieces, 2)
                 if best_price and pieces and pieces > 0 else None)

    enough = len(series) >= MIN_OBSERVATIONS_FOR_TYPICAL
    verdict = None
    if enough and best_price is not None:
        mrp = best.get("mrp") or item.get("mrp")
        verdict = score_deal(stats, mrp=mrp)

    return {
        "rows": rows,
        "best": best,
        "series": series,
        "stats": stats,
        "verdict": verdict,
        "enough_history": enough,
        "per_piece": per_piece,
        "retailers_checked": sum(1 for r in rows if r["state"] != "unchecked"),
        "retailers_in_stock": len(priced),
        "low_7d": window_low(series, 7, now) if enough else None,
        "low_30d": window_low(series, 30, now) if enough else None,
        "all_time_low": min((s["price_inr"] for s in series), default=None) if enough else None,
        "all_time_low_at": (min(series, key=lambda s: s["price_inr"])["observed_at"]
                            if enough and series else None),
        "days_tracked": len(series),
    }


# ---------------------------------------------------------------- chart

def sparkline(series: list[dict], width: int = 660, height: int = 190) -> str:
    """An inline SVG of the daily best price. No charting library, no JS.

    Colours come from `currentColor` and CSS custom properties so the chart
    follows the page into dark mode instead of becoming a dark line on a dark
    ground. Every axis label names a value the line actually reaches.
    """
    points = series[-90:]
    if len(points) < MIN_OBSERVATIONS_FOR_TYPICAL:
        return ""

    pad_l, pad_r, pad_t, pad_b = 62, 14, 16, 30
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b

    values = [p["price_inr"] for p in points]
    lo, hi = min(values), max(values)
    if hi == lo:                       # a perfectly flat price still deserves a line
        hi = lo * 1.02 + 1
    span = hi - lo

    def x(i: int) -> float:
        return pad_l + (plot_w * i / max(1, len(points) - 1))

    def y(v: float) -> float:
        return pad_t + plot_h - (plot_h * (v - lo) / span)

    coords = [(x(i), y(v)) for i, v in enumerate(values)]
    line = " ".join(f"{'M' if i == 0 else 'L'}{px:.1f},{py:.1f}"
                    for i, (px, py) in enumerate(coords))
    area = (line + f" L{coords[-1][0]:.1f},{pad_t + plot_h:.1f}"
                   f" L{coords[0][0]:.1f},{pad_t + plot_h:.1f} Z")

    lo_y, hi_y = y(lo), y(hi)
    first_day, last_day = points[0]["observed_at"], points[-1]["observed_at"]

    def day_label(iso: str) -> str:
        dt = parse_ts(iso)
        return f"{dt:%-d %b}" if dt else iso

    cheapest = min(points, key=lambda p: p["price_inr"])
    ci = points.index(cheapest)

    return f"""<svg class="chart" viewBox="0 0 {width} {height}" role="img"
     preserveAspectRatio="xMidYMid meet"
     aria-label="Cheapest price per day over the last {len(points)} days,
                 between {rupees(lo)} and {rupees(hi)}">
  <line x1="{pad_l}" y1="{hi_y:.1f}" x2="{width - pad_r}" y2="{hi_y:.1f}" class="grid"/>
  <line x1="{pad_l}" y1="{lo_y:.1f}" x2="{width - pad_r}" y2="{lo_y:.1f}" class="grid"/>
  <path d="{area}" class="area"/>
  <path d="{line}" class="line" fill="none"/>
  <circle cx="{x(ci):.1f}" cy="{y(cheapest['price_inr']):.1f}" r="4" class="dot-low"/>
  <circle cx="{coords[-1][0]:.1f}" cy="{coords[-1][1]:.1f}" r="4" class="dot-now"/>
  <text x="{pad_l - 8}" y="{hi_y + 4:.1f}" class="ylab">{esc(rupees(hi))}</text>
  <text x="{pad_l - 8}" y="{lo_y + 4:.1f}" class="ylab">{esc(rupees(lo))}</text>
  <text x="{pad_l}" y="{height - 8}" class="xlab start">{esc(day_label(first_day))}</text>
  <text x="{width - pad_r}" y="{height - 8}" class="xlab end">{esc(day_label(last_day))}</text>
</svg>"""


# ----------------------------------------------------------------- shell

NAV = [("", "Home"), ("browse/", "Browse"), ("builds/", "Builds"), ("about/", "About")]


def page(*, title: str, description: str, rel: str, body: str,
         cfg: SiteConfig, meta: dict, active: str = "",
         head_extra: str = "") -> str:
    """The page shell. `rel` is the path back to the site root ("", "../", …)."""
    nav = "".join(
        '<a href="{}{}"{}>{}</a>'.format(
            rel, href, ' class="on"' if href == active else "", esc(label))
        for href, label in NAV
    )
    ig = safe_url(cfg.instagram_url)
    ig_link = (f'<a href="{esc(ig)}" rel="me noopener" target="_blank">Instagram</a>'
               if ig else "")
    handle = (cfg.instagram_handle or cfg.instagram or "").lstrip("@")
    ig_svg = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
              'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
              '<rect x="2" y="2" width="20" height="20" rx="5"/>'
              '<circle cx="12" cy="12" r="4.2"/>'
              '<circle cx="17.4" cy="6.6" r="1" fill="currentColor" stroke="none"/>'
              '</svg>')
    # Top-left is the site's own mark now — Rho's logo, not the handle.
    # The wordmark is the Greek letter itself (ρ); cfg.title stays "Rho" for
    # page titles and alt text, where the spelled-out name reads better.
    brand = (f'<a class="brand" href="{rel}">'
              f'<img src="{rel}assets/logo.png" alt="{esc(cfg.title)}" width="28" height="28">'
              f'<span>ρ</span></a>')
    # Instagram moves to the top right as its own link.
    social = (f'<a class="social" href="{esc(ig)}" rel="me noopener" target="_blank">'
              f'{ig_svg}<span>@{esc(handle)}</span></a>' if ig and handle else
              f'<a class="social" href="{esc(ig)}" rel="me noopener" target="_blank">'
              f'{ig_svg}</a>' if ig else "")
    repo = safe_url(cfg.repo)
    repo_link = (f'<a href="{esc(repo)}" rel="noopener" target="_blank">Source on GitHub</a>'
                 if repo else "")

    checked = freshness(meta.get("last_observation"))
    obs_count = meta.get("observations") or 0

    return f"""<!doctype html>
<html lang="en-IN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<meta name="description" content="{esc(description)}">
<meta property="og:title" content="{esc(title)}">
<meta property="og:description" content="{esc(description)}">
<meta property="og:type" content="website">
<meta name="twitter:card" content="summary_large_image">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wght@500;600;700&family=IBM+Plex+Mono:wght@400;500;600&family=Instrument+Sans:wght@400;500;600&display=swap">
<link rel="stylesheet" href="{rel}assets/site.css">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='5' fill='%23EA580C'/%3E%3Ccircle cx='11' cy='11' r='3.4' fill='%23fff'/%3E%3Ccircle cx='21' cy='11' r='3.4' fill='%23fff'/%3E%3Crect x='6' y='17' width='20' height='9' rx='2' fill='%23fff'/%3E%3C/svg%3E">
{head_extra}
</head>
<body>
<a class="skip" href="#main">Skip to content</a>
<header class="bar">
  <div class="bar-in">
    {brand}
    <nav aria-label="Main">{nav}</nav>
    {social}
  </div>
</header>
<main id="main">
{body}
</main>
<footer class="foot">
  <div class="foot-in">
    <p class="foot-data">
      <strong>{obs_count:,}</strong> price observations ·
      last collected {esc(checked["text"])} ·
      {esc(meta.get("catalog_items") or 0)} sets in the catalogue
    </p>
    <p>Prices are what each retailer showed when we last checked. They change
       without notice — <strong>always confirm on the retailer's own page before
       you buy</strong>. Nothing here is a purchase offer.</p>
    <p class="foot-legal">LEGO® is a trademark of the LEGO Group, which does not
       sponsor, authorise or endorse this site. This is an independent price
       tracker built by a fan.</p>
    <p class="foot-links">{ig_link}{" · " if ig_link and repo_link else ""}{repo_link}</p>
  </div>
</footer>
<script src="{rel}assets/site.js" defer></script>
</body>
</html>
"""


# ------------------------------------------------------------- components

def price_table(analysis: dict) -> str:
    """The comparison table.

    Only retailers we actually have an observation for get a row. A retailer
    with nothing on record isn't a useful comparison point — it just pads the
    table with "Not checked yet" — so it is left out here; the set page's own
    headline already says plainly when nothing has been checked at all.
    """
    checked_rows = [r for r in analysis["rows"] if r["state"] != "unchecked"]
    if not checked_rows:
        return """<div class="tablewrap">
<p class="empty">No retailer has been checked for this set yet.</p>
</div>"""

    rows = []
    for i, r in enumerate(checked_rows):
        fresh = r["fresh"]
        if r["state"] == "in_stock":
            price = f'<span class="p">{esc(rupees(r["price"]))}</span>'
            mrp = (f'<span class="mrp">{esc(rupees(r["mrp"]))}</span>'
                   if r.get("mrp") and r["price"] and r["mrp"] > r["price"] else "")
            status = f'{price}{mrp}'
            note = f'<span class="when {fresh["level"]}">checked {esc(fresh["text"])}</span>'
            # nofollow, but NOT rel="sponsored": there is no affiliate
            # relationship and marking one that doesn't exist is a lie to
            # search engines and to readers who check.
            link = (f'<a class="buy" href="{esc(r["url"])}" target="_blank"'
                    f' rel="noopener nofollow">Open</a>' if r["url"] else "")
        elif r["state"] == "out_of_stock":
            status = '<span class="na">Out of stock</span>'
            note = f'<span class="when {fresh["level"]}">checked {esc(fresh["text"])}</span>'
            link = (f'<a class="buy ghost" href="{esc(r["url"])}" target="_blank"'
                    f' rel="noopener nofollow">View</a>' if r["url"] else "")
        else:  # "no_price" — reached, but nothing to quote
            status = '<span class="na">No price listed</span>'
            note = f'<span class="when {fresh["level"]}">checked {esc(fresh["text"])}</span>'
            link = ""

        best = ' class="best"' if i == 0 and r["state"] == "in_stock" else ""
        flag = ('<span class="tag-best">Cheapest</span>'
                if i == 0 and r["state"] == "in_stock" else "")
        rows.append(
            f'<tr{best}><th scope="row">{esc(r["label"])}{flag}</th>'
            f'<td class="c-price">{status}</td>'
            f'<td class="c-when">{note}</td>'
            f'<td class="c-go">{link}</td></tr>'
        )

    return f"""<div class="tablewrap">
<table class="prices">
  <caption>Retailers checked, cheapest first</caption>
  <thead><tr><th scope="col">Retailer</th><th scope="col">Price</th>
    <th scope="col">Last checked</th><th scope="col"><span class="sr">Link</span></th></tr></thead>
  <tbody>{''.join(rows)}</tbody>
</table>
</div>"""


def history_block(analysis: dict) -> str:  # pragma: no cover — currently unused
    """Price history — or an honest refusal to show one.

    Not called from `render_set_page` right now: the chart is being held back
    from set pages until there's enough history for it to be worth showing.
    Left in place, working, so it's a one-line change to bring back.
    """
    if not analysis["enough_history"]:
        days = analysis["days_tracked"]
        return f"""<section class="panel thin">
  <h2>Price history</h2>
  <p class="empty"><strong>Not enough data yet.</strong>
    We have {days} day{'s' if days != 1 else ''} of observations for this set, and
    a chart drawn from that would suggest a trend that isn't there.
    It appears once there are {MIN_OBSERVATIONS_FOR_TYPICAL}.</p>
</section>"""

    stats = analysis["stats"]
    atl_when = analysis["all_time_low_at"]
    atl_dt = parse_ts(atl_when) if atl_when else None
    facts = [
        ("Lowest, last 7 days", rupees(analysis["low_7d"])),
        ("Lowest, last 30 days", rupees(analysis["low_30d"])),
        ("Lowest since we started tracking",
         f'{rupees(analysis["all_time_low"])}'
         + (f' <span class="sub">on {atl_dt:%-d %b %Y}</span>' if atl_dt else "")),
        ("Usual price (90-day median)", rupees(stats.typical)),
    ]
    dl = "".join(f'<div><dt>{esc(k)}</dt><dd>{v}</dd></div>' for k, v in facts)

    return f"""<section class="panel">
  <h2>Price history</h2>
  <p class="sub">Cheapest price available on each day we checked, across all
     retailers. {analysis['days_tracked']} days on record.</p>
  {sparkline(analysis["series"])}
  <dl class="facts">{dl}</dl>
  <p class="caveat">"Lowest since we started tracking" means exactly that — it is
     not the lowest price this set has ever been sold for in India.</p>
</section>"""


def verdict_block(analysis: dict) -> str:
    v = analysis["verdict"]
    if v is None:
        return ""
    cls = ("good" if v.score >= 65 else
           "fair" if v.score >= 45 else "poor")
    reasons = "".join(f"<li>{esc(r)}</li>" for r in v.reasons[:4])
    return f"""<div class="verdict {cls}">
  <span class="v-label">{esc(v.label)}</span>
  <ul class="v-why">{reasons}</ul>
</div>"""


def set_json_ld(item: dict, analysis: dict) -> str:
    """Product + AggregateOffer, so Google can show a price range in results.

    Only emitted when there is a real in-stock price to quote; structured data
    that claims an offer we cannot back up is worse than none.
    """
    offers = [r for r in analysis["rows"] if r["state"] == "in_stock"]
    if not offers:
        return ""
    data = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": item.get("display_name") or item.get("name"),
        "sku": item["set_num"],
        "brand": {"@type": "Brand", "name": "LEGO"},
        "offers": {
            "@type": "AggregateOffer",
            "priceCurrency": "INR",
            "lowPrice": min(o["price"] for o in offers),
            "highPrice": max(o["price"] for o in offers),
            "offerCount": len(offers),
        },
    }
    image = safe_image(item.get("image"))
    if image:
        data["image"] = image
    # Set names come from retailer listings, so this JSON can contain anything.
    # Inside <script> the HTML parser is still looking for `</script`, and the
    # `<!--` + `<script` combination puts it into an even stranger state. JSON
    # string escapes are understood by every JSON parser, so escaping the angle
    # brackets outright costs nothing and closes the whole category.
    payload = (json.dumps(data, ensure_ascii=False)
               .replace("<", "\\u003c")
               .replace(">", "\\u003e")
               .replace("&", "\\u0026"))
    return f'<script type="application/ld+json">{payload}</script>'


# ------------------------------------------------------------------ pages

def render_set_page(item: dict, analysis: dict, cfg: SiteConfig,
                    meta: dict) -> str:
    rel = "../../"
    set_num = item["set_num"]
    name = item.get("display_name") or item.get("name") or f"Set {set_num}"
    image = safe_image(item.get("image"))
    pieces = item.get("pieces")
    theme = item.get("theme")

    best = analysis["best"]
    if best:
        headline = f"""<div class="headline">
        <span class="h-label">Cheapest right now</span>
        <span class="h-price">{esc(rupees(best["price"]))}</span>
        <span class="h-where">at {esc(best["label"])} ·
          checked {esc(best["fresh"]["text"])}</span>
      </div>"""
    else:
        # "Nobody has it" and "we haven't looked" are different claims. Saying
        # the first when the second is true is the exact kind of confident
        # wrongness this whole project exists to avoid.
        why = ("No retailer we checked has it in stock"
               if analysis["retailers_checked"]
               else "We haven't priced this set yet")
        headline = f"""<div class="headline none">
        <span class="h-label">Cheapest right now</span>
        <span class="h-price">—</span>
        <span class="h-where">{esc(why)}</span>
      </div>"""

    chips = []
    if pieces:
        chips.append(f'<span class="chip">{pieces:,} pieces</span>')
    if analysis["per_piece"]:
        chips.append(f'<span class="chip">{esc(rupees(analysis["per_piece"], decimals=True))} per piece</span>')
    checked = analysis["retailers_checked"]
    if checked:
        chips.append(f'<span class="chip">in stock at {analysis["retailers_in_stock"]}'
                     f' of {checked} checked</span>')
    else:
        chips.append('<span class="chip">not priced yet</span>')

    desc = (f"Compare LEGO {set_num} {name} prices across "
            f"{len(PRICE_RETAILERS)} Indian retailers. "
            + (f"Cheapest {rupees(best['price'])} at {best['label']}."
               if best else "Currently out of stock everywhere we track."))

    body = f"""
<article class="set">
  <p class="crumbs"><a href="{rel}browse/">Browse</a>
    {f'<span>›</span> {esc(theme)}' if theme else ''}</p>

  <div class="set-top">
    <div class="shot">{
      f'<img src="{esc(image)}" alt="LEGO set {esc(set_num)}, {esc(name)}"'
      f' loading="lazy" referrerpolicy="no-referrer" width="420" height="420">'
      if image else '<div class="noshot" aria-hidden="true">No image</div>'}</div>
    <div class="set-head">
      <p class="eyebrow">{esc(theme or "LEGO")} · Set {esc(set_num)}</p>
      <h1>{esc(name)}</h1>
      <div class="chips">{''.join(chips)}</div>
      {headline}
      {verdict_block(analysis)}
    </div>
  </div>

  {price_table(analysis)}

  <p class="disclaim">Prices are collected automatically and can be wrong or out
     of date. Check the retailer's page before buying.</p>
</article>
"""
    return page(title=f"LEGO {set_num} {name} — price comparison India",
                description=desc, rel=rel, body=body, cfg=cfg, meta=meta,
                head_extra=set_json_ld(item, analysis))


def render_home(items: list[dict], analyses: dict, builds: list[Build],
                cfg: SiteConfig, meta: dict,
                thresholds: tuple[float, float] = (0.0, 0.0)) -> str:
    rel = ""

    # "Best value" strips are built from real observations only. A set we have
    # never compared has no business being called a deal.
    compared = [(i, analyses[i["set_num"]]) for i in items
                if analyses.get(i["set_num"], {}).get("best")]

    deals = sorted(
        [(i, a) for i, a in compared
         if a["best"].get("mrp") and a["best"]["price"] < a["best"]["mrp"]],
        key=lambda pair: (pair[1]["best"]["price"] - pair[1]["best"]["mrp"]) / pair[1]["best"]["mrp"],
    )[:6]
    value = sorted([(i, a) for i, a in compared if a["per_piece"]],
                   key=lambda pair: pair[1]["per_piece"])[:6]

    def card(item, analysis, note) -> str:
        image = safe_image(item.get("image"))
        name = item.get("display_name") or item.get("name")
        pieces = item.get("pieces")
        best = analysis["best"]
        mrp = best.get("mrp") or item.get("mrp")
        mrp_html = (f'<span class="mrp">{esc(rupees(mrp))}</span>'
                    if mrp and best["price"] and mrp > best["price"] else "")
        meta_bits = []
        if pieces:
            meta_bits.append(f'{pieces:,} pieces')
        pp = pp_span(analysis.get("per_piece"), thresholds)
        if pp:
            meta_bits.append(pp)
        meta_line = " · ".join(meta_bits)
        return f"""<a class="card" href="{rel}set/{esc(item['set_num'])}/">
      <span class="well">{f'<img src="{esc(image)}" alt="" loading="lazy" referrerpolicy="no-referrer">' if image else ''}</span>
      <span class="c-name">{esc(name)}</span>
      <span class="c-price">{esc(rupees(best['price']))}{mrp_html}</span>
      {f'<span class="c-meta">{meta_line}</span>' if meta_line else ''}
      <span class="c-note">{note}</span>
    </a>"""

    deal_cards = "".join(
        card(i, a, f'{round((1 - a["best"]["price"] / a["best"]["mrp"]) * 100)}% off '
                   f'at {esc(a["best"]["label"])}')
        for i, a in deals)
    value_cards = "".join(
        card(i, a, f'at {esc(a["best"]["label"])}')
        for i, a in value)

    last_checked = freshness(meta.get("last_observation"))
    discounts_heading = (f"Biggest discounts right now"
                          f' <span class="sub">— last updated {esc(last_checked["text"])}</span>')

    body = f"""
<section class="hero">
  <h1>Compare Lego prices in the market</h1>
  <p class="lede">Type a set number or a name.</p>

  <form class="search" role="search" id="searchform" autocomplete="off">
    <label class="sr" for="q">Search LEGO sets by number or name</label>
    <input id="q" name="q" type="search" placeholder="42176, or “porsche”"
           inputmode="search" enterkeyhint="search" spellcheck="false">
    <button type="submit">Search</button>
  </form>
  <div id="results" class="results" aria-live="polite"></div>
  <p class="hint" id="hint">{meta.get('catalog_items') or 0} sets tracked ·
     {meta.get('observations') or 0:,} price observations on record</p>
</section>

{f'<section class="strip"><h2>{discounts_heading}</h2><div class="cards">{deal_cards}</div></section>' if deal_cards else ''}
{f'<section class="strip"><h2>Best value per piece</h2><div class="cards">{value_cards}</div></section>' if value_cards else ''}
"""
    return page(title=f"{cfg.title} — compare LEGO prices across Indian retailers",
                description=cfg.tagline or cfg.description, rel=rel, body=body,
                cfg=cfg, meta=meta, active="")


def render_browse(items: list[dict], analyses: dict, cfg: SiteConfig,
                  meta: dict, thresholds: tuple[float, float] = (0.0, 0.0)) -> str:
    rel = "../"
    item_tags = {item["set_num"]: theme_tags(item) for item in items}
    themes = sorted({t for tags in item_tags.values() for t in tags})
    opts = "".join(f'<option value="{esc(t)}">{esc(t)}</option>' for t in themes)

    tiles = []
    for item in items:
        a = analyses.get(item["set_num"], {})
        best = a.get("best")
        image = safe_image(item.get("image"))
        name = item.get("display_name") or item.get("name")
        price = best["price"] if best else item.get("price")
        pieces = item.get("pieces") or 0
        per_piece = a.get("per_piece") or (
            round(price / pieces, 2) if price and pieces else None)
        out = a.get("retailers_checked") and not a.get("retailers_in_stock")
        tags = item_tags[item["set_num"]]

        note = (f'cheapest of {a.get("retailers_checked")} checked'
                if best else "store price only")
        pp = pp_span(per_piece, thresholds)
        tiles.append(f"""<a class="tile{' out' if out else ''}"
   href="{rel}set/{esc(item['set_num'])}/"
   data-name="{esc((name or '').lower())}" data-num="{esc(item['set_num'])}"
   data-theme="{esc('|'.join(tags))}"
   data-price="{price or ''}" data-pieces="{pieces or ''}"
   data-per="{per_piece or ''}" data-out="{1 if out else 0}">
  <span class="well">{f'<img src="{esc(image)}" alt="" loading="lazy" referrerpolicy="no-referrer">' if image else ''}</span>
  <span class="t-name">{esc(name)}</span>
  <span class="t-num">#{esc(item['set_num'])}</span>
  <span class="t-price">{esc(rupees(price))}{' <span class="t-out">out of stock</span>' if out else ''}</span>
  <span class="t-note">{esc(note)}{f' · {pp}' if pp else ''}</span>
</a>""")

    catalog_refreshed = freshness(meta.get("catalog_refreshed_at"))

    body = f"""
<section class="browse">
  <h1>Browse the catalogue</h1>
  <p class="lede">Click any set for details.</p>

  <div class="filters">
    <label class="sr" for="bq">Filter by name or number</label>
    <input id="bq" type="search" placeholder="Filter…" spellcheck="false">
    <label class="sr" for="btheme">Theme</label>
    <select id="btheme"><option value="">All themes</option>{opts}</select>
    <label class="sr" for="bsort">Sort by</label>
    <select id="bsort">
      <option value="name">Name A–Z</option>
      <option value="price">Price, low to high</option>
      <option value="price-desc">Price, high to low</option>
      <option value="per">Value per piece</option>
      <option value="pieces-desc">Most pieces</option>
    </select>
    <label class="check"><input type="checkbox" id="bstock"> In stock only</label>
  </div>
  <p class="freshmark">Catalogue last refreshed {esc(catalog_refreshed["text"])}</p>
  <p class="count" id="bcount" aria-live="polite"></p>
  <div class="tiles" id="tiles">{''.join(tiles)}</div>
  <p class="empty" id="bempty" hidden>Nothing matches that filter.</p>
</section>
"""
    return page(title=f"Browse {len(items)} LEGO sets — {cfg.title}",
                description=f"Every LEGO set we track in India, {len(items)} in "
                            "total, sorted by price, piece count or value.",
                rel=rel, body=body, cfg=cfg, meta=meta, active="browse/")


def render_builds(builds: list[Build], catalog_by_num: dict,
                  cfg: SiteConfig, meta: dict) -> str:
    rel = "../"
    if not builds:
        cards = ('<p class="empty">No builds posted yet. Add a file to '
                 '<code>content/builds/</code> to start the log.</p>')
    else:
        out = []
        for b in builds:
            photos = "".join(
                f'<img src="{esc(safe_image(p) or rel + "media/" + p)}" alt=""'
                f' loading="lazy" referrerpolicy="no-referrer">'
                for p in b.photos[:12])
            set_link = ""
            if b.set_num and b.set_num in catalog_by_num:
                item = catalog_by_num[b.set_num]
                set_link = (f'<a class="setlink" href="{rel}set/{esc(b.set_num)}/">'
                            f'Prices for #{esc(b.set_num)} '
                            f'{esc(item.get("display_name") or "")}</a>')
            elif b.set_num:
                set_link = f'<span class="setlink quiet">Set #{esc(b.set_num)}</span>'

            ig = safe_url(b.instagram)
            # A bare bar with no number is a rough guess dressed up as precision.
            bar = (f'<div class="bar-track"><div class="bar-fill" '
                   f'style="width:{b.progress}%"></div></div>'
                   f'<p class="sub">{b.progress}% done</p>'
                   if b.progress is not None else "")
            body_html = "".join(f"<p>{esc(p)}</p>"
                                for p in b.body.split("\n\n") if p.strip())
            out.append(f"""<article class="build {esc(b.status)}">
  <header>
    <span class="state">{esc(b.status_label)}</span>
    <h2>{esc(b.title)}</h2>
    <p class="dates">{esc(b.completed or b.started)}</p>
  </header>
  {bar}
  {f'<div class="shots">{photos}</div>' if photos else ''}
  <div class="prose">{body_html}</div>
  <p class="build-links">{set_link}
    {f'<a href="{esc(ig)}" target="_blank" rel="noopener">On Instagram</a>' if ig else ''}</p>
</article>""")
        cards = "".join(out)

    body = f"""
<section class="buildlog">
  <h1>Build log</h1>
  <p class="lede">What I'm putting together, what's finished, and what's next.</p>
  {cards}
</section>
"""
    return page(title=f"Build log — {cfg.title}",
                description="LEGO builds in progress and completed, with photos.",
                rel=rel, body=body, cfg=cfg, meta=meta, active="builds/")


def render_about(cfg: SiteConfig, meta: dict) -> str:
    rel = "../"
    ig = safe_url(cfg.instagram_url)

    body = f"""
<section class="prose-page">
  <h1>About this tracker</h1>
  <p class="lede">A free, independent price comparison for LEGO sets sold in
     India. No account, no ads, no tracking, no affiliate links.</p>

  <h2>Why I built this</h2>
  <p>I built this because I like two things: solving problems and building
     LEGO.</p>
  <p>I'm an engineer, so I'm wired to look at a problem and think, there has
     to be a better way to do this.</p>
  <p>When I wanted to buy LEGO sets, I found myself checking different
     websites, comparing prices, waiting for discounts, and wondering if I
     was actually getting a good deal. So I decided to build a simple
     solution: a place where you can quickly find the best available price
     for a LEGO set.</p>
  <p>LEGO itself is a pretty good representation of how I like to work.
     Brick by brick. Build something, see if it works, break it, rebuild it,
     and come back stronger.</p>
  <p>I made this tracker for myself, but also for anyone who loves LEGO and
     finds the prices a little hard to justify. If there's a set you've
     always wanted, hopefully this helps you find it at a price that makes a
     little more sense.</p>
  <p>This is still a work in progress, and I'm building it the same way I
     build LEGO — one brick at a time.</p>
  <p>I also share my LEGO builds and build videos on Instagram at
     {f'<a href="{esc(ig)}" target="_blank" rel="noopener">@{esc(cfg.instagram_handle or "baijalbuilds")}</a>' if ig else '@baijalbuilds'}.</p>
  <p>If you spot something that's wrong, have an idea that would make the
     tracker better, or simply want to say hi, I'd genuinely love to hear
     from you.</p>

  <h2>Not affiliated with LEGO</h2>
  <p>LEGO® is a trademark of the LEGO Group. The LEGO Group does not sponsor,
     authorise or endorse this site. Outbound links to retailers carry no
     affiliate tags.</p>
  <p><a href="{rel}">← Back to search</a></p>
</section>
"""
    return page(title=f"About — {cfg.title}",
                description="Why this free LEGO price tracker for India exists, "
                            "and who built it.",
                rel=rel, body=body, cfg=cfg, meta=meta, active="about/")


# ------------------------------------------------------------------ build

def search_index(items: list[dict], analyses: dict) -> list[dict]:
    """Small enough to ship to every visitor: ~900 rows, roughly 120 KB.

    Keys are one character long because this file is downloaded by every person
    who opens the site, and at this row count the difference between short and
    descriptive keys is about 40 KB of someone's mobile data.
    """
    out = []
    for item in items:
        a = analyses.get(item["set_num"], {})
        best = a.get("best")
        out.append({
            "n": item["set_num"],
            "t": item.get("display_name") or item.get("name") or "",
            "h": item.get("theme") or "",
            "p": (best["price"] if best else item.get("price")) or None,
            "r": best["label"] if best else "",
        })
    return out


def build_site(data_dir: Path, content_dir: Path, out_dir: Path,
               base_url: str = "") -> dict:
    """Generate the whole site. Returns a small summary for the CLI to print."""
    data_dir, content_dir, out_dir = Path(data_dir), Path(content_dir), Path(out_dir)

    items = load_catalog(data_dir)
    observations = load_observations(data_dir)
    meta = load_meta(data_dir)
    cfg = load_site_config(content_dir)
    builds = load_builds(content_dir)

    if not items:
        raise SystemExit(
            f"No catalogue found in {data_dir}/catalog.json.\n"
            "Run `python -m legotracker export` first (it reads your database)."
        )

    items = [i for i in items if SET_NUM_RE.match(i.get("set_num") or "")]
    items.sort(key=lambda i: (i.get("display_name") or i.get("name") or "").lower())

    now = datetime.now(timezone.utc)
    analyses = {i["set_num"]: analyse_set(i, observations.get(i["set_num"], []), now)
                for i in items}
    catalog_by_num = {i["set_num"]: i for i in items}
    thresholds = compute_value_thresholds(
        a["per_piece"] for a in analyses.values() if a.get("per_piece"))

    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    def write(rel_path: str, text: str) -> None:
        target = out_dir / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    write("index.html", render_home(items, analyses, builds, cfg, meta, thresholds))
    write("browse/index.html", render_browse(items, analyses, cfg, meta, thresholds))
    write("builds/index.html", render_builds(builds, catalog_by_num, cfg, meta))
    write("about/index.html", render_about(cfg, meta))

    for item in items:
        write(f"set/{item['set_num']}/index.html",
              render_set_page(item, analyses[item["set_num"]], cfg, meta))

    write("search-index.json",
          json.dumps(search_index(items, analyses), ensure_ascii=False,
                     separators=(",", ":")))

    # A 404 that offers the search box is more use than a blank one.
    write("404.html", page(
        title=f"Not found — {cfg.title}",
        description="That page does not exist.", rel="", cfg=cfg, meta=meta,
        body='<section class="hero"><h1>That page doesn\'t exist</h1>'
             '<p class="lede">The set may not be in the catalogue, or the link '
             'may be old.</p><p><a href="browse/">Browse everything →</a></p>'
             '</section>'))

    write("robots.txt",
          "User-agent: *\nAllow: /\n"
          + (f"Sitemap: {base_url.rstrip('/')}/sitemap.xml\n" if base_url else ""))

    if base_url:
        base = base_url.rstrip("/")
        urls = [""] + ["browse/", "builds/", "about/"] + [
            f"set/{i['set_num']}/" for i in items]
        entries = "".join(
            f"<url><loc>{esc(base)}/{esc(u)}</loc>"
            f"<lastmod>{now:%Y-%m-%d}</lastmod></url>" for u in urls)
        write("sitemap.xml",
              '<?xml version="1.0" encoding="UTF-8"?>'
              '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
              f"{entries}</urlset>")

    # Jekyll would otherwise try to process the output and drop anything in a
    # folder beginning with an underscore. We are publishing plain HTML.
    write(".nojekyll", "")

    assets_out = out_dir / "assets"
    assets_out.mkdir(parents=True, exist_ok=True)
    for asset in ("site.css", "site.js", "logo.png"):
        shutil.copyfile(ASSET_DIR / asset, assets_out / asset)

    media_src = content_dir / "media"
    if media_src.is_dir():
        shutil.copytree(media_src, out_dir / "media", dirs_exist_ok=True)

    summary = {
        "sets": len(items),
        "pages": len(items) + 5,
        "builds": len(builds),
        "with_prices": sum(1 for a in analyses.values() if a["best"]),
        "with_history": sum(1 for a in analyses.values() if a["enough_history"]),
        "out_dir": str(out_dir),
    }
    log.info("built %(pages)d pages for %(sets)d sets in %(out_dir)s", summary)
    return summary


def main(argv: Optional[list] = None) -> int:
    """`python -m legotracker.site` — deliberately its own entry point.

    Going through `legotracker.cli` would import the retailer adapters and
    therefore `requests`. This module needs none of that, which is what lets the
    GitHub Actions workflow build the site with no `pip install` step at all.
    """
    import argparse

    root = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(
        prog="python -m legotracker.site",
        description="Build the static website from the exported data files.")
    ap.add_argument("--data", default=str(root / "data"))
    ap.add_argument("--content", default=str(root / "content"))
    ap.add_argument("--out", default=str(root / "site"))
    ap.add_argument("--base-url", default="",
                    help="Public URL, used for sitemap.xml (optional)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)-7s %(message)s")
    summary = build_site(Path(args.data), Path(args.content), Path(args.out),
                         base_url=args.base_url)
    print(f"Built {summary['pages']} pages for {summary['sets']} sets "
          f"→ {summary['out_dir']}")
    print(f"  {summary['with_prices']} sets have a comparable price")
    print(f"  {summary['with_history']} have enough history for a chart")
    return 0


if __name__ == "__main__":                                   # pragma: no cover
    raise SystemExit(main())
