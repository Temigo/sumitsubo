# Sumitsubo: guide

How the desktop program works, in more detail than the [README](../README.md).

- [The cards](#the-cards)
- [How well you know something](#how-well-you-know-something)
- [Finding your way around](#finding-your-way-around)
- [Kinds of source](#kinds-of-source)
- [Importing your progress](#importing-your-progress)
- [The reader device](#the-reader-device)
- [Back to Anki, with your progress](#back-to-anki-with-your-progress)
- [Command line](#command-line)
- [How it works](#how-it-works)
- [Known limits](#known-limits)

## The cards

You do not design cards in Sumitsubo. There are five kinds, each made automatically from your own
sources, and each aimed at something a plain word-and-meaning flashcard does not train.

| Card | Front | Back | What it trains |
|---|---|---|---|
| **Word** | The word inside a sentence from your material. Up to five sentences per word, from every source it appears in, shown in turn | Reading and meaning | Recognising the word in context, not one memorised sentence |
| **Listening** | Audio only, from a sentence in which this is the one unfamiliar word | The sentence, the word, its meaning | Catching the word by ear, in the speaker's own voice |
| **Kanji in context** | One kanji with words you have met that use it | That reading, the words' readings and meanings | A kanji as a reading inside real words, one reading at a time |
| **Collocation** | A sentence with the verb hidden: 仮説を＿＿. The English is shown too while the card is New or Young, and dropped once it is Mature | The full sentence and meaning | Which verb goes with which noun, the step from intermediate to advanced |
| **Contrast** | Two to four things that are easily confused, side by side | What tells them apart | Kanji that look alike (検 験), readings of one kanji (出: しゅつ, だ), words that sound the same (構成, 較正), and keigo (言う, おっしゃる, 申す) |

Every kind keeps its own memory state. Listening to a word, reading it, and telling it from its
look-alike are learned and scheduled separately.

**Contrast cards are shown sparingly.** By default one appears only after you have mixed its items
up; you can instead have them appear once you have met all of the items, or never.

**On a collocation card** the particle is hidden along with the verb when it is not を
(結論＿＿ for 結論に達する), since に, が, と and で are part of what has to be recalled. The noun is
never hidden: you choose the noun for its meaning, and the language then dictates the rest.

**Collocations** come from two places: expressions the dictionary lists (仮説を立てる, 役割を果たす),
and pair lists you add (a plain file of noun, particle, verb), which is how field-specific pairs
and any outside source of collocations come in.

### Register and keigo

For reading and listening, what matters about keigo is recognising who is doing what to whom.

- **Words carry the dictionary's register label** where it has one: respectful, humble, polite,
  formal or colloquial. 申す shows as humble.
- **Sentences are labelled** plain (だ, である), polite (です, ます) or keigo (respectful or humble
  forms), from their wording. It is a rough label that catches the common forms. A word's
  sentences are chosen to vary in register where there is a choice, and each source shows its
  mix: a paper is mostly plain, a seminar mostly polite with humble forms.
- **Keigo contrast cards** put a plain verb beside its respectful and humble counterparts, from a
  short hand-written table of the verbs that have their own forms (言う, 行く, いる, する, 見る,
  食べる, 知る and others). A card is made once you have met one of the three.

### How this differs from a deck tool

This is how we understand the tools most learners use; each does its own job well.

| | Plain Anki | Renshuu | Sumitsubo |
|---|---|---|---|
| Where cards come from | You make them, or download a shared deck | Its own vocabulary, kanji, grammar and sentence schedules | Your own talks, papers and books |
| Card design | Yours to design and maintain | Fixed by the site, with many options | Fixed: five kinds, no options to tune |
| Sentences on a card | Whatever the deck author put there | The site's example sentences | Sentences from your sources, several per word, rotating |
| Kanji | A separate deck, usually by meaning | Per kanji, with its readings | Per reading, inside words you have met |
| Confusable items | Not connected to each other | Not grouped into cards | Contrast cards, built from what you have met |
| Register and keigo | Whatever a deck says | Taught in its grammar lessons | Labelled on words and sentences; keigo contrast cards |
| Verb-noun pairings | Only if a deck has them | Not a card type | Collocation cards with the verb hidden |
| "Can I read this yet?" | Not answered | Not answered for your own texts | A score per source, sentences you can read in full, and a date |
| Where it runs | Your devices, syncing through AnkiWeb | Their website and apps | Your computer and a reading device, offline |

Progress made in Anki or Renshuu comes with you (see [Importing your progress](#importing-your-progress)), and cards go back
out as an Anki deck with that progress (see [Back to Anki](#back-to-anki-with-your-progress)), so trying this does not mean leaving either.

### Sources, priorities and dates

Each source shows how much of it you can read today, how many of its sentences you can read in
full, and how many more are one word away. Sources have a priority (first, normal, later), and new
cards are introduced in that order within your daily budget. From the budget and the priorities,
Sumitsubo works out the date by which the last new word of each source will have been introduced;
it adds about three weeks for those words to settle, which is an estimate.

## How well you know something

There is no "known" flag. As in Anki, an item is **New** (never studied), **Learning**, or in
**Review** with a memory state; a review item with an interval of three weeks or more is called
**Mature**, under that **Young**. An item can also be **Suspended**. The memory model is
FSRS, through the [py-fsrs](https://github.com/open-spaced-repetition/py-fsrs) library.

- **Your level** does not mark words as known. Words and kanji readings at or below it are treated
  as already in review with a long interval, and each comes back once, four to eight months later
  (spread out, not all on one day), as a check. Changing the level redoes this.
- **"I know this"** does the same, about a year out. **"Learn this"** makes an item New whatever
  anything else says. Both can be undone, on the spot or under Cards, Suspended.
- **Difficulty** is the share of running words you can expect to read today: each word counts by
  its chance of being recalled now, which falls as time passes without a review. The bar shows
  mature, young, new but already queued by an earlier source, and new here.
- **Every item you have met has a card** unless it is suspended. New cards are the ones to learn;
  the rest wait for their due date. Pages show the cards still to learn by default.
- **A listening card** is its own skill and starts New. A **kanji reading** imported through a
  word inherits that word's state.

Reviewing happens on the reader device or in Anki; this program has no review screen.

### Word frequency

A frequency list breaks ties when ordering cards and choosing words for kanji cards. It is a file
in the data folder and is read from disk; Sumitsubo contacts no service while you work. `setup`
downloads the global list published by [Jiten](https://jiten.moe/frequency-dictionaries) once.
To use another list, or if that download fails, choose a file in Settings or run
`sumitsubo frequency FILE` with a CSV file or a frequency dictionary in Yomitan format.

Jiten's own program is not bundled. What is useful here is the result of its analysis of about
19,000 titles, and that result is the frequency file; running its code locally would need its
database server and that whole library of texts.

## Finding your way around

Four sections, on the dark rail at the left; the lighter sidebar beside it lists the pages of the
section you are in.

- **Library.** *Sources*: one line a source with how readable it is today, the chosen source on
  top. *Cards*: one line a card, the chosen card on top with its word and every sentence it can be
  shown with; filters for state, kind and source; Contrast cards and what you suspended or marked
  by hand are views of the same page. *Add a source*: a link, a file, text, or page photos.
  "Check difficulty only" shows how readable something is without adding it.
- **Statistics.** *Overview*, *Kanji* (every kanji by JLPT level, coloured by how well you know
  it), *Reviews* (twelve weeks day by day, what falls due, totals by month).
- **Reader.** *Sync*, *Study* (new cards a day, when contrast cards appear), *Books*, and *New
  card* (set up a card for the device).
- **Settings.** *General* (your level), *Dictionaries*, *Imports*, *Engines*.

On a card, **I know this** and **Suspend** act on the word or kanji reading, everywhere. **Bad
sentence** keeps the word, takes that sentence out of its cards, and looks for another in every
source. All three can be undone under Cards, Suspended.

The data lives in one folder, `~/.local/share/sumitsubo` (or wherever `SUMITSUBO_DATA` points),
not in the program folder. Updating the program does not touch it.

## Kinds of source

A video link, a link to a PDF or article, a local file (PDF, EPUB, text, Markdown, subtitles,
audio, video), pasted text, an Aozora Bunko link, or photographs of printed pages. Sources can
carry tags and a series name.

### Speech recognition

Videos without captions, and recordings, need a Whisper model. Install it from the Settings page
("Install speech model", about 3 GB for large-v3), which runs Anki Miner's own installer, including
the parts for an NVIDIA card.

### Scanned PDFs

A PDF that is only page images has no text for Anki Miner to read. Sumitsubo then runs
[OCRmyPDF](https://github.com/ocrmypdf/OCRmyPDF) with Tesseract's Japanese models: horizontal text
first, and vertical text if that finds little Japanese. This is an optional install:

    sudo apt install ocrmypdf tesseract-ocr-jpn tesseract-ocr-jpn-vert

Without it, a scan ends with a message saying what to install. A source read this way is marked on
its page. Character recognition makes mistakes, and a misread character can turn into a real but
wrong word (鉋 read as 錠, say) and get a card: use "Suspend" for those, and "Bad sentence" where
the word is right but its sentence is garbled.

### Page photos

Books and printed papers come in through the phone's camera.

    sumitsubo serve --phone

1. Open **Add a source**, then **Page photos**, on the computer. It shows a code; scan it with the phone (same Wi-Fi). The phone
   opens a page with two buttons: photograph a page, or choose photos (or a PDF from a scanner app).
2. Each photo appears on that page as it arrives and is read in the background. It gets a verdict:
   *read well*, or *left out* with the reason.
3. Tick the pages that belong together, put them in order with the arrows, give a title, and press
   **Add the ticked pages**. For a book read over months, each chapter can be its own source in one
   series, or later pages can be added to the end of an existing paper source.

Pages are read by [NDLOCR-Lite](https://github.com/ndl-lab/ndlocr-lite), the National Diet Library's
OCR for Japanese books and magazines. It runs on this computer, without a graphics card, in a few
seconds a page. It finds the blocks of text on the page and leaves the pictures alone, tells body
text from titles, page numbers, running heads and furigana (the last three are dropped), and reads
vertical and horizontal lines alike, so a page may mix them. `sumitsubo setup` fetches a
pinned version (about 160 MB) from GitHub into the data folder; it is not part of this package. `sumitsubo paper` fetches it by itself. Without it pages are read by Tesseract (`sudo apt install tesseract-ocr-jpn
tesseract-ocr-jpn-vert`), which on real book pages got so little right that nothing usable was left.

Nothing is corrected by hand; the point is that it costs no time. Instead, what cannot be trusted
is left out:

- **A line** gets a confidence from the reader. Body text read correctly sits around 94 of 100; a
  strip of the facing page, or a coloured label, falls well below.
- **A sentence** that runs through a line below 75 is left out. Titles are held to a higher mark.
- **A page** whose average is below 75, or with almost no Japanese on it, is left out of the source
  and named on the source's page, to be photographed again and added.
- A column printed in two pieces (a label such as 【歴史】 and the text under it) is put back in
  order by position. When such a label is short and was read with little confidence, the label is
  dropped and the sentence under it is kept.

The "text" link under a page shows what was read, with doubted words in ⟦brackets⟧.

`--phone` makes the program listen on the local network, and it then answers other devices only on
the upload page, whose address carries a code; the library, cards and settings stay reachable from
this computer alone. If the phone cannot open the page, the network is keeping devices apart
(common on campus Wi-Fi) or a firewall blocks port 8378 (`sudo ufw allow 8378/tcp`): use the
phone's hotspot, or move the photos to the computer and use "From this computer" on the same page.

For good photos: one page per photo, filling the frame. Two real photographs taken at an angle, in
uneven light, one with a thumb on the page, were read with every body sentence correct.

### Text that is not a sentence

Text from a PDF, a web page or a photographed page comes with debris. Each sentence is cleaned as
it is read: stray symbols before its first word, a scrap of letters left by a formula, half a
bracket or quotation mark. A text that is not a sentence at all is set aside, with the reason: an
entry of the reference list, a title or label with no hiragana, a flattened formula or table, text
that is mostly not Japanese. Its words still count for the source, but it never carries a card.
What was set aside is listed under Cards, Suspended, Bad sentences, and "Use again" brings one
back for good. `sumitsubo tidy` runs the same check over everything already in the library. Books from Aozora Bunko and sentences you saved in the reader are taken as
they are. A wrong character inside an otherwise good sentence (a misread kanji) is not something
this can see: mark that one as a bad sentence.

### Books from Aozora Bunko, and books for the reader

Paste the link of a work's page on [Aozora Bunko](https://www.aozora.gr.jp/) (the page that lists
its files, or the link of the zip file itself). The program takes the text file, or the XHTML page
when there is none; the `.ebk` file is for an old viewer and is not used. It reads Aozora's notation:
readings written as 漢字《かんじ》, notes in ［＃…］, headings, page breaks, and characters outside
Shift_JIS described after ※.

Two things come out of it:

- **Sentences for the cards**, like any source. Where the edition gives a reading and the dictionary
  knows the word with that reading, it is used instead of the parser's guess (今日《こんにち》 is not
  read きょう). This holds for the sentence the reading is written in. Elsewhere in the book it is
  carried over only when the two readings are one dictionary word said two ways (私 as わたくし
  throughout こころ), never when they are two words (方 as かた in one place, ほう in another).
- **An EPUB** with vertical text, the edition's furigana, emphasis dots, side lines (as
  underlines), bold and italic type, larger and smaller type, framed passages (罫囲み), indented
  passages, the pictures the text places, one file per chapter, and the edition's credits on the
  last page. Download it from the source's page. Aozora's notation has no tables: an edition that
  needs one draws it as a picture, which is carried over like any other. The kinds of emphasis
  mark (white sesame, circles, triangles) all become the one dot, and the kinds of side line
  (double, dotted, wavy) the one line.

Tick "Also send it to the reader device's library" when adding (or later, on the source's page) and
the next `sumitsubo sync` puts the book in the `Books` folder of the device's card. The same box
works for EPUB, text and Markdown files you add, which are sent as they are. Other kinds of source
(videos, PDFs, web articles, page photos) are not sent. A book is sent once; removing the source
here does not remove it from the device.

While a source is read, the Library shows which step it is on (step 2 of 4: getting the text,
analysing, storing the words, making cards; a fifth, cutting audio clips, for a recording) and a bar
for how far that step is. A test book of 6,000 sentences took about 35 seconds on the machine this
was written on.

If a step reports nothing for two minutes, the program writes where it is to `stuck.log` in the data
folder, even when it no longer answers the browser. If a source never finishes, that file says why.

## Importing your progress

Settings, Imports brings in what you have studied elsewhere. Cards arrive with their memory state
and due date, so nothing starts again from zero and difficulty rests on your real progress.

- **Anki:** a deck or collection file exported with "Include scheduling information" (.apkg or
  .colpkg, current and older formats), or a running Anki through the AnkiConnect add-on. Where the
  review history is present it is replayed through the scheduler and kept; otherwise the state
  Anki's FSRS stored is used; otherwise interval and ease are converted. AnkiWeb is never
  contacted; its terms allow only Anki's own apps to sync with it.
- **Renshuu:** your API key (Renshuu menu, Tools, Renshuu API), used once and not saved, or a saved
  export file. There is no dated history, so each state is an estimate from the mastery
  percentage. Vocabulary and kanji schedules are read; grammar and sentence schedules are not.
  A kanji studied as a character gives its state to that kanji's readings here. Renshuu keeps
  example sentences apart from words (a word's entry holds meanings only), so they cost one
  request per word; you can ask for a limited number, and Renshuu caps requests per day.
- **A list:** one word per line, or a table with word, reading and an optional mastery percentage.

Each deck gets its own choice: leave out; progress only (the default: nothing is added to study,
and a word gets a card when it turns up in your own material); progress plus the studied cards;
or everything, including cards not started. Your own marks outrank an import, and an import
outranks the level. An import can be changed or removed later; removing it puts things back.

From the command line: `sumitsubo import deck.apkg`, `sumitsubo import deck.apkg --take studied`,
`sumitsubo import --renshuu-key KEY`.

Fetching from Renshuu, reading from a running Anki, or replaying a long review history can take
minutes. These run in the background with a progress page that names the current step and moves
on by itself when done.

## The reader device

A [Sumitsubo Reader](https://github.com/Temigo/sumitsubo-reader) (Matcha Reader with a Study mode) is where the cards are
reviewed. This program prepares its card and exchanges with it.

    sumitsubo card-setup /media/card     # once: Matcha's Japanese dictionary and fonts (225 MB, from Matcha's release files), and the deck
    sumitsubo sync /media/card           # the card in a card reader
    sumitsubo sync                       # over Wi-Fi, with the device on File Transfer (http://sumitsubo.local)

The Reader page has the same thing as a button, with a box for the address or the folder. A sync:

- **takes in the reviews**, of word, kanji and collocation cards and of contrast cards (each
  contrast card has its own state);
- **takes in the marks**: suspended, known, and "bad sentence", which removes that sentence from
  every card as the button on the Cards page does;
- **takes in the words you saved** in the reader's dictionary (Matcha's
  `sentence-mining/sentences-ja.csv`). A word that has a card is introduced before all other new
  cards. A word saved in a book this program has needs nothing more. For any other book, the
  saved sentences become a source named after the book (one per book), so the word gets its card
  there.;
- **sends the deck**, the books marked for the reader, and what the reader needs to know about
  your words: which have a card (they are not saved again), which words of each book are new (they
  can be marked on the page), and which readings you know well (their furigana can be left out).
  It also sends `forms.tsv`, with which the reader keeps those three up to date from the reviews
  you do between syncs.
  And `big.bin`: the characters of the words shown large on kanji cards, drawn at their real size
  (88 pixels, four greys) from a Japanese serif font found on this computer, because the reader
  cannot draw outlines itself and its own letters enlarged look ragged. About 0.9 KB a character
  (1.7 MB for 2,000). The font is found with `fc-match` (Noto Serif CJK JP if installed, else
  another Japanese font), or named in the setting `big_font`; without one the file is not written
  and the reader enlarges its own font as before.

## Back to Anki, with your progress

Export (on the Cards page or a source's page) has "With my progress", on by default; on the
command line, `sumitsubo export-anki out.apkg --progress`. Each card that has a memory state
then arrives in Anki as a review card due on its day, with its stability and difficulty (Anki's
scheduler, FSRS, uses them directly) and its past reviews: those made on the reader, taken from a
copy of the device's log kept at each sync, and those that came with an import. Cards never
studied arrive as new cards. In Anki, import with "Import any learning progress" on, and have
FSRS enabled in the deck options. The file follows Anki's format as documented; its contents are
checked by the tests, but it has not been opened in Anki itself.

The whole device log is read each time and gives the same result each time.

## Command line

sumitsubo add "https://www.youtube.com/watch?v=..." --tags "physics/detector, seminar"
    sumitsubo add paper.pdf --tags math/optimization
    sumitsubo add talk.srt --media talk.mp3        # subtitles with their recording
    sumitsubo add --text "検出器の較正について発表を始めます。"
    sumitsubo add https://www.aozora.gr.jp/cards/000148/card773.html --reader   # a book: cards here, an EPUB to the device at the next sync
    sumitsubo paper                                # fetch the reader for photographed pages by itself
    sumitsubo serve --phone                        # the interface, plus an upload page for a phone
    sumitsubo sync                                 # with a Sumitsubo Reader on File Transfer: take in its reviews, send the deck and new books
    sumitsubo sync /media/card                     # the same through a card reader
    sumitsubo card-setup /media/card               # once per card: Japanese dictionary and fonts for the reader, and the deck
    sumitsubo status                               # every source with its difficulty
    sumitsubo score book.epub                      # difficulty without adding
    sumitsubo level 2                              # N2 and below: in review with a long interval
    sumitsubo glossary physics.tsv                 # term <TAB> reading <TAB> meaning
    sumitsubo pairs lab-pairs.tsv                  # collocations: noun <TAB> particle <TAB> verb
    sumitsubo priority 3 1                         # source 3 first
    sumitsubo contrast                             # list the contrast cards
    sumitsubo frequency                            # download the frequency list once, or: frequency FILE
    sumitsubo import core.apkg                     # progress from an Anki deck, with its review history
    sumitsubo export-anki all.apkg
    sumitsubo export-anki physics.apkg --tag physics --deck Physics
    sumitsubo export-anki mixed.apkg --source 1 --source 3 --one-deck --kind word
    sumitsubo pack

## How it works

1. **Ingest.** Anki Miner's `--api` commands download videos, transcribe speech, and read books
   into sentences. Sumitsubo calls them as a separate program.
2. **Words.** Anki Miner's parser, imported as a library, splits each sentence into dictionary
   words (it joins 走り出した into 走り出す, 考慮に入れる into one expression). It asks us which
   strings are words; we answer from JMdict plus your glossaries, so glossary terms are joined too.
3. **Kanji readings.** Each word's reading is split over its kanji (発表 → 発 はつ, 表 ひょう), and
   each kanji-reading pair is tracked as its own item.
4. **What you know.** Each item has a memory state, as in Anki: New, Learning, or Review. A stated
   level puts JLPT words at or below it, and readings of kanji at or below it, in review with a
   long interval. Imports and your own marks take precedence.
5. **Cards and order.** Every dictionary-confirmed item gets its cards. New ones are ranked by how
   often they occur in the source, and the reader releases them within a daily budget; the
   rest come back when due.

Words found in no dictionary get no card, which keeps transcription mistakes out. They are listed
on the source's page; add real field terms to a glossary and process the source again.

## Known limits

- Japanese only. English glosses by default; another dictionary in Yomitan format can be added in
  Settings and chosen for meanings.
- A kanji's JLPT level comes from Jonathan Waller's lists, which are unofficial (no official list exists). A kanji not on them falls back to KANJIDIC's older four-level scale.
- Words whose reading cannot be split by kanji (今日, 大人, 日本) produce no kanji-reading items.
- Grammar points are not detected. Short kana-only grammatical expressions are set aside.
- Coverage counts content words as the parser reports them; particles and names are left out.
- Page photos: one page per photo. A photo of an open book (two pages) is not split, and a sideways
  photo must be turned with the "Turn" button. A reading printed in the line itself, as in
  錦部家(にしごりけ), stays in the sentence. Formulas are not text and their sentences are mostly
  dropped. With Tesseract instead of NDLOCR-Lite, expect little from real book pages.

- Aozora Bunko: the kinds of emphasis mark all become one dot and the kinds of side line one line;
  the notation has no tables. Characters outside the device's built-in
  font show as a box there until the Japanese fonts are on its card (`sumitsubo card-setup`).
- Reader link: what the device is told about your words is kept in small filters that are wrong
  about once in a few thousand words.
