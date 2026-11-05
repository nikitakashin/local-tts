import hashlib
import json
import os
import shutil
import sys
import tarfile
import tempfile
import threading
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

from ..audio import Audio


@dataclass
class Voice:
    engine: str
    name: str
    gender: str = ""
    installed: bool = False
    lang: str = "ru"
    note: str = ""

    @property
    def id(self) -> str:
        return f"{self.engine}:{self.name}"

    def to_dict(self) -> dict:
        return {"id": self.id, **asdict(self)}


class EngineError(RuntimeError):
    """Ошибка запроса: нет такого голоса, движок не собран и т.п. (HTTP 400)."""


class ModelError(EngineError):
    """Файл модели битый или не загружается (HTTP 503). Файл уже удалён — следующий вызов скачает заново."""


class Engine:
    name: str = ""
    title: str = ""

    def __init__(self) -> None:
        self.lock = threading.Lock()

    def status(self) -> tuple[bool, str]:
        """(готов ли движок, пояснение/как починить)."""
        return True, ""

    def installable(self) -> bool:
        """Можно ли довести движок до рабочего состояния автоматически (при первом download)."""
        return self.status()[0]

    def voices(self) -> list[Voice]:
        raise NotImplementedError

    def download(self, voice: str) -> None:
        raise NotImplementedError

    def reset(self, voice: str) -> None:
        """Удалить файлы голоса, чтобы download скачал их заново (ltts download --force)."""

    def synth(self, text: str, voice: str, speed: float = 1.0) -> Audio:
        raise NotImplementedError

    def supports_emphasis(self, voice: str) -> bool:
        """Понимает ли голос разметку эмфаз **слово** / *слово*; если нет, core вырезает звёздочки."""
        return False

    def has_voice(self, voice: str) -> bool:
        return any(v.name == voice for v in self.voices())

    def is_installed(self, voice: str) -> bool:
        return any(v.name == voice and v.installed for v in self.voices())

    def voice(self, name: str) -> Voice | None:
        return next((v for v in self.voices() if v.name == name), None)


# ---------- целостность файлов моделей ----------
# Рядом с каждым скачанным файлом лежит <file>.complete: {"size", "sha256", "url"}.
# Файл считается установленным, только если маркер есть и размер совпадает (дёшево);
# `ltts doctor --verify` дополнительно сверяет sha256.


def _marker(path: Path) -> Path:
    return path.with_name(path.name + ".complete")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def mark_complete(path: Path, sha256: str | None = None, url: str = "") -> None:
    info = {"size": path.stat().st_size, "sha256": sha256 or _sha256(path), "url": url}
    _marker(path).write_text(json.dumps(info))


def is_complete(path: Path) -> bool | None:
    """True — файл цел (по размеру), False — битый/нет, None — старый файл без маркера."""
    if not path.exists():
        return False
    try:
        info = json.loads(_marker(path).read_text())
    except FileNotFoundError:
        return None
    except ValueError:
        return False
    return info.get("size") == path.stat().st_size


def invalidate(*paths: Path) -> None:
    for p in paths:
        p.unlink(missing_ok=True)
        _marker(p).unlink(missing_ok=True)


def ensure_valid(path: Path, check) -> bool:
    """Цел ли файл. Старый файл без маркера проверяется check(path) один раз: ок — пишем маркер, нет — удаляем."""
    state = is_complete(path)
    if state is None:
        try:
            ok = bool(check(path))
        except Exception:
            ok = False
        if ok:
            mark_complete(path)
        else:
            invalidate(path)
        return ok
    if state is False and path.exists():
        invalidate(path)
    return bool(state)


def verify(path: Path) -> bool:
    """Полная проверка sha256 по маркеру."""
    try:
        info = json.loads(_marker(path).read_text())
        return path.exists() and info.get("size") == path.stat().st_size and info.get("sha256") == _sha256(path)
    except (FileNotFoundError, ValueError):
        return False


def fetch(url: str, dest: Path, label: str = "") -> Path:
    """Скачивает url в dest: свой .part на процесс, проверка длины, sha256, атомарный rename, маркер .complete."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f"{dest.name}.{os.getpid()}.part")
    req = urllib.request.Request(url, headers={"User-Agent": "ltts"})
    h = hashlib.sha256()
    try:
        with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            while chunk := r.read(1 << 20):
                f.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if label and sys.stderr.isatty():
                    pct = f"{done * 100 // total}%" if total else f"{done >> 20} МБ"
                    print(f"\r  ↓ {label} {pct}   ", end="", file=sys.stderr, flush=True)
        if label and sys.stderr.isatty():
            print(file=sys.stderr)
        if total and done != total:
            raise EngineError(f"обрыв загрузки: получено {done} из {total} байт")
        if not done:
            raise EngineError("пустой ответ")
        invalidate(dest)
        tmp.replace(dest)
        mark_complete(dest, h.hexdigest(), url)
    except Exception as e:
        tmp.unlink(missing_ok=True)
        raise EngineError(f"не удалось скачать {url}: {e}") from e
    return dest


def fetch_github_tree(repo: str, dest: Path, label: str = "") -> Path:
    """Скачивает содержимое GitHub-репозитория (HEAD) в dest без git."""
    if dest.exists():
        return dest
    with tempfile.TemporaryDirectory() as tmp:
        archive = fetch(f"https://codeload.github.com/{repo}/tar.gz/HEAD", Path(tmp) / "repo.tar.gz", label)
        with tarfile.open(archive) as tf:
            tf.extractall(tmp, filter="data")
        root = next(p for p in Path(tmp).iterdir() if p.is_dir())
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(root), str(dest))
    return dest
