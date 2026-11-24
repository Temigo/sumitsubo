"""The learner's database: sources, sentences, what is known, and the cards."""
import sqlite3
import time
from contextlib import contextmanager

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources(
  id INTEGER PRIMARY KEY, kind TEXT, title TEXT, origin TEXT, series TEXT,
  status TEXT DEFAULT 'queued', stage TEXT, message TEXT, added REAL,
  media TEXT, text_file TEXT, n_sentences INT DEFAULT 0, added_score TEXT, options TEXT,
  priority INT DEFAULT 2,    -- 1 first, 2 normal, 3 later: the order in which new cards are introduced
  done INT DEFAULT 0, total INT DEFAULT 0,   -- how far the reading is, in sentences, while it runs
  to_reader INT DEFAULT 0, book TEXT);       -- book: a file a reader device can open; to_reader: send it at the next sync
-- Photographs of paper pages waiting to be made into a source (and, once used, kept with source_id set).
CREATE TABLE IF NOT EXISTS inbox(
  id INTEGER PRIMARY KEY, name TEXT, added REAL, ord INT,
  status TEXT DEFAULT 'waiting',   -- waiting, reading, ok, poor (left out), error
  message TEXT, layout_set TEXT DEFAULT 'auto',   -- auto, h, v: what was asked for
  layout TEXT, conf REAL, hit REAL, chars INT,    -- what the reading gave
  source_id INT, engine TEXT);                   -- engine: ndl or tesseract
CREATE TABLE IF NOT EXISTS tags(id INTEGER PRIMARY KEY, name TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS source_tags(source_id INT, tag_id INT, PRIMARY KEY(source_id, tag_id));
CREATE TABLE IF NOT EXISTS sentences(
  id INTEGER PRIMARY KEY, source_id INT, idx INT, text TEXT, start REAL, end REAL, location TEXT, clip TEXT,
  register TEXT);   -- plain, polite or keigo, from the sentence's wording
CREATE INDEX IF NOT EXISTS sentences_source ON sentences(source_id, idx);
-- An item is something to learn: a word (key "form|reading") or a kanji reading (key "字|よみ").
-- How well it is known is not stored here but in `memory` (below), as in any spaced-repetition
-- program. `band` is a summary kept up to date by memory.recompute: new (never studied), young
-- (learning, or in review with an interval under 21 days), mature (21 days or more), suspended.
-- status is 'learning' for a normal item and 'ignored' (known_by 'auto') for the short grammatical
-- expressions set aside automatically; older values are converted by memory.migrate.
CREATE TABLE IF NOT EXISTS items(
  id INTEGER PRIMARY KEY, kind TEXT, key TEXT, lemma TEXT, reading TEXT, gloss TEXT,
  level INT, common INT DEFAULT 0, freq INT, status TEXT DEFAULT 'learning', known_by TEXT,
  first_source INT, custom INT DEFAULT 0, band TEXT DEFAULT 'new', created REAL,
  register TEXT,   -- of a word: respectful, humble, polite, formal, colloquial, from the dictionary's tags
  UNIQUE(kind, key));
-- The memory state of one skill of one item, in FSRS terms. skill: word (reading a word), kanji
-- (reading a kanji), listen (hearing a word). No row means New. state: learning, relearning, review.
-- stability is in days; due and last_review are times. origin says where the state comes from:
--   assumed  from your stated level: treated as in review with a long interval, to be checked once
--   user     you pressed "I know this"
--   import   carried over from Anki, Renshuu or a list
--   review   from reviews made on a device (next milestone)
CREATE TABLE IF NOT EXISTS memory(item_id INT, skill TEXT, state TEXT, stability REAL, difficulty REAL,
  last_review REAL, due REAL, origin TEXT, PRIMARY KEY(item_id, skill));
-- What you decided by hand about an item: known, new ("learn this", whatever anything else says),
-- or ignored (suspended). It outranks imports and the level.
CREATE TABLE IF NOT EXISTS marks(item_id INTEGER PRIMARY KEY, mark TEXT, ts REAL);
-- The review log: one row per review. Imported history lands here (import_id set); device reviews
-- will be appended here too. rating: 1 again, 2 hard, 3 good, 4 easy.
CREATE TABLE IF NOT EXISTS reviews(id INTEGER PRIMARY KEY, item_id INT, skill TEXT, ts REAL, rating INT, import_id INT);
CREATE INDEX IF NOT EXISTS reviews_item ON reviews(item_id, skill, ts);
CREATE TABLE IF NOT EXISTS occ(item_id INT, sentence_id INT, source_id INT, n INT DEFAULT 1,
  surface TEXT, PRIMARY KEY(item_id, sentence_id));   -- surface: the item as written in that sentence
CREATE INDEX IF NOT EXISTS occ_source ON occ(source_id, item_id);
CREATE INDEX IF NOT EXISTS occ_sentence ON occ(sentence_id);   -- from a sentence to its words; without it a long book makes those lookups crawl
CREATE TABLE IF NOT EXISTS word_kanji(word_id INT, kanji_id INT, pos INT, PRIMARY KEY(word_id, pos));
CREATE INDEX IF NOT EXISTS word_kanji_k ON word_kanji(kanji_id);
CREATE TABLE IF NOT EXISTS unverified(source_id INT, form TEXT, n INT, PRIMARY KEY(source_id, form));
-- kind: word, listen, kanji.  rank is the release order within the source (the device releases
-- cards day by day within the budget).  guid is the card's stable identity for Anki re-export.
CREATE TABLE IF NOT EXISTS cards(
  id INTEGER PRIMARY KEY, kind TEXT, item_id INT, source_id INT, sentence_id INT, rank INT,
  guid TEXT UNIQUE, created REAL, UNIQUE(kind, item_id));
CREATE TABLE IF NOT EXISTS card_words(card_id INT, word_id INT, ord INT, PRIMARY KEY(card_id, ord));
-- The sentences a word or listening card can be shown with, best first, drawn from every source.
-- ord 0 is the card's main sentence (also in cards.sentence_id); a device rotates through them.
CREATE TABLE IF NOT EXISTS card_sentences(card_id INT, sentence_id INT, ord INT, PRIMARY KEY(card_id, ord));
CREATE INDEX IF NOT EXISTS card_sentences_s ON card_sentences(sentence_id);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
-- A collocation: a noun, a particle and the verb that goes with it (仮説 を 立てる). The item is
-- either a dictionary expression (kind word) or a pair from a pair list found in a sentence (kind
-- colloc). Its card hides the verb.
CREATE TABLE IF NOT EXISTS colloc(item_id INTEGER PRIMARY KEY, noun TEXT, particle TEXT, verb TEXT);
-- A contrast card: two or three items that are easily confused, shown together. kind: look
-- (kanji that look alike), read (one kanji, different readings), sound (words that sound the
-- same), keigo (a plain verb with its respectful and humble counterparts). key is the kind plus its members, and never changes: a new look-alike makes a new card.
CREATE TABLE IF NOT EXISTS contrasts(id INTEGER PRIMARY KEY, kind TEXT, key TEXT UNIQUE, label TEXT, guid TEXT UNIQUE, created REAL);
CREATE TABLE IF NOT EXISTS contrast_items(contrast_id INT, item_id INT, ord INT, role TEXT, PRIMARY KEY(contrast_id, ord));
CREATE INDEX IF NOT EXISTS contrast_items_i ON contrast_items(item_id);
-- Sentences a person marked as bad (garbled, misleading). Kept by text, so the mark survives
-- processing the source again. Such a sentence is never used on a card.
CREATE TABLE IF NOT EXISTS rejected(source_id INT, text TEXT, why TEXT, PRIMARY KEY(source_id, text));   -- why: set when the program, not you, set it aside
CREATE TABLE IF NOT EXISTS kept(source_id INT, text TEXT, PRIMARY KEY(source_id, text));               -- brought back by you: not set aside again
-- Words you asked for by saving them in the reader: their cards are introduced before all others (device.py).
CREATE TABLE IF NOT EXISTS wanted(item_id INTEGER PRIMARY KEY, ts REAL);
-- Progress brought in from Anki, Renshuu or a list (imports.py). A batch is one upload or fetch,
-- kept as a snapshot file; each of its decks has its own choice (mode: skip, progress, studied,
-- all). import_items records the memory state each deck brings for an item, so removing or
-- changing an import can be undone exactly.
CREATE TABLE IF NOT EXISTS import_batches(id INTEGER PRIMARY KEY, kind TEXT, label TEXT, file TEXT, mapping TEXT,
  days INT DEFAULT 21, pct INT DEFAULT 50, added REAL);
CREATE TABLE IF NOT EXISTS imports(id INTEGER PRIMARY KEY, batch_id INT, deck TEXT, mode TEXT DEFAULT 'known',
  source_id INT, stats TEXT);
CREATE TABLE IF NOT EXISTS import_items(import_id INT, item_id INT, state TEXT, stability REAL, difficulty REAL,
  last_review REAL, due REAL, PRIMARY KEY(import_id, item_id));
-- A kanji studied as a character elsewhere (a Renshuu kanji schedule). readings: the readings that
-- study covered, space-separated; empty means all. It gives its state to our kanji-reading items.
CREATE TABLE IF NOT EXISTS import_kanji(import_id INT, literal TEXT, readings TEXT, state TEXT, stability REAL,
  difficulty REAL, last_review REAL, due REAL, PRIMARY KEY(import_id, literal));
CREATE INDEX IF NOT EXISTS import_items_item ON import_items(item_id);
"""


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(config.data_dir() / "sumitsubo.sqlite", timeout=30, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=OFF")
    return con


MODEL = 2      # 2: memory states instead of a "known" flag (version 0.2.0)


def _add_columns(con, table: str, columns: dict):
    have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    for name, decl in columns.items():
        if name not in have:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def init():
    """Create what is missing. Data written by an earlier version is kept; memory.migrate converts it."""
    with session() as con:
        old = bool(con.execute("SELECT 1 FROM sqlite_master WHERE name='items'").fetchone())
        if old:                                   # columns added since the first version, before the indexes that use them
            _add_columns(con, "items", dict(band="TEXT DEFAULT 'new'", created="REAL"))
            _add_columns(con, "sources", dict(priority="INT DEFAULT 2", done="INT DEFAULT 0", total="INT DEFAULT 0",
                                              to_reader="INT DEFAULT 0", book="TEXT"))   # book: a file a reader device can open
            _add_columns(con, "occ", dict(surface="TEXT"))
            if con.execute("SELECT 1 FROM sqlite_master WHERE name='rejected'").fetchone():
                _add_columns(con, "rejected", dict(why="TEXT"))
            _add_columns(con, "items", dict(register="TEXT"))
            _add_columns(con, "sentences", dict(register="TEXT"))
            if con.execute("SELECT 1 FROM sqlite_master WHERE name='inbox'").fetchone():
                _add_columns(con, "inbox", dict(engine="TEXT"))
            if con.execute("SELECT 1 FROM sqlite_master WHERE name='contrast_items'").fetchone():
                _add_columns(con, "contrast_items", dict(role="TEXT"))
            if con.execute("SELECT 1 FROM sqlite_master WHERE name='import_items'").fetchone():
                _add_columns(con, "import_items", dict(stability="REAL", difficulty="REAL", last_review="REAL", due="REAL"))
        con.executescript(SCHEMA)
        if not old or not con.execute("SELECT 1 FROM items LIMIT 1").fetchone():
            if not con.execute("SELECT 1 FROM settings WHERE key='model'").fetchone():
                con.execute("INSERT INTO settings VALUES('model', ?)", (str(MODEL),))


@contextmanager
def session():
    con = connect()
    try:
        yield con
        con.commit()
    finally:
        con.close()


def now() -> float:
    return time.time()


def setting(con, key: str) -> str:
    r = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r[0] if r else config.DEFAULTS.get(key, "")


def set_setting(con, key: str, value):
    con.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (key, str(value)))


def set_tags(con, source_id: int, tags: str):
    con.execute("DELETE FROM source_tags WHERE source_id=?", (source_id,))
    for name in dict.fromkeys(t.strip() for t in (tags or "").replace("、", ",").split(",")):
        if name:
            con.execute("INSERT OR IGNORE INTO tags(name) VALUES(?)", (name,))
            tid = con.execute("SELECT id FROM tags WHERE name=?", (name,)).fetchone()[0]
            con.execute("INSERT OR IGNORE INTO source_tags VALUES(?,?)", (source_id, tid))


def tags_of(con, source_id: int) -> list[str]:
    return [r[0] for r in con.execute(
        "SELECT t.name FROM tags t JOIN source_tags s ON s.tag_id=t.id WHERE s.source_id=? ORDER BY t.name", (source_id,))]
