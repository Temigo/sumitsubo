"""Books from Aozora Bunko (aozora.gr.jp), the library of Japanese works out of copyright.

A work's page ("図書カード") offers a text file in a zip and usually an XHTML page. Both are in
Shift_JIS and use Aozora's own notation: readings as 漢字《かんじ》 (or ｜ before the first
character they belong to), notes as ［＃…］, and characters outside Shift_JIS described in a note
after ※. Neither is good to read as it is, so this module turns a work into

  * sentences for the analysis, each with the readings the edition gives, and
  * a small EPUB (vertical, with furigana, one file per chapter) for a reader device.

The .ebk file some pages offer is for an old viewer program and is not read.
"""
import html
import io
import re
import urllib.parse
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

HOST = "aozora.gr.jp"
LIMIT = 40 * 1024 * 1024        # no single work comes near this; a guard against a wrong link

_KANJI = "㐀-䶿一-鿿豈-﫿々〆〇ヶ仝"
_RUBY = re.compile(rf"｜([^｜《》]+?)《([^《》]+)》|([{_KANJI}]+|[ァ-ヴー]+|[A-Za-zＡ-Ｚａ-ｚ]+|[ぁ-ゖ]+)《([^《》]+)》")
_NOTE = re.compile(r"［＃(.*?)］")
_GAIJI = re.compile(r"※［＃(.*?)］")
_HEAD_AFTER = re.compile(r"［＃「(.+?)」は(?:窓|同行)?(大|中|小)見出し］")
_HEAD_OPEN = re.compile(r"［＃(?:窓|同行)?(大|中|小)見出し］")
_BREAK = re.compile(r"［＃改(?:ページ|丁|段|見開き)］")
_EM_AFTER = re.compile(r"［＃「([^」]+)」に(?:白ゴマ|丸|白丸|黒三角|白三角|二重丸|蛇の目|ばつ)?傍点］")
_EM_OPEN, _EM_CLOSE = re.compile(r"［＃(?:白ゴマ|丸|白丸|黒三角|白三角|二重丸|蛇の目|ばつ)?傍点］"), re.compile(r"［＃(?:白ゴマ|丸|白丸|黒三角|白三角|二重丸|蛇の目|ばつ)?傍点終わり］")
_INDENT_LINE = re.compile(r"^［＃([０-９0-9]+)字下げ］")
_INDENT_OPEN = re.compile(r"［＃ここから([０-９0-9]+)字下げ(?:、[^］]*)?］")
_INDENT_CLOSE = re.compile(r"［＃ここで字下げ終わり］")
_END_LINE = re.compile(r"［＃(?:地付き|地から[０-９0-9]+字上げ)］")
_END_OPEN, _END_CLOSE = re.compile(r"［＃ここから(?:地付き|地から[０-９0-9]+字上げ)］"), re.compile(r"［＃ここで(?:地付き|字上げ)終わり］")
_IMAGE = re.compile(r"［＃[^］]*?（([A-Za-z0-9_.\-]+\.(?:png|jpe?g))(?:、[^）]*)?）入る］", re.I)
MAX_IMAGES, MAX_IMAGE = 60, 3 * 1024 * 1024
_LEVEL = {"大": 1, "中": 2, "小": 3}


EM0, EM1 = "\ue000", "\ue001"     # where emphasis dots begin and end, inside the text of a run
# Every kind of marking inside a line: name -> (begin mark, end mark, opening tag, closing tag, how the notes name it)
_SIDE = "(?:白ゴマ|丸|白丸|黒三角|白三角|二重丸|蛇の目|ばつ)?"
_N = "[０-９0-9]+段階"
MARKS = {
    "dots": (EM0, EM1, '<em class="sesame">', "</em>", f"{_SIDE}傍点", "に"),
    "line": ("\ue002", "\ue003", '<span class="uline">', "</span>", "(?:二重|鎖|破|波)?傍線", "に"),
    "bold": ("\ue004", "\ue005", "<b>", "</b>", "太字", "は"),
    "ital": ("\ue006", "\ue007", "<i>", "</i>", "斜体", "は"),
    "big": ("\ue008", "\ue009", '<span class="big">', "</span>", f"{_N}大きな文字", "は"),
    "small": ("\ue00a", "\ue00b", '<span class="small">', "</span>", f"{_N}小さな文字", "は"),
}
_MARK = re.compile("[\ue000-\ue00f]")
_OPENS = {v[0]: k for k, v in MARKS.items()}
_CLOSES = {v[1]: k for k, v in MARKS.items()}
# The same for a whole passage: ［＃ここから…］ to ［＃ここで…終わり］, as a class on its paragraphs.
_PASSAGE = (("bold", "太字"), ("ital", "斜体"), ("big", f"{_N}大きな文字"), ("small", f"{_N}小さな文字"), ("frame", "罫囲み"))


@dataclass
class Block:
    kind: str                                   # p, h1, h2, h3, break, img (runs hold the file name)
    runs: list = field(default_factory=list)    # (text, reading or None)
    indent: int = 0                             # whole characters the block is set in from the top of the column
    end: bool = False                           # set at the foot of the column (a signature, a date)
    cls: tuple = ()                             # bold, ital, big, small, frame: for a whole passage

    @property
    def text(self) -> str:
        return _MARK.sub("", "".join(t for t, _ in self.runs))


@dataclass
class Book:
    title: str = ""
    author: str = ""
    blocks: list = field(default_factory=list)
    colophon: list = field(default_factory=list)    # the edition and the volunteers, as Aozora asks to be kept with the text
    images: dict = field(default_factory=dict)      # file name -> bytes, for the pictures the text places


def is_aozora(url: str) -> bool:
    host = urllib.parse.urlparse(url or "").netloc.lower()
    return host == HOST or host.endswith("." + HOST)


# ---- fetching ------------------------------------------------------------------------------------
def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Sumitsubo)"})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = r.read(LIMIT + 1)
    if len(data) > LIMIT:
        raise RuntimeError("That file is too large to be a book from Aozora Bunko.")
    return data


def links(card_html: str, card_url: str) -> list[str]:
    """The files a work's page offers that can be read, best first: the text zip, then the XHTML."""
    found = [urllib.parse.urljoin(card_url, h) for h in re.findall(r'href="([^"]*?/files/[^"]+)"', card_html)]
    found = [u for u in found if is_aozora(u)]
    zips = [u for u in found if u.lower().endswith(".zip")]
    pages = [u for u in found if u.lower().endswith((".html", ".htm"))]
    return sorted(zips, key=lambda u: "ruby" not in u) + pages


def fetch(url: str, get=None) -> Book:
    """A work from its page on Aozora Bunko, or from a direct link to its zip or XHTML file."""
    get = get or _get
    path = urllib.parse.urlparse(url).path.lower()
    if "/files/" in path:
        todo = [url]
    else:
        todo = links(decode(get(url)), url)
        if not todo:
            raise RuntimeError("No text or XHTML file was found on that page. Paste the link of the work's own page "
                               "on Aozora Bunko (the one that lists its files), or of the zip file itself.")
    data = get(todo[0])
    book = read(data, todo[0])
    for b in book.blocks:                           # the pictures the text places, from beside the text file
        name = b.runs[0][0] if b.kind == "img" else None
        if name and name not in book.images and len(book.images) < MAX_IMAGES:
            try:
                img = get(urllib.parse.urljoin(todo[0], name))
            except Exception:  # noqa: BLE001 - a missing picture must not cost the book
                continue
            if len(img) <= MAX_IMAGE and (img[:8] == b"\x89PNG\r\n\x1a\n" or img[:2] == b"\xff\xd8"):
                book.images[name] = img
    book.blocks = [b for b in book.blocks if b.kind != "img" or b.runs[0][0] in book.images]
    return book


def read(data: bytes, name: str = "") -> Book:
    """A book from the bytes of a zip, a text file or an XHTML page."""
    if data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as z:        # read in memory; nothing is unpacked to disk
            texts = [i for i in z.infolist() if i.filename.lower().endswith(".txt") and i.file_size <= LIMIT]
            if not texts:
                raise RuntimeError("That zip file holds no text file.")
            data, name = z.read(max(texts, key=lambda i: i.file_size)), ".txt"
    text = decode(data)
    return parse_html(text) if name.lower().endswith((".html", ".htm")) or "<html" in text[:2000].lower() else parse_text(text)


def decode(data: bytes) -> str:
    if data[:3] == b"\xef\xbb\xbf":
        return data[3:].decode("utf-8", "replace")
    head = data[:2000].lower()
    if b"utf-8" in head and b"charset" in head:
        return data.decode("utf-8", "replace")
    try:
        return data.decode("cp932")
    except UnicodeDecodeError:
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return data.decode("cp932", "replace")


# ---- the notation --------------------------------------------------------------------------------
def _gaiji(note: str) -> str:
    """The character a note describes: 「…」、第3水準1-85-25 (JIS X 0213 plane-row-cell) or U+XXXX."""
    m = re.search(r"U\+([0-9A-Fa-f]{4,6})", note)
    if m:
        return chr(int(m.group(1), 16))
    m = re.search(r"(?:第[3-4３４]水準|JIS X 0213)?\s*([12])-(\d{1,2})-(\d{1,2})", note)
    if m:
        plane, row, cell = (int(g) for g in m.groups())
        try:
            raw = bytes(([0x8F] if plane == 2 else []) + [0xA0 + row, 0xA0 + cell])
            ch = raw.decode("euc_jis_2004")
            if len(ch) >= 1:
                return ch
        except (UnicodeDecodeError, ValueError):
            pass
    return "※"


def _runs(line: str) -> list:
    out, pos = [], 0
    for m in _RUBY.finditer(line):
        if m.start() > pos:
            out.append((line[pos:m.start()], None))
        out.append((m.group(1) or m.group(3), m.group(2) or m.group(4)))
        pos = m.end()
    if pos < len(line):
        out.append((line[pos:], None))
    return [(t.replace("｜", ""), r) for t, r in out if t]


def _number(s: str) -> int:
    return min(12, int(s.translate(str.maketrans("０１２３４５６７８９", "0123456789"))))


def _emphasis(line: str) -> str:
    """Emphasis dots, side lines, bold, italics and larger or smaller type: written after the words
    they apply to (［＃「X」に傍点］, ［＃「X」は太字］) or around them, turned into marks in the text."""
    for o, c, _, _, name, particle in MARKS.values():
        after = re.compile(rf"［＃「([^」]+)」{particle}{name}］")
        while True:
            m = after.search(line)
            if not m:
                break
            head, tail = line[:m.start()], line[m.end():]
            at = head.rfind(m.group(1))
            if at >= 0:
                head = head[:at] + o + m.group(1) + c + head[at + len(m.group(1)):]
            line = head + tail
        line = re.sub(rf"［＃{name.replace(_N, f'(?:{_N})?')}終わり］", c, line)
        line = re.sub(rf"［＃{name}］", o, line)
    return line


def _blocks(lines: list[str]) -> list[Block]:
    out, open_level, indent, at_end, passage = [], 0, 0, False, []
    for line in lines:
        line = _GAIJI.sub(lambda m: _gaiji(m.group(1)), line)
        line = line.replace("／″＼", "〴〵").replace("／＼", "〳〵")
        if _BREAK.search(line):
            out.append(Block("break"))
        for m in _IMAGE.finditer(line):
            out.append(Block("img", [(m.group(1), None)]))
        m = _INDENT_OPEN.search(line)
        if m:
            indent = _number(m.group(1))
        if _INDENT_CLOSE.search(line):
            indent = 0
        if _END_OPEN.search(line):
            at_end = True
        if _END_CLOSE.search(line):
            at_end = False
        for key, name in _PASSAGE:
            if re.search(rf"［＃ここから{name}］", line) and key not in passage:
                passage.append(key)
            if re.search(rf"［＃ここで{name.replace(_N, f'(?:{_N})?')}終わり］", line) and key in passage:
                passage.remove(key)
        m = _INDENT_LINE.search(line.lstrip("\u3000 "))
        own, foot = (_number(m.group(1)) if m else indent), at_end or bool(_END_LINE.search(line))
        line = _emphasis(line)
        level = 0
        m = _HEAD_AFTER.search(line)
        if m:
            level = _LEVEL[m.group(2)]
        m = _HEAD_OPEN.search(line)
        if m:
            level = open_level = _LEVEL[m.group(1)]
        elif open_level:
            level = open_level
        if "見出し終わり］" in line:
            open_level = 0
        line = _NOTE.sub("", line).strip().strip("　")
        if not _MARK.sub("", line):
            continue
        out.append(Block(f"h{level}" if level else "p", _runs(line), 0 if level else own, foot and not level, () if level else tuple(passage)))
    return out


def parse_text(text: str) -> Book:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    head = []
    while lines and lines[0].strip():
        head.append(lines.pop(0).strip())
    book = Book(title=_plain(head[0]) if head else "", author=_plain(head[-1]) if len(head) > 1 else "")
    rules = [i for i, s in enumerate(lines) if re.fullmatch(r"-{20,}", s.strip())]
    if len(rules) >= 2 and any("記号について" in s for s in lines[rules[0]:rules[1]]):
        del lines[rules[0]:rules[1] + 1]            # the box that explains the notation
    for i, s in enumerate(lines):
        if s.startswith(("底本：", "底本:")):
            book.colophon = [x.strip() for x in lines[i:] if x.strip()]
            lines = lines[:i]
            break
    book.blocks = _blocks(lines)
    return book


def _plain(s: str) -> str:
    return _NOTE.sub("", re.sub(r"《[^《》]*》", "", s)).replace("｜", "").strip()


def parse_html(page: str) -> Book:
    """The XHTML edition, by writing it back in the text notation and reading that."""
    def inner(pattern):
        m = re.search(pattern, page, re.S | re.I)
        return re.sub(r"<[^>]+>", "", m.group(1)).strip() if m else ""
    book = Book(title=html.unescape(inner(r'<h1 class="title">(.*?)</h1>')), author=html.unescape(inner(r'<h2 class="author">(.*?)</h2>')))
    m = re.search(r'<div class="main_text">(.*)', page, re.S | re.I)
    body = m.group(1) if m else re.sub(r"(?is)^.*?<body[^>]*>", "", page)
    body, _, tail = body.partition('<div class="bibliographical_information">')
    body = re.sub(r"(?is)</body>.*$", "", body)

    def to_notation(s: str) -> str:
        s = s.replace("\r", "").replace("\n", "")
        s = re.sub(r"(?is)<ruby>\s*<rb>(.*?)</rb>.*?<rt>(.*?)</rt>.*?</ruby>", r"｜\1《\2》", s)
        s = re.sub(r"(?is)<ruby>(.*?)<rt>(.*?)</rt>.*?</ruby>", lambda m: "｜" + re.sub(r"(?is)<rp>.*?</rp>", "", m.group(1)) + "《" + m.group(2) + "》", s)
        s = re.sub(r'(?is)<img[^>]*?alt="※[(（](.*?)[)）]"[^>]*>', r"※［＃\1］", s)
        s = re.sub(r'(?is)<img[^>]*?src="(?:[^"]*/)?([A-Za-z0-9_.\-]+\.(?:png|jpe?g))"[^>]*>', r"\n［＃挿絵（\1）入る］\n", s)
        s = re.sub(r'(?is)<(?:em|strong) class="[^"]*sesame[^"]*">(.*?)</(?:em|strong)>', r"［＃傍点］\1［＃傍点終わり］", s)
        s = re.sub(r'(?is)<div class="jisage_(\d+)"[^>]*>', r"\n［＃ここから\1字下げ］\n", s)
        for tag, name in (("h3", "大"), ("h4", "中"), ("h5", "小")):
            s = re.sub(rf"(?is)<{tag}[^>]*>(.*?)</{tag}>", rf"\n［＃{name}見出し］\1［＃{name}見出し終わり］\n", s)
        s = re.sub(r"(?i)</div>", "\n［＃ここで字下げ終わり］\n", s)
        s = re.sub(r"(?i)<br\s*/?>|</p>", "\n", s)
        return html.unescape(re.sub(r"<[^>]+>", "", s))
    book.blocks = _blocks(to_notation(body).split("\n"))
    book.colophon = [x for x in (_NOTE.sub("", x).strip() for x in to_notation(tail).split("\n")) if x]
    return book


# ---- sentences for the analysis ------------------------------------------------------------------
_END = re.compile(r"[^。！？!?‼⁇⁈⁉]+[。！？!?‼⁇⁈⁉]*[」』）〕］]*")


def sentences(book: Book) -> list[dict]:
    """The body as sentences: text, the heading it is under, and the readings the edition gives in it."""
    out, where = [], ""
    for b in book.blocks:
        if b.kind in ("break", "img"):
            continue
        if b.kind != "p":
            where = b.text[:30]
            continue
        text, marks = "", []
        for t, r in ((_MARK.sub("", t), r) for t, r in b.runs):
            if not t:
                continue
            if r:
                marks.append((len(text), t, r))
            text += t
        for m in _END.finditer(text):
            lead = len(m.group()) - len(m.group().lstrip())
            s = m.group().strip()
            if len(s) > 1:
                out.append(dict(text=s, location=where,
                                ruby=[(base, r) for at, base, r in marks if m.start() + lead <= at < m.end()]))
    return out


class Readings:
    """Takes the edition's readings over the parser's guess, where they settle a choice.

    The parser reads 一寸 as いっすん; the edition wrote 一寸《ちょっと》. When a word of the sentence
    begins with the characters a reading is written over, and the dictionary has that word with a
    reading that begins the same way, that reading is used. Readings the dictionary does not
    know (old or personal ones) change nothing.

    That holds in the sentence where the reading is written. Elsewhere in the book it is carried
    over only when the edition gives that word one reading throughout and the two readings are one
    dictionary entry said two ways (私 as わたくし, not 方 as かた where ほう may be meant).
    """
    def __init__(self, dic, items: list[dict]):
        self.dic, self.items, self.changed = dic, items, 0

    def apply(self, lists: list[list]) -> list[list]:
        """lists: the parser's words for each of the sentences, in the same order."""
        from .dictdata import hira  # noqa: PLC0415
        if len(lists) != len(self.items):
            return lists
        given: dict = {}
        for s in self.items:
            for base, r in s.get("ruby") or ():
                given.setdefault(base, set()).add(hira(r))
        steady = {b: next(iter(rs)) for b, rs in given.items() if len(rs) == 1}
        for s, toks in zip(self.items, lists):
            for base, r in s.get("ruby") or ():
                r = hira(r)
                for i, t in enumerate(toks):
                    if not (t.surface.startswith(base) and t.form.startswith(base)) or hira(t.reading).startswith(r):
                        continue
                    known = self.dic.readings([t.form]).get(t.form, [])
                    pick = next((k for k in known if hira(k) == r), None) if t.form == base else \
                        next((k for k in known if hira(k).startswith(r)), None)
                    if pick:
                        toks[i] = type(t)(t.form, hira(pick), t.surface)
                        self.changed += 1
                    break
        cache: dict = {}
        for s, toks in zip(self.items, lists):
            here = {b for b, _ in s.get("ruby") or ()}
            for i, t in enumerate(toks):
                r = steady.get(t.form) if t.form == t.surface and t.form not in here else None
                if not r or hira(t.reading) == r:
                    continue
                key = (t.form, hira(t.reading), r)
                if key not in cache:
                    cache[key] = r in {hira(k) for k in self.dic.readings([t.form]).get(t.form, [])} and self.dic.same_entry(t.form, t.reading, r)
                if cache[key]:
                    toks[i] = type(t)(t.form, r, t.surface)
                    self.changed += 1
        return lists


# ---- the EPUB ------------------------------------------------------------------------------------
CSS = """@charset "UTF-8";
html { writing-mode: vertical-rl; -epub-writing-mode: vertical-rl; -webkit-writing-mode: vertical-rl; }
body { line-height: 1.8; }
p { margin: 0; text-indent: 1em; }
h1, h2, h3 { font-weight: bold; text-indent: 0; margin: 0 1em; }
h1 { font-size: 1.4em; } h2 { font-size: 1.2em; } h3 { font-size: 1em; }
.author { text-indent: 0; margin: 0 1em; }
.colophon p { text-indent: 0; font-size: 0.8em; }
em.sesame { font-style: normal; text-emphasis: sesame; -epub-text-emphasis-style: sesame; -webkit-text-emphasis: sesame; }
.end { text-align: right; text-indent: 0; }
.uline { text-decoration: underline; } .big { font-size: 1.25em; } .small { font-size: 0.85em; }
.bold { font-weight: bold; } .ital { font-style: italic; }
.frame { border: 1px solid; padding: 0.5em; margin: 0.5em; }
.fig { text-indent: 0; text-align: center; margin: 0; }
.fig img { max-width: 100%; height: auto; }
""" + "".join(f".vrtl .i{n} {{ margin-top: {n}em; }}\n" for n in range(1, 13))
CHAPTER = 30000         # characters: a chapter longer than this is cut, since a small device reads one file at a time


def _x(s: str) -> str:
    return html.escape(s, quote=False)


def _inline(runs) -> str:
    """Runs as XHTML: readings as ruby, the marks as tags. Tags are kept nested and closed whatever the text does."""
    out, stack = "", []
    for t, r in runs:
        if r:
            out += f"<ruby>{_x(_MARK.sub('', t))}<rt>{_x(r)}</rt></ruby>"
            continue
        for part in re.split("([\ue000-\ue00f])", t):
            if part in _OPENS:
                if _OPENS[part] not in stack:
                    stack.append(_OPENS[part])
                    out += MARKS[_OPENS[part]][2]
            elif part in _CLOSES:
                if _CLOSES[part] in stack:
                    while stack:                    # close what was opened inside it first
                        top = stack.pop()
                        out += MARKS[top][3]
                        if top == _CLOSES[part]:
                            break
            elif not _MARK.fullmatch(part or "x"):
                out += _x(part)
    return out + "".join(MARKS[k][3] for k in reversed(stack))


def chapters(book: Book) -> list[dict]:
    """Cut the book where a reader would: at headings and page breaks, and inside very long stretches."""
    out, cur = [], dict(title="", blocks=[], size=0)

    def close():
        nonlocal cur
        if cur["blocks"]:
            out.append(cur)
        cur = dict(title="", blocks=[], size=0)
    for b in book.blocks:
        if b.kind == "break":
            close()
            continue
        if b.kind in ("h1", "h2", "h3") and any(x.kind == "p" for x in cur["blocks"]):
            close()
        if b.kind == "p" and cur["size"] > CHAPTER:
            title = cur["title"]
            close()
            cur["title"] = title
        if b.kind not in ("p", "img"):
            cur["title"] = (cur["title"] + "　" if cur["title"] else "") + b.text
        cur["blocks"].append(b)
        cur["size"] += 0 if b.kind == "img" else len(b.text)
    close()
    return out


def to_epub(book: Book, dest: Path) -> Path:
    """Write the book as an EPUB 3 file: vertical, right to left, furigana as ruby, one file per chapter."""
    parts = chapters(book)
    uid = uuid.uuid5(uuid.NAMESPACE_URL, f"sumitsubo:{book.title}:{book.author}").urn
    title = _x(book.title or "無題")

    def page(body: str, name: str, cls: str = "") -> str:
        return ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE html>\n'
                '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="ja" lang="ja" class="vrtl">\n'
                f'<head><meta charset="UTF-8"/><title>{_x(name)}</title><link rel="stylesheet" type="text/css" href="style.css"/></head>\n'
                f'<body class="vrtl{" " + cls if cls else ""}">\n{body}</body>\n</html>\n')
    files, toc = [], []
    for n, ch in enumerate(parts, 1):
        body = ""
        if n == 1:
            body += f"<h1>{title}</h1>\n" + (f'<p class="author">{_x(book.author)}</p>\n' if book.author else "")
        for b in ch["blocks"]:
            if b.kind == "img":
                body += f'<p class="fig"><img src="img/{_x(b.runs[0][0])}" alt=""/></p>\n'
                continue
            tag = "p" if b.kind == "p" else "h2" if b.kind == "h1" else "h3"
            cls = " ".join(x for x in (f"i{b.indent}" if b.indent else "", "end" if b.end else "", *b.cls) if x)
            body += f"<{tag}{' class=' + chr(34) + cls + chr(34) if cls else ''}>{_inline(b.runs)}</{tag}>\n"
        name = ch["title"] or (book.title if n == 1 else "")
        files.append((f"c{n:04d}.xhtml", page(body, name or book.title)))
        if name and (not toc or toc[-1][1] != name):
            toc.append((f"c{n:04d}.xhtml", name))
    if book.colophon:
        files.append(("colophon.xhtml", page("".join(f"<p>{_x(_plain(s))}</p>\n" for s in book.colophon), "底本", "colophon")))
        toc.append(("colophon.xhtml", "底本"))
    if not toc:
        toc.append((files[0][0], book.title or "本文"))
    manifest = "".join(f'<item id="f{i}" href="{f}" media-type="application/xhtml+xml"/>\n' for i, (f, _) in enumerate(files))
    manifest += "".join(f'<item id="g{i}" href="img/{_x(n)}" media-type="image/{"png" if n.lower().endswith(".png") else "jpeg"}"/>\n'
                        for i, n in enumerate(book.images))
    spine = "".join(f'<itemref idref="f{i}"/>\n' for i in range(len(files)))
    opf = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id" xml:lang="ja">\n'
           '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
           f'<dc:identifier id="id">{uid}</dc:identifier>\n<dc:title>{title}</dc:title>\n'
           + (f"<dc:creator>{_x(book.author)}</dc:creator>\n" if book.author else "") +
           '<dc:language>ja</dc:language>\n<dc:source>青空文庫 https://www.aozora.gr.jp/</dc:source>\n'
           '<meta property="dcterms:modified">2000-01-01T00:00:00Z</meta>\n</metadata>\n<manifest>\n'
           '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>\n'
           '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>\n'
           '<item id="css" href="style.css" media-type="text/css"/>\n' + manifest +
           '</manifest>\n<spine toc="ncx" page-progression-direction="rtl">\n' + spine + '</spine>\n</package>\n')
    nav = ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE html>\n'
           '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="ja" lang="ja">\n'
           f'<head><meta charset="UTF-8"/><title>{title}</title></head>\n<body>\n<nav epub:type="toc" id="toc"><h1>目次</h1>\n<ol>\n'
           + "".join(f'<li><a href="{f}">{_x(t)}</a></li>\n' for f, t in toc) + '</ol>\n</nav>\n</body>\n</html>\n')
    ncx = ('<?xml version="1.0" encoding="UTF-8"?>\n<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n'
           f'<head><meta name="dtb:uid" content="{uid}"/></head>\n<docTitle><text>{title}</text></docTitle>\n<navMap>\n'
           + "".join(f'<navPoint id="n{i}" playOrder="{i}"><navLabel><text>{_x(t)}</text></navLabel><content src="{f}"/></navPoint>\n'
                     for i, (f, t) in enumerate(toc, 1)) + '</navMap>\n</ncx>\n')
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?>\n<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
                   '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>\n</container>\n')
        z.writestr("OEBPS/content.opf", opf)
        z.writestr("OEBPS/nav.xhtml", nav)
        z.writestr("OEBPS/toc.ncx", ncx)
        z.writestr("OEBPS/style.css", CSS)
        for f, text in files:
            z.writestr("OEBPS/" + f, text)
        for n, data in book.images.items():
            z.writestr(zipfile.ZipInfo("OEBPS/img/" + n), data, zipfile.ZIP_STORED)
    return dest


def file_name(book: Book) -> str:
    """A name for the book's file that any card's file system accepts."""
    name = " - ".join(x for x in (book.title, book.author) if x) or "aozora"
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", name).strip(" .")
    while len(name.encode("utf-8")) > 120:
        name = name[:-1]
    return (name or "aozora") + ".epub"
