"""Bringing in what you already know from elsewhere: Anki decks, Renshuu, or a plain list.

Cards arrive with their memory state, not as "known" or "unknown": a card you have reviewed for
two years comes in as a mature review card, one you started last week as a young one, and each
keeps its due date. Difficulty scores then rest on your real progress, not only on a level.
Nothing is sent back to Anki or Renshuu.

Where the state comes from, best first:
  the review history    Anki's log of every review, replayed through the scheduler and kept
  a stored state        the stability and difficulty Anki keeps when its FSRS scheduler is on
  interval and ease     converted (the interval is, near enough, the stability)
  a mastery percentage  Renshuu and lists: an estimate, clearly rougher

One upload or fetch is a *batch*: a snapshot kept in <data>/imports, so its choices can be changed
later without fetching again. A batch holds one or more *decks* (Anki decks, Renshuu schedules).
Each deck has its own choice:

    skip       leave it out
    progress   take the memory states only; nothing is added to study here   (the default)
    studied    also bring the cards you have studied, to continue them here
    all        also bring the cards you have not started

With "progress", an imported word gets a card here only when it turns up in your own material.
A kanji reading inherits the state of the best-known imported word that contains it. What you
marked by hand outranks an import, and an import outranks the level assumption.

Anki is read from a deck file (.apkg, .colpkg) or from a running Anki through the AnkiConnect
add-on. AnkiWeb is never contacted: its terms allow only Anki's own apps to sync with it.
"""
import csv
import html
import io
import json
import re
import sqlite3
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from . import analyze, config, db, memory
from .dictdata import hira

MODES = [("skip", "Leave out"), ("progress", "Progress only"), ("studied", "Progress + studied cards"), ("all", "Everything")]
RENSHUU = "https://api.renshuu.org/v1"
ANKICONNECT = "http://127.0.0.1:8765"
_JA = re.compile(r"[぀-ヿ㐀-鿿]")
_KANA = re.compile(r"^[぀-ヿー・]+$")
_FURI = re.compile(r" ?([^\s\[\]]+)\[([^\[\]]+)\]")
_NAMES = dict(
    word=("expression", "word", "vocab", "vocabulary", "vocabulary-kanji", "target", "targetword", "term", "kanji", "front", "japanese", "単語", "語彙", "表現", "vocabkanji", "key"),
    reading=("reading", "kana", "furigana", "vocabulary-kana", "vocabfurigana", "vocabulary-furigana", "wordreading", "expressionreading", "読み", "よみ", "ふりがな", "hiragana"),
    sentence=("sentence", "example", "examplesentence", "context", "expression-sentence", "sentence-kanji", "例文", "文", "sentencekanji", "example sentence"))


def folder() -> Path:
    p = config.data_dir() / "imports"
    p.mkdir(parents=True, exist_ok=True)
    return p


# ---- cleaning field values -------------------------------------------------------------------------
def clean(v: str) -> str:
    """A field's text without markup or sound tags."""
    v = re.sub(r"\[sound:[^\]]*\]", "", v or "")
    v = re.sub(r"(?i)<br\s*/?>|</div>|</p>", " ", v)
    v = html.unescape(re.sub(r"<[^>]+>", "", v))
    return re.sub(r"[\s ]+", " ", v).strip()


def split_furigana(v: str) -> tuple[str, str | None]:
    """'食[た]べる' -> ('食べる', 'たべる'). Text without Anki furigana comes back unchanged, with no reading."""
    if "[" not in v:
        return v, None
    return _FURI.sub(r"\1", v).replace(" ", ""), _FURI.sub(r"\2", v).replace(" ", "")


def entry(note: dict, m: dict) -> tuple[str, str | None, str] | None:
    """(word, reading, sentence) of a note under a field mapping, or None if it holds no single word."""
    f = note["f"]

    def get(key):
        i = m.get(key)
        return clean(f[i]) if i is not None and 0 <= i < len(f) else ""
    word, reading = split_furigana(get("word"))
    r2 = split_furigana(get("reading"))
    reading = (r2[1] or r2[0] or reading or "").strip() or None
    word = re.sub(r"[\s　]+", "", word)
    if reading and not _KANA.match(reading):
        reading = None
    if not word or not _JA.search(word) or len(word) > 20 or re.search(r"[。、！？!?「」]", word):
        return None
    sentence = _FURI.sub(r"\1", get("sentence")).strip()
    return word, reading, sentence if _JA.search(sentence) and len(sentence) <= 400 else ""


def guess_mapping(fields: list[str], samples: list[list[str]]) -> dict:
    """Which field holds the word, the reading and an example sentence: by name, then by content."""
    low = [re.sub(r"[\s_]+", "", x.lower()) for x in fields]
    m: dict = {}
    for key, names in _NAMES.items():
        m[key] = next((i for i, x in enumerate(low) if x in names), None)
    if m["word"] is None:                         # the first field that mostly holds short Japanese
        for i in range(len(fields)):
            vals = [split_furigana(clean(s[i]))[0] for s in samples if i < len(s) and s[i]]
            if vals and sum(1 for v in vals if _JA.search(v) and len(v) <= 12) >= 0.7 * len(vals):
                m["word"] = i
                break
    if m["sentence"] == m["word"]:
        m["sentence"] = None
    return m


# ---- readers: each returns a batch dict -------------------------------------------------------------
def _batch(kind: str, label: str) -> dict:
    return dict(kind=kind, label=label, notetypes={}, notes=[], skipped=[], read=time.time())


def _usable(log) -> list:
    """Reviews worth replaying: a real answer (again, hard, good, easy) while learning or reviewing."""
    return [[ts, ease] for ts, ease, typ in log if ease in (1, 2, 3, 4) and typ in (0, 1, 2)]


def _unzstd(data: bytes) -> bytes:
    try:
        from compression import zstd  # noqa: PLC0415 - Python 3.14 and later
        return zstd.decompress(data)
    except ImportError:
        pass
    try:
        import zstandard  # noqa: PLC0415
    except ImportError as e:
        raise RuntimeError("This deck file uses Anki's newer format, which needs one more library. Run: pip install zstandard   "
                           "(or export from Anki with \"Support older Anki versions\" ticked).") from e
    return zstandard.ZstdDecompressor().decompressobj().decompress(data)


def _open_anki(path: Path, tmp: Path) -> sqlite3.Connection:
    """The collection database inside a deck file (or a bare collection file)."""
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            if "collection.anki21b" in names:     # current format, compressed
                (tmp / "c.db").write_bytes(_unzstd(z.read("collection.anki21b")))
            else:
                name = next((n for n in ("collection.anki21", "collection.anki2") if n in names), None)
                if not name:
                    raise RuntimeError("This file is not an Anki deck: no collection found inside it.")
                (tmp / "c.db").write_bytes(z.read(name))
        path = tmp / "c.db"
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.create_collation("unicase", lambda a, b: (a.lower() > b.lower()) - (a.lower() < b.lower()))
    return con


def read_anki(path) -> dict:
    """Notes of an Anki deck file, each with its best card (longest interval) and that card's review history."""
    path = Path(path)
    b = _batch("anki", path.name)
    with tempfile.TemporaryDirectory() as t:
        con = _open_anki(path, Path(t))
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "notes" not in tables or "cards" not in tables:
            raise RuntimeError("This file is not an Anki collection.")
        if "notetypes" in tables:                 # current layout
            types = {r["id"]: r["name"] for r in con.execute("SELECT id, name FROM notetypes")}
            fields: dict = {}
            for r in con.execute("SELECT ntid, ord, name FROM fields ORDER BY ntid, ord"):
                fields.setdefault(r["ntid"], []).append(r["name"])
            decks = {r["id"]: r["name"].replace("\x1f", "::") for r in con.execute("SELECT id, name FROM decks")}
        else:                                     # older layout: everything in one row of col
            col = con.execute("SELECT models, decks FROM col").fetchone()
            models = json.loads(col["models"] or "{}")
            types = {int(k): v["name"] for k, v in models.items()}
            fields = {int(k): [f["name"] for f in sorted(v["flds"], key=lambda f: f["ord"])] for k, v in models.items()}
            decks = {int(k): v["name"] for k, v in json.loads(col["decks"] or "{}").items()}
        crt = (con.execute("SELECT crt FROM col").fetchone() or [0])[0] or 0     # day zero of the collection's due numbers
        best: dict = {}
        for c in con.execute("SELECT id, nid, did, odid, type, ivl, reps, lapses, due, odue, factor, mod, data FROM cards"):
            cur = best.get(c["nid"])
            if cur is None or (c["type"] == 2, c["ivl"]) > (cur["type"] == 2, cur["ivl"]):
                best[c["nid"]] = dict(c)
        wanted, logs = {c["id"] for c in best.values()}, {}
        if "revlog" in tables:
            for r in con.execute("SELECT id, cid, ease, type FROM revlog ORDER BY id"):
                if r["cid"] in wanted:
                    logs.setdefault(r["cid"], []).append([r["id"] / 1000.0, r["ease"], r["type"]])
        for n in con.execute("SELECT id, mid, flds FROM notes"):
            c = best.get(n["id"])
            if not c or n["mid"] not in types:
                continue
            b["notetypes"].setdefault(types[n["mid"]], fields.get(n["mid"], []))
            try:
                extra = json.loads(c["data"] or "{}")
            except ValueError:
                extra = {}
            ivl, due = max(0, c["ivl"]), c["odue"] or c["due"]
            # when it was last reviewed: a review card is due `ivl` days after that, counted in days from crt
            last = min(b["read"], crt + (due - ivl) * 86400.0) if c["type"] == 2 and crt and 0 < due < 100000 else float(c["mod"])
            b["notes"].append(dict(deck=decks.get(c["odid"] or c["did"], "Default"), nt=types[n["mid"]], f=n["flds"].split("\x1f"),
                                   type=c["type"], ivl=ivl, reps=c["reps"], lapses=c["lapses"], factor=c["factor"], last=last,
                                   fsrs=dict(s=extra["s"], d=extra.get("d")) if extra.get("s") else None,
                                   log=_usable(logs.get(c["id"], []))))
        con.close()
    if any("Please update to the latest Anki version" in " ".join(n["f"]) for n in b["notes"][:3]) and len(b["notes"]) <= 1:
        raise RuntimeError("This deck file only holds a placeholder. Export it again from a current Anki.")
    return b


def _ankiconnect(action: str, url: str = ANKICONNECT, **params):
    req = urllib.request.Request(url, json.dumps(dict(action=action, version=6, params=params)).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            res = json.load(r)
    except OSError as e:
        raise RuntimeError("Could not reach Anki. Open Anki on this computer with the AnkiConnect add-on installed "
                           f"(add-on code 2055492159), then try again. ({e})") from e
    if res.get("error"):
        raise RuntimeError(f"AnkiConnect: {res['error']}")
    return res["result"]


def read_ankiconnect(url: str = ANKICONNECT, progress=lambda stage, done=0, total=0: None) -> dict:
    """The same information from a running Anki, through the AnkiConnect add-on."""
    b = _batch("ankiconnect", "Anki (running)")
    ids = _ankiconnect("findCards", url, query="deck:*")
    best: dict = {}
    for i in range(0, len(ids), 500):
        progress("Reading cards from Anki", i, len(ids))
        for c in _ankiconnect("cardsInfo", url, cards=ids[i:i + 500]):
            cur = best.get(c["note"])
            if cur is None or (c["type"] == 2, c["interval"]) > (cur["type"] == 2, cur["interval"]):
                best[c["note"]] = c
    logs: dict = {}
    progress("Reading the review history from Anki")
    try:                                          # the review history, where this version of the add-on offers it
        got = _ankiconnect("getReviewsOfCards", url, cards=[str(c["cardId"]) for c in best.values()])
        logs = {str(k): _usable([[r["id"] / 1000.0, r["ease"], r["type"]] for r in v]) for k, v in (got or {}).items()}
    except RuntimeError:
        pass
    for c in best.values():
        fs = sorted(c["fields"].items(), key=lambda kv: kv[1]["order"])
        b["notetypes"].setdefault(c["modelName"], [k for k, _ in fs])
        b["notes"].append(dict(deck=c["deckName"], nt=c["modelName"], f=[v["value"] for _, v in fs], type=c["type"],
                               ivl=max(0, c["interval"]), reps=c.get("reps", 0), lapses=c.get("lapses", 0), factor=c.get("factor"),
                               last=float(c.get("mod") or b["read"]), fsrs=None, log=logs.get(str(c["cardId"]), [])))
    return b


KANJI_NT, VOCAB_NT = "Renshuu kanji", "Renshuu vocabulary"
FIXED = (KANJI_NT,)                 # note types whose fields are known, so there is nothing to choose


def kanji_readings(*fields) -> list[str]:
    """Readings from Renshuu's on and kun fields, as hiragana stems: 'ケン' and 'しら.べる' give けん, しら."""
    out = []
    for f in fields:
        for r in re.split(r"[、,，/;・\s]+", f or ""):
            r = hira(r.split(".")[0].replace("-", "").replace("～", "").strip())
            if r and _KANA.match(r) and r not in out:
                out.append(r)
    return out


def _user(t: dict) -> tuple[float, bool]:
    u = t.get("user_data") or {}
    pct = float(u.get("mastery_avg_perc") or 0)
    return pct, bool(pct > 0 or (u.get("correct_count") or 0) > 0)


def _renshuu_notes(b: dict, schedule: dict, terms: list):
    """Notes from one Renshuu schedule. Vocabulary and kanji schedules are read; a term that is
    neither a word nor a kanji (grammar, sentences) is counted as not read."""
    deck, kind, other = schedule.get("name") or "Renshuu", schedule.get("booktype"), 0
    if kind not in (None, "vocab", "kanji"):
        b["skipped"].append(f"{deck} ({kind})")
        return
    for t in terms:
        pct, studied = _user(t)
        if t.get("hiragana_full") or t.get("kanji_full"):
            meaning = t.get("def") or ""
            b["notetypes"].setdefault(VOCAB_NT, ["Word", "Reading", "Meaning", "Sentence"])
            b["notes"].append(dict(deck=deck, nt=VOCAB_NT, rid=t.get("id"), pct=pct, studied=studied,
                                   f=[t.get("kanji_full") or t.get("hiragana_full") or "", t.get("hiragana_full") or "",
                                      "; ".join(meaning) if isinstance(meaning, list) else str(meaning), ""]))
        elif t.get("kanji") and len(str(t["kanji"])) == 1:
            b["notetypes"].setdefault(KANJI_NT, ["Kanji", "Onyomi", "Kunyomi", "Meaning"])
            b["notes"].append(dict(deck=deck, nt=KANJI_NT, kanji=t["kanji"], pct=pct, studied=studied,
                                   readings=kanji_readings(t.get("onyomi"), t.get("kunyomi")),
                                   f=[t["kanji"], t.get("onyomi") or "", t.get("kunyomi") or "", str(t.get("definition") or "")]))
        else:
            other += 1
    if other:
        b["skipped"].append(f"{other} entries of {deck} that are neither words nor kanji")


def read_renshuu(key: str, base: str = RENSHUU, delay: float = 0.4, sentences: int = 0,
                 progress=lambda stage, done=0, total=0: None) -> dict:
    """Vocabulary and kanji schedules from Renshuu's API with each term's mastery. The key is used,
    not kept. A word's entry holds its meanings but no example sentence; those come from a separate
    request per word, so they are fetched only on request, for at most `sentences` studied words."""
    def get(path, **params):
        url = base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"}), timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                raise RuntimeError("Renshuu's daily request allowance for your key is used up. Try again tomorrow.") from e
            raise RuntimeError("Renshuu refused the key." if e.code in (401, 403) else f"Renshuu answered with error {e.code}.") from e
        except OSError as e:
            raise RuntimeError(f"Could not reach Renshuu ({e}).") from e
    b = _batch("renshuu", "Renshuu")
    progress("Asking Renshuu for your schedules")
    schedules = get("/schedule").get("schedules", [])
    for k, s in enumerate(schedules):
        if s.get("booktype") not in (None, "vocab", "kanji"):
            _renshuu_notes(b, s, [])
            continue
        terms, page, pages = [], 1, 1
        while True:
            progress(f"Fetching schedule {k + 1} of {len(schedules)}: {s.get('name')} (page {page} of {pages})", k, len(schedules))
            contents = get(f"/schedule/{s['id']}/list", pg=page, group="all").get("contents", {})
            terms += contents.get("terms", [])
            pages = int(contents.get("total_pg") or 1)
            if page >= pages:
                break
            page += 1
            time.sleep(delay)
        _renshuu_notes(b, s, terms)
        time.sleep(delay)
    done: dict = {}
    for n in sorted((n for n in b["notes"] if n["nt"] == VOCAB_NT and n["studied"] and n.get("rid")), key=lambda n: n["pct"]):
        if len(done) >= sentences:                # least-known words first: they are the ones a sentence helps
            break
        if n["rid"] not in done:
            progress("Fetching example sentences", len(done), sentences)
            try:
                res = get(f"/reibun/search/{n['rid']}")
            except RuntimeError:
                break                             # out of allowance or unreachable: keep what we have
            found = next((x.get("japanese") for v in res.values() if isinstance(v, list) for x in v if isinstance(x, dict) and x.get("japanese")), "")
            done[n["rid"]] = clean(re.sub(r"[（(][぀-ゟ]+[）)]", "", found))     # without bracketed readings
            time.sleep(delay)
    for n in b["notes"]:
        if n.get("rid") in done:
            n["f"][3] = done[n["rid"]]
    return b


def read_renshuu_export(path) -> dict:
    """A saved copy of the same data: {"schedules": [{"name", "booktype", "terms_list": [...]}]}."""
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or "schedules" not in data:
        raise RuntimeError("This JSON file is not a Renshuu export (no \"schedules\" in it).")
    b = _batch("renshuu", "Renshuu")
    for s in data["schedules"]:
        _renshuu_notes(b, s, s.get("terms_list") or s.get("terms") or [])
    return b


def read_list(path) -> dict:
    """A plain list: one word per line, or a CSV/TSV table with word, reading and mastery columns.
    Without a mastery column every word counts as known."""
    path = Path(path)
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    first = text.splitlines()[0] if text.strip() else ""
    delim = "\t" if "\t" in first else "," if "," in first else None
    rows = list(csv.reader(io.StringIO(text), delimiter=delim)) if delim else [[x] for x in text.splitlines()]
    rows = [[c.strip() for c in r] for r in rows if any(c.strip() for c in r)]
    b = _batch("list", path.name)
    if not rows:
        raise RuntimeError("The file is empty.")
    head = [h.lower() for h in rows[0]]
    has_head = not any(_JA.search(c) for c in rows[0])
    width = max(len(r) for r in rows)
    names = [(rows[0][i] if has_head and i < len(rows[0]) and rows[0][i] else f"Column {i + 1}") for i in range(width)]
    pct_col = next((i for i, h in enumerate(head) if has_head and re.search(r"master|percent|%|level|score|known", h)), None)
    body = rows[1:] if has_head else rows
    b["notetypes"]["List"] = names
    for r in body:
        r = r + [""] * (width - len(r))
        pct, studied = 100.0, True
        if pct_col is not None:
            mo = re.search(r"\d+(\.\d+)?", r[pct_col])
            pct = float(mo.group()) if mo else 0.0
            studied = pct > 0
        b["notes"].append(dict(deck=path.stem, nt="List", f=r, pct=pct, studied=studied))
    return b


def read_file(path) -> dict:
    ext = Path(path).suffix.lower()
    if ext in (".apkg", ".colpkg", ".anki2", ".anki21"):
        return read_anki(path)
    return read_renshuu_export(path) if ext == ".json" else read_list(path)


# ---- batches ---------------------------------------------------------------------------------------
def seed_for(note: dict, now: float) -> dict | None:
    """The memory state a note brings, or None if it was never studied. See the top of this file for the order."""
    if "pct" in note:
        return memory.from_percent(note["pct"], now) if note.get("studied") else None
    if note.get("log"):
        s = memory.replay([(ts, ease) for ts, ease in note["log"]])
        if s:
            return s
    last = min(now, note.get("last") or now)
    if note.get("fsrs"):
        st = dict(state="review" if note["type"] == 2 else "learning", stability=float(note["fsrs"]["s"]),
                  difficulty=float(note["fsrs"].get("d") or 5.0), last_review=last)
        return st | dict(due=last + max(note["ivl"], 1) * memory.DAY)
    if note["type"] == 2:
        return memory.from_interval(note["ivl"], (note.get("factor") or 2500) / 1000.0, last)
    if note["type"] in (1, 3) or note.get("reps"):
        return memory.from_interval(0.5, (note.get("factor") or 2500) / 1000.0, last, learning=True)
    return None


def state(note: dict) -> str:
    """mature, young or new: the band of the state a note brings."""
    seed = note.get("seed")
    return memory.band(seed["state"], seed["stability"]) if seed else "new"


def _with_seeds(b: dict, progress=lambda stage, done=0, total=0: None) -> bool:
    """Work out each note's state once and keep it in the snapshot. True if anything was added."""
    todo = [n for n in b["notes"] if "seed" not in n]
    for k, n in enumerate(todo):
        if k % 200 == 0:
            progress("Replaying review histories", k, len(todo))
        n["seed"] = seed_for(n, b.get("read") or time.time())
    return bool(todo)


def load(con, batch_id: int) -> tuple[dict, dict]:
    row = con.execute("SELECT * FROM import_batches WHERE id=?", (batch_id,)).fetchone()
    b = json.loads((folder() / row["file"]).read_text(encoding="utf-8"))
    if _with_seeds(b):                             # a snapshot from before 0.2.0
        (folder() / row["file"]).write_text(json.dumps(b, ensure_ascii=False), encoding="utf-8")
    return dict(row), b


def save_batch(con, b: dict, progress=lambda stage, done=0, total=0: None) -> int:
    """Keep a snapshot. Bringing in the same thing again updates it and keeps each deck's choice."""
    if not b["notes"]:
        raise RuntimeError("Nothing to import was found." + (f" Not vocabulary, so not read: {', '.join(b['skipped'])}." if b["skipped"] else ""))
    row = con.execute("SELECT id, mapping FROM import_batches WHERE kind=? AND label=?", (b["kind"], b["label"])).fetchone()
    samples: dict = {}
    for n in b["notes"]:
        s = samples.setdefault(n["nt"], [])
        if len(s) < 40:
            s.append(n["f"])
    mapping = json.loads(row["mapping"]) if row else {}
    for nt, fields in b["notetypes"].items():
        if nt not in mapping or any(v is not None and v >= len(fields) for v in mapping[nt].values()):
            mapping[nt] = guess_mapping(fields, samples.get(nt, []))
    if row:
        bid = row["id"]
        con.execute("UPDATE import_batches SET mapping=?, added=? WHERE id=?", (json.dumps(mapping), db.now(), bid))
    else:
        bid = con.execute("INSERT INTO import_batches(kind, label, file, mapping, days, pct, added) VALUES(?,?,?,?,?,?,?)",
                          (b["kind"], b["label"], "", json.dumps(mapping), 21, 50, db.now())).lastrowid
        con.execute("UPDATE import_batches SET file=? WHERE id=?", (f"batch-{bid}.json", bid))
    _with_seeds(b, progress)
    (folder() / f"batch-{bid}.json").write_text(json.dumps(b, ensure_ascii=False), encoding="utf-8")
    have = {r["deck"] for r in con.execute("SELECT deck FROM imports WHERE batch_id=?", (bid,))}
    for deck in sorted({n["deck"] for n in b["notes"]} - have):
        con.execute("INSERT INTO imports(batch_id, deck, mode) VALUES(?,?,?)", (bid, deck, "progress"))
    return bid


def preview(con, batch_id: int) -> dict:
    """Per deck: how many notes come in mature, young, new, or hold no single word; plus samples."""
    row, b = load(con, batch_id)
    mapping = json.loads(row["mapping"])
    decks: dict = {}
    for n in b["notes"]:
        d = decks.setdefault(n["deck"], dict(mature=0, young=0, new=0, unusable=0, total=0, history=0, kanji=0, with_sentence=0))
        d["kanji"] += bool(n.get("kanji"))
        d["with_sentence"] += bool(n["nt"] == VOCAB_NT and len(n["f"]) > 3 and n["f"][3])
        d["total"] += 1
        d["history"] += bool(n.get("log"))
        d["unusable" if entry(n, mapping.get(n["nt"], {})) is None else state(n)] += 1
    samples = {}
    for nt, fields in b["notetypes"].items():
        ns = [n for n in b["notes"] if n["nt"] == nt][:3]
        samples[nt] = dict(fields=fields, count=sum(1 for n in b["notes"] if n["nt"] == nt),
                           rows=[[clean(v)[:40] for v in n["f"]] for n in ns], parsed=[entry(n, mapping.get(nt, {})) for n in ns])
    imports = {r["deck"]: dict(r) for r in con.execute("SELECT * FROM imports WHERE batch_id=?", (batch_id,))}
    return dict(batch=row, mapping=mapping, decks=decks, samples=samples, imports=imports, skipped=b.get("skipped", []), fixed=FIXED,
                anki=row["kind"] in ("anki", "ankiconnect"))


def configure(con, batch_id: int, mapping: dict | None = None, modes: dict | None = None):
    if mapping is not None:
        con.execute("UPDATE import_batches SET mapping=? WHERE id=?", (json.dumps(mapping), batch_id))
    for deck, mode in (modes or {}).items():
        if mode in dict(MODES):
            con.execute("UPDATE imports SET mode=? WHERE batch_id=? AND deck=?", (mode, batch_id, deck))


# ---- applying ------------------------------------------------------------------------------------
def _clear_source(con, source_id: int | None):
    if source_id:
        con.execute("DELETE FROM occ WHERE source_id=?", (source_id,))
        con.execute("DELETE FROM sentences WHERE source_id=?", (source_id,))


def _drop_source(con, source_id: int | None):
    if source_id:
        _clear_source(con, source_id)
        for t in ("source_tags", "rejected", "unverified"):
            con.execute(f"DELETE FROM {t} WHERE source_id=?", (source_id,))
        con.execute("DELETE FROM cards WHERE source_id=?", (source_id,))
        con.execute("DELETE FROM sources WHERE id=?", (source_id,))


def apply(con, dic, batch_id: int, progress=lambda stage, done=0, total=0: None) -> dict:
    """Carry out each deck's choice. Safe to repeat: the batch's earlier effect is replaced."""
    row, b = load(con, batch_id)
    mapping, level = json.loads(row["mapping"]), int(db.setting(con, "level") or 0)
    by_deck: dict = {}
    for n in b["notes"]:
        by_deck.setdefault(n["deck"], []).append(n)
    total = dict(studied=0, mature=0, added=0, left_out=0, not_found=0, unusable=0, reviews=0)
    todo = con.execute("SELECT * FROM imports WHERE batch_id=?", (batch_id,)).fetchall()
    for k, imp in enumerate(todo):
        progress(f"Bringing in {imp['deck']}", k, len(todo))
        con.execute("DELETE FROM import_items WHERE import_id=?", (imp["id"],))
        con.execute("DELETE FROM reviews WHERE import_id=?", (imp["id"],))
        con.execute("DELETE FROM import_kanji WHERE import_id=?", (imp["id"],))
        _clear_source(con, imp["source_id"])
        st = dict(studied=0, mature=0, added=0, left_out=0, not_found=0, unusable=0, reviews=0)
        sid, mode, idx = imp["source_id"], imp["mode"], 0
        for n in by_deck.get(imp["deck"], []) if mode != "skip" else []:
            seed = n.get("seed")
            if n.get("kanji"):                    # a kanji studied as a character: progress only, for its readings
                if seed:
                    st["studied"] += 1
                    st["kanji"] = st.get("kanji", 0) + 1
                    st["mature"] += memory.band(seed["state"], seed["stability"]) == "mature"
                    con.execute("INSERT OR REPLACE INTO import_kanji VALUES(?,?,?,?,?,?,?,?)",
                                (imp["id"], n["kanji"], " ".join(n.get("readings") or []), seed["state"], seed["stability"],
                                 seed["difficulty"], seed["last_review"], seed["due"]))
                else:
                    st["left_out"] += 1
                continue
            e = entry(n, mapping.get(n["nt"], {}))
            if e is None:
                st["unusable"] += 1
                continue
            as_card = mode == "all" or (mode == "studied" and seed)      # gets a card here from the deck itself
            if not seed and not as_card:
                st["left_out"] += 1
                continue
            word, reading, sentence = e
            hit = dic.lookup(word, reading)
            if not hit:
                st["not_found"] += 1
                continue
            if as_card and not sid:
                sid = con.execute("INSERT INTO sources(kind, title, origin, status, added, options) VALUES('deck',?,?,'done',?,?)",
                                  (f"{imp['deck']} (imported)", f"import:{imp['id']}", db.now(), json.dumps(dict(imported=row["kind"])))).lastrowid
                db.set_tags(con, sid, "imported")
            wid = analyze._word_item(con, dic, word, hit["reading"] or reading or "", sid if as_card else None, level)
            if wid is None:
                st["not_found"] += 1
                continue
            if seed:
                st["studied"] += 1
                st["mature"] += memory.band(seed["state"], seed["stability"]) == "mature"
                ids, kana = [wid], hit["reading"]
                if kana and kana != word and hit.get("seq") and seed["stability"] >= memory.MATURE and dic.usually_kana(kana, hit["seq"]):
                    alias = analyze._word_item(con, dic, kana, kana, None, level)   # 沢山 is mature, so たくさん is too
                    ids += [alias] if alias else []
                con.executemany("INSERT OR REPLACE INTO import_items VALUES(?,?,?,?,?,?,?)",
                                [(imp["id"], i, seed["state"], seed["stability"], seed["difficulty"], seed["last_review"], seed["due"]) for i in ids])
                con.executemany("INSERT INTO reviews(item_id, skill, ts, rating, import_id) VALUES(?,'word',?,?,?)",
                                [(wid, ts, ease, imp["id"]) for ts, ease in n.get("log") or []])
                st["reviews"] += len(n.get("log") or [])
            else:
                con.execute("INSERT OR IGNORE INTO import_items(import_id, item_id, state) VALUES(?,?,'new')", (imp["id"], wid))
            if as_card:
                st["added"] += 1
                text = sentence if sentence and (word in sentence or word[:-1] in sentence) else word
                sen = con.execute("INSERT INTO sentences(source_id, idx, text, location) VALUES(?,?,?,?)", (sid, idx, text, "")).lastrowid
                con.execute("INSERT OR IGNORE INTO occ(item_id, sentence_id, source_id, n) VALUES(?,?,?,1)", (wid, sen, sid))
                idx += 1
        if sid and not idx:                       # nothing brought as cards any more: the deck leaves the library
            _drop_source(con, sid)
            sid = None
        if sid:
            con.execute("UPDATE sources SET n_sentences=?, status='done' WHERE id=?", (idx, sid))
        con.execute("UPDATE imports SET source_id=?, stats=? WHERE id=?", (sid, json.dumps(st), imp["id"]))
        for k in total:
            total[k] += st[k]
    progress("Updating your cards")
    analyze.rebuild_cards(con)
    return total


def reapply_all(con, dic):
    """After converting data from before 0.2.0: apply every import again so its states are carried over."""
    for b in con.execute("SELECT id FROM import_batches").fetchall():
        apply(con, dic, b["id"])
    db.set_setting(con, "imports_stale", 0)


def remove(con, batch_id: int):
    """Forget a batch: the states it brought are dropped (your level decides again) and its cards go."""
    row = con.execute("SELECT file FROM import_batches WHERE id=?", (batch_id,)).fetchone()
    for imp in con.execute("SELECT id, source_id FROM imports WHERE batch_id=?", (batch_id,)).fetchall():
        con.execute("DELETE FROM import_items WHERE import_id=?", (imp["id"],))
        con.execute("DELETE FROM reviews WHERE import_id=?", (imp["id"],))
        con.execute("DELETE FROM import_kanji WHERE import_id=?", (imp["id"],))
        _drop_source(con, imp["source_id"])
    con.execute("DELETE FROM imports WHERE batch_id=?", (batch_id,))
    con.execute("DELETE FROM import_batches WHERE id=?", (batch_id,))
    if row and row["file"]:
        (folder() / row["file"]).unlink(missing_ok=True)
    analyze.rebuild_cards(con)


def listing(con) -> list[dict]:
    out = []
    for b in con.execute("SELECT * FROM import_batches ORDER BY id DESC").fetchall():
        decks = [dict(r) | dict(stats=json.loads(r["stats"] or "{}")) for r in
                 con.execute("SELECT * FROM imports WHERE batch_id=? ORDER BY deck", (b["id"],))]
        out.append(dict(b) | dict(decks=decks, studied=sum(d["stats"].get("studied", 0) for d in decks),
                                  added=sum(d["stats"].get("added", 0) for d in decks)))
    return out


def summary(con) -> dict:
    """Items whose state now comes from an import, by kind."""
    return {k: n for k, n in con.execute(
        "SELECT i.kind, COUNT(*) FROM memory m JOIN items i ON i.id=m.item_id WHERE m.origin='import' AND m.skill=i.kind GROUP BY i.kind")}
