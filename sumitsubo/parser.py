"""Word analysis, done by Anki Miner's parser used as a library.

Anki Miner (GPL-3.0-or-later, github.com/0xzerolight/anki_miner) tokenizes with MeCab and UniDic
and then joins tokens into dictionary words: 走り出した is mined as 走り出す, 考慮に入れる as one
expression, 先行研究 as one noun. It does not carry a dictionary of its own; it asks the caller
"is this string a word?" and similar questions. We answer from our dictionary and glossaries, so
field terms in a glossary are joined the same way.

Only this module imports anki_miner. It is tied to the pinned version in pyproject.toml.
"""
import os
from dataclasses import dataclass

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")   # the parser imports Qt's core module; no window is opened


@dataclass(frozen=True)
class Token:
    form: str        # dictionary form as it would appear on a card front
    reading: str     # hiragana reading of that form
    surface: str     # as written in the sentence


class Parser:
    def __init__(self, dic):
        try:
            from anki_miner.config import AnkiMinerConfig  # noqa: PLC0415
            from anki_miner.models.reading import ReadingUnit  # noqa: PLC0415
            from anki_miner.services.subtitle_parser import SubtitleParserService  # noqa: PLC0415
        except ImportError as e:
            raise RuntimeError("Anki Miner is not installed. Install Sumitsubo with its dependencies "
                               "(pip install -e .), which pulls in the pinned Anki Miner.") from e
        self._unit = ReadingUnit
        self._svc = SubtitleParserService(
            AnkiMinerConfig(),
            term_lookup=lambda terms: dic.terms_exist(list(terms)),
            name_lookup=lambda terms: dic.names_exist(list(terms)),
            reading_lookup=lambda terms: dic.readings(list(terms)),
            kana_attest_lookup=lambda terms: dic.kana_attest(list(terms)),
            term_common_lookup=lambda terms: dic.common(list(terms)),
        )

    def parse(self, sentences: list[str]) -> list[list[Token]]:
        """Content words of each sentence, every occurrence, in order."""
        units = [self._unit(text=s, index=i, location_label="") for i, s in enumerate(sentences)]
        words, _, _ = self._svc.parse_text_units(units, False, every_occurrence=True)
        out: list[list[Token]] = [[] for _ in sentences]
        for w in words:
            i = int(w.start_time)
            if 0 <= i < len(out):
                out[i].append(Token(w.mined_form, w.expression_reading or w.reading or "", w.surface))
        return out
