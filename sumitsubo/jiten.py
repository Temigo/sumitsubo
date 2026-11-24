"""Optional use of Jiten (jiten.moe), a free and open-source site for Japanese immersion learners.

Two things are taken from it, both through its public API (https://jiten.moe/guides/using-the-api):
  - its frequency list, used as the lowest-priority tie-breaker when ranking cards and when
    choosing the context words of a kanji card;
  - a title's vocabulary coverage, to compare our difficulty score against (later).

NOT YET TESTED AGAINST THE LIVE SERVICE: the API guide does not document response formats, and
the machine this was written on could not reach jiten.moe. The CSV reader below is deliberately
forgiving about column names. Jiten's data is licensed CC BY-SA 4.0.
"""
import csv
import io
import json
import urllib.parse
import urllib.request

from . import dictdata

BASE = "https://api.jiten.moe"


def _get(path: str, params: dict | None = None, api_key: str | None = None, timeout: int = 120) -> bytes:
    url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    headers = {"User-Agent": "sumitsubo"}
    if api_key:
        headers["X-Api-Key"] = api_key
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as r:
        return r.read()


def parse_frequency_csv(text: str) -> list[tuple[str, int]]:
    """(term, rank) pairs from a frequency CSV. Uses a header if there is one, else column order."""
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    if not rows:
        return []
    head = [h.strip().lower() for h in rows[0]]
    term_col = next((i for i, h in enumerate(head) if h in ("word", "term", "text", "expression", "kanji")), None)
    rank_col = next((i for i, h in enumerate(head) if "rank" in h), None)
    body = rows[1:] if term_col is not None else rows
    term_col = 0 if term_col is None else term_col
    out, seen = [], set()
    for n, row in enumerate(body, 1):
        if len(row) <= term_col or not row[term_col].strip() or row[term_col] in seen:
            continue
        seen.add(row[term_col])
        rank = n
        if rank_col is not None and len(row) > rank_col and row[rank_col].strip().isdigit():
            rank = int(row[rank_col])
        out.append((row[term_col].strip(), rank))
    return out


def import_frequency(api_key: str | None = None, path: str | None = None) -> int:
    """Load Jiten's global frequency list, from a downloaded CSV file or straight from the API."""
    text = open(path, encoding="utf-8").read() if path else \
        _get("/api/frequency-list/download", {"downloadType": "csv"}, api_key).decode("utf-8", "replace")
    return dictdata.add_frequency(parse_frequency_csv(text), "jiten")


def parse(text: str, api_key: str | None = None) -> dict:
    """Jiten's own word breakdown of a short text (for comparison with ours)."""
    return json.loads(_get("/api/vocabulary/parse", {"text": text}, api_key))


def title_detail(deck_id: int, api_key: str | None = None) -> dict:
    """A library title's statistics; includes personal coverage when an API key is given."""
    return json.loads(_get(f"/api/media-deck/{deck_id}/detail", None, api_key))
