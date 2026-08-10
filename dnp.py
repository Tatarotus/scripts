#!/usr/bin/env python3
"""
download_and_process.py — Hardened torrent → SSD → HDD pipeline (Python).

Architecture:
  - Up to MAX_CONCURRENT (default 3) magnets downloading to the SSD in
    parallel, each as an asyncio task gated by a Semaphore.
  - Each task: add to qBittorrent → wait for completion → ffprobe →
    remux via remux.sh → stage the output file in a stable directory → push onto an asyncio.Queue.
  - A single rsync worker drains the queue, transferring one file at a
    time to the external HDD. We never parallelize rsync; spinning rust
    drives hate it.
  - All heavy lifting is in external tools (qBittorrent, remux.sh, ffprobe, rsync, flatpak, yt-dlp).
    This script is pure orchestration.

Requires Python 3.10+.
Dependencies: nala install python3-rich

Environment overrides (all optional):
    QBT_WEBUI_URL            default: http://localhost:8080
    QBT_USER                 default: admin
    QBT_PASS                 password (visible in env — less safe)
    QBT_PASS_FILE            path to a chmod-600 file containing the password
    MAX_CONCURRENT           default: 3 (parallel downloads to SSD)
    SSD_BUFFER               default: ~/torrent_buffer
    POLL_INTERVAL            default: 10 (seconds)
    POLL_TIMEOUT             default: 3600 (seconds)
    REMUX_SCRIPT              default: ~/scripts/remux.sh

CLI:
    download_and_process.py [input_file] [--max-concurrent N] [--test] [--organize] [--dry-run]

    --test      Run internal sanitizer + decision unit tests, then exit.
    --organize  Scan & reorganize HDD (merge duplicates, create season dirs)
    --dry-run   Preview changes without executing (works with --organize)
"""

from __future__ import annotations

import argparse
import asyncio
import http.cookiejar
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Rich Library Imports
from rich.live import Live
from rich.progress import Progress, BarColumn, TextColumn, TaskProgressColumn
from rich.console import Group
from rich.panel import Panel
from rich.text import Text

# =============================================================================
# Utilities
# =============================================================================

def strip_ansi(text: str) -> str:
    ansi_escape = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')
    return ansi_escape.sub('', text)

def _timestamp() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y%m%d_%H%M%S")

# =============================================================================
# Configuration
# =============================================================================

def _resolve_password() -> str:
    """Prefer QBT_PASS_FILE (chmod 600) over QBT_PASS env var."""
    pw_file = os.environ.get("QBT_PASS_FILE")
    if pw_file and os.access(pw_file, os.R_OK):
        with open(pw_file) as f:
            return f.read().rstrip("\n")
    return os.environ.get("QBT_PASS", "adminadmin")


@dataclass
class Config:
    ssd_buffer: Path
    hdd_mount: Path
    hdd_series_dir: Path
    remux_script: Path
    safety_threshold_gb: int
    min_free_space_gb: int
    qbt_webui_url: str
    qbt_user: str
    qbt_pass: str
    max_concurrent: int
    poll_interval: int
    poll_timeout: int
    organize_whitelist: dict[str, set[str]]
    best_quality: bool = False


def load_config() -> Config:
    return Config(
        ssd_buffer=Path(os.environ.get("SSD_BUFFER", str(Path.home() / "torrent_buffer"))),
        hdd_mount=Path("/media/sam/Videos"),
        hdd_series_dir=Path("/media/sam/Videos/SERIES"),
        remux_script=Path(os.environ.get("REMUX_SCRIPT", str(Path.home() / "scripts" / "remux.sh"))),
        safety_threshold_gb=int(os.environ.get("SAFETY_THRESHOLD_GB", "4")),
        min_free_space_gb=int(os.environ.get("MIN_FREE_SPACE_GB", "4")),
        qbt_webui_url=os.environ.get("QBT_WEBUI_URL", "http://localhost:8080"),
        qbt_user=os.environ.get("QBT_USER", "admin"),
        qbt_pass=_resolve_password(),
        max_concurrent=int(os.environ.get("MAX_CONCURRENT", "3")),
        poll_interval=int(os.environ.get("POLL_INTERVAL", "10")),
        poll_timeout=int(os.environ.get("POLL_TIMEOUT", "3600")),
        organize_whitelist={
            "FILMES": set(),
            "OUTROS": set(),
        },
    )


# =============================================================================
# Logging
# =============================================================================

class DashboardLoggingHandler(logging.Handler):
    def emit(self, record):
        try:
            msg = self.format(record)
            dash = Dashboard.get_instance()
            if dash._live and dash._live.is_started:
                if not dash.should_suppress_live_log(msg):
                    dash.log(msg)
            else:
                sys.stdout.write(msg + "\n")
                sys.stdout.flush()
        except Exception:
            self.handleError(record)


log = logging.getLogger("pipeline")
log_file_path: Optional[Path] = None


def setup_logging() -> Path:
    global log_file_path
    log_dir = Path.home() / ".local" / "share" / "download_and_process" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"pipeline_{_timestamp()}.log"
    log_file_path = log_file

    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    formatter = logging.Formatter(fmt)

    log.setLevel(logging.INFO)
    log.propagate = False
    fh = logging.FileHandler(str(log_file))
    fh.setFormatter(formatter)
    sh = DashboardLoggingHandler()
    sh.setFormatter(formatter)
    log.addHandler(fh)
    log.addHandler(sh)
    return log_file_path


# =============================================================================
# Animated Progress Dashboard (Rich Implementation)
# =============================================================================

class TaskStatus:
    DOWNLOADING = "downloading"
    PROCESSING = "processing"
    REMUXING = "remuxing"
    STAGING = "staging"
    RSYNC = "rsync"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class Dashboard:
    _instance: Optional["Dashboard"] = None

    def __init__(self) -> None:
        self.progress = Progress(
            TextColumn("[bold cyan]{task.description}"),
            BarColumn(bar_width=40),
            TaskProgressColumn(),
            TextColumn("{task.fields[status_text]}"),
            TextColumn("[dim]{task.fields[detail]}"),
        )
        self._tasks: dict[str, int] = {}
        self._log_lines: list[str] = []
        self._live: Optional[Live] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._log_lock = threading.Lock()
        self._layout: Optional[Group] = None

    @classmethod
    def get_instance(cls) -> "Dashboard":
        if cls._instance is None:
            cls._instance = Dashboard()
        return cls._instance

    def start(self, stop_event: asyncio.Event) -> None:
        self._stop_event = stop_event
        self._layout = Group(
            Panel(self.progress, title="[bold cyan]DOWNLOAD & PROCESS PIPELINE[/bold cyan]", border_style="cyan"),
            Panel(Text(""), title="[bold dim]Live Logs[/bold dim]", border_style="dim")
        )
        self._live = Live(self._layout, refresh_per_second=6)
        self._live.start()

    async def stop(self) -> None:
        if self._live:
            self._live.stop()

    def set_stop_event(self, stop_event: asyncio.Event) -> None:
        self._stop_event = stop_event

    def register(self, key: str, label: str) -> None:
        if key not in self._tasks:
            task_id = self.progress.add_task(
                description=label, 
                total=100.0, 
                status_text=f"[cyan]{TaskStatus.DOWNLOADING}[/cyan]",
                detail=""
            )
            self._tasks[key] = task_id

    def unregister(self, key: str) -> None:
        if key in self._tasks:
            self.progress.remove_task(self._tasks[key])
            del self._tasks[key]

    def update(self, key: str, status: str, progress: float = 0.0, detail: str = "") -> None:
        if key in self._tasks:
            task_id = self._tasks[key]
            
            color = "cyan"
            if status == TaskStatus.DONE:
                color = "green"
            elif status == TaskStatus.FAILED:
                color = "red"
            elif status == TaskStatus.SKIPPED:
                color = "dim"
            elif status in (TaskStatus.RSYNC, TaskStatus.STAGING, TaskStatus.REMUXING):
                color = "yellow"
            
            status_formatted = f"[{color}]{status}[/{color}]"
            
            self.progress.update(
                task_id, 
                completed=progress * 100, 
                status_text=status_formatted,
                detail=detail
            )

    def remove(self, key: str) -> None:
        self.unregister(key)

    def log(self, line: str) -> None:
        with self._log_lock:
            self._log_lines.append(self._sanitize_text(line))
            if len(self._log_lines) > 8:
                self._log_lines.pop(0)
            
            if self._layout:
                log_text = "\n".join(self._log_lines)
                self._layout.renderables[1] = Panel(Text(log_text), title="[bold dim]Live Logs[/bold dim]", border_style="dim")

    def should_suppress_live_log(self, line: str) -> bool:
        return False

    def _sanitize_text(self, text: str) -> str:
        text = strip_ansi(str(text))
        text = text.replace("\r", " ").replace("\n", " ").replace("\t", " ")
        text = "".join(ch if ch.isprintable() else " " for ch in text)
        return re.sub(r"\s+", " ", text).strip()


def dashboard_log(msg: str) -> None:
    dash = Dashboard.get_instance()
    dash.log(msg)


# =============================================================================
# Path sanitization (security-critical)
# =============================================================================

ALLOWED_NAME_CHARS = re.compile(r"[^A-Za-z0-9 .,_&()']")


def sanitize_show_name(raw: str) -> str:
    """Strict allowlist. Defence-in-depth against path traversal from a
    maliciously-crafted magnet dn= parameter."""
    if not raw:
        return "Unknown_Show"
    cleaned = ALLOWED_NAME_CHARS.sub("", raw)
    cleaned = re.sub(r"^\.+", "", cleaned)
    cleaned = re.sub(r"\.{2,}", ".", cleaned)
    cleaned = cleaned.replace("/", "_")
    cleaned = re.sub(r"^[\s\-]+", "", cleaned)
    cleaned = re.sub(r"_+", "_", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = cleaned.strip()
    if not cleaned or cleaned in (".", ".."):
        return "Unknown_Show"
    return cleaned


def derive_show_name(torrent_name: str) -> str:
    """Strip release tags (S01E01, 1080p, WEB-DL, …) → sanitized show name."""
    name = torrent_name
    
    # Strip TV Compatible, remux, and extensions first
    name = re.sub(r"_TV_Compatible$", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\.remux$", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\.(mp4|mkv|avi|m4v|webm|mov|ts|wmv)$", "", name, flags=re.IGNORECASE)
    
    name = re.sub(r"^\[[^\]]*\]\s*", "", name)
    name = re.sub(r"^\([^)]*\)\s*", "", name)
    
    # Strip year (with or without parentheses) and everything after it
    name = re.sub(r"\s*\((19|20)\d{2}\).*$", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\s*\b(19|20)\d{2}\b.*$", "", name, flags=re.IGNORECASE)
    
    # Strip uploader suffixes like " - ToTTi9" when no year is present
    name = re.sub(r"\s+-\s+[A-Za-z0-9_]+$", "", name)
    
    name = re.sub(r"\s*\[[^\]]*\]", "", name)
    name = re.sub(r"\s*\([^)]*\)", "", name)
    name = re.sub(r"[._]", " ", name)
    name = re.sub(r"\s+", " ", name)
    name = re.sub(r" - \d+.*$", "", name, flags=re.IGNORECASE)
    name = re.sub(r" S\d+.*$", "", name, flags=re.IGNORECASE)
    name = re.sub(r" Season\s*\d+.*$", "", name, flags=re.IGNORECASE)
    name = re.sub(r" \d{4}.*$", "", name)
    name = re.sub(r" 1080p.*$", "", name, flags=re.IGNORECASE)
    name = re.sub(r" 720p.*$", "", name, flags=re.IGNORECASE)
    name = re.sub(r" 480p.*$", "", name, flags=re.IGNORECASE)
    name = re.sub(r" 2160p.*$", "", name, flags=re.IGNORECASE)
    name = re.sub(r" 4K.*$", "", name, flags=re.IGNORECASE)
    name = re.sub(r" WEB.*$", "", name, flags=re.IGNORECASE)
    name = re.sub(r" BluRay.*$", "", name, flags=re.IGNORECASE)
    name = re.sub(r" BDRip.*$", "", name, flags=re.IGNORECASE)
    name = re.sub(r" HDTV.*$", "", name, flags=re.IGNORECASE)
    name = re.sub(r" (Dual|Dublado|Dubbed|Legendado|5\.1|7\.1|5\s*1|7\s*1|CH|Multi|x264|x265|h264|h265|hevc|AAC|AC3|EAC3|DDP).*$", "", name, flags=re.IGNORECASE)
    return sanitize_show_name(name.strip())


def parse_torrent_name(magnet: str) -> str:
    m = re.search(r"dn=([^&]+)", magnet)
    if not m:
        return "Unknown_Show"
    return urllib.parse.unquote(m.group(1))


# =============================================================================
# Classification (Anime / Movie / Series / Other)
# =============================================================================

ANIME_KEYWORDS = [
    "horriblesubs", "subsplease", "erai-raws", "commie", "gg", "coalguys",
    "doki", "utw", "fate", "nyaa", "anime", "batch", "bd", "ova", "ona",
    "season", "s01", "s02", "s03", "s04", "s05",
]

SERIES_PATTERNS = [
    re.compile(r"[Ss]\d{1,2}[Ee]\d{1,2}"),  # S01E01, s01e01
    re.compile(r"[Ss]eason[\s.]?\d{1,2}"),   # Season 1, Season.1
    re.compile(r"\d{1,2}x\d{1,2}"),          # 1x01, 12x05
]

MOVIE_PATTERNS = [
    re.compile(r"\(\d{4}\)"),               # (2023), (1999)
    re.compile(r"\d{4}\s+(?:BD|BluRay|WEB|DVD|HDRip)"),  # 2023 BluRay
]


def classify_media(torrent_name: str, duration_seconds: float = 0.0) -> tuple[str, str]:
    """Classify media and return (category, clean_name).

    Categories: "ANIMES", "SERIES", "FILMES", "OUTROS"
    """
    lower = torrent_name.lower()
    clean = derive_show_name(torrent_name)

    # 1. Anime detection
    anime_score = 0
    for kw in ANIME_KEYWORDS:
        if kw in lower:
            anime_score += 1
    if 0 < duration_seconds <= 1800:
        anime_score += 2
    if re.search(r"^\[[^\]]+\]", torrent_name):
        anime_score += 2
    if re.search(r"\s-\s\d{1,3}\s*\[", torrent_name) or re.search(r"\s-\s\d{1,3}\b", torrent_name):
        anime_score += 1

    if anime_score >= 3:
        return "ANIMES", clean

    # 2. Series detection
    for pat in SERIES_PATTERNS:
        if pat.search(torrent_name):
            return "SERIES", clean

    # 3. Movie detection
    for pat in MOVIE_PATTERNS:
        if pat.search(torrent_name):
            return "FILMES", clean

    # 4. Duration-based fallback
    if duration_seconds > 2700:  # > 45 min
        return "FILMES", clean
    elif duration_seconds > 0:
        return "SERIES", clean

    # 5. Default fallback
    return "OUTROS", clean


# =============================================================================
# Magnet and URL Parsing
# =============================================================================

INFOHASH_PATTERNS = (
    re.compile(r"xt=urn:btih:([a-fA-F0-9]{40})"),  # v1 (40 hex)
    re.compile(r"xt=urn:btih:([a-zA-Z2-7]{32})"),  # compact v1 (32 base32)
)


def get_infohash(magnet: str) -> str:
    for pat in INFOHASH_PATTERNS:
        m = pat.search(magnet)
        if m:
            return m.group(1).lower()
    return ""


@dataclass
class LinkEntry:
    url: str
    kind: str  # "magnet" | "youtube"


def is_link(s: str) -> bool:
    s = s.strip()
    return s.startswith("magnet:") or "youtube.com" in s or "youtu.be" in s or s.startswith("http://") or s.startswith("https://")


def extract_links(path_or_link: Path | str) -> list[LinkEntry]:
    """Parse input file or direct link for magnet links AND YouTube URLs."""
    s = str(path_or_link).strip()
    if is_link(s):
        links: list[LinkEntry] = []
        if "magnet:" in s:
            idx = s.find("magnet:")
            magnet_url = s[idx:]
            links.append(LinkEntry(url=magnet_url, kind="magnet"))
        elif "youtube.com" in s or "youtu.be" in s:
            m = re.search(r"https?://[^\s#]+", s)
            url = m.group(0) if m else s
            links.append(LinkEntry(url=url, kind="youtube"))
        return links

    path = Path(path_or_link)
    links: list[LinkEntry] = []
    seen_yt_ids = set()
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            
            if "magnet:" in line:
                idx = line.find("magnet:")
                magnet_url = line[idx:]
                links.append(LinkEntry(url=magnet_url, kind="magnet"))
            elif "youtube.com" in line or "youtu.be" in line:
                m = re.search(r"https?://[^\s#]+", line)
                url = m.group(0) if m else line
                yt_id = extract_youtube_id(url)
                if yt_id:
                    if yt_id in seen_yt_ids:
                        log.warning("Duplicate YouTube video ID skipped: %s (URL: %s)", yt_id, url)
                        continue
                    seen_yt_ids.add(yt_id)
                links.append(LinkEntry(url=url, kind="youtube"))
    return links


# =============================================================================
# Season/Episode Parsing & Canonicalization
# =============================================================================

def parse_season_episode(filename: str) -> tuple[int, int]:
    """Return (season, episode) from filename. Season 0 = specials."""
    m = re.search(r"(?i)\bS(\d+)\s*E(\d+)(?![A-Za-z0-9])", filename)
    if m:
        return int(m.group(1)), int(m.group(2))
    
    m = re.search(r"\b(\d+)x(\d+)(?![A-Za-z0-9])", filename)
    if m:
        return int(m.group(1)), int(m.group(2))
    
    m = re.search(r"(?i)\bSeason\s*[\.]?\s*(\d+)\s*[._-]*\s*E(?:pisode)?\s*(\d+)(?![A-Za-z0-9])", filename)
    if m:
        return int(m.group(1)), int(m.group(2))

    season = None
    m_season = re.search(r"(?i)\bSeason\s*[\.]?\s*(\d+)(?![A-Za-z0-9])", filename)
    if m_season:
        season = int(m_season.group(1))
    else:
        m_season = re.search(r"(?i)\b(\d+)(?:st|nd|rd|th)?\s+Season\b", filename)
        if m_season:
            season = int(m_season.group(1))
        else:
            m_season = re.search(r"(?i)\bS(\d{1,2})(?![A-Za-z0-9])", filename)
            if m_season:
                season = int(m_season.group(1))

    episode = None
    m_ep = re.search(r"(?i)\bE(?:pisode)?\s*(\d+)(?![A-Za-z0-9])", filename)
    if m_ep:
        episode = int(m_ep.group(1))
    else:
        m_ep = re.search(r"(?i)\bEpisodio\s*(\d+)(?![A-Za-z0-9])", filename)
        if m_ep:
            episode = int(m_ep.group(1))
        else:
            m_ep = re.search(r"\s+-\s+(\d+)(?![A-Za-z0-9])", filename)
            if m_ep:
                episode = int(m_ep.group(1))

    if season is not None and episode is not None:
        return season, episode
    elif season is not None:
        return season, 0
    elif episode is not None:
        return 1, episode

    if re.search(r"^\[[^\]]+\]", filename):
        m = re.search(r"\s+-\s+(\d+)(?![A-Za-z0-9])", filename)
        if m:
            return 1, int(m.group(1))

    return 1, 0


def canonicalize_name(name: str) -> str:
    """Normalize for fuzzy comparison: lowercase, strip spaces/punct/accents."""
    name = name.lower()
    name = "".join(c for c in unicodedata.normalize('NFD', name) if unicodedata.category(c) != 'Mn')
    name = re.sub(r"[\s._\-\;']", "", name)
    return name


def find_canonical_folder(
    show_name: str,
    existing_dirs: list[str],
) -> Optional[str]:
    canon_input = canonicalize_name(show_name)
    matches = []
    for d in existing_dirs:
        if canonicalize_name(d) == canon_input:
            matches.append(d)
    if not matches:
        return None
    
    def prettiness(name: str) -> tuple[int, int, int]:
        spaces = name.count(" ")
        uppers = sum(1 for c in name if c.isupper())
        return (spaces, uppers, len(name))
    
    matches.sort(key=prettiness, reverse=True)
    return matches[0]


# =============================================================================
# YouTube Video ID Extraction & Naming
# =============================================================================

def extract_youtube_id(url: str) -> Optional[str]:
    m = re.search(r"youtu\.be/([a-zA-Z0-9_\-]{11})", url)
    if m:
        return m.group(1)
    
    m = re.search(r"v=([a-zA-Z0-9_\-]{11})", url)
    if m:
        return m.group(1)
    
    m = re.search(r"youtube\.com/(?:embed|v|shorts)/([a-zA-Z0-9_\-]{11})", url)
    if m:
        return m.group(1)
        
    return None


def sanitize_youtube_filename(title: str) -> str:
    p = Path(title)
    ext = p.suffix
    stem = p.stem
    
    yt_id = ""
    m = re.search(r"\[([a-zA-Z0-9_\-]{11})\]$", stem)
    if m:
        yt_id = m.group(1)
        stem = re.sub(r"\s*\[[a-zA-Z0-9_\-]{11}\]$", "", stem)
    
    sanitized_stem = sanitize_show_name(stem)
    
    if yt_id:
        return f"{sanitized_stem} [{yt_id}]{ext}"
    return sanitized_stem + ext


# =============================================================================
# qBittorrent WebUI client (stdlib only)
# =============================================================================

class QBittorrentClient:
    def __init__(self, base_url: str, user: str, password: str):
        self.base_url = base_url.rstrip("/")
        self.user = user
        self.password = password
        self._cookie_jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._cookie_jar)
        )
        self._lock = asyncio.Lock()

    def _request_sync(self, method: str, path: str, data: Optional[bytes]) -> tuple[int, str]:
        url = f"{self.base_url}{path}"
        req = urllib.request.Request(url, data=data, method=method)
        try:
            with self._opener.open(req, timeout=30) as resp:
                return resp.status, resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace") if e.fp else ""
            return e.code, body

    async def _request(self, method: str, path: str, data: Optional[dict] = None) -> tuple[int, str]:
        encoded = urllib.parse.urlencode(data).encode() if data else None
        async with self._lock:
            return await asyncio.to_thread(self._request_sync, method, path, encoded)

    async def login(self) -> None:
        status, _ = await self._request("POST", "/api/v2/auth/login", {
            "username": self.user, "password": self.password,
        })
        if status not in (200, 204):
            raise RuntimeError(f"qBittorrent login failed (HTTP {status})")

    async def logout(self) -> None:
        try:
            await self._request("POST", "/api/v2/auth/logout")
        except Exception:
            pass

    async def is_responsive(self) -> bool:
        try:
            status, _ = await self._request("GET", "/api/v2/app/version")
            return status == 200
        except Exception:
            return False

    async def add_magnet(self, magnet: str, save_dir: str) -> int:
        status, _ = await self._request("POST", "/api/v2/torrents/add", {
            "urls": magnet,
            "savepath": save_dir,
            "sequentialDownload": "true",
            "firstLastPiecePrio": "true",
            "skip_checking": "false",
            "forced": "false",
        })
        return status

    async def toggle_sequential_download(self, infohash: str) -> None:
        await self._request("POST", "/api/v2/torrents/toggleSequentialDownload", {"hashes": infohash})

    async def toggle_first_last_piece_priority(self, infohash: str) -> None:
        await self._request("POST", "/api/v2/torrents/toggleFirstLastPiecePrio", {"hashes": infohash})

    async def set_force_start(self, infohash: str, value: bool = False) -> None:
        await self._request("POST", "/api/v2/torrents/setForceStart", {
            "hashes": infohash, "value": "true" if value else "false"
        })

    async def torrent_info(self, infohash: str) -> Optional[dict]:
        status, body = await self._request("GET", f"/api/v2/torrents/info?hashes={infohash}")
        if status != 200:
            return None
        try:
            data = json.loads(body)
            return data[0] if data else None
        except (json.JSONDecodeError, IndexError):
            return None

    async def get_torrent_files(self, infohash: str) -> list[dict]:
        status, body = await self._request("GET", f"/api/v2/torrents/files?hash={infohash}")
        if status != 200:
            return []
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return []

    async def set_file_priority(self, infohash: str, file_id: int, priority: int) -> None:
        await self._request("POST", "/api/v2/torrents/filePrio", {
            "hash": infohash, "id": str(file_id), "priority": str(priority)
        })

    async def recheck(self, infohash: str) -> None:
        await self._request("POST", "/api/v2/torrents/recheck", {"hashes": infohash})

    async def delete_torrent(self, infohash: str, delete_files: bool = False) -> None:
        await self._request("POST", "/api/v2/torrents/delete", {
            "hashes": infohash, "deleteFiles": "true" if delete_files else "false",
        })

    async def pause_torrent(self, infohash: str) -> None:
        await self._request("POST", "/api/v2/torrents/pause", {"hashes": infohash})

    async def resume_torrent(self, infohash: str) -> None:
        await self._request("POST", "/api/v2/torrents/resume", {"hashes": infohash})

    async def list_torrents(self, filter_status: Optional[str] = None) -> list[dict]:
        path = "/api/v2/torrents/info"
        if filter_status:
            path += f"?filter={filter_status}"
        status, body = await self._request("GET", path)
        if status != 200:
            return []
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return []


# =============================================================================
# Filesystem helpers & Robust File Operations
# =============================================================================

def get_free_space(path: str | Path) -> int:
    st = os.statvfs(str(path))
    return st.f_bavail * st.f_frsize


def is_hdd_mounted(cfg: Config) -> bool:
    try:
        return os.stat("/").st_dev != os.stat(cfg.hdd_mount).st_dev
    except OSError:
        return False


def robust_move(src: Path, dest: Path, max_retries: int = 3, delay: float = 5.0) -> bool:
    """Move a file with retry logic for aging HDDs. Uses os.rename for same-device, shutil.move for cross-device."""
    if not src.exists():
        log.error("robust_move source missing: %s", src)
        return False
        
    try:
        src_size = src.stat().st_size
    except OSError as e:
        log.error("Failed to stat source file %s: %s", src, e)
        return False
        
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.error("Failed to create destination directory %s: %s", dest.parent, e)
        return False
        
    same_fs = False
    try:
        same_fs = os.stat(src.parent).st_dev == os.stat(dest.parent).st_dev
    except OSError:
        pass
        
    current_delay = delay
    for attempt in range(1, max_retries + 1):
        try:
            if dest.exists():
                try:
                    dest.unlink()
                except OSError:
                    pass
            
            if same_fs:
                log.info("Same-device move (attempt %d/%d): %s -> %s", attempt, max_retries, src, dest)
                os.rename(src, dest)
            else:
                log.info("Cross-device move (attempt %d/%d): %s -> %s", attempt, max_retries, src, dest)
                shutil.move(str(src), str(dest))
                
            if dest.exists() and dest.stat().st_size == src_size:
                log.info("Move successful & verified: %s", dest.name)
                return True
            else:
                raise OSError("Post-move verification failed (destination missing or size mismatch)")
        except Exception as e:
            log.warning("Move attempt %d failed: %s", attempt, e)
            if attempt < max_retries:
                log.info("Waiting %.1f seconds before retry...", current_delay)
                time.sleep(current_delay)
                current_delay *= 2.0
            else:
                log.error("All move attempts failed for %s", src)
                return False


async def read_until_cr_or_lf(stream: asyncio.StreamReader) -> bytes:
    buf = bytearray()
    while True:
        try:
            b = await stream.read(1)
            if not b:
                break
            buf.extend(b)
            if b == b"\r" or b == b"\n":
                break
        except Exception:
            break
    return bytes(buf)


async def robust_rsync_to_hdd(src: Path, dest_dir: Path, max_retries: int = 3, dash_key: Optional[str] = None) -> bool:
    """Rsync with retry for aging HDDs. Verifies file exists at destination after transfer."""
    if not src.exists():
        log.error("rsync source missing: %s", src)
        return False
        
    try:
        src_size = src.stat().st_size
    except OSError as e:
        log.error("Failed to stat source file %s: %s", src, e)
        return False
        
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.error("Cannot create dest dir %s: %s", dest_dir, e)
        return False
        
    dest_file = dest_dir / src.name
    
    cmd = [
        "rsync",
        "-ah",
        "--info=progress2",
        str(src),
        str(dest_dir) + "/",
    ]
    
    dash = Dashboard.get_instance()
    delay = 5.0
    for attempt in range(1, max_retries + 1):
        log.info("rsync (attempt %d/%d): %s -> %s/", attempt, max_retries, src.name, dest_dir)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            while True:
                line_bytes = await read_until_cr_or_lf(proc.stdout)
                if not line_bytes:
                    break
                line = line_bytes.decode("utf-8", errors="ignore")
                match = re.search(r"(\d+)%", line)
                if match and dash_key:
                    pct = int(match.group(1)) / 100.0
                    dash.update(dash_key, TaskStatus.RSYNC, pct, f"transferring {match.group(1)}%")
            
            await proc.wait()
            if proc.returncode == 0:
                if dest_file.exists() and dest_file.stat().st_size == src_size:
                    log.info("rsync successful & verified. Removing source file: %s", src)
                    src.unlink(missing_ok=True)
                    return True
                else:
                    log.warning("rsync finished with rc=0 but verification failed (size mismatch or file missing)")
            else:
                stderr_data = await proc.stderr.read()
                log.warning("rsync failed (rc=%d): %s", proc.returncode, stderr_data.decode("utf-8", errors="ignore")[-500:])
        except Exception as e:
            log.warning("rsync attempt %d exception: %s", attempt, e)
            
        if attempt < max_retries:
            log.info("Waiting %.1f seconds before rsync retry...", delay)
            await asyncio.sleep(delay)
            delay *= 2.0
            
    log.error("All rsync attempts failed for %s", src)
    return False


# =============================================================================
# Media validation (ffprobe) & External remux.sh Integration
# =============================================================================

@dataclass
class MediaInfo:
    video_codec: str
    fps: float
    width: int
    height: int
    audio_codecs: list[str]
    duration: float


def _ffprobe_stream(file: Path, stream: str, fields: list[str]) -> dict:
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", stream,
        "-show_entries", "stream=" + ",".join(fields),
        "-of", "json",
        str(file),
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=True)
        streams = json.loads(out.stdout).get("streams", [])
        return streams[0] if streams else {}
    except Exception as e:
        log.warning("ffprobe failed (%s stream=%s): %s", file.name, stream, e)
        return {}


def _probe_audio_codecs(file: Path) -> list[str]:
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "a",
        "-show_entries", "stream=codec_name",
        "-of", "json",
        str(file),
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=True)
        streams = json.loads(out.stdout).get("streams", [])
        return [s.get("codec_name", "unknown").lower() for s in streams]
    except Exception:
        return []


def _probe_duration(file: Path) -> float:
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(file),
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=True)
        return float(out.stdout.strip())
    except Exception:
        return 0.0


def validate_media(file: Path) -> MediaInfo:
    video = _ffprobe_stream(file, "v:0", ["codec_name", "width", "height", "r_frame_rate"])
    codec = video.get("codec_name", "unknown")
    fps_str = video.get("r_frame_rate", "0") or "0"
    if "/" in fps_str:
        try:
            num, den = fps_str.split("/", 1)
            d = float(den)
            fps = float(num) / d if d else 0.0
        except ValueError:
            fps = 0.0
    else:
        try:
            fps = float(fps_str)
        except ValueError:
            fps = 0.0
    try:
        width = int(video.get("width", 0))
        height = int(video.get("height", 0))
    except (TypeError, ValueError):
        width = height = 0
    audio_codecs = _probe_audio_codecs(file)
    duration = _probe_duration(file)
    return MediaInfo(codec, fps, width, height, audio_codecs, duration)


_REMUX_LOCK = asyncio.Lock()


async def run_remux_sh(
    video_path: Path, cfg: Config, task_key: Optional[str] = None
) -> Optional[Path]:
    """Delegate remuxing/transcoding to the external remux.sh script.
    Serialized via _REMUX_LOCK to ensure remux.sh is never triggered in parallel.
    """
    remux_script = cfg.remux_script
    if not remux_script.exists():
        log.error("remux.sh script not found at %s", remux_script)
        return None

    cmd = [str(remux_script), "-w"]
    if cfg.best_quality:
        cmd.append("-q")
    cmd.append(str(video_path))

    dash = Dashboard.get_instance()
    if task_key:
        dash.update(task_key, TaskStatus.REMUXING, 0.0, "waiting for remux lock")

    async with _REMUX_LOCK:
        if task_key:
            dash.update(task_key, TaskStatus.REMUXING, 0.0, "remuxing (remux.sh)")

        log.info("Running remux.sh: %s", " ".join(cmd))
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            async def _read_stdout():
                while proc.stdout:
                    line_bytes = await read_until_cr_or_lf(proc.stdout)
                    if not line_bytes:
                        break
                    line = line_bytes.decode("utf-8", errors="ignore").strip()
                    if line:
                        log.info("[remux.sh] %s", line)

            async def _read_stderr():
                while proc.stderr:
                    line_bytes = await read_until_cr_or_lf(proc.stderr)
                    if not line_bytes:
                        break
                    line = line_bytes.decode("utf-8", errors="ignore").strip()
                    if line:
                        log.warning("[remux.sh] %s", line)

            await asyncio.gather(_read_stdout(), _read_stderr())
            rc = await proc.wait()

            if rc == 0:
                # remux.sh with -w produces <name>.mkv or overwrites video_path
                expected_mkv = video_path.with_suffix(".mkv")
                if expected_mkv.exists():
                    return expected_mkv
                elif video_path.exists():
                    return video_path
                else:
                    log.error("remux.sh completed with rc=0 but output file missing")
                    return None
            else:
                log.error("remux.sh failed with exit code %d", rc)
                return None
        except Exception as e:
            log.error("Failed to execute remux.sh: %s", e)
            return None


# =============================================================================
# YouTube download & processing
# =============================================================================

YTDLP_FORMAT = "bestvideo[height<=1080]+bestaudio/best[height<=1080]"


async def run_ytdlp(args: list[str], task_key: Optional[str] = None) -> tuple[int, str, str]:
    if "--js-runtimes" not in args:
        if shutil.which("node"):
            args = ["--js-runtimes", "node"] + args
        elif shutil.which("deno"):
            args = ["--js-runtimes", "deno"] + args

    cmd = ["yt-dlp"] + args
    log.info("Running: %s", " ".join(cmd))
    
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        
        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        dash = Dashboard.get_instance()
        pct_regex = re.compile(r"\[download\]\s+(\d+(?:\.\d+)%)")
        
        async def read_stream(stream: asyncio.StreamReader, lines_list: list[str]) -> None:
            while True:
                line_bytes = await stream.readline()
                if not line_bytes:
                    break
                line = line_bytes.decode("utf-8", errors="replace")
                lines_list.append(line)
                
                if task_key:
                    m = pct_regex.search(line)
                    if m:
                        try:
                            pct = float(m.group(1))
                            dash.update(task_key, TaskStatus.DOWNLOADING, pct / 100.0, f"downloading ({pct:.1f}%)")
                        except ValueError:
                            pass

        await asyncio.gather(
            read_stream(proc.stdout, stdout_lines),
            read_stream(proc.stderr, stderr_lines),
        )
        
        rc = await proc.wait()
        return rc, "".join(stdout_lines), "".join(stderr_lines)
    except Exception as e:
        log.error("yt-dlp execution failed: %s", e)
        return -1, "", str(e)


async def download_youtube(url: str, cfg: Config, task_key: Optional[str] = None) -> Optional[Path]:
    yt_dir = cfg.ssd_buffer / "yt-dlp"
    yt_dir.mkdir(parents=True, exist_ok=True)
    
    yt_sort = "res" if cfg.best_quality else "res,fps:30,vcodec:h264,acodec:aac"
    args = [
        "-f", YTDLP_FORMAT,
        "-S", yt_sort,
        "--paths", str(yt_dir),
        "--print", "after_move:filepath",
        url
    ]
    rc, stdout, stderr = await run_ytdlp(args, task_key=task_key)
    
    if rc != 0:
        log.warning("yt-dlp download failed (rc=%d): %s", rc, stderr)
        log.warning("yt-dlp automatic update skipped to prevent hangs (please update manually via pip/wheel if needed)")
        return None
            
    downloaded_file = None
    for line in reversed(stdout.strip().splitlines()):
        line_clean = line.strip()
        if line_clean and (line_clean.startswith("/") or Path(line_clean).exists()):
            p = Path(line_clean)
            if p.exists():
                downloaded_file = p
                break
                
    if not downloaded_file:
        log.error("yt-dlp completed but no valid file path found in output: %s", stdout.strip()[:200])
        return None
        
    sanitized_name = sanitize_youtube_filename(downloaded_file.name)
    sanitized_file = downloaded_file.with_name(sanitized_name)
    
    if downloaded_file != sanitized_file:
        try:
            log.info("Renaming %s to %s", downloaded_file.name, sanitized_name)
            downloaded_file.rename(sanitized_file)
            downloaded_file = sanitized_file
        except OSError as e:
            log.error("Failed to rename downloaded file: %s", e)
            return None
            
    return downloaded_file


async def process_youtube_url(
    url: str, index: int, total: int, cfg: Config, hdd_queue: asyncio.Queue
) -> bool:
    """Full YouTube pipeline: download → validate → remux.sh → stage → classify → rsync."""
    dash = Dashboard.get_instance()
    task_key = f"youtube_{index}"
    dash.register(task_key, f"YouTube {index}/{total}")
    log.info("=" * 70)
    log.info("Processing YouTube link %d/%d: %s", index, total, url)
    log.info("=" * 70)
    
    dash.update(task_key, TaskStatus.DOWNLOADING, 0.0, "downloading")
    downloaded = await download_youtube(url, cfg, task_key)
    if not downloaded:
        log.error("Failed to download YouTube video: %s", url)
        dash.update(task_key, TaskStatus.FAILED, 0.0, "download failed")
        return False
    
    dash.update(task_key, TaskStatus.DONE, 0.4, "downloaded")
    log.info("Downloaded YouTube video: %s", downloaded)
    
    dash.update(task_key, TaskStatus.PROCESSING, 0.5, "validating")
    media_info = validate_media(downloaded)
    log.info("Codec=%s fps=%.2f res=%dx%d audio=%s duration=%.1fs",
             media_info.video_codec, media_info.fps,
             media_info.width, media_info.height,
             media_info.audio_codecs, media_info.duration)
    
    dash.update(task_key, TaskStatus.REMUXING, 0.6, "remuxing")
    remuxed = await run_remux_sh(downloaded, cfg, task_key)
    if not remuxed:
        log.error("remux.sh failed for YouTube video: %s", downloaded.name)
        downloaded.unlink(missing_ok=True)
        dash.update(task_key, TaskStatus.FAILED, 0.0, "remux failed")
        return False

    dash.update(task_key, TaskStatus.STAGING, 0.8, "staging")
    staging_dir = cfg.ssd_buffer / "ready_for_hdd"
    try:
        staged = stage_output(remuxed, staging_dir)
    except OSError as e:
        log.error("Failed to stage YouTube output: %s", e)
        remuxed.unlink(missing_ok=True)
        dash.update(task_key, TaskStatus.FAILED, 0.0, "staging failed")
        return False
        
    hdd_available = is_hdd_mounted(cfg)
    duration = media_info.duration
    title = staged.stem
    folder_name = title.replace("_TV_Compatible", "").replace(".remux", "")
    
    if duration > 3600:  # > 60 min
        category = "FILMES"
        if hdd_available:
            dest_dir = cfg.hdd_mount / category / folder_name
        else:
            dest_dir = cfg.ssd_buffer / "processed" / category / folder_name
    else:
        category = "OUTROS"
        if hdd_available:
            dest_dir = cfg.hdd_mount / category / "YouTube" / folder_name
        else:
            dest_dir = cfg.ssd_buffer / "processed" / category / "YouTube" / folder_name
            
    if hdd_available:
        log.info("Enqueuing YouTube video for rsync: %s -> %s/", staged.name, dest_dir)
        dash.update(task_key, TaskStatus.RSYNC, 0.9, f"queued: {staged.name[:20]}")
        await hdd_queue.put((staged, dest_dir, task_key))
    else:
        log.warning("HDD unavailable, moving YouTube video to SSD fallback: %s", dest_dir)
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(staged), str(dest_dir / staged.name))
        except OSError as e:
            log.error("Fallback move failed: %s", e)
            dash.update(task_key, TaskStatus.FAILED, 0.0, "fallback failed")
            return False
        dash.update(task_key, TaskStatus.DONE, 1.0, "SSD fallback")
            
    return True


# =============================================================================
# HDD Scanner & Duplicate Detector
# =============================================================================

@dataclass
class HDDIndex:
    existing_episodes: set[tuple[str, int, int]]
    existing_yt_ids: set[str]


def build_hdd_index(hdd_mount: Path) -> HDDIndex:
    episodes: set[tuple[str, int, int]] = set()
    yt_ids: set[str] = set()
    video_exts = {".mkv", ".mp4", ".avi", ".m4v", ".webm", ".mov", ".ts", ".wmv"}
    categories = ["ANIMES", "FILMES", "OUTROS", "SERIES"]
    
    for cat in categories:
        cat_path = hdd_mount / cat
        if not cat_path.exists():
            continue
        for root, dirs, files in os.walk(cat_path):
            dirs[:] = [d for d in dirs if d not in ("sync_pipeline", "output", "temp", "__pycache__")]
            for f in files:
                if Path(f).suffix.lower() not in video_exts:
                    continue
                show_name = derive_show_name(f)
                canon = canonicalize_name(show_name)
                season, episode = parse_season_episode(f)
                if canon and canon != "unknownshow" and episode >= 0:
                    episodes.add((canon, season, episode))
                m = re.search(r"\[([a-zA-Z0-9_\-]{11})\]", f)
                if m:
                    yt_ids.add(m.group(1))
    
    return HDDIndex(existing_episodes=episodes, existing_yt_ids=yt_ids)


def is_episode_on_hdd(torrent_name: str, hdd_index: HDDIndex) -> bool:
    name_lower = torrent_name.lower()
    if (
        "~" in name_lower
        or re.search(r"\d+\s*-\s*\d+", torrent_name)
        or any(x in name_lower for x in ["complete", "season", "temporada", "pack"])
    ):
        return False

    show_name = derive_show_name(torrent_name)
    canon = canonicalize_name(show_name)
    season, episode = parse_season_episode(torrent_name)
    if canon and canon != "unknownshow" and episode >= 0:
        return (canon, season, episode) in hdd_index.existing_episodes
    return False


def is_youtube_on_hdd(yt_id: str, hdd_index: HDDIndex) -> bool:
    return yt_id in hdd_index.existing_yt_ids

@dataclass
class ScannedVideo:
    path: Path
    filename: str
    show_name: str
    canonical_name: str
    season: int
    episode: int
    category: str
    parent_folder: str


def scan_hdd_videos(hdd_mount: Path) -> list[ScannedVideo]:
    video_exts = {".mkv", ".mp4", ".avi", ".m4v", ".webm", ".mov", ".ts", ".wmv"}
    categories = ["ANIMES", "FILMES", "OUTROS", "SERIES"]
    scanned: list[ScannedVideo] = []
    
    for cat in categories:
        cat_path = hdd_mount / cat
        if not cat_path.exists():
            continue
        
        for root, dirs, files in os.walk(cat_path):
            dirs[:] = [d for d in dirs if d not in ("sync_pipeline", "output", "temp", "__pycache__")]
            
            for f in files:
                filepath = Path(root) / f
                if filepath.suffix.lower() not in video_exts:
                    continue
                
                try:
                    rel_parts = filepath.relative_to(cat_path).parts
                except ValueError:
                    continue
                
                if not rel_parts:
                    continue
                
                parent_folder = rel_parts[0]
                if len(rel_parts) == 1:
                    parent_folder = "Unknown_Show"
                
                show_name = derive_show_name(f)
                canonical_name = canonicalize_name(show_name)
                season, episode = parse_season_episode(f)
                
                scanned.append(ScannedVideo(
                      path=filepath,
                      filename=f,
                      show_name=show_name,
                      canonical_name=canonical_name,
                      season=season,
                      episode=episode,
                      category=cat,
                      parent_folder=parent_folder
                ))
    return scanned


@dataclass
class DuplicateGroup:
    canonical_name: str
    folders: list[str]
    canonical_folder: str
    videos: list[ScannedVideo]


def detect_duplicates(scanned: list[ScannedVideo]) -> list[DuplicateGroup]:
    unknown_by_cat = defaultdict(list)
    for v in scanned:
        if v.parent_folder == "Unknown_Show":
            unknown_by_cat[v.category].append(v)
            
    for cat, v_list in unknown_by_cat.items():
        canon_names = {v.canonical_name for v in v_list if v.canonical_name != "unknownshow"}
        if len(canon_names) != 1:
            for v in v_list:
                v.canonical_name = "unknownshow"
                
    groups = defaultdict(list)
    for v in scanned:
        groups[(v.category, v.canonical_name)].append(v)
        
    duplicate_groups: list[DuplicateGroup] = []
    for (category, canon_name), v_list in groups.items():
        if canon_name == "unknownshow":
            continue
            
        folders = sorted(list({v.parent_folder for v in v_list}))
        if len(folders) > 1:
            folder_file_counts = defaultdict(int)
            for v in v_list:
                folder_file_counts[v.parent_folder] += 1
                
            def folder_key(folder_name: str) -> tuple[int, int, int, int]:
                is_unknown = 1 if folder_name == "Unknown_Show" else 0
                spaces = folder_name.count(" ")
                file_count = folder_file_counts[folder_name]
                uppers = sum(1 for c in folder_name if c.isupper())
                return (-is_unknown, spaces, file_count, uppers)
                
            sorted_folders = sorted(folders, key=folder_key, reverse=True)
            canonical_folder = sorted_folders[0]
            
            duplicate_groups.append(DuplicateGroup(
                canonical_name=canon_name,
                folders=folders,
                canonical_folder=canonical_folder,
                videos=v_list
            ))
    return duplicate_groups


# =============================================================================
# HDD Organizer & Report Generator
# =============================================================================

def _format_episode_list(eps: list[int]) -> str:
    if not eps:
        return ""
    ranges = []
    start = eps[0]
    prev = eps[0]
    for e in eps[1:]:
        if e == prev + 1:
            prev = e
        else:
            if start == prev:
                ranges.append(f"{start:02d}")
            else:
                ranges.append(f"{start:02d}-{prev:02d}")
            start = e
            prev = e
    if start == prev:
        ranges.append(f"{start:02d}")
    else:
        ranges.append(f"{start:02d}-{prev:02d}")
    return ",".join(ranges)


def generate_report(
    duplicates: list[DuplicateGroup],
    planned_moves: list[tuple[Path, Path]],
    scanned: list[ScannedVideo],
    hdd_mount: Path
) -> str:
    lines = []
    lines.append("=== HDD Organization Report ===")
    lines.append("DUPLICATE SHOW FOLDERS:")
    if duplicates:
        for dg in duplicates:
            others = [f"{dg.videos[0].category}/{f}" for f in dg.folders if f != dg.canonical_folder]
            others_str = " + ".join(others)
            lines.append(f"  {dg.videos[0].category}/{dg.canonical_folder} + {others_str} → merge into \"{dg.canonical_folder}\"")
            
            for f in dg.folders:
                f_videos = [v for v in dg.videos if v.parent_folder == f]
                se_list = []
                season_eps = defaultdict(list)
                for v in f_videos:
                    if v.episode > 0:
                        season_eps[v.season].append(v.episode)
                
                for s in sorted(season_eps.keys()):
                    eps = sorted(season_eps[s])
                    se_list.append(f"S{s:02d}E{_format_episode_list(eps)}")
                
                se_str = ", ".join(se_list)
                unit = "episodes" if dg.videos[0].category == "SERIES" else "files"
                lines.append(f"    {f}: {len(f_videos)} {unit} ({se_str})")
    else:
        lines.append("  None")
        
    lines.append("MISPLACED EPISODES:")
    misplaced = []
    for src, dest in planned_moves:
        if "Unknown_Show" in src.parts or len(src.relative_to(hdd_mount).parts) <= 3:
            rel = src.relative_to(hdd_mount)
            category = rel.parts[0]
            dest_rel = dest.parent.relative_to(hdd_mount)
            misplaced.append(f"  {category}/{rel.parts[1]}/{src.name} → {category}/{'/'.join(dest_rel.parts[1:])}/")
            
    if misplaced:
        lines.extend(misplaced)
    else:
        lines.append("  None")
        
    lines.append("SEASON REORGANIZATION:")
    show_moves = defaultdict(list)
    for src, dest in planned_moves:
        rel = dest.relative_to(hdd_mount)
        category = rel.parts[0]
        show_folder = rel.parts[1]
        show_moves[(category, show_folder)].append((src, dest))
        
    if show_moves:
        for (category, show_folder), moves in sorted(show_moves.items()):
            show_dir = hdd_mount / category / show_folder
            total_videos = 0
            seasons_set = set()
            for v in scanned:
                is_this_show = False
                if v.category == category:
                    if v.parent_folder == show_folder:
                        is_this_show = True
                    else:
                        for dg in duplicates:
                            if dg.videos[0].category == category and dg.canonical_folder == show_folder and v.parent_folder in dg.folders:
                                is_this_show = True
                                break
                if is_this_show:
                    total_videos += 1
                    seasons_set.add(f"Season {v.season:02d}")
                    
            if category == "FILMES":
                seasons_str = "no seasons"
            else:
                seasons_str = ", ".join(sorted(list(seasons_set)))
            unit = "episodes" if category == "SERIES" else "files"
            
            preserved_items = []
            for name in ["sync_pipeline", "output", "README.md", "temp", "requirements.txt"]:
                if (show_dir / name).exists():
                    preserved_items.append(name + ("/" if (show_dir / name).is_dir() else ""))
            preserve_str = f" (+ preserve {', '.join(preserved_items)})" if preserved_items else ""
            
            lines.append(f"  {category}/{show_folder}: {total_videos} {unit} → {seasons_str}{preserve_str}")
    else:
        lines.append("  None")
        
    lines.append("ACTIONS:")
    action_counter = 1
    
    if duplicates:
        for dg in duplicates:
            others = [f for f in dg.folders if f != dg.canonical_folder]
            target_suffix = "" if dg.videos[0].category == "FILMES" else "/Season XX"
            for other in others:
                lines.append(f"  {action_counter}. MOVE {dg.videos[0].category}/{other}/* → {dg.videos[0].category}/{dg.canonical_folder}{target_suffix}/")
                action_counter += 1
                
    for src, dest in planned_moves:
        is_misplaced = "Unknown_Show" in src.parts
        if not is_misplaced and "FILMES" not in src.parts and len(src.relative_to(hdd_mount).parts) <= 3:
            is_misplaced = True
        if not is_misplaced and "FILMES" in src.parts and "Season " in src.parts:
            is_misplaced = True
            
        if is_misplaced:
            rel_src = src.relative_to(hdd_mount)
            rel_dest = dest.relative_to(hdd_mount)
            dest_dir_display = "/".join(rel_dest.parent.parts)
            lines.append(f"  {action_counter}. MOVE {rel_src.parts[0]}/{rel_src.parts[1]}/{src.name} → {dest_dir_display}/")
            action_counter += 1
            
    has_tv_reorg = any("FILMES" not in dest.parts for _, dest in planned_moves)
    if has_tv_reorg:
        lines.append(f"  {action_counter}. CREATE Season XX/ dirs in affected show folders")
        action_counter += 1
        lines.append(f"  {action_counter}. MOVE files into Season XX/ subdirs")
        action_counter += 1
    
    if duplicates:
        empty_dirs = []
        for dg in duplicates:
            others = [f for f in dg.folders if f != dg.canonical_folder]
            for other in others:
                empty_dirs.append(f"{dg.videos[0].category}/{other}/")
        empty_dirs_str = ", ".join(empty_dirs)
        lines.append(f"  {action_counter}. REMOVE empty folders ({empty_dirs_str})")
    else:
        has_unknown_show = any("Unknown_Show" in src.parts for src, _ in planned_moves)
        if has_unknown_show:
            lines.append(f"  {action_counter}. REMOVE empty folders (Unknown_Show/)")
        else:
            lines.append(f"  {action_counter}. REMOVE empty folders")
            
    return "\n".join(lines)


def organize_hdd(cfg: Config, dry_run: bool) -> bool:
    log.info("Starting HDD organization scan...")
    if not is_hdd_mounted(cfg):
        log.error("HDD not mounted at %s. Cannot organize.", cfg.hdd_mount)
        return False
        
    scanned = scan_hdd_videos(cfg.hdd_mount)
    log.info("Scanned %d video files on HDD.", len(scanned))
    
    duplicates = detect_duplicates(scanned)
    
    planned_moves: list[tuple[Path, Path]] = []
    create_dirs: set[Path] = set()
    empty_dirs_to_check: set[Path] = set()
    
    canon_folder_map = {}
    for dg in duplicates:
        for folder in dg.folders:
            canon_folder_map[(dg.videos[0].category, folder)] = dg.canonical_folder
            
    for v in scanned:
        category = v.category
        show_folder = canon_folder_map.get((category, v.parent_folder), v.parent_folder)
        
        if show_folder == "Unknown_Show" and v.canonical_name == "unknownshow":
            continue
        
        whitelist = cfg.organize_whitelist.get(category, None)
        if whitelist is not None and (not whitelist or show_folder in whitelist or v.parent_folder in whitelist):
            continue
            
        target_show_dir = cfg.hdd_mount / category / show_folder
        if category == "FILMES":
            dest_path = target_show_dir / v.filename
        else:
            target_season_dir = target_show_dir / f"Season {v.season:02d}"
            dest_path = target_season_dir / v.filename
        
        if v.path != dest_path:
            planned_moves.append((v.path, dest_path))
            if category != "FILMES":
                create_dirs.add(target_season_dir)
            empty_dirs_to_check.add(v.path.parent)
            
    report = generate_report(duplicates, planned_moves, scanned, cfg.hdd_mount)
    print(report)
    log.info("HDD Organization Report generated.")
    
    if dry_run:
        log.info("Dry run enabled. No changes were made.")
        return True
        
    if not planned_moves:
        log.info("No organization moves needed.")
        return True
        
    log.info("Executing HDD organization moves...")
    
    for d in sorted(list(create_dirs)):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log.error("Failed to create directory %s: %s", d, e)
            return False
            
    success_count = 0
    fail_count = 0
    for src, dest in planned_moves:
        if dest.exists():
            log.warning("Destination file already exists, skipping to prevent overwrite: %s", dest)
            fail_count += 1
            continue
            
        ok = robust_move(src, dest)
        if ok:
            success_count += 1
        else:
            fail_count += 1
            
    log.info("Moves completed: %d succeeded, %d failed", success_count, fail_count)
    
    for d in sorted(list(empty_dirs_to_check), key=lambda x: len(x.parts), reverse=True):
        if d.exists() and d.is_dir():
            try:
                rel_parts = d.relative_to(cfg.hdd_mount).parts
                if len(rel_parts) >= 2:
                    if not any(d.iterdir()):
                        log.info("Removing empty directory: %s", d)
                        d.rmdir()
            except (OSError, ValueError) as e:
                log.warning("Could not remove directory %s: %s", d, e)
                
    return fail_count == 0


# =============================================================================
# Per-magnet pipeline
# =============================================================================

async def wait_for_completion(
    qbt: QBittorrentClient,
    infohash: str,
    cfg: Config,
    stop_event: Optional[asyncio.Event] = None,
    task_key: Optional[str] = None,
) -> bool:
    elapsed = 0
    dash = Dashboard.get_instance()
    log.info("Waiting for %s...", infohash[:12])
    while elapsed < cfg.poll_timeout:
        if stop_event and stop_event.is_set():
            log.warning("Stop requested while waiting for download")
            return False

        ssd_free = get_free_space(cfg.ssd_buffer)
        threshold = cfg.safety_threshold_gb * (1024 ** 3)
        if ssd_free < threshold:
            log.error("SSD free space %d MB below safety threshold (%d GB) during download! Pausing torrent.",
                      ssd_free // (1024 * 1024), cfg.safety_threshold_gb)
            try:
                await qbt.pause_torrent(infohash)
            except Exception as e:
                log.error("Failed to pause torrent: %s", e)
            if task_key:
                dash.update(task_key, TaskStatus.FAILED, 0.0, "low SSD space")
            return False

        info = await qbt.torrent_info(infohash)
        if info is None:
            log.error("Torrent missing or qBittorrent error")
            if task_key:
                dash.update(task_key, TaskStatus.FAILED, 0.0, "torrent missing")
            return False
        state = info.get("state", "unknown")
        progress = info.get("progress", 0) * 100

        if task_key:
            dash.update(task_key, TaskStatus.DOWNLOADING, progress / 100.0, state)

        if state in ("uploading", "stalledUP", "forcedUP", "queuedUP", "completed"):
            log.info("Download complete (state=%s, %.1f%%)", state, progress)
            if task_key:
                dash.update(task_key, TaskStatus.DONE, 1.0, state)
            return True
        if state == "missingFiles":
            log.warning("Missing files, requesting recheck")
            await qbt.recheck(infohash)
        elif state == "error":
            log.error("Torrent in error state")
            if task_key:
                dash.update(task_key, TaskStatus.FAILED, 0.0, "error state")
            return False
        elif state == "stalledDL" and elapsed > 600:
            log.warning("Stalled for >10min, still waiting")

        for _ in range(cfg.poll_interval):
            if stop_event and stop_event.is_set():
                log.warning("Stop requested while waiting for download")
                return False
            await asyncio.sleep(1)
        elapsed += cfg.poll_interval
    log.error("Timed out after %ds", cfg.poll_timeout)
    if task_key:
        dash.update(task_key, TaskStatus.FAILED, 0.0, "timeout")
    return False


def find_largest_video(content_path: Path) -> Optional[Path]:
    video_exts = {".mkv", ".mp4", ".avi", ".m4v", ".webm", ".mov", ".ts", ".wmv"}
    candidates: list[tuple[int, Path]] = []
    for root, dirs, files in os.walk(content_path):
        rel = Path(root).relative_to(content_path).parts
        if len(rel) >= 2:
            dirs.clear()
            continue
        for f in files:
            p = Path(root) / f
            if p.suffix.lower() in video_exts:
                try:
                    candidates.append((p.stat().st_size, p))
                except OSError:
                    pass
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def stage_output(src: Path, staging_dir: Path) -> Path:
    """Move the output file out of content_path to a stable staging dir."""
    staging_dir.mkdir(parents=True, exist_ok=True)
    candidate = staging_dir / src.name
    counter = 0
    while candidate.exists():
        counter += 1
        candidate = staging_dir / f"{src.stem}_{counter}{src.suffix}"
    shutil.move(str(src), str(candidate))
    return candidate


async def process_downloaded_file(
    downloaded: Path,
    file_task_key: str,
    cfg: Config,
    hdd_available: bool,
    hdd_queue: asyncio.Queue,
) -> bool:
    dash = Dashboard.get_instance()
    dash.update(file_task_key, TaskStatus.PROCESSING, 0.0, "validating")
    log.info("Validating media: %s", downloaded.name)
    try:
        media_info = validate_media(downloaded)
        log.info("Codec=%s fps=%.2f res=%dx%d audio=%s duration=%.1fs",
                 media_info.video_codec, media_info.fps,
                 media_info.width, media_info.height,
                 media_info.audio_codecs, media_info.duration)
    except Exception as e:
        log.error("Failed to validate media for %s: %s", downloaded.name, e)
        dash.update(file_task_key, TaskStatus.FAILED, 0.0, "validation failed")
        return False

    # --- Remux via remux.sh ---
    dash.update(file_task_key, TaskStatus.REMUXING, 0.0, "remuxing via remux.sh")
    remuxed = await run_remux_sh(downloaded, cfg, file_task_key)
    if not remuxed:
        log.error("remux.sh failed for %s", downloaded.name)
        dash.update(file_task_key, TaskStatus.FAILED, 0.0, "remux failed")
        return False

    # --- Stage for rsync ---
    dash.update(file_task_key, TaskStatus.STAGING, 0.0, "staging")
    staging_dir = cfg.ssd_buffer / "ready_for_hdd"
    try:
        staged = stage_output(remuxed, staging_dir)
    except OSError as e:
        log.error("Failed to stage output for %s: %s", remuxed.name, e)
        dash.update(file_task_key, TaskStatus.FAILED, 0.0, "staging failed")
        return False

    # --- Classify and enqueue ---
    category, show_name = classify_media(staged.name, media_info.duration)
    season, _ = parse_season_episode(staged.name)
    season_dir = f"Season {season:02d}"

    if hdd_available:
        if category == "FILMES":
            dest_dir = cfg.hdd_mount / category / show_name
        else:
            dest_dir = cfg.hdd_mount / category / show_name / season_dir
        log.info("Enqueuing for rsync: %s -> %s/", staged.name, dest_dir)
        dash.update(file_task_key, TaskStatus.RSYNC, 0.0, f"queued: {staged.name[:25]}")
        await hdd_queue.put((staged, dest_dir, file_task_key))
    else:
        if category == "FILMES":
            fallback_dir = cfg.ssd_buffer / "processed" / category / show_name
        else:
            fallback_dir = cfg.ssd_buffer / "processed" / category / show_name / season_dir
        log.warning("HDD unavailable, moving to SSD fallback: %s", fallback_dir)
        try:
            fallback_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(staged), str(fallback_dir / staged.name))
            dash.update(file_task_key, TaskStatus.DONE, 1.0, "moved to SSD fallback")
        except OSError as e:
            log.error("Fallback move failed for %s: %s", staged.name, e)
            dash.update(file_task_key, TaskStatus.FAILED, 0.0, "fallback move failed")
            return False

    return True


async def process_one_magnet(
    qbt: QBittorrentClient,
    magnet: str,
    index: int,
    total: int,
    cfg: Config,
    hdd_queue: asyncio.Queue,
    stop_event: asyncio.Event,
    hdd_index: Optional[HDDIndex] = None,
) -> bool:
    if stop_event.is_set():
        return False
    log.info("=" * 70)
    log.info("Processing magnet %d/%d", index, total)
    log.info("Magnet: %s...", magnet[:80])
    log.info("=" * 70)

    infohash = get_infohash(magnet)
    if not infohash:
        log.error("Could not extract infohash from magnet")
        return False

    hdd_available = is_hdd_mounted(cfg)
    if hdd_available:
        log.info("HDD detected at %s", cfg.hdd_mount)
    else:
        log.warning("HDD NOT mounted at %s", cfg.hdd_mount)

    already_in_qbt = False
    try:
        torrents = await qbt.list_torrents()
        for t in torrents:
            if t.get("hash", "").lower() == infohash.lower():
                already_in_qbt = True
                break
    except Exception as e:
        log.warning("Could not check if torrent is already in qBittorrent: %s", e)

    ssd_free = get_free_space(cfg.ssd_buffer)
    threshold = cfg.safety_threshold_gb * (1024 ** 3)
    if ssd_free < threshold:
        if not already_in_qbt:
            log.error("SSD free space %d MB below safety threshold (%d GB). Aborting download.",
                      ssd_free // (1024 * 1024), cfg.safety_threshold_gb)
            return False
        else:
            log.warning("SSD free space %d MB below safety threshold (%d GB), but torrent is already in qBittorrent. Pausing download to process completed files first.",
                        ssd_free // (1024 * 1024), cfg.safety_threshold_gb)
            try:
                await qbt.pause_torrent(infohash)
            except Exception as e:
                log.error("Failed to pause torrent: %s", e)

    try:
        status = await qbt.add_magnet(magnet, str(cfg.ssd_buffer))
        if status in (200, 204):
            log.info("Magnet added (save: %s)", cfg.ssd_buffer)
        elif status == 409:
            log.warning("Already in qBittorrent, resuming")
            already_in_qbt = True
            try:
                if ssd_free >= threshold:
                    await qbt.resume_torrent(infohash)
                else:
                    log.info("Low SSD space, keeping torrent paused during processing")
            except Exception as e:
                log.error("Failed to resume torrent: %s", e)
        else:
            log.error("qBittorrent rejected magnet (HTTP %d)", status)
            return False
    except Exception as e:
        log.error("Failed to add magnet: %s", e)
        return False

    dash = Dashboard.get_instance()
    task_key = f"magnet_{infohash[:12]}"
    dash.register(task_key, f"Magnet {index}/{total}")
    dash.update(task_key, TaskStatus.DOWNLOADING, 0.0, "waiting for metadata")
    log.info("Waiting for torrent metadata...")
    content_path = None
    elapsed = 0
    while elapsed < cfg.poll_timeout:
        if stop_event.is_set():
            dash.update(task_key, TaskStatus.SKIPPED, 0.0, "stopped")
            return False
        info = await qbt.torrent_info(infohash)
        if info and info.get("content_path") and info.get("state") != "metaDL":
            content_path = Path(info["content_path"])
            break
        await asyncio.sleep(cfg.poll_interval)
        elapsed += cfg.poll_interval
    else:
        log.error("Timed out waiting for torrent metadata")
        if not already_in_qbt:
            await qbt.delete_torrent(infohash, delete_files=False)
        dash.update(task_key, TaskStatus.FAILED, 0.0, "metadata timeout")
        return False

    files = await qbt.get_torrent_files(infohash)
    video_exts = {".mkv", ".mp4", ".avi", ".m4v", ".webm", ".mov", ".ts", ".wmv"}
    video_files_info = []
    for f in files:
        f_path = Path(f["name"])
        if f_path.suffix.lower() in video_exts:
            video_files_info.append((f["index"], f_path))
    video_files_info.sort(key=lambda x: x[1].name)

    if not video_files_info:
        log.error("No video files found in torrent")
        if not already_in_qbt:
            await qbt.delete_torrent(infohash, delete_files=False)
        dash.update(task_key, TaskStatus.FAILED, 0.0, "no video files")
        return False

    log.info("Torrent metadata resolved. Found %d video file(s) in torrent.", len(video_files_info))

    try:
        await qbt.toggle_sequential_download(infohash)
        await qbt.toggle_first_last_piece_priority(infohash)
        await qbt.recheck(infohash)
        await qbt.set_force_start(infohash, False)
        log.info("Enforced sequential order, force recheck, and download queueing for magnet %s", infohash[:12])
    except Exception as e:
        log.warning("Could not enforce qBittorrent settings for %s: %s", infohash[:12], e)

    info_dict = await qbt.torrent_info(infohash)
    actual_save_path = Path(info_dict["save_path"]) if info_dict and info_dict.get("save_path") else cfg.ssd_buffer

    processed_files = set()
    if hdd_index:
        log.info("Checking for files already existing on HDD...")
        for f_id, f_rel_path in video_files_info:
            if is_episode_on_hdd(f_rel_path.name, hdd_index):
                log.info("Episode already on HDD: %s. Setting priority to 0 and skipping.", f_rel_path.name)
                try:
                    await qbt.set_file_priority(infohash, f_id, 0)
                except Exception as e:
                    log.warning("Failed to set priority 0 for existing file %s: %s", f_rel_path.name, e)
                downloaded = actual_save_path / f_rel_path
                try:
                    if downloaded.exists():
                        downloaded.unlink(missing_ok=True)
                except Exception as e:
                    log.warning("Failed to delete existing file %s from SSD: %s", downloaded.name, e)
                processed_files.add(f_id)

    success_count = 0
    failed_count = 0
    any_failed = False

    elapsed = 0
    while len(processed_files) < len(video_files_info) and elapsed < cfg.poll_timeout:
        if stop_event.is_set():
            any_failed = True
            break

        ssd_free = get_free_space(cfg.ssd_buffer)
        threshold = cfg.safety_threshold_gb * (1024 ** 3)
        if ssd_free < threshold:
            log.warning("SSD free space %d MB below safety threshold (%d GB)! Pausing torrent to process completed files first.",
                        ssd_free // (1024 * 1024), cfg.safety_threshold_gb)
            try:
                await qbt.pause_torrent(infohash)
            except Exception as e:
                log.error("Failed to pause torrent: %s", e)
        else:
            try:
                info = await qbt.torrent_info(infohash)
                if info and info.get("state") in ("pausedDL", "paused"):
                    log.info("SSD space recovered (%d MB free). Resuming torrent.", ssd_free // (1024 * 1024))
                    await qbt.resume_torrent(infohash)
            except Exception as e:
                log.error("Failed to resume torrent: %s", e)

        info = await qbt.torrent_info(infohash)
        if info is None:
            log.error("Torrent missing or qBittorrent error")
            dash.update(task_key, TaskStatus.FAILED, 0.0, "torrent missing")
            any_failed = True
            break
        state = info.get("state", "unknown")
        progress = info.get("progress", 0.0)
        dash.update(task_key, TaskStatus.DOWNLOADING, progress, f"{state} ({len(processed_files)}/{len(video_files_info)} done)")

        current_files = await qbt.get_torrent_files(infohash)
        files_by_id = {f["index"]: f for f in current_files}

        for f_id, f_rel_path in video_files_info:
            if f_id in processed_files:
                continue
            f_data = files_by_id.get(f_id)
            if f_data and f_data["progress"] == 1.0:
                log.info("File completed downloading: %s. Processing...", f_rel_path.name)
                downloaded = actual_save_path / f_rel_path

                file_task_key = f"{task_key}_{f_id}"
                dash.register(file_task_key, f"File {f_id+1}: {downloaded.name[:25]}")

                ok = await process_downloaded_file(downloaded, file_task_key, cfg, hdd_available, hdd_queue)
                if ok:
                    success_count += 1
                    try:
                        await qbt.set_file_priority(infohash, f_id, 0)
                    except Exception as e:
                        log.warning("Failed to set priority 0 for file %s: %s", downloaded.name, e)
                else:
                    failed_count += 1
                    any_failed = True

                processed_files.add(f_id)
                dash.unregister(file_task_key)

        for _ in range(cfg.poll_interval):
            if stop_event.is_set():
                break
            await asyncio.sleep(1)
        elapsed += cfg.poll_interval

    if stop_event.is_set():
        log.info("Closing/interrupted — pausing torrent in qBittorrent and preserving downloaded files on disk to save progress")
        try:
            await qbt.pause_torrent(infohash)
        except Exception:
            pass
    elif success_count > 0 or not any_failed:
        log.info("Torrent processing finished — removing torrent entry from qBittorrent (preserving downloaded files on disk)")
        await qbt.delete_torrent(infohash, delete_files=False)

    dash.unregister(task_key)
    return not any_failed


def cleanup_empty_dirs(path: Path) -> None:
    if not path.exists() or not path.is_dir():
        return
    for child in list(path.iterdir()):
        if child.is_dir():
            cleanup_empty_dirs(child)
    try:
        if not any(path.iterdir()):
            path.rmdir()
    except OSError:
        pass


async def resume_staged_and_processed_transfers(cfg: Config, hdd_queue: asyncio.Queue) -> None:
    if not is_hdd_mounted(cfg):
        return

    ready_dir = cfg.ssd_buffer / "ready_for_hdd"
    if ready_dir.exists() and ready_dir.is_dir():
        for f in ready_dir.iterdir():
            if f.is_file() and not f.name.endswith(".!qB"):
                clean_name = f.name.replace("_TV_Compatible", "").replace(".remux", "")
                category, show_name = classify_media(clean_name, duration_seconds=0)
                if category == "FILMES":
                    dest_dir = cfg.hdd_mount / category / show_name
                else:
                    season, _ = parse_season_episode(clean_name)
                    season_dir = f"Season {season:02d}"
                    dest_dir = cfg.hdd_mount / category / show_name / season_dir
                log.info("Resuming staged file transfer: %s -> %s/", f.name, dest_dir)
                await hdd_queue.put((f, dest_dir, None))

    processed_dir = cfg.ssd_buffer / "processed"
    if processed_dir.exists() and processed_dir.is_dir():
        for f in processed_dir.rglob("*"):
            if f.is_file() and not f.name.endswith(".!qB"):
                try:
                    rel = f.relative_to(processed_dir)
                    if len(rel.parts) >= 3:
                        category = rel.parts[0]
                        show_name = rel.parts[1]
                        if category == "FILMES":
                            dest_dir = cfg.hdd_mount / category / show_name
                        elif len(rel.parts) >= 4:
                            season_dir = rel.parts[2]
                            dest_dir = cfg.hdd_mount / category / show_name / season_dir
                        else:
                            continue
                        log.info("Moving SSD fallback file to HDD: %s -> %s/", f.name, dest_dir)
                        await hdd_queue.put((f, dest_dir, None))
                except Exception as e:
                    log.error("Failed to parse relative path for processed file %s: %s", f, e)


async def process_buffer(cfg: Config, organize_after: bool = False, dry_run: bool = False) -> tuple[int, int]:
    """Scan torrent_buffer for unprocessed video files & move to HDD after remuxing via remux.sh."""
    video_exts = {".mkv", ".mp4", ".avi", ".m4v", ".webm", ".mov", ".ts", ".wmv"}
    skip_subdirs = {"yt-dlp", "ready_for_hdd", "processed", "to_convert"}
    
    active_paths: set[Path] = set()
    qbt = QBittorrentClient(cfg.qbt_webui_url, cfg.qbt_user, cfg.qbt_pass)
    try:
        if await qbt.is_responsive():
            await qbt.login()
            torrents = await qbt.list_torrents()
            for t in torrents:
                if t.get("progress", 1.0) < 1.0:
                    content_path = t.get("content_path")
                    if content_path:
                        active_paths.add(Path(content_path).resolve())
                    save_path = t.get("save_path")
                    name = t.get("name")
                    if save_path and name:
                        active_paths.add((Path(save_path) / name).resolve())
            await qbt.logout()
    except Exception as e:
        log.warning("Could not query qBittorrent active downloads: %s", e)

    candidates: list[Path] = []
    for entry in cfg.ssd_buffer.iterdir():
        if entry.name in skip_subdirs:
            continue
        if entry.resolve() in active_paths:
            log.info("Skipping active torrent download: %s", entry.name)
            continue
        if entry.is_file() and entry.suffix.lower() in video_exts:
            if not entry.name.endswith(".!qB") and not any(x in entry.name for x in [".tmp.mkv", ".tmp.mp4"]):
                candidates.append(entry)
        elif entry.is_dir():
            for f in entry.iterdir():
                if f.resolve() in active_paths:
                    continue
                if f.is_file() and f.suffix.lower() in video_exts:
                    if not f.name.endswith(".!qB") and not any(x in f.name for x in [".tmp.mkv", ".tmp.mp4"]):
                        candidates.append(f)
    
    has_resumable = False
    ready_dir = cfg.ssd_buffer / "ready_for_hdd"
    if ready_dir.exists() and any(ready_dir.iterdir()):
        has_resumable = True
    processed_dir = cfg.ssd_buffer / "processed"
    if processed_dir.exists() and any(processed_dir.iterdir()):
        has_resumable = True

    if not candidates and not has_resumable:
        log.info("No unprocessed video files or resumable transfers found in %s", cfg.ssd_buffer)
        return 0, 0
    
    log.info("Found %d unprocessed video file(s) in buffer (and check for resumable transfers)", len(candidates))
    
    hdd_index: Optional[HDDIndex] = None
    if is_hdd_mounted(cfg):
        log.info("Building HDD index to check for existing files...")
        hdd_index = build_hdd_index(cfg.hdd_mount)
        log.info("HDD index: %d episodes, %d YouTube IDs", len(hdd_index.existing_episodes), len(hdd_index.existing_yt_ids))
        
        yt_dir = cfg.ssd_buffer / "yt-dlp"
        if yt_dir.exists() and yt_dir.is_dir():
            for f in yt_dir.iterdir():
                if f.is_file():
                    m = re.search(r"\[([a-zA-Z0-9_\-]{11})\]", f.name)
                    if m and m.group(1) in hdd_index.existing_yt_ids:
                        log.info("Removing orphaned YouTube temp file (already on HDD): %s", f.name)
                        f.unlink(missing_ok=True)
    
    hdd_available = is_hdd_mounted(cfg)
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    
    def _stop() -> None:
        log.warning("Stop signal received, finishing in-flight work...")
        stop_event.set()
    
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            pass
    
    dash = Dashboard.get_instance()
    dash.set_stop_event(stop_event)
    dash.start(stop_event)
    
    hdd_queue: asyncio.Queue = asyncio.Queue()
    rsync_task = asyncio.create_task(rsync_worker(hdd_queue, stop_event))
    await resume_staged_and_processed_transfers(cfg, hdd_queue)
    
    success = 0
    failed = 0
    
    for i, video_path in enumerate(candidates):
        if stop_event.is_set():
            break
        
        task_key = f"buffer_{i+1}"
        dash.register(task_key, f"Buffer {i+1}/{len(candidates)}")
        
        log.info("=" * 70)
        log.info("Processing buffer file %d/%d: %s", i + 1, len(candidates), video_path.name)
        log.info("=" * 70)
        
        filename = video_path.name
        
        if not video_path.exists():
            log.warning("File no longer exists, skipping: %s", video_path)
            dash.update(task_key, TaskStatus.SKIPPED, 0.0, "file missing")
            continue
        
        if hdd_index:
            show_name = derive_show_name(filename)
            canon = canonicalize_name(show_name)
            season, episode = parse_season_episode(filename)
            if canon and canon != "unknownshow" and episode >= 0:
                if (canon, season, episode) in hdd_index.existing_episodes:
                    log.warning("SKIP (already on HDD): %s", filename)
                    dash.update(task_key, TaskStatus.SKIPPED, 0.0, "already on HDD")
                    if video_path.exists():
                        log.info("Removing duplicate file from SSD buffer: %s", video_path)
                        video_path.unlink(missing_ok=True)
                    parent_dir = video_path.parent
                    if parent_dir != cfg.ssd_buffer and parent_dir.is_relative_to(cfg.ssd_buffer):
                        try:
                            remaining = [f for f in parent_dir.iterdir() if f.name not in skip_subdirs]
                            if not any(f.is_file() and f.suffix.lower() in video_exts for f in remaining):
                                log.info("Removing empty parent dir: %s", parent_dir)
                                shutil.rmtree(parent_dir, ignore_errors=True)
                        except (OSError, ValueError):
                            pass
                    continue
            m = re.search(r"\[([a-zA-Z0-9_\-]{11})\]", filename)
            if m and m.group(1) in hdd_index.existing_yt_ids:
                log.warning("SKIP (YouTube ID already on HDD): %s", filename)
                dash.update(task_key, TaskStatus.SKIPPED, 0.0, "already on HDD")
                if video_path.exists():
                    log.info("Removing duplicate YouTube file from SSD buffer: %s", video_path)
                    video_path.unlink(missing_ok=True)
                parent_dir = video_path.parent
                if parent_dir != cfg.ssd_buffer and parent_dir.is_relative_to(cfg.ssd_buffer):
                    try:
                        remaining = [f for f in parent_dir.iterdir() if f.name not in skip_subdirs]
                        if not any(f.is_file() and f.suffix.lower() in video_exts for f in remaining):
                            log.info("Removing empty parent dir: %s", parent_dir)
                            shutil.rmtree(parent_dir, ignore_errors=True)
                    except (OSError, ValueError):
                        pass
                continue
        
        ssd_free = get_free_space(cfg.ssd_buffer)
        threshold = cfg.safety_threshold_gb * (1024 ** 3)
        if ssd_free < threshold:
            log.warning("SSD free space %d MB below safety threshold (%d GB). Skipping %s",
                        ssd_free // (1024 * 1024), cfg.safety_threshold_gb, filename)
            dash.update(task_key, TaskStatus.SKIPPED, 0.0, "low SSD space")
            continue

        dash.update(task_key, TaskStatus.PROCESSING, 0.1, "validating")
        media_info = validate_media(video_path)
        log.info("Codec=%s fps=%.2f res=%dx%d audio=%s duration=%.1fs",
                 media_info.video_codec, media_info.fps,
                 media_info.width, media_info.height,
                 media_info.audio_codecs, media_info.duration)
        
        dash.update(task_key, TaskStatus.REMUXING, 0.3, "remuxing via remux.sh")
        remuxed = await run_remux_sh(video_path, cfg, task_key)
        if not remuxed:
            log.error("remux.sh failed for: %s", video_path)
            dash.update(task_key, TaskStatus.FAILED, 0.0, "remux failed")
            failed += 1
            continue

        dash.update(task_key, TaskStatus.STAGING, 0.6, "staging")
        staging_dir = cfg.ssd_buffer / "ready_for_hdd"
        try:
            staged = stage_output(remuxed, staging_dir)
        except OSError as e:
            log.error("Failed to stage output: %s", e)
            dash.update(task_key, TaskStatus.FAILED, 0.0, "staging failed")
            failed += 1
            continue
        
        category, show_name = classify_media(filename, media_info.duration)
        season, _ = parse_season_episode(filename)
        season_dir = f"Season {season:02d}"
        
        if hdd_available:
            if category == "FILMES":
                dest_dir = cfg.hdd_mount / category / show_name
            else:
                dest_dir = cfg.hdd_mount / category / show_name / season_dir
            log.info("Enqueuing for rsync: %s -> %s/", staged.name, dest_dir)
            dash.update(task_key, TaskStatus.RSYNC, 0.9, f"queued: {staged.name[:20]}")
            await hdd_queue.put((staged, dest_dir, task_key))
        else:
            if category == "FILMES":
                fallback_dir = cfg.ssd_buffer / "processed" / category / show_name
            else:
                fallback_dir = cfg.ssd_buffer / "processed" / category / show_name / season_dir
            log.warning("HDD unavailable, moving to SSD fallback: %s", fallback_dir)
            try:
                fallback_dir.mkdir(parents=True, exist_ok=True)
                shutil.move(str(staged), str(fallback_dir / staged.name))
            except OSError as e:
                log.error("Fallback move failed: %s", e)
                dash.update(task_key, TaskStatus.FAILED, 0.0, "fallback failed")
                failed += 1
                continue
            dash.update(task_key, TaskStatus.DONE, 1.0, "SSD fallback")
        
        parent_dir = video_path.parent
        if parent_dir != cfg.ssd_buffer and parent_dir.is_relative_to(cfg.ssd_buffer):
            try:
                remaining = [f for f in parent_dir.iterdir() if f.name not in skip_subdirs]
                if not any(f.is_file() and f.suffix.lower() in video_exts for f in remaining):
                    shutil.rmtree(parent_dir, ignore_errors=True)
            except (OSError, ValueError):
                pass
        
        success += 1
    
    log.info("All buffer files processed. Draining rsync queue...")
    await hdd_queue.join()
    stop_event.set()
    await rsync_task
    await dash.stop()
    cleanup_empty_dirs(cfg.ssd_buffer / "processed")
    
    log.info("=" * 70)
    log.info("Buffer processing complete: %d succeeded, %d failed", success, failed)
    
    if organize_after and success > 0:
        log.info("=" * 70)
        log.info("Running post-processing HDD organization...")
        log.info("=" * 70)
        organize_hdd(cfg, dry_run)
    
    return success, failed


# =============================================================================
# Rsync worker (single consumer)
# =============================================================================

async def rsync_worker(q: asyncio.Queue, stop_event: asyncio.Event) -> None:
    log.info("rsync worker started")
    dash = Dashboard.get_instance()
    while True:
        if stop_event.is_set() and q.empty():
            break
        try:
            item = await asyncio.wait_for(q.get(), timeout=2.0)
        except asyncio.TimeoutError:
            continue
        try:
            if len(item) == 3:
                src, dest_dir, task_key = item
            else:
                src, dest_dir = item
                task_key = None
            dash_rsync_key = f"rsync_{src.name[:20]}"
            dash.register(dash_rsync_key, f"rsync: {src.name[:28]}")
            dash.update(dash_rsync_key, TaskStatus.RSYNC, 0.0, "transferring")
            ok = await robust_rsync_to_hdd(src, dest_dir, dash_key=dash_rsync_key)
            if ok:
                dash.update(dash_rsync_key, TaskStatus.DONE, 1.0, "complete")
                if task_key:
                    dash.update(task_key, TaskStatus.DONE, 1.0, "transferred")
            else:
                log.error("rsync failed for %s — manual intervention needed", src)
                dash.update(dash_rsync_key, TaskStatus.FAILED, 0.0, "failed")
                if task_key:
                    dash.update(task_key, TaskStatus.FAILED, 0.0, "rsync failed")
        finally:
            q.task_done()
    log.info("rsync worker stopped")


# =============================================================================
# Main orchestrator
# =============================================================================

async def run_pipeline(cfg: Config, input_file: Path | str, organize_after: bool = False, dry_run: bool = False) -> tuple[int, int]:
    is_direct_link = is_link(str(input_file))
    if not is_direct_link:
        path = Path(input_file)
        if not path.exists() or path.is_symlink():
            log.error("Input file missing or is a syslink: %s", input_file)
            return 0, 0

    deps = ["ffprobe", "rsync", "flatpak", "yt-dlp"]
    for cmd in deps:
        if not shutil.which(cmd):
            log.error("Missing dependency: %s", cmd)
            return 0, 0

    if not cfg.remux_script.exists():
        log.error("Missing remux script at %s", cfg.remux_script)
        return 0, 0

    cfg.ssd_buffer.mkdir(parents=True, exist_ok=True)

    links = extract_links(input_file)
    magnets = [link.url for link in links if link.kind == "magnet"]
    youtube_urls = [link.url for link in links if link.kind == "youtube"]

    if not magnets and not youtube_urls:
        log.error("No magnet links or YouTube URLs found in %s", input_file)
        return 0, 0
        
    log.info("Found %d magnet(s) and %d YouTube URL(s) to process", len(magnets), len(youtube_urls))

    hdd_index: Optional[HDDIndex] = None
    if is_hdd_mounted(cfg):
        log.info("Building HDD index to check for existing files...")
        hdd_index = build_hdd_index(cfg.hdd_mount)
        log.info("HDD index: %d episodes, %d YouTube IDs", len(hdd_index.existing_episodes), len(hdd_index.existing_yt_ids))
        
        yt_dir = cfg.ssd_buffer / "yt-dlp"
        if yt_dir.exists() and yt_dir.is_dir():
            for f in yt_dir.iterdir():
                if f.is_file():
                    m = re.search(r"\[([a-zA-Z0-9_\-]{11})\]", f.name)
                    if m and m.group(1) in hdd_index.existing_yt_ids:
                        log.info("Removing orphaned YouTube temp file (already on HDD): %s", f.name)
                        f.unlink(missing_ok=True)
        
        filtered_magnets = []
        for m in magnets:
            torrent_name = parse_torrent_name(m)
            if is_episode_on_hdd(torrent_name, hdd_index):
                log.warning("SKIP (already on HDD): %s", torrent_name)
            else:
                filtered_magnets.append(m)
        skipped_magnets = len(magnets) - len(filtered_magnets)
        if skipped_magnets:
            log.info("Skipped %d magnet(s) already on HDD", skipped_magnets)
        magnets = filtered_magnets
        
        filtered_yt = []
        for url in youtube_urls:
            yt_id = extract_youtube_id(url)
            if yt_id and is_youtube_on_hdd(yt_id, hdd_index):
                log.warning("SKIP (already on HDD): YouTube %s", yt_id)
            else:
                filtered_yt.append(url)
        skipped_yt = len(youtube_urls) - len(filtered_yt)
        if skipped_yt:
            log.info("Skipped %d YouTube URL(s) already on HDD", skipped_yt)
        youtube_urls = filtered_yt

    if not magnets and not youtube_urls:
        log.info("All links already exist on HDD. Nothing to do.")
        return 0, 0

    if youtube_urls:
        log.info("Automatic yt-dlp update check skipped to prevent hangs.")

    qbt = None
    if magnets:
        qbt = QBittorrentClient(cfg.qbt_webui_url, cfg.qbt_user, cfg.qbt_pass)

        if not await qbt.is_responsive():
            log.info("qBittorrent not responding, launching via flatpak...")
            try:
                subprocess.Popen(
                    ["flatpak", "run", "org.qbittorrent.qBittorrent", "--no-splash"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except Exception as e:
                log.error("Failed to launch qBittorrent: %s", e)
                return 0, 0
            for _ in range(30):
                await asyncio.sleep(1)
                if await qbt.is_responsive():
                    break
            else:
                log.error("qBittorrent failed to start within 30s")
                return 0, 0
            log.info("qBittorrent is up")
        else:
            log.info("qBittorrent already running")

        try:
            await qbt.login()
        except Exception as e:
            log.error("Login failed: %s", e)
            return 0, 0

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _stop() -> None:
        log.warning("Stop signal received, finishing in-flight work...")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            pass

    dash = Dashboard.get_instance()
    dash.set_stop_event(stop_event)
    dash.start(stop_event)

    hdd_queue: asyncio.Queue = asyncio.Queue()
    rsync_task = asyncio.create_task(rsync_worker(hdd_queue, stop_event))
    await resume_staged_and_processed_transfers(cfg, hdd_queue)

    sem = asyncio.Semaphore(cfg.max_concurrent)
    success = 0
    failed = 0

    async def _run_one(idx: int, total: int, magnet: str) -> None:
        nonlocal success, failed
        async with sem:
            if stop_event.is_set():
                return
            try:
                ok = await process_one_magnet(
                    qbt, magnet, idx, total, cfg, hdd_queue, stop_event, hdd_index=hdd_index
                )
            except Exception:
                log.exception("Unhandled error processing magnet")
                ok = False
            if ok:
                success += 1
            else:
                failed += 1

    magnet_tasks = []
    if magnets and qbt:
        magnet_tasks = [
            asyncio.create_task(_run_one(i + 1, len(magnets), m))
            for i, m in enumerate(magnets)
        ]

    async def _run_youtube_sequentially():
        nonlocal success, failed
        for i, url in enumerate(youtube_urls):
            if stop_event.is_set():
                break
            try:
                ok = await process_youtube_url(url, i + 1, len(youtube_urls), cfg, hdd_queue)
            except Exception:
                log.exception("Unhandled error processing YouTube URL")
                ok = False
            if ok:
                success += 1
            else:
                failed += 1

    yt_task = asyncio.create_task(_run_youtube_sequentially())

    try:
        all_tasks = magnet_tasks + [yt_task]
        await asyncio.gather(*all_tasks, return_exceptions=True)
    finally:
        log.info("All downloads complete. Draining rsync queue...")
        await hdd_queue.join()
        stop_event.set()
        await rsync_task
        await dash.stop()
        cleanup_empty_dirs(cfg.ssd_buffer / "processed")

        log.info("=" * 70)
        log.info("Pipeline complete: %d succeeded, %d failed", success, failed)
        log.info("Log file: %s", log_file_path)

        if qbt:
            await qbt.logout()
            cfg.qbt_pass = ""
            os.environ.pop("QBT_PASS", None)

    if organize_after and success > 0:
        log.info("=" * 70)
        log.info("Running post-download HDD organization...")
        log.info("=" * 70)
        organize_hdd(cfg, dry_run)

    return success, failed


# =============================================================================
# Unit tests (run with --test)
# =============================================================================

def run_tests() -> bool:
    log.info("Running internal tests...")
    failures = 0

    def check(label: str, got, expected) -> None:
        nonlocal failures
        if got != expected:
            log.error("FAIL %s: got %r, expected %r", label, got, expected)
            failures += 1
        else:
            log.info("ok   %s -> %r", label, got)

    # sanitize_show_name
    check("sanitize ../etc/passwd", sanitize_show_name("../../etc/passwd"), "etcpasswd")
    check("sanitize ../",            sanitize_show_name("../"),              "Unknown_Show")
    check("sanitize ..",             sanitize_show_name(".."),               "Unknown_Show")
    check("sanitize /etc/passwd",    sanitize_show_name("/etc/passwd"),      "etcpasswd")
    check("sanitize -rf",            sanitize_show_name("-rf"),              "rf")
    check("sanitize evil/../../x",   sanitize_show_name("evil/../../tmp/x"), "evil.tmpx")
    check("sanitize after-derive",   sanitize_show_name("evil/ / / /x"),     "evil x")
    check("sanitize .../.../foo",    sanitize_show_name(".../.../foo"),      "foo")
    check("sanitize empty",          sanitize_show_name(""),                 "Unknown_Show")
    check("sanitize null bytes",     sanitize_show_name("\x00\x01\x02bad"), "bad")

    # derive_show_name
    check("derive The.Boys.S01",
          derive_show_name("The.Boys.S01.1080p.AMZN.WEBRip.DDP5.1.x264"),
          "The Boys")
    check("derive the.boys.s02",
          derive_show_name("the.boys.s02.1080p.webrip"),
          "the boys")
    check("derive With.Year",
          derive_show_name("Some.Show.2024.1080p.WEB-DL.x264"),
          "Some Show")
    check("derive Episode",
          derive_show_name("The.Boys.S01E04.The.Female.of.the.Species.1080p.AMZN.WEB-DL"),
          "The Boys")
    check("derive with uploader suffix and tv compatible",
          derive_show_name("Um Sonho Possivel - ToTTi9_TV_Compatible.mp4"),
          "Um Sonho Possivel")

    # parse_season_episode
    check("season S01E04",       parse_season_episode("Show.S01E04.1080p.mkv"),  (1, 4))
    check("season s01e01",       parse_season_episode("show.s01e01.mkv"),       (1, 1))
    check("season 1x04",         parse_season_episode("Show.1x04.mkv"),         (1, 4))
    check("season S00E01",       parse_season_episode("Show.S00E01.Special.mkv"), (0, 1))
    check("season Episodio 16",  parse_season_episode("Steins Gate - Episodio 16.mp4"), (1, 16))
    check("season anime - 01",   parse_season_episode("[Erai-raws] Show - 01 [1080p].mkv"), (1, 1))
    check("season anime 4th Season - 02", parse_season_episode("[Erai-raws] Tensei Shitara Slime Datta Ken 4th Season - 02 [1080p].mkv"), (4, 2))
    check("season S03E03 with TV compatible suffix", parse_season_episode("The Boys S03E03_TV_Compatible.mkv"), (3, 3))
    check("season no match",     parse_season_episode("random_file.mkv"),        (1, 0))

    # canonicalize_name
    check("canonical The Boys",    canonicalize_name("The Boys"),    "theboys")
    check("canonical TheBoys",     canonicalize_name("TheBoys"),    "theboys")
    check("canonical Steins;gate", canonicalize_name("Steins;gate"), "steinsgate")
    check("canonical Steins Gate", canonicalize_name("Steins Gate"), "steinsgate")

    # find_canonical_folder
    check("find TheBoys",  find_canonical_folder("TheBoys", ["The Boys", "Other"]), "The Boys")
    check("find no match", find_canonical_folder("Breaking Bad", ["The Boys"]), None)

    # sanitize_youtube_filename
    check("yt filename clean",
          sanitize_youtube_filename("Israel Adesanya x Alex ＂Poatan＂ Pereira ｜ LUTA COMPLETA ｜ UFC Freedom 250 [whp637Nl_eU].mp4"),
          "Israel Adesanya x Alex Poatan Pereira LUTA COMPLETA UFC Freedom 250 [whp637Nl_eU].mp4")
    check("yt filename brackets",
          sanitize_youtube_filename("Movie Name (2024) [abc123defgh].mkv"),
          "Movie Name (2024) [abc123defgh].mkv")

    # extract_youtube_id
    check("yt id short",  extract_youtube_id("https://youtu.be/aNALMkWfME4"), "aNALMkWfME4")
    check("yt id long",   extract_youtube_id("https://www.youtube.com/watch?v=qZPkM6Tg1co"), "qZPkM6Tg1co")
    check("yt id none",   extract_youtube_id("https://example.com"), None)

    # classify_media
    check("classify movie → FILMES",
          classify_media("Inception (2010) 1080p BluRay x264", 0.0)[0], "FILMES")
    check("classify anime → ANIMES",
          classify_media("[HorribleSubs] Attack on Titan - 01 [1080p].mkv", 1440.0)[0], "ANIMES")
    check("classify series (S01E01)",
          classify_media("The.Boys.S01E04.1080p.WEBRip.x264", 0.0)[0], "SERIES")
    check("classify outros (no clues)",
          classify_media("random_file.mkv", 0.0)[0], "OUTROS")

    # is_link and extract_links tests
    check("is_link magnet", is_link("magnet:?xt=urn:btih:xyz"), True)
    check("is_link youtube url", is_link("https://www.youtube.com/watch?v=MRcjGT85OXY"), True)
    check("is_link non-link file path", is_link("links.txt"), False)
    check("extract_links magnet", extract_links("magnet:?xt=urn:btih:xyz"), [LinkEntry(url="magnet:?xt=urn:btih:xyz", kind="magnet")])
    check("extract_links youtube", extract_links("https://www.youtube.com/watch?v=MRcjGT85OXY"), [LinkEntry(url="https://www.youtube.com/watch?v=MRcjGT85OXY", kind="youtube")])

    # remux_script check
    cfg = load_config()
    check("remux_script exists", cfg.remux_script.exists(), True)

    if failures:
        log.error("%d test(s) failed", failures)
        return False
    log.info("All tests passed.")
    return True


# =============================================================================
# Entry point
# =============================================================================

def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(
        description="Torrent download pipeline (Python)"
    )
    parser.add_argument("input_file", nargs="?", default=None,
                        help="Input file with magnet/YouTube links. "
                             "Required unless using --organize standalone.")
    parser.add_argument("--max-concurrent", type=int, default=int(os.environ.get("MAX_CONCURRENT", "3")),
                        help="Max parallel downloads to SSD (default 3, or $MAX_CONCURRENT)")
    parser.add_argument("--test", action="store_true",
                        help="Run internal sanitizer + decision tests, then exit")
    parser.add_argument("--organize", action="store_true",
                        help="Scan & reorganize HDD (merge duplicates, create season dirs). "
                             "Standalone (no input file) or after download pipeline (with input file).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview changes without executing (works with --organize)")
    parser.add_argument("--best-quality", "-q", action="store_true",
                        help="Download in best quality")
    args = parser.parse_args()

    cfg = load_config()
    if args.max_concurrent is not None:
        cfg.max_concurrent = args.max_concurrent
    if args.best_quality:
        cfg.best_quality = True

    log.info("=" * 70)
    log.info("Download and Process Pipeline (Python)")
    log.info("=" * 70)
    if args.input_file:
        log.info("Input: %s", args.input_file)
    log.info("SSD buffer: %s", cfg.ssd_buffer)
    log.info("HDD target: %s/<category>/<show>/", cfg.hdd_mount)
    log.info("Remux script: %s", cfg.remux_script)
    log.info("Max concurrent downloads: %d", cfg.max_concurrent)
    if cfg.best_quality:
        log.info("Best Quality Mode: enabled")
    log.info("Log file: %s", log_file_path)

    if args.organize:
        log.info("HDD organization: %s", "dry-run" if args.dry_run else "enabled")

    url = cfg.qbt_webui_url
    if url.startswith("http://"):
        host = urllib.parse.urlparse(url).hostname or ""
        if host not in ("localhost", "127.0.0.1", "::1"):
            log.warning(
                "WebUI uses plain HTTP for non-loopback host %r — credentials will be in cleartext",
                host,
            )

    if args.test:
        sys.exit(0 if run_tests() else 1)

    if args.organize and not args.input_file:
        sys.exit(0 if organize_hdd(cfg, args.dry_run) else 1)

    if not args.input_file:
        if shutil.which("ffprobe") is None:
            log.error("ffprobe is required")
            sys.exit(1)
        try:
            asyncio.run(process_buffer(cfg, organize_after=args.organize, dry_run=args.dry_run))
        except KeyboardInterrupt:
            log.warning("Interrupted")
            sys.exit(130)
        return

    if shutil.which("ffprobe") is None:
        log.error("ffprobe is required")
        sys.exit(1)

    try:
        asyncio.run(run_pipeline(cfg, args.input_file, organize_after=args.organize, dry_run=args.dry_run))
    except KeyboardInterrupt:
        log.warning("Interrupted")
        sys.exit(130)


if __name__ == "__main__":
    main()
