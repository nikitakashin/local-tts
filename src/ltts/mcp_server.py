"""MCP-сервер (stdio): инструменты speak / list_voices для агентов."""

from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .cli import play
from .client import synth_auto
from .core import all_voices, default_voice, ordered, out_path
from .text import LANGS

mcp = MCPServer(
    "ltts",
    instructions=(
        "Локальный синтез речи на русском, английском и немецком (Silero, Piper, RHVoice, Kokoro, клоны). "
        "Язык определяется по тексту, голос подбирается того же движка и пола. "
        "speak сохраняет аудиофайл и возвращает путь; play=true — сразу проиграть на этом компьютере. "
        "Голос задаётся как engine:voice (silero:xenia, kokoro:af_heart, piper:denis, rhvoice:anna); list_voices покажет все. "
        "Русский Silero понимает выделение слова голосом: **слово** (в вопросе да/нет — *слово*); другие голоса звёздочки вырезают. "
        "clone_voice клонирует голос по образцу — дальше voice=clone:<имя> (или clonehq:<имя> для 48 кГц)."
    ),
)


@mcp.tool()
def speak(
    text: str,
    voice: str | None = None,
    speed: float = 1.0,
    play_audio: bool = False,
    output_path: str | None = None,
    format: str = "wav",
    lang: str = "auto",
    sample_rate: int | None = None,
) -> dict:
    """Озвучить текст. Возвращает путь к файлу и метаданные (длительность, голос).

    voice: engine:voice, просто имя голоса или имя движка; пусто — голос по умолчанию.
    speed: 0.25–4.0. format: wav|mp3|ogg|opus|flac. play_audio: проиграть через динамики.
    lang: auto|ru|en|de — auto сам определит язык и подберёт голос под него.
    sample_rate: частота на выходе (например 48000), чтобы склеивать файлы разных движков; пусто — родная.
    """
    try:
        data, meta = synth_auto(text, voice, speed, format, lang, sample_rate)
    except Exception as e:  # ToolError — чтобы агент увидел причину, а не «Error executing tool»
        raise ToolError(str(e)) from e
    path = Path(output_path).expanduser().resolve() if output_path else out_path(format)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if play_audio:
        play(path)
    return ordered({**meta, "ok": True, "path": str(path)})


@mcp.tool()
def list_voices(engine: str | None = None, lang: str | None = None, installed_only: bool = False) -> dict:
    """Список голосов. engine: silero|piper|rhvoice|kokoro|clone|clonehq, lang: ru|en|de. Нескачанные голоса скачаются при первом speak."""
    try:
        voices = [v.to_dict() for v in all_voices(engine, lang) if v.installed or not installed_only]
    except KeyError:
        raise ToolError("engine должен быть silero, piper или rhvoice") from None
    return {"defaults": {lg: default_voice(lg) for lg in LANGS}, "voices": voices}


@mcp.tool()
def clone_voice(name: str, audio_path: str, text: str = "", gender: str = "", overwrite: bool = False) -> dict:
    """Клонировать голос по образцу речи (8–20 с, wav/mp3/flac/ogg). Потом speak(voice="clone:<имя>").

    text: что сказано в образце — улучшает похожесть и нужен для HQ (voice="clonehq:<имя>", 48 кГц).
    Клонируй только свой голос или с явного согласия владельца.
    """
    from pathlib import Path as _P

    from . import clones

    try:
        c = clones.add(name, _P(audio_path).expanduser().read_bytes(), text, gender, overwrite)
    except Exception as e:
        raise ToolError(str(e)) from e
    return {"voice": f"clone:{c.slug}", "hq_voice": f"clonehq:{c.slug}" if c.text else None, **c.to_dict()}


def run() -> None:
    mcp.run()
