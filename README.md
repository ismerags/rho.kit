# Rho — LEGO price search & tracker for India

A free, independent price comparison for LEGO sets sold in India, published as
a **static website**. No server, no database in production, no login, no ads.

```
YOUR MAC                          GITHUB                      VISITORS
python -m legotracker publish  →  Actions builds the site  →  a fast, free page
 (weekly, ~45 min)                 (~2 min, free)              (no retailer is
 the 8 retailer adapters           ~900 set pages               ever contacted)
```

The website only ever *reads* data that was collected earlier. That is what
makes it instant, free to host, impossible to get blocked, and polite to the
retailers — a thousand visitors cause exactly as much retailer traffic as zero.

**Deploying it: see [DEPLOY.md](DEPLOY.md).**

---

Three tools, one engine.

**Browse** — a catalogue of ~830 LEGO sets sold in India, with thumbnails.
Click one to compare all eight retailers live.

**Search** — type a set number, get live prices from eight retailers in ~3 seconds.
No database, no waiting.

**Track** — optionally keep the history and get a verdict on whether today's
price is actually good.

Runs entirely on your Mac. No API keys, no accounts, no hosting.

```bash
python -m legotracker serve          # opens the search page in your browser
```

---

## The eight retailers

All verified against the live sites, September 2026. Each costs **one HTTP
request** per search, and all eight run in parallel.

| Retailer | How | Notes |
|---|---|---|
| **LEGO Certified Store** | Shopify `/search/suggest.json` | `lego.mybrickhouse.com` — also the catalogue source |
| **Toycra** | Shopify `/search/suggest.json` | Public JSON API |
| **Jaiman Toys** | Shopify `/search/suggest.json` | Public JSON API |
| **Hamleys** | Fynd REST catalog API | Public storefront token, lifted at runtime |
| **Amazon.in** | HTML search page | Server-rendered prices |
| **Flipkart** | `window.__INITIAL_STATE__` JSON | Embedded in the page |
| **FirstCry** | HTML `/search/?q=` | Server-rendered |
| **BuyHatke** | `buyhatke.com` search + paste-link lookup | Aggregator — see below |
| *LEGO.com/en-in* | *catalog only* | *Publishes no prices in HTML — see below* |

Four of these are proper JSON APIs, not scraping. That is a large part of why
search is fast and unlikely to break.

**There is no lego.com vendor, and that is not a limitation of the scraper.**
lego.com/en-in publishes no prices at all — `meta[product:price:amount]` is
literally `"0"` and no rupee figure appears anywhere, even in a real browser with
JavaScript fully run. LEGO does not sell direct into India; it sells through
Certified Stores. `lego.mybrickhouse.com` (shop id `legoin.myshopify.com`) is
that store, so it is the closest thing to LEGO's own Indian retail price.

**Two traps worth knowing about, both found by testing:**

- **Shopify quotes prices two different ways.** `suggest.json` returns rupees
  (`"18499.00"`); the product `.js` endpoint returns paise (`1849900`). Mixing
  them is a silent 100x error. Both are handled, and a test pins it.
- **FirstCry's obvious search URL is a trap.** `/searchresult?searchstring=`
  renders client-side and returns zero products — it looks like "no results"
  rather than like a bug. `/search/?q=` is server-rendered and works.

**Hamleys' token is public but rotates.** It is the storefront credential their
own JavaScript sends from every visitor's browser, and it is also server-rendered
into their homepage as `"authorizationHeader"`. Rather than hardcode it, the
adapter lifts it from the homepage and caches it — so a rotation fixes itself. A
401 clears the cache and refetches.

**LEGO.com** renders prices client-side only: `__NEXT_DATA__` carries
`"price":0`, the price element is empty until a GraphQL call fills it, and that
endpoint rejects replayed requests as CSRF. It is used only as a catalog source
(its theme pages *do* server-render official set numbers and names). Adding its
prices needs a headless browser — see the end of this file.

**BuyHatke is different from the other seven: it does not talk to a retailer,
it talks to a price-tracking aggregator that already does.** `buyhatke.com`
watches Amazon, Flipkart, Hamleys, Ajio and others itself, for free, with no
login or API key. Two of its routes are used here:

- `/search?product=lego` — one request surfaces dozens of BuyHatke-tracked LEGO
  sets across every retailer it watches.
- pasting any Amazon/Flipkart product URL after `buyhatke.com/` resolves that
  exact product and returns its current price, a small cross-retailer
  comparison, **and its full historical price series** — often a year or more,
  which is far more history than this project could collect on its own.

Both routes server-render their data into the HTML, so no headless browser is
needed. Two things happen with what comes back that the other adapters don't
do: `_absorb_deals` folds BuyHatke's own "Amazon ₹13,499 / Hamleys ₹12,999"
comparison into *this project's own* `amazon_in`/`flipkart`/`hamleys` price
history — so a BuyHatke lookup indirectly prices those retailers without ever
requesting amazon.in or flipkart.com directly — and `_backfill_history` imports
BuyHatke's multi-year price series the first time each listing is seen, which
is where a set's history can suddenly start well before this project began
tracking it. See `legotracker/sources/buyhatke.py` for the parsers (a
brace-matching extractor for the paste-link route's near-JSON blob, a small
regex for the search route's compiled `x.field=value;` output) and
`legotracker/collect.py`'s `refresh()` for where both get wired in.

---

## Install

```bash
cd lego-price-tracker
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Python 3.9+. Three dependencies: `requests`, `beautifulsoup4`, `lxml`.
The search server uses the standard library only.

## Browse the catalogue

```bash
python -m legotracker catalog     # fetch ~830 sets with thumbnails (~10s)
python -m legotracker serve       # then open the "Browse catalogue" tab
```

The catalogue comes from the LEGO Certified Store's `/products.json` — four
requests for the whole storefront, including **set numbers, prices, stock and
image URLs**. The set number is taken from `variants[0].sku`, which on this
store is the real LEGO set number, so it never has to be guessed from a title.

### What a tile shows

The **best price known from any retailer** — not just the Certified Store —
along with who is offering it, the MRP, the discount, and ₹ per piece computed
off that best price. Plus how many retailers that figure was drawn from and how
fresh it is ("cheapest of 4 checked · today").

That data comes from observations already in your database. Nothing is fetched
while browsing, so the grid is instant. A set nobody has compared yet shows the
Certified Store's price and says so.

Sort by name, price, piece count, or ₹ per piece **in either direction**. Sets
that are out of stock at *every* retailer checked sink to the bottom whatever the
sort — but a set nobody has priced yet is treated as **unknown, not
unavailable**, so it is never demoted for lack of data.

Click a set and all eight retailers are checked live, cheapest first. Every
retailer gets a row, including the ones with nothing: "doesn't stock it" and
"unavailable" are real answers and are shown as such.

### Filling in the prices

```bash
python -m legotracker priceall        # ~45 min, resumable, run it overnight
```

Without this, tiles mostly show the Certified Store's price, because that is all
the catalogue feed contains. `priceall` walks every catalogue set and prices it
at every retailer, which is what makes the grid genuinely comparative.

It skips sets priced within the last 7 days (`--stale-days`), so Ctrl+C and
re-run picks up where it stopped. `--limit 50` tries a small batch first.

`catalog --show` prints the cached catalogue; the **Refresh catalogue** button
re-fetches it. Refreshing also records the Certified Store's price into the
history, so browsing feeds the tracker.

## Search

```bash
python -m legotracker serve                 # web UI at 127.0.0.1:8765
python -m legotracker search 71806          # same thing in the terminal
python -m legotracker search millennium falcon
```

Search by **set number** (`71806`) for an exact answer, or by **name**
(`millennium falcon`) to see every matching set grouped separately.

Every search quietly records what it saw into the price history, so tracking
builds up as a by-product. `--no-log` turns that off.

### Why results get thrown away

Every retailer's search is fuzzy. Asked for `71806`, they really returned:

| Retailer | Top hit |
|---|---|
| Toycra | 71814 Tournament Temple City |
| Hamleys | 10355 Blacktron Renegade |
| FirstCry | 71826 Dragon Spinjitzu Battle Pack |

Not one returned 71806 first. Showing each site's top hit would produce a tidy
comparison table of six completely different products — confident, well laid out
and wrong.

So nothing is shown unless it is verified: the set number must appear in the
title (after piece counts and ages are stripped, so `Cole's Mech 71806 (235
Pieces)` resolves to `71806`, not `235`), the LEGO brand must be present, and
accessory markers (`display stand`, `light kit`, `compatible with`, `BRIKSMAX`…)
are an instant reject. The page tells you how many results it rejected.

A retailer showing "no match" means *it did not offer a verified listing* — not
that it was unreachable. Unreachable shows as "unavailable" in red.

### The refine pass

A retailer's search being fuzzy is not evidence it lacks the set. So after the
first fan-out, any retailer that came back empty gets re-queried by exact set
number. That is why a search sometimes takes 4 seconds instead of 2 — and why it
finds stock the retailer's own search box would have hidden from you.
`--no-refine` skips it.

---

## Track

The tracker answers a different question: *is this price actually good?*

```bash
python -m legotracker diagnose      # confirm each retailer still parses
python -m legotracker discover      # build the catalog (~10 min)
python -m legotracker collect       # daily: re-price everything, rebuild dashboard
open dashboard.html
```

```bash
python -m legotracker watch 75375 --target 7500   # alert at or below ₹7,500
python -m legotracker list
python -m legotracker export prices.csv
```

**Day one shows no verdicts.** Scoring is against a set's own price history, so
it needs about four observations before saying anything stronger than *Watching*.
That is the design working.

### Run it on a schedule

```bash
./scripts/install-schedule.sh      # macOS: every Sunday at 03:00
```

That runs `scripts/weekly.sh`, which collects prices, rebuilds the data files
and pushes — so the public site updates itself. `--remove` undoes it.

The paths are filled in from wherever the project actually lives, so nothing
personal ends up in the repository. On Linux, use cron instead:

```cron
0 3 * * 0 /path/to/lego-price-tracker/scripts/weekly.sh
```

macOS note: launchd cannot wake a sleeping Mac. If yours is asleep on Sunday
night the job runs at the next wake instead, and the site keeps working — it
just starts labelling prices "last checked 12 days ago" in amber. Being
visibly out of date beats being quietly wrong.

### How the score works

**Situation.** Indian marketplaces quote an "MRP" that is often fictional. A set
permanently listed at "60% off ₹5,000" was never a ₹5,000 set.

**Task.** Separate a real discount from a manufactured one.

**Action.** Anchor on the set's *own trading history*, not the sticker:

| Signal | Weight | Why |
|---|---|---|
| Price vs its own 90-day median | ±30 | The honest anchor |
| Position vs everything seen *before today* | ±7 (+5 at a new floor) | Near its real floor? |
| Discount vs MRP | +6 max | Weakest signal, deliberately capped |

**Result.** 0–100 from a neutral 50:

| Score | Label | Roughly means |
|---|---|---|
| 80+ | Strong buy | ~14%+ below its own median, at a new floor |
| 65–79 | Good price | ~5–13% below its median |
| 45–64 | Fair | Around its usual price |
| 35–44 | Wait | Above its usual price |
| <35 | Overpriced | Well above |
| — | Watching | Fewer than 4 observations; no verdict yet |

A `--target` overrides all of it.

**Analogy.** MRP is the asking price on a house listing. The 90-day median is
what comparable houses on that street actually sold for. You negotiate against
the second number.

**The rule to remember:** a discount is only real relative to what the thing
normally trades at.

---

## When it breaks

It will. Retailers change their markup.

```bash
python -m legotracker diagnose
```

names the retailer and distinguishes **UNREACHABLE** (network problem) from
**EMPTY** (reached it, parsed nothing — a layout change or a bot wall). Different
problems, different fixes.

Fixes live in one file per retailer:

| File | What to look at |
|---|---|
| `sources/amazon_in.py` | CSS selectors |
| `sources/flipkart.py` | the `__INITIAL_STATE__` walker |
| `sources/firstcry.py` | the `Sale price RS` aria-labels |
| `sources/hamleys.py` | the token regex and API path |
| `sources/shopify.py` | serves Toycra, Jaiman Toys and the Certified Store |
| `catalog.py` | set-number / theme / piece-count extraction |

`--cache` saves raw responses to `data/cache/` so you can iterate on a parser
without re-fetching:

```bash
python -m legotracker --cache --cache-ttl 86400 diagnose
```

## Tests

```bash
python tests/test_tracker.py
```

269 checks, no network needed. Fixtures are genuine markup and JSON captured
from the live sites.

## Commands

| Command | What it does |
|---|---|
| `publish` | **Rebuild the public website's data and pages** |
| `serve` | Search + Browse locally, with live prices |
| `catalog` | Refresh the browsable catalogue (sets, prices, thumbnails) |
| `priceall` | Price every catalogue set at every retailer (~45 min, resumable) |
| `search 71806` | Same search, in the terminal |
| `discover` | Sweep retailers, register sets |
| `collect` | Re-price everything, rebuild dashboard (**run daily**) |
| `hunt 75375 …` | Find specific sets at every retailer |
| `watch 75375 --target 7500` | Watchlist and price targets |
| `list` | Tracked sets and best prices |
| `dashboard --open` | Rebuild `dashboard.html` |
| `export prices.csv` | Full price history as CSV |
| `diagnose` | Per-retailer health check |

The website build is a separate entry point with no third-party imports, which
is what lets GitHub Actions run it with no `pip install` step:

```bash
python -m legotracker.site --out _site     # what CI runs
```

Global flags: `--delay` (seconds between hits on the **same** host, default 1.5;
different hosts always run in parallel), `--sources amazon_in,toycra`
(comma-separated), `--cache`, `-v`.

## Data

Plain SQLite at `data/lego.sqlite3`:

```sql
SELECT s.set_num, s.name, l.retailer, p.observed_at, p.price_inr
FROM price_points p
JOIN listings l ON l.id = p.listing_id
JOIN sets s ON s.set_num = l.set_num
WHERE s.set_num = '75375'
ORDER BY p.observed_at;
```

The price table is append-only: every observation writes a row even when the
price is unchanged, because "still ₹2,175 on day 40" is what makes a median
meaningful.

## Adding LEGO.com prices later

```bash
pip install playwright && playwright install chromium
```

In `sources/lego_in.py`, replace `fetch_offer` with a Playwright page load that
waits for `[data-test="product-price-display-price"]` to have text. Expect ~3
seconds per set instead of ~0.3, so use it on the watchlist only.

## Being a good citizen

- Per-host rate limiting with jitter and backoff; different hosts in parallel.
- Public endpoints only — the same ones each site's own front end calls. No
  login, no cart, no checkout, nothing behind auth.
- Personal price research. Don't point this at a retailer at scale or resell the
  output — that is a different activity with different rules.
- Bot checks are detected and reported, not hammered through.
