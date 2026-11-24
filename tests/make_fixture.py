"""Build the small dictionary the tests use, from the jamdict-data package and the JLPT lists.

    python tests/make_fixture.py /path/to/yomitan-jlpt-vocab/original_data

Writes tests/fixture/: a JMdict subset and a KANJIDIC2 subset in the jmdict-simplified JSON layout,
and n1..n5.csv subsets. Only entries that can occur in tests/samples are kept. JMdict and KANJIDIC2
are the property of the Electronic Dictionary Research and Development Group (CC BY-SA 4.0).
"""
import csv
import json
import sys
import zipfile
from pathlib import Path

import jamdict_data

from sumitsubo import dictdata

here = Path(__file__).parent
text = "".join(p.read_text(encoding="utf-8") for p in sorted((here / "samples").glob("*")) if p.suffix in {".txt", ".srt", ".tsv"})
text += "発表受付今日大人思わず実は走り出す先行研究一人日本語勉強資料配布"
text += "おっしゃる いらっしゃる なさる おる いただく ご覧になる 言う 見る 行く する 下さる"      # keigo verbs, met conjugated in the samples
entries, kanji_rows, _ = dictdata._jamdict(jamdict_data.JAMDICT_DB_PATH)
words, keep = [], set()
for e in entries:
    forms = [k for k, _ in e["kanji"]] + [k for k, _, _ in e["kana"]]
    stems = [f[:-1] for f in forms if len(f) >= 2 and not dictdata.kanji_of(f[-1])]
    if not any(f in text for f in forms) and not any(dictdata.kanji_of(s) and s in text for s in stems):
        continue
    keep.add(e["seq"])
    words.append(dict(id=str(e["seq"]),
                      kanji=[dict(text=k, common=c) for k, c in e["kanji"]],
                      kana=[dict(text=k, common=c, appliesToKanji=a if a is not None else ["*"]) for k, c, a in e["kana"]],
                      sense=[dict(partOfSpeech=[p for p in e["pos"].split(",") if p], field=[],
                                  misc=(["uk"] if e["uk"] else []) + ([dict(respectful="hon", humble="hum", polite="pol", formal="form", colloquial="col")[e["reg"]]] if e.get("reg") else []),
                                  gloss=[dict(lang="eng", text=g) for g in e["gloss"].replace(" | ", "; ").split("; ") if g])]))
chars = []
for lit, ons, kuns, meaning, jlpt, grade, freq in kanji_rows:
    if lit in text or any(lit in k["text"] for w in words for k in w["kanji"]):
        chars.append(dict(literal=lit, misc=dict(grade=grade, frequency=freq, jlptLevel=jlpt),
                          readingMeaning=dict(groups=[dict(
                              readings=[dict(type="ja_on", value=r) for r in ons.split()] + [dict(type="ja_kun", value=r) for r in kuns.split()],
                              meanings=[dict(lang="en", value=m) for m in meaning.split("; ") if m])])))
out = here / "fixture"
for name, payload in (("jmdict-eng-fixture", dict(version="fixture", words=words)), ("kanjidic2-en-fixture", dict(version="fixture", characters=chars))):
    with zipfile.ZipFile(out / f"{name}.json.zip", "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{name}.json", json.dumps(payload, ensure_ascii=False))
for n in range(1, 6):
    rows = [r for r in csv.DictReader(open(Path(sys.argv[1]) / f"n{n}.csv", encoding="utf-8")) if r["jmdict_seq"].isdigit() and int(r["jmdict_seq"]) in keep]
    with open(out / f"n{n}.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["jmdict_seq", "kana", "kanji", "waller_definition"])
        w.writeheader()
        w.writerows(rows)
print(len(words), "entries,", len(chars), "kanji")
