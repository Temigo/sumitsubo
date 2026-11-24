"""Build real Anki deck files with Anki's own library: two decks with scheduling, exported as .apkg
in the current and the older format, without scheduling, and as .colpkg."""
import sys
from anki.collection import Collection, DeckIdLimit, ExportAnkiPackageOptions
out = sys.argv[1]
col = Collection(f"{out}/c.anki2")
mm = col.models
m = mm.new("Core Vocab")
for f in ("Expression", "Reading", "Meaning", "Sentence"):
    mm.add_field(m, mm.new_field(f))
t = mm.new_template("Recognition"); t["qfmt"] = "{{Expression}}"; t["afmt"] = "{{Reading}}<br>{{Meaning}}"; mm.add_template(m, t); mm.add(m)
m2 = mm.new("Furigana card")
for f in ("Front", "Back"):
    mm.add_field(m2, mm.new_field(f))
t = mm.new_template("Card 1"); t["qfmt"] = "{{Front}}"; t["afmt"] = "{{Back}}"; mm.add_template(m2, t); mm.add(m2)
d1, d2 = col.decks.id("Japanese::Core"), col.decks.id("Physics terms")
core = [("検出器", "けんしゅつき", "detector", "まず、<b>検出器</b>の構成を説明します。", 2, 120), ("較正", "こうせい", "calibration", "較正の結果を報告する。", 2, 30),
        ("誤差", "ごさ", "error", "誤差を評価する。", 2, 5), ("勾配", "こうばい", "gradient", "", 1, 0), ("測定", "そくてい", "measurement", "[sound:x.mp3]測定を行う。", 0, 0),
        ("沢山", "たくさん", "a lot", "", 2, 400), ("これはペンです。", "", "sentence card", "", 2, 50), ("食べる", "たべる", "to eat", "", 2, 200)]
for w, r, mean, sent, typ, ivl in core:
    n = col.new_note(mm.by_name("Core Vocab")); n["Expression"], n["Reading"], n["Meaning"], n["Sentence"] = w, r, mean, sent
    col.add_note(n, d1); c = n.cards()[0]; c.type = c.queue = typ; c.ivl = ivl; c.reps = 5 if typ else 0; col.update_card(c)
for front, typ, ivl in [(" 分解能[ぶんかいのう]", 2, 60), ("系統[けいとう]", 2, 10), ("統計[とうけい]", 0, 0), ("鞍点[あんてん]", 2, 90)]:
    n = col.new_note(mm.by_name("Furigana card")); n["Front"], n["Back"] = front, "x"
    col.add_note(n, d2); c = n.cards()[0]; c.type = c.queue = typ; c.ivl = ivl; c.reps = 3 if typ else 0; col.update_card(c)
# A real review history for two of the words (rows as Anki writes them: id is the time in ms, ease 1-4,
# type 0 learning / 1 review / 2 relearning), and a stored FSRS state for a third.
import time
from anki.cards import FSRSMemoryState
now, DAY = int(time.time()), 86400
cid = lambda word: col.db.scalar("select c.id from cards c join notes n on n.id=c.nid where n.sfld=?", word)  # noqa: E731
history = {"検出器": [(200, 3, 0), (199, 3, 1), (195, 3, 1), (184, 3, 1), (160, 3, 1), (100, 4, 1)],
           "誤差": [(30, 3, 0), (29, 3, 1), (25, 1, 1), (25, 3, 2), (22, 3, 1), (16, 2, 1), (9, 3, 1)]}
for word, rows in history.items():
    for k, (days_ago, ease, typ) in enumerate(rows):
        col.db.execute("insert into revlog values (?,?,?,?,?,?,?,?,?)", (now - days_ago * DAY) * 1000 + k, cid(word), -1, ease, 10, 5, 2500, 6000, typ)
c = col.get_card(cid("較正")); c.memory_state = FSRSMemoryState(stability=48.5, difficulty=6.2); col.update_card(c)
opt = lambda sched, legacy: ExportAnkiPackageOptions(with_scheduling=sched, with_deck_configs=False, with_media=False, legacy=legacy)  # noqa: E731
col.export_anki_package(out_path=f"{out}/new.apkg", options=opt(True, False), limit=None)
col.export_anki_package(out_path=f"{out}/legacy.apkg", options=opt(True, True), limit=None)
col.export_anki_package(out_path=f"{out}/noschedule.apkg", options=opt(False, False), limit=DeckIdLimit(deck_id=d2))
col.export_collection_package(f"{out}/all.colpkg", include_media=False, legacy=False)
col.close()
