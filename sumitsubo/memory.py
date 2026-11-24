"""How well each item is known, kept the way spaced-repetition programs keep it.

There is no "known" flag. An item is New (never studied), or it has a memory state: Learning, or
Review with a stability in days (the interval at which you would still recall it nine times out of
ten). A Review item with 21 days or more is called Mature, under that Young, as in Anki. An item
can also be Suspended (ignored). How likely you are to recall an item today follows from its
stability and the time since its last review; difficulty scores use that probability.

The scheduling model is FSRS, through the py-fsrs library with its default parameters.

Where a state comes from, strongest first:
  your own mark    "I know this" (in review, about a year out), "Learn this" (New), "Ignore"
  an import        the state carried over from Anki, Renshuu or a list; a kanji reading takes the
                   state of a kanji studied as a character, or of the best-known imported word with it
  your level       words and kanji at or below it are treated as in review with a long interval
                   and come back once, four to eight months later, as a check
"""
import datetime as dt
import math
import random

from . import db

MATURE = 21.0                       # days of stability from which a review item counts as mature
ASSUMED = dict(assumed=(180.0, 120, 240), user=(365.0, 240, 480))   # stability, then the window (days) for the first check
DAY = 86400.0
_scheduler = None


def scheduler():
    global _scheduler
    if _scheduler is None:
        from fsrs import Scheduler  # noqa: PLC0415
        _scheduler = Scheduler(enable_fuzzing=False)
    return _scheduler


def recall(stability: float | None, last_review: float | None, now: float | None = None) -> float:
    """Probability of recalling an item now (FSRS forgetting curve). New items: 0."""
    if not stability or stability <= 0:
        return 0.0
    days = max(0.0, ((now or db.now()) - (last_review or 0)) / DAY) if last_review else 0.0
    decay = -scheduler().parameters[20]
    factor = 0.9 ** (1 / decay) - 1
    return (1 + factor * days / stability) ** decay


def band(state: str | None, stability: float | None) -> str:
    if not state:
        return "new"
    return "mature" if state == "review" and (stability or 0) >= MATURE else "young"


LABEL = dict(new="New", young="Young", mature="Mature", suspended="Suspended")


def label(state: str | None, stability: float | None, suspended: bool = False) -> str:
    """The name an Anki user would expect: New, Learning, Young, Mature, Suspended."""
    if suspended:
        return "Suspended"
    if state in ("learning", "relearning"):
        return "Learning"
    return LABEL[band(state, stability)]


# ---- making a state ----------------------------------------------------------------------------------
def assumed(item_id: int, since: float, kind: str = "assumed") -> dict:
    """A long assumed interval with the first check spread out, so they do not all fall on one day.
    The same item always gets the same day."""
    stability, lo, hi = ASSUMED[kind]
    days = random.Random(item_id * 7919 + (1 if kind == "user" else 0)).uniform(lo, hi)
    return dict(state="review", stability=stability, difficulty=5.0, last_review=since, due=since + days * DAY)


def replay(reviews: list) -> dict | None:
    """The state reached by a history of (time, rating) reviews, under FSRS."""
    from fsrs import Card, Rating, State  # noqa: PLC0415
    card, n = Card(), 0
    for ts, rating in sorted(reviews):
        if rating not in (1, 2, 3, 4):
            continue
        card, _ = scheduler().review_card(card, Rating(rating), dt.datetime.fromtimestamp(ts, dt.timezone.utc))
        n += 1
    if not n or card.stability is None:
        return None
    state = {State.Learning: "learning", State.Review: "review", State.Relearning: "relearning"}[card.state]
    return dict(state=state, stability=float(card.stability), difficulty=float(card.difficulty),
                last_review=card.last_review.timestamp(), due=card.due.timestamp())


def from_interval(interval_days: float, ease: float | None, last_review: float, learning: bool = False) -> dict:
    """A state from an interval and an ease factor when there is no history to replay. The interval
    is, near enough, the stability; ease 1.3 maps to the hardest difficulty and 2.5 to the middle."""
    stability = max(0.5, float(interval_days))
    difficulty = min(10.0, max(1.0, 10.0 - ((ease or 2.5) - 1.3) * (5.0 / 1.2)))
    return dict(state="learning" if learning else "review", stability=stability, difficulty=difficulty,
                last_review=last_review, due=last_review + stability * DAY)


def from_percent(pct: float, now: float) -> dict | None:
    """An estimate from a mastery percentage (Renshuu, lists): 100% is taken as six months of
    stability, 50% as about two weeks, on a smooth curve. Rougher than a real history."""
    if pct <= 0:
        return None
    stability = 180.0 ** (min(100.0, pct) / 100.0)
    return dict(state="review" if stability >= 1 else "learning", stability=stability, difficulty=5.0,
                last_review=now, due=now + stability * DAY)


# ---- your own marks ----------------------------------------------------------------------------------
def mark(con, item_id: int, value: str | None):
    """known, new, ignored, or None to take the mark back."""
    if value is None:
        con.execute("DELETE FROM marks WHERE item_id=?", (item_id,))
    else:
        con.execute("INSERT OR REPLACE INTO marks VALUES(?,?,?)", (item_id, value, db.now()))


# ---- bringing every state up to date -------------------------------------------------------------------
def recompute(con):
    """Rebuild the states that follow from marks, imports and the level, and each item's band.
    States from real reviews (origin 'review') are left alone. Cheap; safe to call at any time."""
    level = int(db.setting(con, "level") or 0)
    since = float(db.setting(con, "level_set_at") or 0)
    if not since:
        since = db.now()
        db.set_setting(con, "level_set_at", since)
    con.execute("DELETE FROM memory WHERE origin IN ('assumed','user','import')")
    marks = {r["item_id"]: (r["mark"], r["ts"]) for r in con.execute("SELECT * FROM marks")}
    rows = []
    have = {(r[0], r[1]) for r in con.execute("SELECT item_id, skill FROM memory")}
    items = con.execute("SELECT id, kind, level, created, status, known_by FROM items").fetchall()
    # the best state any import brings for an item: the most recently reviewed
    imported: dict[int, dict] = {}
    for r in con.execute("SELECT * FROM import_items WHERE stability IS NOT NULL ORDER BY last_review"):
        imported[r["item_id"]] = dict(state=r["state"], stability=r["stability"], difficulty=r["difficulty"],
                                      last_review=r["last_review"], due=r["due"])
    # a kanji reading inherits from the best-known imported word that contains it
    for r in con.execute("""SELECT wk.kanji_id, m.item_id FROM word_kanji wk JOIN import_items m ON m.item_id=wk.word_id
                            WHERE m.stability IS NOT NULL"""):
        w = imported.get(r["item_id"])
        if w and w["state"] == "review" and w["stability"] > imported.get(-r["kanji_id"], {}).get("stability", 0):
            imported[-r["kanji_id"]] = w                      # kept apart (negative key) from a direct import of the item
    # a kanji studied as a character (a Renshuu kanji schedule) covers the readings that study included
    direct: dict[str, list] = {}
    for r in con.execute("SELECT * FROM import_kanji ORDER BY last_review"):
        direct.setdefault(r["literal"], []).append((set((r["readings"] or "").split()), dict(
            state=r["state"], stability=r["stability"], difficulty=r["difficulty"], last_review=r["last_review"], due=r["due"])))
    if direct:
        for k in con.execute("SELECT id, lemma, reading FROM items WHERE kind='kanji'"):
            for readings, seed in direct.get(k["lemma"], []):
                if not readings or k["reading"] in readings:
                    imported[k["id"]] = seed
    suspended = set()
    for it in items:
        iid, skill = it["id"], it["kind"]
        m = marks.get(iid)
        if m and m[0] == "ignored" or (not m and it["status"] == "ignored"):
            suspended.add(iid)
            continue
        if (iid, skill) in have or (m and m[0] == "new"):
            continue
        seed, origin = None, None
        if m and m[0] == "known":
            seed, origin = assumed(iid, m[1], "user"), "user"
        covered = bool(level and it["level"] and it["level"] >= level)
        inherited = imported.get(-iid)            # a kanji reading, from an imported word
        if inherited and covered and inherited["stability"] < ASSUMED["assumed"][0]:
            inherited = None                      # a young word says little about a kanji your level covers
        if m and m[0] == "known":
            pass
        elif iid in imported or inherited:
            seed, origin = imported.get(iid) or inherited, "import"
        elif covered:
            seed, origin = assumed(iid, max(since, it["created"] or 0)), "assumed"
        if seed:
            rows.append((iid, skill, seed["state"], seed["stability"], seed["difficulty"], seed["last_review"], seed["due"], origin))
    con.executemany("INSERT OR REPLACE INTO memory VALUES(?,?,?,?,?,?,?,?)", rows)
    con.execute("UPDATE items SET band='new'")
    con.execute("""UPDATE items SET band=CASE WHEN m.state='review' AND m.stability>=? THEN 'mature' ELSE 'young' END
                   FROM memory m WHERE m.item_id=items.id AND m.skill=items.kind""", (MATURE,))
    if suspended:
        con.executemany("UPDATE items SET band='suspended' WHERE id=?", [(i,) for i in suspended])


def set_level(con, level: int):
    """State your level. The assumption is dated from now, and the first checks spread out from here."""
    db.set_setting(con, "level", level)
    db.set_setting(con, "level_set_at", db.now())


def migrate(con) -> bool:
    """Convert data written before version 0.2.0, which kept a known/learning/ignored status.
    Returns True if anything was converted (imports then need applying again, see imports.reapply_all)."""
    if db.setting(con, "model") == str(db.MODEL):
        return False
    now = db.now()
    con.execute("UPDATE items SET created=? WHERE created IS NULL", (now,))
    for status, value in (("known", "known"), ("ignored", "ignored"), ("learning", "new")):
        con.execute("INSERT OR IGNORE INTO marks SELECT id, ?, ? FROM items WHERE status=? AND known_by='user'", (value, now, status))
    con.execute("UPDATE items SET status='learning', known_by=NULL WHERE NOT (status='ignored' AND known_by='auto')")
    con.execute("UPDATE imports SET mode=CASE mode WHEN 'known' THEN 'progress' WHEN 'learning' THEN 'studied' ELSE mode END")
    if not db.setting(con, "level_set_at"):
        db.set_setting(con, "level_set_at", now)
    stale = bool(con.execute("SELECT 1 FROM import_batches LIMIT 1").fetchone())
    db.set_setting(con, "imports_stale", int(stale))
    db.set_setting(con, "model", db.MODEL)
    recompute(con)
    return True


def describe(m, now: float | None = None) -> dict | None:
    """A memory row as something to show: label, recall today, days until due, where it comes from."""
    if not m:
        return None
    now = now or db.now()
    why = dict(assumed="assumed from your level", user="marked by you", **{"import": "carried over from an import"}, review="from your reviews")
    return dict(state=m["state"], stability=round(m["stability"], 1), difficulty=round(m["difficulty"] or 0, 1),
                recall=round(100 * recall(m["stability"], m["last_review"], now)), due_days=math.ceil((m["due"] - now) / DAY),
                last_review=m["last_review"], due=m["due"], origin=m["origin"], why=why.get(m["origin"], ""),
                label=label(m["state"], m["stability"]))
