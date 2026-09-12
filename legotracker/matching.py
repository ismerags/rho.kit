"""Turning messy marketplace titles into a confident (set_num, listing) match.

This is the part that decides whether the tracker is trustworthy. Indian
marketplace search is fuzzy and heavily polluted:

  query "LEGO 75192"  ->  "Millennium Falcon Vertical Display Stand for Lego 75192"
  query "lego star wars" -> "Pureey StarLinks: Kids' Educational Interlocking Blocks"

So we never trust search rank. We extract the set number from the title and
score the listing, and anything below the threshold is dropped rather than
recorded as a price for a set it is not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional

# LEGO set numbers are 3-7 digits. Modern retail sets are almost all 4-6.
SETNUM_RE = re.compile(r"(?<!\d)(\d{4,7})(?!\d)")

# Phrases that mean "this is not the set itself".
ACCESSORY_MARKERS = (
    "display stand", "display case", "stand for", "case for", "acrylic",
    "light kit", "lighting kit", "led light", "lights for", "briksmax",
    "lightailing", "compatible with", "compatible for", "replacement",
    "instruction", "manual only", "sticker", "decal", "keychain", "keyring",
    "key chain", "magnet", "poster", "frame for", "storage box", "brick separator",
    "minifigure lot", "minifig lot", "custom", "moc ", "not lego",
    "building block set for", "cover for", "mount for", "wall mount",
    "dust cover", "showcase", "vertical stand", "empty box", "box only",
)

# Words that raise confidence a listing is a genuine LEGO product.
BRAND_MARKERS = ("lego", "lego®", "lego(r)")

# Known non-set-number numeric patterns inside titles.
PIECE_COUNT_RE = re.compile(
    r"(\d{3,6})\s*(?:\+\s*)?(?:pieces|pcs|piece|parts|blocks|bricks)\b", re.I)
AGE_RE = re.compile(r"\b(\d{1,2})\s*\+", re.I)


@dataclass
class MatchResult:
    set_num: Optional[str]
    confidence: float
    reason: str

    @property
    def ok(self) -> bool:
        return self.set_num is not None and self.confidence >= 0.55


def normalise_set_num(raw: str) -> str:
    """'75192-1' -> '75192';  ' 10307 ' -> '10307'."""
    raw = str(raw).strip()
    if "-" in raw:
        raw = raw.split("-", 1)[0]
    return raw.strip()


def candidate_set_numbers(title: str) -> list[str]:
    """All plausible set numbers in a title, best guess first.

    Piece counts and ages are removed before matching, because
    "Cole's Elemental Earth Mech Toy 71806 ( 235 Pieces)" contains two numbers
    and only one of them is the set.
    """
    if not title:
        return []
    cleaned = PIECE_COUNT_RE.sub(" ", title)
    cleaned = AGE_RE.sub(" ", cleaned)

    found: list[str] = []
    for m in SETNUM_RE.finditer(cleaned):
        num = m.group(1)
        # A 4-digit number that reads like a year is usually not a set number
        # in a marketplace title (e.g. "2024 edition").
        if len(num) == 4 and 1990 <= int(num) <= 2035:
            continue
        if num not in found:
            found.append(num)

    # 5-digit numbers are the most common modern set length; rank them first.
    found.sort(key=lambda n: (0 if len(n) in (5, 6) else 1))
    return found


def _token_overlap(a: str, b: str) -> float:
    stop = {"lego", "the", "and", "set", "toy", "toys", "building", "kit",
            "blocks", "brick", "bricks", "for", "with", "of", "a", "gift"}
    ta = {t for t in re.findall(r"[a-z0-9]+", (a or "").lower()) if t not in stop}
    tb = {t for t in re.findall(r"[a-z0-9]+", (b or "").lower()) if t not in stop}
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def score_listing(title: str, want_set_num: str,
                  known_name: Optional[str] = None,
                  brand: Optional[str] = None,
                  price_inr: Optional[float] = None,
                  msrp_usd: Optional[float] = None) -> MatchResult:
    """Score how likely `title` is the retail box for `want_set_num`."""
    if not title:
        return MatchResult(None, 0.0, "no title")

    want = normalise_set_num(want_set_num)
    low = title.lower()
    reasons: list[str] = []

    for marker in ACCESSORY_MARKERS:
        if marker in low:
            return MatchResult(None, 0.0, f"accessory/knock-off marker: '{marker.strip()}'")

    cands = candidate_set_numbers(title)
    if want not in cands:
        return MatchResult(None, 0.0, f"set number {want} not in title")

    score = 0.60
    reasons.append("set number in title")

    if cands[0] == want:
        score += 0.05
        reasons.append("primary numeric token")

    if any(b in low for b in BRAND_MARKERS):
        score += 0.20
        reasons.append("LEGO brand in title")
    elif brand and "lego" in brand.lower():
        score += 0.15
        reasons.append("LEGO brand field")
    else:
        score -= 0.25
        reasons.append("no LEGO brand anywhere")

    if known_name:
        ov = _token_overlap(title, known_name)
        if ov >= 0.30:
            score += 0.15
            reasons.append(f"name overlap {ov:.0%}")
        elif ov > 0:
            score += 0.05

    # A price wildly below plausible retail is a knock-off or a parts lot.
    if price_inr is not None and msrp_usd:
        implied_floor = msrp_usd * 40 * 0.25   # ~USD->INR, then 75% off
        if price_inr < implied_floor:
            score -= 0.35
            reasons.append(f"price ₹{price_inr:,.0f} implausibly low vs ${msrp_usd} MSRP")

    score = max(0.0, min(1.0, score))
    return MatchResult(want, score, "; ".join(reasons))


def best_match(candidates: Iterable[dict], want_set_num: str,
               known_name: Optional[str] = None,
               msrp_usd: Optional[float] = None) -> Optional[dict]:
    """Pick the highest-confidence candidate dict, or None.

    Each candidate must have at least 'title'; may have 'price_inr', 'brand'.
    The winning dict is returned with 'confidence' and 'match_reason' added.
    """
    best: Optional[dict] = None
    best_score = 0.0
    for cand in candidates:
        res = score_listing(
            cand.get("title", ""),
            want_set_num,
            known_name=known_name,
            brand=cand.get("brand"),
            price_inr=cand.get("price_inr"),
            msrp_usd=msrp_usd,
        )
        if res.ok and res.confidence > best_score:
            enriched = dict(cand)
            enriched["confidence"] = res.confidence
            enriched["match_reason"] = res.reason
            best, best_score = enriched, res.confidence
    return best
