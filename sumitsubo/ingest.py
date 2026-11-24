"""Taking a source in: a video link, a document link, a local file, or pasted text.

Each source becomes a list of sentences, with start and end times when it has audio. The heavy
work is done by Anki Miner's API (see ankiminer.py); this module decides which route a source takes.
"""
import faulthandler
import json
import re
import shutil
import subprocess
import time
import traceback
import urllib.parse
import urllib.request
from pathlib import Path

from . import analyze, ankiminer, aozora, config, db, ocr, pages, quality

DOC_EXT = {".pdf", ".epub", ".txt", ".md", ".html", ".htm"}
SUB_EXT = {".srt", ".vtt", ".ass", ".ssa"}
AUDIO_EXT = {".mp3", ".m4a", ".wav", ".flac", ".ogg", ".opus", ".aac"}
VIDEO_EXT = {".mp4", ".mkv", ".webm", ".mov", ".avi"}
JAPANESE = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff]")
BOOK_EXT = {".epub", ".txt", ".md"}       # what a Sumitsubo Reader opens as a book
VIDEO_HOSTS = ("youtube.com", "youtu.be", "nicovideo.jp", "bilibili.com", "vimeo.com")


def is_url(s: str) -> bool:
    return bool(re.match(r"https?://", s or ""))


def classify(target: str) -> str:
    """video-url, aozora, doc-url, web-url, subtitle, media, doc, or text."""
    if is_url(target):
        if aozora.is_aozora(target):
            return "aozora"
        host = urllib.parse.urlparse(target).netloc.lower()
        ext = Path(urllib.parse.urlparse(target).path).suffix.lower()
        if any(h in host for h in VIDEO_HOSTS):
            return "video-url"
        return "doc-url" if ext in DOC_EXT - {".html", ".htm"} else "web-url"
    ext = Path(target).suffix.lower()
    return "subtitle" if ext in SUB_EXT else "media" if ext in AUDIO_EXT | VIDEO_EXT else "doc"


def add(con, target: str | None = None, text: str | None = None, title: str | None = None, tags: str = "",
        series: str = "", media: str | None = None, reader: bool = False) -> int:
    """Register a source for processing and return its id. Files are copied into the data folder."""
    data = config.data_dir()
    if text and not target:
        n = con.execute("SELECT COALESCE(MAX(id),0)+1 FROM sources").fetchone()[0]
        f = data / "media" / f"pasted-{n}.txt"
        f.write_text(text, encoding="utf-8")
        target, kind = str(f), "text"
        title = title or text.strip().splitlines()[0][:40]
    else:
        kind = classify(target)
        if not is_url(target):
            src = Path(target).expanduser()
            if not src.exists():
                raise FileNotFoundError(f"No such file: {target}")
            if data.resolve() not in src.resolve().parents:
                dest = data / "media" / src.name
                if dest.resolve() != src.resolve():
                    shutil.copy2(src, dest)
                target = str(dest)
        title = title or (Path(urllib.parse.urlparse(target).path).stem if is_url(target) else Path(target).stem) or target
    if media and not is_url(media):
        m = Path(media).expanduser()
        dest = data / "media" / m.name
        if dest.resolve() != m.resolve():
            shutil.copy2(m, dest)
        media = str(dest)
    sid = con.execute("INSERT INTO sources(kind, title, origin, series, status, added, media, options, to_reader) VALUES(?,?,?,?,?,?,?,?,?)",
                      (kind, title, target if is_url(target) else config.rel(target), series, "queued", db.now(),
                       config.rel(media) if media else None, json.dumps({}), int(bool(reader)))).lastrowid
    db.set_tags(con, sid, tags)
    return sid


# ---- turning a source into sentences -------------------------------------------------------------
def _split_plain(text: str) -> list[dict]:
    """Fallback sentence splitter, used only if Anki Miner's reader is unavailable."""
    out = []
    for para in text.splitlines():
        for s in re.findall(r"[^。！？!?]+[。！？!?」』）]*", para.strip()):
            if len(s.strip()) > 1:
                out.append(dict(text=s.strip(), location=""))
    return out


def read_subtitles(path: Path) -> list[dict]:
    import pysubs2  # noqa: PLC0415 - comes with Anki Miner
    out = []
    for ev in pysubs2.load(str(path)):
        t = re.sub(r"\s+", " ", ev.plaintext).strip()
        if t and not ev.is_comment:
            out.append(dict(text=t, start=ev.start / 1000, end=ev.end / 1000, location=_clock(ev.start / 1000)))
    return out


def _clock(sec: float) -> str:
    return f"{int(sec // 60)}:{int(sec % 60):02d}"


def _download(url: str, dest_dir: Path) -> Path:
    name = Path(urllib.parse.urlparse(url).path).name or "download"
    dest = dest_dir / name
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Sumitsubo)"})
    with urllib.request.urlopen(req, timeout=120) as r:
        dest.write_bytes(r.read())
    return dest


def sentences_for(src, set_stage=lambda s: None, dic=None, parser=None) -> tuple[list[dict], dict]:
    """Return (sentences, updates) for a source row. updates may set title, media."""
    data, work = config.data_dir(), config.data_dir() / "work" / f"source-{src['id']}"
    origin, updates = src["origin"], {}
    kind = src["kind"]
    if kind == "pages":                    # photographs of paper pages, already read one by one in the inbox
        set_stage("putting the pages together")
        opts = json.loads(src["options"] or "{}")
        with db.session() as con:
            again = pages.stale(con, opts.get("pages", []))
            pages.requeue(con, again)
        for n, pid in enumerate(again, 1):      # read by Tesseract before the better reader was installed
            set_stage(f"reading page {n} of {len(again)} again")
            pages.process(pid, dic, parser)
        units, stats = pages.sentences(opts.get("pages", []))
        if not units and stats["left_out"]:
            raise RuntimeError("None of these pages could be read well enough: " +
                               "; ".join(f"{p['name']} ({p['why']})" for p in stats["left_out"][:6]))
        return units, dict(options=json.dumps(opts | dict(ocr="pages", stats=stats)))
    if kind == "saved":                    # words saved in the reader's dictionary, with the sentences they were in
        with db.session() as con:
            book = origin[len("saved:"):] if origin.startswith("saved:") else None
            rows = con.execute("SELECT DISTINCT sentence, book FROM saved WHERE sentence<>''" + (" AND book=?" if book is not None else "") +
                               " ORDER BY taken, guid", (book,) if book is not None else ()).fetchall()
        return [dict(text=r["sentence"], location=(r["book"] or "")[:30]) for r in rows], updates
    if kind == "aozora":
        set_stage("downloading from Aozora Bunko")
        book = aozora.fetch(origin)
        units = aozora.sentences(book)
        if src["id"]:                          # not when only scoring
            set_stage("making the book for the reader")
            out = aozora.to_epub(book, data / "books" / f"{src['id']}" / aozora.file_name(book))
            updates["book"] = config.rel(out)
        if book.title and src["id"] and src["title"] == Path(urllib.parse.urlparse(origin).path).stem:
            updates["title"] = book.title + (f"（{book.author}）" if book.author else "")
        return units, updates
    if kind == "video-url":
        set_stage("downloading and transcribing")
        res = ankiminer.fetch(origin, work)
        updates = dict(media=config.rel(res["video_file"]), title=res.get("title"))
        return read_subtitles(Path(res["subtitle_file"])), updates
    if kind == "doc-url":
        set_stage("downloading")
        path = _download(origin, data / "media")
    elif kind == "web-url":
        path = origin                      # Anki Miner's reader fetches the article text itself
    else:
        path = config.resolve(origin)
    if kind == "subtitle":
        return read_subtitles(path), updates
    if kind == "media":
        set_stage("transcribing")
        sub = ankiminer.transcribe(path, work)
        updates = dict(media=config.rel(path))
        return (read_subtitles(Path(sub)) if sub else []), updates
    set_stage("reading")
    is_pdf = isinstance(path, Path) and path.suffix.lower() == ".pdf"
    if isinstance(path, Path) and path.suffix.lower() in BOOK_EXT and src["id"]:
        updates["book"] = config.rel(path)       # a reader device opens this file as it is
    try:
        units, info = ankiminer.units(path, work)
    except ankiminer.AnkiMinerError as e:
        if isinstance(path, Path) and path.suffix.lower() in {".txt", ".md"}:
            return _split_plain(path.read_text(encoding="utf-8", errors="replace")), updates
        if not (is_pdf and "text layer" in str(e)):
            raise
        units, info = [], {}                # a scan: Anki Miner reads only the text inside a PDF
    if is_pdf and not any(JAPANESE.search(u["text"]) for u in units):
        units, layout = ocr.read_pdf(path, work, set_stage)
        updates["options"] = json.dumps(dict(ocr=layout))
    if info.get("title") and info["title"] != Path(str(path)).stem:
        updates["title"] = info["title"]
    return units, updates


_JA = "\u3040-\u30ff\u3400-\u9fff\uff00-\uffef\u3000-\u303f"


def tidy(text: str) -> str:
    """Japanese is written without spaces. A PDF's text often has one between every character (each
    glyph was placed separately), which splits words for the parser and spreads the sentence out on
    a card. Spaces between Japanese characters are removed; a full stop typed as "." after Japanese
    becomes "。"."""
    text = re.sub(rf"(?<=[{_JA}])[\s\u200b]+(?=[{_JA}])", "", text.strip())      # any kind of space: plain, thin, no-break
    return re.sub(rf"(?<=[{_JA}])\s*\.$", "。", text)


SPACED = re.compile(rf"(?:[{_JA}][\s\u200b]+){{4}}[{_JA}]")      # five Japanese characters in a row, each followed by a space


def requeue_spaced(con) -> list[int]:
    """Sources read before 0.4.2 whose text still has a space between every character: their words
    were split into single kanji, so they are queued to be read again. Returns their ids."""
    ids = []
    for s in con.execute("SELECT id FROM sources WHERE status='done' AND kind<>'deck'").fetchall():
        rows = con.execute("SELECT text FROM sentences WHERE source_id=? LIMIT 200", (s["id"],)).fetchall()
        if rows and sum(bool(SPACED.search(r["text"])) for r in rows) * 5 >= len(rows):
            con.execute("UPDATE sources SET status='queued', message=NULL WHERE id=?", (s["id"],))
            ids.append(s["id"])
    return ids


# ---- clips -------------------------------------------------------------------------------------
def cut_clips(con, source_id: int | None = None, tell=None) -> int:
    """Cut an audio clip for every sentence in a card's pool that has audio. Idempotent."""
    rows = con.execute(
        """SELECT DISTINCT s.id, s.start, s.end, src.media FROM card_sentences c JOIN sentences s ON s.id=c.sentence_id
           JOIN sources src ON src.id=s.source_id
           WHERE s.clip IS NULL AND s.start IS NOT NULL AND src.media IS NOT NULL""" +
        (" AND src.id=?" if source_id else ""), (source_id,) if source_id else ()).fetchall()
    n = 0
    for k, r in enumerate(rows):
        if tell:
            tell(k, len(rows))
        media, out = config.resolve(r["media"]), config.data_dir() / "clips" / f"s{r['id']}.mp3"
        if not media or not media.exists():
            continue
        start, dur = max(0.0, r["start"] - 0.15), (r["end"] - r["start"]) + 0.35
        p = subprocess.run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}",
                            "-i", str(media), "-vn", "-ac", "1", "-b:a", "64k", str(out)], capture_output=True)
        if p.returncode == 0 and out.exists():
            con.execute("UPDATE sentences SET clip=? WHERE id=?", (config.rel(out), r["id"]))
            n += 1
    return n


# ---- the whole job -------------------------------------------------------------------------------
# How far each source being read is: {source id: dict(step, steps, stage, done, total, unit)}. Kept
# in memory, not in the database, so it can be updated while the database is being written.
progress: dict[int, dict] = {}
STEPS = ("getting the text", "analysing", "storing the words", "making cards", "cutting audio clips")


QUIET = 120         # seconds without any progress after which the program writes down what it is doing
_stuck = {}


def stuck_log() -> Path:
    return config.data_dir() / "stuck.log"


def _watch(note: str | None):
    """Arm (or, with None, disarm) the watch on the work in progress. If nothing reports progress for
    QUIET seconds, where every thread of the program is at that moment is written to stuck.log, and
    again every QUIET seconds. It works even when the program is too busy to answer the browser."""
    try:
        if note is None:
            faulthandler.cancel_dump_traceback_later()
            return
        f = _stuck.get("file")
        if f is None or f.closed:
            p = stuck_log()
            if p.exists() and p.stat().st_size > 1_000_000:
                p.unlink()
            f = _stuck["file"] = open(p, "a", encoding="utf-8")       # kept open: the watch writes to it from outside Python
        if note != _stuck.get("note"):
            _stuck["note"] = note
            f.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] {note}\n")
            f.flush()
        faulthandler.dump_traceback_later(QUIET, repeat=True, file=f)
    except Exception:  # noqa: BLE001 - a watch that cannot be set must not stop the work
        pass


def steps_for(src) -> int:
    """Four steps, or five for a source with a recording."""
    return 5 if src["kind"] in ("video-url", "media") or src["media"] else 4


class _Counted:
    """A parser that works through a long text in pieces and says how far it is. The source is
    parsed for its score and again for its words; the second time the answer is already here."""
    PIECE = 150

    def __init__(self, parser, tell, after=None):
        self.parser, self.tell, self.after, self.last = parser, tell, after, None

    def parse(self, texts: list[str]):
        if self.last and self.last[0] == texts:
            return self.last[1]
        out = []
        for i in range(0, len(texts), self.PIECE):
            out += self.parser.parse(texts[i:i + self.PIECE])
            self.tell("analysing", min(i + self.PIECE, len(texts)), len(texts))
        if self.after:
            out = self.after(out)               # the edition's own readings, where it gives them
        self.last = (list(texts), out)
        return out


def process(source_id: int, dic, parser) -> None:
    """Process one queued source from start to finish. Errors are recorded on the source, not raised."""
    state = progress[source_id] = dict(step=1, steps=4, stage="", done=0, total=0, unit="", at=time.time())

    def stage(text, done=0, total=0, step=None, unit="sentences"):
        """Say where the work is. A new stage is also written to the database; counts within one are not."""
        new = text != state["stage"]
        state.update(stage=text, done=done, total=total, unit=unit, step=step or state["step"], at=time.time())
        if state["step"] > 1:                    # step 1 may rightly be silent for long: a download, speech recognition
            _watch(f"source {source_id}, step {state['step']} of {state['steps']}: {text}")
        if new:
            with db.session() as c:
                c.execute("UPDATE sources SET status='processing', stage=? WHERE id=?", (text, source_id))
    try:
        with db.session() as con:
            src = con.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        state["steps"] = steps_for(src)
        stage("starting")
        sentences, updates = sentences_for(src, stage, dic, parser)
        raw = src["kind"] not in ("saved", "aozora")          # an edition, or a sentence you chose yourself, is taken as it is
        for s in sentences:
            s["text"] = tidy(s["text"])
            if raw and not s.get("ruby"):
                s["text"] = quality.clean(s["text"])
        sentences = [s for s in sentences if 1 < len(s["text"]) <= 400 and JAPANESE.search(s["text"])]
        if not sentences:
            raise RuntimeError("No Japanese text was found in this source.")
        stage("analysing", 0, len(sentences), step=2)
        parser = _Counted(parser, stage, aozora.Readings(dic, sentences).apply if any(s.get("ruby") for s in sentences) else None)
        parser.parse([s["text"] for s in sentences])       # the long part, done before the database is held; kept for the two uses below
        stage("storing the words", 0, len(sentences), step=3)
        with db.session() as con:
            for k, v in updates.items():
                if v:
                    con.execute(f"UPDATE sources SET {k}=? WHERE id=?", (v, source_id))
            before = analyze.score_text(con, dic, parser, [s["text"] for s in sentences])
            analyze.remember_score(con, source_id, before)
            analyze.analyze_source(con, dic, parser, source_id, sentences, lambda d, n: stage("storing the words", d, n))
            if raw:                                         # debris, reference lists, labels: counted, but never a card's sentence
                kept = {r[0] for r in con.execute("SELECT text FROM kept WHERE source_id=?", (source_id,))}
                for text in {s["text"] for s in sentences} - kept:
                    why = quality.poor(text)
                    if why:
                        con.execute("INSERT OR IGNORE INTO rejected(source_id, text, why) VALUES(?,?,?)", (source_id, text, why))
        stage("making cards", step=4)
        with db.session() as con:
            analyze.rebuild_cards(con, lambda d, n: stage("making cards", d, n, unit=""))
        if state["steps"] == 5:
            stage("cutting audio clips", step=5)
        with db.session() as con:
            cut_clips(con, tell=(lambda d, n: stage("cutting audio clips", d, n, unit="clips")) if state["steps"] == 5 else None)
            con.execute("UPDATE sources SET status='done', stage=NULL, message=NULL WHERE id=?", (source_id,))
    except Exception as e:  # noqa: BLE001 - one bad source must not stop the queue
        traceback.print_exc()
        msg = (str(e) if type(e) is RuntimeError else f"{type(e).__name__}: {e}")[:600]
        if "speech model" in msg:
            msg = ("This source needs speech recognition, and the speech model is not installed yet. "
                   "Install it on the Settings page (about 3 GB), then press Try again.")
        with db.session() as con:
            con.execute("UPDATE sources SET status='error', stage=NULL, message=? WHERE id=?", (msg, source_id))
    finally:
        _watch(None)
        progress.pop(source_id, None)


def delete_source(con, source_id: int):
    for r in con.execute("SELECT clip FROM sentences WHERE source_id=? AND clip IS NOT NULL", (source_id,)):
        p = config.resolve(r["clip"])
        if p and p.exists():
            p.unlink()
    pages.delete(con, [r[0] for r in con.execute("SELECT id FROM inbox WHERE source_id=?", (source_id,)).fetchall()])
    made = config.data_dir() / "books" / str(source_id)         # an EPUB made here; a file you added stays in media
    if made.is_dir():
        shutil.rmtree(made)
    for t in ("occ", "sentences", "unverified", "source_tags", "rejected", "kept"):
        con.execute(f"DELETE FROM {t} WHERE source_id=?", (source_id,))
    con.execute("DELETE FROM cards WHERE source_id=?", (source_id,))
    con.execute("DELETE FROM sources WHERE id=?", (source_id,))
    # Items first met in the deleted source now belong to the earliest source that still has them.
    con.execute("UPDATE items SET first_source=(SELECT MIN(source_id) FROM occ WHERE item_id=items.id) "
                "WHERE kind='word' AND first_source=?", (source_id,))
    con.execute("""UPDATE items SET first_source=(SELECT MIN(o.source_id) FROM word_kanji wk JOIN occ o ON o.item_id=wk.word_id
                   WHERE wk.kanji_id=items.id) WHERE kind='kanji' AND first_source=?""", (source_id,))
    analyze.rebuild_cards(con)
