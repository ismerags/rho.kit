"""Polite HTTP layer shared by every retailer adapter.

Design goals:
  * Look like a real browser (retailers reject bare python-requests UA).
  * Never hammer a host: per-domain minimum interval + jitter.
  * Survive transient failures with bounded exponential backoff.
  * Cache raw responses on disk so re-parsing during development costs nothing.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import random
import threading
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests

log = logging.getLogger(__name__)

# Recent desktop UA strings. Rotated per-session, not per-request: flipping the
# UA on every request inside one session is itself a bot signal.
USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.3 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
]

BASE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
              "image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-IN,en-GB;q=0.9,en;q=0.8",
    # NOTE: Accept-Encoding is deliberately NOT set here.
    #
    # requests/urllib3 build it from the codecs they can actually decode —
    # "gzip, deflate", plus "br" only when the optional `brotli` package is
    # installed. Hardcoding "gzip, deflate, br" advertises Brotli even when
    # nothing can decode it; urllib3 then returns the body UNDECODED and
    # response.text is mojibake. Nothing raises: BeautifulSoup finds zero
    # products and json.loads fails, so every retailer just reports "no match".
    # Let the library speak for itself.
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}


class FetchError(RuntimeError):
    """Raised when a URL could not be retrieved after all retries."""


#: Hosts that need more room than the session default. Amazon and Flipkart
#: bot-wall scripted traffic that arrives in bursts; the Shopify and Fynd JSON
#: APIs do not care. Applied as a floor: a session configured slower than this
#: stays slower.
HOST_MIN_INTERVAL = {
    "www.amazon.in": 4.0,
    "www.flipkart.com": 3.0,
    # Not a bot-walled host — BuyHatke is a public, unauthenticated aggregator
    # that is happy to be hit. This floor exists purely to be a good citizen,
    # not because it has ever throttled us.
    "buyhatke.com": 1.5,
}


class PoliteSession:
    """One shared session with per-domain throttling and an optional disk cache."""

    def __init__(
        self,
        min_interval: float = 3.0,
        jitter: float = 2.0,
        timeout: int = 30,
        max_retries: int = 3,
        cache_dir: Optional[Path] = None,
        cache_ttl: int = 0,
        backoff_cap: float = 30.0,
    ):
        self.min_interval = min_interval
        self.jitter = jitter
        self.timeout = timeout
        self.max_retries = max_retries
        #: Longest single backoff sleep. Batch jobs can afford to wait out a
        #: throttle; an interactive search cannot — a 60-second sleep there is
        #: indistinguishable from a hang, so `serve`/`search` pass a small cap.
        self.backoff_cap = backoff_cap
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.cache_ttl = cache_ttl
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

        # Earliest monotonic time each host may be hit again.
        self._next_ok: dict[str, float] = {}
        self._lock = threading.Lock()

        # requests.Session is NOT thread-safe (its connection pool and cookie
        # jar are shared mutable state), and the search fans out to six
        # retailers at once. Give each thread its own Session; the throttle
        # state above stays shared, which is the part that must be global.
        # One UA for the whole run — rotating it per request is a bot signal.
        self._user_agent = random.choice(USER_AGENTS)
        self._local = threading.local()

    @property
    def session(self) -> requests.Session:
        sess = getattr(self._local, "session", None)
        if sess is None:
            sess = requests.Session()
            sess.headers.update(BASE_HEADERS)
            sess.headers["User-Agent"] = self._user_agent
            self._local.session = sess
        return sess

    # ---------------------------------------------------------------- caching

    def _cache_path(self, url: str) -> Optional[Path]:
        if not self.cache_dir:
            return None
        key = hashlib.sha256(url.encode()).hexdigest()[:32]
        host = urlparse(url).netloc.replace(".", "_")
        return self.cache_dir / f"{host}_{key}.html.gz"

    def _cache_read(self, url: str) -> Optional[str]:
        path = self._cache_path(url)
        if not path or not path.exists() or self.cache_ttl <= 0:
            return None
        if time.time() - path.stat().st_mtime > self.cache_ttl:
            return None
        try:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            return None

    def _cache_write(self, url: str, body: str) -> None:
        path = self._cache_path(url)
        if not path:
            return
        try:
            with gzip.open(path, "wt", encoding="utf-8") as fh:
                fh.write(body)
        except OSError as exc:  # cache failures must never break a run
            log.debug("cache write failed for %s: %s", url, exc)

    # --------------------------------------------------------------- throttle

    def _wait_turn(self, host: str) -> None:
        """Rate-limit per host, never globally.

        The sleep must happen OUTSIDE the lock. Holding the lock while sleeping
        makes every host wait behind every other host, which silently turns a
        parallel six-retailer fan-out back into a serial one — the difference
        between a 3-second search and a 20-second one.
        """
        while True:
            with self._lock:
                now = time.monotonic()
                next_ok = self._next_ok.get(host, 0.0)
                if now >= next_ok:
                    gap = max(self.min_interval, HOST_MIN_INTERVAL.get(host, 0.0))
                    self._next_ok[host] = (now + gap
                                           + random.uniform(0, self.jitter))
                    return
                wait = next_ok - now
            time.sleep(min(wait, 5.0))

    # ------------------------------------------------------------------ fetch

    def get(self, url: str, referer: Optional[str] = None,
            use_cache: bool = True,
            extra_headers: Optional[dict[str, str]] = None) -> str:
        """Return the response body as text, or raise FetchError."""
        if use_cache:
            cached = self._cache_read(url)
            if cached is not None:
                log.debug("cache hit %s", url)
                return cached

        host = urlparse(url).netloc
        headers = {}
        if referer:
            headers["Referer"] = referer
            headers["Sec-Fetch-Site"] = "same-origin"
        if extra_headers:
            headers.update(extra_headers)

        last_exc: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            self._wait_turn(host)
            try:
                resp = self.session.get(url, headers=headers,
                                        timeout=self.timeout)
            except requests.RequestException as exc:
                last_exc = exc
                log.warning("attempt %d/%d network error on %s: %s",
                            attempt, self.max_retries, url, exc)
            else:
                if resp.status_code == 200:
                    undecoded = self._undecoded_encoding(resp)
                    if undecoded:
                        # Fail loudly rather than parsing binary as if it were
                        # HTML. A silent "no results" is far worse than an error.
                        raise FetchError(
                            f"{host} returned Content-Encoding: {undecoded}, "
                            f"which this Python cannot decode. "
                            f"Fix: pip install brotli")
                    body = resp.text
                    if self._looks_like_block(body):
                        last_exc = FetchError(f"bot-check page returned by {host}")
                        log.warning("attempt %d/%d bot check on %s",
                                    attempt, self.max_retries, url)
                    else:
                        if use_cache:
                            self._cache_write(url, body)
                        return body
                elif resp.status_code in (429, 503):
                    last_exc = FetchError(f"HTTP {resp.status_code} from {host}")
                    log.warning("attempt %d/%d throttled (%d) on %s",
                                attempt, self.max_retries, resp.status_code, url)
                    time.sleep(min(self.backoff_cap, 10 * attempt))
                else:
                    raise FetchError(f"HTTP {resp.status_code} for {url}")

            if attempt < self.max_retries:
                time.sleep(min(self.backoff_cap,
                               (2 ** attempt) + random.uniform(0, 2)))

        raise FetchError(f"giving up on {url}: {last_exc}")

    def get_json(self, url: str, referer: Optional[str] = None,
                 use_cache: bool = False,
                 extra_headers: Optional[dict[str, str]] = None):
        """Fetch and parse JSON. Raises FetchError on a non-JSON body, because
        an HTML error page parsed as 'no results' hides real breakage."""
        body = self.get(url, referer=referer, use_cache=use_cache,
                        extra_headers=extra_headers)
        try:
            return json.loads(body)
        except ValueError as exc:
            raise FetchError(
                f"expected JSON from {url}, got {body[:80]!r}") from exc

    @staticmethod
    def _undecoded_encoding(resp) -> Optional[str]:
        """Return the Content-Encoding if the body came back still compressed.

        urllib3 strips Content-Encoding once it decodes a body. If the header
        survives into the response we are holding compressed bytes that every
        downstream parser will quietly misread.
        """
        enc = (resp.headers.get("Content-Encoding") or "").strip().lower()
        if not enc or enc in ("identity", "gzip", "deflate", "x-gzip"):
            # gzip/deflate are always decodable; urllib3 leaves the header on
            # some redirect paths even after decoding, so trust the content.
            return None
        raw = resp.content[:4]
        if enc == "br" and raw[:1] in (b"\x1b", b"\x0b", b"\x21", b"\x8b", b"\xa1"):
            return enc
        if enc == "zstd" and raw[:4] == b"\x28\xb5\x2f\xfd":
            return enc
        return enc if enc not in ("gzip", "deflate") else None

    @staticmethod
    def _looks_like_block(body: str) -> bool:
        """Detect the common Indian-retailer bot-wall bodies."""
        if len(body) < 3000:
            head = body.lower()
            if "captcha" in head or "robot" in head or "access denied" in head:
                return True
        low = body[:6000].lower()
        markers = (
            "enter the characters you see below",   # Amazon
            "to discuss automated access",          # Amazon
            "request blocked",
            "are you a human",
            "px-captcha",                           # PerimeterX
            "incapsula incident id",                # Imperva
        )
        return any(m in low for m in markers)
