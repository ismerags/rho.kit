"""Turning raw price history into a buy/wait decision.

The whole point of the tracker is answering one question: **is today's price
actually good, or does it just look good next to an inflated MRP?**

Indian marketplaces quote a "MRP" that is often fictional — a 60% discount off a
made-up MRP is not a deal. So the anchor here is the set's own trading history,
not the sticker. Three signals, all explainable:

  1. Discount vs the 90-day median   -> "cheaper than it normally is"
  2. Percentile rank in its own history -> "how close to its floor"
  3. Discount vs MRP                 -> the weakest signal, capped and down-weighted

Analogy: MRP is the asking price on a house listing; the 90-day median is what
comparable houses on the street actually sold for. You negotiate against the
second number.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

# How much history must exist before a "typical price" means anything.
MIN_OBSERVATIONS_FOR_TYPICAL = 4

# An "all-time low" that beats the old floor by ₹10 on a ₹3,000 set is noise,
# not news. Alerts must clear a move worth acting on.
MIN_MEANINGFUL_MOVE = 0.02      # 2%
MIN_MEANINGFUL_RUPEES = 50.0


def _parse_ts(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass
class PriceStats:
    n: int = 0
    current: Optional[float] = None
    low: Optional[float] = None
    high: Optional[float] = None
    median_30d: Optional[float] = None
    median_90d: Optional[float] = None
    #: Where today sits in the range of every PREVIOUS observation, 0.0 = at or
    #: below the old floor. Today's own price is excluded on purpose: including
    #: it makes every new low score 0.0 no matter how small the move, which
    #: turns a ₹10 dip into "at the bottom of its range".
    percentile: Optional[float] = None
    prior_low: Optional[float] = None
    first_seen: Optional[str] = None
    last_seen: Optional[str] = None

    @property
    def typical(self) -> Optional[float]:
        """Best available anchor for 'what this normally costs'."""
        return self.median_90d or self.median_30d


def compute_stats(points: Sequence[dict]) -> PriceStats:
    """`points` = [{'observed_at': iso, 'price_inr': float|None}, ...] any order."""
    priced = [p for p in points
              if p.get("price_inr") is not None and p["price_inr"] > 0]
    if not priced:
        return PriceStats()

    priced = sorted(priced, key=lambda p: p["observed_at"])
    values = [float(p["price_inr"]) for p in priced]
    now = datetime.now(timezone.utc)

    def median_within(days: int) -> Optional[float]:
        cutoff = now - timedelta(days=days)
        window = [float(p["price_inr"]) for p in priced
                  if _parse_ts(p["observed_at"]) >= cutoff]
        if len(window) < MIN_OBSERVATIONS_FOR_TYPICAL:
            return None
        return statistics.median(window)

    current = values[-1]
    low, high = min(values), max(values)

    prior = values[:-1]
    pct = None
    prior_low = None
    if len(prior) >= 3:
        prior_low, prior_high = min(prior), max(prior)
        if prior_high > prior_low:
            pct = (current - prior_low) / (prior_high - prior_low)
            pct = max(0.0, min(1.0, pct))
        else:
            # Perfectly flat history: only a real move off it means anything.
            pct = 0.0 if current < prior_low else (
                1.0 if current > prior_low else 0.5)

    return PriceStats(
        n=len(values),
        current=current,
        low=low,
        high=high,
        median_30d=median_within(30),
        median_90d=median_within(90),
        percentile=pct,
        prior_low=prior_low,
        first_seen=priced[0]["observed_at"],
        last_seen=priced[-1]["observed_at"],
    )


@dataclass
class DealVerdict:
    score: int = 0                     # 0-100, higher = better time to buy
    label: str = "No data"
    reasons: list[str] = field(default_factory=list)
    vs_typical_pct: Optional[float] = None    # +12.0 means 12% below typical
    vs_mrp_pct: Optional[float] = None
    is_all_time_low: bool = False
    confidence: str = "low"            # low | medium | high — depends on history depth


def score_deal(stats: PriceStats, mrp: Optional[float] = None,
               target: Optional[float] = None) -> DealVerdict:
    v = DealVerdict()
    if stats.current is None:
        return v

    price = stats.current
    reasons: list[str] = []
    score = 50.0    # neutral starting point

    # --- 1. versus its own typical price (the dominant term) ----------------
    typical = stats.typical
    if typical and typical > 0:
        delta = (typical - price) / typical
        v.vs_typical_pct = round(delta * 100, 1)
        score += max(-30.0, min(30.0, delta * 100))
        if delta >= 0.08:
            reasons.append(f"{delta:.0%} below its "
                           f"{'90' if stats.median_90d else '30'}-day median")
        elif delta <= -0.05:
            reasons.append(f"{-delta:.0%} ABOVE its usual price")
    else:
        reasons.append("not enough history for a typical price yet")

    # --- 2. position against everything seen BEFORE today -------------------
    if stats.percentile is not None and stats.n >= MIN_OBSERVATIONS_FOR_TYPICAL:
        score += (0.5 - stats.percentile) * 14
        beat_floor_by = (
            (stats.prior_low - price) / stats.prior_low
            if stats.prior_low else 0.0
        )
        if stats.low is not None and price <= stats.low * 1.005:
            v.is_all_time_low = True
            reasons.append(f"lowest price we have ever recorded (₹{price:,.0f})")
            # Only a move worth acting on earns the bonus — matches the alert rule.
            if beat_floor_by >= MIN_MEANINGFUL_MOVE:
                score += 5
        elif stats.percentile <= 0.25:
            reasons.append("near the bottom of its tracked range")

    # --- 3. versus MRP (deliberately weak) ----------------------------------
    if mrp and mrp > price:
        off = (mrp - price) / mrp
        v.vs_mrp_pct = round(off * 100, 1)
        score += min(6.0, off * 12)        # capped: MRP is a soft number
        reasons.append(f"{off:.0%} off listed MRP ₹{mrp:,.0f}")

    # --- 4. explicit user target overrides everything -----------------------
    if target and price <= target:
        score = max(score, 85.0)
        reasons.insert(0, f"at or below your ₹{target:,.0f} target")

    v.score = int(max(0, min(100, round(score))))
    v.reasons = reasons

    if stats.n >= 20:
        v.confidence = "high"
    elif stats.n >= MIN_OBSERVATIONS_FOR_TYPICAL:
        v.confidence = "medium"
    else:
        v.confidence = "low"

    if v.confidence == "low":
        v.label = "Watching"
    elif v.score >= 80:
        v.label = "Strong buy"
    elif v.score >= 65:
        v.label = "Good price"
    elif v.score >= 45:
        v.label = "Fair"
    elif v.score >= 35:
        v.label = "Wait"
    else:
        v.label = "Overpriced"
    return v


def value_per_piece(price: Optional[float],
                    pieces: Optional[int]) -> Optional[float]:
    if not price or not pieces or pieces <= 0:
        return None
    return round(price / pieces, 2)


def detect_alerts(set_num: str, listing_id: int, retailer: str,
                  stats: PriceStats, prev_price: Optional[float],
                  target: Optional[float] = None) -> list[dict]:
    """Alerts worth waking someone up for. Kept deliberately few."""
    out: list[dict] = []
    price = stats.current
    if price is None:
        return out

    if target and price <= target and (prev_price is None or prev_price > target):
        out.append({
            "kind": "target_hit", "price_inr": price, "prev_price": prev_price,
            "message": f"{set_num} hit your ₹{target:,.0f} target — ₹{price:,.0f} on {retailer}",
        })

    moved_enough = (
        prev_price is not None
        and (prev_price - price) >= max(MIN_MEANINGFUL_RUPEES,
                                        prev_price * MIN_MEANINGFUL_MOVE)
    )
    if (stats.low is not None and price <= stats.low * 1.005
            and stats.n >= MIN_OBSERVATIONS_FOR_TYPICAL
            and moved_enough):
        out.append({
            "kind": "all_time_low", "price_inr": price, "prev_price": prev_price,
            "message": f"{set_num} at an all-time low: ₹{price:,.0f} on {retailer} "
                       f"(was ₹{prev_price:,.0f})",
        })

    if prev_price and price < prev_price * 0.90:
        drop = (prev_price - price) / prev_price
        out.append({
            "kind": "drop", "price_inr": price, "prev_price": prev_price,
            "message": f"{set_num} dropped {drop:.0%} on {retailer}: "
                       f"₹{prev_price:,.0f} → ₹{price:,.0f}",
        })

    return out
