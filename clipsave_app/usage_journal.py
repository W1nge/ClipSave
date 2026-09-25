"""Human-readable usage journal backing recency ordering.

Every time an item is reused (re-captured or copied from within ClipSave)
one plain-text line is appended under ``Data/Usage`` so the trail stays
visible and editable by the user. At startup the journal is parsed,
validated against the main database and loaded into an in-memory table
that provides recency ordering.

Line format, one usage event per line (local time with its UTC offset):

    2026-09-25 14:30:52.123456+0800 <- captured 2026-07-01T09:12:33Z hash=ab12cd34ef567890 会议纪要

The referenced item is identified by its original capture timestamp plus a
content hash prefix. A deleted item's capture timestamp never occurs again,
so stale lines simply expire instead of attaching to a different item.
Parsing is deliberately tolerant: malformed or truncated lines are skipped,
and lines written without an offset by older builds are interpreted in the
current local timezone.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

LINE_PATTERN = re.compile(
    r"^(?P<used>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{1,6}(?:[+-]\d{2}:?\d{2})?)"
    r" <- captured (?P<captured>.+?)"
    r" hash=(?P<hash>[0-9a-f]{16})"
    r"(?: (?P<title>.*))?$"
)
HASH_PREFIX_LENGTH = 16


def journal_file(directory: Path, local_moment: dt.datetime) -> Path:
    return directory / f"usage-{local_moment:%Y-%m}.md"


def append_usage(
    directory: Path,
    captured_at: str,
    content_hash: str,
    title: str,
    moment: dt.datetime,
) -> None:
    local_moment = moment.astimezone()
    directory.mkdir(parents=True, exist_ok=True)
    title_text = " ".join(str(title or "").split())[:60]
    suffix = f" {title_text}" if title_text else ""
    line = (
        f"{local_moment:%Y-%m-%d %H:%M:%S.%f}{local_moment:%z} <- captured {captured_at} "
        f"hash={content_hash[:HASH_PREFIX_LENGTH]}{suffix}"
    )
    with journal_file(directory, local_moment).open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def parse_usage(directory: Path) -> list[tuple[str, str, str]]:
    """Return (captured_at, hash_prefix, used_at_utc) for every valid line."""
    entries: list[tuple[str, str, str]] = []
    if not directory.is_dir():
        return entries
    for path in sorted(directory.glob("usage-*.md")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        for line in lines:
            match = LINE_PATTERN.match(line.strip())
            if match is None:
                continue
            try:
                used = dt.datetime.fromisoformat(match.group("used"))
                if used.tzinfo is None:
                    used = used.astimezone()
            except ValueError:
                continue
            used_utc = (
                used.astimezone(dt.timezone.utc)
                .isoformat(timespec="microseconds")
                .replace("+00:00", "Z")
            )
            entries.append((match.group("captured"), match.group("hash"), used_utc))
    return entries
