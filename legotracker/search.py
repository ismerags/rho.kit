"""Live multi-retailer search: one query in, a price table out.

The tracker answers "is this a good price?" from history. This answers the much
simpler question "what does this cost right now, everywhere?" — and it needs no
database and no waiting, because every retailer is queried at the moment you ask.

Shape of a search:

    1. Fan out to all six retailers in parallel (one HTTP request each).
    2. Throw away everything that is not confidently the set you asked for.
    3. Optionally re-query, by exact set number, the retailers that came back
       empty — their own search engines are fuzzy enough to miss stock they have.
    4. Group by set, sort by price.

Step 2 is the one that matters. Every retailer's search is fuzzy: asking Toycra,
Hamleys and FirstCry for "71806" returns 71814, 10355 and 71826 respectively.
Showing the top hit from each site would produce a tidy comparison table of six
completely different products.
"""

from __future__ import annotations

import logging
import re
import time
import queue
import threading
from dataclasses import dataclass, field, replace
from typing import Optional

from .db import Database
from .http_client import PoliteSession
from .matching import candidate_set_numbers, normalise_set_num, score_listing
from .sources import (RETAILER_LABELS, SEARCH_SOURCES, Offer, build_sources)
from .sources import buyhatke as buyhatke_source

log = logging.getLogger(__name__)

SET_NUMBER_QUERY_RE = re.compile(r"^\s*(\d{4,7})\s*$")

#: Hard wall-clock budget for a whole search. Whatever has arrived by then is
#: returned; stragglers are reported as timed out.
#:
#: This matters more than it looks. Without a deadline the fan-out waits for the
#: slowest retailer, and one retailer hitting a bot wall can retry three times
#: with escalating backoff — roughly two minutes of a spinning page that says
#: nothing. Five usable prices now beats six in two minutes.
DEFAULT_DEADLINE = 20.0
REFINE_RESERVE = 0.45           # fraction of the budget kept for the retry pass

#: Reuse a recent identical BuyHatke answer rather than re-asking: a LEGO
#: price does not change minute to minute, and every repeat click on the same
#: set was another request that gained nothing. Keyed by source name
#: ("buyhatke"), not by the real retailer BuyHatke resolves each offer to.
RESULT_TTL_SECONDS = {"buyhatke": 2 * 3600}

#: After a retailer answers with a bot-check page or a throttle (429/503), stop
#: asking it for a while. Retrying a block immediately is what extends it.
COOLDOWN_SECONDS = 30 * 60
BLOCK_MARKERS = ("bot-check", "http 429", "http 503", "http 403")
#: 403 is Forbidden -- in practice that is always a bot-wall response
#: from these sites, never a legitimate per-request error, so it is
#: worth a cooldown exactly like 429/503.

_state_lock = threading.Lock()
_cooldown: dict[str, tuple[float, str]] = {}          # retailer -> (until, why)
_results: dict[tuple[str, str], tuple[float, list[Offer]]] = {}


class RetailerPaused(RuntimeError):
    """Raised instead of querying a retailer that is cooling down."""


def is_block_error(err: Optional[str]) -> bool:
    low = (err or "").lower()
    return any(m in low for m in BLOCK_MARKERS)


def plain_reason(err: Optional[str]) -> Optional[str]:
    """Turn a raw fetch error into a few words a person can act on.

    The raw text ("giving up on https://www.amazon.in/s?k=...: HTTPSConnection
    Pool(...)") is kept for tooltips and logs; this is what the page shows.
    """
    if not err:
        return None
    low = err.lower()
    if "paused" in low:
        return err
    if "bot-check" in low:
        return "blocked by the site's bot check"
    if "http 429" in low or "http 503" in low:
        return "the site is rate-limiting us"
    if "timed out" in low or "timeout" in low:
        return "didn't answer in time"
    if any(k in low for k in ("connection", "resolve", "proxy", "network",
                              "max retries", "ssl")):
        return "network error reaching the site"
    if "http 4" in low or "http 5" in low:
        m = re.search(r"http (\d{3})", low)
        return f"the site returned an error (HTTP {m.group(1)})" if m else "the site returned an error"
    return "the site returned something we couldn't read"


def cooling_down(retailer: str) -> Optional[str]:
    """If `retailer` is paused, return a human sentence saying until when."""
    with _state_lock:
        entry = _cooldown.get(retailer)
        if not entry:
            return None
        until, _why = entry
        if time.time() >= until:
            _cooldown.pop(retailer, None)
            return None
    label = RETAILER_LABELS.get(retailer, retailer)
    resume = time.strftime("%H:%M", time.localtime(until))
    return f"paused after {label} blocked us — trying again after {resume}"


def start_cooldown(retailer: str, why: str,
                   seconds: float = COOLDOWN_SECONDS) -> None:
    with _state_lock:
        _cooldown[retailer] = (time.time() + seconds, why)
    log.warning("[%s] blocked (%s) — pausing it for %d min",
                retailer, why[:80], seconds // 60)


def reset_retailer_state() -> None:
    """Forget cooldowns and cached answers (tests, or a manual retry)."""
    with _state_lock:
        _cooldown.clear()
        _results.clear()


def _cache_get(retailer: str, query: str) -> Optional[tuple[list[Offer], int]]:
    """Return (offers, age in seconds) for a recent identical query, else None."""
    ttl = RESULT_TTL_SECONDS.get(retailer)
    if not ttl:
        return None
    key = (retailer, query.strip().lower())
    with _state_lock:
        hit = _results.get(key)
        if not hit:
            return None
        stored_at, offers = hit
        age = time.time() - stored_at
        if age > ttl:
            _results.pop(key, None)
            return None
    # Copies: the matcher annotates offers, and a cached list must not carry
    # one search's annotations into the next.
    return [replace(o) for o in offers], int(age)


def _cache_put(retailer: str, query: str, offers: list[Offer]) -> None:
    if retailer not in RESULT_TTL_SECONDS:
        return
    with _state_lock:
        _results[(retailer, query.strip().lower())] = (
            time.time(), [replace(o) for o in offers])


@dataclass
class RetailerStatus:
    retailer: str
    label: str
    ok: bool = False
    returned: int = 0            # raw hits the retailer gave us
    matched: int = 0             # hits that survived matching
    error: Optional[str] = None
    ms: int = 0
    cached_age_s: Optional[int] = None   # set when the answer came from cache

    @property
    def reason(self) -> Optional[str]:
        return plain_reason(self.error)


@dataclass
class SetResult:
    set_num: str
    name: str
    offers: list[Offer] = field(default_factory=list)

    @property
    def best(self) -> Optional[Offer]:
        priced = [o for o in self.offers if o.price_inr and o.in_stock is not False]
        return min(priced, key=lambda o: o.price_inr) if priced else None

    @property
    def spread(self) -> Optional[float]:
        """Cheapest vs dearest in-stock offer, as a percentage of the cheapest.
        This is the number that justifies the whole tool existing."""
        priced = [o.price_inr for o in self.offers
                  if o.price_inr and o.in_stock is not False]
        if len(priced) < 2:
            return None
        lo, hi = min(priced), max(priced)
        return round((hi - lo) / lo * 100, 1)


@dataclass
class SearchOutcome:
    query: str
    kind: str                                   # "set_number" | "text"
    sets: list[SetResult] = field(default_factory=list)
    statuses: list[RetailerStatus] = field(default_factory=list)
    elapsed_ms: int = 0
    rejected: int = 0                           # hits dropped by the matcher


# --------------------------------------------------------------------------

def _timed_search(adapter, query: str, limit: int) -> tuple[list[Offer], int]:
    """One retailer query, guarded by the answer cache and the cooldown.

    The cache is checked first: an answer from two hours ago is still worth
    showing while the retailer is paused, as long as it is labelled as such.
    """
    cached = _cache_get(adapter.name, query)
    if cached is not None:
        offers, age = cached
        adapter.cache_age = age
        log.debug("[%s] answer cache hit for %r (%ds old)", adapter.name, query, age)
        return offers, 0

    paused = cooling_down(adapter.name)
    if paused:
        raise RetailerPaused(paused)

    t0 = time.monotonic()
    offers = adapter.search(query, limit=limit)
    ms = int((time.monotonic() - t0) * 1000)

    err = adapter.last_error
    if err and is_block_error(err):
        start_cooldown(adapter.name, err)
    elif not err:
        _cache_put(adapter.name, query, offers)
    return offers, ms


def _query_for(retailer: str, raw: str, kind: str) -> str:
    """Retailers differ in how much help their search needs."""
    if kind == "set_number":
        # Shopify's suggest endpoint matches the bare number well; prefixing
        # "LEGO" narrows Amazon and Flipkart usefully.
        if retailer in ("toycra", "jaimantoys"):
            return raw
        return f"LEGO {raw}"
    low = raw.lower()
    return raw if "lego" in low else f"LEGO {raw}"


# --------------------------------------------------------------------------

def search(query: str,
           session: Optional[PoliteSession] = None,
           sources: Optional[list[str]] = None,
           limit: int = 24,
           refine: bool = True,
           db: Optional[Database] = None,
           deadline: float = DEFAULT_DEADLINE,
           deep: bool = False) -> SearchOutcome:
    """Search every retailer for `query` and return matched, priced offers.

    Always returns within roughly `deadline` seconds. Retailers that have not
    answered by then are reported as timed out rather than holding the result.

    `deep=True` (used only by the background collection sweep in catalog.py's
    price_all, never by an interactive lookup) makes one extra BuyHatke
    request per offer already found, to pull that listing's cross-retailer
    deals and full price history — see `_deep_enrich`. It roughly doubles the
    requests for the set, so it stays off for anything a person is waiting on.
    """
    t0 = time.monotonic()
    raw = query.strip()
    m = SET_NUMBER_QUERY_RE.match(raw)
    kind = "set_number" if m else "text"
    wanted = normalise_set_num(m.group(1)) if m else None

    session = session or PoliteSession(min_interval=1.0, jitter=0.5,
                                       timeout=12, max_retries=2,
                                       backoff_cap=3.0)
    adapters = build_sources(session, list(sources or SEARCH_SOURCES))

    outcome = SearchOutcome(query=raw, kind=kind)
    by_set: dict[str, SetResult] = {}
    status_by_name: dict[str, RetailerStatus] = {}

    # ---- pass 1: broad fan-out -------------------------------------------
    per_retailer_queries = {a.name: _query_for(a.name, raw, kind) for a in adapters}
    first_budget = deadline * (1 - REFINE_RESERVE) if refine else deadline
    grouped: dict[str, list[Offer]] = {}
    for name, (offers, st) in _fan_out_multi(adapters, per_retailer_queries,
                                             limit, first_budget).items():
        grouped[name] = offers
        status_by_name[name] = st

    for name, offers in grouped.items():
        kept = _absorb(offers, wanted, by_set)
        status_by_name[name].matched = kept
        outcome.rejected += len(offers) - kept

    # ---- pass 2: targeted retry for retailers that found nothing ----------
    # A retailer's own search being fuzzy is not evidence it lacks the set.
    target = wanted or (_dominant_set(by_set) if refine else None)
    remaining = deadline - (time.monotonic() - t0)
    if refine and target and remaining > 2.0:
        missing = [a for a in adapters
                   if not any(o.retailer == a.name
                              for o in by_set.get(target, SetResult(target, "")).offers)
                   # Re-sending the exact query the first pass already sent
                   # learns nothing and doubles the load on the retailer — for
                   # Amazon that was half of all traffic, for no gain.
                   and _query_for(a.name, target, "set_number")
                       != per_retailer_queries.get(a.name)]
        if missing:
            log.info("refining: re-querying %s for %s (%.1fs left)",
                     [a.name for a in missing], target, remaining)
            retry_queries = {a.name: _query_for(a.name, target, "set_number")
                             for a in missing}
            for name, (offers, status) in _fan_out_multi(missing, retry_queries,
                                                         limit, remaining).items():
                kept = _absorb(offers, target, by_set)
                st = status_by_name.get(name)
                if st:
                    st.matched += kept
                    st.returned += status.returned
                    # A refine pass that ran out of budget says nothing about
                    # whether the retailer was reachable — the first pass
                    # already answered that. Only report a refine error when
                    # the first pass had not already succeeded.
                    if status.error and st.error is None and not st.ok:
                        st.error = status.error
                    st.ok = st.ok or (kept > 0)
                outcome.rejected += len(offers) - kept

    # ---- assemble ---------------------------------------------------------
    for res in by_set.values():
        res.offers.sort(key=lambda o: (o.price_inr is None,
                                       o.in_stock is False,
                                       o.price_inr or 0))
    sets = list(by_set.values())
    if wanted:
        sets = [s for s in sets if s.set_num == wanted]
    sets.sort(key=lambda s: (-len([o for o in s.offers if o.price_inr]),
                             s.best.price_inr if s.best else float("inf")))

    outcome.sets = sets

    detail_by_url: dict[str, dict] = {}
    if deep and outcome.sets:
        detail_by_url = _deep_enrich(session, outcome)
        for res in outcome.sets:
            res.offers.sort(key=lambda o: (o.price_inr is None,
                                           o.in_stock is False,
                                           o.price_inr or 0))

    outcome.statuses = sorted(status_by_name.values(), key=lambda s: s.label)
    outcome.elapsed_ms = int((time.monotonic() - t0) * 1000)

    if db is not None:
        _persist(db, outcome)
        if detail_by_url:
            _backfill_all_history(db, outcome, detail_by_url)

    log.info("search %r -> %d set(s), %d offers, %dms",
             raw, len(sets), sum(len(s.offers) for s in sets), outcome.elapsed_ms)
    return outcome


def _fan_out_multi(adapters, queries: dict[str, str], limit: int,
                   budget: float) -> dict[str, tuple]:
    """Query every adapter concurrently, giving up after `budget` seconds.

    Plain daemon threads rather than a ThreadPoolExecutor, for one reason:
    abandoning a straggler has to be free. An executor joins its workers on
    shutdown — including via an atexit hook — so a retailer still retrying
    would block the whole interpreter from exiting, and `legotracker search`
    would print its results and then appear to hang. Daemon threads are simply
    dropped, and a stalled retailer's late answer is discarded.
    """
    results: dict[str, tuple] = {}
    started = time.monotonic()
    inbox: "queue.Queue[tuple]" = queue.Queue()

    def worker(adapter) -> None:
        try:
            offers, ms = _timed_search(adapter, queries[adapter.name], limit)
            inbox.put((adapter, offers, ms, None))
        except Exception as exc:  # noqa: BLE001
            inbox.put((adapter, [], int((time.monotonic() - started) * 1000),
                       str(exc)[:200]))

    for a in adapters:
        a.last_error = None
        a.cache_age = None
        threading.Thread(target=worker, args=(a,), daemon=True,
                         name=f"search-{a.name}").start()

    deadline_at = started + max(0.1, budget)
    for _ in adapters:
        remaining = deadline_at - time.monotonic()
        if remaining <= 0:
            break
        try:
            adapter, offers, ms, err = inbox.get(timeout=remaining)
        except queue.Empty:
            break
        status = RetailerStatus(adapter.name,
                                RETAILER_LABELS.get(adapter.name, adapter.name))
        status.ms = ms
        status.returned = len(offers)
        status.error = err or adapter.last_error
        status.ok = status.error is None
        status.cached_age_s = getattr(adapter, "cache_age", None)
        results[adapter.name] = (offers, status)

    slow = [a.name for a in adapters if a.name not in results]
    if slow:
        log.warning("search deadline hit after %.1fs; still waiting on %s",
                    budget, slow)

    elapsed_ms = int((time.monotonic() - started) * 1000)
    for a in adapters:
        results.setdefault(a.name, ([], RetailerStatus(
            a.name, RETAILER_LABELS.get(a.name, a.name),
            error=f"timed out after {budget:.0f}s", ms=elapsed_ms)))
    return results


def _absorb(offers: list[Offer], wanted: Optional[str],
            by_set: dict[str, SetResult]) -> int:
    """Match offers to set numbers and file them. Returns how many were kept."""
    kept = 0
    for offer in offers:
        set_num = _identify(offer, wanted)
        if not set_num:
            continue
        res = by_set.setdefault(set_num, SetResult(set_num, offer.title))
        # Prefer the cleanest available name: the shortest LEGO-branded title.
        if ("lego" in offer.title.lower()
                and len(offer.title) < len(res.name)):
            res.name = offer.title
        if any(o.url == offer.url for o in res.offers):
            continue
        res.offers.append(offer)
        kept += 1
    return kept


def _identify(offer: Offer, wanted: Optional[str]) -> Optional[str]:
    """Which set is this listing, if any? Strict by design."""
    if wanted:
        res = score_listing(offer.title, wanted, brand=offer.brand,
                            price_inr=offer.price_inr)
        return wanted if res.ok else None

    for num in candidate_set_numbers(offer.title)[:3]:
        res = score_listing(offer.title, num, brand=offer.brand,
                            price_inr=offer.price_inr)
        if res.ok:
            return num
    return None


def _dominant_set(by_set: dict[str, SetResult]) -> Optional[str]:
    """For a text query, the set the most retailers agreed on."""
    if not by_set:
        return None
    best = max(by_set.values(), key=lambda s: len(s.offers))
    return best.set_num if len(best.offers) >= 1 else None


def _persist(db: Database, outcome: SearchOutcome) -> None:
    """Record what this search saw, so history accrues as a by-product.

    Deliberately does not mark the set as watched: searching for something once
    should not silently enrol it in daily collection.
    """
    try:
        for res in outcome.sets:
            db.upsert_set(res.set_num, name=res.name)
            for offer in res.offers:
                if offer.price_inr is None:
                    continue
                listing_id = db.upsert_listing(
                    set_num=res.set_num, retailer=offer.retailer,
                    url=offer.url, title=offer.title,
                    retailer_sku=offer.sku, seller=offer.seller,
                    confidence=offer.confidence or 0.9)
                db.record_price(listing_id, offer.price_inr,
                                mrp_inr=offer.mrp_inr, in_stock=offer.in_stock,
                                rating=offer.rating,
                                review_count=offer.review_count)
    except Exception as exc:  # noqa: BLE001
        # Logging a search must never break the search.
        log.warning("could not persist search results: %s", exc)


def _deep_enrich(session: PoliteSession, outcome: SearchOutcome) -> dict[str, dict]:
    """For every offer already found, fetch BuyHatke's own detail page once.

    Two things come out of that one extra request per listing: other
    retailers BuyHatke already prices the SAME product at (folded in here as
    ordinary extra offers on the same set, so they get matched, sorted and
    persisted exactly like anything found by the search route itself), and
    that listing's full price history — returned rather than written here,
    since backfilling needs a listing_id that only exists after `_persist`
    has run.

    A dict keyed by real vendor URL, not by (already deduped) offer object:
    the same URL can legitimately appear on more than one SetResult's offers
    across a text-query outcome, and this is a fetch cache, not a copy.
    """
    detail_by_url: dict[str, dict] = {}
    for res in outcome.sets:
        if cooling_down(buyhatke_source.BuyHatke.name):
            # BuyHatke just blocked us (almost certainly in this same
            # search() call's own fan-out, a few lines up) -- every further
            # detail fetch this round would fail the same way, so stop
            # asking rather than 403 once per remaining offer.
            break
        seen_urls = {o.url for o in res.offers}
        for offer in list(res.offers):
            if offer.url in detail_by_url:
                continue
            if offer.retailer in buyhatke_source.NOT_TRACKED_BY_BUYHATKE:
                # Toycra/Jaiman Toys are queried directly precisely because
                # BuyHatke doesn't track them -- asking it anyway is a
                # guaranteed-fail request on every catalogue set.
                continue
            data = buyhatke_source.fetch_detail(
                session, buyhatke_source.proxy_url(offer.url))
            if not data:
                continue
            detail_by_url[offer.url] = data
            for deal in buyhatke_source.deal_list(data):
                site = str(deal.get("site_name") or "").strip().lower()
                retailer = buyhatke_source.SITE_NAME_TO_RETAILER.get(site)
                link = deal.get("link")
                price = deal.get("price")
                title = deal.get("prod") or res.name
                if (not retailer or not link or link in seen_urls
                        or not isinstance(price, (int, float))):
                    continue
                # BuyHatke's own "same product, other retailer" grouping is
                # not infallible -- seen in the wild bundling a completely
                # unrelated Flipkart listing (a microwave oven) into a LEGO
                # set's deals list. Score it exactly like a fresh search hit
                # rather than trusting BuyHatke's own grouping blindly; the
                # set number has to actually be in the title.
                match = score_listing(title, res.set_num, brand="LEGO")
                if not match.ok:
                    log.info("[buyhatke] dropped mismatched deal %r for set %s (%s)",
                             title, res.set_num, match.reason)
                    continue
                mrp = deal.get("mrpFloat")
                res.offers.append(Offer(
                    retailer=retailer, url=link,
                    title=title,
                    price_inr=float(price),
                    mrp_inr=(float(mrp) if isinstance(mrp, (int, float))
                            and mrp >= price else None),
                    in_stock=True, brand="LEGO"))
                seen_urls.add(link)
    return detail_by_url


def _backfill_all_history(db: Database, outcome: SearchOutcome,
                          detail_by_url: dict[str, dict]) -> None:
    """Import each offer's BuyHatke price history, deduped against what this
    listing already has. Runs after `_persist`, which is what makes today's
    price and the historical series line up under the same listing_id."""
    for res in outcome.sets:
        for offer in res.offers:
            data = detail_by_url.get(offer.url)
            if not data:
                continue
            points = buyhatke_source.history_points(data)
            if not points:
                continue
            listing_id = db.upsert_listing(
                set_num=res.set_num, retailer=offer.retailer, url=offer.url)
            existing = db.observed_timestamps(listing_id)
            for at, price in points:
                # BuyHatke's "2024-04-19 05:05:31" normalised to this
                # project's ISO-with-offset convention; no timezone is
                # published for these, treated as UTC.
                observed_at = at.replace(" ", "T") + "+00:00"
                if observed_at in existing:
                    continue
                db.backfill_price_point(listing_id, observed_at, price_inr=price)
                existing.add(observed_at)
