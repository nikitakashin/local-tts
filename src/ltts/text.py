import re

from num2words import num2words

_NUM = re.compile(r"(?<![\w])(\d+(?:[.,]\d+)?)(\s*%)?")
_SENT = re.compile(r"(?<=[.!?…;])\s+|\n+")


_CYR = re.compile(r"[А-Яа-яЁё]")
_LAT = re.compile(r"[A-Za-zÄÖÜäöüß]")
_WORD = re.compile(r"[A-Za-zÄÖÜäöüß']+")
LANGS = ("ru", "en", "de")

# частые служебные слова: по ним латиница делится на английский и немецкий
_DE_WORDS = set("""und der die das ist nicht ich du er sie es wir ihr mit auf für von zu den dem des ein eine einen einem
    eines auch sich auf noch nur wie wenn aber oder so schon kann muss mehr sehr hier dann jetzt heute bitte danke ja nein
    gut ganz habe hat haben sind bin war waren wird werden wurde mein dein sein unser über nach bei aus um vor durch ohne
    gegen weil dass als wo was wer warum doch mal immer wieder""".split())
_EN_WORDS = set("""the and is are was were be been to of in on at for with that this it you he she we they not have has
    had do does did will would can could should a an or but if then so just very here there what who why how my your
    our their from by as about into over after before because which when where""".split())


def detect_lang(text: str) -> str:
    """ru — если заметная доля кириллицы (английские слова в русской фразе — норм).
    Латиница: de, если есть умлауты/ß или немецких служебных слов больше английских, иначе en."""
    cyr, lat = len(_CYR.findall(text)), len(_LAT.findall(text))
    if not lat or cyr * 10 >= lat:
        return "ru"
    words = [w.lower() for w in _WORD.findall(text)]
    de = sum(w in _DE_WORDS for w in words) + 2 * len(re.findall(r"[äöüÄÖÜß]", text))
    en = sum(w in _EN_WORDS for w in words)
    return "de" if de > en else "en"


def numbers_to_words(text: str, lang: str = "ru") -> str:
    """«В 2026 году 3,5%» -> «В две тысячи двадцать шесть году три целых пять десятых процентов».
    Падежи не согласуются, но лучше, чем молча проглоченные цифры."""
    if lang == "en":
        text = re.sub(r"(?<=\d),(?=\d{3}\b)", "", text)  # 1,000 -> 1000
    elif lang == "de":
        text = re.sub(r"(?<=\d)\.(?=\d{3}\b)", "", text)  # 1.000 -> 1000 (в немецком точка — разделитель тысяч)
    percent = {"ru": " процентов", "en": " percent", "de": " Prozent"}

    def repl(m: re.Match) -> str:
        raw = m.group(1)
        raw = raw.replace(",", ".") if lang in ("ru", "de") else raw  # десятичная запятая
        try:
            words = num2words(float(raw) if "." in raw else int(raw), lang=lang)
        except Exception:
            return m.group(0)
        return words + (percent.get(lang, "") if m.group(2) else "")

    return _NUM.sub(repl, text)


_EMPH = re.compile(r"(\*\*?)(?=\S)([^*\n]+?)(?<=\S)\1")


def strip_emphasis(text: str) -> str:
    """«эта булочка **очень** вкусная» -> «эта булочка очень вкусная»: для движков без эмфаз.
    Одиночные звёздочки (2 * 3) не трогаем."""
    return _EMPH.sub(r"\2", text)


_LAT2 = {"sh": "ш", "ch": "ч", "zh": "ж", "th": "т", "ph": "ф", "kh": "х", "ts": "ц", "ya": "я", "yu": "ю",
         "yo": "ё", "ee": "и", "oo": "у", "ck": "к", "qu": "кв", "wh": "в"}  # fmt: skip
_LAT1 = {"a": "а", "b": "б", "c": "к", "d": "д", "e": "е", "f": "ф", "g": "г", "h": "х", "i": "и", "j": "дж",
         "k": "к", "l": "л", "m": "м", "n": "н", "o": "о", "p": "п", "q": "к", "r": "р", "s": "с", "t": "т",
         "u": "у", "v": "в", "w": "в", "x": "кс", "y": "и", "z": "з"}  # fmt: skip
_LATWORD = re.compile(r"[A-Za-z]+")


def latin_to_cyrillic(text: str) -> str:
    """Грубая транслитерация латиницы: silero её иначе просто проглатывает (world -> ворлд)."""

    def word(m: re.Match) -> str:
        w, out, i = m.group(0).lower(), [], 0
        while i < len(w):
            if w[i : i + 2] in _LAT2:
                out.append(_LAT2[w[i : i + 2]])
                i += 2
            else:
                out.append(_LAT1[w[i]])
                i += 1
        return "".join(out)

    return _LATWORD.sub(word, text)


def chunks(text: str, limit: int) -> list[str]:
    """Режет текст по предложениям в куски не длиннее limit символов."""
    out: list[str] = []
    cur = ""
    for sent in filter(None, (s.strip() for s in _SENT.split(text))):
        while len(sent) > limit:  # длинное предложение режем по запятым/пробелам
            cut = max(sent.rfind(",", 0, limit), sent.rfind(" ", 0, limit))
            cut = cut if cut > 0 else limit
            if cur:
                out.append(cur)
                cur = ""
            out.append(sent[: cut + 1].strip())
            sent = sent[cut + 1 :].strip()
        if cur and len(cur) + 1 + len(sent) > limit:
            out.append(cur)
            cur = sent
        else:
            cur = f"{cur} {sent}".strip()
    if cur:
        out.append(cur)
    return out
