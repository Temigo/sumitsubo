"""Reading scanned PDFs.

Anki Miner reads the text inside a PDF and refuses a scan, which has none. For those, Sumitsubo
runs OCRmyPDF (https://github.com/ocrmypdf/OCRmyPDF, MPL-2.0), which uses Tesseract's Japanese
models, and reads the recognised text itself. Both are optional installs:

    sudo apt install ocrmypdf tesseract-ocr-jpn tesseract-ocr-jpn-vert

Horizontal text is tried first. If that yields little Japanese, the page is read again as vertical
text and the better result is kept. Recognition makes mistakes; the source is marked as read by OCR.
"""
import importlib.util
import re
import shutil
import subprocess
import sys
from pathlib import Path

INSTALL = "sudo apt install ocrmypdf tesseract-ocr-jpn tesseract-ocr-jpn-vert"
_JA = r"぀-ヿ㐀-鿿＀-￯　-〿"
_HIRA = re.compile(r"[぀-ゟ]")
_END = "。！？!?」』）"


def _exe() -> list[str] | None:
    if importlib.util.find_spec("ocrmypdf"):
        return [sys.executable, "-m", "ocrmypdf"]
    found = shutil.which("ocrmypdf")
    return [found] if found else None


def _languages() -> set[str]:
    try:
        out = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    return {x.strip() for x in out.splitlines()[1:]}


def missing() -> str | None:
    """None if scans can be read, else what to install."""
    if not _exe():
        return "OCRmyPDF is not installed"
    langs = _languages()
    if "jpn" not in langs:
        return "Tesseract's Japanese model is not installed"
    return None


def status() -> str:
    m = missing()
    if m:
        return f"not available ({m}). Install with: {INSTALL}"
    return "ready" + ("" if "jpn_vert" in _languages() else ", horizontal text only (the vertical model jpn_vert is not installed)")


def _run(pdf: Path, work: Path, vertical: bool) -> str:
    work.mkdir(parents=True, exist_ok=True)
    side = work / ("ocr-vertical.txt" if vertical else "ocr-horizontal.txt")
    lang = ["-l", "jpn_vert", "--tesseract-pagesegmode", "5"] if vertical else ["-l", "jpn"]
    base = _exe() + lang + ["--force-ocr", "--deskew", "--sidecar", str(side)]
    p = subprocess.run(base + ["--output-type", "none", str(pdf), "-"], capture_output=True, text=True, timeout=6 * 3600)
    if p.returncode != 0 and not side.exists():       # older versions cannot skip writing the PDF
        p = subprocess.run(base + ["--output-type", "pdf", str(pdf), str(work / "ocr-out.pdf")],
                           capture_output=True, text=True, timeout=6 * 3600)
        (work / "ocr-out.pdf").unlink(missing_ok=True)
    if not side.exists():
        tail = " ".join((p.stderr or p.stdout or "").strip().splitlines()[-3:])
        raise RuntimeError(f"OCR failed: {tail[:400]}")
    return side.read_text(encoding="utf-8", errors="replace")


def clean(page: str) -> list[str]:
    """Paragraphs of one recognised page: wrapped lines joined, and the spaces Tesseract puts
    between Japanese characters removed."""
    out = []
    for block in re.split(r"\n\s*\n", page):
        text = "".join(line.strip() for line in block.splitlines())
        text = re.sub(rf"(?<=[{_JA}])[ \t]+(?=[{_JA}])", "", text)
        if text:
            out.append(text)
    return out


def sentences(raw: str) -> list[dict]:
    """Sentences with their page (p.N). A sentence cut by a page break is joined to its other half."""
    out, carry = [], ""
    for n, page in enumerate(raw.split("\f"), 1):
        paras = clean(page)
        for i, para in enumerate(paras):
            para, carry = carry + para, ""
            parts = re.findall(rf"[^。！？!?]+[{_END}]*", para)
            if i == len(paras) - 1 and parts and parts[-1][-1] not in _END:
                carry = parts.pop()                    # unfinished at the foot of the page
            out += [dict(text=s.strip(), location=f"p.{n}") for s in parts if len(s.strip()) > 1]
    if len(carry.strip()) > 1:
        out.append(dict(text=carry.strip(), location=out[-1]["location"] if out else "p.1"))
    return out


def _japanese(raw: str) -> tuple[int, float]:
    body = re.sub(r"\s", "", raw)
    hira = len(_HIRA.findall(body))
    return hira, hira / max(1, len(body))


def read_pdf(pdf: Path, work: Path, set_stage=lambda s: None) -> tuple[list[dict], str]:
    """(sentences, layout) for a scanned PDF; layout is "horizontal" or "vertical"."""
    m = missing()
    if m:
        raise RuntimeError(f"This PDF is a scan with no text inside, and reading scans needs an extra install ({m}). "
                           f"Run: {INSTALL}   then press Try again.")
    set_stage("reading the scan (OCR)")
    raw, layout = _run(pdf, work, vertical=False), "horizontal"
    hira, ratio = _japanese(raw)
    if (ratio < 0.2 or hira < 20) and "jpn_vert" in _languages():
        set_stage("reading the scan as vertical text (OCR)")
        try:
            alt = _run(pdf, work, vertical=True)
            if _japanese(alt)[0] > hira:
                raw, layout = alt, "vertical"
        except RuntimeError:
            pass
    return sentences(raw), layout
