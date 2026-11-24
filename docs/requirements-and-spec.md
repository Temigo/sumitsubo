# Requirements and spec (draft 0.1)

> **Changed in 0.2.0.** Where this document says a word is "known" or "assumed known", the program now keeps a memory state instead (New, Learning, Review as Young or Mature, Suspended), as described under "How well you know something" in the README. The level seeds items as review cards with a long interval that come back once as a check.

Date: 2026-11-23
Working name: **Horiato** (掘り跡 / 彫り跡). "Horu" is already used by another Japanese sentence-mining project (github.com/northkey-dev/horu), so the name must change before anything is published. Horiato is a proposal, not a decision.

Status: brainstorming output. Nothing here has been tested on a device. Facts about other projects come from their READMEs and docs as read on 2026-11-21 to 23; items marked **[unverified]** were not checked at all.

---

## 1. Purpose

A pre-assembled, offline path from "material I need to understand" to "reading it and reviewing it on a pocket e-ink device", for intermediate and advanced learners of Japanese.

The individual pieces mostly exist already. What does not exist is the assembled path: today it takes about 12 setup tasks and about 8 actions per source across half a dozen tools. The target is 3 and 3.

### Who it is for

- Intermediate to advanced learners (roughly N2 and beyond) who make progress mainly by consuming real material and reviewing flashcards.
- People whose vocabulary needs are not covered by standard JLPT decks, for example academics who need the language of their field.
- People with little time, who will not hand-pick every card or maintain a tool chain.

### What it is not

- Not a beginner course.
- Not a general Anki replacement.
- Not a new e-reader engine, dictionary, tokenizer, speech recognizer or scheduler. Those are reused.

---

## 2. Principles

1. **Reuse libraries and data, not applications.** Every separate application in the chain is setup work for the user. Embed components; do not ask the user to install and connect other people's apps.
2. **Offline first.** No account, no always-on server, no dependence on AnkiWeb. The network is used only when the desktop fetches a source.
3. **The device holds the learning state.** The desktop mines; the device schedules and reviews.
4. **The review log is the record of truth.** Scheduling state is derived from it and can be recomputed. The log is copied to the desktop at every sync.
5. **No required settings.** Each option added is assembly work handed back to the user. Defaults are chosen in section 9.
6. **Context over isolated items.** Every card shows the item inside material the learner chose.
7. **Open.** Our code, formats and (later) hardware files are open. The one accepted closed piece is the chip vendor's radio library.

---

## 3. The user journey (acceptance test)

### One-time setup: 3 actions

1. Install one desktop program. It downloads its own dictionaries and speech model.
2. Flash the device from that program.
3. State an approximate level (for example "around N2"). This seeds what is assumed known.

### Per source: 3 actions

1. Drop in a link, a recording, a PDF or an ebook.
2. Plug in the device.
3. Read and review.

The first version is done when this journey works end to end with no other step.

---

## 4. Functional requirements

### 4.1 Desktop program

| ID | Requirement |
|---|---|
| D1 | Accept a video or playlist link, an audio or video file, a PDF, a text file, an EPUB. |
| D2 | Transcribe audio locally with Whisper, on GPU when available, CPU otherwise. |
| D3 | Cut transcripts into clause-sized sentences (target 3 to 10 seconds), each with an audio clip. |
| D4 | Tokenize; look up words; identify every kanji and which reading it takes in each word. |
| D5 | Maintain one model of what the learner knows: words, kanji-reading pairs, later grammar points. |
| D6 | Generate the three card kinds in section 5, ranked by frequency in the learner's material, within a daily budget. |
| D7 | Only create cards for words confirmed by a dictionary or a user glossary (guards against transcription errors). |
| D8 | Compute a difficulty figure per source for the device's library view (section 6). |
| D9 | Write packs to the device and read the review log back (section 7). |
| D10 | Accept plain-text glossaries and phrase lists per field (physics, machine learning, ...), shareable between users. |
| D11 | Import an Anki deck file into a pack, and export cards and history in a form Anki can import. One-way tools, so nobody is locked in or out. |
| D12 | Run unattended: a long source can be queued and left overnight. |

### 4.2 Device firmware

| ID | Requirement |
|---|---|
| F1 | Read books and transcripts, including vertical Japanese text, with tap-to-look-up and de-conjugation. (Exists in Matcha.) |
| F2 | Library view listing sources, each with its difficulty figure. |
| F3 | Offline review with FSRS scheduling on the device. |
| F4 | One-press answers during review, including "already known" and "ignore". |
| F5 | Add a looked-up word to the deck of the source being read. |
| F6 | Each source owns its cards; review can be filtered by source. |
| F7 | Append every review to a log; never rewrite the log. |
| F8 | Keep correct time across sleep; refuse to schedule if the clock has gone backwards, and ask for a sync. |
| F9 | Sync over USB, SD card, or local Wi-Fi. |
| F10 | Play a card's audio clip over Bluetooth headphones. **Deferred** until hardware with headphone audio is in hand; the card format carries audio from day one. |

### 4.3 Later, not in the first version

- Grammar-point cards, starting with fixed expressions where detection is reliable.
- Audio playback and audiobooks (F10).
- Writing practice: free writing via a writer-deck firmware, then prompted production and corrections-to-cards.
- Kanji search by components, then by drawing.
- Tilt to answer.
- Chinese. The pack format and the knowledge model must stay language-neutral so this remains possible.
- Custom hardware (section 10).

---

## 5. Card kinds

All three come from the learner's own material.

### Word card

- Front: the word inside a sentence from the source, with audio if the source has it.
- Back: reading and meaning.

### Listening sentence card

- Front: audio only.
- Back: the text, with reading and meaning of the one unknown word.
- Chosen from sentences containing exactly one unknown word.

### Kanji-in-context card

Modelled on the Kanji in Context approach of learning each character through vocabulary.

- **Unit:** one kanji with one reading (生 as せい and 生 as なま are separate items, scheduled separately).
- **Front:** the kanji large, with three to five words that use it in that reading, no furigana.
- **Back:** each word's reading and meaning, the kanji's core meaning, other readings of it already learned.
- **Word choice:** from this source first, then other sources, preferring words whose other kanji are already known, so the card has one new thing.
- **Growth:** a kanji-reading pair is created only when that reading appears in the learner's material. The word list refreshes as new words are met.
- **Grading:** whether the learner can read the words.

### Grammar card (later)

- A sentence from the learner's material with the pattern highlighted, graded on understanding.
- Several sentences per pattern, rotated.
- The learner should commit to what the pattern means before the gloss is revealed.

---

## 6. Difficulty figure

Shown in the library only, not while reading.

- Version 1: share of running words known, plus the count of new kanji-reading pairs. Example: "93% words, 14 new readings".
- Later: add the count of unknown grammar points.
- Possible refinement: weight each item by FSRS's current recall probability instead of known/unknown.

Rough interpretation, from Jiten's guidance: about 80% is hard going, about 90% is a good working range, 98% and above is comfortable.

---

## 7. Pack format and sync (sketch)

The pack is the contract between desktop and device. Design goals: readable one line at a time on a microcontroller, safe against power loss, language-neutral.

```
/packs/<source-id>/
    source.json      title, kind, language, tags, series, difficulty figure
    text/            the book or transcript the reader opens
    cards.jsonl      one card per line
    audio/           one clip per sentence
/state/
    reviews.log      append-only: card id, grade, timestamp
    params.json      FSRS parameters (defaults, or fitted on the desktop)
```

One line of `cards.jsonl` holds: card id, item id, kind (word / listening / kanji / grammar), front text, back text, audio file name if any, source id.

Sync:

1. Desktop reads `reviews.log`, keeps a backup, and updates the knowledge model.
2. Desktop writes new packs and any refreshed cards (for example kanji cards with updated word lists).
3. Desktop sets the device clock.

Open point: whether to agree this format with the Matcha maintainer so other firmwares can read it.

---

## 8. Reuse and fork plan

### Device firmware

| Project | Role | Plan |
|---|---|---|
| [CrossPoint](https://github.com/crosspoint-reader/crosspoint-reader) (MIT) | Reader engine and device support | Not forked directly. Its scope excludes interactive apps, writing tools and media playback, so study and audio cannot go upstream. New-device support (for example the ESP32-S31) can. |
| [Matcha Reader](https://github.com/eszter007/matcha-reader) | Japanese and Chinese reading: vertical text, word splitting, de-conjugated lookup, save word with sentence | **Fork this.** Add our features as self-contained modules. Contact the maintainer early about taking offline study and packs upstream. |
| [CrossPlay](https://github.com/ma-r-s/crossplay) (MIT) | Advertises Anki decks with FSRS, offline | Candidate source for the offline study mode. Read the code first. |
| [crosspoint-anki](https://github.com/H1D/crosspoint-anki) | Review on device plus add-word-from-book, offline cache with queued sync | Reference for the add-from-reading flow. Licence unverified. |
| [metalio-crosspoint](https://github.com/bartiprime/metalio-crosspoint-public) (MIT) | CrossPoint ported to the ESP32-S31 | Reference for the S31 port when audio hardware is taken up. Its toolchain is a vendor copy of unclear provenance. |
| [Draftling](https://github.com/clackups/draftling) | Writer-deck firmware with Japanese (romaji to kana and kanji) and pinyin input | Use for free writing later. Dual boot and licence **[unverified]**. |
| [MicroSlate](https://github.com/Josh-writes/microslate-firmware) | Writer-deck firmware with CrossPoint dual boot | Reference for how dual boot is done. |

Not reused, for the record: [matcha-anki-server](https://github.com/eszter007/matcha-anki-server) (needs an always-on server; its own code is under a non-commercial licence), [JPink](https://github.com/MrProLopstar/JPink), [InkCards](https://github.com/mohitagw15856/Inkcards), [PortableAnki](https://github.com/zykkkl/PortableAnki). Worth reading for ideas.

### Desktop program

| Component | Role | Plan |
|---|---|---|
| Existing horu code | Ingest, Whisper transcription, Sudachi tokenizing, JMdict lookup, glossaries, phrase list, coverage | **Base of the desktop program.** Drop its browser review pages and scheduler; add pack output and the kanji layer. |
| faster-whisper | Speech to text | Embed (already used). Add word-level timestamps for sentence cutting. |
| SudachiPy | Tokenizer | Embed (already used). One tokenizer everywhere, so "a word" means the same thing in cards and in coverage. |
| JMdict, KANJIDIC | Dictionary and kanji data | Embed. Share-alike licences apply. |
| JmdictFurigana | Aligns each word's reading to its individual kanji | Embed; needed for kanji-reading pairs. Seen in Jiten's credits; licence **[unverified]**. |
| FSRS | Scheduler | Device: a small implementation of the scheduling step. Desktop: parameter fitting from the log. Start with FSRS-6 defaults. |
| [Jiten](https://github.com/Sirush/Jiten) (Apache 2.0; data CC BY-SA 4.0) | Coverage, difficulty, frequency data, public API with a text-parse endpoint | Use its open frequency data. Try its API on a few of our own files to check the coverage numbers. Decide later between its API, self-hosting, or computing coverage ourselves. |
| [japanese-immersion-parser](https://github.com/tejhh/japanese-immersion-parser), [Kaidoku](https://github.com/froxily/kaidoku), [OpenJLPT](https://github.com/evanclan/OpenJLPT) | Grammar pattern rules and JLPT lists | Candidates for the later grammar layer. Licences **[unverified]**. |
| KanjiVG, Zinnia | Stroke data and handwriting recognition | For draw-to-search, later. |

### Anki Miner

[Anki Miner](https://github.com/0xzerolight/anki_miner) (GPL-3.0) is the most mature miner and covers more media and languages than horu. It is **not** in the default path, because by its README it only outputs to a running copy of Anki through AnkiConnect, which adds three installs and breaks the 3-action setup. It also does not extract kanji.

It stays supported through requirement D11: a user who already mines with it can import the resulting Anki deck into a pack. See open decision 1.

---

## 9. Defaults (the "no settings" decisions)

| Decision | Default |
|---|---|
| Starting knowledge | Seed from the stated level; "already known" is one press on any card |
| Daily new items | 15 across all card kinds |
| Card confirmation | None. Cards appear without review; "known" and "ignore" correct mistakes |
| Kanji card timing | The day after the first word using that kanji-reading is learned. To be tested |
| Sync | USB |
| Slow machines | Queue and run overnight |

---

## 10. Hardware

### Now

Develop on a **Seeed reTerminal Sticky**: 3.97" touch e-paper, ESP32-S3, microSD, buttons, accelerometer, about $50. Supported by CrossPoint, Matcha, MicroSlate and Draftling. No headphone audio. Japanese radio certification **[unverified]**.

### Later (parked)

- Chip: **ESP32-S31**, the only current Espressif chip with both Classic Bluetooth and LE Audio besides the original ESP32. Arduino support for it is not official yet, and CrossPoint builds on Arduino.
- Screen: 3.97" 800x480 e-paper (about $15 bare). Avoid cheaper low-resolution panels; they are too coarse for kanji with furigana.
- Enough flash for two firmwares (dual boot).
- Estimated parts cost: about $45 per unit at 10 units, about $34 at 50. Estimates, not quotes.
- Wood case, tilt input, solar: undecided.
- A commercial reader with the S31 (Metalio E-Ink 4 Plus, 4.7") already exists; working audio on it is unconfirmed.

---

## 11. Open decisions

1. **Miner.** Recommended: our own desktop program built on horu, embedding libraries. Alternative: rely on Anki Miner with Anki as a pass-through, and build only the kanji layer, packs and firmware. The alternative is less code but fails the 3-action setup.
2. **Fork or contribute.** Depends on the Matcha maintainer's answer. Ask before writing much firmware.
3. **Audio.** Wait for others, or take it on as our own firmware contribution once S31 hardware is on the desk.
4. **Licence.** MIT keeps upstreaming to Matcha and CrossPoint possible. Copying any code from Anki Miner or other GPL projects would force GPL.
5. **Name.**
6. **Pack format ownership.** Ours alone, or agreed with other firmware authors.

---

## 12. Risks

- **Fast-moving field.** Three times in two days a piece assumed missing turned out to exist. Search again before starting any component.
- **Maintenance.** A fork of a fork must keep merging two upstreams. Self-contained modules and pinned library versions reduce this.
- **Card flood.** Automatic extraction produces more than anyone can review; the budget and ranking must hold.
- **Unproven assumption.** No study was found showing that cards from one's own material beat a good general deck. It is plausible, not established.
- **Comprehension only.** Reading and review do not build speaking and writing; that needs the later writing features.
- **Commercial alternatives.** Migaku plus a grammar service covers much of this on a phone today, for a subscription. This project's case rests on being open, offline, phone-free, automatic and built around academic material.

---

## 13. First steps

1. Use the off-the-shelf version for two to three weeks: a Sticky running Matcha, fed by the existing horu or by Anki Miner. Note what is missed most.
2. Write to the Matcha maintainer about offline study and packs.
3. In horu: word-level timestamps and clause cutting; kanji-reading extraction; pack output.
4. Read CrossPlay's and crosspoint-anki's study code; build the offline study mode in a Matcha fork on the Sticky.
5. Run a few of our own files through Jiten's API and compare its coverage with experience of those texts.
