"""Where Sumitsubo keeps its data, and the handful of settings it has.

Everything lives in one folder (SUMITSUBO_DATA, or ~/.local/share/sumitsubo) so it can be copied
to another machine as it is.
"""
import os
from pathlib import Path

VERSION = "0.1.0"
PACK_FORMAT = 2      # 2: a card carries its starting memory state and a list of sentences

DEFAULTS = {
    "level": "3",            # JLPT level assumed known: 5 (N5) .. 1 (N1); 0 = assume nothing
    "daily_budget": "15",    # new items released per day (enforced by the device; kept here as the default)
    "gloss_dict": "jmdict-en",
    "whisper_model": "large-v3",
    "contrast_when": "mixup",  # when a contrast card appears: mixup, all, most, one, never (decided on the device)
}


def data_dir() -> Path:
    d = Path(os.environ.get("SUMITSUBO_DATA") or Path.home() / ".local/share/sumitsubo")
    for sub in ("", "downloads", "glossaries", "media", "clips", "work", "packs", "export"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    return d


def rel(path: Path) -> str:
    """Path as stored in the database: relative to the data folder when inside it."""
    try:
        return str(Path(path).resolve().relative_to(data_dir().resolve()))
    except ValueError:
        return str(path)


def resolve(stored: str | None) -> Path | None:
    if not stored:
        return None
    p = Path(stored)
    return p if p.is_absolute() else data_dir() / p
