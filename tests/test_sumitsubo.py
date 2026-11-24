"""End-to-end tests. Run from the project folder:  pytest -q

They need no network, no GPU and no Anki. The dictionary is a small fixture (tests/fixture) in the
same file layout as the real downloads. Anki Miner must be installed, since its parser is used.
"""
import json
import urllib.parse
import time
import os
import shutil
import sqlite3
import subprocess
import tempfile
import zipfile
from pathlib import Path

import pytest

HERE = Path(__file__).parent
S = HERE / "samples"
_tmp = tempfile.mkdtemp(prefix="sumitsubo-test-")
os.environ["SUMITSUBO_DATA"] = _tmp
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from sumitsubo import analyze, cards, config, contrast, db, dictdata, frequency, furigana, ingest, memory, ocr, register  # noqa: E402


@pytest.fixture(scope="module")
def eng():
    fx = HERE / "fixture"
    counts = dictdata.build(jmdict=str(fx / "jmdict-eng-fixture.json.zip"), kanjidic=str(fx / "kanjidic2-en-fixture.json.zip"),
                            jlpt_dir=str(fx), quiet=True)
    assert counts["entries"] > 2000 and counts["kanji"] > 1000 and counts["jlpt"] > 300
    db.init()
    from sumitsubo.parser import Parser
    dic = dictdata.Dictionary()
    return dic, Parser(dic)


def run(eng, target=None, **kw) -> int:
    with db.session() as con:
        sid = ingest.add(con, str(target) if target else None, **kw)
    ingest.process(sid, *eng)
    with db.session() as con:
        r = con.execute("SELECT status, message FROM sources WHERE id=?", (sid,)).fetchone()
    assert r["status"] == "done", r["message"]
    return sid


def test_alignment(eng):
    dic, _ = eng
    al = lambda w, r: furigana.align(w, r, dic.kanji)
    assert al("分解能", "ぶんかいのう") == [("分", "ぶん"), ("解", "かい"), ("能", "のう")]
    assert al("発表", "はっぴょう") == [("発", "はつ"), ("表", "ひょう")]          # sound changes map back to the base reading
    assert al("受付", "うけつけ") == [("受", "うけ"), ("付", "つけ")]
    assert al("取り組む", "とりくむ") == [("取", "と"), ("組", "く")]
    assert al("今日", "きょう") is None                                          # cannot be split by kanji


def test_parser_joins_dictionary_words(eng):
    _, parser = eng
    forms = lambda s: [t.form for t in parser.parse([s])[0]]
    assert "走り出す" in forms("走り出した粒子が磁場によって曲げられる。")
    assert "先行研究" in forms("先行研究に基づいて仮説を立てる。")
    got = forms("思わず声を上げてしまいましたが、実は結果が再現できなかった。")
    assert "思わず" in got and "実は" in got and "思う" not in got


def test_talk_with_audio(eng):
    audio = Path(_tmp) / "talk.mp3"
    if shutil.which("ffmpeg"):
        subprocess.run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=330:duration=100",
                        "-ac", "1", "-b:a", "32k", str(audio)], check=True)
    sid = run(eng, S / "physics_talk.srt", media=str(audio) if audio.exists() else None, tags="physics/detector, seminar",
              title="Detector talk")
    with db.session() as con:
        kinds = dict(con.execute("SELECT kind, COUNT(*) FROM cards WHERE source_id=? GROUP BY kind", (sid,)).fetchall())
        assert kinds["word"] > 20 and kinds["kanji"] > 10
        assert db.tags_of(con, sid) == ["physics/detector", "seminar"]
        # Order is 1..n with no gaps, cards still to learn before the mature ones.
        ranks = [r[0] for r in con.execute("SELECT rank FROM cards WHERE source_id=? ORDER BY rank", (sid,))]
        assert ranks == list(range(1, len(ranks) + 1))
        order = [c["band"] for c in cards.listing(con, source_id=sid)]
        assert "mature" in order and "new" in order and order == sorted(order, key=lambda b: b == "mature")
        # Every kanji card's reading really occurs in each of its context words.
        for c in cards.listing(con, source_id=sid, kind="kanji"):
            assert c["words"], c["lemma"]
            for w in c["words"]:
                pairs = furigana.align(w["lemma"], w["reading"], eng[0].kanji)
                assert (c["lemma"], c["reading"]) in pairs
        # A field term is New. A word the N3 level covers is not New: it is in review with a long
        # assumed interval, so it has a card that will come back once as a check.
        lemmas = {c["lemma"] for c in cards.listing(con, source_id=sid, kind="word", bands=cards.TO_LEARN)}
        assert "検出器" in lemmas and "説明" not in lemmas and "使う" not in lemmas
        easy = next(c for c in cards.listing(con, source_id=sid, kind="word", bands=("mature",)) if c["lemma"] == "説明")
        assert easy["state"] == "Mature" and easy["memory"]["origin"] == "assumed" and easy["memory"]["stability"] == 180
        assert 120 <= easy["memory"]["due_days"] <= 240 and easy["memory"]["recall"] >= 99
        due = [r[0] for r in con.execute("SELECT due FROM memory WHERE origin='assumed'")]
        assert len({round(d / 86400) for d in due}) > 20          # spread out, not all on one day
        # Short grammatical expressions are set aside, not carded.
        assert not lemmas & {"について", "それでは"}
        if audio.exists():
            assert kinds.get("listen", 0) >= 1
            clips = con.execute("SELECT clip FROM sentences WHERE source_id=? AND clip IS NOT NULL", (sid,)).fetchall()
            assert clips and all(config.resolve(r[0]).stat().st_size > 500 for r in clips)
            for c in cards.listing(con, source_id=sid, kind="listen"):      # exactly one unfamiliar word in the sentence
                n = con.execute("""SELECT COUNT(*) FROM occ o JOIN items i ON i.id=o.item_id
                                   WHERE o.sentence_id=? AND i.band IN ('new','young')""", (c["sentence_id"],)).fetchone()[0]
                assert n == 1 and c["state"] == "New"
        cov = analyze.coverage(con, sid)
        assert cov["pct"]["queued"] == 0 and cov["pct"]["young"] == 0 and abs(sum(cov["pct"].values()) - 100) < 0.3
        assert abs(cov["readable"] - cov["pct"]["mature"]) < 1    # just assumed, so recall is still near certain


def test_second_source_shows_the_stack(eng):
    with db.session() as con:
        first = con.execute("SELECT id FROM sources ORDER BY id LIMIT 1").fetchone()[0]
        before = {c["lemma"]: [w["lemma"] for w in c["words"]] for c in cards.listing(con, source_id=first, kind="kanji")}
    sid = run(eng, S / "math_paper.txt", tags="math/optimization")
    with db.session() as con:
        cov = analyze.coverage(con, sid)
        added = json.loads(con.execute("SELECT added_score FROM sources WHERE id=?", (sid,)).fetchone()[0])
        assert cov["pct"]["queued"] > 0 and cov["unique"]["queued"] >= 2     # 増幅, 課題, 解析: met in the talk
        assert added["pct"]["queued"] == cov["pct"]["queued"] and added["pct"]["new"] > 0
        # A word met in both sources has one card, in the first source.
        r = con.execute("SELECT COUNT(*), MIN(source_id) FROM cards c JOIN items i ON i.id=c.item_id WHERE i.lemma='増幅' AND c.kind='word'").fetchone()
        assert tuple(r) == (1, first)
        # Kanji cards of the first source gained context words from the second.
        after = {c["lemma"]: [w["lemma"] for w in c["words"]] for c in cards.listing(con, source_id=first, kind="kanji")}
        assert any(len(after[k]) > len(before[k]) for k in before if k in after)
        assert [r[0] for r in con.execute("SELECT form FROM unverified WHERE source_id=?", (sid,))] == ["鞍点"]


def test_score_without_adding(eng):
    text = (S / "story.txt").read_text(encoding="utf-8").splitlines()
    with db.session() as con:
        n = con.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        sc = analyze.score_text(con, *eng, text)
        assert con.execute("SELECT COUNT(*) FROM items").fetchone()[0] == n     # nothing stored
    assert sc["total_words"] > 40 and 0 < sc["pct"]["mature"] < 100 and sc["kanji_new"] > 0
    assert sc["readable"] == sc["pct"]["mature"] and set(sc["pct"]) == set(analyze.BANDS)
    old = analyze.normalise_score(dict(pct=dict(known=40.0, stack=10.0, new=50.0), unique=dict(known=4, stack=1, new=5), kanji_new=3, tier="very hard"))
    assert old["pct"] == dict(mature=40.0, queued=10.0, new=50.0, young=0.0) and old["readable"] == 40.0   # scores saved before 0.2.0


def test_level_and_manual_marks(eng):
    """There is no "known" flag: the level and "I know this" put an item in review with a long interval."""
    learn = lambda con: con.execute("SELECT COUNT(*) FROM cards c JOIN items i ON i.id=c.item_id WHERE i.band IN ('new','young')").fetchone()[0]  # noqa: E731
    mem = lambda con, it: con.execute("SELECT * FROM memory WHERE item_id=? AND skill='word'", (it,)).fetchone()  # noqa: E731
    with db.session() as con:
        base, total = learn(con), con.execute("SELECT COUNT(*) FROM cards WHERE kind<>'listen'").fetchone()[0]
        it = con.execute("SELECT id FROM items WHERE lemma='検出器'").fetchone()[0]
        assert mem(con, it) is None                                          # New: no memory state yet
        analyze.set_status(con, it, "known")
        analyze.rebuild_cards(con)
        m = mem(con, it)
        assert (m["state"], m["stability"], m["origin"]) == ("review", 365, "user") and 240 <= (m["due"] - m["last_review"]) / 86400 <= 480
        card = con.execute("SELECT * FROM cards WHERE item_id=? AND kind='word'", (it,)).fetchone()
        assert card is not None and cards.view(con, card)["state"] == "Mature"   # the card stays, to come back as a check
        analyze.apply_level(con, 1)                                          # N1: everything on the lists is covered
        analyze.rebuild_cards(con)
        n1 = learn(con)
        analyze.apply_level(con, 0)                                          # nothing assumed
        analyze.rebuild_cards(con)
        n0 = learn(con)
        assert n1 < base < n0
        assert con.execute("SELECT COUNT(*) FROM memory WHERE origin='assumed'").fetchone()[0] == 0
        assert mem(con, it)["origin"] == "user"                              # your own mark outlives a change of level
        assert con.execute("SELECT COUNT(*) FROM cards WHERE kind<>'listen'").fetchone()[0] == total   # every item keeps its card
        analyze.apply_level(con, 3)
        analyze.set_status(con, it, "ignored")
        analyze.rebuild_cards(con)
        assert con.execute("SELECT band FROM items WHERE id=?", (it,)).fetchone()[0] == "suspended"
        assert not con.execute("SELECT 1 FROM cards WHERE item_id=?", (it,)).fetchone()   # only a suspended item has no card
        easy = con.execute("SELECT id FROM items WHERE lemma='説明'").fetchone()[0]
        analyze.set_status(con, easy, "learning")                            # "Learn this" beats the level
        analyze.rebuild_cards(con)
        assert mem(con, easy) is None and con.execute("SELECT band FROM items WHERE id=?", (easy,)).fetchone()[0] == "new"
        for i in (it, easy):
            analyze.set_status(con, i, "undo")
        analyze.rebuild_cards(con)
        assert learn(con) == base and mem(con, it) is None and mem(con, easy)["origin"] == "assumed"
        assert con.execute("SELECT COUNT(*) FROM marks").fetchone()[0] == 0


def test_memory_states():
    now = 1_800_000_000.0
    assert memory.recall(None, None, now) == 0 and memory.recall(30, now, now) == 1
    assert abs(memory.recall(30, now - 30 * 86400, now) - 0.9) < 0.005       # stability: the interval at which recall is 90%
    assert 0.6 < memory.recall(30, now - 300 * 86400, now) < 0.75
    assert [memory.label(*x) for x in ((None, None), ("learning", 0.5), ("review", 5), ("review", 21), ("relearning", 2))] == ["New", "Learning", "Young", "Mature", "Learning"]
    good = memory.replay([(now + d * 86400, 3) for d in (0, 1, 4, 15, 40)])
    assert good["state"] == "review" and good["stability"] > 60 and good["last_review"] == now + 40 * 86400 and good["due"] > good["last_review"]
    lapsed = memory.replay([(now, 3), (now + 86400, 3), (now + 5 * 86400, 1)])
    assert lapsed["stability"] < 3 and lapsed["difficulty"] > good["difficulty"]
    assert memory.replay([]) is None and memory.replay([(now, 0)]) is None    # manual reschedules are not reviews
    conv = memory.from_interval(45, 2.5, now)
    assert (conv["state"], conv["stability"], conv["difficulty"]) == ("review", 45, 5.0) and memory.from_interval(10, 1.3, now)["difficulty"] == 10
    assert memory.from_percent(0, now) is None and 12 < memory.from_percent(50, now)["stability"] < 15 and memory.from_percent(100, now)["stability"] == 180
    a, b = memory.assumed(7, now), memory.assumed(8, now)
    assert a == memory.assumed(7, now) and a["due"] != b["due"] and all(120 <= (x["due"] - now) / 86400 <= 240 for x in (a, b))


def test_glossary_terms_become_words(eng):
    dictdata.add_glossary(S / "physics.tsv")
    from sumitsubo.parser import Parser
    dic = dictdata.Dictionary()
    eng2 = (dic, Parser(dic))
    with db.session() as con:
        ids = [r[0] for r in con.execute("SELECT id FROM sources ORDER BY id")]
    for sid in ids:
        ingest.process(sid, *eng2)
    with db.session() as con:
        assert con.execute("SELECT COUNT(*) FROM unverified").fetchone()[0] == 0
        got = {r[0]: r[1] for r in con.execute(
            "SELECT i.lemma, i.custom FROM cards c JOIN items i ON i.id=c.item_id WHERE c.kind='word' AND i.lemma IN ('鞍点','系統誤差')")}
        assert got == {"鞍点": 1, "系統誤差": 1}


def test_anki_export_and_pack(eng):
    out = Path(_tmp) / "deck.apkg"
    with db.session() as con:
        res = cards.export_anki(con, out)
        total = con.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
        first = con.execute("SELECT id FROM sources ORDER BY id LIMIT 1").fetchone()[0]
        pack = cards.write_pack(con, first)
    with db.session() as con:
        learn = con.execute("""SELECT COUNT(*) FROM cards c JOIN items i ON i.id=c.item_id WHERE i.band IN ('new','young')
                               AND (c.kind<>'listen' OR (SELECT clip FROM sentences s WHERE s.id=c.sentence_id) IS NOT NULL)""").fetchone()[0]
        everything = cards.export_anki(con, Path(_tmp) / "all.apkg", mature=True)
    assert res["notes"] == learn < everything["notes"] <= total and res["decks"] == 2     # mature cards are left out unless asked for
    total = learn

    def guids(path):
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            z.extract("collection.anki2", Path(_tmp) / "x")
        c = sqlite3.connect(Path(_tmp) / "x" / "collection.anki2")
        g = {r[0] for r in c.execute("SELECT guid FROM notes")}
        tags = {r[0] for r in c.execute("SELECT tags FROM notes")}
        c.close()
        return g, names, tags
    g1, names, tags = guids(out)
    assert len(g1) == total and "media" in names and any("physics::detector" in t for t in tags)
    with db.session() as con:
        cards.export_anki(con, out)
    assert guids(out)[0] == g1                       # stable identity: re-import updates, never duplicates
    meta = json.loads((pack / "source.json").read_text(encoding="utf-8"))
    lines = [json.loads(x) for x in (pack / "cards.jsonl").read_text(encoding="utf-8").splitlines()]
    assert meta["format"] == config.PACK_FORMAT and meta["difficulty"]["pct"] and lines
    assert {x["kind"] for x in lines} >= {"word", "kanji"} and all(x["front"] and x["back"] for x in lines)
    seeded = [x["memory"] for x in lines if x["memory"]]                     # the state each card starts from on a device
    assert seeded and any(x["memory"] is None for x in lines)
    assert all(m["state"] == "review" and m["stability"] > 0 and m["due"] > m["last_review"] and m["origin"] for m in seeded)
    assert all((pack / "audio" / x["audio"]).exists() for x in lines if x["audio"])


def _make_epub(path: Path, title: str, paragraphs: list[str]):
    body = "".join(f"<p>{p}</p>" for p in paragraphs)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                   '<rootfiles><rootfile full-path="content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        z.writestr("content.opf", f'<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">'
                   f'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="id">t1</dc:identifier><dc:title>{title}</dc:title>'
                   '<dc:language>ja</dc:language></metadata><manifest><item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/></manifest>'
                   '<spine><itemref idref="c1"/></spine></package>')
        z.writestr("c1.xhtml", f'<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml"><head><title>{title}</title></head>'
                   f'<body>{body}</body></html>')


def test_epub_through_anki_miner(eng):
    if not (shutil.which("anki-miner") or Path(os.sys.executable).with_name("anki-miner").exists()):
        pytest.skip("anki-miner command not installed")
    epub = Path(_tmp) / "kobo.epub"
    _make_epub(epub, "木工所の少年", (S / "story.txt").read_text(encoding="utf-8").splitlines())
    sid = run(eng, epub, tags="fiction")
    with db.session() as con:
        s = con.execute("SELECT * FROM sources WHERE id=?", (sid,)).fetchone()
        assert s["kind"] == "doc" and s["n_sentences"] >= 14
        assert con.execute("SELECT COUNT(*) FROM cards WHERE source_id=? AND kind='kanji'", (sid,)).fetchone()[0] > 3
        assert con.execute("SELECT COUNT(*) FROM cards WHERE source_id=? AND kind='listen'", (sid,)).fetchone()[0] == 0


def test_bad_source_is_reported_not_raised(eng):
    with db.session() as con:
        sid = ingest.add(con, text="Hello, this has no Japanese at all.")
    ingest.process(sid, *eng)
    with db.session() as con:
        r = con.execute("SELECT status, message FROM sources WHERE id=?", (sid,)).fetchone()
        assert r["status"] == "error" and r["message"]
        ingest.delete_source(con, sid)


def test_other_dictionary_and_frequency(eng):
    z = Path(_tmp) / "zh.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("index.json", json.dumps(dict(title="test-zh", format=3, revision="1")))
        f.writestr("term_bank_1.json", json.dumps([["検出器", "けんしゅつき", "", "", 0, ["探测器"], 1, ""],
                                                   ["較正", "こうせい", "", "", 0, [{"type": "structured-content", "content": "校准"}], 2, ""]]))
    assert dictdata.add_yomitan(z) == 2
    zh = dictdata.Dictionary(gloss_dict="test-zh")
    assert zh.lookup("検出器", "けんしゅつき")["gloss"] == "探测器" and zh.lookup("較正", "こうせい")["gloss"] == "校准"
    assert dictdata.Dictionary().lookup("検出器", "けんしゅつき")["gloss"] == "detector"
    assert set(zh.dictionaries()) == {"jmdict-en", "test-zh"}


def test_frequency_is_a_local_file(eng):
    # Jiten's layout: Word, Form (the reading), Rank; the best rank of a word is kept
    assert dict(frequency.parse_csv("Word,Form,Rank\n検出器,けんしゅつき,120\n勾配,こうばい,45\n検出器,けんしゅつき,90\n")) == {"検出器": 90, "勾配": 45}
    assert frequency.parse_csv("の\nに\n") == [("の", 1), ("に", 2)]
    f = Path(_tmp) / "jiten-global-frequency.csv"
    f.write_text("Word,Form,Rank\n検出器,けんしゅつき,120\n勾配,こうばい,45\n", encoding="utf-8")
    assert frequency.load(f) == dict(name="Jiten global", ranks=2)
    assert dictdata.Dictionary().freq_rank("検出器") == 120 and frequency.current()["ranks"] == 2
    z = Path(_tmp) / "freq.zip"                       # a frequency dictionary in Yomitan format replaces it
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("index.json", json.dumps(dict(title="test-freq", format=3)))
        zf.writestr("term_meta_bank_1.json", json.dumps([
            ["検出器", "freq", 7], ["勾配", "freq", {"value": 30, "displayValue": "30"}],
            ["較正", "freq", {"reading": "こうせい", "frequency": {"value": 55}}], ["較正", "freq", {"reading": "こうせい", "frequency": 80}]]))
    assert frequency.load(z) == dict(name="test-freq", ranks=3)
    dic = dictdata.Dictionary()
    assert (dic.freq_rank("検出器"), dic.freq_rank("勾配"), dic.freq_rank("較正")) == (7, 30, 55)
    with db.session() as con:                         # words collected earlier pick the ranks up
        analyze.refresh_frequency(con, dic)
        got = {r[0]: r[1] for r in con.execute("SELECT lemma, freq FROM items WHERE kind='word' AND lemma IN ('検出器','勾配')")}
    assert got and all(v in (7, 30) for v in got.values())


def test_anki_export_by_source_and_tag(eng):
    import sqlite3
    def notes(path):
        tmp = Path(_tmp) / "x"
        shutil.rmtree(tmp, ignore_errors=True)
        with zipfile.ZipFile(path) as zf:
            zf.extractall(tmp)
        con = sqlite3.connect(next(p for p in tmp.iterdir() if p.name.startswith("collection.anki2")))
        decks = {v["name"] for v in json.loads(con.execute("SELECT decks FROM col").fetchone()[0]).values()} - {"Default"}
        n = con.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
        con.close()
        return n, decks
    out = Path(_tmp) / "sel.apkg"
    with db.session() as con:
        count = lambda ids, extra="": con.execute(  # noqa: E731 - cards to learn; listening cards without audio are not exported
            f"SELECT COUNT(*) FROM cards c JOIN items i ON i.id=c.item_id WHERE c.source_id IN ({','.join(map(str, ids))}) {extra} "
            "AND i.band IN ('new','young') AND (c.kind<>'listen' OR (SELECT clip FROM sentences s WHERE s.id=c.sentence_id) IS NOT NULL)").fetchone()[0]
        done = cards.select_sources(con)
        phys = cards.select_sources(con, tags=["physics"])          # physics covers physics/detector
        rest = [i for i in done if i not in phys]
        assert phys and rest and cards.select_sources(con, rest[:1], ["physics"]) == sorted(phys + rest[:1])
        assert cards.select_sources(con, tags=["no-such-tag"]) == []
        titles = {con.execute("SELECT title FROM sources WHERE id=?", (i,)).fetchone()[0] for i in phys}
        r = cards.export_anki(con, out, tags=["physics"], deck="Physics")
        assert r["notes"] == count(phys) > 0 and r["sources"] == len(phys)
        assert notes(out) == (count(phys), {f"Physics::{t}" for t in titles})
        r = cards.export_anki(con, out, done, deck="Everything", per_source=False)
        assert notes(out) == (count(done), {"Everything"}) and count(done) > count(phys)
        r = cards.export_anki(con, out, phys, kinds=["kanji"])
        assert r["notes"] == count(phys, "AND c.kind='kanji'") and 0 < r["notes"] < count(phys)


def test_bad_sentence_moves_or_drops_cards_and_comes_back(eng):
    other = """(SELECT COUNT(*) FROM occ o JOIN sentences s2 ON s2.id=o.sentence_id
                WHERE o.item_id=c.item_id AND o.source_id=c.source_id AND s2.id<>c.sentence_id)"""
    with db.session() as con:
        total = con.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
        for want_other in (True, False):             # a word with another sentence to move to, and one without
            card = con.execute(f"SELECT c.id, c.sentence_id, c.source_id, c.guid FROM cards c WHERE c.kind='word' AND {other}{'>0' if want_other else '=0'} LIMIT 1").fetchone()
            assert card, "the sample sources should have both kinds of word"
            text = con.execute("SELECT text FROM sentences WHERE id=?", (card["sentence_id"],)).fetchone()[0]
            res = analyze.reject_sentence(con, card["sentence_id"])
            assert res["moved"] + res["dropped"] >= 1
            now = con.execute("SELECT sentence_id, guid FROM cards WHERE id=?", (card["id"],)).fetchone()
            if want_other:                           # the card kept its identity and moved
                assert now and now["sentence_id"] != card["sentence_id"] and now["guid"] == card["guid"]
            else:                                    # no other sentence: the card is gone, and stays gone
                assert now is None
                analyze.rebuild_cards(con)
                assert not con.execute("SELECT 1 FROM cards c JOIN items i ON i.id=c.item_id WHERE c.kind='word' AND c.sentence_id=?", (card["sentence_id"],)).fetchone()
            assert not con.execute("SELECT 1 FROM cards WHERE sentence_id=?", (card["sentence_id"],)).fetchone()
            analyze.restore_sentence(con, card["source_id"], text)
            assert con.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == total     # nothing lost
        assert con.execute("SELECT COUNT(*) FROM rejected").fetchone()[0] == 0


def test_ocr_text_cleanup():
    raw = "検 出 器 の 較 正 に つ い て\n述 べ る 。 次 に 、 誤 差 を\n\n評価 する 。 最後 の 文 は 頁 を\f跨 ぐ 。 Fig. 2 を 見 よ 。"
    got = ocr.sentences(raw)
    assert [g["text"] for g in got] == ["検出器の較正について述べる。", "次に、誤差を", "評価する。", "最後の文は頁を跨ぐ。", "Fig. 2 を見よ。"]
    assert [g["location"] for g in got] == ["p.1", "p.1", "p.1", "p.2", "p.2"]


@pytest.mark.skipif(ocr.missing() is not None, reason="OCRmyPDF or Tesseract's Japanese model is not installed")
def test_scanned_pdf_is_read_by_ocr(eng):
    img2pdf, PIL = pytest.importorskip("img2pdf"), pytest.importorskip("PIL")
    from PIL import Image, ImageDraw, ImageFont
    fonts = [f for f in ("/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc", "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc") if Path(f).exists()]
    if not fonts:
        pytest.skip("no Japanese font to draw a test page with")
    font = ImageFont.truetype(fonts[0], 40, index=0)
    im = Image.new("L", (1240, 900), 250)
    d = ImageDraw.Draw(im)
    for i, line in enumerate(["検出器の分解能を測定した。", "統計誤差に比べて系統誤差が", "支配的であることが分かった。"]):
        d.text((110, 140 + i * 80), line, font=font, fill=20)
    png, pdf = Path(_tmp) / "scan.png", Path(_tmp) / "scan.pdf"
    im.save(png)
    pdf.write_bytes(img2pdf.convert([str(png)]))
    with db.session() as con:
        sid = ingest.add(con, str(pdf), title="Scanned page")
    ingest.process(sid, *eng)
    with db.session() as con:
        r = con.execute("SELECT status, message, options, n_sentences FROM sources WHERE id=?", (sid,)).fetchone()
        text = "".join(x[0] for x in con.execute("SELECT text FROM sentences WHERE source_id=? ORDER BY idx", (sid,)))
        assert r["status"] == "done", r["message"]
        assert json.loads(r["options"])["ocr"] == "horizontal" and "検出器" in text and "誤差" in text and " " not in text
        ingest.delete_source(con, sid)


def test_web_pages(eng):
    from fastapi.testclient import TestClient
    from sumitsubo import web
    web._state.update(dic=eng[0], parser=eng[1])
    with TestClient(web.app) as c:
        home = c.get("/")
        assert home.status_code == 200 and "Detector talk" in home.text and "known" in home.text
        with db.session() as con:
            first = con.execute("SELECT id FROM sources ORDER BY id LIMIT 1").fetchone()[0]
            sent = con.execute("SELECT id FROM sentences WHERE clip IS NOT NULL LIMIT 1").fetchone()
        for url in (f"/source/{first}", f"/source/{first}?show=cards&kind=kanji", f"/source/{first}?show=text", f"/source/{first}?show=known",
                    f"/source/{first + 1}?show=stack", "/cards", "/cards?tag=physics&kind=word", "/settings", "/contrast", "/contrast?kind=read",
                    f"/source/{first}?show=cards&kind=colloc", "/cards?kind=colloc&state=all"):
            r = c.get(url)
            assert r.status_code == 200, url
        assert "検出器" in c.get(f"/source/{first}?show=cards&kind=word").text
        if sent:
            a = c.get(f"/clip/{sent[0]}")
            assert a.status_code == 200 and a.headers["content-type"] == "audio/mpeg"
        r = c.post("/add", data=dict(text="損失関数の勾配を計算して重みを更新する。", action="score"))
        assert r.status_code == 200 and "not added" in r.text
        r = c.get("/export/anki")
        assert r.status_code == 200 and r.content[:2] == b"PK"
        r = c.get("/export/anki", params=dict(tag="physics", deck="Physics", layout="one", kind=["word", "kanji"]))
        assert r.status_code == 200 and r.content[:2] == b"PK" and "physics.apkg" in r.headers["content-disposition"]
        r = c.get("/export/anki", params=dict(tag="no-such-tag"), follow_redirects=False)
        assert r.status_code == 303 and "Nothing" in urllib.parse.unquote(r.headers["location"])
        assert 'name="upload"' in c.get("/add").text and "Export to Anki" in c.get("/export").text
        assert "sentences" in home.text and "readable today" in home.text and "last new word" in home.text and "Contrast" in c.get("/cards").text
        for url in ("/stats", "/reader", "/reader/study", "/reader/books", "/reader/card", "/settings/dictionaries", "/settings/engines", "/aside", "/inbox",
                    f"/?sel={first}&order=study", "/?q=detector", "/?tag=physics", "/cards?q=検出&state=all", "/contrast?p=2"):
            assert c.get(url).status_code == 200, url
        assert "Detector talk" in c.get("/?q=detector").text and "Detector talk" not in c.get("/?q=zzzz").text
        assert "Kanji by JLPT level" in c.get("/stats").text and 'class="kgrid"' in c.get("/stats/kanji").text
        rv = c.get("/stats/reviews").text
        assert "Last 12 weeks" in rv and "Due in the next 14 days" in rv and "days in a row" in rv
        # Contrast cards and what was suspended are views of the Cards page; the old addresses lead there.
        assert c.get("/contrast?kind=read", follow_redirects=False).headers["location"] == "/cards?kind=contrast&ck=read"
        assert c.get("/aside", follow_redirects=False).headers["location"] == "/cards?state=suspended"
        sus = c.get("/cards?state=suspended&mark=auto").text
        assert "Grammar words" in sus and "Bad sentences" in sus and "<h1>Cards</h1>" in sus
        assert "Look alike" in c.get("/cards?kind=contrast").text and "<h1>Cards</h1>" in c.get("/cards?kind=contrast").text
        # A difficulty check shows its figures and can be added as it is.
        chk = c.post("/add", data=dict(text="検出器の信号を測定する。友達に手紙を書いた。", action="score", tags="t1")).text
        assert all(x in chk for x in ("Difficulty check", "Add it", "readable today", 'name="tags" value="t1"', "sentences readable in full")), [x for x in ("Difficulty check", "Add it", "readable today", "t1", "2 sentences", "Internal") if x in chk]
        assert "Bring in your progress" in c.get("/imports").text and 'class="on" href="/inbox"' in c.get("/inbox").text
        r = c.post("/reader/study", data=dict(daily_budget="12", more_new="never", contrast_when="all"), follow_redirects=False)
        with db.session() as con:
            assert r.status_code == 303 and (db.setting(con, "daily_budget"), db.setting(con, "more_new"), db.setting(con, "contrast_when")) == ("12", "never", "all")
            db.set_setting(con, "daily_budget", 15)
            db.set_setting(con, "more_new", "once")
        r = c.post(f"/source/{first}/priority", data=dict(priority="1"), follow_redirects=False)
        assert r.status_code == 303
        with db.session() as con:
            assert con.execute("SELECT priority FROM sources WHERE id=?", (first,)).fetchone()[0] == 1
            con.execute("UPDATE sources SET priority=2")
        assert c.post("/contrast/when", data=dict(when="never"), follow_redirects=False).status_code == 303
        with db.session() as con:
            assert db.setting(con, "contrast_when") == "never"
            db.set_setting(con, "contrast_when", "mixup")
        r = c.post("/settings/pairs", files=dict(upload=("lab.tsv", "較正\tを\t行う\n".encode("utf-8"), "text/plain")), follow_redirects=False)
        assert r.status_code == 303 and "Loaded 1 collocation" in urllib.parse.unquote(r.headers["location"])
        # a file chosen in the browser is added like any other source
        r = c.post("/add", data=dict(title="Uploaded note", action="add"), follow_redirects=False,
                   files=dict(upload=("note.txt", "検出器の較正を行った。".encode("utf-8"), "text/plain")))
        assert r.status_code == 303
        for _ in range(100):
            with db.session() as con:
                up = con.execute("SELECT id, status, message FROM sources WHERE title='Uploaded note'").fetchone()
            if up and up["status"] in ("done", "error"):
                break
            time.sleep(0.1)
        assert up["status"] == "done", up["message"]
        r = c.post("/settings/frequency", data=dict(action="file"), follow_redirects=False,
                   files=dict(upload=("mine.csv", "検出器,3\n較正,9\n".encode("utf-8"), "text/csv")))
        assert r.status_code == 303 and "Loaded 2" in urllib.parse.unquote(r.headers["location"])
        # ignore a word, find it under "Set aside", put it back; mark a sentence as bad and restore it
        with db.session() as con:
            c0 = con.execute("SELECT c.item_id, c.sentence_id, i.lemma FROM cards c JOIN items i ON i.id=c.item_id WHERE c.kind='word' LIMIT 1").fetchone()
            n0 = con.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
        fetch = {"x-requested-with": "fetch"}
        assert c.post(f"/item/{c0['item_id']}/status", data=dict(status="ignored"), headers=fetch).json() == dict(ok=True)
        page = c.get("/aside")
        assert page.status_code == 200 and c0["lemma"] in page.text and "Learn this" in page.text
        assert c.post(f"/item/{c0['item_id']}/status", data=dict(status="undo"), headers=fetch).status_code == 200
        with db.session() as con:
            assert tuple(con.execute("SELECT status, known_by FROM items WHERE id=?", (c0["item_id"],)).fetchone()) == ("learning", None)
        res = c.post(f"/sentence/{c0['sentence_id']}/reject", headers=fetch).json()
        assert res["moved"] + res["dropped"] >= 1 and "Use again" in c.get("/cards?state=suspended&mark=sentences").text
        with db.session() as con:
            rj = con.execute("SELECT source_id, text FROM rejected").fetchone()
        assert c.post("/sentence/restore", data=dict(source_id=rj["source_id"], text=rj["text"]), follow_redirects=False).status_code == 303
        with db.session() as con:
            assert con.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == n0
            assert con.execute("SELECT COUNT(*) FROM rejected").fetchone()[0] == 0
            ingest.delete_source(con, up["id"])
        c.post("/reader/study", data=dict(daily_budget="20"), follow_redirects=False)
        r = c.post("/settings", data=dict(level="2", gloss_dict="jmdict-en"), follow_redirects=False)
        assert r.status_code == 303
        with db.session() as con:
            assert db.setting(con, "daily_budget") == "20" and db.setting(con, "level") == "2"


def test_collocation_cards_hide_the_verb(eng):
    """A noun with the verb that goes with it: from dictionary expressions and from a pair list."""
    assert cards._hide_verb("この結果に基づいて仮説を立てた。", "仮説", "を", "立てる", "仮説を立て") == ("この結果に基づいて仮説を", "立て", "た。")
    assert cards._hide_verb("磁場が影響を与えることが分かった。", "影響", "を", "与える", None) == ("磁場が影響を", "与える", "ことが分かった。")
    assert dictdata.add_pairs(S / "pairs.tsv") == 2                             # the line with の is not a pair
    from sumitsubo.parser import Parser
    dic = dictdata.Dictionary()
    assert ("補正", "を", "行う") in dic.pairs() and ("pairs", 2) in dic.pair_lists()
    with db.session() as con:
        first = con.execute("SELECT id FROM sources ORDER BY id LIMIT 1").fetchone()[0]
        n_words = analyze.coverage(con, first)["total_words"]
    ingest.process(first, dic, Parser(dic))                                      # the talk says 補正を行わない場合
    with db.session() as con:
        it = con.execute("SELECT * FROM items WHERE kind='colloc' AND lemma='補正を行う'").fetchone()
        assert it and it["reading"] == "ほせいをおこなう" and it["gloss"] == "to apply a correction" and it["band"] == "new"
        card = con.execute("SELECT * FROM cards WHERE item_id=?", (it["id"],)).fetchone()
        v = cards.view(con, card)
        assert card["kind"] == "colloc" and v["label"] == "Collocation" and v["parts"][0].endswith("補正を") and v["parts"][1].startswith("行")
        front, back = cards.faces(v)
        assert "補正を＿＿" in front and "行" not in front.split("＿＿")[1][:1] and "補正を行う" in back and v["sentence"] in back
        assert analyze.coverage(con, first)["total_words"] == n_words          # the pair is not counted as a third word
        assert con.execute("SELECT COUNT(*) FROM cards WHERE item_id IN (SELECT id FROM items WHERE lemma IN ('補正','行う')) AND kind='word'").fetchone()[0] >= 1   # its words keep their own cards
        # dictionary expressions of the same shape are collocations too, with one card each, not a word card as well
        for r in con.execute("SELECT co.*, i.lemma, i.kind FROM colloc co JOIN items i ON i.id=co.item_id WHERE i.kind='word'").fetchall():
            assert r["lemma"] == r["noun"] + r["particle"] + r["verb"]
            assert [x[0] for x in con.execute("SELECT kind FROM cards WHERE item_id=?", (r["item_id"],))] in ([], ["colloc"])
        out = Path(_tmp) / "colloc.apkg"
        assert cards.export_anki(con, out, [first], kinds=["colloc"])["notes"] >= 1
        line = next(json.loads(x) for x in (cards.write_pack(con, first) / "cards.jsonl").read_text(encoding="utf-8").splitlines() if json.loads(x)["kind"] == "colloc")
        assert "＿＿" in line["front"] and all("＿＿" in x["front"] for x in line["sentences"])


def test_contrast_cards(eng):
    with db.session() as con:
        analyze.rebuild_cards(con)
        n = contrast.counts(con)
        assert n.get("read", 0) >= 1 and n.get("look", 0) >= 1                  # the samples have 出 (しゅつ, だ) and 検 / 験
        before = {c["guid"]: [m["id"] for m in c["members"]] for c in contrast.listing(con)}
        analyze.rebuild_cards(con)
        assert {c["guid"]: [m["id"] for m in c["members"]] for c in contrast.listing(con)} == before      # a card keeps its identity
        for c in contrast.listing(con):
            assert 2 <= len(c["members"]) <= (contrast.LOOK if c["kind"] == "look" else contrast.SIZE) and c["front"] and c["back"]
            lits = {m["lemma"] for m in c["members"]}
            if c["kind"] == "read":                                             # one kanji, different readings
                assert len(lits) == 1 and len({m["reading"] for m in c["members"]}) == len(c["members"])
            elif c["kind"] == "sound":                                          # different words, one sound
                assert len({m["reading"] for m in c["members"]}) == 1 and len(lits) == len(c["members"])
            elif c["kind"] == "keigo":                                          # a plain verb with its respectful and humble forms
                assert [m["role"] for m in c["members"]] == [r for r in ("plain", "respectful", "humble") if r in {m["role"] for m in c["members"]}]
                assert c["met"] >= 1 and c["members"][0]["role"] == "plain" and all(m["role"] in c["back"] for m in c["members"])
            else:                                                               # each is listed as looking like the first
                first = c["members"][0]["lemma"]
                assert all(m["lemma"] in dictdata.similar_kanji()[first] for m in c["members"][1:]) and all(m["word"] for m in c["members"])
        assert "末" in dictdata.similar_kanji()["未"] and "未" in dictdata.similar_kanji()["末"] and "持" in dictdata.similar_kanji()["待"]
        one = next(c for c in contrast.listing(con, "read"))
        analyze.set_status(con, one["members"][0]["id"], "ignored")              # a suspended member takes the card with it
        analyze.rebuild_cards(con)
        assert one["guid"] not in {c["guid"] for c in contrast.listing(con)}
        analyze.set_status(con, one["members"][0]["id"], "undo")
        analyze.rebuild_cards(con)
        assert contrast.counts(con) == n
        db.set_setting(con, "contrast_when", "all")
        root = cards.write_shared(con, Path(_tmp) / "packs")
        shared = json.loads((root / "settings.json").read_text(encoding="utf-8"))
        lines = [json.loads(x) for x in (root / "contrast.jsonl").read_text(encoding="utf-8").splitlines()]
        assert shared["contrast_when"] == "all" and shared["daily_budget"] > 0 and len(lines) == sum(n.values())
        assert all(x["id"] and len(x["items"]) >= 2 and x["front"] and x["back"] for x in lines)
        db.set_setting(con, "contrast_when", "mixup")


def test_sentences_priority_and_dates(eng):
    import datetime as dt
    with db.session() as con:
        ids = [r[0] for r in con.execute("SELECT id FROM sources WHERE status='done' ORDER BY id")]
        a, b = ids[0], ids[1]
        cov = analyze.coverage(con, a)
        assert cov["sentences"]["total"] == con.execute("SELECT COUNT(*) FROM sentences WHERE source_id=?", (a,)).fetchone()[0]
        assert 0 <= cov["sentences"]["readable"] < cov["sentences"]["total"] and cov["sentences"]["one_away"] >= 1
        analyze.apply_level(con, 1)                                               # with N1 assumed, more whole sentences are readable
        analyze.rebuild_cards(con)
        assert analyze.coverage(con, a)["sentences"]["readable"] > cov["sentences"]["readable"]
        analyze.apply_level(con, 3)
        analyze.rebuild_cards(con)
        sc = analyze.score_text(con, *eng, ["検出器の較正について発表を始めます。", "これは本です。"])
        assert sc["sentences"]["total"] == 2 and sc["sentences"]["readable"] <= 2

        today = dt.date(2026, 12, 1)
        db.set_setting(con, "daily_budget", 10)
        con.execute("UPDATE sources SET priority=2")
        p = analyze.projection(con, today)
        q = analyze.queue(con)
        assert p["budget"] == 10 and p[a]["new_words"] > 0 and p[b]["new_words"] > 0
        assert [c["source_id"] for c in q] == sorted(c["source_id"] for c in q)   # equal priority: in the order sources were added
        last_a = max(n for n, c in enumerate(q) if c["source_id"] == a and c["kind"] in ("word", "colloc"))
        assert p[a]["days"] == last_a // 10 + 1 and p[a]["introduced"] == (today + dt.timedelta(days=p[a]["days"])).isoformat()
        assert p[a]["settled"] == (today + dt.timedelta(days=p[a]["days"] + analyze.SETTLE_DAYS)).isoformat()
        assert p[b]["days"] >= p[a]["days"]                                       # the later source waits its turn
        con.execute("UPDATE sources SET priority=1 WHERE id=?", (b,))            # marked First: its new cards jump the queue
        p2 = analyze.projection(con, today)
        assert p2[b]["days"] < p[b]["days"] and p2[a]["days"] >= p[a]["days"] and analyze.queue(con)[0]["source_id"] == b
        db.set_setting(con, "daily_budget", 20)                                   # a bigger budget brings every date forward
        assert analyze.projection(con, today)[a]["days"] < p2[a]["days"]
        con.execute("UPDATE sources SET priority=2")
        db.set_setting(con, "daily_budget", 15)


def test_register_and_keigo(eng):
    dic, parser = eng
    judge = register.of_sentence
    assert [judge(x) for x in ("この手法は有効であると考えられる。", "結果は予想と一致した。", "企業を誘致し、依存を減らす。", "少なさが目立つ。")] == ["plain"] * 4
    assert [judge(x) for x in ("これから発表を始めます。", "結果は一致しませんでした。", "どう思いますか")] == ["polite", "polite", "polite"]
    assert [judge(x) for x in ("田中と申します。", "先生がおっしゃった通りです。", "資料を拝見しました。", "私は東京におります。", "こちらの図をご覧ください。",
                               "お待ちになりますか。", "ご確認いただけますと幸いです。")] == ["keigo"] * 7
    assert register.of_tags(["uk", "hum"]) == "humble" and register.of_tags(["honorific or respectful (sonkeigo) language"]) == "respectful" and register.of_tags(["uk"]) is None
    assert dic.has_registers() and dic.lookup("申す", "もうす")["reg"] == "humble" and dic.lookup("検出器", "けんしゅつき")["reg"] is None
    sid = run(eng, S / "keigo_talk.txt", title="Keigo talk", tags="seminar")
    with db.session() as con:
        reg = analyze.coverage(con, sid)["register"]
        assert reg["keigo"] >= 60 and reg["plain"] >= 10 and sum(reg.values()) in (99, 100, 101)       # ten keigo lines, two plain
        other = analyze.coverage(con, con.execute("SELECT id FROM sources WHERE title='math_paper'").fetchone()[0])["register"]
        assert other["plain"] == 100                                              # a paper is in plain written style
        w = con.execute("SELECT * FROM items WHERE kind='word' AND lemma='申す'").fetchone()
        assert w["register"] == "humble"
        v = cards.view(con, con.execute("SELECT * FROM cards WHERE item_id=? AND kind='word'", (w["id"],)).fetchone())
        assert v["register"] == "humble" and v["sentences"][0]["register"] == "keigo"
        # keigo contrast cards: a plain verb with its respectful and humble counterparts, once one of them is met
        ks = {c["label"]: c for c in contrast.listing(con, "keigo")}
        say = ks["言う"]
        assert [(m["lemma"], m["role"]) for m in say["members"]] == [("言う", "plain"), ("おっしゃる", "respectful"), ("申す", "humble")]
        assert say["met"] >= 2 and "humble" in say["back"] and "respectful" in say["back"]
        assert {"行く", "見る", "いる"} <= set(ks)                                 # 参る, 拝見, おる are in the talk
        assert [m["lemma"] for m in ks["行く"]["members"]] == ["行く", "いらっしゃる", "参る"]
        unmet = [m for c in ks.values() for m in c["members"] if not m["met"]]
        assert unmet and all(con.execute("SELECT COUNT(*) FROM cards WHERE item_id=?", (m["id"],)).fetchone()[0] == 0 for m in unmet)   # known of, but no card until met
        line = json.loads((cards.write_shared(con, Path(_tmp) / "packs2") / "contrast.jsonl").read_text(encoding="utf-8").splitlines()[-1])
        assert {"met", "of", "items", "front", "back"} <= set(line)
        # a word's sentences are chosen for variety of register where there is a choice
        pool = analyze._pool([dict(id=1, source=1, text="結果を報告する。", audio=False, unknown=1, bare=False, register="plain"),
                              dict(id=2, source=1, text="結果を報告した。", audio=False, unknown=1, bare=False, register="plain"),
                              dict(id=3, source=1, text="結果を報告いたします。", audio=False, unknown=1, bare=False, register="keigo")], 1)
        assert set(pool[:2]) in ({1, 3}, {2, 3}) and len(pool) == 3                # the first two differ in register
        ingest.delete_source(con, sid)
        assert not contrast.listing(con, "keigo") or all(c["met"] >= 1 for c in contrast.listing(con, "keigo"))


def test_collocation_front_gives_a_hint_until_mature(eng):
    with db.session() as con:
        card = con.execute("SELECT c.* FROM cards c JOIN items i ON i.id=c.item_id WHERE c.kind='colloc' AND i.lemma='補正を行う'").fetchone()
        v = cards.view(con, card)
        assert v["hint"] == "to apply a correction"
        first = v["source_id"]
        line = next(json.loads(x) for x in (cards.write_pack(con, first) / "cards.jsonl").read_text(encoding="utf-8").splitlines() if json.loads(x)["id"] == v["guid"])
        assert line["hint"] == "to apply a correction" and line["hint_while"] == ["new", "learning", "young"] and "apply" not in line["front"]
        # with a particle other than を, the particle is hidden along with the verb
        a, b, c = cards._hide_verb("ようやく結論に達した。", "結論", "に", "達する", "結論に達し")
        assert (a, b, c) == ("ようやく結論に", "達し", "た。")
        con.execute("INSERT INTO items(kind, key, lemma, reading, gloss, status, created) VALUES('colloc','結論|に|達する','結論に達する','けつろんにたっする','to reach a conclusion','learning',0)")
        iid = con.execute("SELECT id FROM items WHERE key='結論|に|達する'").fetchone()[0]
        con.execute("INSERT INTO colloc VALUES(?,?,?,?)", (iid, "結論", "に", "達する"))
        sen = con.execute("INSERT INTO sentences(source_id, idx, text, register) VALUES(?,?,?,?)", (first, 999, "ようやく結論に達した。", "plain")).lastrowid
        con.execute("INSERT INTO occ(item_id, sentence_id, source_id, n, surface) VALUES(?,?,?,1,?)", (iid, sen, first, "結論に達し"))
        analyze.rebuild_cards(con)
        v2 = cards.view(con, con.execute("SELECT * FROM cards WHERE item_id=?", (iid,)).fetchone())
        assert v2["parts"] == ("ようやく結論", "に達し", "た。") and cards.faces(v2)[0] == "ようやく結論＿＿た。"
        con.execute("DELETE FROM occ WHERE item_id=?", (iid,))
        con.execute("DELETE FROM sentences WHERE id=?", (sen,))
        con.execute("DELETE FROM items WHERE id=?", (iid,))
        analyze.rebuild_cards(con)


# ---- imports: progress from Anki, Renshuu or a list (kept last: they change what counts as known) ----
from sumitsubo import imports  # noqa: E402

FX = HERE / "fixture"


def _serve(routes):
    """A tiny local stand-in for a web service: routes maps a path (or '*') to a function of the request."""
    import http.server
    import threading

    class H(http.server.BaseHTTPRequestHandler):
        def _go(self):
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            path = urllib.parse.urlparse(self.path)
            fn = routes.get(path.path) or routes.get("*")
            status, out = fn(dict(urllib.parse.parse_qsl(path.query)), body, self.headers) if fn else (404, {})
            data = json.dumps(out).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        do_GET = do_POST = _go

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_import_readers():
    assert imports.split_furigana(" 分解能[ぶんかいのう]") == ("分解能", "ぶんかいのう")
    assert imports.split_furigana("食[た]べる") == ("食べる", "たべる") and imports.split_furigana("お 願[ねが]い") == ("お願い", "おねがい")
    assert imports.clean('<div>まず、<b>検出器</b>の</div>[sound:a.mp3]&nbsp;構成') == "まず、検出器の 構成"
    new, old = imports.read_anki(FX / "anki-current.apkg"), imports.read_anki(FX / "anki-older.apkg")   # made by Anki's own library
    key = lambda b: sorted((n["deck"], n["nt"], tuple(n["f"]), n["type"], n["ivl"], len(n["log"]), str(n["fsrs"])) for n in b["notes"])  # noqa: E731
    assert key(new) == key(old) and len(new["notes"]) == 12
    assert {n["deck"] for n in new["notes"]} == {"Japanese::Core", "Physics terms"}
    assert new["notetypes"] == {"Core Vocab": ["Expression", "Reading", "Meaning", "Sentence"], "Furigana card": ["Front", "Back"]}
    m = imports.guess_mapping(new["notetypes"]["Core Vocab"], [])
    assert m == dict(word=0, reading=1, sentence=3)
    m2 = imports.guess_mapping(["Front", "Back"], [n["f"] for n in new["notes"] if n["nt"] == "Furigana card"])
    imports._with_seeds(new)
    seed = {(imports.entry(n, m if n["nt"] == "Core Vocab" else m2) or ("",))[0]: n["seed"] for n in new["notes"]}
    days = lambda t: (new["read"] - t) / 86400  # noqa: E731
    # 検出器 has a review history in the file: it is replayed, and the state is where that history leaves it
    assert len(next(n for n in new["notes"] if n["f"][0] == "検出器")["log"]) == 6
    assert seed["検出器"]["state"] == "review" and seed["検出器"]["stability"] > 200 and 99 < days(seed["検出器"]["last_review"]) < 101
    assert seed["検出器"]["due"] > new["read"]                                  # and it keeps a due date well ahead
    # 誤差 was forgotten once along the way: a young card, and a harder one
    assert seed["誤差"]["stability"] < 21 < seed["検出器"]["stability"] and seed["誤差"]["difficulty"] > seed["検出器"]["difficulty"]
    # 較正 has no history but carries the state Anki's own FSRS stored
    assert (seed["較正"]["stability"], seed["較正"]["difficulty"]) == (48.5, 6.2)
    # the rest are converted from interval and ease; a card never studied brings nothing
    assert seed["沢山"]["stability"] == 400 and seed["勾配"]["state"] == "learning" and seed["測定"] is None
    states = {(imports.entry(n, m if n["nt"] == "Core Vocab" else m2) or ("",))[0]: imports.state(n) for n in new["notes"]}
    assert (states["検出器"], states["誤差"], states["勾配"], states["測定"], states["分解能"]) == ("mature", "young", "young", "new", "mature")
    assert "" in states                                                        # a sentence card holds no single word
    none = imports.read_anki(FX / "anki-noschedule.apkg")
    imports._with_seeds(none)
    assert {imports.state(n) for n in none["notes"]} == {"new"}
    lst = Path(_tmp) / "known.csv"
    lst.write_text("word,reading,mastery\n検出器,けんしゅつき,80%\n較正,こうせい,20\n測定,そくてい,0\n", encoding="utf-8")
    b = imports.read_list(lst)
    mm = imports.guess_mapping(b["notetypes"]["List"], [n["f"] for n in b["notes"]])
    imports._with_seeds(b)
    assert [(imports.entry(n, mm)[0], imports.state(n)) for n in b["notes"]] == [("検出器", "mature"), ("較正", "young"), ("測定", "new")]
    plain = Path(_tmp) / "plain.txt"
    plain.write_text("検出器\n較正\n", encoding="utf-8")
    b = imports.read_list(plain)
    imports._with_seeds(b)
    assert [imports.state(n) for n in b["notes"]] == ["mature", "mature"]      # a bare list of words you know


def test_import_from_renshuu_and_running_anki():
    terms = [dict(id=1, kanji_full="検出器", hiragana_full="けんしゅつき", **{"def": ["detector"]}, user_data=dict(mastery_avg_perc="72", correct_count=9)),
             dict(id=2, kanji_full="較正", hiragana_full="こうせい", **{"def": "calibration"}, user_data=dict(mastery_avg_perc="30", correct_count=2)),
             dict(id=3, kanji_full="", hiragana_full="たくさん", **{"def": ["a lot"]}, user_data=dict(mastery_avg_perc=0, correct_count=0))]
    seen = []

    def schedules(q, body, headers):
        seen.append(headers.get("Authorization"))
        return 200, dict(schedules=[dict(id=7, name="JLPT N3 Vocab", booktype="vocab"), dict(id=8, name="Kanji N3", booktype="kanji")])

    def page(q, body, headers):
        pg = int(q.get("pg", 1))
        return 200, dict(contents=dict(total_pg=2, terms=terms[:2] if pg == 1 else terms[2:]))
    kanji = [dict(id="k1", kanji="検", onyomi="ケン", kunyomi="しら.べる", definition="examine", user_data=dict(mastery_avg_perc=90, correct_count=30)),
             dict(id="k2", kanji="器", onyomi="キ", kunyomi="うつわ", definition="vessel", user_data=dict(mastery_avg_perc=0))]
    asked = []

    def reibun(q, body, headers):                 # a word's example sentences are a separate request
        asked.append(1)
        return 200, dict(reibuns=[dict(id="9", japanese="較正（こうせい）の結果を報告する。", hiragana="こうせいのけっかをほうこくする。", meaning=dict(en="..."))])
    routes = {"/schedule": schedules, "/schedule/7/list": page, "/schedule/8/list": lambda q, b_, h: (200, dict(contents=dict(total_pg=1, terms=kanji))),
              "/schedule/9/list": lambda q, b_, h: (500, {}), "/reibun/search/2": reibun, "/reibun/search/1": reibun}
    schedules_full = lambda q, body, headers: (seen.append(headers.get("Authorization")), (200, dict(schedules=[  # noqa: E731
        dict(id=7, name="JLPT N3 Vocab", booktype="vocab"), dict(id=8, name="Kanji N3", booktype="kanji"), dict(id=9, name="Grammar N3", booktype="grammar")])))[1]
    srv, url = _serve(routes | {"/schedule": schedules_full})
    b = imports.read_renshuu("secret-key", base=url, delay=0, sentences=1)
    srv.shutdown()
    assert seen == ["Bearer secret-key"] and b["skipped"] == ["Grammar N3 (grammar)"]         # grammar is not read, and not requested
    ks = [n for n in b["notes"] if n.get("kanji")]
    assert [(n["kanji"], n["readings"], n["deck"]) for n in ks] == [("検", ["けん", "しら"], "Kanji N3"), ("器", ["き", "うつわ"], "Kanji N3")]
    assert imports.kanji_readings("ケン、ゲン", "しら.べる, -あ.げる") == ["けん", "げん", "しら", "あ"]
    # one sentence was asked for: it goes to the least-known studied word, and its bracketed readings are dropped
    assert len(asked) == 1 and [n["f"][3] for n in b["notes"] if not n.get("kanji")] == ["", "較正の結果を報告する。", ""]
    b["notes"] = [n for n in b["notes"] if not n.get("kanji")]
    m = imports.guess_mapping(b["notetypes"]["Renshuu vocabulary"], [])
    imports._with_seeds(b)
    assert [(imports.entry(n, m)[:2], imports.state(n)) for n in b["notes"]] == [
        (("検出器", "けんしゅつき"), "mature"), (("較正", "こうせい"), "young"), (("たくさん", "たくさん"), "new")]
    assert 40 < b["notes"][0]["seed"]["stability"] < 45                        # 72% mastery: an estimate, about six weeks
    saved = Path(_tmp) / "renshuu_export.json"                     # the same data as a saved file
    saved.write_text(json.dumps(dict(schedules=[dict(name="JLPT N3 Vocab", booktype="vocab", terms_list=terms),
                                                dict(name="Kanji N3", booktype="kanji", terms_list=kanji)])), encoding="utf-8")
    assert [n["f"][0] for n in imports.read_file(saved)["notes"]] == ["検出器", "較正", "たくさん", "検", "器"]

    def anki(q, body, headers):
        req = json.loads(body)
        if req["action"] == "findCards":
            return 200, dict(result=[11, 12, 13], error=None)
        if req["action"] == "getReviewsOfCards":
            t0 = (time.time() - 90 * 86400) * 1000
            return 200, dict(result={"11": [dict(id=t0 + d * 86400000, ease=3, type=1) for d in (0, 1, 5, 20, 50)], "13": []}, error=None)
        info = lambda cid, note, word, typ, ivl: dict(cardId=cid, note=note, deckName="Mining", modelName="Mined", type=typ, interval=ivl, reps=3, lapses=0, factor=2500, mod=int(time.time()) - 86400,  # noqa: E731
                                                      fields=dict(Reading=dict(value="", order=1), Word=dict(value=word, order=0)))
        return 200, dict(result=[info(11, 1, "検出器", 2, 40), info(12, 1, "検出器", 1, 0), info(13, 2, "較正", 1, 0)], error=None)
    srv, url = _serve({"*": anki})
    b = imports.read_ankiconnect(url)
    srv.shutdown()
    assert b["notetypes"] == {"Mined": ["Word", "Reading"]} and len(b["notes"]) == 2      # one per note, its best card
    imports._with_seeds(b)
    assert sorted((n["f"][0], imports.state(n), len(n["log"])) for n in b["notes"]) == [("検出器", "mature", 5), ("較正", "young", 0)]
    with pytest.raises(RuntimeError, match="Could not reach Anki"):
        imports.read_ankiconnect("http://127.0.0.1:9")


def test_import_choices_per_deck_and_removal(eng):
    dic, _ = eng
    item = lambda con, lemma, kind="word": con.execute("SELECT * FROM items WHERE kind=? AND lemma=? ORDER BY id LIMIT 1", (kind, lemma)).fetchone()  # noqa: E731
    mem = lambda con, lemma, kind="word": con.execute("SELECT * FROM memory WHERE item_id=? AND skill=?", (item(con, lemma, kind)["id"], kind)).fetchone()  # noqa: E731
    snap = lambda con: (con.execute("SELECT COUNT(*) FROM cards").fetchone()[0],  # noqa: E731
                        sorted(map(tuple, con.execute("SELECT id, band FROM items"))),
                        sorted(map(tuple, con.execute("SELECT item_id, skill, state, stability, origin FROM memory"))))
    with db.session() as con:
        con.execute("DELETE FROM marks")                                          # marks left by earlier tests
        analyze.rebuild_cards(con)
        before, n_items, n_sources = snap(con), con.execute("SELECT COUNT(*) FROM items").fetchone()[0], con.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
        assert item(con, "検出器")["band"] == "new" and mem(con, "説明")["origin"] == "assumed"
        analyze.set_status(con, item(con, "分解能")["id"], "learning")            # your own mark must outrank the import
        bid = imports.save_batch(con, imports.read_anki(FX / "anki-current.apkg"))
        p = imports.preview(con, bid)
        assert p["decks"]["Japanese::Core"] == dict(mature=4, young=2, new=1, unusable=1, total=8, history=2, kanji=0, with_sentence=0)
        assert p["decks"]["Physics terms"] == dict(mature=2, young=1, new=1, unusable=0, total=4, history=0, kanji=0, with_sentence=0)
        assert all(i["mode"] == "progress" for i in p["imports"].values())       # the default takes progress only

        t = imports.apply(con, dic, bid)                                          # progress only
        assert t["added"] == 0 and t["studied"] == 6 and t["reviews"] == 13 and t["unusable"] == 1 and t["not_found"] == 3   # the small test dictionary lacks 沢山, 食べる, 鞍点
        assert con.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == n_sources          # nothing joined the library
        m = mem(con, "検出器")                                                    # in the talk, so it has a card: now a mature one
        assert (m["state"], m["origin"]) == ("review", "import") and m["stability"] > 200 and item(con, "検出器")["band"] == "mature"
        card = con.execute("SELECT * FROM cards WHERE item_id=? AND kind='word'", (item(con, "検出器")["id"],)).fetchone()
        v = cards.view(con, card)
        assert v["state"] == "Mature" and v["memory"]["due_days"] > 100 and 90 < v["memory"]["recall"] <= 100
        assert item(con, "誤差")["band"] == "young" and mem(con, "誤差")["stability"] < 21     # still being learned: carried over as such
        assert con.execute("SELECT COUNT(*) FROM reviews WHERE item_id=?", (item(con, "検出器")["id"],)).fetchone()[0] == 6   # its history is kept
        k = con.execute("SELECT m.origin, m.stability FROM memory m JOIN items i ON i.id=m.item_id WHERE i.kind='kanji' AND i.key='検|けん'").fetchone()
        assert k and k["origin"] == "import" and k["stability"] == m["stability"]             # a reading inherits from its best-known word
        assert item(con, "分解能")["band"] == "new" and mem(con, "分解能") is None             # "Learn this" stands

        imports.configure(con, bid, modes={"Japanese::Core": "studied"})         # also bring the studied cards of one deck
        t = imports.apply(con, dic, bid)
        imp = con.execute("SELECT * FROM imports WHERE batch_id=? AND deck='Japanese::Core'", (bid,)).fetchone()
        src = con.execute("SELECT * FROM sources WHERE id=?", (imp["source_id"],)).fetchone()
        assert t["added"] == 4 and src["kind"] == "deck" and src["n_sentences"] == 4 and src["status"] == "done"
        texts = {r[0] for r in con.execute("SELECT text FROM sentences WHERE source_id=?", (src["id"],))}
        assert {"誤差を評価する。", "勾配", "まず、検出器の構成を説明します。"} <= texts          # its own sentence, else the word alone
        cov = analyze.coverage(con, src["id"])
        assert cov["pct"]["new"] == 0 and cov["pct"]["mature"] > 0 and cov["pct"]["young"] > 0 and 50 < cov["readable"] < 100
        assert con.execute("SELECT source_id FROM imports WHERE batch_id=? AND deck='Physics terms'", (bid,)).fetchone()[0] is None

        imports.configure(con, bid, modes={"Japanese::Core": "all"})             # and the card never started
        t = imports.apply(con, dic, bid)
        assert t["added"] == 5 and "測定を行う。" in {r[0] for r in con.execute("SELECT text FROM sentences WHERE source_id=?", (src["id"],))}
        lines = [json.loads(x) for x in (cards.write_pack(con, src["id"]) / "cards.jsonl").read_text(encoding="utf-8").splitlines()]
        got = {x["front"]: x["memory"] for x in lines if x["kind"] == "word"}
        assert got["誤差を評価する。"]["origin"] == "import" and got["誤差を評価する。"]["stability"] < 21 and len(lines) >= 1   # words already met in your own sources keep their card there

        imports.configure(con, bid, modes={"Japanese::Core": "progress", "Physics terms": "skip"})
        imports.apply(con, dic, bid)
        assert con.execute("SELECT COUNT(*) FROM sources WHERE kind='deck'").fetchone()[0] == 0   # no cards brought, so the deck left the library

        again = imports.save_batch(con, imports.read_anki(FX / "anki-older.apkg"))
        assert again != bid                                                       # another file is another batch
        imports.remove(con, again)
        assert imports.save_batch(con, imports.read_anki(FX / "anki-current.apkg")) == bid       # the same file updates, keeping choices
        imports.remove(con, bid)
        analyze.set_status(con, item(con, "分解能")["id"], "undo")
        analyze.rebuild_cards(con)
        after = snap(con)
        keep = {i for i, _ in before[1]}                                          # items the import created stay, harmlessly
        assert after[0] == before[0] and [x for x in after[1] if x[0] in keep] == before[1] and [x for x in after[2] if x[0] in keep] == before[2]
        assert con.execute("SELECT COUNT(*) FROM import_items").fetchone()[0] == 0 and con.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 0


def test_data_from_before_0_2_0_is_converted(eng):
    """A database as version 0.1.x left it: a status column, no memory states."""
    dic, _ = eng
    with db.session() as con:
        before = {r["id"]: r["band"] for r in con.execute("SELECT id, band FROM items")}
        n_cards = con.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
        level = int(db.setting(con, "level"))
        ids = lambda lemma: con.execute("SELECT id FROM items WHERE kind='word' AND lemma=?", (lemma,)).fetchone()[0]  # noqa: E731
        con.execute("DELETE FROM memory")
        con.execute("DELETE FROM marks")
        con.execute("UPDATE items SET status='known', known_by='level' WHERE status='learning' AND level IS NOT NULL AND level>=?", (level,))
        con.execute("UPDATE items SET status='known', known_by='user' WHERE id=?", (ids("検出器"),))
        con.execute("UPDATE items SET status='ignored', known_by='user' WHERE id=?", (ids("較正"),))
        con.execute("DELETE FROM settings WHERE key IN ('model', 'level_set_at')")
        assert memory.migrate(con) and not memory.migrate(con)                    # once only
        analyze.rebuild_cards(con)
        assert {r["item_id"]: r["mark"] for r in con.execute("SELECT * FROM marks")} == {ids("検出器"): "known", ids("較正"): "ignored"}
        assert con.execute("SELECT band FROM items WHERE id=?", (ids("較正"),)).fetchone()[0] == "suspended"
        assert con.execute("SELECT origin, stability FROM memory WHERE item_id=?", (ids("検出器"),)).fetchone()[:] == ("user", 365)
        assert not con.execute("SELECT 1 FROM items WHERE status='known'").fetchone()
        for lemma in ("検出器", "較正"):
            analyze.set_status(con, ids(lemma), "undo")
        analyze.rebuild_cards(con)
        assert {r["id"]: r["band"] for r in con.execute("SELECT id, band FROM items")} == before
        assert con.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == n_cards


def test_kanji_studied_as_characters_elsewhere(eng):
    """A Renshuu kanji schedule: the kanji's state goes to the readings that study covered."""
    dic, _ = eng
    f = Path(_tmp) / "renshuu_kanji.json"
    f.write_text(json.dumps(dict(schedules=[dict(name="My kanji", booktype="kanji", terms_list=[
        dict(kanji="跡", onyomi="セキ", kunyomi="あと", definition="tracks", user_data=dict(mastery_avg_perc=95, correct_count=40)),
        dict(kanji="飛", onyomi="ヒ", kunyomi="と.ぶ", definition="fly", user_data=dict(mastery_avg_perc=20, correct_count=3)),
        dict(kanji="補", onyomi="ホ", kunyomi="おぎな.う", definition="supplement", user_data=dict(mastery_avg_perc=0))])])), encoding="utf-8")
    state = lambda con, key: (con.execute("SELECT band FROM items WHERE kind='kanji' AND key=?", (key,)).fetchone() or [None])[0]  # noqa: E731
    with db.session() as con:
        before = {k: state(con, k) for k in ("跡|せき", "飛|ひ", "補|ほ")}
        assert before["跡|せき"] == "new" and before["補|ほ"] is not None
        bid = imports.save_batch(con, imports.read_file(f))
        p = imports.preview(con, bid)
        assert p["decks"]["My kanji"]["kanji"] == 3 and (p["decks"]["My kanji"]["mature"], p["decks"]["My kanji"]["young"], p["decks"]["My kanji"]["new"]) == (1, 1, 1)
        imports.configure(con, bid, modes={"My kanji": "all"})                    # kanji bring progress only, whatever is chosen
        t = imports.apply(con, dic, bid)
        assert t["studied"] == 2 and t["added"] == 0 and con.execute("SELECT COUNT(*) FROM sources WHERE kind='deck'").fetchone()[0] == 0
        assert state(con, "跡|せき") == "mature" and state(con, "飛|ひ") == "young" and state(con, "補|ほ") == before["補|ほ"]
        m = con.execute("SELECT m.* FROM memory m JOIN items i ON i.id=m.item_id WHERE i.key='跡|せき'").fetchone()
        assert m["origin"] == "import" and 130 < m["stability"] < 145      # 95% mastery: an estimate
        card = con.execute("SELECT c.* FROM cards c JOIN items i ON i.id=c.item_id WHERE c.kind='kanji' AND i.key='跡|せき'").fetchone()
        assert cards.view(con, card)["state"] == "Mature"
        imports.remove(con, bid)
        assert {k: state(con, k) for k in before} == before


def test_a_card_gathers_sentences_from_every_source(eng):
    with db.session() as con:
        # a word that occurs in more than one source
        w = con.execute("""SELECT o.item_id, COUNT(DISTINCT o.source_id) k FROM occ o JOIN items i ON i.id=o.item_id JOIN cards c ON c.item_id=i.id AND c.kind='word'
                           GROUP BY o.item_id HAVING k>1 ORDER BY k DESC, o.item_id LIMIT 1""").fetchone()
        assert w, "the sample sources share at least one word"
        card = con.execute("SELECT * FROM cards WHERE item_id=? AND kind='word'", (w["item_id"],)).fetchone()
        v = cards.view(con, card)
        assert 2 <= len(v["sentences"]) <= analyze.POOL and v["sentences"][0]["id"] == card["sentence_id"]
        assert len({x["source_id"] for x in v["sentences"]}) >= 2 and v["sentences"][0]["source_id"] == card["source_id"]   # its first source leads
        assert len({x["text"] for x in v["sentences"]}) == len(v["sentences"]) and all(v["lemma"][:1] in x["text"] for x in v["sentences"])
        total = con.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
        assert con.execute("SELECT COUNT(DISTINCT card_id) FROM card_sentences").fetchone()[0] == con.execute("SELECT COUNT(*) FROM cards WHERE kind<>'kanji'").fetchone()[0]
        line = next(json.loads(x) for x in (cards.write_pack(con, card["source_id"]) / "cards.jsonl").read_text(encoding="utf-8").splitlines()
                    if json.loads(x)["id"] == card["guid"])
        assert [x["text"] for x in line["sentences"]] == [x["text"] for x in v["sentences"]] and line["front"] == line["sentences"][0]["text"]
        # marking the main sentence as bad: the card carries on with the others, the same card as before
        first = v["sentences"][0]
        res = analyze.reject_sentence(con, first["id"])
        again = cards.view(con, con.execute("SELECT * FROM cards WHERE id=?", (card["id"],)).fetchone())
        assert res["moved"] >= 1 and again["guid"] == v["guid"] and first["text"] not in [x["text"] for x in again["sentences"]]
        analyze.restore_sentence(con, first["source_id"], first["text"])
        assert con.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == total
        # listening cards: every sentence in the pool has audio and this word as its only unfamiliar one
        for c in con.execute("SELECT * FROM cards WHERE kind='listen'").fetchall():
            for x in cards.view(con, c)["sentences"]:
                n = con.execute("SELECT COUNT(*) FROM occ o JOIN items i ON i.id=o.item_id WHERE o.sentence_id=? AND i.band IN ('new','young')", (x["id"],)).fetchone()[0]
                assert n == 1 and con.execute("SELECT start FROM sentences WHERE id=?", (x["id"],)).fetchone()[0] is not None


def _wait_for_job(c) -> dict:
    """Slow work runs in the background with its progress shown; wait for it as the page does."""
    for _ in range(300):
        j = c.get("/job/status").json()
        if j["state"] != "running":
            return j
        time.sleep(0.05)
    raise AssertionError("the job did not finish")


def test_import_pages(eng):
    from fastapi.testclient import TestClient
    from sumitsubo import web
    web._state.update(dic=eng[0], parser=eng[1])
    with TestClient(web.app) as c:
        assert "Anki" in c.get("/imports").text
        r = c.post("/imports/add", data=dict(action="file"), files=dict(upload=("my-deck.apkg", (FX / "anki-current.apkg").read_bytes())), follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/job" and "Reading the file" in c.get("/job").text
        j = _wait_for_job(c)
        assert j["state"] == "done" and j["url"].startswith("/imports/")
        url = j["url"]
        page = c.get(url).text
        assert "Japanese::Core" in page and "Physics terms" in page and "Progress + studied cards (+6 cards)" in page and "検出器" in page
        form = {"map-0-word": "0", "map-0-reading": "1", "map-0-sentence": "3", "map-1-word": "0", "map-1-reading": "", "map-1-sentence": "",
                "mode-0": "studied", "mode-1": "skip", "action": "apply"}
        r = c.post(url, data=form, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/job"
        note = urllib.parse.unquote(_wait_for_job(c)["url"])
        assert "progress of 4 words and kanji (2 mature), with 13 past reviews" in note and "4 cards were brought in" in note
        lib = c.get("/").text
        assert "Japanese::Core (imported)" in lib and "my-deck.apkg" in c.get("/imports").text
        with db.session() as con:
            sid = con.execute("SELECT id FROM sources WHERE kind='deck'").fetchone()[0]
            assert json.loads(con.execute("SELECT stats FROM imports WHERE deck='Physics terms'").fetchone()[0])["studied"] == 0
        deck_page = c.get(f"/source/{sid}?all=1").text
        assert "Imports" in deck_page and "Known words" in deck_page and "Young" in deck_page and "due in" in deck_page
        c.post("/imports/add", data=dict(action="file"), files=dict(upload=("broken.apkg", b"not a deck")), follow_redirects=False)
        j = _wait_for_job(c)
        assert j["state"] == "error" and "Could not read" in j["error"]            # a failure is shown, not left hanging
        r = c.post(url + "/delete", follow_redirects=False)
        assert r.status_code == 303
        with db.session() as con:
            assert con.execute("SELECT COUNT(*) FROM sources WHERE kind='deck'").fetchone()[0] == 0
            assert con.execute("SELECT COUNT(*) FROM memory WHERE origin='import'").fetchone()[0] == 0


# ---- paper pages (0.4.0) ------------------------------------------------------------------------
def _font():
    import glob
    for pat in ("/usr/share/fonts/**/NotoSerifCJK*.ttc", "/usr/share/fonts/**/NotoSansCJK*.ttc", "/usr/share/fonts/**/*CJK*.*tc",
                "/usr/share/fonts/**/ipa*.ttf", "/usr/share/fonts/**/TakaoP*.ttf"):
        hit = sorted(glob.glob(pat, recursive=True))
        if hit:
            return hit[0]
    return None


def _page_image(path: Path, text: str, vertical: bool = False):
    """A clean synthetic page, as a scanner app would give."""
    from PIL import Image, ImageDraw, ImageFont
    font = ImageFont.truetype(_font(), 48)
    im = Image.new("L", (1700, 2300), 245)
    d = ImageDraw.Draw(im)
    if vertical:
        x, k, text = 1500, 0, text.replace("\n", "")
        while x > 150 and k < len(text):
            y = 160
            while y < 2100 and k < len(text):
                d.text((x, y), text[k], font=font, fill=20)
                y, k = y + 56, k + 1
            x -= 110
    else:
        y = 180
        for para in text.splitlines():
            for i in range(0, len(para), 26):
                d.text((170, y), para[i:i + 26], font=font, fill=20)
                y += 84
    im.convert("RGB").save(path, quality=90)


needs_pages = pytest.mark.skipif(bool(__import__("sumitsubo.pages", fromlist=["x"]).missing()) or not _font(),
                                 reason="needs Tesseract with Japanese, and a Japanese font to draw test pages")


def test_phone_gate():
    """From another device only the upload page answers, only with the code, only in phone mode."""
    from fastapi.testclient import TestClient
    from sumitsubo import web
    with db.session() as con:
        tok = web.phone_token(con)
    os.environ.pop("SUMITSUBO_PHONE", None)
    with TestClient(web.app, client=("192.168.1.50", 50000)) as c:
        assert c.get(f"/phone/{tok}").status_code == 403                  # not started with --phone
        os.environ["SUMITSUBO_PHONE"] = "1"
        try:
            assert c.get(f"/phone/{tok}").status_code == 200
            for url in ("/", "/inbox", "/settings", "/inbox/status", "/phone/wrong", "/phone", f"/phone/{tok}x"):
                assert c.get(url).status_code == 403, url
            assert c.post("/inbox/act", data=dict(page="1", act="delete")).status_code == 403
            r = c.post(f"/phone/{tok}/upload", files=dict(files=("notes.txt", b"hello")))
            assert r.status_code == 200 and r.json()["ids"] == [] and "not a picture" in r.json()["errors"][0]
        finally:
            os.environ.pop("SUMITSUBO_PHONE", None)
    with TestClient(web.app) as c:                                        # this computer
        page = c.get("/inbox")
        assert page.status_code == 200 and "serve --phone" in page.text
        assert c.get("/upload", follow_redirects=False).headers["location"] == "/inbox"


@needs_pages
def test_page_photos(eng, tmp_path):
    from sumitsubo import pages
    story = (S / "story.txt").read_text(encoding="utf-8")
    paper = (S / "math_paper.txt").read_text(encoding="utf-8")
    half = len(paper) // 2
    cut = paper.index("。", half) + 8                       # break a sentence across two pages
    files = dict(h1=paper[:cut], h2=paper[cut:], v=story)
    for name, text in files.items():
        _page_image(tmp_path / f"{name}.jpg", text, vertical=(name == "v"))
    from PIL import Image
    Image.effect_noise((900, 1200), 60).convert("RGB").save(tmp_path / "blank.jpg")
    Image.open(tmp_path / "h1.jpg").save(tmp_path / "scan.pdf", save_all=True, append_images=[Image.open(tmp_path / "v.jpg")])
    with db.session() as con:
        ids = {n: pages.take(con, tmp_path / f"{n}.jpg")[0] for n in ("h1", "h2", "v", "blank")}
        pdf = pages.take(con, tmp_path / "scan.pdf")
        assert len(pdf) == 2 and "p.2" in con.execute("SELECT name FROM inbox WHERE id=?", (pdf[1],)).fetchone()[0]
        with pytest.raises(RuntimeError):
            pages.take(con, S / "story.txt")
    for pid in list(ids.values()) + pdf:
        pages.process(pid, *eng)
    with db.session() as con:
        got = {r["id"]: r for r in pages.listing(con)}
    assert got[ids["h1"]]["status"] == "ok" and got[ids["h1"]]["layout"] == "h" and got[ids["h1"]]["conf"] > 80
    assert got[ids["v"]]["status"] == "ok" and got[ids["v"]]["layout"] == "v"          # found by itself
    assert got[pdf[1]]["layout"] == "v" and got[pdf[0]]["layout"] == "h"               # page by page
    assert got[ids["blank"]]["status"] == "poor" and got[ids["blank"]]["message"]
    assert "confidence" in pages.text_of(ids["h1"])

    # forcing the wrong layout gives a worse reading; putting it back restores it
    with db.session() as con:
        pages.requeue(con, [ids["v"]], "h")
    pages.process(ids["v"], *eng)
    with db.session() as con:
        forced = con.execute("SELECT * FROM inbox WHERE id=?", (ids["v"],)).fetchone()
        assert forced["layout"] == "h" and forced["conf"] < got[ids["v"]]["conf"] - 20 and forced["status"] == "poor"
        pages.requeue(con, [ids["v"]], "auto")
    pages.process(ids["v"], *eng)

    # pages in a chosen order become one source; the unreadable one is left out and named
    with db.session() as con:
        sid = pages.make_source(con, [ids["h1"], ids["h2"], ids["blank"]], title="Paper pages", tags="paper")
        assert [r["id"] for r in pages.listing(con)] == [ids["v"]] + pdf                 # the rest still wait
    ingest.process(sid, *eng)
    with db.session() as con:
        src = con.execute("SELECT * FROM sources WHERE id=?", (sid,)).fetchone()
        assert src["status"] == "done", src["message"]
        stats = json.loads(src["options"])["stats"]
        assert stats["read"] == 2 and stats["pages"] == 3 and stats["left_out"][0]["name"] == "blank.jpg" and stats["sentences"] > 5
        sents = [dict(r) for r in con.execute("SELECT text, location FROM sentences WHERE source_id=? ORDER BY idx", (sid,))]
        text = "".join(s["text"] for s in sents)
        assert "勾配" in text and " " not in text and {s["location"] for s in sents} == {"p.1", "p.2"}
        whole = [s.strip() + "。" for s in paper.replace("\n", "").split("。") if s.strip()]
        assert sum(s["text"] in whole for s in sents) >= len(sents) * 0.6               # read exactly, joined across lines and the page break
        assert con.execute("SELECT COUNT(*) FROM occ WHERE source_id=?", (sid,)).fetchone()[0] > 20
        n_before = len(sents)

        # a later batch is added to the end of the same source
        pages.make_source(con, [ids["v"]], into=sid)
        with pytest.raises(RuntimeError):
            pages.make_source(con, [ids["v"]], into=sid)                                 # already used
    ingest.process(sid, *eng)
    with db.session() as con:
        src = con.execute("SELECT * FROM sources WHERE id=?", (sid,)).fetchone()
        assert src["status"] == "done" and src["n_sentences"] > n_before
        assert con.execute("SELECT COUNT(*) FROM sentences WHERE source_id=? AND location='p.4'", (sid,)).fetchone()[0] > 0
        assert "少年" in "".join(r[0] for r in con.execute("SELECT text FROM sentences WHERE source_id=?", (sid,)))
        ingest.delete_source(con, sid)
        assert con.execute("SELECT COUNT(*) FROM inbox WHERE source_id=?", (sid,)).fetchone()[0] == 0
        assert not (pages.folder() / f"p{ids['h1']}.jpg").exists() and (pages.folder() / f"p{pdf[0]}.jpg").exists()
        pages.delete(con, pdf)
        assert pages.listing(con) == []


@needs_pages
def test_inbox_in_the_browser(eng, tmp_path):
    from fastapi.testclient import TestClient
    from sumitsubo import pages, web
    web._state.update(dic=eng[0], parser=eng[1])
    _page_image(tmp_path / "a.jpg", (S / "math_paper.txt").read_text(encoding="utf-8"))
    with TestClient(web.app) as c:
        with db.session() as con:
            tok = web.phone_token(con)
        os.environ["SUMITSUBO_PHONE"] = "1"
        try:
            with TestClient(web.app, client=("192.168.1.50", 50000)) as phone:
                assert "Photograph a page" in phone.get(f"/phone/{tok}").text
                r = phone.post(f"/phone/{tok}/upload", files=dict(files=("a.jpg", (tmp_path / "a.jpg").read_bytes(), "image/jpeg")))
                pid = r.json()["ids"][0]
                for _ in range(120):
                    st = phone.get(f"/phone/{tok}/status?ids={pid}").json()[0]
                    if st["status"] not in ("waiting", "reading"):
                        break
                    time.sleep(0.5)
                assert st["status"] == "ok" and st["layout"] == "h"
            inbox = c.get("/inbox")
            assert inbox.status_code == 200 and "a.jpg" in inbox.text and "<svg" in inbox.text and f"/phone/{tok}" in inbox.text
        finally:
            os.environ.pop("SUMITSUBO_PHONE", None)
        assert c.get(f"/inbox/{pid}/picture?small=1").headers["content-type"] == "image/jpeg"
        assert "layout: horizontal" in c.get(f"/inbox/{pid}/text").text
        assert "Tick the pages first" in urllib.parse.unquote(c.post("/inbox/act", data=dict(act="make"), follow_redirects=False).headers["location"])
        r = c.post("/inbox/act", data=dict(page=str(pid), act="make", title="From the phone", tags="paper"), follow_redirects=False)
        assert r.headers["location"] == "/"
        for _ in range(120):
            row = [s for s in c.get("/status").json()][-1]
            if row["status"] in ("done", "error"):
                break
            time.sleep(0.5)
        assert row["status"] == "done", row
        page = c.get(f"/source/{row['id']}").text
        assert "Read from 1 of 1 page photos" in page and "From the phone" in page
        assert f"/inbox/{pid}/picture?small=1" in page and f"/inbox/{pid}/text" in page          # the photos stay visible on the source
        assert c.get(f"/inbox/{pid}/picture").status_code == 200
        assert c.post(f"/source/{row['id']}/delete", follow_redirects=False).status_code == 303
        with db.session() as con:
            assert pages.listing(con) == []


@pytest.mark.skipif(not os.environ.get("SUMITSUBO_NDL") or not _font(), reason="set SUMITSUBO_NDL to a checkout of ndlocr-lite to run")
def test_ndlocr_reader(eng, tmp_path):
    """With NDLOCR-Lite installed it reads the pages; one read by Tesseract earlier is read again."""
    from sumitsubo import ndl, pages
    story = (S / "story.txt").read_text(encoding="utf-8")
    _page_image(tmp_path / "t.jpg", (S / "math_paper.txt").read_text(encoding="utf-8"))
    with db.session() as con:
        pid = pages.take(con, tmp_path / "t.jpg")[0]
    assert pages.engine() == "tesseract"
    pages.process(pid, *eng)
    link = ndl.home()
    link.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(os.environ["SUMITSUBO_NDL"], link)
    try:
        assert pages.engine() == "ndl" and pages.missing() is None
        with db.session() as con:
            assert pages.stale(con, [pid]) == [pid]
            sid = pages.make_source(con, [pid], title="NDL pages")
        ingest.process(sid, *eng)                                   # reads the page again with the better reader
        with db.session() as con:
            row = con.execute("SELECT * FROM inbox WHERE id=?", (pid,)).fetchone()
            assert row["engine"] == "ndl" and row["status"] == "ok" and row["layout"] == "h" and row["conf"] > 85
            src = con.execute("SELECT * FROM sources WHERE id=?", (sid,)).fetchone()
            assert src["status"] == "done" and json.loads(src["options"])["stats"]["engine"] == "ndl"
            sents = [r[0] for r in con.execute("SELECT text FROM sentences WHERE source_id=? ORDER BY idx", (sid,))]
            paper = (S / "math_paper.txt").read_text(encoding="utf-8").replace("\n", "")
            whole = [s.strip() + "。" for s in paper.split("。") if s.strip()]
            assert len(sents) >= len(whole) - 2 and sum(s in whole for s in sents) >= len(sents) - 1     # nearly every sentence, exactly
            assert "read by: NDLOCR-Lite" in pages.text_of(pid)
            ingest.delete_source(con, sid)
        # a column printed in two pieces is put back together; a misread label is dropped
        col = lambda t, x, y, h, c: dict(para="b", order=0, text=t, conf=c, vertical=True, body=True, cx=x, cy=y + h / 2, w=60, h=h)  # noqa: E731
        got = ndl._in_columns([col("である", 700, 100, 800, 94), col("養和元年、", 810, 300, 600, 93), col("【庭園】", 815, 100, 180, 86),
                               col("えられ", 590, 100, 800, 94), col("大学校", 925, 100, 150, 72), col("平安遷都", 920, 280, 600, 93)])
        assert [r["text"] for r in got] == ["平安遷都", "【庭園】", "養和元年、", "である", "えられ"]
    finally:
        link.unlink()


def test_spaces_in_pdf_text():
    assert ingest.tidy("の 理 解 と と も に 述 べ て お こ う .") == "の理解とともに述べておこう。"
    assert ingest.tidy("Kaluza と Klein の理論") == "Kaluza と Klein の理論"          # spaces beside Latin words stay
    assert ingest.tidy("ブラック ホール　の 質量は 3.5 倍。") == "ブラックホールの質量は 3.5 倍。"
    assert ingest.tidy("考\u2009え\u00a0よ\u200bう .") == "考えよう。"                      # thin, no-break and zero-width spaces
    assert ingest.SPACED.search("そ こ で 長 い 線 分") and not ingest.SPACED.search("そこで 長い 線分 の")


def test_spaced_sources_are_read_again(eng):
    with db.session() as con:
        sid = con.execute("INSERT INTO sources(kind, title, origin, status, added) VALUES('doc','spaced','x.pdf','done',0)").lastrowid
        ok = con.execute("INSERT INTO sources(kind, title, origin, status, added) VALUES('doc','fine','y.pdf','done',0)").lastrowid
        for i in range(6):
            con.execute("INSERT INTO sentences(source_id, idx, text) VALUES(?,?,?)", (sid, i, "こ れ は 任 意 次 元 で 成 立 す る ."))
            con.execute("INSERT INTO sentences(source_id, idx, text) VALUES(?,?,?)", (ok, i, "これは任意次元で成立する。"))
        assert ingest.requeue_spaced(con) == [sid]
        con.execute("DELETE FROM sentences WHERE source_id IN (?,?)", (sid, ok))
        con.execute("DELETE FROM sources WHERE id IN (?,?)", (sid, ok))


def test_device_deck_and_log(eng, tmp_path):
    """The deck a device reads, and its log coming back: replayed as the device replays it."""
    from sumitsubo import device
    with db.session() as con:
        stats, out = device.sync_folder(con, tmp_path)
        assert stats is None and (out / "deck" / "deck.ini").exists() and not (out / "deck" / "deck.txt").exists()
        lines = [l.split("\t") for l in (out / "deck" / "cards.tsv").read_text(encoding="utf-8").splitlines()]
        assert lines and all(len(c) == 13 for c in lines)
        new = next(c for c in lines if c[1] == "W" and c[5] == "")
        seeded = next(c for c in lines if c[1] == "W" and c[5])
        assert "\x01" in new[9] and "format=1" in (out / "deck" / "deck.ini").read_text()
        d = device.day(db.now())
        log = tmp_path / device.LOG
        log.parent.mkdir(parents=True)
        log.write_text(f"{d},{new[0]},3\n{d},{seeded[0]},1\n{d},{seeded[0]},undo,{seeded[5]},0,0\n{d},ffffffffffffffff,3\n", encoding="utf-8")
        for _ in range(2):                                    # reading the same log twice changes nothing
            stats, _ = device.sync_folder(con, tmp_path)
            assert stats["reviews"] == 2 and stats["cards"] == 1 and stats["unknown"] == 1
            rows = con.execute("SELECT * FROM memory WHERE origin='review'").fetchall()
            assert len(rows) == 1 and rows[0]["item_id"] == int(new[2]) and abs(rows[0]["stability"] - 2.3065) < 1e-3
            assert device.day(rows[0]["due"]) == d + 2 and device.day(rows[0]["last_review"]) == d
        again = [l.split("\t") for l in (out / "deck" / "cards.tsv").read_text(encoding="utf-8").splitlines()]
        same = next(c for c in again if c[0] == new[0])
        assert same[5] == ""                                  # its starting state on the device never changes
        con.execute("DELETE FROM memory WHERE origin='review'")
        con.execute("DELETE FROM device_cards")
        analyze.rebuild_cards(con)


AOZORA = """山の音
試験 太郎

-------------------------------------------------------
【テキスト中に現れる記号について】

《》：ルビ
（例）私《わたくし》
-------------------------------------------------------

［＃３字下げ］上　朝［＃「上　朝」は大見出し］

　私《わたくし》は先生と山に登った。先生は｜一寸《ちょっと》考《かんが》えてから、「今日《こんにち》の学生は本を読まない」と言った。
　※［＃「區＋鳥」、第3水準1-94-69］外の本を読んだ※［＃感嘆符二つ、1-8-75］　くり返し／＼読んだ。
［＃改ページ］

［＃３字下げ］［＃大見出し］下　夜［＃大見出し終わり］

　夜になると雨が降った。私は｜手紙《てがみ》を書いた［＃「書いた」に傍点］。

底本：「試験全集」試験書房
入力：試験者
"""


def _aozora_site(monkeypatch):
    import io
    import zipfile
    from sumitsubo import aozora
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("yama.txt", AOZORA.replace("\n", "\r\n").encode("cp932"))
    card = ('<a href="./files/9_ruby_1.zip">zip</a> <a href="./files/9.ebk">ebk</a> <a href="./files/9_2.html">html</a> '
            '<a href="https://example.com/files/x.zip">elsewhere</a>').encode()
    asked = []

    def get(url):
        asked.append(url)
        return buf.getvalue() if url.endswith(".zip") else card
    monkeypatch.setattr(aozora, "_get", get)
    return asked


def test_aozora_notation(monkeypatch):
    from sumitsubo import aozora
    asked = _aozora_site(monkeypatch)
    assert aozora.is_aozora("https://www.aozora.gr.jp/cards/000009/card9.html") and not aozora.is_aozora("https://aozora.gr.jp.example.com/x")
    assert ingest.classify("https://www.aozora.gr.jp/cards/000009/card9.html") == "aozora"
    b = aozora.fetch("https://www.aozora.gr.jp/cards/000009/card9.html")
    assert asked[-1] == "https://www.aozora.gr.jp/cards/000009/files/9_ruby_1.zip"      # the text, not the .ebk, and nothing from another site
    assert (b.title, b.author) == ("山の音", "試験 太郎") and b.colophon[0].startswith("底本：")
    assert [x.kind for x in b.blocks] == ["h1", "p", "p", "break", "h1", "p"]
    assert b.blocks[1].runs[:2] == [("私", "わたくし"), ("は先生と山に登った。先生は", None)] and ("一寸", "ちょっと") in b.blocks[1].runs
    assert b.blocks[2].text.startswith("鷗外の本を読んだ") and "〳〵" in b.blocks[2].text and "［" not in "".join(x.text for x in b.blocks)
    ss = aozora.sentences(b)
    assert ss[0] == dict(text="私は先生と山に登った。", location="上　朝", ruby=[("私", "わたくし")])
    assert ss[1]["ruby"] == [("一寸", "ちょっと"), ("考", "かんが"), ("今日", "こんにち")] and ss[-1]["text"] == "私は手紙を書いた。"
    page = ('<h1 class="title">山の音</h1><h2 class="author">試験 太郎</h2><div class="main_text"><h3 class="o-midashi"><a>上</a></h3><br />\r\n'
            '<ruby><rb>私</rb><rp>（</rp><rt>わたくし</rt><rp>）</rp></ruby>は山に登った。<img src="g.png" alt="※(「區＋鳥」、第3水準1-94-69)" class="gaiji" />外。<br />\r\n'
            '</div><div class="bibliographical_information">底本：「試験全集」<br /></div></body>')
    h = aozora.read(page.encode("cp932"), "9_2.html")
    assert (h.title, [x.kind for x in h.blocks], h.blocks[1].runs[0], h.colophon) == ("山の音", ["h1", "p"], ("私", "わたくし"), ["底本：「試験全集」"])
    assert "鷗外" in h.blocks[1].text


def test_aozora_readings():
    """The edition's reading is taken only where the dictionary knows the word with it."""
    from sumitsubo import aozora
    from sumitsubo.parser import Token

    class Dic:
        def readings(self, terms):
            return {"今日": ["きょう", "こんにち"], "辛い": ["からい", "つらい"], "行く": ["いく", "ゆく"], "私": ["わたし", "わたくし"]}

        def same_entry(self, form, a, b):
            return form == "私"
    items = [dict(ruby=[("今日", "こんにち"), ("辛", "つら"), ("行", "おこな"), ("山", "サン"), ("私", "わたくし")]), dict(ruby=[])]
    lists = [[Token("今日", "きょう", "今日"), Token("辛い", "からい", "辛かっ"), Token("行く", "いく", "行っ"), Token("山", "やま", "山"), Token("私", "わたし", "私")],
             [Token("今日", "きょう", "今日"), Token("私", "わたし", "私")]]
    r = aozora.Readings(Dic(), items)
    out = r.apply(lists)
    assert [t.reading for t in out[0]] == ["こんにち", "つらい", "いく", "やま", "わたくし"] and r.changed == 4
    # in a sentence without the reading: 私 follows the edition (one word said two ways), 今日 does not (two words)
    assert [t.reading for t in out[1]] == ["きょう", "わたくし"]


def test_aozora_book_to_the_reader(eng, monkeypatch, tmp_path):
    """A link to a work: sentences for the cards, an EPUB for the device, sent by the next sync."""
    import zipfile
    from sumitsubo import device
    _aozora_site(monkeypatch)
    dic, parser = eng
    with db.session() as con:
        sid = ingest.add(con, "https://www.aozora.gr.jp/cards/000009/card9.html", reader=True)
    ingest.process(sid, dic, parser)
    with db.session() as con:
        s = con.execute("SELECT * FROM sources WHERE id=?", (sid,)).fetchone()
        assert (s["status"], s["kind"], s["title"], s["to_reader"]) == ("done", "aozora", "山の音（試験 太郎）", 1), s["message"]
        assert s["n_sentences"] == 6 and s["book"].endswith("山の音 - 試験 太郎.epub")
        book = config.resolve(s["book"])
        with zipfile.ZipFile(book) as z:
            assert z.namelist()[0] == "mimetype" and z.infolist()[0].compress_type == zipfile.ZIP_STORED
            opf, first = z.read("OEBPS/content.opf").decode(), z.read("OEBPS/c0001.xhtml").decode()
            assert 'page-progression-direction="rtl"' in opf and "<dc:creator>試験 太郎</dc:creator>" in opf
            assert "<ruby>私<rt>わたくし</rt></ruby>は先生と山に登った。" in first and "<h2>上　朝</h2>" in first
            assert "OEBPS/c0002.xhtml" in z.namelist() and "底本" in z.read("OEBPS/colophon.xhtml").decode()
            import xml.dom.minidom
            for n in z.namelist()[1:]:
                if n.endswith((".xhtml", ".opf", ".ncx", ".xml")):
                    xml.dom.minidom.parseString(z.read(n))            # every part is well-formed
        said = []
        device.sync_folder(con, tmp_path, said.append)
        there = tmp_path / "Books" / book.name
        assert there.read_bytes() == book.read_bytes() and sum("Copied" in x for x in said) == 1
        device.sync_folder(con, tmp_path, said.append)
        assert sum("Copied" in x for x in said) == 1                                          # already there: not copied again
        con.execute("UPDATE sources SET to_reader=0 WHERE id=?", (sid,))
        assert device.books(con) == []
        ingest.delete_source(con, sid)
        assert not book.exists()


def test_progress_while_a_source_is_read(eng):
    """A long text is parsed in pieces, the count is reported, and the second parse costs nothing."""
    dic, parser = eng
    texts = ["私は本を読んだ。", "先生は山に登った。", "雨が降った。"] * 120
    told, calls = [], []

    class Spy:
        def parse(self, t):
            calls.append(len(t))
            return parser.parse(t)
    p = ingest._Counted(Spy(), lambda stage, done, total: told.append((done, total)))
    a = p.parse(texts)
    assert told == [(150, 360), (300, 360), (360, 360)] and calls == [150, 150, 60] and a == parser.parse(texts)
    assert p.parse(texts) is a and len(calls) == 3


def test_steps_and_pages(eng, monkeypatch):
    """Each step of reading a source reports where it is; a source's long lists come a page at a time."""
    from fastapi.testclient import TestClient
    from sumitsubo import web
    dic, parser = eng
    seen = []
    real = analyze.rebuild_cards

    def spy(con, tell=None):
        return real(con, lambda d, n: (seen.append(("cards", d, n)), tell and tell(d, n)))
    monkeypatch.setattr(analyze, "rebuild_cards", spy)
    text = "".join(f"{w}は{v}を読んだ。\n" for w in ("私", "先生", "学生", "友達") for v in ("本", "手紙", "新聞", "小説", "雑誌")) * 16
    with db.session() as con:
        sid = ingest.add(con, text=text, title="long")
    assert web._progress(sid) == dict(step=0, steps=0, done=0, total=0, unit="", quiet=0)
    ingest.process(sid, dic, parser)
    assert sid not in ingest.progress and seen and seen[-1][1] == seen[-1][2] and [d for _, d, _ in seen] == sorted(d for _, d, _ in seen)
    with db.session() as con:
        s = con.execute("SELECT * FROM sources WHERE id=?", (sid,)).fetchone()
        assert s["status"] == "done" and s["n_sentences"] == 320 and ingest.steps_for(s) == 4
    with TestClient(web.app) as c:
        one = c.get(f"/source/{sid}?show=text").text
        assert one.count('<span class="ja">') == 50 and "1&ndash;50 of 320" in one and "Next" in one and "Previous" not in one
        last = c.get(f"/source/{sid}?show=text&p=99").text
        assert last.count('<span class="ja">') == 20 and "301&ndash;320 of 320" in last and "Next" not in last
        assert c.get(f"/source/{sid}?show=cards&all=1").status_code == 200 and c.get(f"/source/{sid}?show=known&p=2").status_code == 200
    with db.session() as con:
        ingest.delete_source(con, sid)


def test_stuck_step_is_written_down(monkeypatch):
    """When a step reports nothing for a while, where the program is gets written to stuck.log."""
    import time
    monkeypatch.setattr(ingest, "QUIET", 1)
    ingest.stuck_log().unlink(missing_ok=True)
    ingest._stuck.clear()
    ingest._watch("source 7, step 3 of 4: storing the words")

    def somewhere_slow():
        time.sleep(1.6)
    somewhere_slow()
    ingest._watch(None)
    log = ingest.stuck_log().read_text(encoding="utf-8")
    assert "source 7, step 3 of 4: storing the words" in log and "Timeout" in log and "somewhere_slow" in log
    n = log.count("Timeout")
    time.sleep(1.3)
    assert ingest.stuck_log().read_text(encoding="utf-8").count("Timeout") == n      # disarmed


def test_library_page_does_not_crawl_with_a_long_book(eng):
    """How many sentences are readable is counted from the source's own words, not by trying every
    word of the library against every sentence of the book (which took minutes for a novel)."""
    with db.session() as con:
        assert con.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='occ_sentence'").fetchone()
        seen = []
        con.set_trace_callback(seen.append)
        analyze.coverage(con, 1)
        con.set_trace_callback(None)
        q = next(s for s in seen if "one_away" in s)
        plan = " | ".join(r[3] for r in con.execute("EXPLAIN QUERY PLAN " + q))
        assert "occ_source" in plan and "sentences" not in q, plan


def test_aozora_dots_indents_pictures(monkeypatch):
    from sumitsubo import aozora
    text = ("題\n作者\n\n［＃ここから２字下げ］\n　これは大事［＃「大事」に傍点］な話だ。\n［＃ここで字下げ終わり］\n"
            "　［＃傍点］全部［＃傍点終わり］読んだ。\n［＃地付き］作者しるす\n［＃３字下げ］見出しでない行\n"
            "［＃挿絵（fig9_01.png）入る］\n　終わり。\n")
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 20
    b = aozora.parse_text(text)
    assert [(x.kind, x.indent, x.end) for x in b.blocks] == [("p", 2, False), ("p", 0, False), ("p", 0, True), ("p", 3, False), ("img", 0, False), ("p", 0, False)]
    assert b.blocks[0].text == "これは大事な話だ。" and aozora.EM0 in b.blocks[0].runs[0][0]
    assert [s["text"] for s in aozora.sentences(b)][:2] == ["これは大事な話だ。", "全部読んだ。"]
    assert aozora._inline(b.blocks[0].runs) == 'これは<em class="sesame">大事</em>な話だ。'
    assert aozora._inline([("a" + aozora.EM0 + "b", None)]) == 'a<em class="sesame">b</em>'          # never left open
    monkeypatch.setattr(aozora, "_get", lambda url: png if url.endswith("fig9_01.png") else text.encode("cp932"))
    got = aozora.fetch("https://www.aozora.gr.jp/cards/000009/files/9_1.txt")
    assert list(got.images) == ["fig9_01.png"]
    import zipfile
    out = aozora.to_epub(got, config.data_dir() / "work" / "dots.epub")
    with zipfile.ZipFile(out) as z:
        page = z.read("OEBPS/c0001.xhtml").decode()
        assert '<p class="i2">これは<em class="sesame">大事</em>な話だ。</p>' in page and '<p class="end">作者しるす</p>' in page
        assert '<img src="img/fig9_01.png" alt=""/>' in page and z.read("OEBPS/img/fig9_01.png") == png and 'href="img/fig9_01.png" media-type="image/png"' in z.read("OEBPS/content.opf").decode()
    monkeypatch.setattr(aozora, "_get", lambda url: b"not a picture" if url.endswith(".png") else text.encode("cp932"))
    assert not aozora.fetch("https://www.aozora.gr.jp/cards/000009/files/9_1.txt").images       # a bad picture is left out, the book stays


def test_what_the_device_sends_back(eng, tmp_path):
    """Contrast reviews get their own state; a sentence marked bad leaves its cards; a word saved in
    the reader is put first, or gets a card from its sentence."""
    from sumitsubo import device
    dic, parser = eng
    with db.session() as con:
        _, out = device.sync_folder(con, tmp_path)
        lines = [l.split("\t") for l in (out / "deck" / "cards.tsv").read_text(encoding="utf-8").splitlines()]
        x = next(c for c in lines if c[1] == "X")
        w = next(c for c in lines if c[1] == "W" and c[5] == "" and c[9].count("\x1f") >= 1)       # a word card with two sentences
        ids = con.execute("SELECT sentences FROM device_cards WHERE guid=?", (w[0],)).fetchone()[0].split(",")
        bad = con.execute("SELECT source_id, text FROM sentences WHERE id=?", (int(ids[1]),)).fetchone()
        d = device.day(db.now())
        log = tmp_path / device.LOG
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(f"{d},{x[0]},3\n{d},{w[0]},bad,1\n", encoding="utf-8")
        said = []
        device.sync_folder(con, tmp_path, said.append)
        assert "1 contrast cards" in said[0] and "1 sentences marked bad" in said[0]
        cid = con.execute("SELECT id FROM contrasts WHERE guid=?", (x[0],)).fetchone()[0]
        m = con.execute("SELECT * FROM contrast_memory WHERE contrast_id=?", (cid,)).fetchone()
        assert m and abs(m["stability"] - 2.3065) < 1e-3 and contrast.view(con, con.execute("SELECT * FROM contrasts WHERE id=?", (cid,)).fetchone())["memory"]["state"] == "young"
        assert con.execute("SELECT 1 FROM rejected WHERE source_id=? AND text=?", tuple(bad)).fetchone()
        now = con.execute("SELECT sentences FROM device_cards WHERE guid=?", (w[0],)).fetchone()[0].split(",")
        assert ids[1] not in now
        # the same log again: the mark is not applied to whatever sentence now sits in that place
        n = con.execute("SELECT COUNT(*) FROM rejected").fetchone()[0]
        device.sync_folder(con, tmp_path)
        assert con.execute("SELECT COUNT(*) FROM rejected").fetchone()[0] == n and con.execute("SELECT COUNT(*) FROM contrast_memory").fetchone()[0] == 1

        # words saved in the reader
        have = con.execute("""SELECT i.id, i.lemma, i.reading FROM items i JOIN cards c ON c.item_id=i.id AND c.kind='word'
                              WHERE i.band='new' ORDER BY c.source_id DESC, c.rank DESC LIMIT 1""").fetchone()     # last in line today
        head = "#separator:comma\n#html:true\n#guid column:1\n#tags column:11\n#columns:GUID,Word,Reading,Sentence,Definition,Book,Author,Date,Dictionary,Furigana,Tags\n"
        csv = tmp_path / device.SAVED
        csv.parent.mkdir(parents=True)
        csv.write_text(head + f'g1,{have["lemma"]},{have["reading"]},"x<b>{have["lemma"]}</b>y",def,Book A,,2026-11-23,JMdict,,matcha\n'
                       'g2,手紙,てがみ,"友達に<b>手紙</b>を書いた。","a letter, with a comma",こころ,夏目,2026-11-23,JMdict,,matcha\n', encoding="utf-8")
        said = []
        engine_calls = []
        device.sync_folder(con, tmp_path, said.append, lambda: (engine_calls.append(1), (dic, parser))[1])
        assert any("2 words saved in the reader" in s for s in said) and engine_calls == [1]
        assert analyze.queue(con)[0]["item_id"] in {r[0] for r in con.execute("SELECT item_id FROM wanted")}
        assert con.execute("SELECT 1 FROM wanted WHERE item_id=?", (have["id"],)).fetchone()
        got = {r["title"]: r for r in con.execute("SELECT * FROM sources WHERE kind='saved'")}
        assert set(got) == {"Book A", "こころ"}                                        # one source for each book, named after it
        src = got["こころ"]
        assert src["status"] == "done" and src["n_sentences"] == 1
        assert con.execute("SELECT 1 FROM sentences WHERE source_id=? AND text='友達に手紙を書いた。'", (src["id"],)).fetchone()
        deck = [l.split("\t") for l in (tmp_path / "sumitsubo" / "deck" / "cards.tsv").read_text(encoding="utf-8").splitlines()]
        assert next(c for c in deck if c[1] == "W" and c[2] == str(have["id"]))[12] == "1"
        device.sync_folder(con, tmp_path, said.append, lambda: engine_calls.append(1))      # nothing new: the engine is not started
        assert engine_calls == [1]
        # A word saved in a book this program has makes no new source; a later word of こころ joins its source.
        own = con.execute("SELECT title FROM sources WHERE kind<>'saved' AND status='done' LIMIT 1").fetchone()["title"]
        assert device._book_source(con, own) and not device._book_source(con, "こころ")
        csv.write_text(csv.read_text(encoding="utf-8") + f'g3,手紙,てがみ,"<b>手紙</b>が来た。",def,{own},,2026-11-24,JMdict,,matcha\n'
                       'g4,先生,せんせい,"<b>先生</b>に会った。",def,こころ,,2026-11-24,JMdict,,matcha\n', encoding="utf-8")
        device.sync_folder(con, tmp_path, said.append, lambda: (dic, parser))
        again = {r["title"]: r for r in con.execute("SELECT * FROM sources WHERE kind='saved'")}
        assert set(again) == {"Book A", "こころ"} and again["こころ"]["id"] == src["id"] and again["こころ"]["n_sentences"] == 2
        # The single source of earlier versions is split by book at the next sync.
        for r in again.values():
            ingest.delete_source(con, r["id"])
        con.execute("INSERT INTO sources(kind, title, origin, status, added, options, priority) VALUES('saved',?,?,'done',?,'{}',1)",
                    (device.SAVED_TITLE, device.SAVED, 0))
        con.commit()
        device.sync_folder(con, tmp_path, said.append, lambda: (dic, parser))
        assert {r["title"] for r in con.execute("SELECT title FROM sources WHERE kind='saved'")} == {"Book A", "こころ"}
        assert (config.data_dir() / "device-log.csv").exists() or not (tmp_path / device.LOG).exists()
        for r in con.execute("SELECT id FROM sources WHERE kind='saved'").fetchall():
            ingest.delete_source(con, r["id"])
        con.execute("DELETE FROM wanted")
        con.execute("DELETE FROM saved")
        con.execute("DELETE FROM rejected WHERE source_id=? AND text=?", tuple(bad))
        con.execute("DELETE FROM contrast_memory")
        con.execute("DELETE FROM memory WHERE origin='review'")
        analyze.rebuild_cards(con)


def test_card_setup(eng, tmp_path):
    """What a card needs once comes from Matcha's release files; only their own folder is unpacked."""
    import zipfile
    from sumitsubo import device
    asked = []

    def get(url, dest):
        asked.append(url)
        with zipfile.ZipFile(dest, "w") as z:
            folder = "dictionaries/jp/vocab.dat" if "dictionar" in url else "fonts/NotoSerifJP/a.cpfont"
            z.writestr(folder, b"x")
            z.writestr("README.txt", b"not copied")
            z.writestr("../outside.txt", b"never")
    for f in (config.data_dir() / "downloads").glob("japanese-*.zip"):
        f.unlink()
    card, said = tmp_path / "card", []
    with db.session() as con:
        device.card_setup(con, card, said.append, get=get)
        assert (card / "dictionaries/jp/vocab.dat").exists() and (card / "fonts/NotoSerifJP/a.cpfont").exists() and (card / "sumitsubo/deck/deck.ini").exists()
        assert not (card / "README.txt").exists() and not (tmp_path / "outside.txt").exists() and len(asked) == 2
        device.card_setup(con, card, said.append, get=get)
        assert len(asked) == 2 and sum("Already on the card" in s for s in said) == 2
    for f in (config.data_dir() / "downloads").glob("japanese-*.zip"):
        f.unlink()


def test_sync_button_and_paged_cards(eng, tmp_path):
    import time
    from fastapi.testclient import TestClient
    from sumitsubo import web
    with TestClient(web.app) as c:
        assert "Sync now" in c.get("/reader").text
        r = c.post("/sync", data={"where": str(tmp_path)}, follow_redirects=False)
        assert r.status_code == 303
        for _ in range(100):
            j = c.get("/job/status").json()
            if j["state"] != "running":
                break
            time.sleep(0.2)
        assert j["state"] == "done", j
        home = c.get("/reader").text
        assert "Last sync" in home and "Wrote the deck" in home and (tmp_path / "sumitsubo/deck/cards.tsv").exists()
        assert str(tmp_path) in home and "Synced " in c.get("/").text                 # the address is kept; the sidebar says when
        r = c.post("/sync", data={"where": str(tmp_path / "nowhere")}, follow_redirects=False)
        for _ in range(50):
            j = c.get("/job/status").json()
            if j["state"] != "running":
                break
            time.sleep(0.2)
        assert j["state"] == "error" and "no folder" in j["error"]
        page = c.get("/cards?state=all").text
        assert 0 < page.count('class="lrow') <= web.PER_PAGE and ("Its sentences" in page or "Words it is shown in" in page) and "p=2" in page and "Suspend this" in page
        assert c.get("/cards?state=all&p=9999").status_code == 200 and c.get("/contrast").status_code == 200


def test_what_the_reader_is_told(eng, tmp_path):
    """The small word filters beside the deck: every member is found, and the device's hash is this one."""
    from sumitsubo import device
    assert device.fnv("sumitsubo".encode()) == 0x772513CFEA6E9AEE and device.fnv("私\tわたくし".encode()) == 0xB2433F15A26FF9C4   # as in test_core.cpp
    keys = [f"語{i}" for i in range(3000)]
    f = device.word_filter(keys, 16)
    assert f[:4] == b"SBL1" and all(device.filter_has(f, k) for k in keys)
    assert sum(device.filter_has(f, f"他{i}") for i in range(20000)) < 40          # a stray in a thousand at the very most
    assert len(device.word_filter([f"k{i}" for i in range(200000)])) <= device.MAX_FILTER + 9
    assert device._stem("考える", "かんがえる") == ("考", "かんが") and device._stem("手紙", "てがみ") == ("手紙", "てがみ")
    with db.session() as con:
        out = device.write(con, tmp_path) / "deck"
        words = (out / "words.bloom").read_bytes()
        for r in con.execute("SELECT i.lemma FROM items i JOIN cards c ON c.item_id=i.id WHERE i.kind='word' AND i.band<>'suspended' LIMIT 200"):
            assert device.filter_has(words, r[0])
        known = (out / "known.bloom").read_bytes()
        m = con.execute("SELECT lemma, reading FROM items WHERE kind='word' AND band='mature' AND reading<>lemma LIMIT 1").fetchone()
        if m and dictdata.kanji_of(m["lemma"]):
            assert device.filter_has(known, f"{m['lemma']}\t{dictdata.hira(m['reading'])}")
        assert "log_lines=" in (out / "deck.ini").read_text() and (out / "books.tsv").exists()
        forms = [l.split("\t") for l in (out / "forms.tsv").read_text(encoding="utf-8").split("\n") if l]
        ids = {l.split("\t")[0] for l in (out / "cards.tsv").read_text(encoding="utf-8").splitlines()}
        assert all(len(f) == 3 and f[0] in ids for f in forms)                 # what the reader needs to follow reviews between syncs
        # the characters shown large on kanji cards, drawn at their real size (when this computer has a Japanese font)
        big = out / "big.bin"
        if device.big_font(con):
            import struct
            data = big.read_bytes()
            em, line_h, count = struct.unpack("<HHI", data[4:12])
            assert data[:4] == b"SBG2" and em == device.BIG_EM and line_h >= em and count > 0
            index = [struct.unpack("<II", data[12 + 8 * i:20 + 8 * i]) for i in range(count)]
            assert index == sorted(index)
            cp, at = index[0]
            w, h, _left, _top, adv, coded = struct.unpack("<BBbbBH", data[at:at + 7])
            assert sum((b & 63) + 1 for b in data[at + 7:at + 7 + coded]) == w * h and adv > 0     # the runs cover the character exactly
            kanji_words = [l.split("\t") for l in (out / "cards.tsv").read_text(encoding="utf-8").splitlines() if l.split("\t")[1] == "K"]
            first = kanji_words[0][9].split("~")[0]
            assert all(ord(ch) in dict(index) for ch in first)
        else:
            assert not big.exists()


def test_anki_export_with_progress(eng, tmp_path):
    """The way back to Anki: a card's memory, its due day and its reviews travel with it."""
    from sumitsubo import device
    out = tmp_path / "back.apkg"
    with db.session() as con:
        device.tables(con)
        v = next(c for c in cards.listing(con, limit=10**9) if c["kind"] == "word")
        now = db.now()
        day = int(now // 86400)
        old = con.execute("SELECT * FROM memory WHERE item_id=? AND skill='word'", (v["item_id"],)).fetchone()
        con.execute("INSERT OR REPLACE INTO memory VALUES(?,?,?,?,?,?,?,?)", (v["item_id"], "word", "review", 12.5, 6.1, now - 3 * 86400, now + 9 * 86400, "review"))
        con.execute("INSERT OR REPLACE INTO device_cards VALUES(?,?,?,?,?)", (v["guid"], v["item_id"], "word", "", ""))
        (config.data_dir() / "device-log.csv").write_text(
            f"{day - 10},{v['guid']},3\n{day - 6},{v['guid']},1\n{day - 6},{v['guid']},undo\n{day - 6},{v['guid']},3\n{day - 3},{v['guid']},4\n", encoding="utf-8")
        hist = device.history(con)[v["guid"]]
        assert [(d - day, r) for d, r, _ in hist] == [(-10, 3), (-6, 3), (-3, 4)] and all(n >= 1 for _, _, n in hist)
        try:
            r = cards.export_anki(con, out, [v["source_id"]], progress=True)
            assert r["cards"] >= 1 and r["reviews"] >= 3
            with zipfile.ZipFile(out) as z:
                z.extract("collection.anki2", tmp_path)
            col = sqlite3.connect(str(tmp_path / "collection.anki2"))
            crt = col.execute("SELECT crt FROM col").fetchone()[0]
            c = col.execute("SELECT c.id, c.type, c.queue, c.due, c.ivl, c.reps, c.data FROM cards c JOIN notes n ON n.id=c.nid WHERE n.guid=?", ("smt" + v["guid"],)).fetchone()
            assert c[1:3] == (2, 2) and c[3] == int((now - crt) // 86400) + 9 and c[4] == 12 and c[5] == 3
            assert json.loads(c[6]) == {"s": 12.5, "d": 6.1}
            log = col.execute("SELECT ease, ivl, lastIvl, type FROM revlog WHERE cid=? ORDER BY id", (c[0],)).fetchall()
            assert [x[0] for x in log] == [3, 3, 4] and log[0][3] == 0 and log[1][2] == log[0][1]
            fresh = col.execute("SELECT COUNT(*) FROM cards WHERE type=0").fetchone()[0]
            col.close()
            assert fresh > 0                                                    # a card never studied stays a new card
            r = cards.export_anki(con, out, [v["source_id"]])                   # without progress: nothing is carried
            assert "cards" not in r or r["cards"] == 0
        finally:
            (config.data_dir() / "device-log.csv").unlink(missing_ok=True)
            con.execute("DELETE FROM device_cards WHERE guid=?", (v["guid"],))
            con.execute("DELETE FROM memory WHERE item_id=? AND skill='word'", (v["item_id"],))
            if old:
                con.execute("INSERT INTO memory VALUES(?,?,?,?,?,?,?,?)", tuple(old))


def test_layout_notes_and_kanji_levels():
    """Aozora's notes for type and frames reach the EPUB; kanji levels come from Waller's lists."""
    from sumitsubo import aozora
    b = aozora.parse_text("題\n作者\n\n　これは大事［＃「大事」は太字］で、［＃傍線］ここ［＃傍線終わり］も見よ。\n［＃ここから２段階小さな文字］\n　小さい注［＃「注」に傍点］。\n"
                          "［＃ここで小さな文字終わり］\n［＃ここから罫囲み］\n　囲みの中［＃「中」は斜体］。\n［＃ここで罫囲み終わり］\n　［＃１段階大きな文字］大［＃大きな文字終わり］きい［＃「きい」に二重傍線］。\n")
    got = [(x.cls, aozora._inline(x.runs), x.text) for x in b.blocks if x.kind == "p"]
    assert got[0] == ((), 'これは<b>大事</b>で、<span class="uline">ここ</span>も見よ。', "これは大事で、ここも見よ。")
    assert got[1] == (("small",), '小さい<em class="sesame">注</em>。', "小さい注。")
    assert got[2] == (("frame",), "囲みの<i>中</i>。", "囲みの中。")
    assert got[3] == ((), '<span class="big">大</span><span class="uline">きい</span>。', "大きい。")
    assert [s["text"] for s in aozora.sentences(b)][:1] == ["これは大事で、ここも見よ。"]        # the marks never reach the cards
    assert aozora._inline([("太字", None)]) == '<b>太<span class="big">字</span></b>'           # left open: closed in order
    assert (analyze.kanji_level(None, "日"), analyze.kanji_level(None, "与"), analyze.kanji_level(None, "並")) == (5, 3, 2)
    assert analyze.kanji_level(dict(jlpt=2, freq=100), "龘") == 3 and analyze.kanji_level(None, "龘") is None    # not on the lists: the old scale
    assert sum(len(v) for v in json.loads((Path(analyze.__file__).parent / "data" / "jlpt_kanji.json").read_text(encoding="utf-8"))["levels"].values()) == 2211


def test_sentence_quality(eng, tmp_path):
    """Debris is cleaned off a sentence; a reference entry or a label never carries a card, and can be brought back."""
    from sumitsubo import quality
    junk = "［・／／：rD ブレーン超弦理論の高次元物体が描く世界像」（東京大学出版会, 2006）."
    assert quality.clean(junk) == "ブレーン超弦理論の高次元物体が描く世界像（東京大学出版会, 2006）." and quality.poor(quality.clean(junk)) == "a reference"
    assert quality.clean("・ 友達に手紙を書いた。") == "友達に手紙を書いた。" and quality.clean("「今日は行く。") == "今日は行く。"
    for good in ("検出器の信号を測定する。", "ダメ！", "これは「大事」な点である。", "彼は（2006年に）東京へ行った。", "LHCのATLAS実験ではヒッグス粒子の質量を測定した。"):
        assert quality.clean(good) == good and quality.poor(good) is None, good
    assert quality.poor("東京大学 理学部 物理学科") == "not a sentence" and quality.poor("x = a + b c d e f の式") and quality.poor("N. Arkani-Hamed et al., Phys. Lett. B 429 (1998) 263.")
    dic, parser = eng
    f = tmp_path / "refs.txt"
    f.write_text("余剰次元の重力を検証する。\n" + junk + "\n・ 重力の検証は難しい。\n", encoding="utf-8")
    with db.session() as con:
        sid = ingest.add(con, str(f), title="Quality")
    ingest.process(sid, dic, parser)
    with db.session() as con:
        texts = [r[0] for r in con.execute("SELECT text FROM sentences WHERE source_id=? ORDER BY idx", (sid,))]
        assert "重力の検証は難しい。" in texts and not any(t.startswith(("［", "・")) for t in texts)
        bad = con.execute("SELECT text, why FROM rejected WHERE source_id=?", (sid,)).fetchall()
        assert len(bad) == 1 and bad[0]["why"] == "a reference"
        used = {r[0] for r in con.execute("SELECT s.text FROM card_sentences cs JOIN sentences s ON s.id=cs.sentence_id WHERE s.source_id=?", (sid,))}
        assert bad[0]["text"] not in used
        analyze.restore_sentence(con, sid, bad[0]["text"])                  # brought back by hand: the sweep leaves it alone
        assert quality.sweep(con)["set_aside"] == 0 or not con.execute("SELECT 1 FROM rejected WHERE source_id=?", (sid,)).fetchone()
        ingest.delete_source(con, sid)
        analyze.rebuild_cards(con)
