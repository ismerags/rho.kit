"""Common interface every retailer adapter implements."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from ..http_client import PoliteSession

log = logging.getLogger(__name__)

RUPEE_RE = re.compile(r"[₹Rs\.\s]*([\d][\d,]*(?:\.\d{1,2})?)")


def parse_inr(text: Optional[str]) -> Optional[float]:
    """'₹8,695.00' -> 8695.0 ; 'Rs. 2,175' -> 2175.0 ; junk -> None."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    m = RUPEE_RE.search(str(text).replace("₹", "₹"))
    if not m:
        return None
    try:
        val = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    # Guard against parsing a rating ("4.8") or a review count as a price.
    return val if val >= 50 else None


@dataclass
class Offer:
    """One retailer's current state for one product page."""
    retailer: str
    url: str
    title: str
    price_inr: Optional[float] = None
    mrp_inr: Optional[float] = None
    in_stock: Optional[bool] = None
    seller: Optional[str] = None
    sku: Optional[str] = None
    rating: Optional[float] = None
    review_count: Optional[int] = None
    brand: Optional[str] = None
    image: Optional[str] = None
    confidence: float = 0.0
    match_reason: str = ""

    def as_candidate(self) -> dict:
        return {
            "title": self.title,
            "price_inr": self.price_inr,
            "brand": self.brand,
            "url": self.url,
            "_offer": self,
        }


class Source:
    """Base adapter. Subclasses implement search() and optionally fetch_offer()."""

    name: str = "base"
    label: str = "Base"
    base_url: str = ""

    def __init__(self, session: PoliteSession):
        self.session = session
        #: Set by adapters when a fetch fails, so `diagnose` can tell
        #: "could not reach the site" apart from "reached it, parsed nothing".
        #: Those two need completely different fixes.
        self.last_error: Optional[str] = None

    # -- discovery -----------------------------------------------------------

    def search(self, query: str, limit: int = 24) -> list[Offer]:
        """Return offers for a free-text query. Must never raise on parse
        problems — return [] and log, so one broken retailer cannot abort a run.
        """
        raise NotImplementedError

    # -- refresh -------------------------------------------------------------

    def fetch_offer(self, url: str) -> Optional[Offer]:
        """Re-read a known product page for its current price.

        Default implementation is None, meaning "this source can only be
        refreshed via search". Adapters that can read a product page override it.
        """
        return None

    # -- helpers -------------------------------------------------------------

    def _safe(self, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - resilience is the point here
            log.warning("[%s] %s failed: %s", self.name, fn.__name__, exc)
            return None
