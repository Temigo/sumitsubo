"""The browser interface: add sources, watch the queue, browse the library and its cards, settings."""
import json
import os
import queue
import re
import secrets
import shutil
import socket
import threading
import time
import urllib.parse
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, Query, Request, UploadFile
import segno
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import analyze, ankiminer, cards, config, contrast, db, dictdata, frequency, imports, ingest, memory, ndl, ocr, pages

@asynccontextmanager
async def lifespan(_app):
    db.init()
    with db.session() as con:
        stale = memory.migrate(con)              # data from before 0.2.0
        if stale or (con.execute("SELECT 1 FROM cards WHERE kind<>'kanji' LIMIT 1").fetchone()
                     and not con.execute("SELECT 1 FROM card_sentences LIMIT 1").fetchone()):   # or before sentence pools (0.2.1)
            analyze.rebuild_cards(con)
            ingest.cut_clips(con)
        if db.setting(con, "spaces_checked") != "1":          # text from some PDFs, read before 0.4.2
            ingest.requeue_spaced(con)
            db.set_setting(con, "spaces_checked", "1")
        if db.setting(con, "quality_sweep") != "1":          # once: the sentences read before 0.8.1 are checked as new ones are
            from . import quality  # noqa: PLC0415
            found = quality.sweep(con)
            if found["set_aside"] or found["cleaned"]:
                analyze.rebuild_cards(con)
            db.set_setting(con, "quality_sweep", "1")
        pending = [r[0] for r in con.execute("SELECT id FROM sources WHERE status IN ('queued','processing') ORDER BY id")]
        waiting = [r[0] for r in con.execute("SELECT id FROM inbox WHERE status IN ('waiting','reading') ORDER BY id")]
    for pid in waiting:                 # page photos first: a source made of them needs them read
        _jobs.put(("page", pid))
    for sid in pending:                 # anything interrupted last time is picked up again
        _jobs.put(sid)
    if not _state.get("worker"):
        _state["worker"] = threading.Thread(target=_worker, daemon=True)
        _state["worker"].start()
    yield


app = FastAPI(title="Sumitsubo", lifespan=lifespan)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
_jobs: "queue.Queue" = queue.Queue()     # source ids, and ("page", id) for page photos
_state: dict = {}
LEVELS = [("0", "Assume nothing"), ("5", "N5"), ("4", "N4"), ("3", "N3"), ("2", "N2"), ("1", "N1")]


def engine():
    """Dictionary and parser, created on first use (loading them takes a moment)."""
    if "parser" not in _state:
        with db.session() as con:
            gloss = db.setting(con, "gloss_dict")
        from .parser import Parser  # noqa: PLC0415
        _state["dic"] = dictdata.Dictionary(gloss_dict=gloss)
        _state["parser"] = Parser(_state["dic"])
        with db.session() as con:
            if db.setting(con, "imports_stale") == "1":       # imports made before 0.2.0: carry their states over
                imports.reapply_all(con, _state["dic"])
            if db.setting(con, "colloc_backfilled") != "1":   # collocations among what was collected before 0.3.0
                analyze.backfill_collocations(con, _state["dic"])
                analyze.rebuild_cards(con)
            if db.setting(con, "registers_backfilled") != "1":   # register labels, new in 0.3.1
                analyze.backfill_registers(con, _state["dic"])
                analyze.rebuild_cards(con)
    return _state["dic"], _state["parser"]


def reset_engine():
    _state.pop("parser", None)
    _state.pop("dic", None)


def _worker():
    while True:
        sid = _jobs.get()
        if isinstance(sid, tuple):          # ("page", id): read one page photo in the inbox
            try:
                dic, parser = engine() if dictdata.db_path().exists() else (None, None)
            except Exception:  # noqa: BLE001
                dic = parser = None
            pages.process(sid[1], dic, parser)
            continue
        try:
            dic, parser = engine()
            ingest.process(sid, dic, parser)
        except Exception as e:  # noqa: BLE001
            with db.session() as con:
                con.execute("UPDATE sources SET status='error', message=? WHERE id=?", (str(e)[:600], sid))


def _save_upload(up: UploadFile | None, folder: str) -> Path | None:
    """Keep an uploaded file in the data folder (copied in pieces, so a long recording is fine)."""
    if up is None or not up.filename:
        return None
    dest = config.data_dir() / folder / Path(up.filename).name
    with open(dest, "wb") as f:
        shutil.copyfileobj(up.file, f, 1024 * 1024)
    return dest


def start_job(title: str, work) -> RedirectResponse:
    """Run something slow in the background and send the browser to a page that shows its progress.
    work(progress) does the job, calling progress(stage, done, total) as it goes, and returns the
    address to go to when it is finished."""
    job = dict(id=uuid.uuid4().hex[:8], title=title, stage="Starting", done=0, total=0, state="running", error=None, url=None, started=time.time())

    def progress(stage, done=0, total=0):
        job.update(stage=stage, done=done, total=total)

    def run():
        try:
            job["url"] = work(progress)
            job["state"] = "done"
        except Exception as e:  # noqa: BLE001
            job.update(state="error", error=str(e))
    _state["job"] = job
    threading.Thread(target=run, daemon=True).start()
    return RedirectResponse("/job", status_code=303)


templates.env.filters["num"] = lambda n: f"{int(n or 0):,}"
KINDS = {"aozora": "Book", "doc": "Document", "doc-url": "Document", "web-url": "Article", "video-url": "Video", "media": "Recording",
         "subtitle": "Talk", "pages": "Page photos", "text": "Text", "deck": "Import", "saved": "Reader"}
templates.env.filters["kindname"] = lambda k: KINDS.get(k, k or "")


def _day(iso) -> str:
    """2026-12-19 as 19 Dec, with the year when it is not this one."""
    try:
        d = time.strptime(str(iso), "%Y-%m-%d")
    except (TypeError, ValueError):
        return str(iso or "")
    return f"{d.tm_mday} {time.strftime('%b', d)}" + (f" {d.tm_year}" if d.tm_year != time.localtime().tm_year else "")


templates.env.filters["day"] = _day


def _nav() -> dict:
    """The counts beside the entries of the section sidebar, and when the reader last synced."""
    try:
        with db.session() as con:
            one = lambda q: con.execute(q).fetchone()[0]  # noqa: E731
            last = json.loads(db.setting(con, "last_sync") or "null")
            return dict(sources=one("SELECT COUNT(*) FROM sources"), cards=f'{one("SELECT COUNT(*) FROM cards"):,}',
                        books=one("SELECT COUNT(*) FROM sources WHERE to_reader=1 AND book IS NOT NULL"),
                        imports=len(imports.listing(con)), synced=(last or {}).get("at"))
    except Exception:  # noqa: BLE001 - the sidebar must never stop a page
        return {}


def page(request: Request, name: str, **ctx):
    ctx.setdefault("ready", dictdata.db_path().exists())
    ctx.setdefault("nav", _nav())
    return templates.TemplateResponse(request, name, ctx)


def _pager(rows: int, p: int, link: str, per: int | None = None) -> dict:
    per = per or PER_PAGE
    pages = max(1, -(-rows // per))
    p = max(1, min(p, pages))
    return dict(page=p, pages=pages, rows=rows, first=(p - 1) * per + 1 if rows else 0, last=min(rows, p * per), link=link, start=(p - 1) * per)


def back(request: Request, default: str = "/"):
    return RedirectResponse(request.headers.get("referer") or default, status_code=303)


def _library(con) -> list[dict]:
    out, when = [], analyze.projection(con)
    for s in con.execute("SELECT * FROM sources ORDER BY id DESC").fetchall():
        d = dict(s) | dict(tags=db.tags_of(con, s["id"]), cov=None, ncards={}) | _progress(s["id"])
        if s["status"] == "done":
            d["cov"] = analyze.coverage(con, s["id"])
            d["ncards"] = {r[0]: r[1] for r in con.execute(
                """SELECT c.kind, COUNT(*) FROM cards c JOIN items i ON i.id=c.item_id
                   WHERE c.source_id=? AND i.band IN ('new','young') GROUP BY c.kind""", (s["id"],))}
        d["added_cov"] = analyze.normalise_score(json.loads(s["added_score"])) if s["added_score"] else None
        d["when"] = when.get(s["id"])
        out.append(d)
    return out


@app.get("/job", response_class=HTMLResponse)
def job_page(request: Request):
    job = _state.get("job")
    if not job:
        return RedirectResponse("/", status_code=303)
    return page(request, "job.html", job=job)


@app.get("/job/status")
def job_status():
    job = _state.get("job") or dict(state="none")
    return JSONResponse({k: v for k, v in job.items() if k != "started"} | dict(seconds=round(time.time() - job.get("started", time.time()))))


# ---- pages ---------------------------------------------------------------------------------------
SOURCES_PER_PAGE = 25


@app.get("/", response_class=HTMLResponse)
def index(request: Request, q: str = "", tag: str = "", order: str = "readable", sel: int = 0, p: int = 1):
    """Library, Sources: the chosen source on top, then one line a source."""
    with db.session() as con:
        lib = _library(con)
        tags = con.execute("SELECT t.name, COUNT(*) n FROM tags t JOIN source_tags st ON st.tag_id=t.id GROUP BY t.id ORDER BY n DESC, t.name").fetchall()
    if q.strip():
        lib = [s for s in lib if q.strip().lower() in (s["title"] or "").lower()]
    if tag:
        lib = [s for s in lib if any(t == tag or t.startswith(tag + "/") for t in s["tags"])]
    done = [s for s in lib if s["status"] == "done"]
    rest = [s for s in lib if s["status"] != "done"]                      # being read, or failed: always in sight, at the top
    if order == "new":
        done.sort(key=lambda s: -s["id"])
    elif order == "study":
        done.sort(key=lambda s: (s["priority"] or 2, s["id"]))
    else:
        order = "readable"
        done.sort(key=lambda s: -(s["cov"]["readable"] if s["cov"] else 0))
    link = "?" + urllib.parse.urlencode({k: v for k, v in dict(q=q, tag=tag, order=order).items() if v and v != "readable"})
    pager = _pager(len(done), p, link, SOURCES_PER_PAGE)
    shown = done[pager["start"]:pager["start"] + SOURCES_PER_PAGE]
    chosen = next((s for s in done if s["id"] == sel), None) or (shown[0] if shown else None)
    return page(request, "index.html", sources=shown, rest=rest, chosen=chosen, tags=tags, q=q, tag=tag, order=order, pager=pager,
                busy=any(s["status"] in ("queued", "processing") for s in lib))


@app.get("/add", response_class=HTMLResponse)
def add_page(request: Request, way: str = "link"):
    return page(request, "add.html", score=None, again=None, way=way if way in ("link", "file", "text") else "link")


@app.post("/add")
async def add(request: Request, target: str = Form(""), text: str = Form(""), title: str = Form(""), tags: str = Form(""),
              series: str = Form(""), upload: UploadFile | None = File(None), media: UploadFile | None = File(None),
              action: str = Form("add"), reader: str = Form(""), kept_media: str = Form("")):
    up_path, media_path = _save_upload(upload, "media"), _save_upload(media, "media")
    media_path = str(media_path) if media_path else None
    if not media_path and kept_media and Path(kept_media).resolve().is_relative_to(config.data_dir().resolve()):
        media_path = kept_media                 # the recording sent with a difficulty check, now being added
    target = (str(up_path) if up_path else target).strip()
    if action == "score":                     # "should I read this?" without adding it
        dic, parser = engine()
        if text.strip() and not target:
            sentences = [s["text"] for s in ingest._split_plain(text)]
            label = text.strip()[:30]
        else:
            fake = dict(id=0, origin=target if ingest.is_url(target) else config.rel(target), kind=ingest.classify(target))
            sentences = [s["text"] for s in ingest.sentences_for(fake)[0]]
            label = Path(urllib.parse.urlparse(target).path).name or target
        with db.session() as con:
            score = analyze.score_text(con, dic, parser, sentences) | dict(label=label)
        return page(request, "add.html", score=score, way="text" if text.strip() and not target else "link",
                    again=dict(target=target, text=text if not target else "", title=title, tags=tags, series=series, reader=reader, media=media_path or ""))
    if not target and not text.strip():
        return back(request)
    with db.session() as con:
        sid = ingest.add(con, target or None, text=text if not target else None, title=title.strip() or None,
                         tags=tags, series=series.strip(), media=media_path, reader=bool(reader))
    _jobs.put(sid)
    return RedirectResponse("/", status_code=303)


@app.post("/sync")
def sync(where: str = Form("")):
    """Exchange with the reader device, from the Library page: the same as `sumitsubo sync`."""
    from . import device  # noqa: PLC0415
    where = where.strip() or "http://sumitsubo.local"

    def work(progress):
        said = []

        def say(line):
            said.append(line)
            progress(line)
        progress("Looking for the device")
        try:
            with db.session() as con:
                db.set_setting(con, "device_address", where)
                if where.startswith("http"):
                    device.sync_wifi(con, where, say, engine)
                    said.append("The device has the new deck. Leave File Transfer and open Study.")
                else:
                    card = Path(where).expanduser()
                    if not card.is_dir():
                        raise RuntimeError(f"There is no folder {card}.")
                    device.sync_folder(con, card, say, engine)
                    said.append(f"Wrote the deck to {card / 'sumitsubo'}.")
        except OSError as e:
            if not where.startswith("http"):
                raise
            raise RuntimeError(f"No device answered at {where} ({getattr(e, 'reason', e)}). Put the reader on File Transfer, on the same "
                               "network as this computer, and check the address it shows.") from e
        with db.session() as con:
            db.set_setting(con, "last_sync", json.dumps(dict(lines=said, at=time.strftime("%d %b %H:%M").lstrip("0"), where=where)))
        return "/reader"
    return start_job("Syncing with the reader", work)


@app.get("/status")
def status():
    with db.session() as con:
        rows = [dict(r) | _progress(r["id"]) for r in con.execute("SELECT id, status, stage, message FROM sources ORDER BY id")]
    return JSONResponse(rows)


PER_PAGE = 50       # rows of a source's cards, words or sentences on one page


def _progress(sid: int) -> dict:
    """Where a source being read is: step 2 of 4, and how far within it."""
    p = ingest.progress.get(sid) or {}
    quiet = int(time.time() - p["at"]) if p.get("step", 0) > 1 else 0       # seconds since it last reported anything
    return dict(step=p.get("step", 0), steps=p.get("steps", 0), done=p.get("done", 0), total=p.get("total", 0), unit=p.get("unit", ""),
                quiet=quiet if quiet >= 60 else 0)


@app.get("/source/{sid}", response_class=HTMLResponse)
def source(request: Request, sid: int, kind: str = "", show: str = "cards", all: int = 0, p: int = 1):
    with db.session() as con:
        s = con.execute("SELECT * FROM sources WHERE id=?", (sid,)).fetchone()
        if not s:
            return RedirectResponse("/", status_code=303)
        cov = analyze.coverage(con, sid) if s["status"] == "done" else None
        counts = {r[0]: r[1] for r in con.execute(
            """SELECT c.kind, COUNT(*) FROM cards c JOIN items i ON i.id=c.item_id
               WHERE c.source_id=? AND (? OR i.band IN ('new','young')) GROUP BY c.kind""", (sid, all))}
        n_mature = con.execute("SELECT COUNT(*) FROM cards c JOIN items i ON i.id=c.item_id WHERE c.source_id=? AND i.band='mature'", (sid,)).fetchone()[0]
        rows = con.execute("SELECT COUNT(*) FROM sentences WHERE source_id=?", (sid,)).fetchone()[0] if show == "text" else \
            counts.get(kind, 0) if kind else sum(counts.values())
        if show in ("cards", "text"):
            p = min(p, max(1, -(-rows // PER_PAGE)))    # past the end: the last page
        start = (max(1, p) - 1) * PER_PAGE              # a long book has thousands of rows: one page at a time
        cs = cards.listing(con, source_id=sid, kind=kind or None, bands=None if all else cards.TO_LEARN, limit=PER_PAGE, offset=start) if show == "cards" else []
        sentences = con.execute("SELECT * FROM sentences WHERE source_id=? ORDER BY idx LIMIT ? OFFSET ?", (sid, PER_PAGE, start)).fetchall() if show == "text" else []
        stack = con.execute(
            """SELECT i.id, i.lemma, i.reading, i.gloss, SUM(o.n) n, src.title FROM occ o JOIN items i ON i.id=o.item_id
               LEFT JOIN sources src ON src.id=i.first_source
               WHERE o.source_id=? AND i.band IN ('new','young') AND i.first_source<>? GROUP BY i.id ORDER BY n DESC""", (sid, sid)).fetchall() if show == "stack" else []
        known = con.execute(
            """SELECT i.id, i.lemma, i.reading, i.gloss, i.level, m.origin, m.stability, m.last_review, m.due, m.state, m.difficulty, SUM(o.n) n
               FROM occ o JOIN items i ON i.id=o.item_id JOIN memory m ON m.item_id=i.id AND m.skill=i.kind
               WHERE o.source_id=? AND i.band='mature' GROUP BY i.id ORDER BY m.origin, i.level, n DESC""", (sid,)).fetchall() if show == "known" else []
        if show in ("stack", "known"):
            rows = len(stack) + len(known)
            stack, known = stack[start:start + PER_PAGE], known[start:start + PER_PAGE]
        known = [dict(w) | dict(m=memory.describe(w)) for w in known]
        unv = con.execute("SELECT form, n FROM unverified WHERE source_id=? ORDER BY n DESC", (sid,)).fetchall()
        tabs = dict(cards=sum(counts.values()), text=s["n_sentences"] or 0, unknown=len(unv),
                    known=con.execute("SELECT COUNT(DISTINCT o.item_id) FROM occ o JOIN items i ON i.id=o.item_id WHERE o.source_id=? AND i.kind='word' AND i.band='mature'", (sid,)).fetchone()[0],
                    stack=con.execute("SELECT COUNT(DISTINCT o.item_id) FROM occ o JOIN items i ON i.id=o.item_id WHERE o.source_id=? AND i.band IN ('new','young') AND i.first_source<>?", (sid, sid)).fetchone()[0])
        tags = db.tags_of(con, sid)
        when = analyze.projection(con).get(sid) if s["status"] == "done" else None
        order = json.loads(s["options"] or "{}").get("pages", [])
        photos = sorted(pages.listing(con, sid), key=lambda r: order.index(r["id"]) if r["id"] in order else 10**6)
        photos = [r | dict(n=order.index(r["id"]) + 1 if r["id"] in order else 0) for r in photos]
    return page(request, "source.html", photos=photos, s=s, cov=cov, added=analyze.normalise_score(json.loads(s["added_score"])) if s["added_score"] else None,
                all=all, n_mature=n_mature, when=when, priorities=analyze.PRIORITY, tabs=tabs,
                pager=dict(page=max(1, p), pages=max(1, -(-rows // PER_PAGE)), rows=rows, first=start + 1, last=min(rows, start + PER_PAGE),
                           link=f"?show={show}" + (f"&kind={kind}" if kind else "") + ("&all=1" if all else "")),
                cards=cs, counts=counts, kind=kind, show=show, sentences=sentences, stack=stack, known=known,
                unverified=unv, tags=tags, ocr=json.loads(s["options"] or "{}").get("ocr"), paper=json.loads(s["options"] or "{}").get("stats"))


@app.post("/source/{sid}/reader")
def source_reader(sid: int, on: str = Form("")):
    with db.session() as con:
        con.execute("UPDATE sources SET to_reader=? WHERE id=?", (int(bool(on)), sid))
    return RedirectResponse(f"/source/{sid}", status_code=303)


@app.get("/source/{sid}/book")
def source_book(sid: int):
    with db.session() as con:
        s = con.execute("SELECT book FROM sources WHERE id=?", (sid,)).fetchone()
    p = config.resolve(s["book"]) if s and s["book"] else None
    if not p or not p.exists():
        return RedirectResponse(f"/source/{sid}", status_code=303)
    return FileResponse(p, filename=p.name)


MARKED = (("ignored", "Suspended"), ("known", "Marked known"), ("new", "Marked to learn"), ("sentences", "Bad sentences"), ("auto", "Grammar words"))


def _card_counts(con) -> dict:
    by_band = {r[0]: r[1] for r in con.execute("SELECT i.band, COUNT(*) FROM cards c JOIN items i ON i.id=c.item_id GROUP BY i.band")}
    by_kind = {r[0]: r[1] for r in con.execute("SELECT kind, COUNT(*) FROM cards GROUP BY kind")}
    return dict(learn=by_band.get("new", 0) + by_band.get("young", 0), new=by_band.get("new", 0), young=by_band.get("young", 0), mature=by_band.get("mature", 0),
                suspended=con.execute("SELECT COUNT(*) FROM items WHERE status='ignored' AND id IN (SELECT item_id FROM marks)").fetchone()[0],
                contrast=sum(contrast.counts(con).values())) | by_kind


def _marked(con, mark: str, p: int) -> dict:
    """What was decided by hand, one kind at a time, each with its way back."""
    by = {r[0]: r[1] for r in con.execute("SELECT mark, COUNT(*) FROM marks GROUP BY mark")}
    n = dict(ignored=by.get("ignored", 0), known=by.get("known", 0), new=by.get("new", 0),
             sentences=con.execute("SELECT COUNT(*) FROM rejected").fetchone()[0],
             auto=con.execute("SELECT COUNT(*) FROM items WHERE status='ignored' AND id NOT IN (SELECT item_id FROM marks)").fetchone()[0])
    mark = mark if mark in n else "ignored"
    pager = _pager(n[mark], p, f"?state=suspended&mark={mark}")
    if mark == "sentences":
        rows = con.execute("""SELECT r.source_id, r.text, r.why, src.title FROM rejected r LEFT JOIN sources src ON src.id=r.source_id
                              ORDER BY r.source_id, r.text LIMIT ? OFFSET ?""", (PER_PAGE, pager["start"])).fetchall()
    elif mark == "auto":
        rows = con.execute("""SELECT i.id, i.kind, i.lemma, i.reading, i.gloss, '' AS title FROM items i
                              WHERE i.status='ignored' AND i.id NOT IN (SELECT item_id FROM marks) ORDER BY i.id DESC LIMIT ? OFFSET ?""", (PER_PAGE, pager["start"])).fetchall()
    else:
        rows = con.execute("""SELECT i.id, i.kind, i.lemma, i.reading, i.gloss, src.title FROM items i JOIN marks k ON k.item_id=i.id
                              LEFT JOIN sources src ON src.id=i.first_source WHERE k.mark=? ORDER BY k.ts DESC LIMIT ? OFFSET ?""", (mark, PER_PAGE, pager["start"])).fetchall()
    return dict(mark=mark, rows=rows, n=n, pager=pager, was=dict(ignored="ignored", auto="ignored", known="known", new="learning").get(mark, ""))


@app.get("/cards", response_class=HTMLResponse)
def all_cards(request: Request, kind: str = "", tag: str = "", source: int = 0, note: str = "", state: str = "learn", p: int = 1, q: str = "", sel: int = 0,
              ck: str = "", mark: str = ""):
    """Library, Cards: the chosen card on top (its word, and every sentence it can be shown with), then one line a card.
    Contrast cards and what was suspended or marked by hand are views of the same page."""
    with db.session() as con:
        counts = _card_counts(con)
        sources = con.execute("SELECT s.id, s.title FROM sources s WHERE s.status='done' ORDER BY s.title").fetchall()
        base = dict(sources=sources, tag=tag, source=source, note=note, q=q, counts=counts, cards=[], chosen=None, link="", marked=None, contrasts=None)
        if state == "suspended":
            got = _marked(con, mark, p)
            return page(request, "cards.html", **base | dict(kind="", state=state, marked=got, pager=got["pager"], marks=MARKED))
        if kind == "contrast":
            cc = contrast.counts(con)
            pager = _pager(cc.get(ck, 0) if ck else sum(cc.values()), p, "?kind=contrast" + ("&ck=" + ck if ck else ""))
            return page(request, "cards.html", **base | dict(kind=kind, state="", pager=pager, ck=ck, ckinds=contrast.KINDS, ccounts=cc,
                        contrasts=contrast.listing(con, ck or None, PER_PAGE, pager["start"]), components=bool(dictdata.similar_kanji())))
        bands = dict(learn=cards.TO_LEARN, new=("new",), young=("young",), mature=("mature",)).get(state)
        want = dict(source_id=source or None, kind=kind or None, tag=tag or None, bands=bands, q=q.strip() or None)
        rows = cards.listing(con, limit=None, **want)
        link = "?" + urllib.parse.urlencode({k: v for k, v in dict(source=source, tag=tag, kind=kind, state=state, q=q).items() if v})
        pager = _pager(rows, p, link)
        cs = cards.listing(con, limit=PER_PAGE, offset=pager["start"], **want)
        one = con.execute("SELECT * FROM cards WHERE id=?", (sel,)).fetchone() if sel else None
        chosen = cards.view(con, one) if one else (cs[0] if cs else None)
    return page(request, "cards.html", **base | dict(cards=cs, chosen=chosen, kind=kind, state=state, pager=pager, link=link))


@app.get("/export", response_class=HTMLResponse)
def export_page(request: Request, source: int = 0, tag: str = ""):
    with db.session() as con:
        tags = [r[0] for r in con.execute("SELECT name FROM tags ORDER BY name")]
        sources = con.execute("SELECT s.id, s.title, (SELECT COUNT(*) FROM cards c WHERE c.source_id=s.id) n FROM sources s WHERE s.status='done' ORDER BY s.id").fetchall()
    return page(request, "export.html", sources=sources, source=source, tag=tag,
                export_tags=sorted({"/".join(t.split("/")[:i]) for t in tags for i in range(1, t.count("/") + 2)}))


@app.get("/clip/{sentence_id}")
def clip(sentence_id: int):
    with db.session() as con:
        r = con.execute("SELECT clip FROM sentences WHERE id=?", (sentence_id,)).fetchone()
    p = config.resolve(r["clip"]) if r else None
    if not p or not p.exists():
        return HTMLResponse("No audio for this sentence", status_code=404)
    return FileResponse(p, media_type="audio/mpeg")


# ---- actions -------------------------------------------------------------------------------------
@app.post("/item/{item_id}/status")
def item_status(request: Request, item_id: int, status: str = Form(...)):
    with db.session() as con:
        if status in ("known", "ignored", "learning", "undo"):   # undo takes the mark back: imports and the level decide again
            analyze.set_status(con, item_id, status)
            analyze.rebuild_cards(con)
        ingest.cut_clips(con)
    return JSONResponse(dict(ok=True)) if request.headers.get("x-requested-with") else back(request)


@app.post("/sentence/{sentence_id}/reject")
def sentence_reject(request: Request, sentence_id: int):
    """Mark a sentence as bad: its cards move to another sentence, or go if there is none."""
    with db.session() as con:
        res = analyze.reject_sentence(con, sentence_id)
        ingest.cut_clips(con)
    return JSONResponse(res) if request.headers.get("x-requested-with") else back(request)


@app.post("/sentence/restore")
def sentence_restore(request: Request, source_id: int = Form(...), text: str = Form(...)):
    with db.session() as con:
        analyze.restore_sentence(con, source_id, text)
        ingest.cut_clips(con)
    return back(request, "/cards?state=suspended&mark=sentences")


@app.get("/aside")
def aside():
    return RedirectResponse("/cards?state=suspended", status_code=303)


@app.post("/source/{sid}/edit")
def source_edit(request: Request, sid: int, title: str = Form(""), tags: str = Form(""), series: str = Form(""), priority: int = Form(2)):
    with db.session() as con:
        con.execute("UPDATE sources SET priority=? WHERE id=?", (priority if priority in analyze.PRIORITY else 2, sid))
        if title.strip():
            con.execute("UPDATE sources SET title=? WHERE id=?", (title.strip(), sid))
        con.execute("UPDATE sources SET series=? WHERE id=?", (series.strip(), sid))
        db.set_tags(con, sid, tags)
    return back(request)


@app.post("/source/{sid}/delete")
def source_delete(sid: int):
    with db.session() as con:
        ingest.delete_source(con, sid)
    return RedirectResponse("/", status_code=303)


@app.post("/source/{sid}/retry")
def source_retry(sid: int):
    with db.session() as con:
        con.execute("UPDATE sources SET status='queued', message=NULL WHERE id=?", (sid,))
    _jobs.put(sid)
    return RedirectResponse("/", status_code=303)


@app.get("/export/anki")
def export_anki(source: list[int] = Query(default=[]), tag: list[str] = Query(default=[]),
                kind: list[str] = Query(default=[]), deck: str = "Sumitsubo", layout: str = "split", mature: int = 0, progress: int = 0):
    """An Anki deck of the chosen sources and tags (nothing chosen = everything)."""
    source, tag = [x for x in source if x], [t for t in tag if t.strip()]
    kind = [k for k in kind if k in ("word", "listen", "kanji", "colloc")]
    deck = deck.strip() or "Sumitsubo"
    name = re.sub(r"[^\w.-]+", "-", deck, flags=re.UNICODE).strip("-").lower() or "sumitsubo"
    if len(source) == 1 and not tag and deck == "Sumitsubo":
        name += f"-source-{source[0]}"
    dest = config.data_dir() / "export" / f"{name}.apkg"
    with db.session() as con:
        res = cards.export_anki(con, dest, source, tag, kind, deck, layout != "one", bool(mature), bool(progress))
    if not res["notes"]:
        return RedirectResponse("/cards?note=" + urllib.parse.quote("Nothing to export: no cards match that choice."), status_code=303)
    return FileResponse(dest, filename=dest.name, media_type="application/octet-stream")


@app.post("/export/pack")
def export_pack(request: Request, source: int = Form(0)):
    with db.session() as con:
        ids = [source] if source else [r[0] for r in con.execute("SELECT id FROM sources WHERE status='done'")]
        for sid in ids:
            cards.write_pack(con, sid)
        cards.write_shared(con)
    return RedirectResponse(f"/settings?note=Wrote {len(ids)} pack(s), the settings and the contrast cards to {config.data_dir() / 'packs'}", status_code=303)


@app.post("/source/{sid}/priority")
def source_priority(request: Request, sid: int, priority: int = Form(2)):
    with db.session() as con:
        con.execute("UPDATE sources SET priority=? WHERE id=?", (priority if priority in analyze.PRIORITY else 2, sid))
    return back(request)


@app.get("/contrast")
def contrast_page(kind: str = "", p: int = 1):
    return RedirectResponse("/cards?kind=contrast" + (f"&ck={kind}" if kind else "") + (f"&p={p}" if p > 1 else ""), status_code=303)


@app.post("/contrast/when")
def contrast_when(request: Request, when: str = Form("mixup")):
    with db.session() as con:
        if when in dict(contrast.WHEN):
            db.set_setting(con, "contrast_when", when)
    return back(request, "/cards?kind=contrast")


# ---- imports: what you already know from Anki, Renshuu or a list -----------------------------------
def _note(text: str, to: str = "/imports"):
    return RedirectResponse(to + "?note=" + urllib.parse.quote(text), status_code=303)


@app.get("/imports", response_class=HTMLResponse)
def imports_page(request: Request, note: str = ""):
    with db.session() as con:
        return page(request, "imports.html", batches=imports.listing(con), totals=imports.summary(con), note=note,
                    modes=dict(imports.MODES))


@app.post("/imports/add")
def imports_add(upload: UploadFile | None = File(None), renshuu_key: str = Form(""), action: str = Form("file"),
                sentences: int = Form(0)):
    """Take a snapshot from a deck file, a list, Renshuu or a running Anki, then show what is in it.
    This can take a while (a fetch from Renshuu, a long review history), so it runs in the
    background with its progress on screen."""
    key, up = renshuu_key.strip(), None
    if not (action == "ankiconnect" or (action == "renshuu" and key)):
        imports.folder()
        up = _save_upload(upload, "imports")
        if up is None:
            return _note("Choose a file first." if action != "renshuu" else "Paste your Renshuu key, or choose a saved export file.")

    def work(progress):
        try:
            if action == "ankiconnect":
                b = imports.read_ankiconnect(progress=progress)
            elif up is None:
                b = imports.read_renshuu(key, sentences=max(0, min(300, sentences)), progress=progress)   # the key is used for this fetch and not stored
            else:
                progress("Reading the file")
                try:
                    b = imports.read_file(up)
                finally:
                    up.unlink(missing_ok=True)                     # the snapshot is what is kept
            with db.session() as con:
                bid = imports.save_batch(con, b, progress)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"Could not read that: {e}") from e
        return f"/imports/{bid}"
    return start_job({"ankiconnect": "Reading from Anki", "renshuu": "Fetching from Renshuu"}.get(action if up is None else "", "Reading the file"), work)


@app.get("/imports/{bid}", response_class=HTMLResponse)
def imports_review(request: Request, bid: int, note: str = ""):
    with db.session() as con:
        if not con.execute("SELECT 1 FROM import_batches WHERE id=?", (bid,)).fetchone():
            return RedirectResponse("/imports", status_code=303)
        return page(request, "import.html", p=imports.preview(con, bid), modes=imports.MODES, note=note)


@app.post("/imports/{bid}")
async def imports_save(request: Request, bid: int):
    """Save the field choices, thresholds and each deck's choice; apply them unless only recounting."""
    form = await request.form()
    with db.session() as con:
        p = imports.preview(con, bid)
        mapping = {}
        for i, nt in enumerate(p["samples"]):
            pick = lambda key: (int(form[f"map-{i}-{key}"]) if form.get(f"map-{i}-{key}", "") != "" else None)  # noqa: E731
            mapping[nt] = dict(word=pick("word"), reading=pick("reading"), sentence=pick("sentence"))
        modes = {deck: form.get(f"mode-{j}", "progress") for j, deck in enumerate(sorted(p["decks"]))}
        imports.configure(con, bid, mapping, modes)
        if form.get("action") != "apply":
            return RedirectResponse(f"/imports/{bid}", status_code=303)

    def work(progress):
        progress("Loading the dictionary")
        dic, _ = engine()
        with db.session() as con:
            t = imports.apply(con, dic, bid, progress)
        msg = (f"Imported the progress of {t['studied']} words and kanji ({t['mature']} mature), with {t['reviews']} past reviews. "
               f"{t['added']} cards were brought in to study here.")
        if t["not_found"]:
            msg += f" {t['not_found']} words were not in the dictionary and were left out."
        return "/imports?note=" + urllib.parse.quote(msg)
    return start_job("Importing", work)


@app.post("/imports/{bid}/delete")
def imports_delete(bid: int):
    with db.session() as con:
        imports.remove(con, bid)
    return _note("Import removed. Its words are back to what your level assumes.")


# ---- settings ------------------------------------------------------------------------------------
# ---- paper pages: the phone, the inbox ---------------------------------------------------------
LOCAL = {"127.0.0.1", "::1", "localhost", "testclient"}


def phone_token(con) -> str:
    tok = db.setting(con, "phone_token")
    if not tok:
        tok = secrets.token_urlsafe(6)
        db.set_setting(con, "phone_token", tok)
    return tok


def lan_address() -> str | None:
    """This computer's address on the local network (no packet is sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def phone_link(con) -> str | None:
    if not os.environ.get("SUMITSUBO_PHONE"):
        return None
    ip = lan_address()
    return f"http://{ip}:{os.environ.get('SUMITSUBO_PORT', '8378')}/phone/{phone_token(con)}" if ip else None


@app.middleware("http")
async def only_the_upload_page_from_outside(request: Request, call_next):
    """With `serve --phone` the program listens on the local network. Another device may then reach
    the upload page, with the right code in its address, and nothing else."""
    host = request.client.host if request.client else ""
    if host not in LOCAL and not os.environ.get("SUMITSUBO_OPEN"):
        parts = request.url.path.split("/")
        with db.session() as con:
            tok = phone_token(con)
        if not (os.environ.get("SUMITSUBO_PHONE") and len(parts) >= 3 and parts[1] == "phone" and secrets.compare_digest(parts[2], tok)):
            return HTMLResponse("<meta name=viewport content='width=device-width'><p style='font:18px system-ui;margin:2rem'>"
                                "To send pages from this phone, scan the code shown on the computer under <b>Inbox</b>.</p>", status_code=403)
    return await call_next(request)


async def _take_uploads(files: list[UploadFile]) -> tuple[list[int], list[str]]:
    ids, errors = [], []
    for up in files:
        if not up.filename:
            continue
        tmp = config.data_dir() / "work" / f"upload-{uuid.uuid4().hex[:8]}{Path(up.filename).suffix.lower()}"
        with open(tmp, "wb") as f:
            shutil.copyfileobj(up.file, f, 1024 * 1024)
        try:
            with db.session() as con:
                new = pages.take(con, tmp, Path(up.filename).name)
            ids += new
        except Exception as e:  # noqa: BLE001
            errors.append(str(e)[:200])
        finally:
            tmp.unlink(missing_ok=True)
    for pid in ids:
        _jobs.put(("page", pid))
    return ids, errors


def _page_state(r) -> dict:
    return dict(id=r["id"], name=r["name"], status=r["status"], message=r["message"] or "", layout=r["layout"] or "",
                layout_set=r["layout_set"] or "auto", conf=round(r["conf"]) if r["conf"] is not None else None,
                hit=round(100 * r["hit"]) if r["hit"] is not None else None, engine=r["engine"] or "tesseract")


@app.get("/phone/{token}", response_class=HTMLResponse)
def phone_page(request: Request, token: str):
    return templates.TemplateResponse(request, "phone.html", dict(token=token))


@app.post("/phone/{token}/upload")
async def phone_upload(token: str, files: list[UploadFile] = File(...)):
    ids, errors = await _take_uploads(files)
    return JSONResponse(dict(ids=ids, errors=errors))


@app.get("/phone/{token}/status")
def phone_status(token: str, ids: str = ""):
    want = [int(x) for x in ids.split(",") if x.isdigit()][:500]
    with db.session() as con:
        rows = [_page_state(r) for r in con.execute(f"SELECT * FROM inbox WHERE id IN ({','.join('?' * len(want))})", want)] if want else []
    return JSONResponse(rows)


@app.get("/upload")
def upload_alias():
    return RedirectResponse("/inbox", status_code=303)


@app.get("/inbox", response_class=HTMLResponse)
def inbox_page(request: Request, note: str = ""):
    with db.session() as con:
        rows = pages.listing(con)
        link = phone_link(con)
        paper = con.execute("SELECT id, title FROM sources WHERE kind='pages' ORDER BY id DESC").fetchall()
    qr = segno.make(link, error="m").svg_inline(scale=5, border=2, dark="#1f2328", light="#ffffff") if link else None
    return page(request, "inbox.html", rows=[r | _page_state(r) for r in rows], link=link, qr=qr, paper=paper, note=note,
                missing=pages.missing(), install=ocr.INSTALL, layouts=pages.LAYOUTS, engine=pages.engine(),
                old=sum(1 for r in rows if r["status"] in ("ok", "poor") and (r["engine"] or "tesseract") != pages.engine()))


@app.post("/inbox/engine")
def inbox_engine():
    """Fetch NDLOCR-Lite, then read again whatever Tesseract had read."""
    def work(progress):
        ndl.install(progress)
        with db.session() as con:
            again = pages.stale(con, [r["id"] for r in pages.listing(con)])
            pages.requeue(con, again)
        for pid in again:
            _jobs.put(("page", pid))
        return "/inbox"
    return start_job("Installing the reader for books", work)


@app.post("/inbox/upload")
async def inbox_upload(files: list[UploadFile] = File(...)):
    _, errors = await _take_uploads(files)
    return _note("; ".join(errors), "/inbox") if errors else RedirectResponse("/inbox", status_code=303)


@app.get("/inbox/status")
def inbox_status():
    with db.session() as con:
        return JSONResponse([_page_state(r) for r in pages.listing(con)])


@app.get("/inbox/{pid}/picture")
def inbox_picture(pid: int, small: int = 0):
    f = pages.folder() / (f"p{pid}-small.jpg" if small else f"p{pid}.jpg")
    return FileResponse(f, headers={"Cache-Control": "no-store"}) if f.exists() else JSONResponse({}, status_code=404)


@app.get("/inbox/{pid}/text")
def inbox_text(pid: int):
    return PlainTextResponse(pages.text_of(pid))


@app.post("/inbox/act")
async def inbox_act(request: Request):
    """One action on the chosen pages: read again with a layout, turn, delete, or make a source."""
    form = await request.form()
    ids = [int(x) for x in form.getlist("page") if str(x).isdigit()]
    act = str(form.get("act", ""))
    if not ids:
        return _note("Tick the pages first.", "/inbox")
    try:
        with db.session() as con:
            if act.startswith("layout-"):
                pages.requeue(con, ids, act.split("-", 1)[1])
            elif act == "turn":
                for pid in ids:
                    pages.rotate(con, pid)
            elif act == "delete":
                pages.delete(con, ids)
            elif act == "make":
                sid = pages.make_source(con, ids, title=str(form.get("title", "")), tags=str(form.get("tags", "")),
                                        series=str(form.get("series", "")), into=int(form.get("into") or 0) or None)
    except RuntimeError as e:
        return _note(str(e), "/inbox")
    if act == "make":
        _jobs.put(sid)
        return RedirectResponse("/", status_code=303)
    if act.startswith("layout-") or act == "turn":
        for pid in ids:
            _jobs.put(("page", pid))
    return RedirectResponse("/inbox", status_code=303)


@app.get("/settings", response_class=HTMLResponse)
@app.get("/settings/{tab}", response_class=HTMLResponse)
def settings(request: Request, note: str = "", tab: str = "general"):
    with db.session() as con:
        vals = {k: db.setting(con, k) for k in config.DEFAULTS}
    info = dict(dicts=[], glossaries=[], freq=frequency.current(), am_version=None, dict_version="", ocr=ocr.status(), pairs=[], registers=True)
    if dictdata.db_path().exists():
        dic, _ = engine()
        info["dicts"] = dic.dictionaries()
        info["glossaries"] = [tuple(r) for r in dic.con.execute("SELECT source, COUNT(*) FROM custom GROUP BY source")]
        info["dict_version"] = (dic.con.execute("SELECT value FROM meta WHERE key='version'").fetchone() or [""])[0]
        info["pairs"] = dic.pair_lists()
        info["registers"] = dic.has_registers()
    try:
        info["am_version"] = _state.setdefault("am_version", ankiminer.version())
    except Exception as e:  # noqa: BLE001
        info["am_error"] = str(e)
    with db.session() as con:
        paper = dict(engine=pages.engine(), missing=pages.missing()) if tab == "engines" else {}
    return page(request, "settings.html", v=vals, levels=LEVELS, info=info, note=note, data=str(config.data_dir()), paper=paper,
                tab=tab if tab in ("general", "dictionaries", "engines") else "general")


@app.post("/settings")
def settings_save(level: str = Form(""), gloss_dict: str = Form("")):
    changed_gloss = False
    with db.session() as con:
        if gloss_dict and db.setting(con, "gloss_dict") != gloss_dict:
            db.set_setting(con, "gloss_dict", gloss_dict)
            changed_gloss = True
        if level and db.setting(con, "level") != level:
            analyze.apply_level(con, int(level))
            analyze.rebuild_cards(con)
            ingest.cut_clips(con)
    if changed_gloss:
        reset_engine()
    return RedirectResponse("/settings?note=Saved", status_code=303)


# ---- the reader device -------------------------------------------------------------------------------
@app.get("/reader", response_class=HTMLResponse)
def reader_page(request: Request):
    with db.session() as con:
        return page(request, "reader.html", tab="sync", address=db.setting(con, "device_address") or "http://sumitsubo.local",
                    last=json.loads(db.setting(con, "last_sync") or "null"))


@app.get("/reader/study", response_class=HTMLResponse)
def reader_study(request: Request, note: str = ""):
    with db.session() as con:
        return page(request, "reader.html", tab="study", note=note, daily=db.setting(con, "daily_budget") or 15, more=db.setting(con, "more_new") or "once",
                    when=db.setting(con, "contrast_when") or "mixup", choices=contrast.WHEN)


@app.post("/reader/study")
def reader_study_save(daily_budget: str = Form("15"), more_new: str = Form("once"), contrast_when: str = Form("")):
    with db.session() as con:
        db.set_setting(con, "daily_budget", max(1, min(200, int(daily_budget or 15))))
        if more_new in ("never", "once", "free"):
            db.set_setting(con, "more_new", more_new)
        if contrast_when in dict(contrast.WHEN):
            db.set_setting(con, "contrast_when", contrast_when)
    return RedirectResponse("/reader/study?note=Saved", status_code=303)


@app.get("/reader/books", response_class=HTMLResponse)
def reader_books(request: Request):
    with db.session() as con:
        rows = [dict(s) | dict(cov=analyze.coverage(con, s["id"])) for s in
                con.execute("SELECT * FROM sources WHERE status='done' AND book IS NOT NULL ORDER BY to_reader DESC, title").fetchall()]
    return page(request, "reader.html", tab="books", books=rows)


@app.get("/reader/card", response_class=HTMLResponse)
def reader_card(request: Request, note: str = ""):
    return page(request, "reader.html", tab="card", note=note)


@app.post("/reader/card")
def reader_card_setup(folder: str = Form(""), fonts: str = Form("")):
    """Prepare a new card: Matcha's Japanese dictionary and fonts, and the deck (as `sumitsubo card-setup`)."""
    from . import device  # noqa: PLC0415
    card = Path(folder.strip()).expanduser()

    def work(progress):
        if not folder.strip() or not card.is_dir():
            raise RuntimeError(f"There is no folder {card}. Give the folder where the card is mounted.")
        with db.session() as con:
            device.card_setup(con, card, progress, fonts=bool(fonts))
        return "/reader/card?note=" + urllib.parse.quote(f"The card in {card} is ready.")
    return start_job("Preparing the card", work)


# ---- statistics ---------------------------------------------------------------------------------------
def _kanji_levels(con) -> list[dict]:
    state = {}                                   # a kanji is as well known as the best known of its readings
    rank = dict(new=1, young=2, mature=3)
    for r in con.execute("SELECT lemma, band FROM items WHERE kind='kanji' AND status<>'ignored'"):
        if rank.get(r["band"], 0) > rank.get(state.get(r["lemma"]), 0):
            state[r["lemma"]] = r["band"]
    if not dictdata.db_path().exists():
        return []
    dic, _ = engine()
    groups: dict = {}
    for k in dic.con.execute("SELECT literal, jlpt, freq FROM kanji ORDER BY COALESCE(freq, 99999), literal"):
        lv = analyze.kanji_level(dict(k), k["literal"])
        if lv:
            groups.setdefault(lv, []).append((k["literal"], state.get(k["literal"], "")))
    out = []
    for lv in (5, 4, 3, 2, 1):
        cells = groups.get(lv, [])
        if cells:
            n = {b: sum(1 for _, x in cells if x == b) for b in ("mature", "young", "new")}
            out.append(dict(name=f"N{lv}", cells=cells, **n, pct={b: round(100 * v / len(cells), 1) for b, v in n.items()}))
    return out


def _review_days(con) -> dict:
    """Reviews by day: those made on the reader, and those that came with an import."""
    from . import device  # noqa: PLC0415
    device.tables(con)
    per = dict(con.execute("SELECT day, reviews FROM device_days").fetchall())
    for d, n in con.execute("SELECT CAST(ts / 86400 AS INT), COUNT(*) FROM reviews GROUP BY 1"):
        per[d] = per.get(d, 0) + n
    return per


def _days(per: dict, today: int, n: int) -> list[dict]:
    return [dict(day=d, n=per.get(d, 0), label=time.strftime("%d %b", time.gmtime(d * 86400)).lstrip("0")) for d in range(today - n + 1, today + 1)]


@app.get("/stats", response_class=HTMLResponse)
def stats_page(request: Request):
    from . import device  # noqa: PLC0415
    with db.session() as con:
        by = {(r[0], r[1]): r[2] for r in con.execute("SELECT kind, band, COUNT(*) FROM items WHERE status<>'ignored' GROUP BY kind, band")}
        words = {b: by.get(("word", b), 0) for b in ("mature", "young", "new")}
        total = sum(words.values()) or 1
        days = _days(_review_days(con), device.day(db.now()), 14)
        levels = _kanji_levels(con)
    return page(request, "stats.html", tab="overview", words=words, pct={b: round(100 * n / total) for b, n in words.items()},
                kanji=dict(mature=by.get(("kanji", "mature"), 0), young=by.get(("kanji", "young"), 0), new=by.get(("kanji", "new"), 0)),
                days=days, peak=max([d["n"] for d in days] + [1]), levels=levels)


@app.get("/stats/kanji", response_class=HTMLResponse)
def stats_kanji(request: Request):
    with db.session() as con:
        levels = _kanji_levels(con)
    return page(request, "stats.html", tab="kanji", levels=levels)


@app.get("/stats/reviews", response_class=HTMLResponse)
def stats_reviews(request: Request):
    from . import device  # noqa: PLC0415
    with db.session() as con:
        per = _review_days(con)
        today = device.day(db.now())
        ahead = {}                                  # what falls due, day by day; everything overdue counts as today
        for (due,) in con.execute("SELECT due FROM memory WHERE due IS NOT NULL AND state<>'new'"):
            d = max(today, device.day(due))
            if d < today + 14:
                ahead[d] = ahead.get(d, 0) + 1
    days = _days(per, today, 84)
    streak = 0
    while per.get(today - streak - (0 if per.get(today) else 1), 0):
        streak += 1
    studied = [n for n in per.values() if n]
    months: dict = {}
    for d, n in per.items():
        key = time.strftime("%Y-%m", time.gmtime(d * 86400))
        m = months.setdefault(key, dict(label=time.strftime("%B %Y", time.gmtime(d * 86400)), n=0, days=0))
        m["n"] += n
        m["days"] += 1 if n else 0
    coming = [dict(n=ahead.get(d, 0), label=time.strftime("%d %b", time.gmtime(d * 86400)).lstrip("0")) for d in range(today, today + 14)]
    return page(request, "stats.html", tab="reviews", days=days, peak=max([d["n"] for d in days] + [1]), coming=coming,
                cpeak=max([d["n"] for d in coming] + [1]), months=[months[k] for k in sorted(months, reverse=True)][:12],
                totals=dict(reviews=sum(studied), days=len(studied), streak=streak, mean=round(sum(studied) / len(studied)) if studied else 0))


@app.post("/settings/pairs")
def settings_pairs(upload: UploadFile = File(...)):
    n = dictdata.add_pairs(_save_upload(upload, "glossaries"))
    reset_engine()
    return RedirectResponse(f"/settings?note=Loaded {n} collocation pairs. Process a source again to find them in it.", status_code=303)


@app.post("/settings/glossary")
def settings_glossary(upload: UploadFile = File(...)):
    n = dictdata.add_glossary(_save_upload(upload, "glossaries"), copy=False)
    reset_engine()
    return RedirectResponse(f"/settings?note=Loaded {n} glossary terms. Reprocess a source to apply them to it.", status_code=303)


@app.post("/settings/dictionary")
def settings_dictionary(upload: UploadFile = File(...)):
    n = dictdata.add_yomitan(_save_upload(upload, "downloads"))
    reset_engine()
    return RedirectResponse(f"/settings?note=Added a dictionary with {n} entries", status_code=303)


@app.post("/settings/frequency")
def settings_frequency(upload: UploadFile | None = File(None), action: str = Form("file")):
    """Load a frequency list from a chosen file, or download Jiten's list once. Read from disk afterwards."""
    try:
        up = _save_upload(upload, "downloads")
        if up is None and action != "download":
            raise ValueError("choose a file first")
        f = frequency.install(str(up) if up else None, refresh=up is None)
        reset_engine()
        dic, _ = engine()
        with db.session() as con:
            changed = analyze.refresh_frequency(con, dic)
        note = f"Loaded {f['ranks']} frequency ranks ({f['name']}); updated {changed} words already collected."
    except Exception as e:  # noqa: BLE001
        note = f"Could not load the frequency list: {e}"
    return RedirectResponse("/settings?note=" + urllib.parse.quote(note), status_code=303)


@app.post("/settings/speech")
def settings_speech(model: str = Form("large-v3")):
    def run():
        _state["speech"] = "installing"
        try:
            res = ankiminer.setup_speech(model, config.data_dir() / "work" / "setup" / "progress.json")
            sm = (res.get("result") or {}).get("speech_model") or {}
            _state["speech"] = f"{sm.get('status', 'unknown')}: {sm.get('message') or ''}"
        except Exception as e:  # noqa: BLE001
            _state["speech"] = f"failed: {e}"
    threading.Thread(target=run, daemon=True).start()
    time.sleep(0.2)
    return RedirectResponse("/settings?note=Installing the speech model in the background. This can take a long time.", status_code=303)
