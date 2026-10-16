"""Реестр движков, выбор голоса и синтез."""

import json
import os
import time
import uuid
from pathlib import Path

from .audio import Audio
from .engines.base import Engine, EngineError, ModelError, Voice
from .engines.clone import make_engines as clone_engines
from .engines.kokoro import Kokoro
from .engines.piper import Piper
from .engines.rhvoice import RHVoice
from .engines.silero import Silero
from .paths import CONFIG, OUT
from .text import LANGS, detect_lang, strip_emphasis

ENGINES: dict[str, Engine] = {e.name: e for e in (Silero(), Piper(), RHVoice(), Kokoro(), *clone_engines())}
# порядок предпочтения по языкам: дефолтный голос, выбор голоса движка, замена голоса под язык текста
PREFERRED = {
    "ru": ["silero:xenia", "piper:irina", "rhvoice:anna", "silero:aidar", "piper:denis", "rhvoice:aleksandr"],
    "en": ["kokoro:af_heart", "silero:en_0", "piper:lessac", "rhvoice:slt",
           "kokoro:am_michael", "silero:en_2", "piper:ryan", "rhvoice:bdl"],
    # по замеру Whisper: silero:eva_k 3.8% WER, thorsten — самый естественный мужской; kerstin/ramona (low) — хуже
    "de": ["silero:eva_k", "piper:thorsten", "silero:karlsson", "silero:hokuspokus", "piper:eva_k", "piper:pavoque",
           "silero:bernd_ungerer", "piper:kerstin"],
}  # fmt: skip
DEFAULT_DOWNLOADS = [
    "silero:xenia", "piper:irina", "piper:denis", "rhvoice:anna", "rhvoice:aleksandr",
    "kokoro:af_heart", "silero:en_0", "piper:lessac", "piper:ryan", "rhvoice:slt", "rhvoice:bdl",
    "silero:eva_k", "piper:thorsten",
]  # fmt: skip


def load_config() -> dict:
    try:
        return json.loads(CONFIG.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def save_config(cfg: dict) -> None:
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))


def all_voices(engine: str | None = None, lang: str | None = None) -> list[Voice]:
    engines = [ENGINES[engine]] if engine else ENGINES.values()
    return [v for e in engines for v in e.voices() if not lang or v.lang in (lang, "any")]


def voice_info(eng: Engine, name: str) -> Voice:
    """Voice для любого имени, включая некаталожные silero en_N."""
    return eng.voice(name) or Voice(eng.name, name, "", eng.is_installed(name), "en" if name.startswith("en_") else "ru")


def default_voice(lang: str = "ru") -> str:
    cfg = load_config()
    explicit = os.environ.get("LTTS_VOICE" if lang == "ru" else f"LTTS_VOICE_{lang.upper()}")
    explicit = explicit or cfg.get("default_voices", {}).get(lang) or (cfg.get("default_voice") if lang == "ru" else None)
    if explicit:
        return explicit
    for vid in PREFERRED[lang]:
        eng, name = vid.split(":")
        if ENGINES[eng].status()[0] and ENGINES[eng].is_installed(name):
            return vid
    installed = [v.id for v in all_voices(lang=lang) if v.installed and v.lang == lang]
    if installed:
        return installed[0]
    usable = [p for p in PREFERRED[lang] if ENGINES[p.split(":")[0]].installable()]  # не kokoro на Linux
    return (usable or PREFERRED[lang])[0]


def engine_voice(eng: Engine, lang: str, gender: str = "") -> str | None:
    """Лучший голос движка для языка (и пола): установленный из предпочтительных, затем любой."""
    cands = [v for v in eng.voices() if v.lang in (lang, "any") and (not gender or v.gender == gender)]
    if not cands:
        return None
    pref = [p.split(":")[1] for p in PREFERRED[lang] if p.startswith(eng.name + ":")]
    cands.sort(key=lambda v: (not v.installed, pref.index(v.name) if v.name in pref else len(pref)))
    return cands[0].name


def resolve(voice: str | None, lang: str = "ru") -> tuple[Engine, str]:
    """'silero:xenia' | 'xenia' | 'silero' | None -> (движок, голос); lang — для None и «только движка»."""
    voice = (voice or default_voice(lang)).strip().lower()
    if ":" in voice:
        eng, name = voice.split(":", 1)
        if eng not in ENGINES:
            raise EngineError(f"неизвестный движок {eng!r}, есть: {', '.join(ENGINES)}")
        if not ENGINES[eng].has_voice(name):
            names = ", ".join(v.name for v in ENGINES[eng].voices())
            raise EngineError(f"у {eng} нет голоса {name!r}, есть: {names}")
        return ENGINES[eng], name
    if voice in ENGINES:  # только движок -> его лучший голос для языка
        eng = ENGINES[voice]
        name = engine_voice(eng, lang) or engine_voice(eng, "ru") or next((v.name for v in eng.voices()), None)
        if not name:
            raise EngineError(f"у {eng.name} пока нет голосов" + (" — `ltts clone add <имя> <образец.wav>`" if eng.name.startswith("clone") else ""))
        return eng, name
    matches = [v for v in all_voices() if v.name == voice]
    if not matches:
        raise EngineError(f"голос {voice!r} не найден — смотри `ltts voices`")
    # сначала установленные, затем по порядку движков
    matches.sort(key=lambda v: (not v.installed, list(ENGINES).index(v.engine)))
    return ENGINES[matches[0].engine], matches[0].name


def download(voice_id: str, force: bool = False) -> Voice:
    eng, name = resolve(voice_id)
    if force:
        eng.reset(name)
    if not eng.is_installed(name):
        eng.download(name)
    return next(v for v in eng.voices() if v.name == name)


def synthesize(
    text: str, voice: str | None = None, speed: float = 1.0, lang: str | None = None, auto_download: bool = True
) -> tuple[Audio, dict]:
    """lang: ru|en — язык текста; None/auto — определить по тексту и при необходимости
    заменить голос на голос того же движка и пола для нужного языка (xenia + английский -> silero:en_0)."""
    text = text.strip()
    if not text:
        raise EngineError("пустой текст")
    if not 0.25 <= speed <= 4.0:
        raise EngineError("speed должен быть от 0.25 до 4.0")
    auto = lang in (None, "", "auto")
    if not auto and lang not in LANGS:
        raise EngineError(f"lang должен быть auto или одним из: {', '.join(LANGS)}")
    lang = detect_lang(text) if auto else lang
    eng, name = resolve(voice, lang)
    requested = f"{eng.name}:{name}"
    info = voice_info(eng, name)
    if info.lang not in (lang, "any") and (auto or not voice or voice.strip().lower() in ENGINES):
        swap = engine_voice(eng, lang, info.gender) or engine_voice(eng, lang)
        if swap:
            name = swap
        else:  # у движка нет этого языка (kokoro + немецкий) — лучший голос того же пола из других движков
            for vid in PREFERRED[lang]:
                e, n = vid.split(":")
                cand = ENGINES[e].voice(n)
                if cand and ENGINES[e].installable() and (not info.gender or cand.gender == info.gender):
                    eng, name = ENGINES[e], n
                    break
    if not eng.is_installed(name):  # скачивание голоса заодно доводит движок до рабочего состояния (сборка RHVoice)
        if not auto_download or not eng.installable():
            raise EngineError(f"{eng.name}: {eng.status()[1] or f'голос {name} не скачан — `ltts download {eng.name}:{name}`'}")
        eng.download(name)
    ok, why = eng.status()
    if not ok:
        raise EngineError(f"{eng.name}: {why}")
    if not eng.supports_emphasis(name):
        text = strip_emphasis(text)
    t = time.perf_counter()
    try:
        audio = eng.synth(text, name, speed)
    except ModelError:
        if not auto_download:
            raise
        eng.download(name)  # битый файл уже удалён движком — качаем заново и пробуем ещё раз
        t = time.perf_counter()
        audio = eng.synth(text, name, speed)
    meta = {
        "engine": eng.name,
        "voice": f"{eng.name}:{name}",
        "lang": lang,
        **({"requested_voice": requested} if requested != f"{eng.name}:{name}" else {}),
        "duration": round(audio.duration, 3),
        "sample_rate": audio.sample_rate,
        "synth_seconds": round(time.perf_counter() - t, 3),
    }
    return audio, meta


# порядок ключей в JSON одинаковый для local/server/MCP — удобно диффать и парсить
META_ORDER = ["ok", "path", "format", "bytes", "engine", "voice", "requested_voice", "lang",
              "duration", "sample_rate", "synth_seconds", "via", "fallback"]  # fmt: skip
RATES = (8000, 11025, 16000, 22050, 24000, 32000, 44100, 48000)


def ordered(meta: dict) -> dict:
    return {**{k: meta[k] for k in META_ORDER if k in meta}, **{k: v for k, v in meta.items() if k not in META_ORDER}}


def default_rate() -> int | None:
    raw = os.environ.get("LTTS_RATE") or load_config().get("sample_rate")
    return int(raw) if raw else None


def render(
    text: str, voice: str | None = None, speed: float = 1.0, lang: str | None = None, fmt: str = "wav", rate: int | None = None
) -> tuple[bytes, dict]:
    """synthesize + приведение частоты + кодирование. rate=None — родная частота движка (или LTTS_RATE)."""
    from .audio import ALL_FORMATS

    if fmt.lower() not in ALL_FORMATS:
        raise EngineError(f"формат {fmt!r} не поддерживается, есть: {', '.join(ALL_FORMATS)}")
    rate = rate or default_rate()
    if rate and rate not in RATES:
        raise EngineError(f"rate должен быть одним из: {', '.join(map(str, RATES))}")
    audio, meta = synthesize(text, voice, speed, lang)
    out = audio.for_format(fmt, rate)
    data = out.encode(fmt)
    meta.update(format=fmt.lower(), bytes=len(data), sample_rate=out.sample_rate, duration=round(out.duration, 3))
    return data, ordered(meta)


OUT_KEEP = int(os.environ.get("LTTS_OUT_KEEP", "200"))


def out_path(fmt: str) -> Path:
    """Новый файл в OUT; старые ltts-* сверх OUT_KEEP (по умолчанию 200) удаляются."""
    OUT.mkdir(parents=True, exist_ok=True)
    if OUT_KEEP > 0:
        old = sorted(OUT.glob("ltts-*"), key=lambda p: p.stat().st_mtime, reverse=True)[OUT_KEEP - 1 :]
        for p in old:
            p.unlink(missing_ok=True)
    return OUT / f"ltts-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}.{fmt}"


def verify_models() -> dict:
    """Сверить sha256 каждого скачанного файла с маркером; битые удалить."""
    from .engines.base import invalidate, verify
    from .paths import MODELS

    checked, corrupt = 0, []
    for marker in MODELS.rglob("*.complete"):
        path = marker.with_name(marker.name.removesuffix(".complete"))
        checked += 1
        if not verify(path):
            corrupt.append(str(path.relative_to(MODELS)))
            invalidate(path)
    return {"checked": checked, "corrupt": corrupt}


def doctor() -> dict:
    from . import __version__
    from .paths import HOME

    engines = {}
    for e in ENGINES.values():
        ok, why = e.status()
        engines[e.name] = {
            "title": e.title,
            "ok": ok,
            "installable": e.installable(),
            "hint": why,
            "installed_voices": [v.id for v in e.voices() if v.installed],
        }
    defaults = {lang: default_voice(lang) for lang in LANGS}
    return {"version": __version__, "home": str(HOME), "default_voice": defaults["ru"], "default_voices": defaults, "engines": engines}
