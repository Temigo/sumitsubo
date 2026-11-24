"""Register: how formal a word or a sentence is, and the keigo verbs.

Three things, all for recognising who is doing what to whom when you read or listen:

  a label on words          from the dictionary's own tags: respectful, humble, polite, formal, colloquial
  a label on sentences      plain (だ / である), polite (です / ます), keigo (respectful or humble forms),
                            from the sentence's wording. A rough label: it catches the common forms.
  the keigo verbs           the plain verbs that have their own respectful and humble counterparts,
                            a short hand-written table, used for keigo contrast cards
"""
import re

# ---- words: the dictionary's tags, in the short form jmdict-simplified uses and the long form of the XML
WORD = [("respectful", ("hon", "honorific or respectful (sonkeigo) language")),
        ("humble", ("hum", "humble (kenjougo) language")),
        ("polite", ("pol", "polite (teineigo) language")),
        ("formal", ("form", "formal or literary term")),
        ("colloquial", ("col", "colloquialism", "colloquial"))]
LABEL = dict(respectful="respectful", humble="humble", polite="polite", formal="formal", colloquial="colloquial",
             plain="plain", keigo="respectful or humble")


def of_tags(tags) -> str | None:
    """The register a dictionary entry carries, from its tags; the strongest one if it has several."""
    tags = set(tags or ())
    for name, keys in WORD:
        if tags & set(keys):
            return name
    return None


# ---- sentences
_KEIGO = re.compile(
    r"おります|ております|居ります|いたしま|いたします|(?<![一-鿿])致しま|ございま|申し上げ|申しま|参りま|伺[いうえ]|拝見|拝読|拝聴|拝借"
    r"|(?<![一-鿿])存じ|いらっしゃ|おっしゃ|仰[いっる]|なさ[るっ]|なさいま|ご覧|召し上が|お目にかか|差し上げ|いただ[きくけい]|頂[きくけい]"
    r"|お[一-鿿][一-鿿ぁ-ん]{0,4}にな[るりっ]|ご[一-鿿]{2,3}にな[るりっ]"
    r"|(?:お[一-鿿][一-鿿ぁ-ん]{0,4}|ご[一-鿿]{2,3})(?:ください|下さい|いたし|致し|申し上げ)")
_POLITE = re.compile(r"(?:です|ます|ました|ません|でした|でしょう|ましょう|ください|下さい)(?:か|ね|よ|が|けれど|けど|ので|から)?[、。．！？!?」』）\s]|(?:です|ます|ました|ません|でした|でしょう|ましょう|ください|下さい)(?:か|ね|よ)?$")


def of_sentence(text: str) -> str:
    """plain, polite or keigo."""
    if _KEIGO.search(text):
        return "keigo"
    return "polite" if _POLITE.search(text) else "plain"


# ---- the keigo verbs: plain, then respectful (what someone you respect does), then humble (what you do).
# Each cell lists the spellings it may be met in; an empty cell means the ordinary お〜になる / お〜する pattern is used.
KEIGO = [
    (["言う"], ["おっしゃる", "仰る"], ["申す", "申し上げる"]),
    (["行く"], ["いらっしゃる"], ["参る"]),
    (["いる", "居る"], ["いらっしゃる"], ["おる", "居る"]),
    (["する", "為る"], ["なさる", "為さる"], ["いたす", "致す"]),
    (["見る"], ["ご覧になる", "御覧になる"], ["拝見"]),
    (["食べる"], ["召し上がる"], ["いただく", "頂く"]),
    (["知る"], ["ご存じ", "御存じ", "ご存知"], ["存じる"]),
    (["聞く"], [], ["伺う"]),
    (["会う"], [], ["お目にかかる", "お目に掛かる"]),
    (["もらう", "貰う"], [], ["いただく", "頂く"]),
    (["あげる", "上げる"], [], ["差し上げる"]),
    (["くれる", "呉れる"], ["くださる", "下さる"], []),
    (["読む"], [], ["拝読"]),
    (["借りる"], [], ["拝借"]),
    (["見せる"], [], ["お目にかける", "お目に掛ける"]),
    (["思う"], [], ["存じる"]),
]
ROLES = ("plain", "respectful", "humble")
