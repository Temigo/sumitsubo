"""Paper sources: photographs of pages, read by character recognition.

A photo (from the phone, or dropped in from this computer) lands in the inbox. Each one is tidied,
read by Tesseract, and given a quality score; nothing is corrected by hand. Chosen pages, in a
chosen order, then become one source.

What is automatic:
  layout    A page is read as horizontal text; if Tesseract is not confident it is read again as
            vertical text, and the reading Tesseract is surer of is kept. Each page can be forced.
  pages     A page whose reading is poor (low confidence, or too few of its words are in the
            dictionary) is left out and listed so that it can be photographed again.
  sentences A sentence containing words Tesseract was unsure of is left out of a page that is
            otherwise fine. With enough pages, a word left out here turns up again elsewhere.

The thresholds below were set on synthetic pages, not on real photographs, and are provisional.
"""
import csv
import io
import json
import re
import shutil
import subprocess
from pathlib import Path

from . import config, db, ndl, ocr

LONG_SIDE = 3000        # pixels kept of a photo's longer side
RETRY_BELOW = 80        # horizontal reading less confident than this: try vertical as well
PAGE_CONF = 60          # a page read with less mean confidence than this is left out
PAGE_HIT = 0.70         # ... or with a smaller share of its words found in the dictionary
PAGE_CHARS = 30         # ... or with fewer Japanese characters
WORD_CONF = 30          # a sentence with a word below this is left out
SENT_CONF = 60          # ... or whose mean confidence is below this
# NDLOCR-Lite gives a confidence per line, and on a different scale: body text it reads correctly
# sits near 94, a line it gets wrong (a neighbouring page, a coloured label) below 75.
NDL = dict(page=75, word=75, sent=80)
TESS = dict(page=PAGE_CONF, word=WORD_CONF, sent=SENT_CONF)
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".tif", ".tiff", ".bmp", ".gif"}
LAYOUTS = {"auto": "automatic", "h": "horizontal", "v": "vertical"}
_JA = re.compile(r"[぀-ヿ㐀-鿿]")
_JAEDGE = r"぀-ヿ㐀-鿿＀-￯　-〿"
_END = "。！？!?」』）"


def folder() -> Path:
    d = config.data_dir() / "inbox"
    d.mkdir(parents=True, exist_ok=True)
    return d


def engine() -> str:
    return "ndl" if ndl.available() else "tesseract"


def missing() -> str | None:
    """None if page photos can be read, else what is missing."""
    if ndl.available():
        return None
    if not shutil.which("tesseract"):
        return "Tesseract is not installed"
    if "jpn" not in ocr._languages():
        return "Tesseract's Japanese model is not installed"
    return None


# ---- taking a file in ---------------------------------------------------------------------------
def _open(path: Path):
    from PIL import Image, ImageOps  # noqa: PLC0415
    if path.suffix.lower() in {".heic", ".heif"}:
        try:
            import pillow_heif  # noqa: PLC0415
            pillow_heif.register_heif_opener()
        except ImportError as e:
            raise RuntimeError("This is an iPhone HEIC photo; reading it needs: pip install pillow-heif") from e
    im = ImageOps.exif_transpose(Image.open(path))      # phones store the turn of the camera beside the picture
    return im.convert("RGB")


def _store(con, im, name: str) -> int:
    """Keep one page picture (and a small copy for the inbox) and queue it for reading."""
    im.thumbnail((LONG_SIDE, LONG_SIDE))
    pid = con.execute("INSERT INTO inbox(name, added, status, layout_set) VALUES(?,?,'waiting','auto')", (name, db.now())).lastrowid
    con.execute("UPDATE inbox SET ord=? WHERE id=?", (pid, pid))
    im.save(folder() / f"p{pid}.jpg", quality=88)
    small = im.copy()
    small.thumbnail((360, 360))
    small.save(folder() / f"p{pid}-small.jpg", quality=75)
    return pid


def take(con, path: Path, name: str | None = None) -> list[int]:
    """Put an uploaded photo, or every page of an uploaded PDF, into the inbox. Returns page ids."""
    name = name or path.name
    if path.suffix.lower() == ".pdf":
        import pypdfium2  # noqa: PLC0415
        pdf, out = pypdfium2.PdfDocument(str(path)), []
        try:
            for n in range(len(pdf)):
                page = pdf[n]
                w, h = page.get_size()
                im = page.render(scale=min(4.0, LONG_SIDE / max(w, h))).to_pil().convert("RGB")
                out.append(_store(con, im, f"{name} p.{n + 1}"))
        finally:
            pdf.close()
        return out
    if path.suffix.lower() not in IMAGE_EXT:
        raise RuntimeError(f"{name}: not a picture or a PDF")
    try:
        return [_store(con, _open(path), name)]
    except RuntimeError:
        raise
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"{name}: could not be opened as a picture ({type(e).__name__})") from e


def rotate(con, pid: int, quarter_turns: int = 1):
    from PIL import Image  # noqa: PLC0415
    for f in (folder() / f"p{pid}.jpg", folder() / f"p{pid}-small.jpg"):
        if f.exists():
            im = Image.open(f)
            im = im.rotate(-90 * quarter_turns, expand=True)
            im.save(f, quality=88)
    requeue(con, [pid])


def requeue(con, ids: list[int], layout: str | None = None):
    for pid in ids:
        if layout in LAYOUTS:
            con.execute("UPDATE inbox SET layout_set=? WHERE id=?", (layout, pid))
        con.execute("UPDATE inbox SET status='waiting', message=NULL WHERE id=?", (pid,))
        (folder() / f"p{pid}.json").unlink(missing_ok=True)


def delete(con, ids: list[int]):
    for pid in ids:
        for f in folder().glob(f"p{pid}[.-]*"):
            f.unlink()
        con.execute("DELETE FROM inbox WHERE id=?", (pid,))


# ---- reading one page ---------------------------------------------------------------------------
def _prepare(src: Path, dest: Path):
    """Even out the lighting of a photograph: a shadow across the page is what most often spoils
    recognition. The page is divided by a heavily blurred copy of itself, which leaves ink on white."""
    from PIL import Image, ImageChops, ImageFilter, ImageOps  # noqa: PLC0415
    im = Image.open(src).convert("L")
    small = im.resize((max(1, im.width // 16), max(1, im.height // 16))).filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.GaussianBlur(8))
    light = small.resize(im.size, Image.BILINEAR)
    flat = ImageChops.multiply(im, Image.new("L", im.size, 255))         # copy
    flat = Image.eval(ImageChops.subtract(light, flat), lambda v: 255 - min(255, v * 2))
    ImageOps.autocontrast(flat, cutoff=1).save(dest)


def _tesseract(image: Path, lang: str, psm: int) -> list[dict]:
    try:
        p = subprocess.run(["tesseract", str(image), "stdout", "-l", lang, "--psm", str(psm), "tsv"],
                           capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired as e:      # a picture with no text in it can keep Tesseract busy for minutes
        raise TimeoutError from e
    if p.returncode != 0:
        raise RuntimeError("Tesseract failed: " + " ".join((p.stderr or "").strip().splitlines()[-2:])[:300])
    rows, thick, side = [], {}, "width" if lang.endswith("_vert") else "height"
    for r in csv.DictReader(io.StringIO(p.stdout), delimiter="\t", quoting=csv.QUOTE_NONE):
        text = (r.get("text") or "").strip()
        try:
            conf, line = float(r["conf"]), (r["block_num"], r["par_num"], r["line_num"])
            if r["level"] == "4":
                thick[line] = int(r[side])
        except (KeyError, TypeError, ValueError):
            continue
        if text and conf >= 0:
            rows.append(dict(para=(r["block_num"], r["par_num"]), line=line, text=text, conf=conf))
    return _without_furigana(rows, thick)


def _without_furigana(rows: list[dict], thick: dict) -> list[dict]:
    """Drop lines much thinner than the page's usual line: furigana that Tesseract took for a line
    of its own, which would otherwise land as a run of kana in the middle of a sentence. (Furigana
    it merged into the main line cannot be removed this way; see the README.)"""
    sizes = sorted(thick[r["line"]] for r in rows for _ in r["text"] if r["line"] in thick)
    if len({r["line"] for r in rows}) < 3 or not sizes:
        return rows
    usual = sizes[len(sizes) // 2]                    # median over characters, so body text decides
    return [r for r in rows if thick.get(r["line"], usual) >= 0.55 * usual]


def _paragraphs(rows: list[dict]) -> list[list[list]]:
    """Recognised words grouped into paragraphs: [[text, confidence], ...] each. Wrapped lines are
    joined, and the spaces Tesseract puts between Japanese characters are dropped."""
    paras, key = [], None
    for r in rows:
        if r["para"] != key:
            paras.append([])
            key = r["para"]
        words = paras[-1]
        if words and not (re.match(rf"[{_JAEDGE}]", r["text"]) or re.search(rf"[{_JAEDGE}]$", words[-1][0])):
            words.append([" ", 100])
        words.append([r["text"], round(r["conf"])])
    return [p for p in paras if p]


def _measure(paras) -> tuple[float, int]:
    """(mean confidence weighted by length, Japanese characters)."""
    words = [(t, c) for p in paras for t, c in p if t.strip()]
    n = sum(len(t) for t, _ in words)
    return (sum(c * len(t) for t, c in words) / n if n else 0.0), sum(len(_JA.findall(t)) for t, _ in words)


def read(pid: int, layout: str = "auto") -> dict:
    """Recognise one page and cache the result. layout: auto, h or v."""
    src, work = folder() / f"p{pid}.jpg", folder() / f"p{pid}-work.png"
    if ndl.available():                    # finds vertical and horizontal lines by itself, on the photo as it is
        rows = ndl.read(src, config.data_dir() / "work" / f"page-{pid}")
        paras = _paragraphs(rows)
        kinds, alone = {}, []
        for r in rows:
            kinds.setdefault(r["para"], r["body"])
        alone = [i for i, body in enumerate(kinds.values()) if not body]      # titles, captions: never part of a running sentence
        conf, chars = _measure(paras)
        n_vert = sum(len(r["text"]) for r in rows if r["vertical"])
        res = dict(engine="ndl", layout="v" if 2 * n_vert > sum(len(r["text"]) for r in rows) else "h",
                   conf=round(conf, 1), chars=chars, paras=paras, alone=alone if len(kinds) == len(paras) else [])
        (folder() / f"p{pid}.json").write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
        return res
    _prepare(src, work)
    vert = "jpn_vert" in ocr._languages()
    tries = []
    try:
        if layout in ("auto", "h") or not vert:
            p = _paragraphs(_tesseract(work, "jpn", 3))
            tries.append(("h", p, *_measure(p)))
        if vert and (layout == "v" or (layout == "auto" and (tries[0][2] < RETRY_BELOW or tries[0][3] < PAGE_CHARS))):
            for psm in (5, 3):                        # 5: one block of columns; 3: several blocks (two tiers)
                p = _paragraphs(_tesseract(work, "jpn_vert", psm))
                tries.append(("v", p, *_measure(p)))
    except TimeoutError:
        tries = tries or [(layout if layout in ("h", "v") else "h", [], 0.0, 0)]     # judged on what there is: not a page of text
    finally:
        work.unlink(missing_ok=True)
    usable = [t for t in tries if t[3] >= PAGE_CHARS] or tries
    lay, paras, conf, chars = max(usable, key=lambda t: t[2])
    res = dict(engine="tesseract", layout=lay, conf=round(conf, 1), chars=chars, paras=paras)
    (folder() / f"p{pid}.json").write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
    return res


def stale(con, ids: list[int]) -> list[int]:
    """Pages read by Tesseract although NDLOCR-Lite is now installed."""
    if not ndl.available() or not ids:
        return []
    return [r[0] for r in con.execute(f"SELECT id FROM inbox WHERE COALESCE(engine,'tesseract')<>'ndl' AND status IN ('ok','poor','error') AND id IN ({','.join('?' * len(ids))})", ids)]


def cached(pid: int) -> dict | None:
    f = folder() / f"p{pid}.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None


def hit_rate(dic, parser, paras) -> float | None:
    """Share of the page's words that the dictionary knows: a second opinion beside Tesseract's own
    confidence. It is a weak one (misread text often still splits into real one-kanji words)."""
    texts = ["".join(t for t, _ in p) for p in paras]
    texts = [t for t in texts if _JA.search(t)]
    if not texts:
        return None
    forms = [t.form for toks in parser.parse(texts) for t in toks]
    if len(forms) < 5:
        return None
    known = dic.terms_exist(list(set(forms)))
    return sum(f in known for f in forms) / len(forms)


def judge(conf: float, chars: int, hit: float | None, eng: str = "tesseract") -> tuple[str, str]:
    """(ok or poor, why)."""
    if chars < PAGE_CHARS:
        return "poor", "almost no Japanese text was found"
    if conf < (NDL if eng == "ndl" else TESS)["page"]:
        return "poor", "the reading is too uncertain"
    if hit is not None and hit < PAGE_HIT:
        return "poor", "too many of its words are not real words"
    return "ok", ""


def process(pid: int, dic=None, parser=None):
    """Read one waiting page and record how it went. Errors are recorded on the page."""
    with db.session() as con:
        row = con.execute("SELECT * FROM inbox WHERE id=?", (pid,)).fetchone()
        if not row or row["status"] not in ("waiting", "reading"):
            return
        con.execute("UPDATE inbox SET status='reading' WHERE id=?", (pid,))
    try:
        m = missing()
        if m:
            raise RuntimeError(f"{m}. Run: {ocr.INSTALL}")
        res = read(pid, row["layout_set"] or "auto")
        hit = hit_rate(dic, parser, res["paras"]) if dic and parser else None
        status, why = judge(res["conf"], res["chars"], hit, res["engine"])
        with db.session() as con:
            con.execute("UPDATE inbox SET status=?, message=?, layout=?, conf=?, hit=?, chars=?, engine=? WHERE id=?",
                        (status, why, res["layout"], res["conf"], hit, res["chars"], res["engine"], pid))
    except Exception as e:  # noqa: BLE001
        with db.session() as con:
            con.execute("UPDATE inbox SET status='error', message=? WHERE id=?", (str(e)[:400], pid))


# ---- pages into sentences -----------------------------------------------------------------------
def _weighty(word: str) -> bool:
    """Whether a misreading here would matter: a kanji, katakana, or more than one character.
    Tesseract is often unsure of a lone hiragana or a full stop that it has nevertheless read
    correctly, and a wrong particle does not put a wrong word on a card."""
    return len(word) > 1 or bool(re.search(r"[\u30a0-\u30ff\u3400-\u9fff]", word))


def sentences(ids: list[int]) -> tuple[list[dict], dict]:
    """Sentences of the given pages, in that order, and what was left out. Text runs on from one
    paragraph or page into the next unless it stopped at the end of a sentence; a short line with
    no full stop (a heading, a running title, a page number) stands alone."""
    out, carry, carry_loc = [], [], ""
    stats = dict(pages=len(ids), read=0, left_out=[], doubtful=0, sentences=0, conf=0.0)
    confs = []

    limit = dict(TESS)

    def emit(chars, loc):
        text = "".join(c for c, _, _ in chars).strip()
        if len(text) < 2 or not _JA.search(text):
            return
        cs = [c for ch, c, _ in chars if ch.strip()]
        if (any(c < limit["word"] and w for _, c, w in chars) or sum(cs) / len(cs) < limit["sent"]
                or text[0] in "、。，．）」』"):
            stats["doubtful"] += 1
            return
        out.append(dict(text=text, location=loc))

    with db.session() as con:
        rows = {r["id"]: r for r in con.execute(f"SELECT * FROM inbox WHERE id IN ({','.join('?' * len(ids))})", ids)}
    for n, pid in enumerate(ids, 1):
        row, res = rows.get(pid), cached(pid)
        if not row or not res or row["status"] != "ok":
            stats["left_out"].append(dict(n=n, name=row["name"] if row else f"page {pid}", why=(row["message"] if row else "") or "not read"))
            continue
        stats["read"] += 1
        limit.update(NDL if res.get("engine") == "ndl" else TESS)
        stats["engine"] = res.get("engine", "tesseract")
        confs.append(res["conf"])
        loc = f"p.{n}"
        for i, words in enumerate(res["paras"]):
            para = [(ch, c, _weighty(t)) for t, c in words for ch in t]
            text = "".join(c for c, _, _ in para).strip()
            if not text:
                continue
            if i in res.get("alone", ()) or (len(text) < 20 and text[-1] not in _END):       # stands alone, and does not break a running sentence
                emit(para, loc)
                continue
            if carry:
                para, carry = carry + para, []
            spans = list(re.finditer(rf"[^。！？!?]+[{_END}]*", "".join(c for c, _, _ in para)))
            if spans and spans[-1].group().strip()[-1:] not in _END:
                last = spans.pop()                             # unfinished: its other half comes next
                carry, carry_loc = para[last.start():last.end()], loc
            for m in spans:
                emit(para[m.start():m.end()], loc)
    if carry:
        emit(carry, carry_loc)
    stats["sentences"] = len(out)
    stats["conf"] = round(sum(confs) / len(confs), 1) if confs else 0.0
    return out, stats


def text_of(pid: int) -> str:
    """The recognised text of one page, for a look: words Tesseract doubted are in ⟦brackets⟧."""
    res = cached(pid)
    if not res:
        return "(not read yet)"
    lim = (NDL if res.get("engine") == "ndl" else TESS)["word"]
    lines = [f"read by: {'NDLOCR-Lite' if res.get('engine') == 'ndl' else 'Tesseract'}   layout: {LAYOUTS.get(res['layout'], res['layout'])}   mean confidence: {res['conf']}   Japanese characters: {res['chars']}", ""]
    for p in res["paras"]:
        lines.append("".join(t if c >= lim or not _weighty(t) else f"⟦{t}⟧" for t, c in p))
        lines.append("")
    return "\n".join(lines)


# ---- the inbox ----------------------------------------------------------------------------------
def listing(con, source_id: int | None = None) -> list[dict]:
    q = "SELECT * FROM inbox WHERE source_id IS ? ORDER BY ord, id" if source_id is None else "SELECT * FROM inbox WHERE source_id=? ORDER BY ord, id"
    return [dict(r) | dict(layout_name=LAYOUTS.get(r["layout"] or "", "")) for r in con.execute(q, (source_id,))]


def make_source(con, ids: list[int], title: str = "", tags: str = "", series: str = "", into: int | None = None) -> int:
    """Turn the chosen pages, in this order, into a source, or add them to the end of an existing
    paper source. The pages leave the inbox but are kept, so the source can be read again."""
    ids = [i for i in ids if con.execute("SELECT 1 FROM inbox WHERE id=? AND source_id IS NULL", (i,)).fetchone()]
    if not ids:
        raise RuntimeError("No pages were chosen.")
    if into:
        src = con.execute("SELECT * FROM sources WHERE id=? AND kind='pages'", (into,)).fetchone()
        if not src:
            raise RuntimeError("That source is not made of page photos.")
        opts = json.loads(src["options"] or "{}")
        opts["pages"] = list(opts.get("pages", [])) + ids
        con.execute("UPDATE sources SET options=?, status='queued', message=NULL WHERE id=?", (json.dumps(opts), into))
        sid = into
    else:
        first = con.execute("SELECT name FROM inbox WHERE id=?", (ids[0],)).fetchone()["name"]
        sid = con.execute("INSERT INTO sources(kind, title, origin, series, status, added, options) VALUES('pages',?,?,?,?,?,?)",
                          (title.strip() or f"Pages from {Path(first).stem}", "page photos", series.strip(), "queued", db.now(),
                           json.dumps(dict(pages=ids)))).lastrowid
        db.set_tags(con, sid, tags)
    con.executemany("UPDATE inbox SET source_id=? WHERE id=?", [(sid, i) for i in ids])
    return sid
