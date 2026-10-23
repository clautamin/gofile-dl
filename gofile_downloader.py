#!/usr/bin/env python3
"""
gofile_downloader.py - download files and folders from https://gofile.io
https://github.com/clautamin/gofile-dl

Usage:
    python gofile_downloader.py <url | content_id | urls.txt> [password]

    The first argument is a gofile link (https://gofile.io/d/<id>), a bare
    content id, or a text file with one link per line. A line in that file
    may carry its own password after a space: "https://gofile.io/d/abc secret".

Environment variables (all optional):
    GF_DOWNLOAD_DIR              Root download directory (default: current dir)
    GF_MAX_CONCURRENT_DOWNLOADS  Parallel downloads (default: 5)
    GF_MAX_RETRIES               Retries per file on network errors (default: 5)
    GF_TIMEOUT                   Connect/read timeout in seconds (default: 15)
    GF_USERAGENT                 User-Agent header (default: Mozilla/5.0)
    GF_CHUNK_SIZE                Bytes per streamed chunk (default: 2097152)
    GF_TOKEN                     Existing gofile account token (skips guest account)
    GF_INTERACTIVE               Set to 1 to pick which files to download

Requires: requests  (pip install requests)
"""

from __future__ import annotations

import os
import re
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("This script needs the 'requests' package: pip install requests")


API_ROOT = "https://api.gofile.io"
# Salt and time window used by gofile's front-end to build the X-Website-Token header.
WT_SALT = "12af056dacea0b"
WT_WINDOW = 14400  # 4 hours
LANGUAGE = "en-US"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


DOWNLOAD_DIR = Path(os.environ.get("GF_DOWNLOAD_DIR", os.getcwd()))
MAX_WORKERS = max(1, _env_int("GF_MAX_CONCURRENT_DOWNLOADS", 5))
MAX_RETRIES = max(0, _env_int("GF_MAX_RETRIES", 5))
TIMEOUT = _env_float("GF_TIMEOUT", 15.0)
USER_AGENT = os.environ.get("GF_USERAGENT", "Mozilla/5.0")
CHUNK_SIZE = max(1024, _env_int("GF_CHUNK_SIZE", 2 * 1024 * 1024))
TOKEN = os.environ.get("GF_TOKEN")
INTERACTIVE = os.environ.get("GF_INTERACTIVE") == "1"


class GofileError(Exception):
    pass


@dataclass
class FileItem:
    name: str
    link: str
    size: int
    dest: Path  # full output path


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def safe_name(name: str) -> str:
    """Make a file/folder name safe on Windows, macOS and Linux."""
    name = _INVALID_CHARS.sub("_", name).strip().rstrip(".")
    if not name:
        name = "_"
    if name.split(".")[0].upper() in _RESERVED_NAMES:
        name = "_" + name
    return name[:240]


def parse_content_id(url: str) -> str:
    url = url.strip()
    m = re.search(r"gofile\.io/d/([A-Za-z0-9]+)", url)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9]+", url):
        return url
    raise GofileError(f"Not a gofile link: {url}")


class Progress:
    """Thread-safe single status line showing overall progress."""

    def __init__(self, total_files: int, total_bytes: int):
        self.total_files = total_files
        self.total_bytes = total_bytes
        self.done_files = 0
        self.done_bytes = 0
        self.start = time.monotonic()
        self._last_draw = 0.0
        self._lock = threading.Lock()

    def add_bytes(self, n: int) -> None:
        with self._lock:
            self.done_bytes += n
            now = time.monotonic()
            if now - self._last_draw >= 0.5:
                self._last_draw = now
                self._draw()

    def file_done(self, msg: str) -> None:
        with self._lock:
            self.done_files += 1
            sys.stdout.write("\r\033[K" + msg + "\n")
            self._draw()

    def message(self, msg: str) -> None:
        with self._lock:
            sys.stdout.write("\r\033[K" + msg + "\n")
            self._draw()

    def _draw(self) -> None:
        elapsed = max(time.monotonic() - self.start, 1e-6)
        speed = self.done_bytes / elapsed
        pct = (self.done_bytes / self.total_bytes * 100) if self.total_bytes else 0.0
        sys.stdout.write(
            f"\r\033[K[{self.done_files}/{self.total_files}] "
            f"{human_size(self.done_bytes)} / {human_size(self.total_bytes)} "
            f"({pct:5.1f}%)  {human_size(speed)}/s"
        )
        sys.stdout.flush()

    def finish(self) -> None:
        with self._lock:
            self._draw()
            sys.stdout.write("\n")
            sys.stdout.flush()


class GofileDownloader:
    def __init__(self, token: str | None = None):
        self._stop = threading.Event()
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Encoding": "gzip",
            "Accept": "*/*",
            "Connection": "keep-alive",
            "Origin": "https://gofile.io",
            "Referer": "https://gofile.io/",
        })
        self.token = token or self._create_guest_account()
        self.session.headers["Authorization"] = f"Bearer {self.token}"
        self.session.cookies.set("accountToken", self.token, domain=".gofile.io")

    # ---------------------------------------------------------------- API

    def _website_token(self) -> str:
        account_token = self.token if getattr(self, "token", None) else ""
        slot = int(time.time()) // WT_WINDOW
        raw = f"{USER_AGENT}::{LANGUAGE}::{account_token}::{slot}::{WT_SALT}"
        return sha256(raw.encode()).hexdigest()

    def _api(self, method: str, path: str, **kwargs) -> dict:
        headers = {"X-Website-Token": self._website_token(), "X-BL": LANGUAGE}
        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                r = self.session.request(method, API_ROOT + path, headers=headers,
                                         timeout=TIMEOUT, **kwargs)
                body = r.json()
            except (requests.RequestException, ValueError) as e:
                last_error = e
                time.sleep(min(2 ** attempt, 30))
                continue
            status = body.get("status")
            if status == "ok":
                return body.get("data", {})
            if status == "error-rateLimit":
                last_error = GofileError("rate limited by gofile")
                time.sleep(min(5 * (attempt + 1), 60))
                continue
            raise GofileError(f"API {path} returned status '{status}'")
        raise GofileError(f"API {path} failed: {last_error}")

    def _create_guest_account(self) -> str:
        self.token = ""
        data = self._api("POST", "/accounts")
        token = data.get("token")
        if not token:
            raise GofileError("Could not create a guest account")
        return token

    def get_contents(self, content_id: str, password_hash: str | None) -> dict:
        params = {"cache": "true", "sortField": "createTime", "sortDirection": "1"}
        if password_hash:
            params["password"] = password_hash
        data = self._api("GET", f"/contents/{content_id}", params=params)
        if "password" in data and data.get("passwordStatus") != "passwordOk":
            status = data.get("passwordStatus", "passwordRequired")
            raise GofileError(f"Content {content_id} is password protected ({status})")
        return data

    # --------------------------------------------------------- collecting

    def collect(self, content_id: str, dest: Path, password_hash: str | None,
                out: list[FileItem], top_level: bool = True) -> None:
        data = self.get_contents(content_id, password_hash)

        if data.get("type") != "folder":
            # A direct file link.
            out.append(FileItem(data["name"], data["link"], int(data.get("size", 0)),
                                dest / safe_name(data["name"])))
            return

        folder = dest / safe_name(data.get("name") or content_id)
        if top_level:
            # Put each top-level link in its own folder named after the content id,
            # like the original tool, so two links never collide.
            folder = dest / content_id
        used: set[str] = set()
        for child in data.get("children", {}).values():
            if child.get("type") == "folder":
                self.collect(child["id"], folder, password_hash, out, top_level=False)
                continue
            if not child.get("link"):
                continue
            name = safe_name(child["name"])
            base, ext = os.path.splitext(name)
            n = 1
            while name.lower() in used:
                name = f"{base} ({n}){ext}"
                n += 1
            used.add(name.lower())
            out.append(FileItem(child["name"], child["link"], int(child.get("size", 0)),
                                folder / name))

    # -------------------------------------------------------- downloading

    def download(self, item: FileItem, progress: Progress) -> bool:
        item.dest.parent.mkdir(parents=True, exist_ok=True)
        part = item.dest.with_name(item.dest.name + ".part")

        if item.dest.exists() and (item.size == 0 or item.dest.stat().st_size == item.size):
            progress.add_bytes(item.size)
            progress.file_done(f"skip (exists): {item.dest}")
            return True

        if part.exists():  # resuming a previous run
            progress.add_bytes(part.stat().st_size)

        for attempt in range(MAX_RETRIES + 1):
            have = part.stat().st_size if part.exists() else 0
            headers = {"Range": f"bytes={have}-"} if have else {}
            try:
                with self.session.get(item.link, headers=headers, stream=True,
                                      timeout=TIMEOUT) as r:
                    if r.status_code == 416:  # already complete
                        break
                    if r.status_code not in (200, 206):
                        raise GofileError(f"HTTP {r.status_code}")
                    if have and r.status_code == 200:
                        # Server ignored the range: start over.
                        progress.add_bytes(-have)
                        have = 0
                    ctype = r.headers.get("Content-Type", "")
                    if "text/html" in ctype and not item.name.lower().endswith((".html", ".htm")):
                        raise GofileError("got an HTML page instead of the file (link expired or blocked)")
                    with open(part, "ab" if have else "wb") as f:
                        for chunk in r.iter_content(chunk_size=CHUNK_SIZE):
                            if self._stop.is_set():
                                return False  # keep the .part file for next time
                            if chunk:
                                f.write(chunk)
                                progress.add_bytes(len(chunk))
                break
            except (requests.RequestException, GofileError, OSError) as e:
                if attempt == MAX_RETRIES:
                    progress.message(f"FAILED: {item.dest} ({e})")
                    return False
                progress.message(f"retry {attempt + 1}/{MAX_RETRIES}: {item.name} ({e})")
                if self._stop.wait(min(2 ** attempt, 30)):
                    return False

        size = part.stat().st_size if part.exists() else 0
        if item.size and size != item.size:
            progress.message(f"FAILED: {item.dest} (size {size} != expected {item.size})")
            if size > item.size:
                part.unlink()  # corrupt, resuming from it would never succeed
            return False
        os.replace(part, item.dest)
        progress.file_done(f"done: {item.dest}")
        return True

    def download_all(self, items: list[FileItem]) -> int:
        if not items:
            return 0
        progress = Progress(len(items), sum(i.size for i in items))
        failed = 0
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            pending = {pool.submit(self.download, it, progress) for it in items}
            try:
                while pending:
                    # Short timeout so Ctrl+C is noticed, notably on Windows.
                    done, pending = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
                    for fut in done:
                        try:
                            if not fut.result():
                                failed += 1
                        except Exception as e:  # never let one file kill the batch
                            failed += 1
                            progress.message(f"FAILED: {e}")
            except KeyboardInterrupt:
                self._stop.set()
                for fut in pending:
                    fut.cancel()
                raise
        progress.finish()
        return failed


def choose_files(items: list[FileItem]) -> list[FileItem]:
    for i, it in enumerate(items, 1):
        print(f"{i:4d}. {it.dest.relative_to(DOWNLOAD_DIR)}  [{human_size(it.size)}]")
    while True:
        sel = input("Files to download (e.g. 1,3-5; Enter = all): ").strip()
        if not sel:
            return items
        picked: set[int] = set()
        try:
            for part in sel.split(","):
                part = part.strip()
                if "-" in part:
                    a, b = part.split("-", 1)
                    picked.update(range(int(a), int(b) + 1))
                elif part:
                    picked.add(int(part))
        except ValueError:
            print("Couldn't read that. Use numbers like 1,3-5.")
            continue
        return [it for i, it in enumerate(items, 1) if i in picked]


def read_targets(arg: str, password: str | None) -> list[tuple[str, str | None]]:
    path = Path(arg)
    if path.is_file():
        targets = []
        # utf-8-sig drops the BOM Windows Notepad may add to the first line.
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            url, *rest = line.split(None, 1)
            targets.append((url, rest[0].strip() if rest else password))
        return targets
    return [(arg, password)]


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        return 0 if len(sys.argv) >= 2 else 1
    if os.name == "nt":
        os.system("")  # enable ANSI escape codes in the Windows console

    password = sys.argv[2] if len(sys.argv) > 2 else None
    targets = read_targets(sys.argv[1], password)

    try:
        dl = GofileDownloader(TOKEN)
    except GofileError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    failed = 0
    for url, pw in targets:
        try:
            content_id = parse_content_id(url)
            print(f"Fetching {content_id} ...")
            items: list[FileItem] = []
            pw_hash = sha256(pw.encode()).hexdigest() if pw else None
            dl.collect(content_id, DOWNLOAD_DIR, pw_hash, items)
        except GofileError as e:
            print(f"error: {e}", file=sys.stderr)
            failed += 1
            continue

        print(f"Found {len(items)} file(s), {human_size(sum(i.size for i in items))}")
        if INTERACTIVE:
            items = choose_files(items)
        failed += dl.download_all(items)

    if failed:
        print(f"{failed} item(s) failed.", file=sys.stderr)
        return 2
    print("All done.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted. Run again to resume partial downloads.")
        sys.exit(130)
