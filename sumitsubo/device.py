"""The deck a Sumitsubo Reader device studies from.

One folder, `sumitsubo/deck/`, to copy to the root of the device's card (or send over Wi-Fi):

    deck.ini      key=value: format, the day it was written, the settings the device follows
    sources.tsv   one line per source: id, priority, title, and what the device needs for "readable"
    cards.tsv     one line per card, twelve tab-separated columns:
                  0 id  1 kind (W word, K kanji, C collocation, X contrast, L listening)  2 item  3 rank
                  4 first source  5 starting memory state "stability,difficulty,last day,due day" (empty: new)
                  6 occurrences "source:count,..."  7 head (word, kanji, collocation; X: the kind of contrast)
                  8 reading (X: member item ids)  9 a list, items separated by \x1f (W, C, L: sentences
                  "source~place~text"; K: words "word~reading~meaning"; X: members
                  "head~word~reading~meaning~role~sentence")  10 meaning, senses separated by " | "
                  11 extra (W: register; C: the hint; K: one sentence)

Plain text, one record per line, so the device can read a card without holding the deck in memory
and without a JSON parser. The firmware's reader is lib/Sumitsubo/Deck.cpp in the sumitsubo-reader
repository; the two must change together (FORMAT).

Inside a sentence, control characters mark what the device draws specially:
    \\x01 ... \\x02          the tested part (underlined; blanked on a collocation card)
    \\x03 base \\x04 reading \\x05   furigana for another word you have not learned yet
"""
import json
import time
from pathlib import Path

from . import analyze, cards, config, contrast, db, memory

FORMAT = 1
KIND = dict(word="W", kanji="K", colloc="C", listen="L")
T0, T1, R0, R1, R2, SEP = "\x01", "\x02", "\x03", "\x04", "\x05", "\x1f"


def day(ts: float | None) -> int:
    """Days since 1 January 1970 in local time: the device schedules by day."""
    if ts is None:
        return 0
    lt = time.localtime(ts)
    return int((ts + lt.tm_gmtoff) // 86400)


def _clean(s) -> str:
    return " ".join(str(s or "").replace(SEP, " ").split())


TABLE = """CREATE TABLE IF NOT EXISTS device_cards(
  guid TEXT PRIMARY KEY, item_id INT, skill TEXT, seed TEXT,    -- what each card started from on the device
  sentences TEXT);                                              -- the ids of the sentences it was last written with, in order
-- A contrast card's own memory state, from its reviews on the device.
CREATE TABLE IF NOT EXISTS contrast_memory(contrast_id INTEGER PRIMARY KEY, stability REAL, difficulty REAL, last_review REAL, due REAL);
-- Words saved in the reader's dictionary (Matcha's sentence-mining file), each taken in once.
CREATE TABLE IF NOT EXISTS saved(guid TEXT PRIMARY KEY, word TEXT, reading TEXT, sentence TEXT, book TEXT, date TEXT, taken REAL);
-- Words you asked for by saving them: their cards are introduced before all others.
CREATE TABLE IF NOT EXISTS wanted(item_id INTEGER PRIMARY KEY, ts REAL);
-- Reviews made on the device, counted by day, for the statistics page.
CREATE TABLE IF NOT EXISTS device_days(day INTEGER PRIMARY KEY, reviews INT);"""


def tables(con):
    con.executescript(TABLE)
    if "sentences" not in {r[1] for r in con.execute("PRAGMA table_info(device_cards)")}:
        con.execute("ALTER TABLE device_cards ADD COLUMN sentences TEXT")


def _seed(con, guid: str, item_id: int, skill: str) -> str:
    """A card's starting memory state on the device. Fixed the first time the card is written: the
    device rebuilds its state from this plus its own log, so a later deck must not change it."""
    kept = con.execute("SELECT seed FROM device_cards WHERE guid=?", (guid,)).fetchone()
    if kept:
        return kept["seed"]
    m = con.execute("SELECT * FROM memory WHERE item_id=? AND skill=?", (item_id, skill)).fetchone()
    seed = "" if not m or m["state"] == "new" or not m["stability"] else \
        f"{m['stability']:.3f},{(m['difficulty'] or 5.0):.3f},{day(m['last_review'])},{day(m['due'])}"
    con.execute("INSERT INTO device_cards(guid, item_id, skill, seed) VALUES(?,?,?,?)", (guid, item_id, skill, seed))
    return seed


def _sentence(con, s: dict, item_id: int) -> str:
    """source~location~text, the tested part marked and other unlearned words given furigana."""
    before, target, after = s["parts"]
    others = [r for r in con.execute(
        """SELECT o.surface, i.lemma, i.reading FROM occ o JOIN items i ON i.id=o.item_id
           WHERE o.sentence_id=? AND i.kind='word' AND i.id<>? AND i.band IN ('new','young')""", (s["id"], item_id))
        if r["surface"] and r["surface"] == r["lemma"] and r["reading"] and r["reading"] != r["lemma"]]   # uninflected only

    def ruby(text: str) -> str:
        for r in sorted(others, key=lambda r: -len(r["surface"])):
            if r["surface"] in text and R0 not in text.split(r["surface"])[0][-1:]:
                text = text.replace(r["surface"], f"{R0}{r['surface']}{R1}{r['reading']}{R2}", 1)
        return text
    text = f"{ruby(_clean(before))}{T0}{_clean(target)}{T1}{ruby(_clean(after))}" if target else ruby(_clean(s["text"]))
    return f"{s['source_id']}~{_clean(s['location']).replace('~', '-')}~{text}"


# ---- what the reader is told about your words ---------------------------------------------------------
MAX_FILTER = 16 * 1024      # bytes: a filter is held in the device's memory while a book is open


def fnv(data: bytes) -> int:
    h = 0xcbf29ce484222325
    for b in data:
        h = ((h ^ b) * 0x100000001b3) & 0xFFFFFFFFFFFFFFFF
    return h


def _spread(h: int) -> tuple[int, int]:
    """Two well-mixed 32-bit numbers from one hash (FNV alone leaves like words too alike)."""
    M = 0xFFFFFFFFFFFFFFFF
    h ^= h >> 33
    h = (h * 0xFF51AFD7ED558CCD) & M
    h ^= h >> 33
    h = (h * 0xC4CEB9FE1A85EC53) & M
    h ^= h >> 33
    return h & 0xFFFFFFFF, (h >> 32) | 1


def word_filter(keys, bits_each: int = 16) -> bytes:
    """A Bloom filter over strings: "SBL1", the number of bits, the number of hashes, the bits.
    The device asks it "is this one of them?" and gets "certainly not" or "almost certainly"
    (lib/Sumitsubo/ReaderLink.cpp reads it with the same hash)."""
    keys = sorted({k for k in keys if k})
    m = max(64, min(MAX_FILTER * 8, len(keys) * bits_each))
    k = max(1, min(14, round(m / max(1, len(keys)) * 0.693)))
    bits = bytearray((m + 7) // 8)
    for key in keys:
        a, b = _spread(fnv(key.encode("utf-8")))
        for i in range(k):
            bit = (a + i * b) % m
            bits[bit >> 3] |= 1 << (bit & 7)
    return b"SBL1" + m.to_bytes(4, "little") + bytes([k]) + bytes(bits)


def filter_has(data: bytes, key: str) -> bool:
    m, k = int.from_bytes(data[4:8], "little"), data[8]
    a, b = _spread(fnv(key.encode("utf-8")))
    return all(data[9 + (((a + i * b) % m) >> 3)] & (1 << (((a + i * b) % m) & 7)) for i in range(k))


def _stem(lemma: str, reading: str) -> tuple[str, str]:
    """The part of a word furigana is written over: 考える/かんがえる -> 考/かんが."""
    from .dictdata import hira  # noqa: PLC0415
    reading = hira(reading)
    while lemma and reading and lemma[-1] == reading[-1] and "\u3040" <= lemma[-1] <= "\u309f":
        lemma, reading = lemma[:-1], reading[:-1]
    return lemma, reading


def write_reader_link(con, root: Path):
    """Beside the deck: which words have a card, which readings you no longer need furigana for,
    and for each book sent to the reader how readable it is and which of its words are new."""
    from .dictdata import hira, kanji_of  # noqa: PLC0415
    words = [r[0] for r in con.execute("SELECT DISTINCT i.lemma FROM items i JOIN cards c ON c.item_id=i.id WHERE i.kind='word' AND i.band<>'suspended'")]
    (root / "words.bloom").write_bytes(word_filter(words, 14))
    known = set()
    for r in con.execute("SELECT lemma, reading FROM items WHERE kind='word' AND band='mature' AND reading IS NOT NULL"):
        if kanji_of(r["lemma"]):
            known.add(f"{r['lemma']}\t{hira(r['reading'])}")
            stem, rd = _stem(r["lemma"], r["reading"])
            if stem and rd and kanji_of(stem):
                known.add(f"{stem}\t{rd}")
    for r in con.execute("SELECT lemma, reading FROM items WHERE kind='kanji' AND band='mature'"):
        known.add(f"{r['lemma']}\t{hira(r['reading'] or '')}")        # one kanji with one of its readings
    (root / "known.bloom").write_bytes(word_filter(known, 12))
    for old in root.glob("new-*.bloom"):
        old.unlink()
    lines = []
    for s in con.execute("SELECT id, book FROM sources WHERE status='done' AND book IS NOT NULL ORDER BY id"):
        fresh = {r[0] for r in con.execute(
            """SELECT DISTINCT o.surface FROM occ o JOIN items i ON i.id=o.item_id
               WHERE o.source_id=? AND i.kind='word' AND i.band='new' AND o.surface IS NOT NULL""", (s["id"],))
            if r[0] and (kanji_of(r[0]) or len(r[0]) >= 3)}          # a short kana word would be marked inside other words
        n_new = con.execute("""SELECT COUNT(DISTINCT o.item_id) FROM occ o JOIN items i ON i.id=o.item_id
                               WHERE o.source_id=? AND i.kind='word' AND i.band='new'""", (s["id"],)).fetchone()[0]
        (root / f"new-{s['id']}.bloom").write_bytes(word_filter(fresh, 20))
        cov = analyze.coverage(con, s["id"])
        lines.append("\t".join(str(x) for x in (Path(s["book"]).name, s["id"], round(cov["readable"] * 10), n_new,
                                               max((len(x.encode("utf-8")) for x in fresh), default=0))))
    (root / "books.tsv").write_text("".join(l + "\n" for l in lines), encoding="utf-8")
    # forms.tsv: for each word card not yet mature whose word is in a book on the reader, how the
    # word is written in those books and the readings furigana gives it. The device uses it between
    # syncs: a word you have started is no longer marked as new, and one that became mature loses
    # its furigana, without waiting for the desktop. Columns: card id, written forms (new words
    # only), "base\x1ereading" keys; lists separated by \x1f.
    out = []
    rows = con.execute(
        """SELECT d.guid, i.id, i.lemma, i.reading, i.band FROM device_cards d JOIN items i ON i.id=d.item_id
           WHERE d.skill='word' AND i.kind='word' AND i.band IN ('new', 'young')
             AND i.id IN (SELECT o.item_id FROM occ o JOIN sources s ON s.id=o.source_id WHERE s.status='done' AND s.book IS NOT NULL)
           ORDER BY d.guid""").fetchall()
    for r in rows:
        forms = sorted({x[0] for x in con.execute(
            """SELECT DISTINCT o.surface FROM occ o JOIN sources s ON s.id=o.source_id
               WHERE o.item_id=? AND s.book IS NOT NULL AND o.surface IS NOT NULL""", (r["id"],))
            if x[0] and (kanji_of(x[0]) or len(x[0]) >= 3)}) if r["band"] == "new" else []
        keys = []
        if r["reading"] and kanji_of(r["lemma"]):
            keys.append(f"{r['lemma']}\x1e{hira(r['reading'])}")
            stem, rd = _stem(r["lemma"], r["reading"])
            if stem and rd and kanji_of(stem) and stem != r["lemma"]:
                keys.append(f"{stem}\x1e{rd}")
        if forms or keys:
            out.append(f"{r['guid']}\t{chr(31).join(forms)}\t{chr(31).join(keys)}\n")
    (root / "forms.tsv").write_text("".join(out), encoding="utf-8")


def _occ(con, item_ids) -> str:
    ids = list(item_ids)
    rows = con.execute(f"SELECT source_id, SUM(n) FROM occ WHERE item_id IN ({','.join('?' * len(ids))}) GROUP BY source_id", ids).fetchall() if ids else []
    return ",".join(f"{r[0]}:{r[1]}" for r in rows)


BIG_EM = 88          # pixels: the size of the word at the top of a kanji card on the reader


def big_font(con) -> tuple[str, int] | None:
    """The font file the large characters are drawn from: the one named in the setting "big_font",
    or a Japanese serif found on this computer."""
    import shutil  # noqa: PLC0415
    import subprocess  # noqa: PLC0415
    named = db.setting(con, "big_font")
    if named and Path(named).exists():
        return named, 0
    if shutil.which("fc-match"):
        for want in ("Noto Serif CJK JP", "Noto Serif JP", "serif:lang=ja", ":lang=ja"):
            try:
                out = subprocess.run(["fc-match", "-f", "%{file}|%{index}|%{lang}", want], capture_output=True, text=True, timeout=20).stdout
            except (OSError, subprocess.SubprocessError):
                break
            file, _, rest = out.partition("|")
            index, _, lang = rest.partition("|")
            if file and Path(file).exists() and "ja" in lang.split("|"):
                return file, int(index or 0)
    for known in ("/System/Library/Fonts/ヒラギノ明朝 ProN.ttc", "/System/Library/Fonts/Hiragino Mincho ProN.ttc", "C:/Windows/Fonts/yumin.ttf", "C:/Windows/Fonts/msmincho.ttc"):
        if Path(known).exists():
            return known, 0
    return None


def write_big(con, root: Path, chars: set) -> int:
    """big.bin: each character of the words shown large on kanji cards, drawn here at the size the
    reader shows it, in four levels of grey. The reader has no room to draw outlines itself, and
    enlarging its 18-point letters leaves their edges ragged. Layout: "SBG2", em and line height
    (uint16), count (uint32); an index of (code point, offset) pairs, sorted; then per character
    width, height, left, top, advance (one byte each, left and top signed), the length of what
    follows (uint16), and its pixels row by row as runs: each byte is a level (0 white to 3
    black) in its top two bits and the run's length less one in the other six. Returns how many were written (0: no font found)."""
    import struct  # noqa: PLC0415
    out = root / "big.bin"
    found = big_font(con) if chars else None
    if not found:
        out.unlink(missing_ok=True)
        return 0
    from PIL import Image, ImageDraw, ImageFont  # noqa: PLC0415
    try:
        font = ImageFont.truetype(found[0], BIG_EM, index=found[1])
    except OSError:
        out.unlink(missing_ok=True)
        return 0
    ascent, descent = font.getmetrics()
    records = []
    for ch in sorted(chars):
        box = font.getbbox(ch)
        w, h = box[2] - box[0], box[3] - box[1]
        if w <= 0 or h <= 0 or w > 255 or h > 255:
            continue
        im = Image.new("L", (w, h), 0)
        ImageDraw.Draw(im).text((-box[0], -box[1]), ch, font=font, fill=255)
        levels = [(v * 3 + 127) // 255 for v in im.tobytes()]
        runs, i = bytearray(), 0
        while i < len(levels):
            n = 1
            while i + n < len(levels) and levels[i + n] == levels[i] and n < 64:
                n += 1
            runs.append(levels[i] << 6 | (n - 1))
            i += n
        records.append((ord(ch), struct.pack("<BBbbBH", w, h, max(-128, min(127, box[0])), max(-128, min(127, box[1])),
                                             min(255, round(font.getlength(ch))), len(runs)) + bytes(runs)))
    head = 12 + 8 * len(records)
    index, body = bytearray(), bytearray()
    for cp, rec in records:
        index += struct.pack("<II", cp, head + len(body))
        body += rec
    out.write_bytes(b"SBG2" + struct.pack("<HHI", BIG_EM, ascent + descent, len(records)) + bytes(index) + bytes(body))
    return len(records)


def write(con, dest: Path | None = None) -> Path:
    root = (dest or config.data_dir() / "device") / "sumitsubo" / "deck"
    root.mkdir(parents=True, exist_ok=True)
    tables(con)
    wanted = {r[0] for r in con.execute("SELECT item_id FROM wanted")}
    (root / "deck.txt").unlink(missing_ok=True)       # its name before 0.4.4; Matcha's library listed it as a book
    now, n = db.now(), 0
    with_card = set()
    large: set = set()                                  # the characters shown large on kanji cards
    with open(root / "cards.tsv", "w", encoding="utf-8", newline="\n") as f:
        def line(*cols):
            f.write("\t".join(_clean(c) if i not in (9,) else str(c) for i, c in enumerate(cols)) + "\n")
        for c in con.execute("SELECT c.* FROM cards c JOIN items i ON i.id=c.item_id WHERE i.band<>'suspended' ORDER BY c.source_id, c.rank"):
            v = cards.view(con, c)
            k = KIND[v["kind"]]
            skill = "listen" if v["kind"] == "listen" else ("word" if v["kind"] == "colloc" else v["kind"])
            it = con.execute("SELECT kind FROM items WHERE id=?", (v["item_id"],)).fetchone()
            seed = _seed(con, v["guid"], v["item_id"], "listen" if v["kind"] == "listen" else it["kind"])
            sents = SEP.join(_sentence(con, s, v["item_id"]) for s in v["sentences"])
            con.execute("UPDATE device_cards SET sentences=? WHERE guid=?", (",".join(str(s["id"]) for s in v["sentences"]), v["guid"]))
            first = "1" if v["item_id"] in wanted else ""          # saved in the reader: before every other new card
            if k in "WL":
                with_card.add(v["item_id"]) if k == "W" else None
                line(v["guid"], k, v["item_id"], v["rank"], v["source_id"] or 0, seed, _occ(con, [v["item_id"]]),
                     v["lemma"], v["reading"], sents, v["gloss"], v["register"] or "", first)
            elif k == "C":
                with_card.add(v["item_id"])
                line(v["guid"], k, v["item_id"], v["rank"], v["source_id"] or 0, seed, _occ(con, [v["item_id"]]),
                     v["lemma"], v["reading"], sents, v["gloss"], v["hint"] or "", first)
            else:   # kanji in context: the words that use this reading, the one from your source first
                words = SEP.join(f"{w['lemma']}~{w['reading']}~{_clean(w['gloss']).replace('~', '-')}" for w in v["words"])
                large.update(v["words"][0]["lemma"] if v["words"] else v["lemma"])
                one = con.execute("""SELECT s.id, s.text, s.location, s.source_id, o.surface FROM occ o JOIN sentences s ON s.id=o.sentence_id
                                     WHERE o.item_id=? ORDER BY LENGTH(s.text) LIMIT 1""", (v["words"][0]["id"],)).fetchone() if v["words"] else None
                sent = ""
                if one and one["surface"] and one["surface"] in one["text"]:
                    a, b = one["text"].split(one["surface"], 1)
                    sent = _sentence(con, dict(id=one["id"], parts=(a, one["surface"], b), text=one["text"], location=one["location"], source_id=one["source_id"]), v["words"][0]["id"])
                line(v["guid"], k, v["item_id"], v["rank"], v["source_id"] or 0, seed, _occ(con, [w["id"] for w in v["words"]]),
                     v["lemma"], v["reading"], words, v["gloss"], sent, "")
            n += 1
        for c in contrast.listing(con, limit=10**9):          # contrast cards: one line per set
            ms = c["members"]
            if len(ms) < 2:
                continue
            parts = []
            for m in ms:
                w = m.get("word") or {}
                s = con.execute("""SELECT s.text, o.surface FROM occ o JOIN sentences s ON s.id=o.sentence_id
                                   WHERE o.item_id=? ORDER BY LENGTH(s.text) LIMIT 1""", (m["id"],)).fetchone() if c["kind"] in ("sound", "keigo") else None
                sent = ""
                if s and s["surface"] and s["surface"] in s["text"] and len(s["text"]) <= 40:
                    a, b = s["text"].split(s["surface"], 1)
                    sent = f"{_clean(a)}{T0}{s['surface']}{T1}{_clean(b)}"
                parts.append("~".join(_clean(x).replace("~", "-") for x in (
                    m["lemma"], w.get("lemma") or m["lemma"], w.get("reading") or m["reading"], w.get("gloss") or m["gloss"], m.get("role") or "")) + "~" + sent)
            line(c["guid"], "X", 0, 0, 0, "", _occ(con, [m["id"] for m in ms]), c["kind"], ",".join(str(m["id"]) for m in ms), SEP.join(parts), "", "", "")
            n += 1
    with open(root / "sources.tsv", "w", encoding="utf-8", newline="\n") as f:
        for s in con.execute("SELECT * FROM sources WHERE status='done' ORDER BY id"):
            total, fixed = 0, 0.0
            for r in con.execute("""SELECT i.id, SUM(o.n) AS n, m.stability, m.last_review FROM occ o JOIN items i ON i.id=o.item_id
                                    LEFT JOIN memory m ON m.item_id=i.id AND m.skill=i.kind
                                    WHERE o.source_id=? AND i.kind='word' AND i.band<>'suspended' GROUP BY i.id""", (s["id"],)):
                total += r["n"]
                if r["id"] not in with_card:       # no card on the device: its share of "readable" is fixed at export
                    fixed += r["n"] * memory.recall(r["stability"], r["last_review"], now)
            added = analyze.normalise_score(json.loads(s["added_score"])) if s["added_score"] else None
            started = con.execute("""SELECT SUM(i.band<>'new'), COUNT(*) FROM cards c JOIN items i ON i.id=c.item_id
                                     WHERE c.source_id=? AND c.kind IN ('word','colloc')""", (s["id"],)).fetchone()
            f.write("\t".join(str(x) for x in (s["id"], s["priority"] or 2, _clean(s["title"]), total, round(fixed * 1000),
                                                round((added or {}).get("readable", 0) * 10), started[0] or 0, started[1] or 0)) + "\n")
    write_reader_link(con, root)
    write_big(con, root, large)
    (root / "deck.ini").write_text("\n".join(f"{k}={v}" for k, v in dict(
        format=FORMAT, written_day=day(now), cards=n, log_lines=int(db.setting(con, "device_log_lines") or 0),
        daily_new=int(db.setting(con, "daily_budget") or 15),
        more_new=db.setting(con, "more_new") or "once", more_count=5,
        contrast_when=db.setting(con, "contrast_when") or "mixup").items()) + "\n", encoding="utf-8")
    return root.parent


# ---- what comes back from the device ---------------------------------------------------------------
LOG = "sumitsubo/log/events.csv"


def _noon(d: int) -> float:
    """Local noon of a device day, as a timestamp."""
    return time.mktime(time.gmtime(d * 86400)[:3] + (12, 0, 0, 0, 0, -1))


def read_log(con, text: str) -> dict:
    """Take in the device's log (every verdict and mark, in order). The whole log is replayed each
    time from the states the cards were written with, exactly as the device does, so reading it
    twice changes nothing. Returns what was found."""
    import datetime as dt  # noqa: PLC0415

    from fsrs import Card, Rating, Scheduler, State  # noqa: PLC0415
    tables(con)
    lines = text.splitlines()
    handled = int(db.setting(con, "device_log_lines") or 0)      # marks on sentences are resolved once, against the deck they were made on
    if handled > len(lines):
        handled = 0                         # a shorter log is another log (a new card, a reset): all of it is new
    sch = Scheduler(learning_steps=(), relearning_steps=(), enable_fuzzing=False)      # as lib/Sumitsubo/Fsrs.cpp
    at = lambda d: dt.datetime(1970, 1, 1, 12, tzinfo=dt.timezone.utc) + dt.timedelta(days=d)   # noqa: E731
    cards, stats = {}, dict(reviews=0, cards=0, unknown=0, suspended=0, known=0, bad=0, contrast=0, last_day=0)
    marks = []
    per_day: dict = {}
    for number, line in enumerate(lines):
        c = line.strip().split(",")
        if len(c) < 3 or not c[0].isdigit():
            continue
        d, guid, what = int(c[0]), c[1], c[2]
        row = con.execute("SELECT * FROM device_cards WHERE guid=?", (guid,)).fetchone()
        if not row:
            x = con.execute("SELECT id FROM contrasts WHERE guid=?", (guid,)).fetchone()
            if not x:
                stats["unknown"] += 1      # a card from a deck this program did not write, or a contrast card since removed
                continue
            row = dict(contrast=x["id"], seed="", item_id=None, skill=None, sentences="")
        if guid not in cards:
            card = Card()
            if row["seed"]:
                st, df, last, due = row["seed"].split(",")
                card = Card(state=State.Review, step=None, stability=float(st), difficulty=float(df), last_review=at(int(last)), due=at(int(due)))
            cards[guid] = [card, row, 0]            # the card, its record, verdicts that stand
        entry = cards[guid]
        if what in ("1", "2", "3", "4"):
            entry[0], _ = sch.review_card(entry[0], Rating(int(what)), at(d))
            entry[2] += 1
            per_day[d] = per_day.get(d, 0) + 1
            stats["reviews"] += 1
            stats["last_day"] = max(stats["last_day"], d)
        elif what == "undo" and len(c) >= 7:
            st, df, last, due = float(c[3]), float(c[4]), int(c[5]), int(c[6])
            entry[0] = Card(state=State.Review, step=None, stability=st, difficulty=df, last_review=at(last), due=at(due)) if st > 0 else Card()
            entry[2] = max(0, entry[2] - 1)                    # a take-back cancels the verdict before it
        elif what in ("suspend", "known") and row["item_id"]:
            marks.append((row["item_id"], what))
        elif what == "bad" and len(c) >= 4 and number >= handled and c[3].isdigit():
            ids = [int(x) for x in (row["sentences"] or "").split(",") if x]
            s = con.execute("SELECT source_id, text FROM sentences WHERE id=?", (ids[int(c[3])],)).fetchone() if int(c[3]) < len(ids) else None
            if s:                          # as "Bad sentence" on the cards page: it leaves every card
                stats["bad"] += con.execute("INSERT OR IGNORE INTO rejected(source_id, text) VALUES(?,?)", (s["source_id"], s["text"])).rowcount
    con.execute("DELETE FROM memory WHERE origin='review'")
    con.execute("DELETE FROM contrast_memory")
    con.execute("DELETE FROM device_days")
    con.executemany("INSERT INTO device_days VALUES(?,?)", list(per_day.items()))
    db.set_setting(con, "device_log_lines", len(lines))
    for card, row, touched in cards.values():
        if not touched or card.stability is None:
            continue
        last, due = (card.last_review - at(0)).days, (card.due - at(0)).days
        if "contrast" in row.keys():
            con.execute("INSERT OR REPLACE INTO contrast_memory VALUES(?,?,?,?,?)",
                        (row["contrast"], float(card.stability), float(card.difficulty), _noon(last), _noon(due)))
            stats["contrast"] += 1
            continue
        con.execute("INSERT OR REPLACE INTO memory VALUES(?,?,?,?,?,?,?,?)",
                    (row["item_id"], row["skill"], "review", float(card.stability), float(card.difficulty), _noon(last), _noon(due), "review"))
        stats["cards"] += 1
    for item_id, what in marks:
        item = con.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if item:
            analyze.set_status(con, item_id, "ignored" if what == "suspend" else "known")
            stats["suspended" if what == "suspend" else "known"] += 1
    analyze.rebuild_cards(con)
    return stats


# ---- words saved in the reader -----------------------------------------------------------------------
SAVED = "sentence-mining/sentences-ja.csv"       # written by Matcha's Word Lookup when you press Save
SAVED_TITLE = "Saved in the reader"


def _book_source(con, book: str):
    """The source a book of the reader belongs to, when this program already has it: by its title,
    or by the name of the file that was sent to the reader."""
    book = (book or "").strip()
    if not book:
        return None
    for r in con.execute("SELECT id, title, book FROM sources WHERE kind<>'saved' AND status='done'"):
        names = {r["title"] or "", Path(r["book"]).stem if r["book"] else ""}
        if any(n == book or n.startswith(book + "（") or n.startswith(book + " - ") or n.startswith(book + " (") for n in names if n):
            return r["id"]
    return None


def read_saved(con, text: str, engine=None) -> dict:
    """Take in the words saved from the reader's dictionary. Each is put before every other new card.
    A word saved in a book this program already has needs nothing more. For any other book, the saved
    sentences become a source of their own, named after the book, so the word gets its card there.
    Rows are taken in once; engine() returns (dictionary, parser) and is only called when needed."""
    import csv  # noqa: PLC0415
    import html  # noqa: PLC0415
    import re  # noqa: PLC0415

    from . import ingest  # noqa: PLC0415
    tables(con)
    new = []
    for row in csv.reader(l for l in text.splitlines(keepends=True) if not l.startswith("#")):
        if len(row) < 8 or not row[1].strip():
            continue
        guid, word, reading, sentence, book, date = row[0], row[1].strip(), row[2].strip(), row[3], row[5].strip(), row[7]
        sentence = " ".join(html.unescape(re.sub(r"<[^>]+>", "", sentence)).split())
        if con.execute("INSERT OR IGNORE INTO saved VALUES(?,?,?,?,?,?,?)", (guid, word, reading, sentence, book, date, db.now())).rowcount:
            new.append((word, reading, book))
    old = con.execute("SELECT id FROM sources WHERE kind='saved' AND origin=?", (SAVED,)).fetchall()      # the one catch-all source of 0.6 and 0.7
    stats = dict(saved=len(new), moved=0, added=0, books=0)
    if not new and not old:
        return stats
    before = {(w, r): _item_with_card(con, w, r) for w, r, _ in new}
    books = {b for _, _, b in new}
    if old:                                         # split it by book
        for r in old:
            ingest.delete_source(con, r["id"])
        books = {r[0] for r in con.execute("SELECT DISTINCT book FROM saved")}
    todo = []
    for book in sorted(books):
        if _book_source(con, book) or not con.execute("SELECT 1 FROM saved WHERE book=? AND sentence<>''", (book,)).fetchone():
            continue                                # a book this program has: its words have their cards already
        origin = "saved:" + book
        src = con.execute("SELECT id FROM sources WHERE kind='saved' AND origin=?", (origin,)).fetchone()
        todo.append(src["id"] if src else con.execute(
            "INSERT INTO sources(kind, title, origin, status, added, options, priority) VALUES('saved',?,?,?,?,'{}',1)",
            (book or SAVED_TITLE, origin, "queued", db.now())).lastrowid)
    if todo and engine:
        con.commit()                                # a source is read on its own connections
        dic, parser = engine()
        for sid in todo:
            ingest.process(sid, dic, parser)
        stats["books"] = len(todo)
    for w, r, _ in new:
        item = _item_with_card(con, w, r)
        if item:
            con.execute("INSERT OR REPLACE INTO wanted VALUES(?,?)", (item, db.now()))
            stats["moved" if before[(w, r)] else "added"] += 1
    return stats


def _item_with_card(con, word: str, reading: str) -> int | None:
    from .dictdata import hira  # noqa: PLC0415
    rows = con.execute("""SELECT i.id, i.reading FROM items i WHERE i.kind='word' AND i.lemma=?
                          AND EXISTS(SELECT 1 FROM cards c WHERE c.item_id=i.id)""", (word,)).fetchall()
    rows.sort(key=lambda r: hira(r["reading"] or "") != hira(reading or ""))
    return rows[0]["id"] if rows else None


# ---- a new card ---------------------------------------------------------------------------------------
ASSETS = "https://github.com/eszter007/matcha-reader-assets/releases/download/"
PACKS = (   # name, address, the folder it adds to the card, a file that shows it is there, what it is for
    ("dictionary", ASSETS + "dictionaries-2.0/japanese-dictionaries.zip", "dictionaries", "dictionaries/jp/vocab.dat",
     "the Japanese dictionary for Word Lookup in the reader (about 130 MB on the card)"),
    ("fonts", ASSETS + "fonts-2.0/japanese-fonts.zip", "fonts", None,
     "the Japanese fonts, for good-looking text and rare kanji (about 95 MB)"),
)


def card_setup(con, card: Path, say=print, fonts: bool = True, get=None) -> Path:
    """Put on a card what a Sumitsubo Reader needs once: Matcha's Japanese dictionary and fonts (from
    Matcha's own release files), and the deck. What is there already is left alone."""
    import urllib.request  # noqa: PLC0415
    import zipfile  # noqa: PLC0415
    card = Path(card).expanduser()
    card.mkdir(parents=True, exist_ok=True)

    def fetch(url: str, dest: Path):
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Sumitsubo"}), timeout=120) as r, open(dest, "wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
    for name, url, folder, proof, what in PACKS:
        if name == "fonts" and not fonts:
            continue
        there = (card / proof).exists() if proof else (card / folder).is_dir() and any((card / folder).iterdir())
        if there:
            say(f"Already on the card: {what.split(',')[0].split(' (')[0]}.")
            continue
        z = config.data_dir() / "downloads" / url.rsplit("/", 1)[1]
        if not z.exists():
            say(f"Downloading {what} ...")
            part = z.with_suffix(".part")
            (get or fetch)(url, part)
            part.replace(z)
        n = 0
        with zipfile.ZipFile(z) as zf:
            for info in zf.infolist():
                target = (card / info.filename).resolve()
                if info.is_dir() or not info.filename.startswith(folder + "/") or card.resolve() not in target.parents:
                    continue                    # only that folder, and nothing that would land outside the card
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as out:
                    while chunk := src.read(1 << 20):
                        out.write(chunk)
                n += 1
        say(f"Copied {n} files to {card / folder}.")
    out = write(con, card)
    say(f"Wrote the deck to {out}.")
    return card


BOOKS = "Books"         # where books for the reader's library go on the card


def books(con) -> list[Path]:
    """The files of the sources marked "send to the reader"."""
    out = []
    for r in con.execute("SELECT book FROM sources WHERE to_reader=1 AND book IS NOT NULL AND status='done' ORDER BY id"):
        p = config.resolve(r["book"])
        if p and p.exists() and p.name not in {q.name for q in out}:
            out.append(p)
    return out


def keep_log(text: str) -> str:
    """A copy of the device's log stays here: an export to Anki takes the review history from it."""
    try:
        (config.data_dir() / "device-log.csv").write_text(text, encoding="utf-8")
    except OSError:
        pass
    return text


def history(con) -> dict:
    """Every review made on the device, from the kept log: {card guid: [(day, rating, days to the next review)]}.
    A verdict that was taken back is left out."""
    from datetime import datetime, timedelta, timezone  # noqa: PLC0415

    from fsrs import Card, Rating, Scheduler, State  # noqa: PLC0415
    p = config.data_dir() / "device-log.csv"
    if not p.exists():
        return {}
    tables(con)
    sched = Scheduler(learning_steps=(), relearning_steps=(), enable_fuzzing=False)
    at = lambda d: datetime.fromtimestamp(d * 86400 + 43200, timezone.utc)  # noqa: E731
    cards, out = {}, {}

    def start(guid):
        row = con.execute("SELECT seed FROM device_cards WHERE guid=?", (guid,)).fetchone()
        seed = (row["seed"] if row else "") or ""
        if not seed:
            return Card()
        st, df, last, due = seed.split(",")
        return Card(state=State.Review, stability=float(st), difficulty=float(df), last_review=at(int(last)), due=at(int(due)))

    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        c = line.strip().split(",")
        if len(c) < 3 or not c[0].isdigit():
            continue
        d, guid = int(c[0]), c[1]
        if c[2] in ("1", "2", "3", "4"):
            if guid not in cards:
                cards[guid] = start(guid)
            cards[guid], _ = sched.review_card(cards[guid], Rating(int(c[2])), at(d))
            out.setdefault(guid, []).append((d, int(c[2]), max(1, (cards[guid].due - at(d)).days)))
        elif c[2] == "undo" and out.get(guid):
            out[guid].pop()
            card = start(guid)                      # its state again, from what remains
            for dd, rr, _ in out[guid]:
                card, _ = sched.review_card(card, Rating(rr), at(dd))
            cards[guid] = card
    return {g: h for g, h in out.items() if h}


def told(st: dict | None, saved: dict | None) -> list[str]:
    """What a sync took in, in words."""
    out = []
    if st:
        extra = [f"{st['contrast']} contrast cards" if st["contrast"] else "", f"{st['suspended']} suspended" if st["suspended"] else "",
                 f"{st['known']} marked known" if st["known"] else "", f"{st['bad']} sentences marked bad and taken off their cards" if st["bad"] else "",
                 f"{st['unknown']} lines were for cards no longer here" if st["unknown"] else ""]
        out.append(f"Took in {st['reviews']} reviews of {st['cards']} cards from the device" + "".join("; " + e for e in extra if e) + ".")
    else:
        out.append("No reviews on the device yet.")
    if saved and saved["saved"]:
        out.append(f"{saved['saved']} words saved in the reader: {saved['moved']} had a card and now come first, "
                   f"{saved['added']} got a card from their sentence" + (f" ({saved['books']} books added as sources)" if saved.get("books") else "") +
                   (f", {saved['saved'] - saved['moved'] - saved['added']} are not in the dictionary" if saved["saved"] - saved["moved"] - saved["added"] else "") + ".")
    return out


def sync_folder(con, card: Path, say=lambda s: None, engine=None) -> tuple[dict | None, Path]:
    """A card (or a copy of one) in a folder: take in its log and the words saved in the reader, then
    write the deck and copy the books that are not there yet."""
    import shutil  # noqa: PLC0415
    log = card / LOG
    stats = read_log(con, keep_log(log.read_text(encoding="utf-8", errors="replace"))) if log.exists() else None
    words = card / SAVED
    saved = read_saved(con, words.read_text(encoding="utf-8", errors="replace"), engine) if words.exists() else None
    for line in told(stats, saved):
        say(line)
    out = write(con, card)
    for b in books(con):
        dest = card / BOOKS / b.name
        if not dest.exists() or dest.stat().st_size != b.stat().st_size:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(b, dest)
            say(f"Copied {b.name} to the library ({b.stat().st_size // 1024} KB).")
    return stats, out


def _http(url: str, data: bytes | None = None, headers: dict | None = None, timeout: int = 60) -> bytes:
    import urllib.request  # noqa: PLC0415
    req = urllib.request.Request(url, data=data, headers=headers or {})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))       # the device is on the local network
    with opener.open(req, timeout=timeout) as r:
        return r.read()


def sync_wifi(con, base: str = "http://sumitsubo.local", say=print, engine=None) -> dict | None:
    """Over Wi-Fi, with the device on its File Transfer screen: fetch the log, take it in, send the
    deck. Uses the web server Matcha already has (docs/webserver-endpoints.md there)."""
    import tempfile  # noqa: PLC0415
    import urllib.error  # noqa: PLC0415
    import urllib.parse  # noqa: PLC0415
    import uuid  # noqa: PLC0415
    base = base.rstrip("/")
    status = json.loads(_http(base + "/api/status", timeout=10))
    say(f"Found a device: {status.get('device', '?')}, firmware {status.get('version', '?')}.")
    stats = None
    try:
        text = _http(base + "/download?path=" + urllib.parse.quote("/" + LOG)).decode("utf-8", "replace")
        stats = read_log(con, keep_log(text))
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
    saved = None
    try:
        saved = read_saved(con, _http(base + "/download?path=" + urllib.parse.quote("/" + SAVED)).decode("utf-8", "replace"), engine)
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
    for line in told(stats, saved):
        say(line)
    def mkdir(parent, name):
        try:
            _http(base + "/mkdir", urllib.parse.urlencode(dict(name=name, path=parent)).encode(),
                  {"Content-Type": "application/x-www-form-urlencoded"})
        except urllib.error.HTTPError:
            pass                                  # it is there already

    def upload(f: Path, folder: str):
        boundary = uuid.uuid4().hex
        name = f.name.replace('"', "")
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\n"
                f"Content-Type: application/octet-stream\r\n\r\n").encode() + f.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        _http(base + "/upload?path=" + urllib.parse.quote(folder) + "&overwrite=1", body,
              {"Content-Type": f"multipart/form-data; boundary={boundary}"}, timeout=300)
    with tempfile.TemporaryDirectory() as tmp:
        root = write(con, Path(tmp)) / "deck"
        mkdir("/", "sumitsubo"), mkdir("/sumitsubo", "deck")
        for f in sorted(root.iterdir()):
            upload(f, "/sumitsubo/deck")
            say(f"Sent {f.name} ({f.stat().st_size // 1024} KB).")
    todo = books(con)
    if todo:
        mkdir("/", BOOKS)
        try:
            there = {e.get("name"): e.get("size") for e in json.loads(_http(base + "/api/files?path=/" + BOOKS, timeout=30))}
        except (urllib.error.HTTPError, ValueError):
            there = {}
        for b in todo:
            if there.get(b.name) != b.stat().st_size:
                upload(b, "/" + BOOKS)
                say(f"Sent {b.name} to the library ({b.stat().st_size // 1024} KB).")
    return stats
