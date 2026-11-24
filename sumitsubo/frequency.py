"""Word frequency, kept as a local file.

A frequency list is a table of words with a rank (1 = most frequent). It is used as the
lowest-priority tie-breaker when ordering cards and when choosing the context words of a kanji card.

Nothing here talks to a service while Sumitsubo runs. The list is a file: `sumitsubo setup` or
`sumitsubo frequency` downloads Jiten's global list once (https://jiten.moe/frequency-dictionaries,
CC BY-SA 4.0, built from about 19,000 analysed titles), and from then on it is read from disk.
Any other list works too, as a CSV file or as a frequency dictionary in Yomitan format.

Jiten itself (the site's code) is not bundled: what we use is the result of its analysis of a large
library of media, and that result is this file.
"""
import csv
import io
import json
import urllib.request
import zipfile
from pathlib import Path

from . import config, dictdata

JITEN_CSV = "https://api.jiten.moe/api/frequency-list/download?downloadType=csv"
LOCAL_NAME = "jiten-global-frequency.csv"
_TERM_HEADS = ("word", "term", "text", "expression", "kanji")


def parse_csv(text: str) -> list[tuple[str, int]]:
    """(term, rank) pairs from a frequency CSV. Jiten's columns are Word, Form (the reading), Rank.
    Other lists are accepted too: a header naming the term and rank columns, or no header at all,
    in which case the row order is the rank."""
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    if not rows:
        return []
    head = [h.strip().lower() for h in rows[0]]
    term_col = next((i for i, h in enumerate(head) if h in _TERM_HEADS), None)
    rank_col = next((i for i, h in enumerate(head) if "rank" in h), None)
    body = rows[1:] if term_col is not None else rows
    term_col = 0 if term_col is None else term_col
    best: dict[str, int] = {}
    for n, row in enumerate(body, 1):
        if len(row) <= term_col or not row[term_col].strip():
            continue
        term, rank = row[term_col].strip(), n
        if rank_col is not None and len(row) > rank_col and row[rank_col].strip().isdigit():
            rank = int(row[rank_col])
        if term not in best or rank < best[term]:
            best[term] = rank
    return list(best.items())


def parse_yomitan(path) -> tuple[str, list[tuple[str, int]]]:
    """(title, (term, rank) pairs) from a frequency dictionary in Yomitan format."""
    best: dict[str, int] = {}
    with zipfile.ZipFile(path) as z:
        try:
            title = json.load(z.open("index.json")).get("title") or Path(path).stem
        except KeyError:
            title = Path(path).stem
        for name in sorted(n for n in z.namelist() if n.startswith("term_meta_bank_") and n.endswith(".json")):
            for row in json.load(z.open(name)):
                if len(row) < 3 or row[1] != "freq":
                    continue
                v = row[2]
                if isinstance(v, dict) and "frequency" in v:      # {"reading": ..., "frequency": ...}
                    v = v["frequency"]
                if isinstance(v, dict):                           # {"value": 123, "displayValue": "123"}
                    v = v.get("value")
                if isinstance(v, str) and v.strip().isdigit():
                    v = int(v)
                if isinstance(v, (int, float)) and v > 0 and (row[0] not in best or v < best[row[0]]):
                    best[row[0]] = int(v)
    return title, list(best.items())


def load(path) -> dict:
    """Make the given file the frequency list in use. Returns its name and size."""
    path = Path(path)
    if path.suffix.lower() == ".zip":
        name, rows = parse_yomitan(path)
    else:
        name, rows = ("Jiten global" if "jiten" in path.name.lower() else path.stem), parse_csv(path.read_text(encoding="utf-8", errors="replace"))
    if not rows:
        raise ValueError(f"No frequency ranks found in {path.name}")
    return dict(name=name, ranks=dictdata.add_frequency(rows, name))


def download(dest: Path | None = None, timeout: int = 180) -> Path:
    """Fetch Jiten's global list once and keep it in the data folder."""
    dest = dest or config.data_dir() / "downloads" / LOCAL_NAME
    req = urllib.request.Request(JITEN_CSV, headers={"User-Agent": "sumitsubo"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    if not data.lstrip(b"\xef\xbb\xbf").startswith(b"Word,"):
        raise ValueError("The download is not the expected frequency list (it should start with Word,Form,Rank)")
    dest.write_bytes(data)
    return dest


def install(path: str | None = None, refresh: bool = False) -> dict:
    """Load a frequency list: the given file, else the copy already downloaded, else download it."""
    if path:
        return load(path)
    local = config.data_dir() / "downloads" / LOCAL_NAME
    if refresh or not local.exists():
        download(local)
    return load(local)


def current() -> dict:
    """What is loaded now: name and number of ranks (zero if none)."""
    if not dictdata.db_path().exists():
        return dict(name="", ranks=0)
    import sqlite3  # noqa: PLC0415
    con = sqlite3.connect(dictdata.db_path())
    try:
        r = con.execute("SELECT COALESCE(MAX(source),''), COUNT(*) FROM freq").fetchone()
    except sqlite3.OperationalError:
        r = ("", 0)
    con.close()
    return dict(name=r[0], ranks=r[1])
