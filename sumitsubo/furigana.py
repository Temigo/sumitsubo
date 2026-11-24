"""Work out which reading each kanji takes inside a word.

    align("分解能", "ぶんかいのう")  ->  [("分", "ぶん"), ("解", "かい"), ("能", "のう")]
    align("発表", "はっぴょう")      ->  [("発", "はつ"), ("表", "ひょう")]

The reading recorded for a kanji is its dictionary reading (はつ, ひょう), not the sound-changed
form it takes in this word (はっ, ぴょう), so the same kanji-reading pair is recognised across
words. Words whose reading cannot be split by kanji (今日 = きょう, 大人 = おとな) return None and
simply yield no kanji-reading pairs.
"""
from .dictdata import hira, is_kanji

_VOICE = {"か": "が", "き": "ぎ", "く": "ぐ", "け": "げ", "こ": "ご", "さ": "ざ", "し": "じ", "す": "ず", "せ": "ぜ",
          "そ": "ぞ", "た": "だ", "ち": "ぢ", "つ": "づ", "て": "で", "と": "ど", "は": "ば", "ひ": "び", "ふ": "ぶ",
          "へ": "べ", "ほ": "ぼ"}
_HALF = {"は": "ぱ", "ひ": "ぴ", "ふ": "ぷ", "へ": "ぺ", "ほ": "ぽ"}


def base_readings(info: dict | None) -> list[str]:
    """A kanji's dictionary readings in hiragana: on readings, and kun readings without okurigana."""
    if not info:
        return []
    out = [hira(r).replace("-", "") for r in (info.get("ons") or "").split()]
    for k in (info.get("kuns") or "").split():
        k = hira(k).replace("-", "")
        stem, _, oku = k.partition(".")
        out.append(stem)
        if oku:
            out.append(stem + oku)          # okurigana left unwritten: 受付 = うけ + つけ
            i_row = dict(zip("うくぐすつぬぶむる", "いきぎしちにびみり"))
            if oku[-1] in i_row:            # the noun form of a verb: 読み, 書き, 受け
                out.append(stem + oku[:-1] + i_row[oku[-1]])
                if oku[-1] == "る":
                    out.append(stem + oku[:-1])
    return [r for r in dict.fromkeys(out) if r]


def _variants(base: str, first: bool, last: bool) -> list[str]:
    """Forms a reading can take inside a compound: voiced at the start, doubled at the end."""
    out = [base]
    heads = [base]
    if not first and base[0] in _VOICE:
        heads.append(_VOICE[base[0]] + base[1:])
        if base[0] in _HALF:
            heads.append(_HALF[base[0]] + base[1:])
    for h in heads:
        if h not in out:
            out.append(h)
        if not last and len(h) > 1 and h[-1] in "つちくき":
            out.append(h[:-1] + "っ")
    return out


def align(surface: str, reading: str, kanji_info) -> list[tuple[str, str]] | None:
    """Split `reading` over the characters of `surface`. kanji_info(ch) returns the kanji's data."""
    reading = hira(reading)
    chars = list(surface)
    memo: dict = {}

    def go(i: int, j: int):
        if i == len(chars):
            return [] if j == len(reading) else None
        if (i, j) in memo:
            return memo[(i, j)]
        ch, res = chars[i], None
        if ch == "々" and i > 0:
            ch = chars[i - 1]
        if is_kanji(ch):
            for base in base_readings(kanji_info(ch)):
                for form in _variants(base, i == 0, i == len(chars) - 1):
                    if reading.startswith(form, j):
                        rest = go(i + 1, j + len(form))
                        if rest is not None:
                            res = [(ch, base)] + rest
                            break
                if res is not None:
                    break
        elif j < len(reading) and hira(ch) == reading[j]:
            res = go(i + 1, j + 1)
        memo[(i, j)] = res
        return res

    return go(0, 0)
