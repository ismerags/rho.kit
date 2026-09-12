"""Reads the hand-edited content files: site settings and build posts.

The format is deliberately not JSON, TOML or YAML. Those are all editable in a
browser, but each of them fails loudly and unhelpfully on the mistakes a person
actually makes at 11pm on a phone — a smart quote, a trailing comma, a tab
instead of two spaces. A build page going blank because of a comma is a bad
trade for strictness nobody needed.

So: ``key: value`` lines, a ``---`` divider, then free text. Indented lines
continue the key above as a list. Unknown keys are ignored, missing keys get
defaults, and a file that cannot be understood at all is skipped with a warning
rather than taking the site down with it.

    title: Mercedes-Benz G 500 Professional Line
    set: 42177
    status: building
    progress: 60
    instagram: https://www.instagram.com/p/abc123/
    photos:
      chassis.jpg
      https://example.com/hosted.jpg
    ---
    Day 12. The chassis is finally done and it is heavier than it looks.

Standard library only, because this runs inside GitHub Actions alongside the
site generator.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

DIVIDER_RE = re.compile(r"^\s*-{3,}\s*$")
KEY_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_ -]{0,40}):\s*(.*)$")

#: Build states, in the order they should appear on the page.
STATUSES = ("building", "planning", "complete")

STATUS_LABELS = {
    "building": "In progress",
    "planning": "Up next",
    "complete": "Finished",
}


def parse_fields(text: str) -> tuple[dict, str]:
    """Split a content file into its key/value header and its free-text body.

    Returns ``(fields, body)``. Values are strings; a key whose value is empty
    and which is followed by indented lines becomes a list of those lines.
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    fields: dict = {}
    body_start = len(lines)
    last_key: Optional[str] = None

    for i, line in enumerate(lines):
        if DIVIDER_RE.match(line):
            body_start = i + 1
            break
        if not line.strip():
            last_key = None
            continue

        # An indented line continues the previous key as a list item. This is
        # what makes multi-photo posts writable without any punctuation.
        if line[:1] in (" ", "\t") and last_key:
            existing = fields.get(last_key)
            if isinstance(existing, list):
                existing.append(line.strip())
            else:
                fields[last_key] = [line.strip()] if not existing else [
                    str(existing), line.strip()]
            continue

        m = KEY_RE.match(line)
        if m:
            key = m.group(1).strip().lower().replace(" ", "_").replace("-", "_")
            value = m.group(2).strip()
            fields[key] = value
            last_key = key
        else:
            # Not a key line and not indented — treat everything from here as
            # body, so a missing `---` is a typo rather than a broken page.
            body_start = i
            break

    body = "\n".join(lines[body_start:]).strip()
    return fields, body


def _as_list(value) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [v for v in (s.strip() for s in value) if v]
    # Also accept a one-line comma-separated list.
    return [v for v in (s.strip() for s in str(value).split(",")) if v]


def _as_int(value, default=None) -> Optional[int]:
    try:
        return int(re.sub(r"[^\d-]", "", str(value)))
    except (TypeError, ValueError):
        return default


@dataclass
class Build:
    slug: str
    title: str
    status: str = "building"
    set_num: Optional[str] = None
    progress: Optional[int] = None
    started: str = ""
    completed: str = ""
    instagram: str = ""
    cover: str = ""
    photos: list[str] = field(default_factory=list)
    body: str = ""
    source: str = ""

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status.title())

    @property
    def is_current(self) -> bool:
        return self.status == "building"


def parse_build(path: Path) -> Optional[Build]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:                                  # pragma: no cover
        log.warning("could not read %s: %s", path, exc)
        return None

    fields, body = parse_fields(raw)
    title = str(fields.get("title") or "").strip()
    if not title:
        log.warning("%s has no `title:` line — skipping it", path.name)
        return None

    status = str(fields.get("status") or "building").strip().lower()
    if status not in STATUSES:
        log.warning("%s: unknown status %r, treating it as 'building'",
                    path.name, status)
        status = "building"

    progress = _as_int(fields.get("progress"))
    if progress is not None:
        progress = max(0, min(100, progress))
    if status == "complete" and progress is None:
        progress = 100

    set_num = str(fields.get("set") or fields.get("set_num") or "").strip()
    set_num = set_num if re.fullmatch(r"\d{4,7}", set_num) else None

    photos = _as_list(fields.get("photos"))
    cover = str(fields.get("cover") or "").strip()
    if not cover and photos:
        cover = photos[0]

    return Build(
        slug=re.sub(r"[^a-z0-9-]+", "-", path.stem.lower()).strip("-") or "build",
        title=title,
        status=status,
        set_num=set_num,
        progress=progress,
        started=str(fields.get("started") or "").strip(),
        completed=str(fields.get("completed") or "").strip(),
        instagram=str(fields.get("instagram") or "").strip(),
        cover=cover,
        photos=photos,
        body=body,
        source=path.name,
    )


def load_builds(content_dir: Path) -> list[Build]:
    """Read every ``*.md`` in ``content/builds/``.

    Ordered: the build in progress first (that is what the home page leads
    with), then anything planned, then finished builds newest first. Filenames
    are expected to start with a date (``2026-09-g-wagon.md``) which makes the
    reverse-alphabetical sort a reverse-chronological one — but nothing breaks
    if they don't.
    """
    folder = Path(content_dir) / "builds"
    if not folder.is_dir():
        return []

    # A leading underscore means "not a post" — it keeps _TEMPLATE.md around as
    # something to copy without it appearing on the site.
    files = [p for p in sorted(folder.glob("*.md")) if not p.name.startswith("_")]
    builds = [b for b in (parse_build(p) for p in files) if b is not None]

    order = {s: i for i, s in enumerate(STATUSES)}
    builds.sort(key=lambda b: (order.get(b.status, 99), b.source), reverse=False)
    # Within "complete", newest first.
    done = [b for b in builds if b.status == "complete"]
    rest = [b for b in builds if b.status != "complete"]
    done.sort(key=lambda b: b.source, reverse=True)
    return rest + done


@dataclass
class SiteConfig:
    title: str = "LEGO Price Tracker India"
    tagline: str = "Live price comparison across Indian LEGO retailers."
    author: str = ""
    instagram: str = ""
    instagram_handle: str = ""
    repo: str = ""
    description: str = ""
    body: str = ""

    @property
    def instagram_url(self) -> str:
        if self.instagram.startswith("http"):
            return self.instagram
        handle = (self.instagram_handle or self.instagram).lstrip("@")
        return f"https://www.instagram.com/{handle}/" if handle else ""


def load_site_config(content_dir: Path) -> SiteConfig:
    path = Path(content_dir) / "site.md"
    if not path.is_file():
        return SiteConfig()

    fields, body = parse_fields(path.read_text(encoding="utf-8"))
    cfg = SiteConfig(body=body)
    for key in ("title", "tagline", "author", "instagram",
                "instagram_handle", "repo", "description"):
        value = fields.get(key)
        if isinstance(value, str) and value.strip():
            setattr(cfg, key, value.strip())
    return cfg
