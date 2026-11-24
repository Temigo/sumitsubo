"""Command line: setup, serve, paper, deck, sync, card-setup, add, status, score, export-anki, pack, glossary, pairs, priority, contrast, frequency, import, level."""
import argparse
import json
from pathlib import Path

from . import analyze, cards, config, db, dictdata, frequency, imports, ingest, memory


def _engine():
    from .parser import Parser  # noqa: PLC0415
    with db.session() as con:
        gloss = db.setting(con, "gloss_dict")
    dic = dictdata.Dictionary(gloss_dict=gloss)
    return dic, Parser(dic)


def _line(c: dict) -> str:
    c = analyze.normalise_score(c)
    return (f"{c['readable']:5.1f}% readable  ({c['pct']['mature']:4.1f} mature, {c['pct']['young']:4.1f} young, "
            f"{c['pct']['queued'] + c['pct']['new']:4.1f} new)  {c['kanji_new']:3d} new kanji readings  ({c['tier']})")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="sumitsubo", description="Mine Japanese material into word, listening and kanji cards.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("setup", help="download dictionary data and build the dictionary")
    s.add_argument("--jmdict"), s.add_argument("--kanjidic"), s.add_argument("--jlpt-dir")
    s.add_argument("--from-jamdict", action="store_true", help="build from the jamdict-data package (development)")
    s.add_argument("--no-frequency", action="store_true", help="do not download the word frequency list")
    s.add_argument("--no-paper", action="store_true", help="do not fetch NDLOCR-Lite, the reader for photographed pages (about 160 MB)")
    s = sub.add_parser("serve", help="start the browser interface")
    s.add_argument("--host", default="127.0.0.1"), s.add_argument("--port", type=int, default=8378)
    s.add_argument("--phone", action="store_true", help="also let a phone on the same network send page photos (opens the upload page, and only that, to the local network)")
    s = sub.add_parser("deck", help="write the deck a Sumitsubo Reader device studies from")
    s.add_argument("dest", nargs="?", help="folder to write into (default: the data folder's device/); copy its sumitsubo/ to the card")
    s = sub.add_parser("sync", help="exchange with a Sumitsubo Reader device: take in its reviews, send it the deck")
    s.add_argument("where", nargs="?", default="http://sumitsubo.local",
                   help="the device's address while it shows File Transfer (default http://sumitsubo.local), or the folder of its card in a reader")
    s = sub.add_parser("card-setup", help="prepare a card for a Sumitsubo Reader once: Matcha's Japanese dictionary and fonts, and the deck")
    s.add_argument("card", help="the folder of the card in a reader (or the folder the simulator uses)")
    s.add_argument("--no-fonts", action="store_true", help="leave out the Japanese fonts (95 MB); the built-in font is plainer and lacks rare kanji")
    sub.add_parser("paper", help="fetch NDLOCR-Lite, the reader for photographed pages, by itself (setup does this too)")
    s = sub.add_parser("add", help="add and process a source: a link, a file, or text")
    s.add_argument("target", nargs="?"), s.add_argument("--text"), s.add_argument("--title"), s.add_argument("--tags", default="")
    s.add_argument("--series", default=""), s.add_argument("--media", help="recording that goes with a subtitle file")
    s.add_argument("--reader", action="store_true", help="also send the book to the reader device at the next sync (Aozora Bunko links; EPUB, text and Markdown files)")
    sub.add_parser("status", help="list sources with their difficulty")
    sub.add_parser("tidy", help="check the sentences already in the library: remove stray symbols, set aside reference entries and other text that is not a sentence")
    s = sub.add_parser("score", help="score a file or text without adding it")
    s.add_argument("target")
    s = sub.add_parser("export-anki", help="write an Anki deck file")
    s.add_argument("out")
    s.add_argument("--source", type=int, action="append", help="source number (see status); repeat for several")
    s.add_argument("--tag", action="append", help="include every source with this tag; repeat for several")
    s.add_argument("--kind", action="append", choices=["word", "listen", "kanji"], help="only these card kinds")
    s.add_argument("--deck", default="Sumitsubo", help="name of the Anki deck (default: Sumitsubo)")
    s.add_argument("--one-deck", action="store_true", help="put everything in one deck instead of a sub-deck per source")
    s.add_argument("--mature", action="store_true", help="also write mature cards (in Anki they would start as new)")
    s.add_argument("--progress", action="store_true", help="carry each card's memory, due day and reviews, so Anki continues where you are (includes mature cards)")
    s = sub.add_parser("pack", help="write device packs to <data>/packs")
    s.add_argument("--source", type=int, action="append")
    s = sub.add_parser("glossary", help="load a tab-separated glossary: term, reading, meaning")
    s.add_argument("file")
    s = sub.add_parser("pairs", help="load a collocation pair list: noun, particle, verb [, strength, meaning], tab-separated")
    s.add_argument("file")
    s = sub.add_parser("priority", help="set a source's priority: 1 first, 2 normal, 3 later")
    s.add_argument("source", type=int), s.add_argument("n", type=int, choices=(1, 2, 3))
    sub.add_parser("contrast", help="list the contrast cards")
    s = sub.add_parser("frequency", help="load a word frequency list; without a file, download Jiten's once")
    s.add_argument("file", nargs="?", help="a CSV file or a frequency dictionary in Yomitan format (.zip)")
    s.add_argument("--refresh", action="store_true", help="download a fresh copy even if one is already saved")
    s = sub.add_parser("import", help="bring in progress from an Anki deck file, a Renshuu export or a word list")
    s.add_argument("file", nargs="?", help=".apkg or .colpkg (Anki), .json (Renshuu export), or a text/CSV list")
    s.add_argument("--renshuu-key", help="fetch from Renshuu with this API key instead of reading a file")
    s.add_argument("--renshuu-sentences", type=int, default=0, metavar="N",
                   help="Renshuu: also fetch an example sentence for up to N studied words (one request each)")
    s.add_argument("--anki-running", action="store_true", help="read from Anki while it is open (AnkiConnect add-on)")
    s.add_argument("--take", choices=["progress", "studied", "all"], default="progress",
                   help="progress: memory states only (default); studied: also bring the studied cards; all: also the cards not started")
    s = sub.add_parser("level", help="set the level assumed known: 5 (N5) to 1 (N1), 0 for nothing")
    s.add_argument("n", type=int, choices=range(0, 6))
    a = ap.parse_args(argv)

    if a.cmd == "setup":
        dictdata.build(a.jmdict, a.kanjidic, a.jlpt_dir, a.from_jamdict)
        db.init()
        if not a.no_frequency and not frequency.current()["ranks"]:
            try:
                f = frequency.install()
                print(f"Word frequency list ready: {f['ranks']} words ({f['name']}).")
            except Exception as e:  # noqa: BLE001 - optional; only a tie-breaker
                print(f"Word frequency list not loaded ({e}). It is optional; add one later with: sumitsubo frequency [FILE]")
        from . import ndl  # noqa: PLC0415
        if ndl.available():
            print("Reader for photographed pages (NDLOCR-Lite): already installed.")
        elif not a.no_paper:
            try:
                ndl.install(lambda stage, *_: print(stage + " ..."))
                print("Reader for photographed pages (NDLOCR-Lite) ready.")
            except Exception as e:  # noqa: BLE001 - everything else works without it
                print(f"Reader for photographed pages not installed ({e}). Everything else works; try again later with: sumitsubo paper")
        return
    db.init()
    with db.session() as con:
        if memory.migrate(con):                  # data from before 0.2.0
            if db.setting(con, "imports_stale") == "1" and dictdata.db_path().exists():
                imports.reapply_all(con, dictdata.Dictionary(gloss_dict=db.setting(con, "gloss_dict")))
            analyze.rebuild_cards(con)
            print("Converted your data to version 0.2.0: \"known\" is now a memory state, as in Anki.")
        elif con.execute("SELECT 1 FROM cards WHERE kind<>'kanji' LIMIT 1").fetchone() and not con.execute("SELECT 1 FROM card_sentences LIMIT 1").fetchone():
            analyze.rebuild_cards(con)           # sentence pools, new in 0.2.1
            ingest.cut_clips(con)
        if db.setting(con, "colloc_backfilled") != "1" and dictdata.db_path().exists() and a.cmd != "serve":
            analyze.backfill_collocations(con, dictdata.Dictionary(gloss_dict=db.setting(con, "gloss_dict")))   # new in 0.3.0
            analyze.rebuild_cards(con)
        if db.setting(con, "registers_backfilled") != "1" and dictdata.db_path().exists() and a.cmd != "serve":
            analyze.backfill_registers(con, dictdata.Dictionary(gloss_dict=db.setting(con, "gloss_dict")))      # new in 0.3.1
            analyze.rebuild_cards(con)
    if a.cmd == "serve":
        import uvicorn  # noqa: PLC0415
        import os  # noqa: PLC0415
        os.environ["SUMITSUBO_PORT"] = str(a.port)
        print(f"Sumitsubo is at http://127.0.0.1:{a.port}   (data: {config.data_dir()})")
        if a.phone:
            from . import web  # noqa: PLC0415
            os.environ["SUMITSUBO_PHONE"] = "1"
            a.host = "0.0.0.0"  # noqa: S104 - on purpose; web.py refuses everything but the upload page from outside
            with db.session() as con:
                link = web.phone_link(con)
            print(f"Phone upload page: {link or 'no network address found'}   (or scan the code on the Inbox page)")
        elif a.host not in ("127.0.0.1", "localhost", "::1"):
            os.environ["SUMITSUBO_OPEN"] = "1"     # asked for by name: the whole interface, to whoever can reach it
        uvicorn.run("sumitsubo.web:app", host=a.host, port=a.port, log_level="warning")
    elif a.cmd == "tidy":
        from . import quality  # noqa: PLC0415
        with db.session() as con:
            found = quality.sweep(con)
            if found["set_aside"] or found["cleaned"]:
                analyze.rebuild_cards(con)
            db.set_setting(con, "quality_sweep", "1")
        print(f"{found['cleaned']} sentences cleaned, {found['set_aside']} set aside (Cards, Suspended, Bad sentences: each can be brought back)")
    elif a.cmd == "deck":
        from . import device  # noqa: PLC0415
        with db.session() as con:
            out = device.write(con, Path(a.dest) if a.dest else None)
        print(f"Wrote {out}. Copy that sumitsubo folder to the root of the device's card.")
    elif a.cmd == "sync":
        from . import device  # noqa: PLC0415

        with db.session() as con:
            if a.where.startswith("http"):
                device.sync_wifi(con, a.where, print, _engine)
                print("The device has the new deck. Leave File Transfer and open Study.")
            else:
                st, out = device.sync_folder(con, Path(a.where).expanduser(), print, _engine)
                print(f"Wrote the deck to {out}.")
    elif a.cmd == "card-setup":
        from . import device  # noqa: PLC0415
        with db.session() as con:
            device.card_setup(con, Path(a.card), print, fonts=not a.no_fonts)
        print("The card is ready. Later, `sumitsubo sync` with the same folder keeps it up to date.")
    elif a.cmd == "paper":
        from . import ndl  # noqa: PLC0415
        ndl.install(lambda stage, *_: print(stage + " ..."))
        print(f"NDLOCR-Lite is installed in {ndl.home()}. Page photos are now read with it.")
    elif a.cmd == "add":
        dic, parser = _engine()
        with db.session() as con:
            sid = ingest.add(con, a.target, text=a.text, title=a.title, tags=a.tags, series=a.series, media=a.media, reader=a.reader)
        ingest.process(sid, dic, parser)
        with db.session() as con:
            r = con.execute("SELECT * FROM sources WHERE id=?", (sid,)).fetchone()
            if r["status"] != "done":
                print(f"[error] {r['title']}: {r['message']}")
                return 1
            n = {k: v for k, v in con.execute("SELECT kind, COUNT(*) FROM cards WHERE source_id=? GROUP BY kind", (sid,))}
            print(f"[done] {r['title']}: {r['n_sentences']} sentences; cards: {n.get('word', 0)} word, "
                  f"{n.get('listen', 0)} listening, {n.get('kanji', 0)} kanji")
            print("  when added:", _line(json.loads(r["added_score"])))
    elif a.cmd == "status":
        with db.session() as con:
            when = analyze.projection(con)
            for r in con.execute("SELECT * FROM sources ORDER BY id"):
                if r["status"] == "done":
                    c, w = analyze.coverage(con, r["id"]), when[r["id"]]
                    print(f"{r['id']:3d}  {_line(c)}  {r['title']}")
                    print(f"       {c['sentences']['readable']} of {c['sentences']['total']} sentences readable; priority {analyze.PRIORITY[r['priority'] or 2].lower()}; "
                          + (f"{w['new_words']} new words, all introduced by {w['introduced']}" if w["new_words"] else "nothing new left to introduce"))
                else:
                    print(f"{r['id']:3d}  [{r['status']}] {r['title']}: {r['message'] or r['stage'] or ''}")
    elif a.cmd == "score":
        dic, parser = _engine()
        if ingest.is_url(a.target) or Path(a.target).exists():
            fake = dict(id=0, origin=a.target if ingest.is_url(a.target) else str(Path(a.target).resolve()), kind=ingest.classify(a.target))
            sentences = [x["text"] for x in ingest.sentences_for(fake)[0]]
        else:
            sentences = [x["text"] for x in ingest._split_plain(a.target)]
        with db.session() as con:
            print(_line(analyze.score_text(con, dic, parser, sentences)), f" [{len(sentences)} sentences]")
    elif a.cmd == "export-anki":
        with db.session() as con:
            res = cards.export_anki(con, Path(a.out).resolve(), a.source, a.tag, a.kind, a.deck, not a.one_deck, a.mature, a.progress)
        if a.progress:
            print(f"{res['cards']} cards carry their memory, with {res['reviews']} reviews of history")
        print(f"Wrote {res['notes']} notes from {res['sources']} source(s) in {res['decks']} deck(s), {res['audio']} audio clips: {res['file']}")
    elif a.cmd == "pack":
        with db.session() as con:
            for sid in a.source or [r[0] for r in con.execute("SELECT id FROM sources WHERE status='done'")]:
                print("Wrote", cards.write_pack(con, sid))
            print("Wrote the settings and contrast cards to", cards.write_shared(con))
    elif a.cmd == "pairs":
        print(f"Loaded {dictdata.add_pairs(a.file)} collocation pairs. Process a source again to find them in it.")
    elif a.cmd == "priority":
        with db.session() as con:
            con.execute("UPDATE sources SET priority=? WHERE id=?", (a.n, a.source))
        print(f"Source {a.source}: priority {analyze.PRIORITY[a.n].lower()}.")
    elif a.cmd == "contrast":
        from . import contrast  # noqa: PLC0415
        with db.session() as con:
            for c in contrast.listing(con):
                print(f"{c['kind_label']:28s} " + ("  ".join(f"{m['lemma']}({m['reading']})" for m in c["members"])))
    elif a.cmd == "glossary":
        print(f"Loaded {dictdata.add_glossary(a.file)} terms. Reprocess a source to apply them to it.")
    elif a.cmd == "frequency":
        try:
            f = frequency.install(a.file, a.refresh)
        except Exception as e:  # noqa: BLE001
            print(f"Could not load a frequency list: {e}\nDownload the Global CSV from https://jiten.moe/frequency-dictionaries "
                  "in a browser and run: sumitsubo frequency FILE")
            return 1
        with db.session() as con:
            changed = analyze.refresh_frequency(con, dictdata.Dictionary())
        print(f"Loaded {f['ranks']} frequency ranks ({f['name']}); updated {changed} words already collected.")
    elif a.cmd == "import":
        try:
            b = (imports.read_renshuu(a.renshuu_key, sentences=a.renshuu_sentences) if a.renshuu_key else imports.read_ankiconnect() if a.anki_running
                 else imports.read_file(a.file) if a.file else None)
            if b is None:
                print("Give a file, --renshuu-key KEY or --anki-running.")
                return 1
            with db.session() as con:
                bid = imports.save_batch(con, b)
                p = imports.preview(con, bid)
                imports.configure(con, bid, modes={d: a.take for d in p["decks"]})
                for deck, d in sorted(p["decks"].items()):
                    print(f"  {deck}: {d['mature']} mature, {d['young']} young or learning, {d['new']} not started, "
                          f"{d['unusable']} without a single word, {d['history']} with review history")
                t = imports.apply(con, dictdata.Dictionary(gloss_dict=db.setting(con, "gloss_dict")), bid)
        except Exception as e:  # noqa: BLE001
            print(f"Could not import: {e}")
            return 1
        print(f"Imported ({a.take}): progress of {t['studied']} words ({t['mature']} mature) with {t['reviews']} past reviews; "
              f"{t['added']} cards brought in, {t['left_out']} left out, {t['not_found']} not in the dictionary.")
    elif a.cmd == "level":
        with db.session() as con:
            analyze.apply_level(con, a.n)
            made = analyze.rebuild_cards(con)
            ingest.cut_clips(con)
        print(f"Level set to {'N' + str(a.n) if a.n else 'nothing assumed'}. Items it covers are in review with a long interval; cards added: {made}")


if __name__ == "__main__":
    raise SystemExit(main())
