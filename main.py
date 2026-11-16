#!/usr/bin/env python3
"""
YT Playlist -> Mister Download Manager
=====================================

PyQt6 desktop app that:
  1. reads a YouTube playlist (or single video) with yt-dlp metadata,
  2. shows every entry with a checkbox, title, duration and status,
  3. downloads the selected entries through Mister Download Manager's CLI
     (multi-connection, "IDM-style"), with per-row progress/speed/ETA,
  4. falls back to yt-dlp's native downloader when MDM is not available.

Why not yt-dlp's --external-downloader?
---------------------------------------
yt-dlp only accepts a fixed allow-list (aria2c, axel, curl, wget, httpie, ffmpeg)
for --external-downloader; any other executable raises
"No such external downloader" (yt_dlp/__init__.py).  MDM is therefore driven
directly through its documented CLI:

  misterdownloadmanager-cli --json video-get URL -f FMT -o DIR -c N
      -> MDM resolves the video, downloads each stream over N connections,
         merges audio/video (ffmpeg) and prints one JSON event per line:
         {"event":"progress","downloaded":..,"size":..,"speed":..}
         {"event":"done","path":"..."}
         {"error":"..."}

  misterdownloadmanager-cli --json get URL -o FILE -c N [-H "Header: value"]
      -> direct single-file segmented download (experimental "Direct" mode).

HOW TO CHANGE THE MDM EXECUTABLE PATH IF AUTO-DETECTION FAILS
-------------------------------------------------------------
  In the app:  Settings -> "Mister Download Manager CLI" -> Browse...
               pick `misterdownloadmanager-cli.exe` (or `misterdownloadmanager.exe`;
               the app then uses the sibling CLI, or the app exe with `--cli`).
               Press "Validate" to check it answers `help`.

  By hand:     edit the config file
                 Windows: %APPDATA%\\YTPlaylistMDM\\config.json
                 macOS:   ~/Library/Application Support/YTPlaylistMDM/config.json
                 Linux:   ~/.config/YTPlaylistMDM/config.json
               and set "mdm_cli" to the full executable path (or "auto").

Console-free; run with:  py main.py   (Windows)  /  python3 main.py
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yt_dlp
from yt_dlp.utils import DownloadCancelled, DownloadError

from PyQt6.QtCore import QObject, QRunnable, QThread, QThreadPool, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QFont
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

# --------------------------------------------------------------------------- #
# Constants / platform helpers
# --------------------------------------------------------------------------- #

APP_NAME = "YTPlaylistMDM"
IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"

CREATE_NO_WINDOW = 0x08000000 if IS_WIN else 0
CREATE_NEW_PROCESS_GROUP = 0x00000200 if IS_WIN else 0

DEFAULT_CONFIG = {
    "mdm_cli": "auto",
    "output_dir": str(Path.home() / "Downloads"),
    "theme": "dark",
    "engine": "video-get",
    "format": "best",
    "connections": 8,
    "concurrency": 2,
    "subs": "",
    "embed_subs": False,
    "cookies_from_browser": "",
}

# Quality presets per engine.
FORMAT_LABELS = {
    "best": "Best quality",
    "1080": "Up to 1080p",
    "720": "Up to 720p",
    "360": "Up to 360p",
    "240": "Up to 240p",
    "144": "Up to 144p",
    "audio_original": "Audio only (Original)",
    "audio": "Audio only (MP3 320)",
    "audio_opus": "Audio only (Opus)",
    "audio_flac": "Audio only (FLAC)",
}

VIDEO_FORMAT_KEYS = ("best", "1080", "720", "360", "240", "144")
AUDIO_FORMAT_KEYS = ("audio_original", "audio", "audio_opus", "audio_flac")
AUDIO_CONVERSION_KEYS = ("audio", "audio_opus", "audio_flac")

VIDEO_GET_FORMATS = {
    "best": "bv*+ba/b",
    "1080": "bv*[height<=1080]+ba/b[height<=1080]",
    "720": "bv*[height<=720]+ba/b[height<=720]",
    "360": "bv*[height<=360]+ba/b[height<=360]",
    "240": "bv*[height<=240]+ba/b[height<=240]",
    "144": "bv*[height<=144]+ba/b[height<=144]",
    "audio_original": "ba",
    "audio": "mp3:320",
    "audio_opus": "opus:",
    "audio_flac": "flac:",
}

DIRECT_FORMATS = {
    "best": "b[ext=mp4]/b",
    "1080": "b[height<=1080]",
    "720": "b[height<=720]",
    "360": "b[height<=360]",
    "240": "b[height<=240]",
    "144": "b[height<=144]",
    "audio_original": "ba",
    "audio": "ba",
    "audio_opus": "ba",
    "audio_flac": "ba",
}

NATIVE_FORMATS = {
    "best": "bv*+ba/b",
    "1080": "bv*[height<=1080]+ba/b[height<=1080]",
    "720": "bv*[height<=720]+ba/b[height<=720]",
    "360": "bv*[height<=360]+ba/b[height<=360]",
    "240": "bv*[height<=240]+ba/b[height<=240]",
    "144": "bv*[height<=144]+ba/b[height<=144]",
    "audio_original": "ba/b",
    "audio": "ba/b",
    "audio_opus": "ba/b",
    "audio_flac": "ba/b",
}

STATUS_COLORS = {
    "Pending": "#9aa0a6",
    "Queued": "#9aa0a6",
    "Downloading": "#4da3ff",
    "Complete": "#43b581",
    "Error": "#e5534b",
    "Paused": "#e0a33a",
    "Cancelled": "#9aa0a6",
}

DARK_QSS = """
QWidget { background-color: #17181b; color: #e6e8eb; }
QMainWindow, QDialog { background-color: #17181b; }
QLineEdit, QComboBox, QSpinBox {
    background-color: #262a30; border: 1px solid #33383f; border-radius: 6px;
    padding: 6px 8px; selection-background-color: #2f6fbf;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus { border-color: #4da3ff; }
QPushButton {
    background-color: #262a30; border: 1px solid #33383f; border-radius: 6px;
    padding: 6px 14px;
}
QPushButton:hover { background-color: #2d323a; }
QPushButton:pressed { background-color: #23272e; }
QPushButton:disabled { color: #6b7076; border-color: #2b2f35; }
QPushButton#primary { background-color: #2f6fbf; border-color: #2f6fbf; color: #ffffff; }
QPushButton#primary:hover { background-color: #3a7ed1; }
QPushButton#primary:disabled { background-color: #2b3b4f; border-color: #2b3b4f; color: #8b93a0; }
QTableWidget {
    background-color: #1f2126; alternate-background-color: #23262b;
    gridline-color: #2c3036; border: 1px solid #33383f; border-radius: 6px;
}
QHeaderView::section {
    background-color: #1c1e22; color: #c7ccd1; border: none;
    padding: 6px; border-bottom: 1px solid #33383f;
}
QTableWidget::item { padding: 4px; }
QLabel { background: transparent; }
QLabel#banner {
    background-color: #7a4d00; color: #ffe9b0; border-radius: 6px; padding: 8px;
}
QLabel#status { color: #b7bdc4; }
QComboBox QAbstractItemView { background-color: #262a30; selection-background-color: #2f6fbf; }
"""

LIGHT_QSS = """
QWidget { background-color: #f7f7f9; color: #1b1b1f; }
QMainWindow, QDialog { background-color: #f7f7f9; }
QLineEdit, QComboBox, QSpinBox {
    background-color: #ffffff; border: 1px solid #d5d7dd; border-radius: 6px;
    padding: 6px 8px; selection-background-color: #cfe3ff;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus { border-color: #2f6fbf; }
QPushButton {
    background-color: #ffffff; border: 1px solid #d5d7dd; border-radius: 6px;
    padding: 6px 14px;
}
QPushButton:hover { background-color: #ececf1; }
QPushButton:pressed { background-color: #e2e3e8; }
QPushButton:disabled { color: #a0a4ab; border-color: #e4e5ea; }
QPushButton#primary { background-color: #2f6fbf; border-color: #2f6fbf; color: #ffffff; }
QPushButton#primary:hover { background-color: #3a7ed1; }
QPushButton#primary:disabled { background-color: #b9cbe1; border-color: #b9cbe1; color: #f2f5f9; }
QTableWidget {
    background-color: #ffffff; alternate-background-color: #f2f3f6;
    gridline-color: #e7e8ec; border: 1px solid #d5d7dd; border-radius: 6px;
}
QHeaderView::section {
    background-color: #efeff3; color: #3a3d42; border: none;
    padding: 6px; border-bottom: 1px solid #d5d7dd;
}
QTableWidget::item { padding: 4px; }
QLabel { background: transparent; }
QLabel#banner {
    background-color: #fff3cd; color: #5c4400; border-radius: 6px; padding: 8px;
}
QLabel#status { color: #5a5f66; }
QComboBox QAbstractItemView { background-color: #ffffff; selection-background-color: #cfe3ff; }
"""


def child_env() -> dict:
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")

    helper_dirs: list[str] = []
    ffmpeg_dir = find_ffmpeg()
    if ffmpeg_dir:
        helper_dirs.append(ffmpeg_dir)
    for scripts in (Path(sys.executable).resolve().parent / "Scripts",
                    Path(sys.prefix) / "Scripts"):
        try:
            if scripts.is_dir():
                helper_dirs.append(str(scripts))
        except OSError:
            continue

    if helper_dirs:
        path_key = next((key for key in env if key.upper() == "PATH"), "PATH")
        current = env.get(path_key, "")
        parts = [str(Path(p)) for p in current.split(os.pathsep) if p]
        seen = {p.lower() for p in parts}
        prepend = []
        for directory in helper_dirs:
            key = directory.lower()
            if key not in seen:
                seen.add(key)
                prepend.append(directory)
        if prepend:
            env[path_key] = os.pathsep.join([*prepend, *parts])
    return env


def popen_process(cmd: list[str], **overrides):
    """Popen with UTF-8 pipes; MDM/yt-dlp output is parsed line by line."""
    kwargs = dict(
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=child_env(),
    )
    if IS_WIN:
        kwargs["creationflags"] = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    kwargs.update(overrides)
    return subprocess.Popen(cmd, **kwargs)


def stop_process(proc: Optional[subprocess.Popen]) -> None:
    """Ask the child to pause (MDM handles Ctrl+C gracefully) then force it."""
    if proc is None or proc.poll() is not None:
        return
    if IS_WIN:
        try:
            proc.send_signal(signal.CTRL_BREAK_EVENT)
            proc.wait(timeout=5)
            return
        except Exception:
            pass
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW,
                timeout=10,
            )
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass
        return
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        pgid = None
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
        try:
            if pgid is not None:
                os.killpg(pgid, sig)
            else:
                proc.send_signal(sig)
            proc.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            continue
        except OSError:
            return


# --------------------------------------------------------------------------- #
# Small formatting / path helpers
# --------------------------------------------------------------------------- #

def sanitize_name(name: str, max_len: int = 120) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name or "")
    name = re.sub(r"\s+", " ", name).strip().rstrip(". ")
    name = name or "video"
    if re.fullmatch(r"(con|prn|aux|nul|com[1-9]|lpt[1-9])", name, re.IGNORECASE):
        name = f"_{name}"
    return name[:max_len]


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for i in range(2, 1000):
        candidate = path.with_name(f"{stem} ({i}){suffix}")
        if not candidate.exists():
            return candidate
    return path


def format_duration(seconds) -> str:
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return "—"
    if total <= 0:
        return "—"
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_speed(bytes_per_sec) -> str:
    try:
        value = float(bytes_per_sec or 0)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
        if value < 1024 or unit == "GB/s":
            return f"{value:.1f} {unit}" if unit != "B/s" else f"{value:.0f} {unit}"
        value /= 1024
    return ""


def format_eta(seconds) -> str:
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return ""
    if total <= 0:
        return ""
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def parse_cookies_from_browser(value: str):
    """Parse yt-dlp's BROWSER[+KEYRING][:PROFILE][::CONTAINER] syntax."""
    value = (value or "").strip()
    if not value:
        return None
    container = None
    if "::" in value:
        value, container = value.split("::", 1)
    keyring = None
    if "+" in value:
        value, keyring = value.split("+", 1)
    profile = None
    if ":" in value:
        value, profile = value.split(":", 1)
    browser = value.strip().lower()
    if not browser:
        return None
    return (browser, profile or None, keyring or None, container or None)


def _app_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def find_ffmpeg() -> Optional[str]:
    """Return the directory containing ffmpeg (for yt-dlp), or None."""
    candidates: list[Path] = []
    found = shutil.which("ffmpeg")
    if found:
        candidates.append(Path(found))
    base = _app_base_dir()
    candidates.extend((
        base / "bin" / "ffmpeg.exe",
        base / "ffmpeg.exe",
        base / "ffmpeg" / "bin" / "ffmpeg.exe",
    ))
    if IS_WIN:
        local = Path(os.environ.get("LOCALAPPDATA", ""))
        candidates.append(local / "Microsoft" / "WinGet" / "Links" / "ffmpeg.exe")
        packages = local / "Microsoft" / "WinGet" / "Packages"
        if packages.is_dir():
            candidates.extend(sorted(
                packages.glob("Gyan.FFmpeg*/**/bin/ffmpeg.exe"), reverse=True))
        candidates.append(Path("C:/ffmpeg/bin/ffmpeg.exe"))
    elif IS_MAC:
        candidates.extend((Path("/opt/homebrew/bin/ffmpeg"), Path("/usr/local/bin/ffmpeg")))
    for candidate in candidates:
        try:
            if candidate.is_file():
                return str(candidate.parent)
        except OSError:
            continue
    return None


def find_js_runtime() -> Optional[str]:
    """Return a yt-dlp supported JS runtime name, or None."""
    for exe, runtime in (("deno", "deno"), ("node", "node"), ("bun", "bun"),
                         ("qjs", "quickjs"), ("quickjs", "quickjs")):
        if shutil.which(exe):
            return runtime
    if IS_WIN:
        node = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "nodejs" / "node.exe"
        try:
            if node.is_file():
                return "node"
        except OSError:
            pass
    return None


def cookie_read_failed(message: str) -> bool:
    lowered = (message or "").lower()
    return "could not copy" in lowered and "cookie database" in lowered


def config_dir() -> Path:
    if IS_WIN:
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif IS_MAC:
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / APP_NAME


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

class ConfigManager:
    """JSON config, written atomically on save."""

    def __init__(self) -> None:
        self.path = config_dir() / "config.json"
        self.data = dict(DEFAULT_CONFIG)
        self.load()

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                merged = dict(DEFAULT_CONFIG)
                merged.update({k: v for k, v in raw.items() if k in DEFAULT_CONFIG})
                self.data = merged
        except FileNotFoundError:
            pass
        except (OSError, json.JSONDecodeError):
            # Corrupt config: keep defaults, do not destroy the user's file yet.
            pass
        self._clamp()

    def _clamp(self) -> None:
        self.data["connections"] = max(1, min(128, int(self.data.get("connections", 8) or 8)))
        self.data["concurrency"] = max(1, min(4, int(self.data.get("concurrency", 2) or 2)))
        if self.data.get("theme") not in ("dark", "light"):
            self.data["theme"] = "dark"
        if self.data.get("engine") not in ("video-get", "direct", "native"):
            self.data["engine"] = "video-get"
        if self.data.get("format") not in FORMAT_LABELS:
            self.data["format"] = "best"

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    def get(self, key: str, default=None):
        return self.data.get(key, default)


# --------------------------------------------------------------------------- #
# MDM discovery / validation
# --------------------------------------------------------------------------- #

@dataclass
class MdmTool:
    exe: str
    prefix: list[str] = field(default_factory=list)

    def build(self, *args) -> list[str]:
        return [self.exe, *self.prefix, *[str(a) for a in args]]


class MdmLocator:
    ENGINE_NAMES = ["mdm-video-engine.exe", "mdm-video-engine"]
    CLI_NAMES = ["misterdownloadmanager-cli.exe", "misterdownloadmanager-cli"]
    APP_NAMES = ["misterdownloadmanager.exe", "misterdownloadmanager"]

    @classmethod
    def candidates(cls):
        seen = set()

        def emit(path: Path):
            try:
                key = str(path).lower()
            except Exception:
                return
            if key not in seen:
                seen.add(key)
                return path
            return None

        base = _app_base_dir()
        for folder in (base / "engine", base):
            for name in cls.ENGINE_NAMES:
                cand = folder / name
                if emit(cand) and cand.is_file():
                    yield cand

        for name in cls.ENGINE_NAMES + cls.CLI_NAMES + cls.APP_NAMES:
            found = shutil.which(name)
            if found and emit(Path(found)):
                yield Path(found)

        if IS_WIN:
            local = Path(os.environ.get("LOCALAPPDATA", ""))
            pf = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
            pf86 = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
            bases = [local / "Programs", local, pf, pf86,
                     Path.cwd(), base]
            folders = ["Mister Download Manager", "misterdownloadmanager", "MrDownloadManager"]
            for base in bases:
                for folder in folders:
                    for name in cls.CLI_NAMES + cls.APP_NAMES:
                        cand = base / folder / name
                        if emit(cand):
                            yield cand
        elif IS_MAC:
            app_roots = [
                Path("/Applications/Mister Download Manager.app/Contents/MacOS"),
                Path("/Applications/MisterDownloadManager.app/Contents/MacOS"),
                Path.home() / "Applications/Mister Download Manager.app/Contents/MacOS",
            ]
            for root in app_roots:
                for name in cls.CLI_NAMES + cls.APP_NAMES:
                    cand = root / name
                    if emit(cand):
                        yield cand
        else:
            for folder in ("/usr/bin", "/usr/local/bin", "/opt/misterdownloadmanager",
                           "/opt/Mister Download Manager", "/snap/bin"):
                for name in cls.CLI_NAMES + cls.APP_NAMES:
                    cand = Path(folder) / name
                    if emit(cand):
                        yield cand
            for glob_dir in (Path.home() / "Applications", Path.home() / "Downloads", Path.home() / ".local/bin"):
                try:
                    for appimage in glob_dir.glob("*[Mm]ister*[Dd]ownload*.AppImage"):
                        if emit(appimage):
                            yield appimage
                except OSError:
                    pass

    @classmethod
    def resolve_tool(cls, path: str | Path) -> Optional[MdmTool]:
        p = Path(path)
        if not p.exists() or not p.is_file():
            return None
        name = p.name.lower()
        if "cli" in name or "engine" in name:
            return MdmTool(str(p))
        # A GUI executable was chosen: prefer the CLI shipped beside it.
        suffix = ".exe" if IS_WIN else ""
        sibling = p.with_name(f"misterdownloadmanager-cli{suffix}")
        if sibling.exists():
            return MdmTool(str(sibling))
        # Last resort: the app executable understands `--cli <command>`.
        return MdmTool(str(p), ["--cli"])

    @classmethod
    def detect(cls, configured: str = "auto") -> Optional[MdmTool]:
        configured = (configured or "auto").strip()
        if configured and configured.lower() != "auto":
            tool = cls.resolve_tool(configured)
            if tool:
                return tool
        for candidate in cls.candidates():
            tool = cls.resolve_tool(candidate)
            if tool:
                return tool
        return None

    @staticmethod
    def validate(tool: MdmTool) -> tuple[bool, str]:
        try:
            proc = subprocess.run(
                tool.build("help"),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                creationflags=CREATE_NO_WINDOW if IS_WIN else 0,
            )
        except FileNotFoundError:
            return False, f"Not found: {tool.exe}"
        except subprocess.TimeoutExpired:
            return False, "The CLI did not answer within 10 seconds."
        except OSError as exc:
            return False, f"Could not start: {exc}"
        if proc.returncode == 0:
            if "engine" in Path(tool.exe).name.lower():
                return True, "Mister Download Manager video engine is working."
            return True, "Mister Download Manager CLI is working."
        tail = (proc.stdout or "").strip().splitlines()[-3:]
        return False, "The CLI answered with an error.\n" + "\n".join(tail)


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #

@dataclass
class VideoItem:
    key: str
    video_id: str
    title: str
    duration: Optional[int]
    url: str
    thumbnail: str = ""


@dataclass
class DownloadSettings:
    engine: str
    format_key: str
    connections: int
    concurrency: int
    subs: str
    embed_subs: bool
    playlist_dir: Path
    tool: Optional[MdmTool]
    cookies: str = ""


# --------------------------------------------------------------------------- #
# Playlist metadata worker
# --------------------------------------------------------------------------- #

class PlaylistFetcher(QThread):
    loaded = pyqtSignal(list, str)
    failed = pyqtSignal(str)
    warning = pyqtSignal(str)

    def __init__(self, url: str, cookies: str = "", parent=None):
        super().__init__(parent)
        self.url = url
        self.cookies = (cookies or "").strip()
        self._cancelled = threading.Event()
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()

    def cancel(self) -> None:
        self._cancelled.set()
        with self._lock:
            proc = self._proc
        stop_process(proc)

    def _attempt(self, use_cookies: bool) -> tuple[int, str, str]:
        cmd = [
            sys.executable, "-m", "yt_dlp",
            "--flat-playlist", "--dump-single-json",
            "--no-warnings", "--skip-download",
        ]
        if use_cookies and self.cookies:
            cmd += ["--cookies-from-browser", self.cookies]
        cmd.append(self.url)
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as errf:
            proc = popen_process(cmd, stderr=errf)
            with self._lock:
                self._proc = proc
            try:
                stdout, _ = proc.communicate()
            finally:
                with self._lock:
                    self._proc = None
            errf.seek(0)
            return proc.returncode, stdout, errf.read()

    def run(self) -> None:
        try:
            rc, stdout, stderr = self._attempt(True)
        except FileNotFoundError:
            self.failed.emit("Python could not start yt-dlp. Install it with: pip install yt-dlp")
            return
        except Exception as exc:  # noqa: BLE001 - surface anything to the UI
            if not self._cancelled.is_set():
                self.failed.emit(f"Could not fetch playlist metadata:\n{exc}")
            return

        if self._cancelled.is_set():
            return
        if rc != 0 and self.cookies and cookie_read_failed(stderr):
            self.warning.emit(
                "Could not read browser cookies (close the browser and retry to use them). "
                "Fetched without cookies."
            )
            try:
                rc, stdout, stderr = self._attempt(False)
            except FileNotFoundError:
                self.failed.emit("Python could not start yt-dlp. Install it with: pip install yt-dlp")
                return
            except Exception as exc:  # noqa: BLE001
                if not self._cancelled.is_set():
                    self.failed.emit(f"Could not fetch playlist metadata:\n{exc}")
                return
            if self._cancelled.is_set():
                return

        if rc != 0:
            lines = [ln for ln in (stderr or "").splitlines() if ln.strip()]
            reason = "\n".join(lines[-3:]) or f"yt-dlp exited with code {rc}."
            self.failed.emit(f"Could not read the video or playlist:\n{reason}")
            return

        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            start = stdout.find("{")
            try:
                data = json.loads(stdout[start:]) if start >= 0 else None
            except json.JSONDecodeError:
                data = None
            if data is None:
                self.failed.emit("yt-dlp did not return usable JSON metadata.")
                return

        entries = data.get("entries") or []
        container_extractor = str(data.get("extractor_key") or data.get("ie_key") or "")
        if not entries and data.get("id"):
            entries = [data]

        items: list[VideoItem] = []
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict) or not entry:
                continue
            video_id = str(entry.get("id") or index)
            title = str(entry.get("title") or video_id)
            url = self._entry_url(entry, container_extractor)
            if not url:
                continue
            duration = entry.get("duration")
            try:
                duration = int(float(duration)) if duration is not None else None
            except (TypeError, ValueError):
                duration = None
            thumbnail = self._entry_thumbnail(entry, video_id, container_extractor)
            items.append(VideoItem(
                key=f"{video_id}#{index}",
                video_id=video_id,
                title=title,
                duration=duration,
                url=url,
                thumbnail=thumbnail,
            ))

        playlist_title = str(data.get("title") or "Playlist")
        self.loaded.emit(items, playlist_title)

    @staticmethod
    def _entry_url(entry: dict, container_extractor: str) -> str:
        for key in ("webpage_url", "original_url"):
            value = entry.get(key)
            if value and str(value).startswith("http"):
                return str(value)
        value = str(entry.get("url") or "")
        if value.startswith("http"):
            return value
        video_id = entry.get("id")
        ie = str(entry.get("ie_key") or container_extractor or "").lower()
        if video_id and "youtube" in ie:
            return f"https://www.youtube.com/watch?v={video_id}"
        return value

    @staticmethod
    def _entry_thumbnail(entry: dict, video_id: str, container_extractor: str) -> str:
        thumbs = entry.get("thumbnails") or []
        if isinstance(thumbs, list) and thumbs:
            candidate = thumbs[-1]
            if isinstance(candidate, dict) and candidate.get("url"):
                return str(candidate["url"])
        if entry.get("thumbnail"):
            return str(entry["thumbnail"])
        ie = str(entry.get("ie_key") or container_extractor or "").lower()
        if "youtube" in ie and video_id:
            return f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"
        return ""


# --------------------------------------------------------------------------- #
# Download tasks (QRunnable + QThreadPool)
# --------------------------------------------------------------------------- #

class TaskSignals(QObject):
    progress = pyqtSignal(str, float, str, str)   # key, percent(-1 unknown), speed, eta
    done = pyqtSignal(str, str)                   # key, path
    error = pyqtSignal(str, str)                  # key, message
    cancelled = pyqtSignal(str)                   # key
    finished = pyqtSignal(str)                    # key (always, last)


class BaseDownloadTask(QRunnable):
    def __init__(self, key: str, item: VideoItem, settings: DownloadSettings,
                 signals: TaskSignals):
        super().__init__()
        self.setAutoDelete(True)
        self.key = key
        self.item = item
        self.settings = settings
        self.signals = signals
        self._cancelled = threading.Event()
        self._proc: Optional[subprocess.Popen] = None
        self._proc_lock = threading.Lock()
        self._done_emitted = False
        self._error_emitted = False
        self._cancelled_emitted = False
        self._event_error = ""

    # -- cancellation -------------------------------------------------------
    def cancel(self) -> None:
        self._cancelled.set()
        with self._proc_lock:
            proc = self._proc
        stop_process(proc)

    def _set_proc(self, proc: Optional[subprocess.Popen]) -> None:
        with self._proc_lock:
            self._proc = proc

    # -- terminal emitters (idempotent) ------------------------------------
    def _emit_done(self, path: str) -> None:
        if not self._done_emitted and not self._error_emitted:
            self._done_emitted = True
            self.signals.done.emit(self.key, path or "")

    def _emit_error(self, message: str) -> None:
        if not self._error_emitted and not self._done_emitted:
            self._error_emitted = True
            self.signals.error.emit(self.key, message or "Unknown error")

    def _emit_cancelled(self) -> None:
        if not self._cancelled_emitted and not self._done_emitted and not self._error_emitted:
            self._cancelled_emitted = True
            self.signals.cancelled.emit(self.key)

    # -- MDM JSON event stream ---------------------------------------------
    def _handle_event(self, evt: dict) -> None:
        event = str(evt.get("event") or "").lower()
        if event == "progress":
            downloaded = evt.get("downloaded") or 0
            size = evt.get("size")
            speed = evt.get("speed") or 0
            try:
                percent = (float(downloaded) / float(size) * 100.0) if size else -1.0
            except (TypeError, ValueError, ZeroDivisionError):
                percent = -1.0
            eta = ""
            try:
                if size and speed:
                    eta = format_eta((float(size) - float(downloaded)) / float(speed))
            except (TypeError, ValueError, ZeroDivisionError):
                eta = ""
            self.signals.progress.emit(self.key, max(-1.0, min(100.0, percent)),
                                       format_speed(speed), eta)
        elif event == "done":
            self._emit_done(str(evt.get("path") or ""))
        elif event == "paused":
            self._emit_cancelled()
        elif evt.get("error"):
            self._event_error = str(evt.get("error"))
            self._emit_error(self._event_error)

    def _run_json_process(self, cmd: list[str]) -> bool:
        try:
            proc = popen_process(cmd)
        except FileNotFoundError:
            self._emit_error(f"MDM executable not found:\n{cmd[0]}")
            return False
        except OSError as exc:
            self._emit_error(f"Could not start Mister Download Manager:\n{exc}")
            return False

        self._set_proc(proc)
        tail: list[str] = []
        rc = -1
        try:
            assert proc.stdout is not None
            for raw_line in proc.stdout:
                line = raw_line.strip()
                if not line:
                    continue
                if line.startswith("{"):
                    try:
                        self._handle_event(json.loads(line))
                    except json.JSONDecodeError:
                        tail.append(line)
                else:
                    tail.append(line)
                    if len(tail) > 12:
                        tail.pop(0)
                if self._error_emitted or self._done_emitted:
                    # Keep reading so the pipe never blocks; the process may still exit.
                    pass
            rc = proc.wait()
        finally:
            self._set_proc(None)

        if self._cancelled.is_set():
            self._emit_cancelled()
            return False
        if self._error_emitted:
            return False
        if rc != 0 or not self._done_emitted:
            if not self._done_emitted:
                message = self._event_error
                if not message:
                    message = "\n".join(tail[-4:]) or f"MDM exited with code {rc} without reporting a file."
                self._emit_error(message)
            return False
        return True

    # -- QRunnable entry point ---------------------------------------------
    def run(self) -> None:
        try:
            self._execute()
        except Exception as exc:  # noqa: BLE001 - never let a worker kill the pool
            if self._cancelled.is_set():
                self._emit_cancelled()
            else:
                self._emit_error(str(exc) or exc.__class__.__name__)
        finally:
            self.signals.finished.emit(self.key)

    def _execute(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError


class MdmVideoGetTask(BaseDownloadTask):
    """MDM resolves + downloads the media with N connections and merges A/V."""

    def _execute(self) -> None:
        if self._cancelled.is_set():
            self._emit_cancelled()
            return
        tool = self.settings.tool
        if tool is None:
            self._emit_error("Mister Download Manager was not found.")
            return
        selector = VIDEO_GET_FORMATS.get(self.settings.format_key, VIDEO_GET_FORMATS["best"])
        self.settings.playlist_dir.mkdir(parents=True, exist_ok=True)
        cmd = tool.build(
            "--json", "video-get", self.item.url,
            "-f", selector,
            "-o", str(self.settings.playlist_dir),
            "-c", str(self.settings.connections),
        )
        if self.settings.subs:
            cmd += ["--subs", self.settings.subs]
            if self.settings.embed_subs:
                cmd += ["--embed-subs"]
        self._run_json_process(cmd)


class MdmDirectTask(BaseDownloadTask):
    """yt-dlp resolves a single-file media URL; MDM downloads it segmented."""

    def _execute(self) -> None:
        if self._cancelled.is_set():
            self._emit_cancelled()
            return
        tool = self.settings.tool
        if tool is None:
            self._emit_error("Mister Download Manager was not found.")
            return

        selector = DIRECT_FORMATS.get(self.settings.format_key, DIRECT_FORMATS["best"])
        if self.settings.format_key in AUDIO_CONVERSION_KEYS:
            raise RuntimeError(
                "Direct mode cannot convert audio to MP3, Opus or FLAC.\n"
                "Choose Audio only (Original), or use the video-get engine."
            )
        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
            "format": selector,
            "socket_timeout": 30,
            "noprogress": True,
        }
        cookies = parse_cookies_from_browser(self.settings.cookies)
        if cookies:
            opts["cookiesfrombrowser"] = cookies
        with yt_dlp.YoutubeDL(opts) as ydl:
            try:
                info = ydl.extract_info(self.item.url, download=False)
            except DownloadError as exc:
                if "format is not available" in str(exc).lower():
                    raise RuntimeError(
                        "Direct mode needs one ready-made file, but this video only "
                        "offers separate video and audio streams.\n"
                        "Use the video-get engine for it."
                    ) from exc
                raise
        if info.get("entries"):
            info = next((e for e in info["entries"] if e), None)
            if info is None:
                raise RuntimeError("The video has no downloadable formats.")
        if info.get("requested_formats"):
            raise RuntimeError(
                "Direct mode only supports single-file formats.\n"
                "Pick a lower preset or use the default video-get engine."
            )
        protocol = str(info.get("protocol") or "")
        if protocol.startswith("m3u8"):
            raise RuntimeError(
                "Direct mode cannot download HLS playlists.\n"
                "Use the default video-get engine for this video."
            )
        media_url = info.get("url")
        if not media_url:
            raise RuntimeError("No direct media URL was found for this format.")
        if self._cancelled.is_set():
            self._emit_cancelled()
            return

        title = str(info.get("title") or self.item.title)
        video_id = str(info.get("id") or self.item.video_id)
        ext = str(info.get("ext") or "mp4")
        target = unique_path(self.settings.playlist_dir / f"{sanitize_name(title)} [{video_id}].{ext}")
        self.settings.playlist_dir.mkdir(parents=True, exist_ok=True)

        cmd = tool.build(
            "--json", "get", str(media_url),
            "-o", str(target),
            "-c", str(self.settings.connections),
        )
        headers = info.get("http_headers") or {}
        for header in ("User-Agent", "Referer", "Origin", "Cookie", "Authorization",
                       "Accept", "Accept-Language"):
            value = headers.get(header)
            if value:
                cmd += ["-H", f"{header}: {value}"]

        if self._cancelled.is_set():
            self._emit_cancelled()
            return
        self._run_json_process(cmd)


class NativeYtDlpTask(BaseDownloadTask):
    """Fallback: yt-dlp's own downloader with per-row progress."""

    class _Logger:
        def __init__(self) -> None:
            self.errors: list[str] = []

        def debug(self, message) -> None:
            pass

        def info(self, message) -> None:
            pass

        def warning(self, message) -> None:
            pass

        def error(self, message) -> None:
            self.errors.append(str(message))

    def _execute(self) -> None:
        if self._cancelled.is_set():
            self._emit_cancelled()
            return

        ffmpeg_dir = find_ffmpeg()
        has_ffmpeg = ffmpeg_dir is not None
        js_runtime = find_js_runtime()
        selector = NATIVE_FORMATS.get(self.settings.format_key, NATIVE_FORMATS["best"])
        if self.settings.format_key in VIDEO_FORMAT_KEYS and not has_ffmpeg:
            selector = "b[ext=mp4]/b"  # no ffmpeg -> avoid merge-only selectors
        if self.settings.format_key in AUDIO_CONVERSION_KEYS and not has_ffmpeg:
            self._emit_error(
                "ffmpeg is required to convert audio to MP3, Opus or FLAC.\n"
                "Install it with:  winget install Gyan.FFmpeg\n"
                "Or choose Audio only (Original)."
            )
            return

        self.settings.playlist_dir.mkdir(parents=True, exist_ok=True)
        outtmpl = str(self.settings.playlist_dir / "%(title)s [%(id)s].%(ext)s")
        self._last_filename = ""
        self._pp_path = ""

        logger = self._Logger()
        opts = {
            "format": selector,
            "outtmpl": outtmpl,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "windowsfilenames": True,
            "socket_timeout": 30,
            "progress_hooks": [self._progress_hook],
            "postprocessor_hooks": [self._postprocessor_hook],
            "logger": logger,
        }
        if ffmpeg_dir:
            opts["ffmpeg_location"] = ffmpeg_dir
        if js_runtime:
            opts["js_runtimes"] = {js_runtime: {}}
        if has_ffmpeg:
            if self.settings.format_key == "audio":
                opts["postprocessors"] = [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "320",
                }]
            elif self.settings.format_key == "audio_opus":
                opts["postprocessors"] = [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "opus",
                }]
            elif self.settings.format_key == "audio_flac":
                opts["postprocessors"] = [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "flac",
                }]
        cookies = parse_cookies_from_browser(self.settings.cookies)
        if cookies:
            opts["cookiesfrombrowser"] = cookies

        retcode = 0
        failure = ""
        cookie_note = ""
        attempts = 2 if cookies else 1
        for attempt in range(attempts):
            self._last_filename = ""
            self._pp_path = ""
            logger.errors.clear()
            failure = ""
            retcode = 0
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    retcode = ydl.download([self.item.url])
            except DownloadCancelled:
                self._emit_cancelled()
                return
            except Exception as exc:  # noqa: BLE001 - surface yt-dlp failures in the row
                failure = str(exc) or exc.__class__.__name__

            if self._cancelled.is_set():
                self._emit_cancelled()
                return
            if not retcode and not failure:
                break
            problem = "\n".join([failure, *logger.errors])
            if attempt == 0 and cookies and cookie_read_failed(problem):
                cookie_note = (
                    "Browser cookies could not be read (the browser locks its cookie "
                    "database); retried without cookies."
                )
                opts.pop("cookiesfrombrowser", None)
                continue
            break

        if retcode or failure:
            detail = (failure or "\n".join(logger.errors[-3:])
                      or f"yt-dlp failed with code {retcode}.")
            if cookie_note:
                detail = f"{cookie_note}\n\n{detail}"
            if (not has_ffmpeg and self.settings.format_key in VIDEO_FORMAT_KEYS
                    and "format is not available" in detail.lower()):
                detail = (
                    "ffmpeg is required to merge YouTube's separate video and audio streams.\n"
                    "Install it with:  winget install Gyan.FFmpeg\n"
                    "(or use the MDM video-get engine, which brings its own helpers)\n\n"
                    + detail
                )
            if "format is not available" in detail.lower() and not js_runtime:
                detail += (
                    "\nTip: installing Node.js or Deno lets yt-dlp expose more YouTube formats."
                )
            self._emit_error(detail)
            return

        path = self._pp_path or self._last_filename
        if path and Path(path).exists():
            self._emit_done(path)
            return
        found = self._find_output()
        self._emit_done(str(found) if found else "")

    def _progress_hook(self, data: dict) -> None:
        if self._cancelled.is_set():
            raise DownloadCancelled("cancelled")
        status = data.get("status")
        if status == "downloading":
            total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
            downloaded = data.get("downloaded_bytes") or 0
            try:
                percent = float(downloaded) / float(total) * 100.0 if total else -1.0
            except (TypeError, ValueError, ZeroDivisionError):
                percent = -1.0
            self.signals.progress.emit(
                self.key,
                max(-1.0, min(100.0, percent)),
                format_speed(data.get("speed")),
                format_eta(data.get("eta")),
            )
        elif status == "finished":
            self._last_filename = str(data.get("filename") or "")

    def _postprocessor_hook(self, data: dict) -> None:
        if self._cancelled.is_set():
            raise DownloadCancelled("cancelled")
        if data.get("status") == "finished":
            info = data.get("info_dict") or {}
            filepath = info.get("filepath") or info.get("_filename")
            if filepath:
                self._pp_path = str(filepath)

    def _find_output(self) -> Optional[Path]:
        if self._last_filename and Path(self._last_filename).exists():
            return Path(self._last_filename)
        try:
            matches = [
                p for p in self.settings.playlist_dir.iterdir()
                if p.is_file() and f"[{self.item.video_id}]" in p.name
            ]
            matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            matches = []
        return matches[0] if matches else None


def make_task(settings: DownloadSettings, key: str, item: VideoItem,
              signals: TaskSignals) -> BaseDownloadTask:
    if settings.tool is not None:
        if settings.engine == "video-get":
            return MdmVideoGetTask(key, item, settings, signals)
        if settings.engine == "direct":
            return MdmDirectTask(key, item, settings, signals)
    return NativeYtDlpTask(key, item, settings, signals)


class DownloadManager(QObject):
    """Owns a QThreadPool and fans task signals out to the UI."""

    progress = pyqtSignal(str, float, str, str)
    done = pyqtSignal(str, str)
    error = pyqtSignal(str, str)
    cancelled = pyqtSignal(str)
    all_finished = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(2)
        self._tasks: dict[str, BaseDownloadTask] = {}
        self._remaining = 0
        self._lock = threading.Lock()

    def busy(self) -> bool:
        with self._lock:
            return self._remaining > 0

    def start(self, jobs: list[tuple[str, VideoItem]], settings: DownloadSettings) -> None:
        with self._lock:
            self._remaining = len(jobs)
        if not jobs:
            QTimer.singleShot(0, self.all_finished.emit)
            return
        self.pool.setMaxThreadCount(max(1, settings.concurrency))
        for key, item in jobs:
            signals = TaskSignals()
            signals.progress.connect(self.progress)
            signals.done.connect(self._on_done)
            signals.error.connect(self._on_error)
            signals.cancelled.connect(self._on_cancelled)
            signals.finished.connect(lambda k=key: self._finish(k))
            task = make_task(settings, key, item, signals)
            self._tasks[key] = task
            self.pool.start(task)

    def cancel(self, key: str) -> None:
        task = self._tasks.get(key)
        if task:
            task.cancel()

    def cancel_all(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()

    def _on_done(self, key: str, path: str) -> None:
        self.done.emit(key, path)

    def _on_error(self, key: str, message: str) -> None:
        self.error.emit(key, message)

    def _on_cancelled(self, key: str) -> None:
        self.cancelled.emit(key)

    def _finish(self, key: str) -> None:
        with self._lock:
            if key not in self._tasks:
                return
            self._tasks.pop(key, None)
            self._remaining = max(0, self._remaining - 1)
            left = self._remaining
        if left == 0:
            self.all_finished.emit()


# --------------------------------------------------------------------------- #
# Settings dialog
# --------------------------------------------------------------------------- #

class SettingsDialog(QDialog):
    def __init__(self, config: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(560)
        self._config = dict(config)

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        # --- MDM CLI --- #
        self.mdm_edit = QLineEdit(self._config.get("mdm_cli", "auto"))
        self.mdm_edit.setPlaceholderText("auto")
        browse_mdm = QPushButton("Browse…")
        browse_mdm.clicked.connect(self._browse_mdm)
        validate_btn = QPushButton("Validate")
        validate_btn.clicked.connect(self._validate_mdm)
        mdm_row = QHBoxLayout()
        mdm_row.addWidget(self.mdm_edit, 1)
        mdm_row.addWidget(browse_mdm)
        mdm_row.addWidget(validate_btn)
        form.addRow("Mister Download Manager CLI:", mdm_row)

        self.mdm_status = QLabel("")
        self.mdm_status.setObjectName("status")
        self.mdm_status.setWordWrap(True)
        form.addRow("", self.mdm_status)

        # --- Output folder --- #
        self.output_edit = QLineEdit(self._config.get("output_dir", ""))
        browse_out = QPushButton("Browse…")
        browse_out.clicked.connect(self._browse_output)
        out_row = QHBoxLayout()
        out_row.addWidget(self.output_edit, 1)
        out_row.addWidget(browse_out)
        form.addRow("Save downloads to:", out_row)

        # --- Engine --- #
        self.engine_combo = QComboBox()
        self.engine_combo.addItem("MDM video-get (recommended)", "video-get")
        self.engine_combo.addItem("MDM direct URL (experimental)", "direct")
        self.engine_combo.addItem("yt-dlp built-in (slower, single connection)", "native")
        engine_index = self.engine_combo.findData(self._config.get("engine", "video-get"))
        self.engine_combo.setCurrentIndex(max(0, engine_index))
        self.engine_combo.setToolTip(
            "video-get: MDM resolves, segments and merges audio/video.\n"
            "direct: yt-dlp resolves one URL, MDM segments that file only.\n"
            "native: yt-dlp downloads (use for private videos with cookies)."
        )
        form.addRow("Download engine:", self.engine_combo)

        # --- Format --- #
        self.format_combo = QComboBox()
        for key, label in FORMAT_LABELS.items():
            self.format_combo.addItem(label, key)
        fmt_index = self.format_combo.findData(self._config.get("format", "best"))
        self.format_combo.setCurrentIndex(max(0, fmt_index))
        self.format_combo.setToolTip(
            "MP3, Opus and FLAC conversion is done by MDM video-get or by yt-dlp + ffmpeg.\n"
            "The direct engine only supports Audio only (Original)."
        )
        form.addRow("Default quality:", self.format_combo)

        # --- Connections / concurrency --- #
        self.connections_spin = QSpinBox()
        self.connections_spin.setRange(1, 128)
        self.connections_spin.setValue(int(self._config.get("connections", 8)))
        form.addRow("Connections per video (MDM -c):", self.connections_spin)

        self.concurrency_spin = QSpinBox()
        self.concurrency_spin.setRange(1, 4)
        self.concurrency_spin.setValue(int(self._config.get("concurrency", 2)))
        form.addRow("Videos at once:", self.concurrency_spin)

        # --- Subtitles --- #
        self.subs_edit = QLineEdit(self._config.get("subs", ""))
        self.subs_edit.setPlaceholderText("e.g. en,fr (blank = no subtitles)")
        form.addRow("Subtitles (video-get engine):", self.subs_edit)

        self.embed_subs = QCheckBox("Embed subtitles into the video (needs ffmpeg)")
        self.embed_subs.setChecked(bool(self._config.get("embed_subs", False)))
        form.addRow("", self.embed_subs)

        # --- Cookies --- #
        self.cookies_edit = QLineEdit(self._config.get("cookies_from_browser", ""))
        self.cookies_edit.setPlaceholderText("chrome / firefox / edge … (blank = none)")
        form.addRow("Cookies from browser:", self.cookies_edit)

        # --- Theme --- #
        self.theme_combo = QComboBox()
        self.theme_combo.addItem("Dark", "dark")
        self.theme_combo.addItem("Light", "light")
        theme_index = self.theme_combo.findData(self._config.get("theme", "dark"))
        self.theme_combo.setCurrentIndex(max(0, theme_index))
        form.addRow("Theme:", self.theme_combo)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addStretch(1)
        layout.addWidget(buttons)

        tool = MdmLocator.detect(self._config.get("mdm_cli", "auto"))
        self._show_detected(tool)

    def _show_detected(self, tool: Optional[MdmTool]) -> None:
        if tool:
            self.mdm_status.setText(f"Detected: {tool.exe}")
            self.mdm_status.setStyleSheet("color: #43b581;")
        else:
            self.mdm_status.setText(
                "Mister Download Manager CLI or the bundled video engine was not found. "
                "Downloads will use the slower yt-dlp fallback until a valid path is set."
            )
            self.mdm_status.setStyleSheet("color: #e0a33a;")

    def _browse_mdm(self) -> None:
        pattern = "Executable (*.exe);;All files (*)" if IS_WIN else "All files (*)"
        path, _ = QFileDialog.getOpenFileName(self, "Select the MDM executable", "", pattern)
        if path:
            self.mdm_edit.setText(path)
            self._validate_mdm()

    def _validate_mdm(self) -> None:
        raw = self.mdm_edit.text().strip()
        if not raw or raw.lower() == "auto":
            tool = MdmLocator.detect("auto")
        else:
            tool = MdmLocator.resolve_tool(raw)
        if tool is None:
            self.mdm_status.setText("Executable not found at that path.")
            self.mdm_status.setStyleSheet("color: #e5534b;")
            return
        ok, message = MdmLocator.validate(tool)
        self.mdm_status.setText(message)
        self.mdm_status.setStyleSheet("color: #43b581;" if ok else "color: #e5534b;")
        if ok:
            self.mdm_edit.setText(tool.exe)

    def _browse_output(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Choose download folder",
                                                self.output_edit.text())
        if path:
            self.output_edit.setText(path)

    def values(self) -> dict:
        return {
            "mdm_cli": self.mdm_edit.text().strip() or "auto",
            "output_dir": self.output_edit.text().strip() or str(Path.home() / "Downloads"),
            "engine": self.engine_combo.currentData(),
            "format": self.format_combo.currentData(),
            "connections": self.connections_spin.value(),
            "concurrency": self.concurrency_spin.value(),
            "subs": self.subs_edit.text().strip(),
            "embed_subs": self.embed_subs.isChecked(),
            "cookies_from_browser": self.cookies_edit.text().strip(),
            "theme": self.theme_combo.currentData(),
        }


# --------------------------------------------------------------------------- #
# Main window
# --------------------------------------------------------------------------- #

class MainWindow(QMainWindow):
    def __init__(self, config: ConfigManager):
        super().__init__()
        self.config = config
        self.mdm_tool = MdmLocator.detect(self.config.get("mdm_cli", "auto"))

        self._items: dict[str, VideoItem] = {}
        self._row_of: dict[str, int] = {}
        self._status: dict[str, str] = {}
        self._playlist_title = "Playlist"
        self._playlist_dir: Optional[Path] = None
        self._fetcher: Optional[PlaylistFetcher] = None
        self._download_active = False
        self._ffmpeg_warned = False

        self.setWindowTitle("YT Playlist → Mister Download Manager")
        self.resize(1120, 720)

        self.manager = DownloadManager(self)
        self.manager.progress.connect(self._on_progress)
        self.manager.done.connect(self._on_done)
        self.manager.error.connect(self._on_error)
        self.manager.cancelled.connect(self._on_cancelled)
        self.manager.all_finished.connect(self._on_all_finished)

        self._build_ui()
        self._refresh_mdm_state()

    # -- UI construction ----------------------------------------------------
    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(14, 14, 14, 10)
        layout.setSpacing(10)

        # Top bar
        top = QHBoxLayout()
        title = QLabel("Playlist Downloader")
        title_font = QFont()
        title_font.setPointSize(14)
        title_font.setBold(True)
        title.setFont(title_font)
        top.addWidget(title)
        top.addStretch(1)
        top.addWidget(QLabel("Quality:"))
        self.quality_combo = QComboBox()
        for key, label in FORMAT_LABELS.items():
            self.quality_combo.addItem(label, key)
        self._sync_quality_combo()
        self.quality_combo.setToolTip(
            "Quality for the next download batch. Settings sets the default."
        )
        self.quality_combo.currentIndexChanged.connect(self._on_quality_changed)
        top.addWidget(self.quality_combo)
        self.theme_btn = QPushButton()
        self.theme_btn.clicked.connect(self._toggle_theme)
        self._update_theme_button()
        top.addWidget(self.theme_btn)
        self.settings_btn = QPushButton("Settings")
        self.settings_btn.clicked.connect(self._open_settings)
        top.addWidget(self.settings_btn)
        layout.addLayout(top)

        # MDM warning banner (hidden when MDM is usable)
        self.banner = QLabel()
        self.banner.setObjectName("banner")
        self.banner.setWordWrap(True)
        self.banner.setTextFormat(Qt.TextFormat.RichText)
        self.banner.linkActivated.connect(self._open_settings)
        self.banner.hide()
        layout.addWidget(self.banner)

        # URL row
        url_row = QHBoxLayout()
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("Paste a YouTube playlist or video link…")
        self.url_edit.returnPressed.connect(self._fetch_playlist)
        self.fetch_btn = QPushButton("Fetch")
        self.fetch_btn.setObjectName("primary")
        self.fetch_btn.clicked.connect(self._fetch_playlist)
        url_row.addWidget(self.url_edit, 1)
        url_row.addWidget(self.fetch_btn)
        layout.addLayout(url_row)

        # Status line
        status_row = QHBoxLayout()
        self.status_label = QLabel("Paste a YouTube link and press Fetch.")
        self.status_label.setObjectName("status")
        status_row.addWidget(self.status_label, 1)
        self.output_label = QLabel()
        self.output_label.setObjectName("status")
        self.output_label.setToolTip("Download folder")
        status_row.addWidget(self.output_label)
        change_out = QPushButton("Change…")
        change_out.clicked.connect(self._change_output_dir)
        status_row.addWidget(change_out)
        layout.addLayout(status_row)
        self._update_output_label()

        # Table
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["", "Title", "Duration", "Status", "Progress / Speed"])
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setWordWrap(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(0, 34)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(2, 90)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(3, 120)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(4, 250)
        layout.addWidget(self.table, 1)

        # Bottom bar
        bottom = QHBoxLayout()
        self.select_btn = QPushButton("Select All")
        self.select_btn.clicked.connect(self._toggle_select_all)
        bottom.addWidget(self.select_btn)

        self.download_btn = QPushButton("Download Selected")
        self.download_btn.setObjectName("primary")
        self.download_btn.clicked.connect(self._download_selected)
        self.download_btn.setEnabled(False)
        bottom.addWidget(self.download_btn)

        self.stop_btn = QPushButton("Stop All")
        self.stop_btn.clicked.connect(self._stop_all)
        self.stop_btn.setEnabled(False)
        bottom.addWidget(self.stop_btn)

        self.retry_btn = QPushButton("Retry Failed")
        self.retry_btn.clicked.connect(self._retry_failed)
        self.retry_btn.setEnabled(False)
        bottom.addWidget(self.retry_btn)

        bottom.addStretch(1)

        self.open_btn = QPushButton("Open Folder")
        self.open_btn.clicked.connect(self._open_folder)
        bottom.addWidget(self.open_btn)

        self.clear_btn = QPushButton("Clear List")
        self.clear_btn.clicked.connect(self._clear_list)
        bottom.addWidget(self.clear_btn)

        layout.addLayout(bottom)

        self.statusBar().showMessage("Ready")

    # -- config-driven labels ----------------------------------------------
    def _update_output_label(self) -> None:
        out = self.config.get("output_dir", "")
        self.output_label.setText(f"Save to: {out}")
        self.output_label.setToolTip(out)

    def _update_theme_button(self) -> None:
        current = self.config.get("theme", "dark")
        self.theme_btn.setText("Light mode" if current == "dark" else "Dark mode")

    def _sync_quality_combo(self) -> None:
        index = self.quality_combo.findData(self.config.get("format", "best"))
        self.quality_combo.blockSignals(True)
        self.quality_combo.setCurrentIndex(max(0, index))
        self.quality_combo.blockSignals(False)

    def _on_quality_changed(self, index: int) -> None:
        key = self.quality_combo.itemData(index)
        if not key:
            return
        self.config.data["format"] = key
        self.config.save()
        self.statusBar().showMessage(f"Quality set to {self.quality_combo.itemText(index)}")

    def _refresh_mdm_state(self) -> None:
        self.mdm_tool = MdmLocator.detect(self.config.get("mdm_cli", "auto"))
        if self.mdm_tool:
            ok, _ = MdmLocator.validate(self.mdm_tool)
            if not ok:
                self.mdm_tool = None
        if self.mdm_tool:
            self.banner.hide()
        else:
            self.banner.setText(
                "Mister Download Manager and the bundled video engine were not found "
                "or failed to start. Downloads will use the slower built-in yt-dlp "
                'downloader. <a href="settings">Open Settings</a>.'
            )
            self.banner.show()

    # -- theme --------------------------------------------------------------
    def _toggle_theme(self) -> None:
        new_theme = "light" if self.config.get("theme", "dark") == "dark" else "dark"
        self.config.data["theme"] = new_theme
        self.config.save()
        app = QApplication.instance()
        if app:
            app.setStyleSheet(DARK_QSS if new_theme == "dark" else LIGHT_QSS)
        self._update_theme_button()

    # -- fetching -----------------------------------------------------------
    def _fetch_playlist(self) -> None:
        if self._download_active:
            return
        if self._fetcher and self._fetcher.isRunning():
            self._fetcher.cancel()
            return
        url = self.url_edit.text().strip()
        if not url:
            QMessageBox.information(self, "YouTube link", "Paste a YouTube link first.")
            return
        if not re.match(r"^https?://", url, re.IGNORECASE):
            QMessageBox.warning(self, "Playlist URL",
                                "The address must start with http:// or https://.")
            return

        self._reset_results()
        self.fetch_btn.setEnabled(False)
        self.fetch_btn.setText("Fetching…")
        self._set_status("Fetching playlist metadata…", "#4da3ff")

        self._fetcher = PlaylistFetcher(url, self.config.get("cookies_from_browser", ""), self)
        self._fetcher.loaded.connect(self._on_playlist_loaded)
        self._fetcher.failed.connect(self._on_playlist_failed)
        self._fetcher.warning.connect(self._on_fetch_warning)
        self._fetcher.finished.connect(self._on_fetch_finished)
        self._fetcher.start()

        self.statusBar().showMessage("Reading playlist…")

    def _on_fetch_finished(self) -> None:
        self.fetch_btn.setEnabled(not self._download_active)
        self.fetch_btn.setText("Fetch")
        sender = self.sender()
        if sender is self._fetcher:
            self._fetcher = None

    def _on_fetch_warning(self, message: str) -> None:
        self.statusBar().showMessage(message, 12000)

    def _on_playlist_loaded(self, items: list, playlist_title: str) -> None:
        self._playlist_title = str(playlist_title or "Playlist")
        self.table.setUpdatesEnabled(False)
        try:
            self.table.setRowCount(0)
            self.table.setRowCount(len(items))
            self._items.clear()
            self._row_of.clear()
            self._status.clear()

            for row, item in enumerate(items):
                self._items[item.key] = item
                self._row_of[item.key] = row
                self._status[item.key] = "Pending"

                check = QTableWidgetItem()
                check.setFlags(
                    Qt.ItemFlag.ItemIsUserCheckable
                    | Qt.ItemFlag.ItemIsEnabled
                    | Qt.ItemFlag.ItemIsSelectable
                )
                check.setCheckState(Qt.CheckState.Checked)
                self.table.setItem(row, 0, check)

                title = QTableWidgetItem(item.title)
                title.setData(Qt.ItemDataRole.UserRole, item.key)
                title.setToolTip(f"{item.url}\n{item.thumbnail}")
                self.table.setItem(row, 1, title)

                duration = QTableWidgetItem(format_duration(item.duration))
                duration.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(row, 2, duration)

                status = QTableWidgetItem("Pending")
                status.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                status.setForeground(QBrush(QColor(STATUS_COLORS["Pending"])))
                self.table.setItem(row, 3, status)

                progress = QTableWidgetItem("")
                progress.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(row, 4, progress)
        finally:
            self.table.setUpdatesEnabled(True)

        count = len(items)
        if count == 0:
            self._set_status("No videos were found at that URL.", "#e0a33a")
            self.download_btn.setEnabled(False)
        else:
            self._set_status(f"{count} videos · {self._playlist_title}", "#43b581")
            self.download_btn.setEnabled(True)
        self._update_select_button()
        self.statusBar().showMessage(f"Loaded {count} videos")

    def _on_playlist_failed(self, message: str) -> None:
        self._set_status("Could not read the playlist.", "#e5534b")
        self.statusBar().showMessage("Fetch failed")
        QMessageBox.critical(self, "Playlist error", message)
        self.download_btn.setEnabled(False)

    # -- table helpers ------------------------------------------------------
    def _set_status(self, text: str, color: str) -> None:
        self.status_label.setText(text)
        self.status_label.setStyleSheet(f"color: {color};")

    def _set_cell(self, row: int, column: int, text: str) -> None:
        if 0 <= row < self.table.rowCount():
            item = self.table.item(row, column)
            if item:
                item.setText(text)

    def _set_row_status(self, key: str, text: str) -> None:
        self._status[key] = text
        row = self._row_of.get(key)
        if row is None:
            return
        item = self.table.item(row, 3)
        if item:
            item.setText(text)
            item.setForeground(QBrush(QColor(STATUS_COLORS.get(text, "#9aa0a6"))))

    def _toggle_select_all(self) -> None:
        if self.table.rowCount() == 0:
            return
        all_checked = all(
            self.table.item(r, 0) and self.table.item(r, 0).checkState() == Qt.CheckState.Checked
            for r in range(self.table.rowCount())
        )
        state = Qt.CheckState.Unchecked if all_checked else Qt.CheckState.Checked
        self.table.setUpdatesEnabled(False)
        try:
            for row in range(self.table.rowCount()):
                item = self.table.item(row, 0)
                if item:
                    item.setCheckState(state)
        finally:
            self.table.setUpdatesEnabled(True)
        self._update_select_button()

    def _update_select_button(self) -> None:
        if self.table.rowCount() == 0:
            self.select_btn.setText("Select All")
            return
        all_checked = all(
            self.table.item(r, 0) and self.table.item(r, 0).checkState() == Qt.CheckState.Checked
            for r in range(self.table.rowCount())
        )
        self.select_btn.setText("Select None" if all_checked else "Select All")

    def _checked_keys(self) -> list[str]:
        keys = []
        for row in range(self.table.rowCount()):
            cell = self.table.item(row, 0)
            title = self.table.item(row, 1)
            if cell and title and cell.checkState() == Qt.CheckState.Checked:
                keys.append(title.data(Qt.ItemDataRole.UserRole))
        return keys

    def _reset_results(self) -> None:
        self.table.setRowCount(0)
        self._items.clear()
        self._row_of.clear()
        self._status.clear()
        self._playlist_dir = None
        self._update_select_button()
        self.retry_btn.setEnabled(False)

    def _clear_list(self) -> None:
        if self._download_active:
            QMessageBox.information(self, "Downloads running",
                                    "Stop the current downloads first.")
            return
        self._reset_results()
        self._playlist_title = "Playlist"
        self.download_btn.setEnabled(False)
        self._set_status("List cleared.", "#9aa0a6")
        self.statusBar().showMessage("Ready")

    # -- settings / output --------------------------------------------------
    def _open_settings(self, *_args) -> None:
        dialog = SettingsDialog(self.config.data, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values = dialog.values()
        self.config.data.update(values)
        self.config.save()
        self._refresh_mdm_state()
        self._update_output_label()
        self._update_theme_button()
        self._sync_quality_combo()
        app = QApplication.instance()
        if app:
            app.setStyleSheet(DARK_QSS if self.config.get("theme") == "dark" else LIGHT_QSS)
        self.statusBar().showMessage("Settings saved")
        if self._download_active:
            self._set_status(
                "Settings saved. They apply to the next download batch.", "#e0a33a"
            )

    def _change_output_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Choose download folder", self.config.get("output_dir", "")
        )
        if not path:
            return
        self.config.data["output_dir"] = path
        self.config.save()
        self._update_output_label()
        self.statusBar().showMessage("Output folder changed")

    def _ensure_playlist_dir(self) -> Optional[Path]:
        root = Path(self.config.get("output_dir") or (Path.home() / "Downloads"))
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.critical(self, "Output folder", f"Cannot create the output folder:\n{exc}")
            return None
        folder = root / sanitize_name(self._playlist_title or "Playlist")
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.critical(self, "Output folder", f"Cannot create the playlist folder:\n{exc}")
            return None
        return folder

    def _build_settings(self) -> DownloadSettings:
        engine = self.config.get("engine", "video-get")
        tool = MdmLocator.detect(self.config.get("mdm_cli", "auto"))
        if tool:
            ok, _ = MdmLocator.validate(tool)
            if not ok:
                tool = None
        self.mdm_tool = tool
        if tool is None:
            engine = "native"
        return DownloadSettings(
            engine=engine,
            format_key=self.config.get("format", "best"),
            connections=int(self.config.get("connections", 8)),
            concurrency=int(self.config.get("concurrency", 2)),
            subs=self.config.get("subs", ""),
            embed_subs=bool(self.config.get("embed_subs", False)),
            playlist_dir=self._playlist_dir or (Path.home() / "Downloads"),
            tool=tool,
            cookies=self.config.get("cookies_from_browser", ""),
        )

    # -- downloading --------------------------------------------------------
    def _download_selected(self) -> None:
        if self._download_active:
            return
        keys = self._checked_keys()
        if not keys:
            QMessageBox.information(self, "Nothing selected",
                                    "Tick at least one video first.")
            return
        keys = [k for k in keys if self._status.get(k) != "Complete"]
        if not keys:
            QMessageBox.information(self, "Already complete",
                                    "All selected videos are already downloaded in this session.")
            return
        folder = self._ensure_playlist_dir()
        if folder is None:
            return
        self._playlist_dir = folder

        settings = self._build_settings()
        if (settings.engine == "native"
                and settings.format_key in AUDIO_CONVERSION_KEYS
                and find_ffmpeg() is None):
            QMessageBox.warning(
                self,
                "ffmpeg not found",
                "ffmpeg is required to convert audio to MP3, Opus or FLAC.\n\n"
                "Install it with:\n    winget install Gyan.FFmpeg\n\n"
                "Or choose Audio only (Original).",
            )
            return
        if (settings.engine == "native" and settings.format_key in VIDEO_FORMAT_KEYS
                and not self._ffmpeg_warned and find_ffmpeg() is None):
            self._ffmpeg_warned = True
            choice = QMessageBox.warning(
                self,
                "ffmpeg not found",
                "yt-dlp needs ffmpeg to merge YouTube's separate video and audio "
                "streams, and most videos no longer offer a combined format.\n\n"
                "Install it with:\n    winget install Gyan.FFmpeg\n\n"
                "Download anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if choice != QMessageBox.StandardButton.Yes:
                return
        engine_name = {
            "video-get": "Mister Download Manager (video-get)",
            "direct": "Mister Download Manager (direct URL)",
            "native": "yt-dlp fallback",
        }.get(settings.engine, settings.engine)
        self._set_status(
            f"Downloading {len(keys)} video(s) · {settings.connections} connections · {engine_name}",
            "#4da3ff",
        )
        for key in keys:
            self._set_row_status(key, "Queued")
            self._set_cell(self._row_of[key], 4, "")
        self._download_active = True
        self._set_busy(True)
        jobs = [(key, self._items[key]) for key in keys if key in self._items]
        self.manager.start(jobs, settings)
        self.statusBar().showMessage(f"Started {len(jobs)} downloads")

    def _set_busy(self, busy: bool) -> None:
        self._download_active = busy
        self.fetch_btn.setEnabled(not busy)
        self.download_btn.setEnabled(not busy and self.table.rowCount() > 0)
        self.select_btn.setEnabled(not busy)
        self.clear_btn.setEnabled(not busy)
        self.quality_combo.setEnabled(not busy)
        self.settings_btn.setEnabled(True)
        self.stop_btn.setEnabled(busy)
        if not busy:
            self.retry_btn.setEnabled(self._has_failed())

    def _has_failed(self) -> bool:
        return any(v in ("Error", "Paused", "Cancelled") for v in self._status.values())

    def _on_progress(self, key: str, percent: float, speed: str, eta: str) -> None:
        row = self._row_of.get(key)
        if row is None:
            return
        if self._status.get(key) != "Downloading":
            self._set_row_status(key, "Downloading")
        parts = []
        if percent >= 0:
            parts.append(f"{percent:.0f}%")
        if speed:
            parts.append(speed)
        if eta:
            parts.append(f"ETA {eta}")
        self._set_cell(row, 4, " · ".join(parts) if parts else "…")

    def _on_done(self, key: str, path: str) -> None:
        self._set_row_status(key, "Complete")
        row = self._row_of.get(key)
        if row is not None:
            self._set_cell(row, 4, "100%")
            if path:
                title_item = self.table.item(row, 1)
                if title_item:
                    title_item.setToolTip(f"{self._items[key].url}\nSaved: {path}")
        self.statusBar().showMessage(f"Saved: {path or 'completed'}")

    def _on_error(self, key: str, message: str) -> None:
        self._set_row_status(key, "Error")
        row = self._row_of.get(key)
        if row is not None:
            self._set_cell(row, 4, "Failed")
            title_item = self.table.item(row, 1)
            if title_item:
                title_item.setToolTip(f"{self._items[key].url}\nError: {message}")
        self.statusBar().showMessage(
            f"Error: {((message or '').splitlines() or ['Unknown error'])[0]}"
        )

    def _on_cancelled(self, key: str) -> None:
        self._set_row_status(key, "Paused")
        row = self._row_of.get(key)
        if row is not None:
            self._set_cell(row, 4, "Paused")
        self.statusBar().showMessage("Stopped — use Retry Failed to start it again")

    def _on_all_finished(self) -> None:
        self._set_busy(False)
        complete = sum(1 for v in self._status.values() if v == "Complete")
        errors = sum(1 for v in self._status.values() if v == "Error")
        paused = sum(1 for v in self._status.values() if v in ("Paused", "Cancelled"))
        self._set_status(
            f"Finished · {complete} complete · {errors} failed · {paused} paused",
            "#43b581" if errors == 0 and paused == 0 else "#e0a33a",
        )
        self.statusBar().showMessage("All downloads finished")
        self.retry_btn.setEnabled(self._has_failed())

    def _stop_all(self) -> None:
        if not self._download_active:
            return
        self._set_status("Stopping… (MDM keeps resumable state)", "#e0a33a")
        self.manager.cancel_all()

    def _retry_failed(self) -> None:
        if self._download_active:
            return
        failed = [k for k, v in self._status.items() if v in ("Error", "Paused", "Cancelled")]
        if not failed:
            return
        folder = self._ensure_playlist_dir()
        if folder is None:
            return
        self._playlist_dir = folder

        settings = self._build_settings()
        for key in failed:
            self._set_row_status(key, "Queued")
            row = self._row_of.get(key)
            if row is not None:
                self._set_cell(row, 4, "")
        self._download_active = True
        self._set_busy(True)
        self._set_status(f"Retrying {len(failed)} video(s)…", "#4da3ff")
        jobs = [(key, self._items[key]) for key in failed if key in self._items]
        self.manager.start(jobs, settings)

    # -- misc actions -------------------------------------------------------
    def _open_folder(self) -> None:
        folder = self._playlist_dir
        if folder is None:
            folder = Path(self.config.get("output_dir") or (Path.home() / "Downloads"))
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.warning(self, "Open folder", f"Cannot open the folder:\n{exc}")
            return
        try:
            if IS_WIN:
                os.startfile(str(folder))  # type: ignore[attr-defined]
            elif IS_MAC:
                subprocess.Popen(["open", str(folder)])
            else:
                subprocess.Popen(["xdg-open", str(folder)])
        except OSError as exc:
            QMessageBox.warning(self, "Open folder", f"Cannot open the folder:\n{exc}")

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self._download_active:
            answer = QMessageBox.question(
                self,
                "Downloads running",
                "Downloads are still running. Stop them and exit?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.manager.cancel_all()
            self.manager.pool.waitForDone(3000)
        if self._fetcher and self._fetcher.isRunning():
            self._fetcher.cancel()
            self._fetcher.wait(3000)
        try:
            self.config.save()
        except OSError:
            pass
        event.accept()


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    config = ConfigManager()
    app.setStyleSheet(DARK_QSS if config.get("theme", "dark") == "dark" else LIGHT_QSS)
    window = MainWindow(config)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
