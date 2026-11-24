"""Contrast cards: two or three things that are easily confused, shown together.

Four kinds, worked out from what you have met in your own sources:

  look    kanji that look alike (未 末, 待 持 特), from a hand-made list of similar kanji
  read    one kanji read in different ways (学生, 一生, 生物)
  sound   words that sound the same (構成, 校正, 較正: all こうせい)
  keigo   a plain verb with its respectful and humble counterparts (言う, おっしゃる, 申す), from a
          short hand-written table; made once you have met at least one of the three

"read" and "sound" are exact. "look" depends on the list (data/similar-kanji.txt, about 2,900
kanji), so a pair the list does not have is not found.

A contrast card is its own thing to learn, apart from the items on it: telling 未 from 末 is a
different skill from reading either. Its identity is its set of members and never changes. When
another look-alike turns up it gets a new card, and the old one keeps its history.

This program only works out the cards. When one is shown is decided on the device, by the
"contrast cards" setting: after you have mixed the items up (the default), when you have met all,
most or one of them, or never. The first three kinds are made only of items you have met, so for
them "most" and "one" behave like "all". A keigo card can include a verb you have not met yet,
which is where those two choices matter.
"""
import uuid

from . import db, dictdata, register

KINDS = dict(look="Look alike", read="One kanji, several readings", sound="Sound the same", keigo="Keigo")
WHEN = [("mixup", "After I have mixed them up"), ("all", "When I have met all of them"), ("most", "When I have met most of them"),
        ("one", "When I have met one of them"), ("never", "Never")]
SIZE = 3
LOOK = 4            # look-alike kanji are shown in a two-by-two grid


def _met(con) -> tuple[list, list]:
    """Kanji readings and words met in your sources and not suspended, most frequent first."""
    kanji = con.execute("""SELECT k.id, k.lemma, k.reading, COALESCE(SUM(o.n),0) AS n FROM items k JOIN word_kanji wk ON wk.kanji_id=k.id
                           JOIN occ o ON o.item_id=wk.word_id WHERE k.kind='kanji' AND k.band<>'suspended' GROUP BY k.id ORDER BY n DESC, k.id""").fetchall()
    words = con.execute("""SELECT w.id, w.lemma, w.reading, SUM(o.n) AS n FROM items w JOIN occ o ON o.item_id=w.id
                           WHERE w.kind='word' AND w.band<>'suspended' AND w.id NOT IN (SELECT item_id FROM colloc)
                           GROUP BY w.id ORDER BY n DESC, w.id""").fetchall()
    return kanji, words


def groups(con) -> dict[str, list[tuple[str, list[int]]]]:
    """For "read" and "sound": (label, item ids) for every set of confusable items, most frequent member
    first. Look-alike kanji are handled in rebuild, since one kanji can sit on several of those cards."""
    kanji, words = _met(con)
    out: dict = dict(read=[], sound=[])
    by_literal: dict = {}
    for k in kanji:
        by_literal.setdefault(k["lemma"], []).append(k["id"])
    out["read"] = [(lit, ids) for lit, ids in by_literal.items() if len(ids) > 1]
    by_sound: dict = {}
    for w in words:
        if dictdata.kanji_of(w["lemma"]) and w["reading"]:
            by_sound.setdefault(w["reading"], {}).setdefault(w["lemma"], w["id"])
    out["sound"] = [(r, list(m.values())) for r, m in by_sound.items() if len(m) > 1]
    return out


def rebuild(con, make_word=None) -> dict:
    """Bring the contrast cards up to date. A card whose members are all still there is kept as it
    is; members not yet on a card of their kind get new cards, in twos and threes. make_word(form)
    returns the item for a dictionary word, creating it if need be (for keigo verbs not met yet)."""
    alive = {r[0] for r in con.execute("SELECT id FROM items WHERE band<>'suspended'")}
    for c in con.execute("SELECT id FROM contrasts WHERE kind<>'keigo'").fetchall():
        ids = [r[0] for r in con.execute("SELECT item_id FROM contrast_items WHERE contrast_id=?", (c["id"],))]
        if len(ids) < 2 or not set(ids) <= alive:
            con.execute("DELETE FROM contrast_items WHERE contrast_id=?", (c["id"],))
            con.execute("DELETE FROM contrasts WHERE id=?", (c["id"],))
    made = dict(read=0, sound=0)
    for kind, sets in groups(con).items():
        on_card = {r[0] for r in con.execute("SELECT ci.item_id FROM contrast_items ci JOIN contrasts c ON c.id=ci.contrast_id WHERE c.kind=?", (kind,))}
        group_of = {i: n for n, (_, ids) in enumerate(sets) for i in ids}
        # a card stays valid only while its members still belong together
        for c in con.execute("SELECT id FROM contrasts WHERE kind=?", (kind,)).fetchall():
            ids = [r[0] for r in con.execute("SELECT item_id FROM contrast_items WHERE contrast_id=?", (c["id"],))]
            if len({group_of.get(i, -1 - i) for i in ids}) != 1:
                con.execute("DELETE FROM contrast_items WHERE contrast_id=?", (c["id"],))
                con.execute("DELETE FROM contrasts WHERE id=?", (c["id"],))
                on_card -= set(ids)
        for label, ids in sets:
            fresh = [i for i in ids if i not in on_card]
            while fresh:
                take, fresh = fresh[:SIZE], fresh[SIZE:]
                if len(fresh) == 1:                           # never leave one on its own
                    take, fresh = take[:2], [take[2]] + fresh
                if len(take) == 1:                            # a newcomer joins the most frequent members it belongs with
                    take += [i for i in ids if i not in take][:SIZE - 1]
                if len(take) < 2:
                    break
                key = kind + ":" + ",".join(map(str, sorted(take)))
                if con.execute("SELECT 1 FROM contrasts WHERE key=?", (key,)).fetchone():
                    continue
                cid = con.execute("INSERT INTO contrasts(kind, key, label, guid, created) VALUES(?,?,?,?,?)",
                                  (kind, key, label, uuid.uuid4().hex[:16], db.now())).lastrowid
                con.executemany("INSERT INTO contrast_items(contrast_id, item_id, ord) VALUES(?,?,?)", [(cid, i, n) for n, i in enumerate(take)])
                made[kind] += 1
    made["look"] = _look_alikes(con)
    made["keigo"] = _keigo(con, make_word)
    return made


def _keigo(con, make_word) -> int:
    """A card for each plain verb with its respectful and humble counterparts, once you have met at
    least one of them. The others join the card even if not met yet."""
    met = {r[0]: r[1] for r in con.execute("""SELECT i.lemma, i.id FROM items i WHERE i.kind='word' AND i.band<>'suspended'
                                              AND EXISTS(SELECT 1 FROM occ o WHERE o.item_id=i.id)""")}
    have = {r[0]: (r[1], r[2]) for r in con.execute("SELECT lemma, id, band FROM items WHERE kind='word' ORDER BY id DESC")}
    wanted: dict = {}
    for row in register.KEIGO:
        members = []
        for role, forms in zip(register.ROLES, row):
            form = next((f for f in forms if f in met), None) or next((f for f in forms if f in have), None)
            if form:
                iid, band = (met[form], "ok") if form in met else have[form]
            elif forms and make_word:
                iid, band = next((i for i in (make_word(f) for f in forms) if i), None), "new"
                if iid:
                    band = con.execute("SELECT band FROM items WHERE id=?", (iid,)).fetchone()[0]
            else:
                continue
            if iid and band != "suspended":
                members.append((iid, role))
        ids = [i for i, _ in members]
        if len(members) >= 2 and len(set(ids)) == len(ids) and set(ids) & set(met.values()):
            wanted["keigo:" + ",".join(map(str, sorted(ids)))] = (row[0][0], members)
    for c in con.execute("SELECT id, key FROM contrasts WHERE kind='keigo'").fetchall():
        if c["key"] not in wanted:
            con.execute("DELETE FROM contrast_items WHERE contrast_id=?", (c["id"],))
            con.execute("DELETE FROM contrasts WHERE id=?", (c["id"],))
    made = 0
    for key, (label, members) in wanted.items():
        if con.execute("SELECT 1 FROM contrasts WHERE key=?", (key,)).fetchone():
            continue
        cid = con.execute("INSERT INTO contrasts(kind, key, label, guid, created) VALUES('keigo',?,?,?,?)", (key, label, uuid.uuid4().hex[:16], db.now())).lastrowid
        con.executemany("INSERT INTO contrast_items(contrast_id, item_id, ord, role) VALUES(?,?,?,?)", [(cid, i, n, role) for n, (i, role) in enumerate(members)])
        made += 1
    return made


def _look_alikes(con) -> int:
    """Cards for kanji that look alike. Each kanji you have met that has look-alikes among the others
    you have met gets a card with its nearest one or two. A kanji may appear on several cards; a
    card is kept as long as its members are still listed as alike."""
    kanji, _ = _met(con)
    item_of: dict = {}
    for k in kanji:                               # a kanji is shown through its most frequent reading
        item_of.setdefault(k["lemma"], k["id"])
    literal_of = {v: k for k, v in item_of.items()}
    similar = dictdata.similar_kanji()
    near = {a: [b for b in similar.get(a, []) if b in item_of and b != a] for a in item_of}
    covered: set = set()
    for c in con.execute("SELECT id FROM contrasts WHERE kind='look'").fetchall():
        ids = [r[0] for r in con.execute("SELECT item_id FROM contrast_items WHERE contrast_id=? ORDER BY ord", (c["id"],))]
        lits = [literal_of.get(i) for i in ids]
        if None in lits or not all(b in near[lits[0]] for b in lits[1:]):
            con.execute("DELETE FROM contrast_items WHERE contrast_id=?", (c["id"],))
            con.execute("DELETE FROM contrasts WHERE id=?", (c["id"],))
        else:
            covered |= set(lits)
    made = 0
    for a in item_of:
        if a in covered or not near[a]:
            continue
        take = [a] + near[a][:LOOK - 1]
        key = "look:" + ",".join(map(str, sorted(item_of[x] for x in take)))
        covered |= set(take)
        if con.execute("SELECT 1 FROM contrasts WHERE key=?", (key,)).fetchone():
            continue
        cid = con.execute("INSERT INTO contrasts(kind, key, label, guid, created) VALUES('look',?,?,?,?)",
                          (key, "".join(take), uuid.uuid4().hex[:16], db.now())).lastrowid
        con.executemany("INSERT INTO contrast_items(contrast_id, item_id, ord) VALUES(?,?,?)", [(cid, item_of[x], n) for n, x in enumerate(take)])
        made += 1
    return made


def view(con, c) -> dict:
    """One contrast card as shown: its members, each with what tells it apart."""
    members = []
    for it in con.execute("""SELECT i.*, ci.role, EXISTS(SELECT 1 FROM occ o WHERE o.item_id=i.id) AS met
                             FROM contrast_items ci JOIN items i ON i.id=ci.item_id WHERE ci.contrast_id=? ORDER BY ci.ord""", (c["id"],)):
        m = dict(id=it["id"], lemma=it["lemma"], reading=it["reading"], gloss=(it["gloss"] or "").split(";")[0].split("|")[0].strip(), band=it["band"],
                 word=None, role=it["role"], met=bool(it["met"]) or it["kind"] == "kanji")
        if it["kind"] == "kanji":                             # a kanji is shown inside a word you have met
            w = con.execute("""SELECT w.lemma, w.reading, w.gloss FROM word_kanji wk JOIN items w ON w.id=wk.word_id
                               LEFT JOIN occ o ON o.item_id=w.id WHERE wk.kanji_id=? AND w.band<>'suspended'
                               GROUP BY w.id ORDER BY COALESCE(SUM(o.n),0) DESC, w.id LIMIT 1""", (it["id"],)).fetchone()
            if w:
                m["word"] = dict(lemma=w["lemma"], reading=w["reading"], gloss=(w["gloss"] or "").split(";")[0].split("|")[0].strip())
        members.append(m)
    if c["kind"] == "keigo":                              # which is which: plain, respectful (they do it), humble (I do it)
        front = "　".join(m["lemma"] for m in members)
        back = "\n".join(f"{m['lemma']}【{m['reading']}】 {m['role']}: {m['gloss']}" for m in members)
    elif c["kind"] == "sound":
        front, back = members[0]["reading"], "\n".join(f"{m['lemma']}  {m['gloss']}" for m in members)
    else:
        front = "　".join((m["word"] or m)["lemma"] for m in members)
        back = "\n".join(f"{m['lemma']}【{m['reading']}】 " + (f"{m['word']['lemma']}【{m['word']['reading']}】 {m['word']['gloss']}" if m["word"] else m["gloss"]) for m in members)
    mem = None
    if con.execute("SELECT 1 FROM sqlite_master WHERE name='contrast_memory'").fetchone():      # its own reviews on the device, once synced
        r = con.execute("SELECT * FROM contrast_memory WHERE contrast_id=?", (c["id"],)).fetchone()
        if r:
            days = round((r["due"] - r["last_review"]) / 86400)
            mem = dict(state="mature" if days >= 21 else "young", interval=days, due_days=round((r["due"] - db.now()) / 86400))
    return dict(id=c["id"], guid=c["guid"], kind=c["kind"], kind_label=KINDS[c["kind"]], label=c["label"], members=members, front=front, back=back,
                met=sum(m["met"] for m in members), memory=mem)


def listing(con, kind: str | None = None, limit: int = 1000, offset: int = 0) -> list[dict]:
    rows = con.execute("SELECT * FROM contrasts" + (" WHERE kind=?" if kind else "") + " ORDER BY kind, id LIMIT ? OFFSET ?",
                       ((kind, limit, offset) if kind else (limit, offset))).fetchall()
    return [view(con, c) for c in rows]


def counts(con) -> dict:
    return {k: n for k, n in con.execute("SELECT kind, COUNT(*) FROM contrasts GROUP BY kind")}
