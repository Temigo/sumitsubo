"""What each card shows, and the two ways cards leave the program: an Anki deck and a pack on disk."""
import html
import json
import re
import shutil
import zlib
from pathlib import Path

from . import analyze, config, contrast, db, memory

KIND_LABEL = {"word": "Word", "listen": "Listening", "kanji": "Kanji", "colloc": "Collocation"}
BLANK = "＿＿"


def _mark(sentence: str, lemma: str, surface_hint: str | None = None) -> tuple[str, str, str]:
    """Split a sentence around the target word: (before, target, after). Falls back to the stem."""
    for cand in filter(None, [surface_hint, lemma, lemma[:-1] if len(lemma) > 2 else None, lemma[:2] if len(lemma) > 2 else None]):
        i = sentence.find(cand)
        if i >= 0 and cand is surface_hint:
            return sentence[:i], cand, sentence[i + len(cand):]      # exactly as it was written
        if i >= 0:
            j = i + len(cand)
            while j < len(sentence) and cand is not lemma and re.match(r"[぀-ゟ]", sentence[j]) and j - i < len(lemma) + 3:
                j += 1                      # take the conjugated ending with the stem
            return sentence[:i], sentence[i:j], sentence[j:]
    return sentence, "", ""


def _hide_verb(sentence: str, noun: str, particle: str, verb: str, surface: str | None) -> tuple[str, str, str]:
    """Split a sentence around the verb of a collocation: (up to and including the particle, the verb
    as written, the rest). The front of the card shows a blank in place of the verb."""
    head = noun + particle
    i = sentence.find(surface) if surface and surface.startswith(head) else -1
    if i >= 0:
        return sentence[:i + len(head)], surface[len(head):], sentence[i + len(surface):]
    i = sentence.find(head)
    stem = verb[:-1] if len(verb) > 1 else verb
    if i < 0 or not sentence.startswith(stem, i + len(head)):
        return _mark(sentence, noun + particle + verb)
    j = i + len(head) + len(stem)
    if sentence.startswith(verb, i + len(head)):           # written in its dictionary form
        return sentence[:i + len(head)], verb, sentence[i + len(head) + len(verb):]
    k = j
    while k < len(sentence) and k - j < 2 and re.match(r"[ぁ-ん]", sentence[k]):     # the ending, roughly
        k += 1
    return sentence[:i + len(head)], sentence[i + len(head):k], sentence[k:]


def view(con, card) -> dict:
    """Everything needed to show or export one card."""
    it = con.execute("SELECT * FROM items WHERE id=?", (card["item_id"],)).fetchone()
    src = con.execute("SELECT id, title FROM sources WHERE id=?", (card["source_id"],)).fetchone()
    v = dict(id=card["id"], guid=card["guid"], kind=card["kind"], label=KIND_LABEL[card["kind"]], rank=card["rank"],
             item_id=it["id"], lemma=it["lemma"], reading=it["reading"], gloss=it["gloss"] or "", level=it["level"],
             source_id=card["source_id"], source=src["title"] if src else "", sentence="", parts=("", "", ""),
             sentence_id=card["sentence_id"], clip=None, location="", words=[], other_readings=[], custom=it["custom"],
             band=it["band"], memory=None, state="New")
    skill = "listen" if card["kind"] == "listen" else it["kind"]      # a collocation card carries its item's own state
    co = con.execute("SELECT * FROM colloc WHERE item_id=?", (it["id"],)).fetchone() if card["kind"] == "colloc" else None
    v["colloc"] = dict(co) if co else None
    v["register"] = it["register"]
    # On a collocation card the meaning is shown on the front while the card is New or Young, to say
    # which verb is being asked for; once the card is Mature the front shows the sentence alone.
    v["hint"] = (it["gloss"] or "").split(" | ")[0].split(";")[0].strip() if co else None
    m = con.execute("SELECT * FROM memory WHERE item_id=? AND skill=?", (it["id"], skill)).fetchone()
    if m:
        v["memory"] = memory.describe(m)
        v["state"] = v["memory"]["label"]
    if card["kind"] == "listen":
        v["band"] = memory.band(m["state"], m["stability"]) if m else "new"
    v["sentences"] = []                       # the pool, main sentence first
    if card["kind"] != "kanji":
        for s in con.execute("""SELECT s.*, src.title, o.surface FROM card_sentences cs JOIN sentences s ON s.id=cs.sentence_id
                                LEFT JOIN sources src ON src.id=s.source_id LEFT JOIN occ o ON o.sentence_id=s.id AND o.item_id=?
                                WHERE cs.card_id=? ORDER BY cs.ord""", (it["id"], card["id"])):
            parts = _hide_verb(s["text"], co["noun"], co["particle"], co["verb"], s["surface"]) if co else _mark(s["text"], it["lemma"], s["surface"])
            if co and co["particle"] != "を" and parts[0].endswith(co["particle"]):       # に, が, と, で are part of what must be recalled
                parts = (parts[0][:-len(co["particle"])], co["particle"] + parts[1], parts[2])
            v["sentences"].append(dict(id=s["id"], text=s["text"], parts=parts, clip=s["clip"], register=s["register"],
                                       location=s["location"] or "", source_id=s["source_id"], source=s["title"] or ""))
    if v["sentences"]:
        s = v["sentences"][0]
        v.update(sentence=s["text"], parts=s["parts"], clip=s["clip"], location=s["location"], sentence_id=s["id"])
    if card["kind"] == "kanji":
        v["words"] = [dict(r) for r in con.execute(
            """SELECT w.id, w.lemma, w.reading, w.gloss, w.band FROM card_words cw JOIN items w ON w.id=cw.word_id
               WHERE cw.card_id=? ORDER BY cw.ord""", (card["id"],))]
        v["other_readings"] = [r[0] for r in con.execute(
            "SELECT reading FROM items WHERE kind='kanji' AND lemma=? AND id<>? AND band='mature' ORDER BY reading",
            (it["lemma"], it["id"]))]
    return v


TO_LEARN = ("new", "young")


def listing(con, source_id: int | None = None, kind: str | None = None, tag: str | None = None, limit: int | None = 2000,
            bands: tuple | None = None, offset: int = 0, q: str | None = None):
    """Cards in order. bands limits them by how well the item is known, e.g. TO_LEARN leaves out mature cards.
    limit=None returns how many there are instead."""
    sql, args = f"SELECT {'COUNT(*)' if limit is None else 'c.*'} FROM cards c JOIN items i ON i.id=c.item_id WHERE 1=1", []
    if bands:
        sql += " AND i.band IN (%s)" % ",".join("?" * len(bands))
        args += list(bands)
    if source_id:
        sql += " AND c.source_id=?"
        args.append(source_id)
    if kind:
        sql += " AND c.kind=?"
        args.append(kind)
    if tag:
        sql += " AND c.source_id IN (SELECT st.source_id FROM source_tags st JOIN tags t ON t.id=st.tag_id WHERE t.name=? OR t.name LIKE ?)"
        args += [tag, tag + "/%"]
    if q:                                       # a word, a reading or a meaning
        sql += " AND (i.lemma LIKE ? OR i.reading LIKE ? OR i.gloss LIKE ?)"
        args += [f"%{q}%"] * 3
    if limit is None:
        return con.execute(sql, args).fetchone()[0]
    sql += " ORDER BY c.source_id, c.rank LIMIT ? OFFSET ?"
    return [view(con, c) for c in con.execute(sql, args + [limit, max(0, offset)]).fetchall()]


# ---- plain text faces (used by the pack, and as a base for Anki) -----------------------------------
def faces(v: dict) -> tuple[str, str]:
    if v["kind"] == "word":
        return v["sentence"], f"{v['lemma']}【{v['reading']}】\n{v['gloss']}"
    if v["kind"] == "listen":
        return "🔊", f"{v['sentence']}\n{v['lemma']}【{v['reading']}】 {v['gloss']}"
    if v["kind"] == "colloc":                  # the verb is hidden: which one goes with this noun?
        a, _, c = v["parts"]
        return f"{a}{BLANK}{c}", f"{v['sentence']}\n{v['lemma']}【{v['reading']}】\n{v['gloss']}"
    front = v["lemma"] + "\n" + "　".join(w["lemma"] for w in v["words"])
    back = f"{v['lemma']}【{v['reading']}】 {v['gloss']}\n" + "\n".join(
        f"{w['lemma']}【{w['reading']}】 {w['gloss']}" for w in v["words"])
    if v["other_readings"]:
        back += "\nAlso read: " + "、".join(v["other_readings"])
    return front, back


# ---- pack on disk --------------------------------------------------------------------------------
def write_pack(con, source_id: int, dest_root: Path | None = None) -> Path:
    """Write one source as a pack: the folder a device will read. See requirements-and-spec.md, section 7."""
    src = con.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
    root = (dest_root or config.data_dir() / "packs") / f"source-{source_id}"
    if root.exists():
        shutil.rmtree(root)
    (root / "text").mkdir(parents=True)
    (root / "audio").mkdir()
    sentences = con.execute("SELECT * FROM sentences WHERE source_id=? ORDER BY idx", (source_id,)).fetchall()
    (root / "text" / "text.txt").write_text("\n".join(s["text"] for s in sentences), encoding="utf-8")
    with open(root / "cards.jsonl", "w", encoding="utf-8") as f:
        for v in listing(con, source_id=source_id):
            front, back = faces(v)

            def stage(rel):
                clip = config.resolve(rel)
                if clip and clip.exists():
                    shutil.copy2(clip, root / "audio" / clip.name)
                    return clip.name
                return None
            audio = stage(v["clip"])
            # every sentence the card can be shown with; a device shows a different one at each review
            pool = [dict(text=x["text"], audio=stage(x["clip"]), source=x["source_id"], register=x["register"]) | (
                        dict(front=f"{x['parts'][0]}{BLANK}{x['parts'][2]}") if v["kind"] == "colloc" else {}) for x in v["sentences"]]
            m = v["memory"]
            f.write(json.dumps(dict(id=v["guid"], item=v["item_id"], kind=v["kind"], rank=v["rank"], front=front,
                                    back=back, audio=audio, source=source_id, sentences=pool, register=v["register"],
                                    **(dict(hint=v["hint"], hint_while=["new", "learning", "young"]) if v["kind"] == "colloc" else {}),
                                    # the state the card starts from on a device: null for New, else FSRS values
                                    memory=dict(state=m["state"], stability=m["stability"], difficulty=m["difficulty"],
                                                last_review=round(m["last_review"]), due=round(m["due"]), origin=m["origin"]) if m else None),
                               ensure_ascii=False) + "\n")
    (root / "source.json").write_text(json.dumps(dict(
        format=config.PACK_FORMAT, id=source_id, title=src["title"], kind=src["kind"], language="ja",
        series=src["series"] or "", tags=db.tags_of(con, source_id), sentences=len(sentences),
        difficulty=analyze.coverage(con, source_id), daily_budget=int(db.setting(con, "daily_budget") or 15)),
        ensure_ascii=False, indent=1), encoding="utf-8")
    return root


def write_shared(con, dest_root: Path | None = None) -> Path:
    """What a device needs that belongs to no single source: the settings and the contrast cards."""
    root = dest_root or config.data_dir() / "packs"
    root.mkdir(parents=True, exist_ok=True)
    prio = {r["id"]: r["priority"] or 2 for r in con.execute("SELECT id, priority FROM sources WHERE status='done'")}
    (root / "settings.json").write_text(json.dumps(dict(
        format=config.PACK_FORMAT, daily_budget=int(db.setting(con, "daily_budget") or 15),
        contrast_when=db.setting(con, "contrast_when") or "mixup", level=int(db.setting(con, "level") or 0),
        priority={str(k): v for k, v in prio.items()}), ensure_ascii=False, indent=1), encoding="utf-8")
    with open(root / "contrast.jsonl", "w", encoding="utf-8") as f:
        for c in contrast.listing(con, limit=10**9):
            f.write(json.dumps(dict(id=c["guid"], kind=c["kind"], items=[m["id"] for m in c["members"]], front=c["front"], back=c["back"],
                                    met=c["met"], of=len(c["members"])),      # how many of its items you have met, for the "all / most / one" setting
                               ensure_ascii=False) + "\n")
    return root


# ---- Anki export ---------------------------------------------------------------------------------
_CSS = """.card{font-family:"Noto Sans CJK JP","Hiragino Sans","Yu Gothic",sans-serif;font-size:22px;text-align:center;line-height:1.6}
.s{font-size:26px}.w{font-size:34px;margin:.3em 0}.k{font-size:96px;line-height:1.1}.r{color:#555}.g{font-size:18px;color:#333}
.src{font-size:13px;color:#888;margin-top:1.2em}b{color:#b3261e}table{margin:0 auto;text-align:left;font-size:20px}td{padding:.1em .5em}"""


def _models():
    import genanki  # noqa: PLC0415
    src = '<div class="src">{{Source}}</div>'
    word = genanki.Model(1607392811, "Sumitsubo Word", css=_CSS, fields=[{"name": n} for n in
                         ("Word", "Reading", "Sentence", "Meaning", "Audio", "Source")], templates=[{
                             "name": "Reading", "qfmt": '<div class="s">{{Sentence}}</div>',
                             "afmt": '{{FrontSide}}<hr id=answer><div class="w">{{Word}} <span class="r">【{{Reading}}】</span></div>'
                                     '<div class="g">{{Meaning}}</div>{{Audio}}' + src}])
    listen = genanki.Model(1607392812, "Sumitsubo Listening", css=_CSS, fields=[{"name": n} for n in
                           ("Audio", "Sentence", "Word", "Reading", "Meaning", "Source")], templates=[{
                               "name": "Listening", "qfmt": "{{Audio}}",
                               "afmt": '{{FrontSide}}<hr id=answer><div class="s">{{Sentence}}</div>'
                                       '<div class="w">{{Word}} <span class="r">【{{Reading}}】</span></div><div class="g">{{Meaning}}</div>' + src}])
    kanji = genanki.Model(1607392813, "Sumitsubo Kanji", css=_CSS, fields=[{"name": n} for n in
                          ("Kanji", "Reading", "Words", "WordsBack", "Meaning", "OtherReadings", "Source")], templates=[{
                              "name": "Kanji in context", "qfmt": '<div class="k">{{Kanji}}</div><div class="s">{{Words}}</div>',
                              "afmt": '<div class="k">{{Kanji}}</div><hr id=answer><div class="w"><span class="r">{{Reading}}</span></div>'
                                      '<div class="g">{{Meaning}}</div>{{WordsBack}}'
                                      '{{#OtherReadings}}<div class="g">Also read: {{OtherReadings}}</div>{{/OtherReadings}}' + src}])
    colloc = genanki.Model(1607392815, "Sumitsubo Collocation", css=_CSS, fields=[{"name": n} for n in
                           ("Collocation", "Reading", "SentenceBlank", "Sentence", "Meaning", "Audio", "Source")], templates=[{
                               "name": "Which verb", "qfmt": '<div class="s">{{SentenceBlank}}</div><div class="g">{{Meaning}}</div>',   # Anki cannot fade the hint, so it stays
                               "afmt": '<div class="s">{{Sentence}}</div><hr id=answer><div class="w">{{Collocation}} <span class="r">【{{Reading}}】</span></div>'
                                       '<div class="g">{{Meaning}}</div>{{Audio}}' + src}])
    return word, listen, kanji, colloc


def select_sources(con, source_ids=None, tags=None) -> list[int]:
    """The finished sources an export covers: those listed plus those carrying any of the tags
    (a tag also matches its sub-tags, so physics covers physics/detector). Nothing chosen = all."""
    done = [r[0] for r in con.execute("SELECT id FROM sources WHERE status='done' ORDER BY id")]
    source_ids, tags = [int(x) for x in source_ids or []], [t.strip() for t in tags or [] if t and t.strip()]
    if not source_ids and not tags:
        return done
    chosen = set(source_ids)
    for t in tags:
        chosen |= {r[0] for r in con.execute(
            "SELECT st.source_id FROM source_tags st JOIN tags g ON g.id=st.tag_id WHERE g.name=? OR g.name LIKE ?", (t, t + "/%"))}
    return [i for i in done if i in chosen]


def _deck_id(name: str) -> int:
    return 1_500_000_000 + zlib.crc32(name.encode("utf-8")) % 500_000_000


def export_anki(con, dest: Path, source_ids=None, tags=None, kinds=None, deck: str = "Sumitsubo",
                per_source: bool = True, mature: bool = False, progress: bool = False) -> dict:
    """Write an Anki deck file with the chosen cards and their audio.

    Only cards still to learn (New and Young) are written unless mature is set: a mature card,
    such as a word your level covers, would arrive in Anki as a brand-new card.

    source_ids / tags pick the sources (see select_sources); kinds limits the card kinds. The cards
    go into the deck named `deck`, in one sub-deck per source unless per_source is off. Each card
    keeps its identity (guid), so importing a later export updates notes instead of duplicating
    them, whichever deck they were exported into. Source tags become Anki tags.
    """
    import genanki  # noqa: PLC0415
    mature = mature or progress                     # with your progress, a mature card arrives as a mature card
    states: dict = {}
    word_m, listen_m, kanji_m, colloc_m = _models()
    e = html.escape
    decks, media, n = {}, [], 0
    deck = (deck or "Sumitsubo").strip().strip(":") or "Sumitsubo"
    chosen = select_sources(con, source_ids, tags)
    for sid in chosen:
        src = con.execute("SELECT * FROM sources WHERE id=?", (sid,)).fetchone()
        name = f"{deck}::{src['title']}" if per_source else deck
        # the id used by version 0.1.0 is kept for the default layout, so earlier imports still match
        did = 2059400110 + sid if (per_source and deck == "Sumitsubo") else _deck_id(name)
        d = decks.setdefault(name, genanki.Deck(did, name))
        tags = [re.sub(r"\s+", "_", t).replace("/", "::") for t in db.tags_of(con, src["id"])]
        for v in listing(con, source_id=src["id"], limit=10**9, bands=None if mature else TO_LEARN):
            if kinds and v["kind"] not in kinds:
                continue
            audio = ""
            clip = config.resolve(v["clip"])
            if clip and clip.exists():
                name = f"sumitsubo_{clip.name}"
                staged = dest.parent / name
                shutil.copy2(clip, staged)
                media.append(staged)
                audio = f"[sound:{name}]"
            a, b, c = v["parts"]
            sent = f"{e(a)}<b>{e(b)}</b>{e(c)}" if b else e(v["sentence"])
            if v["kind"] == "colloc":
                note = genanki.Note(colloc_m, [e(v["lemma"]), e(v["reading"]), f"{e(a)}{BLANK}{e(c)}", sent, e(v["gloss"]), audio, e(v["source"])])
            elif v["kind"] == "word":
                note = genanki.Note(word_m, [e(v["lemma"]), e(v["reading"]), sent, e(v["gloss"]), audio, e(v["source"])])
            elif v["kind"] == "listen":
                if not audio:
                    continue
                note = genanki.Note(listen_m, [audio, sent, e(v["lemma"]), e(v["reading"]), e(v["gloss"]), e(v["source"])])
            else:
                rows = "".join(f"<tr><td>{e(w['lemma'])}</td><td class=r>{e(w['reading'])}</td><td class=g>{e(w['gloss'][:60])}</td></tr>"
                               for w in v["words"])
                note = genanki.Note(kanji_m, [e(v["lemma"]), e(v["reading"]), "　".join(e(w["lemma"]) for w in v["words"]),
                                              f"<table>{rows}</table>", e(v["gloss"]), "、".join(v["other_readings"]), e(v["source"])])
            note.guid, note.tags = "smt" + v["guid"], tags
            states["smt" + v["guid"]] = (v["guid"], v["item_id"], v["kind"])
            d.add_note(note)
            n += 1
    pkg = genanki.Package(list(decks.values()))
    pkg.media_files = [str(m) for m in media]
    pkg.write_to_file(str(dest))
    for m in media:
        m.unlink(missing_ok=True)
    carried = _with_progress(con, dest, states) if progress and n else dict(cards=0, reviews=0)
    return dict(notes=n, decks=len(decks), sources=len(chosen), audio=len(media), file=str(dest), **carried)


def _with_progress(con, apkg: Path, states: dict) -> dict:
    """Write what you have learned into an exported deck, so that Anki continues instead of starting again.

    Each card that has a memory state here becomes a review card there, due on its day, with the
    state itself (stability and difficulty, which Anki's own scheduler, FSRS, uses) and the reviews
    made on the reader as its history. Anki takes this in when "Import any learning progress" is on.
    The file's format is Anki's internal one; this follows it as documented and was not tried in Anki.
    """
    import sqlite3  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    import zipfile  # noqa: PLC0415

    from . import device  # noqa: PLC0415
    hist = device.history(con)
    now = db.now()
    done = dict(cards=0, reviews=0)
    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(apkg) as z:
            names = z.namelist()
            z.extractall(tmp)                       # a file this program wrote a moment ago
        col = sqlite3.connect(str(Path(tmp) / "collection.anki2"))
        crt = col.execute("SELECT crt FROM col").fetchone()[0]
        today = int((now - crt) // 86400)           # Anki counts due days from the day its collection was made
        for cid, guid in col.execute("SELECT c.id, n.guid FROM cards c JOIN notes n ON n.id=c.nid").fetchall():
            if guid not in states:
                continue
            own, item_id, kind = states[guid]
            skill = "listen" if kind == "listen" else con.execute("SELECT kind FROM items WHERE id=?", (item_id,)).fetchone()["kind"]
            m = con.execute("SELECT * FROM memory WHERE item_id=? AND skill=?", (item_id, skill)).fetchone()
            if not m or not m["stability"] or not m["due"]:
                continue
            # Reviews brought in from elsewhere (the interval after each is not known), then those made on the reader.
            reviews = [(int(r["ts"] // 86400), r["rating"], 0) for r in con.execute(
                "SELECT ts, rating FROM reviews WHERE item_id=? AND skill=? AND rating BETWEEN 1 AND 4 ORDER BY ts", (item_id, skill))] + hist.get(own, [])
            ivl = max(1, round((m["due"] - (m["last_review"] or now)) / 86400))
            lapses = sum(1 for _, r, _ in reviews if r == 1)
            col.execute("UPDATE cards SET type=2, queue=2, due=?, ivl=?, factor=2500, reps=?, lapses=?, left=0, data=? WHERE id=?",
                        (today + round((m["due"] - now) / 86400), ivl, max(1, len(reviews)), lapses,
                         json.dumps(dict(s=round(float(m["stability"]), 4), d=round(float(m["difficulty"] or 5.0), 4))), cid))
            done["cards"] += 1
            last = 0
            for k, (day, rating, nxt) in enumerate(reviews):
                col.execute("INSERT OR IGNORE INTO revlog(id, cid, usn, ease, ivl, lastIvl, factor, time, type) VALUES(?,?,?,?,?,?,?,?,?)",
                            ((day * 86400 + 43200) * 1000 + (cid % 1000) * 10 + k, cid, -1, rating, nxt, last, 2500, 0, 1 if k else 0))
                last = nxt
                done["reviews"] += 1
        col.commit()
        col.close()
        out = apkg.with_suffix(".tmp")
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for name in names:
                z.write(Path(tmp) / name, name)
        out.replace(apkg)
    return done
