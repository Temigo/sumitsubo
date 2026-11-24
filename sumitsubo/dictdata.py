"""Dictionary layer: the one place the rest of the program asks about words and kanji.

A dictionary supplies headwords, readings, glosses, commonness and word type. JMdict (English
glosses) is the default; more dictionaries can be added in Yomitan format, and user glossaries
(tab-separated term, reading, gloss) take precedence over all of them. Every active dictionary and
glossary is pooled when the parser asks "is this string a word?".

Data, all built into <data>/dict.sqlite by `sumitsubo setup`:
  - JMdict and KANJIDIC2, from the jmdict-simplified JSON releases (or, for development, from the
    `jamdict-data` Python package with --from-jamdict)
  - JLPT vocabulary lists keyed by JMdict id (Jonathan Waller's lists, via yomitan-jlpt-vocab)
"""
import csv
import io
import json
import re
import sqlite3
import urllib.request
import zipfile
from functools import lru_cache
from pathlib import Path

from . import config, register

JLPT_URL = "https://raw.githubusercontent.com/stephenmk/yomitan-jlpt-vocab/main/original_data/n{n}.csv"
JMDICT_API = "https://api.github.com/repos/scriptin/jmdict-simplified/releases/latest"

SCHEMA = """
DROP TABLE IF EXISTS entries; DROP TABLE IF EXISTS forms; DROP TABLE IF EXISTS readings;
DROP TABLE IF EXISTS names; DROP TABLE IF EXISTS kanji; DROP TABLE IF EXISTS meta;
CREATE TABLE entries(seq INTEGER, dict TEXT, gloss TEXT, pos TEXT, common INT, jlpt INT, uk INT,
                     reg TEXT, PRIMARY KEY(seq, dict));      -- reg: respectful, humble, polite, formal, colloquial
CREATE TABLE forms(form TEXT, seq INTEGER, is_kana INT, common INT, dict TEXT);
CREATE TABLE readings(term TEXT, reading TEXT, seq INTEGER);
CREATE TABLE names(form TEXT PRIMARY KEY);
CREATE TABLE kanji(literal TEXT PRIMARY KEY, ons TEXT, kuns TEXT, meaning TEXT, jlpt INT, grade INT, freq INT);
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS custom(term TEXT PRIMARY KEY, reading TEXT, gloss TEXT, source TEXT);
CREATE TABLE IF NOT EXISTS freq(term TEXT PRIMARY KEY, rank INT, source TEXT);
"""
INDEXES = """
CREATE INDEX IF NOT EXISTS forms_form ON forms(form);
CREATE INDEX IF NOT EXISTS readings_term ON readings(term);
CREATE INDEX IF NOT EXISTS readings_reading ON readings(reading);
"""
COMMON_PRI = {"news1", "ichi1", "spec1", "spec2", "gai1"}
KATA = str.maketrans({chr(c): chr(c - 0x60) for c in range(0x30A1, 0x30F7)})
EXPRESSION = re.compile(r"^([一-鿿々]{1,4})(を|に|が|と|で)([一-鿿々][一-鿿々ぁ-ん]{1,6})$")     # 仮説 を 立てる
KANJI_RE = re.compile(r"[一-鿿㐀-䶿豈-﫿]")


def hira(s: str) -> str:
    return (s or "").translate(KATA)


def is_kanji(ch: str) -> bool:
    return bool(KANJI_RE.fullmatch(ch))


def kanji_of(s: str) -> list[str]:
    return KANJI_RE.findall(s or "")


def db_path() -> Path:
    return config.data_dir() / "dict.sqlite"


# ---- building ----------------------------------------------------------------------------------
def _fetch(url: str, dest: Path) -> Path:
    if not dest.exists():
        print(f"Downloading {url}")
        req = urllib.request.Request(url, headers={"User-Agent": "sumitsubo"})
        with urllib.request.urlopen(req, timeout=120) as r:
            dest.write_bytes(r.read())
    return dest


def _find_release(dl: Path, pattern: str, given: str | None) -> Path:
    if given:
        return Path(given)
    local = sorted(p for p in dl.iterdir() if re.fullmatch(pattern, p.name))
    if local:
        return local[-1]
    with urllib.request.urlopen(urllib.request.Request(JMDICT_API, headers={"User-Agent": "sumitsubo"}), timeout=60) as r:
        rel = json.load(r)
    for a in rel["assets"]:
        if re.fullmatch(pattern, a["name"]):
            return _fetch(a["browser_download_url"], dl / a["name"])
    raise RuntimeError(f"No file matching {pattern} in the latest jmdict-simplified release")


def _load_json(path: Path):
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as z:
            name = next(n for n in z.namelist() if n.endswith(".json"))
            return json.load(io.TextIOWrapper(z.open(name), encoding="utf-8"))
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _gloss(senses: list[list[str]], fields: list[list[str]] | None = None) -> str:
    out = []
    for i, s in enumerate(senses[:3]):
        g = "; ".join(s[:3])
        if g:
            f = ",".join((fields or [])[i]) if fields and i < len(fields) else ""
            out.append(f"[{f}] {g}" if f else g)
    return " | ".join(out)


def _jmdict_simplified(path: Path):
    """Yield normalized entries from a jmdict-simplified JSON file."""
    for w in _load_json(path)["words"]:
        kanji = [(k["text"], bool(k.get("common"))) for k in w.get("kanji", [])]
        kana = []
        for k in w.get("kana", []):
            applies = k.get("appliesToKanji", ["*"])
            kana.append((k["text"], bool(k.get("common")), None if "*" in applies else list(applies)))
        senses = w.get("sense", [])
        yield dict(seq=int(w["id"]), kanji=kanji, kana=kana,
                   gloss=_gloss([[g["text"] for g in s.get("gloss", [])] for s in senses],
                                [s.get("field", []) for s in senses]),
                   pos=",".join(sorted({p for s in senses for p in s.get("partOfSpeech", [])})),
                   uk=any("uk" in s.get("misc", []) for s in senses),
                   reg=register.of_tags(m for s in senses[:1] for m in s.get("misc", [])))    # the main sense only


def _kanjidic_simplified(path: Path):
    for c in _load_json(path)["characters"]:
        ons, kuns, meanings = [], [], []
        for g in (c.get("readingMeaning") or {}).get("groups", []):
            for r in g.get("readings", []):
                (ons if r.get("type") == "ja_on" else kuns if r.get("type") == "ja_kun" else []).append(r["value"])
            meanings += [m["value"] for m in g.get("meanings", []) if m.get("lang", "en") == "en"]
        misc = c.get("misc", {})
        yield (c["literal"], " ".join(ons), " ".join(kuns), "; ".join(meanings[:4]),
               misc.get("jlptLevel"), misc.get("grade"), misc.get("frequency"))


def _jamdict(path: str):
    """Yield (entries, kanji rows, names) from the jamdict-data package's database (development)."""
    src = sqlite3.connect(path)

    def grouped(sql):
        d: dict = {}
        for k, v in src.execute(sql):
            d.setdefault(k, []).append(v)
        return d
    kj_pri, kn_pri, kn_res = grouped("SELECT kid, text FROM KJP"), grouped("SELECT kid, text FROM KNP"), grouped("SELECT kid, text FROM KNR")
    kanji, kana = {}, {}
    for kid, seq, text in src.execute("SELECT ID, idseq, text FROM Kanji"):
        kanji.setdefault(seq, []).append((text, bool(COMMON_PRI & set(kj_pri.get(kid, [])))))
    for kid, seq, text, nokanji in src.execute("SELECT ID, idseq, text, nokanji FROM Kana"):
        applies = [] if nokanji else kn_res.get(kid)
        kana.setdefault(seq, []).append((text, bool(COMMON_PRI & set(kn_pri.get(kid, []))), applies))
    gl, pos, misc = grouped("SELECT sid, text FROM SenseGloss WHERE lang='eng' OR lang=''"), grouped("SELECT sid, text FROM pos"), grouped("SELECT sid, text FROM misc")
    senses = grouped("SELECT idseq, ID FROM Sense")

    def entries():
        for (seq,) in src.execute("SELECT idseq FROM Entry"):
            sids = senses.get(seq, [])
            yield dict(seq=seq, kanji=kanji.get(seq, []), kana=kana.get(seq, []),
                       gloss=_gloss([gl.get(s, []) for s in sids]),
                       pos=",".join(sorted({p for s in sids for p in pos.get(s, [])})),
                       uk=any("word usually written using kana alone" in misc.get(s, []) for s in sids),
                       reg=register.of_tags(m for s in sids[:1] for m in misc.get(s, [])))

    def kanji_rows():
        rd, mn = {}, {}
        for cid, typ, val in src.execute("SELECT g.cid, r.r_type, r.value FROM reading r JOIN rm_group g ON g.ID=r.gid"):
            rd.setdefault(cid, {}).setdefault(typ, []).append(val)
        for cid, val in src.execute("SELECT g.cid, m.value FROM meaning m JOIN rm_group g ON g.ID=m.gid WHERE m.m_lang='' OR m.m_lang='en'"):
            mn.setdefault(cid, []).append(val)
        num = lambda v: int(v) if str(v or "").isdigit() else None
        for cid, lit, grade, freq, jlpt in src.execute("SELECT ID, literal, grade, freq, jlpt FROM character"):
            r = rd.get(cid, {})
            yield (lit, " ".join(r.get("ja_on", [])), " ".join(r.get("ja_kun", [])), "; ".join(mn.get(cid, [])[:4]),
                   num(jlpt), num(grade), num(freq))
    names = (r[0] for r in src.execute("SELECT text FROM NEKanji UNION SELECT text FROM NEKana"))
    return entries(), kanji_rows(), names


def build(jmdict: str | None = None, kanjidic: str | None = None, jlpt_dir: str | None = None,
          from_jamdict: bool = False, quiet: bool = False) -> dict:
    """Download what is missing and build <data>/dict.sqlite. Returns counts."""
    data, dl = config.data_dir(), config.data_dir() / "downloads"
    jlpt: dict[int, int] = {}
    for n in (1, 2, 3, 4, 5):
        f = Path(jlpt_dir) / f"n{n}.csv" if jlpt_dir else None
        if not (f and f.exists()):
            try:
                f = _fetch(JLPT_URL.format(n=n), dl / f"n{n}.csv")
            except Exception as e:  # noqa: BLE001 - JLPT lists are optional; seeding is then by kanji only
                print(f"JLPT list N{n} unavailable ({e}); continuing without it.")
                continue
        for row in csv.DictReader(open(f, encoding="utf-8")):
            try:
                seq = int(row["jmdict_seq"])
            except (KeyError, ValueError, TypeError):
                continue
            jlpt[seq] = max(jlpt.get(seq, 0), n)   # keep the easiest level
    if from_jamdict:
        import jamdict_data  # noqa: PLC0415 - optional development dependency
        entries, kanji_rows, names = _jamdict(jamdict_data.JAMDICT_DB_PATH)
        version = "jamdict-data"
    else:
        jm = _find_release(dl, r"jmdict-eng-\d[^-]*\.json\.zip", jmdict)
        entries, names, version = _jmdict_simplified(jm), iter(()), jm.name
        try:
            kanji_rows = _kanjidic_simplified(_find_release(dl, r"kanjidic2-en-\d[^-]*\.json\.zip", kanjidic))
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"KANJIDIC2 is required for kanji cards and could not be loaded: {e}") from e

    con = sqlite3.connect(db_path())
    con.executescript(SCHEMA)
    n = 0
    E, F, R = [], [], []
    for e in entries:
        common = any(c for _, c in e["kanji"]) or any(c for _, c, _ in e["kana"])
        E.append((e["seq"], "jmdict-en", e["gloss"], e["pos"], int(common), jlpt.get(e["seq"]), int(e["uk"]), e.get("reg")))
        F += [(k, e["seq"], 0, int(c), "jmdict-en") for k, c in e["kanji"]]
        F += [(k, e["seq"], 1, int(c), "jmdict-en") for k, c, _ in e["kana"]]
        for kana, _, applies in e["kana"]:
            for k, _ in e["kanji"]:
                if applies is None or k in applies:
                    R.append((k, hira(kana), e["seq"]))
        n += 1
    con.executemany("INSERT OR IGNORE INTO entries VALUES(?,?,?,?,?,?,?,?)", E)
    con.executemany("INSERT INTO forms VALUES(?,?,?,?,?)", F)
    con.executemany("INSERT INTO readings VALUES(?,?,?)", R)
    con.executemany("INSERT OR IGNORE INTO names VALUES(?)", ((x,) for x in names))
    nk = con.executemany("INSERT OR IGNORE INTO kanji VALUES(?,?,?,?,?,?,?)", kanji_rows).rowcount
    con.executescript(INDEXES)
    con.execute("INSERT INTO meta VALUES('version', ?)", (version,))
    con.commit()
    con.close()
    for g in sorted((data / "glossaries").glob("*.tsv")):
        add_glossary(g, copy=False)
    out = dict(entries=n, jlpt=len(jlpt), kanji=nk)
    if not quiet:
        print(f"Dictionary ready: {n} entries, {len(jlpt)} with a JLPT level, {nk} kanji.")
    return out


def add_glossary(path, copy: bool = True) -> int:
    """Load a tab-separated glossary: term <TAB> reading <TAB> gloss (reading may be empty)."""
    path = Path(path)
    if copy:
        dest = config.data_dir() / "glossaries" / path.name
        if dest.resolve() != path.resolve():
            dest.write_bytes(path.read_bytes())
    con = sqlite3.connect(db_path())
    con.execute("CREATE TABLE IF NOT EXISTS custom(term TEXT PRIMARY KEY, reading TEXT, gloss TEXT, source TEXT)")
    n = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        p = [x.strip() for x in line.split("\t")]
        if not p or not p[0] or p[0].startswith("#"):
            continue
        p += [""] * (3 - len(p))
        con.execute("INSERT OR REPLACE INTO custom VALUES(?,?,?,?)", (p[0], hira(p[1]), p[2], path.name))
        n += 1
    con.commit()
    con.close()
    return n


def _structured_text(node) -> str:
    """Plain text of a Yomitan glossary item (string, or structured-content tree)."""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return " ".join(filter(None, (_structured_text(x) for x in node)))
    if isinstance(node, dict):
        if node.get("type") == "image":
            return ""
        return _structured_text(node.get("content") or node.get("text") or "")
    return ""


def add_yomitan(path, name: str | None = None) -> int:
    """Add a dictionary in Yomitan format (a zip of term_bank_*.json). Its glosses become selectable."""
    path = Path(path)
    con = sqlite3.connect(db_path())
    with zipfile.ZipFile(path) as z:
        try:
            name = name or json.load(z.open("index.json")).get("title") or path.stem
        except KeyError:
            name = name or path.stem
        con.execute("DELETE FROM entries WHERE dict=?", (name,))
        con.execute("DELETE FROM forms WHERE dict=?", (name,))
        base = 10_000_000_000 + (abs(hash(name)) % 1000) * 10_000_000
        n = 0
        for member in sorted(m for m in z.namelist() if re.fullmatch(r"term_bank_\d+\.json", m)):
            for row in json.load(z.open(member)):
                term, reading, gloss = row[0], row[1] or row[0], row[5]
                text = "; ".join(filter(None, (_structured_text(g).strip() for g in gloss)))[:400]
                seq = base + n
                con.execute("INSERT OR IGNORE INTO entries(seq, dict, gloss, pos, common, jlpt, uk) VALUES(?,?,?,?,?,?,?)", (seq, name, text, row[3] or "", 0, None, 0))
                con.execute("INSERT INTO forms VALUES(?,?,?,?,?)", (term, seq, int(term == reading), 0, name))
                if reading != term:
                    con.execute("INSERT INTO forms VALUES(?,?,?,?,?)", (reading, seq, 1, 0, name))
                    con.execute("INSERT INTO readings VALUES(?,?,?)", (term, hira(reading), seq))
                n += 1
    con.commit()
    con.close()
    return n


def add_frequency(rows, source: str) -> int:
    """Replace the frequency list with (term, rank) pairs; rank 1 = most frequent. One list at a time."""
    con = sqlite3.connect(db_path())
    con.execute("CREATE TABLE IF NOT EXISTS freq(term TEXT PRIMARY KEY, rank INT, source TEXT)")
    con.execute("DELETE FROM freq")
    n = con.executemany("INSERT OR IGNORE INTO freq VALUES(?,?,?)",
                        ((t, int(r), source) for t, r in sorted(rows, key=lambda x: int(x[1])))).rowcount
    con.commit()
    con.close()
    return n


# ---- look-alike kanji ----------------------------------------------------------------------------------
_similar: dict | None = None


def similar_kanji() -> dict[str, list[str]]:
    """kanji -> the kanji that look like it, most alike first. From the list in data/similar-kanji.txt
    (see data/README.md), read both ways: if 末 is listed under 未, then 未 also counts for 末."""
    global _similar
    if _similar is None:
        out: dict = {}
        f = Path(__file__).parent / "data" / "similar-kanji.txt"
        if f.exists():
            lines = [[x for x in line.strip().split("/") if x] for line in f.read_text(encoding="utf-8").splitlines()]
            for row in lines:
                if len(row) > 1:
                    out.setdefault(row[0], [])
                    out[row[0]] += [x for x in row[1:] if x not in out[row[0]]]
            for row in lines:                    # the reverse direction, after the direct ones
                for x in row[1:]:
                    if row and row[0] not in out.setdefault(x, []):
                        out[x].append(row[0])
        _similar = out
    return _similar


# ---- collocation pair lists -----------------------------------------------------------------------------
PARTICLES = ("を", "に", "が", "と", "で")


def add_pairs(path, name: str | None = None) -> int:
    """Load a collocation pair list: a tab-separated file with noun, particle, verb and, optionally,
    a strength (any number, higher is stronger) and a meaning. One list per name; loading it again
    replaces it. This is the one format every source of pairs is converted to."""
    path = Path(path)
    name = name or path.stem
    con = sqlite3.connect(db_path())
    con.execute("CREATE TABLE IF NOT EXISTS pairs(noun TEXT, particle TEXT, verb TEXT, score REAL, gloss TEXT, source TEXT, PRIMARY KEY(noun, particle, verb))")
    con.execute("DELETE FROM pairs WHERE source=?", (name,))
    n = 0
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        p = [x.strip() for x in line.split("\t")]
        if len(p) < 3 or p[0].startswith("#") or p[1] not in PARTICLES or not p[0] or not p[2]:
            continue
        try:
            score = float(p[3]) if len(p) > 3 and p[3] else 0.0
        except ValueError:
            score = 0.0
        n += con.execute("INSERT OR REPLACE INTO pairs VALUES(?,?,?,?,?,?)", (p[0], p[1], p[2], score, p[4] if len(p) > 4 else "", name)).rowcount
    con.commit()
    con.close()
    return n


# ---- reading -----------------------------------------------------------------------------------
class Dictionary:
    """Read access. One instance per process; lookups are cached."""

    def __init__(self, path: Path | None = None, gloss_dict: str = "jmdict-en"):
        p = Path(path or db_path())
        if not p.exists():
            raise FileNotFoundError("No dictionary yet. Run `sumitsubo setup` first.")
        self.con = sqlite3.connect(p, check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self.gloss_dict = gloss_dict
        self._cache: dict = {}
        self._expr: dict = {}
        self._pairs: dict | None = None

    def _in(self, sql: str, items) -> list:
        items, out = list(dict.fromkeys(items)), []
        for i in range(0, len(items), 500):
            chunk = items[i:i + 500]
            out += self.con.execute(sql.format(",".join("?" * len(chunk))), chunk).fetchall()
        return out

    # The five functions Anki Miner's parser asks for. See parser.py.
    def terms_exist(self, terms: list[str]) -> set[str]:
        found = {r[0] for r in self._in("SELECT DISTINCT form FROM forms WHERE form IN ({})", terms)}
        return found | {r[0] for r in self._in("SELECT term FROM custom WHERE term IN ({})", terms)}

    def names_exist(self, terms: list[str]) -> set[str]:
        return {r[0] for r in self._in("SELECT form FROM names WHERE form IN ({})", terms)}

    def readings(self, terms: list[str]) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for term, reading in self._in("SELECT term, reading FROM readings WHERE term IN ({})", terms):
            if reading not in out.setdefault(term, []):
                out[term].append(reading)
        for term, reading in self._in("SELECT term, reading FROM custom WHERE reading<>'' AND term IN ({})", terms):
            out.setdefault(term, []).insert(0, reading)
        return out

    def same_entry(self, form: str, a: str, b: str) -> bool:
        """Whether two readings of a written form belong to one dictionary entry: 私 as わたし and as
        わたくし is one word said two ways, 今日 as きょう and as こんにち is two words."""
        by_seq: dict = {}
        for r in self.con.execute("SELECT seq, reading FROM readings WHERE term=?", (form,)):
            by_seq.setdefault(r[0], set()).add(hira(r[1]))
        return any(hira(a) in s and hira(b) in s for s in by_seq.values())

    def kana_attest(self, terms: list[str]) -> dict[str, bool]:
        found = self.terms_exist(terms) | {r[0] for r in self._in("SELECT DISTINCT reading FROM readings WHERE reading IN ({})", terms)}
        return {t: t in found for t in terms}

    def common(self, terms: list[str]) -> dict[str, bool]:
        yes = {r[0] for r in self._in("SELECT DISTINCT form FROM forms WHERE common=1 AND form IN ({})", terms)}
        return {t: t in yes for t in terms}

    # For our own code.
    def lookup(self, form: str, reading: str | None = None) -> dict | None:
        """Best entry for a dictionary form: dict(seq, reading, gloss, jlpt, common, pos, custom)."""
        key = (form, reading)
        if key in self._cache:
            return self._cache[key]
        res = None
        c = self.con.execute("SELECT * FROM custom WHERE term=?", (form,)).fetchone()
        if c:
            res = dict(seq=None, reading=c["reading"] or hira(reading or ""), gloss=c["gloss"], jlpt=None,
                       common=0, pos="", custom=True)
        else:
            rd = hira(reading or "")
            best, best_score = None, -1.0
            for r in self.con.execute("SELECT e.*, f.is_kana FROM forms f JOIN entries e ON e.seq=f.seq AND e.dict=f.dict "
                                      "WHERE f.form=?", (form,)):
                rs = [x[0] for x in self.con.execute("SELECT reading FROM readings WHERE term=? AND seq=?", (form, r["seq"]))]
                if r["is_kana"]:
                    rs = [hira(form)]
                score = (8 if rd and rd in rs else 0) + 2 * (r["common"] or 0) + (1 if r["jlpt"] else 0) \
                    + (4 if r["dict"] == self.gloss_dict else 0) + (0.5 if not r["is_kana"] or r["uk"] else 0)
                if score > best_score:
                    best, best_score = (r, rs), score
            if best:
                r, rs = best
                res = dict(seq=r["seq"], reading=rd if rd in rs else (rs[0] if rs else rd), gloss=r["gloss"],
                           jlpt=r["jlpt"], common=r["common"], pos=r["pos"], custom=False,
                           reg=r["reg"] if "reg" in r.keys() else None)      # absent in a dictionary built before 0.3.1
        self._cache[key] = res
        return res

    @lru_cache(maxsize=8192)  # noqa: B019 - one long-lived instance
    def kanji(self, literal: str) -> dict | None:
        r = self.con.execute("SELECT * FROM kanji WHERE literal=?", (literal,)).fetchone()
        return dict(r) if r else None

    def has_registers(self) -> bool:
        """Whether this dictionary was built with register labels (0.3.1 or later)."""
        return "reg" in {r[1] for r in self.con.execute("PRAGMA table_info(entries)")}

    def expression(self, form: str) -> tuple[str, str, str] | None:
        """(noun, particle, verb) if this dictionary entry is a verb expression of that shape, such as
        仮説を立てる. Both halves must be dictionary words themselves."""
        if form in self._expr:
            return self._expr[form]
        res = None
        m = EXPRESSION.match(form)
        if m:
            e = self.lookup(form)
            pos = (e or {}).get("pos") or ""
            if e and not e.get("custom") and "expression" in pos and "verb" in pos and self.terms_exist([m.group(1), m.group(3)]) >= {m.group(1), m.group(3)}:
                res = (m.group(1), m.group(2), m.group(3))
        self._expr[form] = res
        return res

    def pairs(self) -> dict:
        """(noun, particle, verb) -> meaning, from every pair list loaded."""
        if self._pairs is None:
            try:
                self._pairs = {(r[0], r[1], r[2]): r[3] for r in self.con.execute("SELECT noun, particle, verb, gloss FROM pairs")}
            except sqlite3.OperationalError:
                self._pairs = {}
        return self._pairs

    def pair_lists(self) -> list:
        try:
            return [tuple(r) for r in self.con.execute("SELECT source, COUNT(*) FROM pairs GROUP BY source ORDER BY source")]
        except sqlite3.OperationalError:
            return []

    def usually_kana(self, kana: str, seq: int) -> bool:
        """Whether this entry is usually written in kana and has that kana form (沢山 / たくさん)."""
        r = self.con.execute("SELECT 1 FROM entries e JOIN forms f ON f.seq=e.seq AND f.dict=e.dict "
                             "WHERE e.seq=? AND e.uk=1 AND f.form=?", (seq, kana)).fetchone()
        return bool(r)

    def freq_rank(self, term: str) -> int | None:
        r = self.con.execute("SELECT MIN(rank) FROM freq WHERE term=?", (term,)).fetchone()
        return r[0] if r else None

    def dictionaries(self) -> list[str]:
        return [r[0] for r in self.con.execute("SELECT DISTINCT dict FROM entries ORDER BY dict")]
