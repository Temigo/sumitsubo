"""NDLOCR-Lite, the National Diet Library's OCR for Japanese books and magazines, as the reader
for page photos.

https://github.com/ndl-lab/ndlocr-lite  (CC BY 4.0, National Diet Library; its layout model is
also under the DINOv3 licence). It finds the blocks of text on a page, tells body text from
titles, furigana, page numbers and pictures, reads vertical and horizontal lines alike, and gives
each line a confidence. It runs on an ordinary processor, a few seconds a page.

It is not bundled: `sumitsubo setup` (or `sumitsubo paper`, or the button on the Inbox page) fetches
a pinned version, about 160 MB with its models, into the data folder. Sumitsubo runs it as a separate program and
reads the XML it writes. Without it, pages are read by Tesseract, which is much weaker on books.
"""
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from . import config

REPO = "https://github.com/ndl-lab/ndlocr-lite"
PINNED = "a25d4564b4a8884fd9430d1eb416a2ce8d4d910b"      # version 1.4.0, November 2026
SKIP = {"図版", "ノンブル", "柱", "ルビ"}                  # pictures, page numbers, running titles, furigana
_JAEDGE = r"぀-ヿ㐀-鿿＀-￯　-〿"


def home() -> Path:
    return config.data_dir() / "engines" / "ndlocr-lite"


def available() -> bool:
    src = home() / "src"
    if not (src / "ocr.py").exists() or not any((src / "model").glob("*.onnx")):
        return False
    try:
        import cv2  # noqa: F401, PLC0415
        import onnxruntime  # noqa: F401, PLC0415
    except ImportError:
        return False
    return True


def install(progress=lambda *a: None) -> None:
    """Fetch the pinned version from GitHub into the data folder."""
    if not shutil.which("git"):
        raise RuntimeError("git is needed to fetch NDLOCR-Lite: sudo apt install git")
    dest = home()
    if (dest / "src" / "ocr.py").exists():
        return
    tmp = dest.with_name("ndlocr-lite-fetching")
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    progress("Downloading NDLOCR-Lite, the reader for photographed pages (about 160 MB)")
    for cmd in (["git", "init", "-q"], ["git", "remote", "add", "origin", REPO],
                ["git", "fetch", "-q", "--depth", "1", "origin", PINNED], ["git", "checkout", "-q", "FETCH_HEAD"]):
        p = subprocess.run(cmd, cwd=tmp, capture_output=True, text=True, timeout=3600)
        if p.returncode != 0:
            raise RuntimeError("Could not fetch NDLOCR-Lite: " + (p.stderr or p.stdout).strip()[-300:])
    shutil.rmtree(tmp / ".git", ignore_errors=True)
    tmp.rename(dest)
    if not available():
        raise RuntimeError("NDLOCR-Lite was fetched but two libraries it needs are missing. Run: pip install -e .")


def read(image: Path, work: Path) -> list[dict]:
    """Lines of one page in reading order: dicts with para (its block), text, conf (0-100), vertical."""
    work.mkdir(parents=True, exist_ok=True)
    try:
        p = subprocess.run([sys.executable, "ocr.py", "--sourceimg", str(image.resolve()), "--output", str(work.resolve())],
                           cwd=home() / "src", capture_output=True, text=True, timeout=600)
        xml = work / (image.stem + ".xml")
        if not xml.exists():
            raise RuntimeError("NDLOCR-Lite failed: " + " ".join((p.stderr or p.stdout or "").strip().splitlines()[-2:])[:300])
        root = ET.parse(xml).getroot()
    finally:
        shutil.rmtree(work, ignore_errors=True)
    rows = []

    def line(el, block):
        text = re.sub(rf"(?<=[{_JAEDGE}])\s+(?=[{_JAEDGE}])", "", (el.get("STRING") or "").strip())
        if not text or el.get("TYPE") in SKIP:
            return
        try:
            w, h = float(el.get("WIDTH") or 0), float(el.get("HEIGHT") or 0)
            x, y = float(el.get("X") or 0), float(el.get("Y") or 0)
            body = el.get("TYPE") == "本文"         # titles and captions are set in display type and misread more: ask more of them
            rows.append(dict(para=block, order=float(el.get("ORDER") or 0), text=text, conf=100 * float(el.get("CONF") or 0) - (0 if body else 10),
                             vertical=h > w, body=body, cx=x + w / 2, cy=y + h / 2, w=w, h=h))
        except ValueError:
            pass

    for page in root.iter("PAGE"):
        for n, child in enumerate(page):
            if child.tag == "TEXTBLOCK":
                for el in child.iter("LINE"):
                    line(el, f"b{n}")
            elif child.tag == "LINE":
                line(child, f"l{n}")
    first, blocks = {}, {}
    for r in rows:
        first[r["para"]] = min(first.get(r["para"], 1e9), r["order"])
        blocks.setdefault(r["para"], []).append(r)
    out = []
    for key in sorted(blocks, key=first.get):                    # blocks in the reading order NDLOCR-Lite gives
        out += _in_columns(blocks[key])
    return out


def _in_columns(lines: list[dict]) -> list[dict]:
    """Lines of one block in the order they are read, worked out from where they are on the page.
    A column that is printed in two pieces (a coloured label such as 【歴史】, then the text under
    it) comes out of the recogniser as two lines, not always in the right order; here they are put
    back one after the other. A short piece of such a column that was read with little confidence
    is the label, misread, and is dropped so that it does not cost the sentence that follows it."""
    if len(lines) < 2:
        return lines
    vertical = sum(r["vertical"] for r in lines) * 2 > len(lines)
    across, along, thick, length = ("cx", "cy", "w", "h") if vertical else ("cy", "cx", "h", "w")

    def overlap(a, b):                       # how much two lines run side by side, as a share of the shorter
        lo = max(a[along] - a[length] / 2, b[along] - b[length] / 2)
        hi = min(a[along] + a[length] / 2, b[along] + b[length] / 2)
        return max(0.0, hi - lo) / max(1.0, min(a[length], b[length]))

    columns = []
    for r in sorted(lines, key=lambda r: -r[length]):            # full columns first, then the pieces find theirs
        fits = [(min(abs(r[across] - m[across]) for m in col), col) for col in columns
                if all(overlap(r, m) < 0.2 for m in col)]
        fits = [(d, col) for d, col in fits if d < 0.6 * max(r[thick], max(m[thick] for m in col))]
        if fits:
            min(fits, key=lambda f: f[0])[1].append(r)
        else:
            columns.append([r])
    columns.sort(key=lambda col: (-1 if vertical else 1) * max(col, key=lambda m: m[length])[across])
    out = []
    for col in columns:
        col.sort(key=lambda r: r[along])
        if len(col) > 1:
            col = [r for r in col if not (r["conf"] < 75 and len(r["text"]) <= 5)] or col
        out += col
    return out
