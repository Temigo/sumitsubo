"""Is this text fit to be the sentence on a card?

Text taken from a PDF or a photographed page comes with debris: stray symbols where a bullet, a
rule or a formula was, half a bracket, an entry of the reference list, a caption. `clean` removes
what can be removed without touching the sentence. `poor` names what is wrong with a text that
should not carry a card at all; its words are still counted for the source, and the sentence is
listed under Cards, Suspended, Bad sentences, where it can be brought back.
"""
import re

_KANA = "぀-ヿ"
_JA = _KANA + "㐀-鿿々〆"
_PAIRS = {"「": "」", "『": "』", "（": "）", "(": ")", "［": "］", "[": "]", "【": "】", "〔": "〕", "〈": "〉", "《": "》"}
_CLOSE = {v: k for k, v in _PAIRS.items()}
# What may stand before the first word of a sentence only by accident.
_LEAD = r"\s・、。，,.．:：;；／/\\|｜\-—–―~〜*＊•◦▪■□●○◆◇▶▷※†‡§#＃=＝_＿^`'\"”’」』）)\]］】〕〉》>"
_DEBRIS = re.compile(r"[／/\\|｜_＿=＝^~*＊#＃<>＜＞]{2,}|[�■-◿]{2,}|(?:[・.．]\s*){4,}")


def _unmatched(text: str) -> str:
    """Brackets and quotation marks whose other half is not in the text are dropped: half of them
    was lost at a line or page break, or was never a bracket."""
    drop, stack = set(), []
    for i, ch in enumerate(text):
        if ch in _PAIRS:
            stack.append(i)
        elif ch in _CLOSE:
            if stack and text[stack[-1]] == _CLOSE[ch]:
                stack.pop()
            else:
                drop.add(i)
    drop.update(stack)
    return "".join(ch for i, ch in enumerate(text) if i not in drop) if drop else text


def clean(text: str) -> str:
    text = re.sub(r"[​-‏ - ﻿�\x00-\x08\x0b-\x1f]", "", text).strip()
    out = _unmatched(text)
    lead = re.match(rf"[{_LEAD}]+", out)
    if lead:
        out = out[lead.end():]
        # debris was there: a scrap of one to three Latin letters or digits before the Japanese belongs to it
        scrap = re.match(rf"[A-Za-z0-9]{{1,3}}\s+(?=[{_JA}])", out)
        if scrap:
            out = out[scrap.end():]
    out = re.sub(r"[\s・／/\\|｜*＊•※#＃=＝_＿^~]+$", "", out)
    return out if len(out) >= 2 else text


def poor(text: str) -> str | None:
    """Why this text should not be a card's sentence, or None when nothing is wrong with it."""
    body = re.sub(r"\s", "", text)
    if not body:
        return "empty"
    ja = len(re.findall(rf"[{_JA}ー]", body))
    if re.search(r"https?://|www\.|\bdoi\b|\bISBN\b|\bpp?\.\s*\d|\bvol\.|\bet al\b|arXiv", text, re.I):
        return "a reference"
    year = re.search(r"(?:19|20)\d\d", text)
    if year and (re.search(r"[（(][^（）()]{0,40}(?:19|20)\d\d\s*[）)]\s*[.．。]?\s*$", text)
                 or re.search(r"出版会|出版社|出版局|書店|書房|新書|文庫|学会誌|論文集|編著|[編訳著]\s*[,，、）)]", text)):
        return "a reference"
    if _DEBRIS.search(text):
        return "stray symbols"
    if ja / len(body) < 0.4:
        return "mostly not Japanese"
    if len(body) >= 8 and not re.search(r"[぀-ゟ]", body):
        return "not a sentence"                       # a title, a label, a list of terms: no hiragana at all
    if len(re.findall(r"(?<![A-Za-z0-9])[A-Za-z](?![A-Za-z0-9])", text)) >= 4 and ja / len(body) < 0.7:
        return "stray symbols"                        # single letters scattered about: a formula or a table, flattened
    return None


def sweep(con, tell=None) -> dict:
    """Go over the sentences already in the library: clean each, and set aside the poor ones.
    Sentences you brought back yourself are left alone."""
    kept = {(r[0], r[1]) for r in con.execute("SELECT source_id, text FROM kept")}
    done = dict(cleaned=0, set_aside=0)
    rows = con.execute("""SELECT s.id, s.source_id, s.text FROM sentences s JOIN sources src ON src.id=s.source_id
                          WHERE src.kind NOT IN ('saved', 'aozora')""").fetchall()
    for n, r in enumerate(rows):
        if tell and n % 2000 == 0:
            tell(n, len(rows))
        text = clean(r["text"])
        if text != r["text"]:
            con.execute("UPDATE sentences SET text=? WHERE id=?", (text, r["id"]))
            done["cleaned"] += 1
        why = poor(text)
        if why and (r["source_id"], text) not in kept:
            done["set_aside"] += con.execute("INSERT OR IGNORE INTO rejected(source_id, text, why) VALUES(?,?,?)", (r["source_id"], text, why)).rowcount
    return done
