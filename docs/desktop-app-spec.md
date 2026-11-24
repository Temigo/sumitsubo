# Desktop app: requirements and spec (draft 0.1)

> **Changed in 0.2.0.** Where this document says a word is "known" or "assumed known", the program now keeps a memory state instead (New, Learning, Review as Young or Mature, Suspended), as described under "How well you know something" in the README. The level seeds items as review cards with a long interval that come back once as a check.

Date: 2026-11-23
Companion to `requirements-and-spec.md`, which covers the whole project. This note covers only the desktop program. Where the two disagree, this one is newer.

Status: for review. Nothing here is built to this spec yet. Statements about Anki Miner come from reading its source and docs at commit `fb8853b` (2026-11-22); none of its commands were run. Items marked **[verify]** are assumptions that the first milestone must test.

Priority labels: **Must** (first usable version), **Should** (soon after), **Later**.

---

## 0. Decisions to confirm

These shape everything below. Each has my recommended answer; change any of them and the spec changes with it.

| # | Decision | Recommended |
|---|---|---|
| 1 | Ingest engine | Ship a pinned, unmodified Anki Miner and call its `--api` commands for download, transcription, audiobook alignment and OCR. Our own code does everything after that. |
| 2 | Tokenizer | **Anki Miner's parser, imported as a library from the pinned copy, behind a thin adapter.** It uses MeCab with UniDic plus about 9,000 lines of Japanese word-boundary logic, and covers Chinese and Korean through the same interface. A spike on 2026-11-23 called `SubtitleParserService.parse_subtitle_file` with about ten lines of glue, with no Anki and only Qt's core module loaded. Given JMdict headwords through its injected `term_lookup`, it found 声を上げる, 分解能, 考慮に入れる, 先行研究, 走り出す and 一点 as single words in six test sentences; Sudachi found only 分解能 of those. The same hook accepts user glossaries, so field terms can be merged the same way. Still to do: wire its other lookups (the rejoin table for 思わず and 実は did not fire without them), and repeat on a real lecture transcript. Upstream is not being asked for changes at this stage. |
| 3 | Form of the program | A local app with a small browser-based window (as horu already is) plus a command line. No separate GUI toolkit of our own. |
| 4 | Review on the desktop | None. The device reviews. People without a device use the Anki export. |
| 5 | First platform | Linux, where the GPU machine is. Windows and macOS follow. |
| 6 | Gloss language | An option, set by which dictionary is active. JMdict with English glosses ships as the default. A user can add or switch to another dictionary, for example one with Chinese glosses, and cards, lookups and exports then use its glosses. The dictionary also supplies headwords to the parser, so all active dictionaries and glossaries are pooled for word splitting. Anki Miner includes an importer for Yomitan-format dictionaries, which is the likely route for third-party dictionaries. **[verify]** |
| 7 | Licence | GPL-3.0-or-later for the desktop program, since it bundles Anki Miner. Firmware stays MIT; the two only exchange files. Not legal advice. |
| 8 | Name | Still open. "Horu" is taken. |

---

## 1. Scope

### The desktop program does

1. Takes in a source (link, recording, PDF, ebook).
2. Turns it into timed, sentence-cut text with audio clips.
3. Works out which words, kanji readings and (later) grammar points in it the learner does not know.
4. Makes cards from them, within a daily budget.
5. Scores how readable the source is for this learner.
6. Writes all of that to the device, and reads the device's review log back.
7. Exports to Anki for people without a device.

### It does not

- Schedule or run reviews.
- Replace Anki Miner's GUI, word curator, or Anki integration.
- Provide a reader. Reading happens on the device.
- Need an account, a server, or Anki.

---

## 2. Architecture

```
            +-------------------------------+
 source --> |  Ingest (Anki Miner --api)    |  fetch, transcribe, sync, ocr
            +-------------------------------+
                        | timed text + media file
                        v
            +-------------------------------+
            |  Analysis (ours)              |  sentences, words, kanji readings
            +-------------------------------+
                        |
        +---------------+----------------+
        v               v                v
  Knowledge model   Card maker      Difficulty score
        ^               |                |
        |               v                v
  review log     +-------------------------------+
  from device <--|  Pack writer and sync         |--> device / Anki export
                 +-------------------------------+
```

The boundary with Anki Miner is deliberately narrow: it hands over a subtitle or text file with timings, and the original media file. Everything that expresses an opinion about what the learner knows stays in our code.

### Why this split

- Anki Miner's API is built for callers that keep their own record of what the user knows; its docs say its known-words list is never touched by API runs.
- Its `fetch`, `transcribe`, `sync` and `ocr` commands are documented as not needing Anki. **[verify]**
- It already cuts long Whisper segments at sentence ends using word timestamps, and handles GPU library installation, which were the two weakest parts of horu.
- Taking only timed text from it means a tokenizer mismatch cannot happen, and its `mine` and `render` commands, which do need Anki, are never called.

### Fallback

If the **[verify]** items fail, horu's existing ingest code (yt-dlp, faster-whisper, pypdf) is the fallback, with sentence cutting added. The analysis stage must not care which produced its input.

---

## 3. Functional requirements

### 3.1 Sources and ingest

| ID | Priority | Requirement |
|---|---|---|
| I1 | Must | Accept a video link, a local audio or video file, a PDF, a plain text file, an EPUB. |
| I2 | Must | Accept a playlist or a folder as one series. Anki Miner's `mine youtube` refuses playlists, so the playlist is expanded by our code first. |
| I3 | Must | Transcribe audio locally, on GPU when present, CPU otherwise, with no user configuration. |
| I4 | Must | Produce sentences of roughly 3 to 10 seconds, each with start and end time. |
| I5 | Must | Cut one audio clip per sentence used on a card. Clips are cut by our code with ffmpeg from the timings. |
| I6 | Must | Keep the full text of the source (transcript or book) for the device's reader. |
| I7 | Must | Queue sources and process them unattended. A failed source is reported and does not block the queue. |
| I8 | Must | Resume after interruption without redoing finished stages. |
| I9 | Should | Tag a source with a field and a series (for example `physics/particle`, "Group seminar 2026"). |
| I10 | Should | Use existing captions when a video has good ones, skipping transcription. |
| I11 | Later | Align a book's text to its audiobook (Anki Miner's `sync`). |
| I12 | Later | Scanned PDFs and manga through OCR. |

### 3.2 Analysis

| ID | Priority | Requirement |
|---|---|---|
| A1 | Must | Split every sentence into words with dictionary form and reading, using one tokenizer throughout. |
| A2 | Must | Look each word up in JMdict; user glossaries take precedence. |
| A3 | Must | For each word, record which kanji it contains and which reading each kanji takes in that word. |
| A4 | Must | Discard words that no dictionary or glossary confirms, so transcription errors do not become cards. |
| A5 | Must | Filter out fragments from broken PDF extraction (by length and share of recognisable Japanese). |
| A6 | Should | Detect entries from the built-in list of academic and meeting phrases. |
| A7 | Later | Detect grammar points, fixed expressions first. |

### 3.3 Knowledge model

| ID | Priority | Requirement |
|---|---|---|
| K1 | Must | Track three item types: word, kanji-reading pair, and (later) grammar point. |
| K2 | Must | Each item is unknown, learning, known, or ignored. |
| K3 | Must | Seed from a stated level: words at or below it start as known, and every reading of every kanji at or below it starts as known. Seeded items are stored as "assumed known", distinct from "confirmed by review", so a wrong assumption can be corrected with one press on the device and so assumed items can be told apart later. |
| K4 | Must | Update from the device's review log at every sync. The log is the record of truth; state is recomputed from it. |
| K5 | Must | Keep every review log ever received, as a backup. |
| K6 | Should | Import a known-words list (plain text, or an Anki deck) to seed more accurately than a level. |
| K7 | Should | Fit FSRS parameters from the log and send them to the device. |
| K8 | Later | Estimate recall probability per item for the difficulty score. |

### 3.4 Cards

| ID | Priority | Requirement |
|---|---|---|
| C1 | Must | Word card: the word in a sentence from the source, audio if available; back has reading and meaning. |
| C2 | Must | Listening card: audio front, text on the back; only from sentences with exactly one unknown word. |
| C3 | Must | Kanji-in-context card: one kanji with one reading; front shows three to five words using it, no furigana; back shows their readings and meanings. |
| C4 | Must | For C3, choose words in this order of priority: (1) from this source, (2) from the learner's other sources, (3) words whose other kanji are known, (4) as a lower-priority tie-breaker, words that are more frequent across the learner's other sources, and in general frequency data where available. |
| C5 | Must | A kanji-reading card is created only when that reading occurs in the learner's material. One context word is enough to create it; more are added as they are met. |
| C6 | Must | Rank candidate items by frequency in this source and across all sources. |
| C7 | Must | New items are released within a daily budget shared across card kinds (default 15). The desktop writes every candidate card with its rank; the device does the releasing, so the budget is enforced where review happens. The budget is adjustable in both places: on the desktop (Settings) and on the device. The most recent change wins and is carried across at the next sync. |
| C8 | Must | Every card belongs to the source it came from. |
| C9 | Should | Refresh a kanji card's word list as new words using that reading are met. |
| C10 | Should | Phrase cards from A6. |
| C11 | Later | Grammar cards: several sentences per pattern, rotated. |

### 3.5 Difficulty score

| ID | Priority | Requirement |
|---|---|---|
| S1 | Must | Per source: share of running words known, and the count of new kanji-reading pairs. |
| S2 | Must | Recompute for all sources after each sync, since knowledge changes. |
| S3 | Should | Score a file without adding it ("should I read this?"). |
| S4 | Later | Compare against Jiten's coverage for titles in its library. |

### 3.6 Output

| ID | Priority | Requirement |
|---|---|---|
| O1 | Must | Write packs in the format in section 7 of `requirements-and-spec.md`. |
| O2 | Must | Sync with a device mounted as a drive or an SD card: detect it, read the log, write new and changed packs, set the clock file. |
| O3 | Must | Never delete or rewrite the device's review log. |
| O4 | Must | Export to an Anki deck file with audio, as an option alongside device sync: all sources or selected ones, all three card kinds. Each card keeps a stable identity, so exporting again updates existing notes in Anki instead of duplicating them. Kanji cards are exported as they stand at export time; a later export refreshes their word lists. |
| O5 | Should | Export the knowledge model's known words as plain text. |
| O6 | Later | Sync over local Wi-Fi. |

### 3.7 Glossaries and field lists

| ID | Priority | Requirement |
|---|---|---|
| G1 | Must | Load tab-separated glossaries: term, reading, gloss. |
| G2 | Should | Ship starter lists for a few fields, and make lists shareable as plain files. |
| G3 | Must | Dictionary access goes through one interface (headwords, readings, glosses, commonness, word type), so a dictionary can be swapped or added without touching the rest of the program. |
| G4 | Should | Add a dictionary in Yomitan format and choose which dictionary's glosses appear on cards. |
| G5 | Later | Ship ready-made dictionaries for gloss languages other than English, subject to their licences. |

---

## 4. User-facing surface

Five screens, no more:

1. **Add.** A box for a link or a dropped file. Optional: series and field. One button.
2. **Queue.** Each source with its stage and progress; errors in plain words.
3. **Library.** Sources with difficulty score, card counts, and what is still unknown in each.
4. **Device.** Sync status, last sync, and a "sync now" button that also runs automatically when a device appears.
5. **Settings.** Level, daily budget, glossaries, data folder. Nothing else in the first version.

Command line equivalents: `add`, `status`, `sync`, `score FILE`, `export-anki`.

Deliberately absent: card editing, a word curator, review, per-source options.

---

## 5. Non-functional requirements

| ID | Priority | Requirement |
|---|---|---|
| N1 | Must | One install. Dictionaries, language data and the speech model are fetched by the program on first run. |
| N2 | Must | Fully local. The network is used only to fetch a source the user named and to download components. |
| N3 | Must | All data in one folder that can be copied to another machine and still work. |
| N4 | Must | Pinned versions of Anki Miner and every library; upgrades are deliberate. |
| N5 | Must | Pack format carries a version number; the writer refuses a device whose firmware expects a newer one. |
| N6 | Must | A test suite that runs without network, GPU or Anki Miner, using fixed transcripts. |
| N7 | Should | An hour of audio processed in minutes on a GPU; overnight on CPU is acceptable. |
| N8 | Should | Works on a headless Linux server, with the window reachable from another machine on the home network. |

---

## 6. Out of scope for the first version

- Grammar detection and grammar cards.
- Chinese and Korean.
- Wi-Fi sync.
- Any review interface.
- Editing cards.
- Calling Anki Miner's `mine` or `render`, or talking to Anki.

---

## 7. Acceptance tests

1. **Journey.** Fresh machine: install, state a level, paste a lecture link, plug in the device. Cards and the transcript are on the device with no other action.
2. **No Anki.** The whole journey passes on a machine where Anki has never been installed.
3. **Kanji.** For a test transcript with known content, every kanji-reading pair present is found, and no card shows a reading that does not occur in its words.
4. **Budget.** A source with 500 unknown words releases 15 items on day one, not 500.
5. **Round trip.** Reviews done on the device change the next sync's difficulty scores and remove learned words from new cards.
6. **Loss.** Deleting the device's state and syncing restores scheduling from the backed-up log.
7. **Bad input.** A scanned PDF and a dead link each produce a clear error and leave the queue running.

---

## 8. Milestones

| # | Outcome | Depends on |
|---|---|---|
| M0 | Anki Miner installed in a scratch environment; `setup`, `transcribe`, `fetch` and `inspect` run with no Anki present; output format understood. Its word analysis and Sudachi's compared on the same lecture transcript. Decisions 1 and 2 confirmed or reversed. | Nothing |
| M1 | Text and PDF in; words, kanji-reading pairs, knowledge model, three card kinds, difficulty score, pack on disk. | Nothing (can start in parallel with M0) |
| M2 | Audio and video in, with sentence clips. | M0 |
| M3 | Device sync both ways, on a Sticky or similar. | Firmware study mode |
| M4 | Anki export (required for the first version; can be built before M3 so the pipeline is usable without a device). | M1 |
| M5 | One-step installer on Linux. | M2 |

---

## 9. Open questions

1. Does Anki Miner's `transcribe` output carry word-level times, or only sentence-level? Sentence-level is enough for I4 and I5.
2. Can Anki Miner's `setup` run on a machine with no display libraries? It depends on PyQt6 even when headless.
3. For PDFs and EPUBs, is our own extraction (already in horu) better kept than routed through Anki Miner, given we only need text?
4. Which data set aligns readings to individual kanji, and under what licence? (JmdictFurigana is the candidate.)
5. Does Anki update an existing note on re-import when the deck file carries the same note identity? Believed yes; to be tested with a real import.
6. Which of the parser's other injected lookups (readings, kana attestation, commonness, deinflection rules) do we need to supply, and from what data?

Resolved on 2026-11-23:
- Anki Miner's parser can be called directly from Python with about ten lines of glue and no dictionary setup of its own; headwords are supplied by the caller.
- A kanji reading seen in only one word gets a card with that one word.
- A stated level marks all readings of all kanji at or below it as assumed known.
