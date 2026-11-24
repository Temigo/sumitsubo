"""From sentences to words and kanji readings, their cards, and difficulty scores.

How well an item is known lives in memory.py; this module reads each item's band (new, young,
mature, suspended) and, for scores, its chance of being recalled today."""
import json
from pathlib import Path
import re
import uuid

import datetime as dt
import math

from . import config, contrast, db, memory, register
from .dictdata import PARTICLES
from .dictdata import hira, kanji_of
from .furigana import align

GRAMMAR_POS = ("exp", "expressions", "prt", "particle", "conj", "conjunction", "cop", "copula")
AUX_POS = ("aux", "auxiliary")
KANA_ONLY = re.compile(r"[぀-ヿー]+")
CONTEXT_WORDS = 5


def is_grammar(form: str, pos: str | None) -> bool:
    """Short kana-only grammatical expressions (について, んです) are set aside: grammar comes later.
    A verb that is also used as an auxiliary (いらっしゃる, おる) is a word like any other."""
    if not KANA_ONLY.fullmatch(form):
        return False
    parts = [p.strip() for p in (pos or "").split(",") if p.strip()]
    if any(p.startswith(GRAMMAR_POS) for p in parts):
        return True
    return bool(parts) and all(p.startswith(AUX_POS) for p in parts)
UNFAMILIAR = "('new','young')"      # not yet reliably readable: what a sentence should contain little of


# ---- levels ------------------------------------------------------------------------------------
_WALLER: dict = {}


def kanji_level(info: dict | None, literal: str | None = None) -> int | None:
    """JLPT level (5 = N5 .. 1 = N1) of a kanji.

    From Jonathan Waller's lists (data/jlpt_kanji.json): unofficial, as every such list has been
    since 2010, but with each kanji assigned to a level. For a kanji not on them, KANJIDIC's
    pre-2010 four-level scale is used, whose level 2 covers today's N3 and N2: the more frequent
    half of that group is then treated as N3.
    """
    if not _WALLER:
        try:
            data = json.loads((Path(__file__).parent / "data" / "jlpt_kanji.json").read_text(encoding="utf-8"))
            for lv, chars in data["levels"].items():
                for ch in chars:
                    _WALLER.setdefault(ch, int(lv))
        except (OSError, ValueError, KeyError):
            pass
        _WALLER.setdefault("", 0)                   # read once, even when the file is missing
    literal = literal or (info or {}).get("literal")
    if literal and _WALLER.get(literal):
        return _WALLER[literal]
    if not info or not info.get("jlpt"):
        return None
    old = info["jlpt"]
    if old == 4:
        return 5
    if old == 3:
        return 4
    if old == 2:
        return 3 if (info.get("freq") or 9999) <= 700 else 2
    return 1


def apply_level(con, level: int):
    """State the level: items at or below it are treated as in review with a long interval."""
    memory.set_level(con, level)
    memory.recompute(con)


def set_status(con, item_id: int, status: str):
    """Your own judgement about an item, which outranks imports and the level:
    known ("I know this"), learning ("Learn this": New again), ignored, or undo to take it back."""
    memory.mark(con, item_id, dict(known="known", learning="new", ignored="ignored").get(status))
    memory.recompute(con)


def clear_mark(con, item_id: int):
    memory.mark(con, item_id, None)
    memory.recompute(con)


def reject_sentence(con, sentence_id: int) -> dict:
    """Mark a sentence as bad. It leaves every card's pool, and a replacement is looked for in every
    source. A card goes only if its word has no other usable sentence anywhere. Returns how many
    cards carried on with other sentences and how many went."""
    s = con.execute("SELECT source_id, text FROM sentences WHERE id=?", (sentence_id,)).fetchone()
    if not s:
        return dict(moved=0, dropped=0)
    ids = [r[0] for r in con.execute("SELECT DISTINCT card_id FROM card_sentences WHERE sentence_id=?", (sentence_id,))]
    con.execute("INSERT OR IGNORE INTO rejected(source_id, text) VALUES(?,?)", (s["source_id"], s["text"]))
    rebuild_cards(con)
    left = sum(1 for i in ids if con.execute("SELECT 1 FROM cards WHERE id=?", (i,)).fetchone())
    return dict(moved=left, dropped=len(ids) - left)


def restore_sentence(con, source_id: int, text: str):
    con.execute("DELETE FROM rejected WHERE source_id=? AND text=?", (source_id, text))
    con.execute("INSERT OR IGNORE INTO kept VALUES(?,?)", (source_id, text))        # the quality check leaves it alone from now on
    rebuild_cards(con)


# ---- items -------------------------------------------------------------------------------------
def _word_item(con, dic, form: str, reading: str, source_id: int, level_setting: int) -> int | None:
    key = f"{form}|{hira(reading)}"
    r = con.execute("SELECT id FROM items WHERE kind='word' AND key=?", (key,)).fetchone()
    if r:
        if source_id:                           # known of before (an import, a keigo table) but first met in this source
            con.execute("UPDATE items SET first_source=? WHERE id=? AND first_source IS NULL", (source_id, r[0]))
            con.execute("""UPDATE items SET first_source=? WHERE first_source IS NULL AND kind='kanji'
                           AND id IN (SELECT kanji_id FROM word_kanji WHERE word_id=?)""", (source_id, r[0]))
        return r[0]
    e = dic.lookup(form, reading)
    if not e:
        return None
    rd = e["reading"] or hira(reading)
    status, by = "learning", None
    if is_grammar(form, e["pos"]):
        status, by = "ignored", "auto"     # んです, について: grammar, handled in a later version
    wid = con.execute(
        "INSERT INTO items(kind, key, lemma, reading, gloss, level, common, freq, status, known_by, first_source, custom, created, register) "
        "VALUES('word',?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (key, form, rd, e["gloss"], e["jlpt"], e["common"], dic.freq_rank(form), status, by, source_id,
         int(bool(e.get("custom"))), db.now(), e.get("reg"))).lastrowid
    pairs = align(form, rd, dic.kanji) if kanji_of(form) else None
    for pos, (ch, base) in enumerate(pairs or []):
        kkey = f"{ch}|{base}"
        k = con.execute("SELECT id FROM items WHERE kind='kanji' AND key=?", (kkey,)).fetchone()
        if k:
            kid = k[0]
        else:
            info = dic.kanji(ch)
            kid = con.execute(
                "INSERT INTO items(kind, key, lemma, reading, gloss, level, freq, status, first_source, created) "
                "VALUES('kanji',?,?,?,?,?,?,'learning',?,?)",
                (kkey, ch, base, (info or {}).get("meaning", ""), kanji_level(info, ch), (info or {}).get("freq"),
                 source_id, db.now())).lastrowid
        con.execute("INSERT OR IGNORE INTO word_kanji VALUES(?,?,?)", (wid, kid, pos))
    parts = dic.expression(form)
    if parts:                                   # 仮説を立てる: a collocation, its card hides the verb
        con.execute("INSERT OR IGNORE INTO colloc VALUES(?,?,?,?)", (wid, *parts))
    return wid


def _pair_item(con, dic, noun, particle, verb, source_id: int) -> int | None:
    """The item for a collocation that is not a dictionary entry but is on a pair list (影響 を 与える)."""
    key = f"{noun.form}|{particle}|{verb.form}"
    r = con.execute("SELECT id FROM items WHERE kind='colloc' AND key=?", (key,)).fetchone()
    if r:
        return r[0]
    n, v = dic.lookup(noun.form, noun.reading), dic.lookup(verb.form, verb.reading)
    if not n or not v:
        return None
    first = lambda g: (g or "").split(";")[0].split("|")[0].strip()  # noqa: E731
    gloss = dic.pairs().get((noun.form, particle, verb.form)) or f"{first(n['gloss'])} + {first(v['gloss'])}"
    cid = con.execute(
        "INSERT INTO items(kind, key, lemma, reading, gloss, status, first_source, created) VALUES('colloc',?,?,?,?,'learning',?,?)",
        (key, f"{noun.form}{particle}{verb.form}", f"{n['reading'] or hira(noun.reading)}{particle}{v['reading'] or hira(verb.reading)}",
         gloss, source_id, db.now())).lastrowid
    con.execute("INSERT OR IGNORE INTO colloc VALUES(?,?,?,?)", (cid, noun.form, particle, verb.form))
    return cid


def backfill_collocations(con, dic) -> int:
    """Mark the dictionary expressions collected before 0.3.0 as collocations. Run once."""
    n = 0
    for r in con.execute("SELECT id, lemma FROM items WHERE kind='word' AND id NOT IN (SELECT item_id FROM colloc)").fetchall():
        parts = dic.expression(r["lemma"])
        if parts:
            n += con.execute("INSERT OR IGNORE INTO colloc VALUES(?,?,?,?)", (r["id"], *parts)).rowcount
    db.set_setting(con, "colloc_backfilled", 1)
    return n


def backfill_registers(con, dic) -> int:
    """Label what was collected before 0.3.1: every sentence by its wording, and every word from the
    dictionary's tags if the dictionary has them (it does once `sumitsubo setup` has been run again)."""
    for r in con.execute("SELECT id, text FROM sentences WHERE register IS NULL").fetchall():
        con.execute("UPDATE sentences SET register=? WHERE id=?", (register.of_sentence(r["text"]), r["id"]))
    for r in con.execute("SELECT id, lemma, reading FROM items WHERE kind='word' AND status='ignored' AND known_by='auto'").fetchall():
        e = dic.lookup(r["lemma"], r["reading"])            # verbs once set aside only for doubling as auxiliaries
        if e and not is_grammar(r["lemma"], e["pos"]):
            con.execute("UPDATE items SET status='learning', known_by=NULL WHERE id=?", (r["id"],))
    n = 0
    if dic.has_registers():
        for r in con.execute("SELECT id, lemma, reading FROM items WHERE kind='word' AND register IS NULL").fetchall():
            e = dic.lookup(r["lemma"], r["reading"])
            if e and e.get("reg"):
                n += con.execute("UPDATE items SET register=? WHERE id=?", (e["reg"], r["id"])).rowcount
        db.set_setting(con, "registers_backfilled", 1)
    return n


def analyze_source(con, dic, parser, source_id: int, sentences: list[dict], tell=None) -> dict:
    """Store a source's sentences and link every word and kanji reading in them. tell(done, total)
    is called every few hundred sentences."""
    level = int(db.setting(con, "level") or 0)
    for r in con.execute("SELECT clip FROM sentences WHERE source_id=? AND clip IS NOT NULL", (source_id,)).fetchall():
        old = config.resolve(r["clip"])
        if old and old.exists():
            old.unlink()
    con.execute("DELETE FROM occ WHERE source_id=?", (source_id,))
    con.execute("DELETE FROM sentences WHERE source_id=?", (source_id,))
    con.execute("DELETE FROM unverified WHERE source_id=?", (source_id,))
    token_lists = parser.parse([s["text"] for s in sentences])
    pairs = dic.pairs()
    unverified: dict[str, int] = {}
    ids: dict = {}                              # a word met again in this source is looked up once
    for i, (s, toks) in enumerate(zip(sentences, token_lists)):
        if tell and i and i % 300 == 0:
            tell(i, len(sentences))
        sid = con.execute("INSERT INTO sentences(source_id, idx, text, start, end, location, register) VALUES(?,?,?,?,?,?,?)",
                          (source_id, i, s["text"], s.get("start"), s.get("end"), s.get("location", ""), register.of_sentence(s["text"]))).lastrowid
        for t in toks:
            if (t.form, t.reading) not in ids:
                ids[(t.form, t.reading)] = _word_item(con, dic, t.form, t.reading, source_id, level)
            wid = ids[(t.form, t.reading)]
            if wid is None:
                unverified[t.form] = unverified.get(t.form, 0) + 1
                continue
            con.execute("INSERT INTO occ(item_id, sentence_id, source_id, n, surface) VALUES(?,?,?,1,?) "
                        "ON CONFLICT(item_id, sentence_id) DO UPDATE SET n=n+1", (wid, sid, source_id, t.surface))
        if pairs:                               # a noun, a particle and its verb written side by side
            for a, b in zip(toks, toks[1:]):
                for p in PARTICLES:
                    if (a.form, p, b.form) in pairs and f"{a.surface}{p}{b.surface}" in s["text"]:
                        cid = _pair_item(con, dic, a, p, b, source_id)
                        if cid:
                            con.execute("INSERT OR IGNORE INTO occ(item_id, sentence_id, source_id, n, surface) VALUES(?,?,?,1,?)",
                                        (cid, sid, source_id, f"{a.surface}{p}{b.surface}"))
    con.executemany("INSERT OR REPLACE INTO unverified VALUES(?,?,?)", [(source_id, f, n) for f, n in unverified.items()])
    con.execute("UPDATE sources SET n_sentences=? WHERE id=?", (len(sentences), source_id))
    return dict(sentences=len(sentences), unverified=len(unverified))


# ---- cards -------------------------------------------------------------------------------------
POOL = 5        # sentences kept per word or listening card


def _candidates(con) -> dict:
    """For every word, the sentences it appears in across all sources (bad ones left out)."""
    unknown = dict(con.execute(f"""SELECT o.sentence_id, COUNT(*) FROM occ o JOIN items i ON i.id=o.item_id
                                   WHERE i.kind='word' AND i.band IN {UNFAMILIAR} GROUP BY o.sentence_id""").fetchall())
    rejected = {(r[0], r[1]) for r in con.execute("SELECT source_id, text FROM rejected")}
    out: dict = {}
    for r in con.execute("""SELECT o.item_id, s.id, s.source_id, s.text, s.start, s.register, i.lemma FROM occ o
                            JOIN sentences s ON s.id=o.sentence_id JOIN items i ON i.id=o.item_id WHERE i.kind IN ('word','colloc')"""):
        if (r["source_id"], r["text"]) not in rejected:
            out.setdefault(r["item_id"], []).append(dict(id=r["id"], source=r["source_id"], text=r["text"], audio=r["start"] is not None,
                                                         unknown=unknown.get(r["id"], 0), bare=r["text"] == r["lemma"], register=r["register"]))
    return out


def _pool(cands: list, first_source: int, keep: int | None = None, listen: bool = False) -> list[int]:
    """Up to POOL sentences for a card, best first: fewest other unfamiliar words, has audio, moderate
    length, and spread over sources and over registers (a paper's plain style, a talk's polite
    style), so a word is seen in more than one setting. The card's current
    main sentence stays first while it is still usable. A listening card needs audio and a sentence
    in which its word is the only unfamiliar one. A bare word (from an imported deck with no example)
    is used only when there is no real sentence."""
    c = [x for x in cands if x["audio"] and x["unknown"] == 1] if listen else list(cands)
    if any(not x["bare"] for x in c):
        c = [x for x in c if not x["bare"]]
    if not c:
        return []
    by_source: dict = {}
    for x in sorted(c, key=lambda x: (x["unknown"], not x["audio"], abs(len(x["text"]) - 28), x["id"])):
        by_source.setdefault(x["source"], []).append(x)
    order = sorted(by_source, key=lambda s: (s != first_source, s))
    main = next((x for x in c if x["id"] == keep), None) or by_source[order[0]][0]
    picked, texts, registers = [main["id"]], {main["text"]}, {main["register"]}
    for s in order:                              # within a source, a register not yet in the pool comes first
        by_source[s].sort(key=lambda x: x["register"] in registers)
    for depth in range(max(len(v) for v in by_source.values())):
        for s in order:
            if depth < len(by_source[s]) and len(picked) < POOL:
                x = by_source[s][depth]
                if x["text"] not in texts:
                    picked.append(x["id"])
                    texts.add(x["text"])
                    registers.add(x["register"])
    return picked


def _context_words(con, kanji_id: int, card_source: int) -> list[int]:
    """Words shown on a kanji card, best first (spec C4): this source, then other sources; words whose
    other kanji are familiar; then the more frequent."""
    rows = con.execute(
        """SELECT w.id, w.freq,
                  (SELECT COALESCE(SUM(n),0) FROM occ WHERE item_id=w.id AND source_id=?) AS here,
                  (SELECT COALESCE(SUM(n),0) FROM occ WHERE item_id=w.id) AS everywhere,
                  (SELECT COUNT(*) FROM word_kanji x JOIN items k ON k.id=x.kanji_id
                    WHERE x.word_id=w.id AND x.kanji_id<>? AND k.band IN ('new','young')) AS other_unknown
           FROM word_kanji wk JOIN items w ON w.id=wk.word_id
           WHERE wk.kanji_id=? AND w.band<>'suspended' GROUP BY w.id""", (card_source, kanji_id, kanji_id)).fetchall()
    rows.sort(key=lambda r: (r["here"] == 0, r["other_unknown"] > 0, -r["everywhere"], r["freq"] or 10**9, r["id"]))
    return [r["id"] for r in rows[:CONTEXT_WORDS]]


def rebuild_cards(con, tell=None) -> dict:
    """Bring memory states and cards up to date. Safe to run at any time.

    Every item met in a source has a card unless it is suspended: New ones to be learned, the rest
    to come back when due. The device, not this program, decides what is shown each day. A
    listening card exists only for a word that is still unfamiliar, in a sentence where it is the
    only such word.
    """
    memory.recompute(con)
    con.execute("DELETE FROM cards WHERE item_id IN (SELECT id FROM items WHERE band='suspended')")
    con.execute("DELETE FROM cards WHERE item_id NOT IN (SELECT DISTINCT item_id FROM occ) AND kind<>'kanji'")
    con.execute("DELETE FROM cards WHERE kind='listen' AND item_id IN (SELECT id FROM items WHERE band='mature')")
    made = dict(word=0, listen=0, kanji=0, colloc=0)

    def add(kind, item_id, source_id, sentence_id) -> int:
        made[kind] += 1
        return con.execute("INSERT INTO cards(kind, item_id, source_id, sentence_id, guid, created) VALUES(?,?,?,?,?,?)",
                           (kind, item_id, source_id, sentence_id, uuid.uuid4().hex[:16], db.now())).lastrowid

    # Sentences: each word and listening card gets a pool from every source, refreshed on each run,
    # so a word first met in a talk is later also shown in a sentence from a paper. A card keeps
    # its identity when its sentences change; it goes only if no usable sentence is left anywhere.
    con.execute("DELETE FROM colloc WHERE item_id NOT IN (SELECT id FROM items)")
    is_colloc = {r[0] for r in con.execute("SELECT item_id FROM colloc")}
    con.execute("UPDATE OR IGNORE cards SET kind='colloc' WHERE kind='word' AND item_id IN (SELECT item_id FROM colloc)")   # same card, same identity
    con.execute("DELETE FROM cards WHERE kind IN ('word','listen') AND item_id IN (SELECT item_id FROM colloc)")
    cands = _candidates(con)
    pools: list = []
    have = {}
    old = con.execute("SELECT id, kind, item_id, source_id, sentence_id FROM cards WHERE kind<>'kanji'").fetchall()
    # How far the work is, for a progress bar: cards kept, words looked at, kanji cards, and the two closing passes.
    n_words = con.execute("SELECT COUNT(DISTINCT item_id) FROM occ").fetchone()[0]
    n_kanji = con.execute("SELECT COUNT(*) FROM items WHERE kind='kanji'").fetchone()[0]
    total, done = len(old) + n_words + n_kanji, 0
    total += max(2, total // 5)                       # the order of the cards and the contrast cards

    def step(n=1):
        nonlocal done
        before, done = done, min(total, done + n)
        if tell and (done // 250 != before // 250 or done == total):
            tell(done, total)
    for c in old:
        step()
        pool = _pool(cands.get(c["item_id"], []), c["source_id"], keep=c["sentence_id"], listen=c["kind"] == "listen")
        if not pool:
            con.execute("DELETE FROM cards WHERE id=?", (c["id"],))
            continue
        have[(c["kind"], c["item_id"])] = c["id"]
        if pool[0] != c["sentence_id"]:
            con.execute("UPDATE cards SET sentence_id=? WHERE id=?", (pool[0], c["id"]))
        pools += [(c["id"], sid, n) for n, sid in enumerate(pool)]
    have.update({("kanji", r[0]): r[1] for r in con.execute("SELECT item_id, id FROM cards WHERE kind='kanji'")})
    words = con.execute(
        """SELECT i.id, (SELECT MIN(source_id) FROM occ WHERE item_id=i.id) AS src FROM items i
           WHERE i.kind IN ('word','colloc') AND i.band<>'suspended' AND EXISTS(SELECT 1 FROM occ WHERE item_id=i.id)""").fetchall()
    unfamiliar = {r[0] for r in con.execute(f"SELECT id FROM items WHERE band IN {UNFAMILIAR}")}
    for w in words:
        step()
        for kind in ("colloc",) if w["id"] in is_colloc else ("word", "listen"):
            if (kind, w["id"]) in have or (kind == "listen" and w["id"] not in unfamiliar):
                continue
            pool = _pool(cands.get(w["id"], []), w["src"], listen=kind == "listen")
            if pool:
                cid = add(kind, w["id"], w["src"], pool[0])
                pools += [(cid, sid, n) for n, sid in enumerate(pool)]
    con.execute("DELETE FROM card_sentences")
    con.executemany("INSERT INTO card_sentences VALUES(?,?,?)", pools)
    con.execute("DELETE FROM card_words WHERE card_id NOT IN (SELECT id FROM cards)")
    kanji = con.execute(
        """SELECT k.id, MIN(o.source_id) AS src FROM items k JOIN word_kanji wk ON wk.kanji_id=k.id
           JOIN occ o ON o.item_id=wk.word_id WHERE k.kind='kanji' AND k.band<>'suspended' GROUP BY k.id""").fetchall()
    live = {k["id"] for k in kanji}
    con.execute("DELETE FROM cards WHERE kind='kanji' AND item_id NOT IN (%s)" % (",".join(map(str, live)) or "0"))
    for k in kanji:
        if ("kanji", k["id"]) not in have:
            add("kanji", k["id"], k["src"], None)
    # Context words are refreshed on every run, so a kanji card gains words as new sources arrive.
    for c in con.execute("SELECT id, item_id, source_id FROM cards WHERE kind='kanji'").fetchall():
        step()
        con.execute("DELETE FROM card_words WHERE card_id=?", (c["id"],))
        con.executemany("INSERT INTO card_words VALUES(?,?,?)",
                        [(c["id"], wid, n) for n, wid in enumerate(_context_words(con, c["item_id"], c["source_id"]))])
    step(max(0, total - max(2, total // 6) - done))   # whatever the counts above overestimated
    _rank(con)
    step((total - done) // 2)
    made["contrast"] = sum(contrast.rebuild(con, lambda form: _dictionary_word(con, form)).values())
    step(total - done)
    return made


_dic = {}


def _dictionary_word(con, form: str) -> int | None:
    """The item for a dictionary word that no source has brought in yet (a keigo verb, say)."""
    from . import dictdata  # noqa: PLC0415
    if not dictdata.db_path().exists():
        return None
    if "d" not in _dic or _dic.get("path") != str(dictdata.db_path()):
        _dic.update(d=dictdata.Dictionary(gloss_dict=db.setting(con, "gloss_dict")), path=str(dictdata.db_path()))
    e = _dic["d"].lookup(form)
    return _word_item(con, _dic["d"], form, e["reading"] or "", None, 0) if e else None


def refresh_frequency(con, dic) -> int:
    """Apply the frequency list in use to the words already collected, then re-order the cards."""
    n = 0
    for r in con.execute("SELECT id, lemma, freq FROM items WHERE kind='word'").fetchall():
        f = dic.freq_rank(r["lemma"])
        if f != r["freq"]:
            con.execute("UPDATE items SET freq=? WHERE id=?", (f, r["id"]))
            n += 1
    rebuild_cards(con)
    return n


def _rank(con):
    """Order within each source. Cards still to learn come first: most frequent in the source first;
    a listening card follows its word; a kanji card comes just after the first word that uses it.
    Mature cards follow; they are only waiting for their next check."""
    for (src,) in con.execute("SELECT DISTINCT source_id FROM cards").fetchall():
        rows = con.execute(
            """SELECT c.id, c.kind, c.item_id, i.freq, i.band,
                      CASE c.kind WHEN 'kanji' THEN
                        (SELECT COALESCE(SUM(o.n),0) FROM word_kanji wk JOIN occ o ON o.item_id=wk.word_id
                          WHERE wk.kanji_id=c.item_id AND o.source_id=c.source_id)
                      ELSE (SELECT COALESCE(SUM(n),0) FROM occ WHERE item_id=c.item_id AND source_id=c.source_id) END AS n,
                      (SELECT COALESCE(SUM(n),0) FROM occ WHERE item_id=c.item_id) AS total
               FROM cards c JOIN items i ON i.id=c.item_id WHERE c.source_id=?""", (src,)).fetchall()
        order = {"word": 0, "colloc": 0, "listen": 1, "kanji": 2}
        rows.sort(key=lambda r: (r["band"] == "mature", -r["n"], -r["total"], r["freq"] or 10**9, r["kind"] == "kanji", r["item_id"], order[r["kind"]]))
        con.executemany("UPDATE cards SET rank=? WHERE id=?", [(n + 1, r["id"]) for n, r in enumerate(rows)])


# ---- difficulty --------------------------------------------------------------------------------
BANDS = ("mature", "young", "queued", "new")


def tier(pct_readable: float) -> str:
    if pct_readable >= 98:
        return "comfortable"
    if pct_readable >= 90:
        return "good working range"
    if pct_readable >= 80:
        return "hard going"
    return "very hard"


def _summarise(counts: dict, expected: float, kanji_new: int, kanji_stack: int, unverified: int, sentences: dict | None = None) -> dict:
    total = sum(v["n"] for v in counts.values()) or 1
    pct = {k: round(100 * counts[k]["n"] / total, 1) for k in counts}
    readable = round(100 * expected / total, 1)
    return dict(total_words=sum(v["n"] for v in counts.values()), unique_words=sum(v["u"] for v in counts.values()),
                pct=pct, unique={k: counts[k]["u"] for k in counts}, readable=readable, kanji_new=kanji_new,
                kanji_stack=kanji_stack, unverified=unverified, tier=tier(readable),
                # whole sentences: how many you can read now, and how many are one word away
                sentences=sentences or dict(total=0, readable=0, one_away=0))


def normalise_score(d: dict | None) -> dict | None:
    """A stored score in today's shape. Scores saved before 0.2.0 had known / stack / new."""
    if d and "mature" not in d.get("pct", {}):
        m = dict(known="mature", stack="queued", new="new")
        d = dict(d, pct={m[k]: v for k, v in d["pct"].items()} | dict(young=0.0),
                 unique={m[k]: v for k, v in d["unique"].items()} | dict(young=0), readable=d["pct"].get("known", 0.0))
    if d and "sentences" not in d:                # scores saved before 0.3.0
        d = dict(d, sentences=dict(total=0, readable=0, one_away=0))
    return d


def coverage(con, source_id: int) -> dict:
    """How readable a stored source is now.

    Running words are split into mature, young (learning or in early review), queued (New, but an
    earlier source already gave them a card) and new (New, first met here). "readable" is the share
    you can expect to read today: each word counts by its chance of being recalled now.
    """
    counts = {k: dict(n=0, u=0) for k in BANDS}
    expected, now = 0.0, db.now()
    for r in con.execute(
            """SELECT i.band, i.first_source, SUM(o.n) AS n, m.stability, m.last_review FROM occ o JOIN items i ON i.id=o.item_id
               LEFT JOIN memory m ON m.item_id=i.id AND m.skill=i.kind
               WHERE o.source_id=? AND i.kind='word' AND i.band<>'suspended' GROUP BY i.id""", (source_id,)):
        k = r["band"] if r["band"] != "new" else ("new" if r["first_source"] == source_id else "queued")
        counts[k]["n"] += r["n"]
        counts[k]["u"] += 1
        expected += r["n"] * memory.recall(r["stability"], r["last_review"], now)
    kn = con.execute(
        f"""SELECT SUM(k.band='new' AND k.first_source=?) AS new, SUM(NOT (k.band='new' AND k.first_source=?)) AS stack FROM items k
           WHERE k.kind='kanji' AND k.band IN {UNFAMILIAR} AND k.id IN (
             SELECT wk.kanji_id FROM word_kanji wk JOIN occ o ON o.item_id=wk.word_id WHERE o.source_id=?)""",
        (source_id, source_id, source_id)).fetchone()
    unv = con.execute("SELECT COALESCE(SUM(n),0) FROM unverified WHERE source_id=?", (source_id,)).fetchone()[0]
    per = con.execute(f"""SELECT COUNT(*) AS total, SUM(u=0) AS readable, SUM(u=1) AS one_away FROM (
                            SELECT o.sentence_id, SUM(i.band IN {UNFAMILIAR}) AS u FROM occ o
                            JOIN items i ON i.id=o.item_id WHERE o.source_id=? AND i.kind='word' AND i.band<>'suspended' GROUP BY o.sentence_id)""",
                      (source_id,)).fetchone()
    out = _summarise(counts, expected, kn["new"] or 0, kn["stack"] or 0, unv,
                     dict(total=per["total"] or 0, readable=per["readable"] or 0, one_away=per["one_away"] or 0))
    reg = {r[0]: r[1] for r in con.execute("SELECT register, COUNT(*) FROM sentences WHERE source_id=? AND register IS NOT NULL GROUP BY register", (source_id,))}
    total = sum(reg.values()) or 1
    out["register"] = {k: round(100 * reg.get(k, 0) / total) for k in ("plain", "polite", "keigo")} if reg else None
    return out


def score_text(con, dic, parser, sentences: list[str]) -> dict:
    """Score text without adding it: 'should I read this?'"""
    level, now = int(db.setting(con, "level") or 0), db.now()
    counts = {k: dict(n=0, u=0) for k in BANDS}
    seen: dict[str, tuple[str, float]] = {}
    expected, kanji_new, kanji_stack, unverified, kseen = 0.0, 0, 0, 0, set()
    per = dict(total=0, readable=0, one_away=0)
    for toks in parser.parse(sentences):
        hard = 0
        for t in toks:
            key = f"{t.form}|{hira(t.reading)}"
            if key not in seen:
                r = con.execute("""SELECT i.band, m.stability, m.last_review, EXISTS(SELECT 1 FROM occ o WHERE o.item_id=i.id) AS met FROM items i
                                   LEFT JOIN memory m ON m.item_id=i.id AND m.skill=i.kind WHERE i.kind='word' AND i.key=?""", (key,)).fetchone()
                if r:                           # New and in a source already: queued. New and in no source yet: new here.
                    seen[key] = (("queued" if r["met"] else "new") if r["band"] == "new" else r["band"], memory.recall(r["stability"], r["last_review"], now))
                else:
                    e = dic.lookup(t.form, t.reading)
                    if not e:
                        seen[key] = ("unverified", 0.0)
                    elif is_grammar(t.form, e["pos"]):
                        seen[key] = ("suspended", 0.0)
                    else:                       # a word not met before: your level decides, as it will when it is added
                        seen[key] = ("mature", 1.0) if level and e["jlpt"] and e["jlpt"] >= level else ("new", 0.0)
                    if e and kanji_of(t.form):
                        for ch, base in align(t.form, e["reading"] or t.reading, dic.kanji) or []:
                            if (ch, base) in kseen:
                                continue
                            kseen.add((ch, base))
                            k = con.execute("SELECT band FROM items WHERE kind='kanji' AND key=?", (f"{ch}|{base}",)).fetchone()
                            if k:
                                kanji_stack += k["band"] in ("new", "young")
                            else:
                                lv = kanji_level(dic.kanji(ch), ch)
                                kanji_new += not (level and lv and lv >= level)
                if seen[key][0] in counts:
                    counts[seen[key][0]]["u"] += 1
            if seen[key][0] == "unverified":
                unverified += 1
            elif seen[key][0] in counts:
                counts[seen[key][0]]["n"] += 1
                expected += seen[key][1]
                hard += seen[key][0] != "mature"
        if any(seen[f"{t.form}|{hira(t.reading)}"][0] in counts for t in toks):
            per["total"] += 1
            per["readable"] += hard == 0
            per["one_away"] += hard == 1
    return _summarise(counts, expected, kanji_new, kanji_stack, unverified, per)


def remember_score(con, source_id: int, score: dict):
    con.execute("UPDATE sources SET added_score=? WHERE id=?", (json.dumps(score), source_id))


# ---- when will a source be readable? ---------------------------------------------------------------
PRIORITY = {1: "First", 2: "Normal", 3: "Later"}
SETTLE_DAYS = 21          # after its last word is introduced, about this long until the new words have settled


def queue(con) -> list:
    """Every New card in the order a device will introduce it: by the priority of its source, then
    the order sources were added, then the card's place within its source."""
    return con.execute(
        """SELECT c.id, c.kind, c.item_id, c.source_id FROM cards c JOIN items i ON i.id=c.item_id JOIN sources s ON s.id=c.source_id
           WHERE i.band='new' AND NOT EXISTS(SELECT 1 FROM memory m WHERE m.item_id=c.item_id AND m.skill=c.kind)
           ORDER BY c.kind IN ('word','colloc') AND c.item_id IN (SELECT item_id FROM wanted) DESC,
                    COALESCE(s.priority,2), s.id, c.rank""").fetchall()


def projection(con, today: dt.date | None = None) -> dict:
    """For each source: how many of the words in it are still New, the date by which all of them will
    have been introduced at the current daily budget and priorities, and a rough date by which they
    will have settled. The first date is arithmetic; the second is an estimate.

    A word counts wherever its card lives: a word of this source whose card belongs to an earlier
    source is introduced when that card's turn comes.
    """
    budget = max(1, int(db.setting(con, "daily_budget") or 15))
    today = today or dt.date.today()
    day = {}                                     # item -> the day (0 = today) its word or collocation card is introduced
    for n, c in enumerate(queue(con)):
        if c["kind"] in ("word", "colloc"):
            day[c["item_id"]] = n // budget
    out = {}
    for s in con.execute("SELECT id FROM sources WHERE status='done'").fetchall():
        days = [day[r[0]] for r in con.execute("""SELECT DISTINCT o.item_id FROM occ o JOIN items i ON i.id=o.item_id
                                                  WHERE o.source_id=? AND i.band='new'""", (s["id"],)) if r[0] in day]
        if days:
            last = max(days) + 1
            out[s["id"]] = dict(new_words=len(days), days=last, introduced=(today + dt.timedelta(days=last)).isoformat(),
                                settled=(today + dt.timedelta(days=last + SETTLE_DAYS)).isoformat())
        else:
            out[s["id"]] = dict(new_words=0, days=0, introduced=None, settled=None)
    return out | dict(budget=budget, queue=len(day), all_days=math.ceil(len(queue(con)) / budget))
