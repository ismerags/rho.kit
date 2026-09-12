"""Discord notifications through a channel webhook.

A webhook is the simplest thing that works: Discord gives you a URL, anything
POSTed to it appears in that channel, and your phone buzzes like it would for
a friend's message. There is no bot to host and nothing listening for
commands — the Mac posts once a day and that is the whole integration.

The webhook URL is a secret: anyone who has it can post in your channel. It is
kept in data/settings.json (git-ignored, readable only by you), or in the
LEGOTRACKER_DISCORD_WEBHOOK environment variable, which wins if both are set.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

import requests

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH = ROOT / "data" / "settings.json"
ENV_VAR = "LEGOTRACKER_DISCORD_WEBHOOK"

WEBHOOK_RE = re.compile(
    r"^https://(?:(?:canary|ptb)\.)?discord(?:app)?\.com/api/webhooks/"
    r"\d{5,25}/[A-Za-z0-9_\-]{20,120}$")

#: Discord's own limits. Exceeding any of them rejects the whole message.
MAX_EMBEDS_PER_MESSAGE = 10
MAX_EMBED_DESCRIPTION = 4096
MAX_EMBED_TOTAL = 6000


class NotifyError(RuntimeError):
    pass


# ------------------------------------------------------------------ settings

def _read_settings(path: Path = SETTINGS_PATH) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def load_webhook(path: Path = SETTINGS_PATH) -> Optional[str]:
    url = os.environ.get(ENV_VAR) or _read_settings(path).get("discord_webhook")
    return url.strip() if url else None


def save_webhook(url: str, path: Path = SETTINGS_PATH) -> None:
    url = url.strip()
    if not WEBHOOK_RE.match(url):
        raise NotifyError(
            "That doesn't look like a Discord webhook URL. It should start "
            "https://discord.com/api/webhooks/ — copy it from Server Settings → "
            "Integrations → Webhooks → Copy Webhook URL.")
    settings = _read_settings(path)
    settings["discord_webhook"] = url
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def mask(url: Optional[str]) -> str:
    """Show enough of the webhook to recognise it, never the secret part."""
    if not url:
        return "(not set)"
    m = re.match(r"(https://[^/]+/api/webhooks/\d+/)(.{4})", url)
    return f"{m.group(1)}{m.group(2)}…" if m else "(set)"


# ------------------------------------------------------------------ posting

class Discord:
    def __init__(self, webhook_url: str, username: str = "LEGO Tracker",
                 timeout: float = 20.0):
        if not WEBHOOK_RE.match(webhook_url or ""):
            raise NotifyError("Discord webhook URL is missing or malformed.")
        self.url = webhook_url
        self.username = username
        self.timeout = timeout
        self.http = requests.Session()
        self.http.headers["User-Agent"] = "DiscordBot (lego-price-tracker, 1.0)"

    def post(self, content: Optional[str] = None,
             embeds: Optional[list[dict]] = None,
             file: Optional[tuple[str, bytes, str]] = None) -> None:
        """Send one message. `file` = (filename, bytes, mime type).

        Retries once on Discord's own rate limit (HTTP 429 with retry_after);
        anything else that is not 2xx raises, with Discord's reason attached.
        """
        payload: dict = {"username": self.username,
                         "allowed_mentions": {"parse": []}}
        if content:
            payload["content"] = content[:2000]
        if embeds:
            payload["embeds"] = embeds[:MAX_EMBEDS_PER_MESSAGE]

        for attempt in range(3):
            if file:
                name, data, mime = file
                resp = self.http.post(
                    self.url, params={"wait": "true"}, timeout=self.timeout,
                    data={"payload_json": json.dumps(payload)},
                    files={"files[0]": (name, data, mime)})
            else:
                resp = self.http.post(self.url, params={"wait": "true"},
                                      json=payload, timeout=self.timeout)

            if resp.status_code == 429 and attempt < 2:
                try:
                    wait = float(resp.json().get("retry_after", 2))
                except ValueError:
                    wait = 2.0
                time.sleep(min(wait, 30.0) + 0.25)
                continue
            if 200 <= resp.status_code < 300:
                return
            if resp.status_code in (401, 404):
                raise NotifyError(
                    f"Discord rejected the webhook (HTTP {resp.status_code}). "
                    "It was probably deleted — make a new one and run "
                    "`python -m legotracker discord <new url>`.")
            raise NotifyError(f"Discord HTTP {resp.status_code}: {resp.text[:300]}")
        raise NotifyError("Discord kept rate-limiting the webhook.")

    def post_many(self, messages: list[dict]) -> None:
        """Send several messages in order, spaced to stay under Discord's
        webhook rate limit (about 5 per 2 seconds)."""
        for i, msg in enumerate(messages):
            if i:
                time.sleep(0.6)
            self.post(**msg)


def embed_size(embed: dict) -> int:
    """Characters Discord counts towards the 6,000-per-message budget."""
    n = len(embed.get("title", "")) + len(embed.get("description", ""))
    n += len((embed.get("footer") or {}).get("text", ""))
    n += len((embed.get("author") or {}).get("name", ""))
    for f in embed.get("fields", []):
        n += len(f.get("name", "")) + len(f.get("value", ""))
    return n


def pack_embeds(embeds: list[dict]) -> list[list[dict]]:
    """Split embeds into groups that each fit in one Discord message."""
    groups: list[list[dict]] = []
    cur: list[dict] = []
    size = 0
    for e in embeds:
        n = embed_size(e)
        if cur and (len(cur) >= MAX_EMBEDS_PER_MESSAGE or size + n > MAX_EMBED_TOTAL):
            groups.append(cur)
            cur, size = [], 0
        cur.append(e)
        size += n
    if cur:
        groups.append(cur)
    return groups
