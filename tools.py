import datetime
import functools
import platform
import re
import shutil
import subprocess
import time
import unicodedata
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import quote_plus, urlparse
 
import psutil
 
# ============================================================================
# Configuration
# ============================================================================
 
SUBPROCESS_TIMEOUT_S = 10
CONFIRMATION_TIMEOUT_S = 30
 
AUDIO_SINK = "@DEFAULT_AUDIO_SINK@"
DEFAULT_VOLUME_STEP = 5
 
MAX_LISTED_ITEMS = 50
MAX_PROCESSES_LISTED = 15
MAX_QUERY_LENGTH = 300
MAX_FILE_CONTENT_CHARS = 100_000
 
HOME = Path.home().resolve()
PROJECT_DIR = Path(__file__).resolve().parent
_GIB = 1024**3
 
 
@dataclass(frozen=True)
class AppSpec:
    """A whitelisted application Gemini is allowed to open or close."""
 
    label: str
    executables: tuple[str, ...]  # candidates, first one found on the system wins
    process_names: tuple[str, ...]  # process names to terminate on "close"
    aliases: tuple[str, ...] = ()
    closable: bool = True
 
 
APPLICATIONS: dict[str, AppSpec] = {
    "vscode": AppSpec("VS Code", ("code",), ("code",), ("visual studio code", "code")),
    "firefox": AppSpec("Firefox", ("firefox",), ("firefox", "firefox-bin")),
    "chrome": AppSpec(
        "Google Chrome",
        ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"),
        ("chrome", "chromium"),
        ("google chrome", "chromium"),
    ),
    # Never closable: it may be the terminal that runs the assistant itself.
    "terminal": AppSpec(
        "Terminal",
        ("gnome-terminal", "ptyxis", "kgx", "x-terminal-emulator"),
        (),
        ("console",),
        closable=False,
    ),
    "files": AppSpec("Files", ("nautilus",), ("nautilus",), ("file manager", "fichiers")),
}
 
WEBSITE_SHORTCUTS: dict[str, str] = {
    "youtube": "https://www.youtube.com",
    "github": "https://github.com",
    "google": "https://www.google.com",
    "gmail": "https://mail.google.com",
    "wikipedia": "https://www.wikipedia.org",
}
SEARCH_URL = "https://www.google.com/search?q={query}"
 
# canonical folder name -> (xdg-user-dir key, fallback directory name); None = home
_FOLDERS: dict[str, tuple[str, str] | None] = {
    "home": None,
    "desktop": ("DESKTOP", "Desktop"),
    "documents": ("DOCUMENTS", "Documents"),
    "downloads": ("DOWNLOAD", "Downloads"),
    "pictures": ("PICTURES", "Pictures"),
    "music": ("MUSIC", "Music"),
    "videos": ("VIDEOS", "Videos"),
}
_FOLDER_ALIASES: dict[str, str] = {
    "bureau": "desktop",
    "telechargements": "downloads",
    "images": "pictures",
    "photos": "pictures",
    "musique": "music",
    "maison": "home",
}
 
# ============================================================================
# Utilities: results, errors, registry, subprocess helpers
# ============================================================================
 
 
class ToolError(Exception):
    """An expected, user-explainable failure (turned into an ERROR string)."""
 
 
def _ok(message: str) -> str:
    return f"SUCCESS: {message}"
 
 
def _err(message: str) -> str:
    return f"ERROR: {message}"
 
 
def _with_error_handling(func: Callable[..., str]) -> Callable[..., str]:
    """Make ``func`` return an ERROR string instead of raising."""
 
    @functools.wraps(func)
    def wrapper(*args, **kwargs) -> str:
        try:
            return func(*args, **kwargs)
        except ToolError as exc:
            return _err(str(exc))
        except subprocess.TimeoutExpired:
            return _err(f"{func.__name__} timed out.")
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or "").strip() or f"exit code {exc.returncode}"
            return _err(f"Command failed: {detail}")
        except FileNotFoundError as exc:
            return _err(f"File or program not found: {exc.filename or exc}")
        except PermissionError:
            return _err("Permission denied.")
        except OSError as exc:
            return _err(f"System error: {exc.strerror or exc}")
        except Exception as exc:  # last resort: a tool must never crash the voice loop
            return _err(f"Unexpected error in {func.__name__}: {exc}")
 
    return wrapper
 
 
TOOLS: list[Callable[..., str]] = []  # handed to Gemini: GenerateContentConfig(tools=TOOLS)
 
 
def tool(func: Callable[..., str]) -> Callable[..., str]:
    """Register ``func`` as a Gemini-callable tool with uniform error handling."""
    wrapped = _with_error_handling(func)
    TOOLS.append(wrapped)
    return wrapped
 
 
def _normalize_key(text: str) -> str:
    """'Téléchargements' -> 'telechargements', 'VS Code' -> 'vscode'."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", ascii_text.lower())
 
 
def _to_int(value: object, name: str, minimum: int, maximum: int) -> int:
    """Validate a number coming from the model."""
    message = f"{name} must be a whole number between {minimum} and {maximum}."
    try:
        number = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        raise ToolError(message) from None
    if not minimum <= number <= maximum:
        raise ToolError(message)
    return number
 
 
def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a command (argument list, no shell) and raise on failure."""
    return subprocess.run(
        args,
        check=True,
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_S,
    )
 
 
def _launch_detached(args: list[str]) -> None:
    """Start a GUI program that keeps running after this call returns."""
    subprocess.Popen(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
 
 
# ============================================================================
# Confirmation gate for dangerous actions
# ============================================================================
 
 
@dataclass
class _PendingAction:
    description: str
    execute: Callable[[], str]
    expires_at: float
 
 
class _ConfirmationGate:
    """Holds at most one dangerous action until the user confirms it.
 
    The model can only *arm* an action. Confirming or cancelling it is done by
    ``confirm_pending_action`` / ``cancel_pending_action``, which are NOT part
    of ``TOOLS``: Gemini cannot call them, so it can never approve on its own.
    """
 
    def __init__(self, timeout_s: float = CONFIRMATION_TIMEOUT_S) -> None:
        self._timeout_s = timeout_s
        self._pending: _PendingAction | None = None
 
    def request(self, description: str, execute: Callable[[], str]) -> str:
        expires_at = time.monotonic() + self._timeout_s
        self._pending = _PendingAction(description, execute, expires_at)
        return (
            f"CONFIRMATION_REQUIRED: {description}. Nothing has been done yet. "
            "Ask the user to confirm (yes/no) and do not retry this action yourself."
        )
 
    def take(self) -> _PendingAction | None:
        """Consume the pending action, unless there is none or it expired."""
        pending, self._pending = self._pending, None
        if pending is None or time.monotonic() > pending.expires_at:
            return None
        return pending
 
    def has_pending(self) -> bool:
        if self._pending and time.monotonic() > self._pending.expires_at:
            self._pending = None
        return self._pending is not None
 
 
_confirmations = _ConfirmationGate()
 
 
def has_pending_action() -> bool:
    """True if a dangerous action is waiting for the user's confirmation."""
    return _confirmations.has_pending()
 
 
@_with_error_handling
def confirm_pending_action() -> str:
    """Execute the pending dangerous action. Called by brain.py, never by Gemini."""
    action = _confirmations.take()
    if action is None:
        return _err("No action is waiting for confirmation (or it expired).")
    return action.execute()
 
 
def cancel_pending_action() -> str:
    """Drop the pending dangerous action. Called by brain.py, never by Gemini."""
    _confirmations.take()
    return _ok("Action cancelled.")
 
 
# ============================================================================
# Audio tools (PipeWire / WirePlumber: wpctl)
# ============================================================================
 
 
def _wpctl(*args: str) -> str:
    binary = shutil.which("wpctl")
    if binary is None:
        raise ToolError("wpctl is not available. Install PipeWire/WirePlumber.")
    return _run([binary, *args]).stdout.strip()
 
 
def _read_volume() -> tuple[int, bool]:
    """Return (volume in percent, is_muted) as reported by wpctl."""
    output = _wpctl("get-volume", AUDIO_SINK)
    match = re.search(r"Volume:\s*([\d.]+)", output)
    if match is None:
        raise ToolError(f"Unexpected wpctl output: {output!r}")
    return round(float(match.group(1)) * 100), "[MUTED]" in output
 
 
def _describe_volume() -> str:
    level, muted = _read_volume()
    return f"{level}%" + (" (muted)" if muted else "")
 
 
def _change_volume(amount: int, sign: str) -> str:
    step = _to_int(amount, "amount", 1, 100)
    _wpctl("set-volume", "-l", "1.0", AUDIO_SINK, f"{step}%{sign}")
    verb = "increased" if sign == "+" else "decreased"
    return _ok(f"Volume {verb} to {_describe_volume()}.")
 
 
def _set_mute(state: str) -> str:
    _wpctl("set-mute", AUDIO_SINK, state)
    _, muted = _read_volume()
    return _ok("Sound is muted." if muted else "Sound is unmuted.")
 
 
@tool
def increase_volume(amount: int = DEFAULT_VOLUME_STEP) -> str:
    """Increase the computer's speaker volume by `amount` percentage points (default 5, max 100 total)."""
    return _change_volume(amount, "+")
 
 
@tool
def decrease_volume(amount: int = DEFAULT_VOLUME_STEP) -> str:
    """Decrease the computer's speaker volume by `amount` percentage points (default 5)."""
    return _change_volume(amount, "-")
 
 
@tool
def set_volume(level: int) -> str:
    """Set the computer's speaker volume to an exact percentage between 0 and 100."""
    percent = _to_int(level, "level", 0, 100)
    _wpctl("set-volume", "-l", "1.0", AUDIO_SINK, f"{percent}%")
    return _ok(f"Volume set to {_describe_volume()}.")
 
 
@tool
def mute_volume() -> str:
    """Mute the computer's sound (cut the sound)."""
    return _set_mute("1")
 
 
@tool
def unmute_volume() -> str:
    """Unmute the computer's sound (restore the sound)."""
    return _set_mute("0")
 
 
@tool
def toggle_mute() -> str:
    """Toggle the computer's mute state: mute if sound is on, unmute if it is muted."""
    return _set_mute("toggle")
 
 
@tool
def get_volume() -> str:
    """Get the current speaker volume percentage and whether the sound is muted."""
    return _ok(f"Volume is {_describe_volume()}.")
 
 
# ============================================================================
# Application tools (whitelist only)
# ============================================================================
 
 
def _build_app_index() -> dict[str, AppSpec]:
    index: dict[str, AppSpec] = {}
    for key, spec in APPLICATIONS.items():
        for name in (key, *spec.aliases):
            index[_normalize_key(name)] = spec
    return index
 
 
_APP_INDEX = _build_app_index()
 
 
def _resolve_app(app_name: str) -> AppSpec:
    spec = _APP_INDEX.get(_normalize_key(app_name or ""))
    if spec is None:
        supported = ", ".join(sorted(APPLICATIONS))
        raise ToolError(f"Unknown application '{app_name}'. Supported: {supported}.")
    return spec
 
 
def _find_executable(spec: AppSpec) -> str:
    for name in spec.executables:
        path = shutil.which(name)
        if path:
            return path
    raise ToolError(f"{spec.label} is not installed.")
 
 
@tool
def open_application(app_name: str) -> str:
    """Open a supported desktop application. Supported names: vscode, firefox, chrome, terminal, files (file manager)."""
    spec = _resolve_app(app_name)
    _launch_detached([_find_executable(spec)])
    return _ok(f"{spec.label} is opening.")
 
 
@tool
def close_application(app_name: str) -> str:
    """Close a running supported application. Supported names: vscode, firefox, chrome, files (file manager)."""
    spec = _resolve_app(app_name)
    if not spec.closable:
        raise ToolError(f"{spec.label} cannot be closed by the assistant.")
    targets = [
        proc
        for proc in psutil.process_iter(["name"])
        if proc.info["name"] in spec.process_names
    ]
    if not targets:
        raise ToolError(f"{spec.label} is not running.")
    for proc in targets:
        try:
            proc.terminate()  # SIGTERM: lets the application close cleanly
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    _, still_alive = psutil.wait_procs(targets, timeout=3)
    if still_alive:
        raise ToolError(f"{spec.label} did not close.")
    return _ok(f"{spec.label} was closed.")
 
 
@tool
def check_application_available(app_name: str) -> str:
    """Check whether a supported application (vscode, firefox, chrome, terminal, files) is installed."""
    spec = _resolve_app(app_name)
    _find_executable(spec)
    return _ok(f"{spec.label} is installed.")
 
 
# ============================================================================
# System monitoring tools
# ============================================================================
 
 
@tool
def get_cpu_usage() -> str:
    """Get the current CPU usage percentage of the computer."""
    usage = psutil.cpu_percent(interval=0.5)
    cores = psutil.cpu_count(logical=True)
    return _ok(f"CPU usage is {usage:.0f}% ({cores} logical cores).")
 
 
@tool
def get_memory_usage() -> str:
    """Get the current RAM (memory) usage of the computer."""
    mem = psutil.virtual_memory()
    return _ok(
        f"RAM usage is {mem.percent:.0f}%: {mem.used / _GIB:.1f} GB used "
        f"out of {mem.total / _GIB:.1f} GB ({mem.available / _GIB:.1f} GB available)."
    )
 
 
@tool
def get_disk_usage() -> str:
    """Get the free and used disk space of the main disk."""
    disk = psutil.disk_usage("/")
    return _ok(
        f"Disk usage is {disk.percent:.0f}%: {disk.free / _GIB:.1f} GB free "
        f"out of {disk.total / _GIB:.1f} GB."
    )
 
 
@tool
def get_battery_status() -> str:
    """Get the battery level, and whether the laptop is charging or on battery."""
    battery = psutil.sensors_battery()
    if battery is None:
        raise ToolError("No battery detected (this may be a desktop PC).")
    state = "charging" if battery.power_plugged else "on battery"
    message = f"Battery is at {battery.percent:.0f}% ({state})"
    if not battery.power_plugged and battery.secsleft > 0:
        hours, minutes = divmod(battery.secsleft // 60, 60)
        message += f", about {hours}h{minutes:02d} remaining"
    return _ok(message + ".")
 
 
def _os_name() -> str:
    try:
        return platform.freedesktop_os_release().get("PRETTY_NAME", platform.system())
    except OSError:
        return platform.system()
 
 
@tool
def get_system_info() -> str:
    """Get general information about the computer: OS, CPU cores, total RAM, uptime."""
    uptime_min = int(time.time() - psutil.boot_time()) // 60
    hours, minutes = divmod(uptime_min, 60)
    return _ok(
        f"OS: {_os_name()}, kernel {platform.release()}, {platform.machine()}. "
        f"CPU: {psutil.cpu_count(logical=True)} logical cores. "
        f"RAM: {psutil.virtual_memory().total / _GIB:.1f} GB. "
        f"Python {platform.python_version()}. Uptime: {hours}h{minutes:02d}."
    )
 
 
@tool
def get_running_processes(limit: int = 5) -> str:
    """List the running programs that use the most memory (default top 5, max 15)."""
    count = _to_int(limit, "limit", 1, MAX_PROCESSES_LISTED)
    totals: dict[str, float] = {}
    for proc in psutil.process_iter(["name", "memory_percent"]):
        name = proc.info["name"] or "unknown"
        totals[name] = totals.get(name, 0.0) + (proc.info["memory_percent"] or 0.0)
    top = sorted(totals.items(), key=lambda item: item[1], reverse=True)[:count]
    listing = ", ".join(f"{name} ({percent:.1f}% RAM)" for name, percent in top)
    return _ok(f"Top {len(top)} programs by memory: {listing}.")
 
 
# ============================================================================
# Date and time tools
# ============================================================================
 
 
@tool
def get_time() -> str:
    """Get the current local time."""
    return _ok(f"It is {datetime.datetime.now():%H:%M}.")
 
 
@tool
def get_date() -> str:
    """Get today's date and the day of the week."""
    return _ok(f"Today is {datetime.datetime.now():%A, %Y-%m-%d}.")
 
 
@tool
def get_datetime() -> str:
    """Get the current local date and time."""
    return _ok(f"It is {datetime.datetime.now():%A, %Y-%m-%d, %H:%M}.")
 
 
# ============================================================================
# Web tools
# ============================================================================
 
_HOSTNAME_RE = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$")
 
 
def _normalize_url(target: str) -> str:
    """Turn 'youtube', 'github.com' or a full URL into a safe http(s) URL."""
    text = (target or "").strip()
    shortcut = WEBSITE_SHORTCUTS.get(_normalize_key(text))
    if shortcut:
        return shortcut
    if "://" not in text:
        text = f"https://{text}"
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"}:
        raise ToolError("Only http and https websites can be opened.")
    hostname = (parsed.hostname or "").lower()
    if not _HOSTNAME_RE.match(hostname):
        raise ToolError(f"Invalid website address: {target!r}.")
    return text
 
 
def _open_in_browser(url: str) -> None:
    if not webbrowser.open(url):
        raise ToolError("Unable to open the web browser.")
 
 
@tool
def search_web(query: str) -> str:
    """Search the web with Google in the user's browser for the given query."""
    text = (query or "").strip()
    if not text:
        raise ToolError("The search query is empty.")
    if len(text) > MAX_QUERY_LENGTH:
        raise ToolError(f"The search query is too long (max {MAX_QUERY_LENGTH} characters).")
    _open_in_browser(SEARCH_URL.format(query=quote_plus(text)))
    return _ok(f"Searching the web for '{text}'.")
 
 
@tool
def open_website(url: str) -> str:
    """Open a website in the browser. Accepts a name (youtube, github, google, gmail, wikipedia) or an address like example.com."""
    address = _normalize_url(url)
    _open_in_browser(address)
    return _ok(f"Opening {address}.")
 
 
# ============================================================================
# File and folder tools (restricted to the user's home folder)
# ============================================================================
 
 
@functools.cache
def _known_folder(name: str) -> Path:
    """Locate a standard folder ('desktop', 'downloads'...), honouring the OS language."""
    spec = _FOLDERS[name]
    if spec is None:
        return HOME
    xdg_key, fallback = spec
    binary = shutil.which("xdg-user-dir")
    if binary:
        try:
            output = _run([binary, xdg_key]).stdout.strip()
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            output = ""
        if output:
            return Path(output)
    return HOME / fallback
 
 
def _resolve_path(location: str) -> Path:
    """Resolve 'desktop', 'Downloads/x', '~/Documents' or an absolute path inside HOME."""
    text = (location or "").strip()
    if not text:
        raise ToolError("The path is empty.")
    raw = Path(text).expanduser()
    if not raw.parts:  # e.g. "."
        return HOME
    if not raw.is_absolute():
        first, *rest = raw.parts
        key = _normalize_key(first)
        key = _FOLDER_ALIASES.get(key, key)
        raw = _known_folder(key).joinpath(*rest) if key in _FOLDERS else HOME / raw
    resolved = raw.resolve()
    if not resolved.is_relative_to(HOME):
        raise ToolError("Access is restricted to the user's home folder.")
    return resolved
 
 
def _validate_name(name: str) -> str:
    cleaned = (name or "").strip()
    if not cleaned or cleaned in {".", ".."} or Path(cleaned).name != cleaned:
        raise ToolError(f"Invalid name {name!r}: use a simple name without '/'.")
    return cleaned
 
 
def _new_child_path(name: str, parent: str) -> Path:
    parent_dir = _resolve_path(parent)
    if not parent_dir.is_dir():
        raise ToolError(f"The folder '{parent}' does not exist.")
    return parent_dir / _validate_name(name)
 
 
def _open_with_default_app(target: Path) -> None:
    opener = shutil.which("xdg-open")
    if opener is None:
        raise ToolError("xdg-open is not available.")
    _launch_detached([opener, str(target)])
 
 
def _ensure_deletable(target: Path) -> None:
    if not target.is_file():
        raise ToolError(f"'{target.name}' is not an existing file (folders cannot be deleted).")
    if any(part.startswith(".") for part in target.relative_to(HOME).parts):
        raise ToolError("Hidden files and configuration folders are protected.")
    if target.is_relative_to(PROJECT_DIR):
        raise ToolError("The assistant's own project files are protected.")
 
 
def _trash_or_unlink(target: Path) -> str:
    gio = shutil.which("gio")
    if gio:
        _run([gio, "trash", str(target)])
        return _ok(f"'{target.name}' was moved to the trash.")
    target.unlink()
    return _ok(f"'{target.name}' was permanently deleted.")
 
 
@tool
def create_folder(folder_name: str, parent: str = "desktop") -> str:
    """Create a new folder. `parent` is a location like desktop, documents, downloads or a path in the home folder (default desktop)."""
    target = _new_child_path(folder_name, parent)
    try:
        target.mkdir()
    except FileExistsError:
        raise ToolError(f"'{target.name}' already exists.") from None
    return _ok(f"Folder '{target.name}' created in {target.parent.name}.")
 
 
@tool
def create_file(file_name: str, parent: str = "desktop", content: str = "") -> str:
    """Create a new text file (never overwrites). `parent` is desktop, documents, downloads or a path in the home folder (default desktop)."""
    if len(content) > MAX_FILE_CONTENT_CHARS:
        raise ToolError(f"The content is too long (max {MAX_FILE_CONTENT_CHARS} characters).")
    target = _new_child_path(file_name, parent)
    try:
        with target.open("x", encoding="utf-8") as handle:
            handle.write(content)
    except FileExistsError:
        raise ToolError(f"'{target.name}' already exists.") from None
    return _ok(f"File '{target.name}' created in {target.parent.name}.")
 
 
@tool
def open_file(file_path: str) -> str:
    """Open a file with its default application. `file_path` is relative to the home folder, e.g. Documents/report.pdf."""
    target = _resolve_path(file_path)
    if not target.is_file():
        raise ToolError(f"File not found: {file_path}.")
    _open_with_default_app(target)
    return _ok(f"Opening '{target.name}'.")
 
 
@tool
def open_folder(folder: str = "home") -> str:
    """Open a folder in the file manager: home, desktop, documents, downloads, pictures, music, videos or a path in the home folder."""
    target = _resolve_path(folder)
    if not target.is_dir():
        raise ToolError(f"Folder not found: {folder}.")
    _open_with_default_app(target)
    return _ok(f"Opening folder '{target.name}'.")
 
 
@tool
def list_directory(folder: str = "home") -> str:
    """List the files and folders inside a folder: home, desktop, documents, downloads, or a path in the home folder."""
    directory = _resolve_path(folder)
    if not directory.is_dir():
        raise ToolError(f"Folder not found: {folder}.")
    entries = sorted(
        (path for path in directory.iterdir() if not path.name.startswith(".")),
        key=lambda path: (not path.is_dir(), path.name.lower()),
    )
    if not entries:
        return _ok(f"'{directory.name}' is empty.")
    shown = ", ".join(p.name + "/" if p.is_dir() else p.name for p in entries[:MAX_LISTED_ITEMS])
    hidden_count = len(entries) - MAX_LISTED_ITEMS
    suffix = f" ... and {hidden_count} more" if hidden_count > 0 else ""
    return _ok(f"'{directory.name}' contains {len(entries)} item(s): {shown}{suffix}.")
 
 
@tool
def delete_file(file_path: str) -> str:
    """Delete ONE file in the home folder (never folders). Requires the user's confirmation, so nothing is deleted immediately."""
    target = _resolve_path(file_path)
    _ensure_deletable(target)
    return _confirmations.request(
        f"Delete the file '{target.name}'", lambda: _trash_or_unlink(target)
    )
 
 
# ============================================================================
# Power tools
# ============================================================================
 
 
def _lock_command() -> list[str]:
    for candidate in (["loginctl", "lock-session"], ["xdg-screensaver", "lock"]):
        if shutil.which(candidate[0]):
            return candidate
    raise ToolError("No screen-lock command found (loginctl or xdg-screensaver).")
 
 
def _arm_power_action(systemctl_verb: str, description: str) -> str:
    binary = shutil.which("systemctl")
    if binary is None:
        raise ToolError("systemctl is not available.")
 
    def execute() -> str:
        _run([binary, systemctl_verb])
        return _ok(f"{description} in progress.")
 
    return _confirmations.request(description, execute)
 
 
@tool
def lock_pc() -> str:
    """Lock the user's session (screen lock)."""
    _run(_lock_command())
    return _ok("The session is locked.")
 
 
@tool
def restart_pc() -> str:
    """Restart the computer. Requires the user's confirmation, so nothing restarts immediately."""
    return _arm_power_action("reboot", "Restart the computer")
 
 
@tool
def shutdown_pc() -> str:
    """Shut down (power off) the computer. Requires the user's confirmation, so nothing shuts down immediately."""
    return _arm_power_action("poweroff", "Shut down the computer")
 