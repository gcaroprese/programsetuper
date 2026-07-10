"""
Venv-to-Executable Converter
Converts a Python virtual environment into a single executable file.
Supports Windows (.exe), macOS (.app), and Android (.apk).
"""

import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
from pathlib import Path
from string import Template

SETTINGS_DIR = os.path.join(os.path.expanduser('~'), '.venv_to_exe')
SETTINGS_FILE = os.path.join(SETTINGS_DIR, 'settings.json')


# ============================================================ Auto-detection helpers
# Pure functions (no Tk) so they are unit-testable.

def _venv_python_for(venv_dir):
    """Return the python binary path inside a venv, or None if not found."""
    if sys.platform == 'win32':
        py = os.path.join(venv_dir, "Scripts", "python.exe")
    else:
        py = os.path.join(venv_dir, "bin", "python")
    return py if os.path.isfile(py) else None


def _is_valid_venv(path):
    """True if path looks like a valid venv (has pyvenv.cfg + python binary)."""
    return os.path.isfile(os.path.join(path, 'pyvenv.cfg')) and _venv_python_for(path) is not None


def _detect_project_root(script_dir):
    """Walk up from script_dir (max 4 levels) to find a project root."""
    markers = {'.git', 'pyproject.toml', 'setup.py', 'setup.cfg',
               'requirements.txt', 'Pipfile', 'poetry.lock'}
    home = os.path.expanduser('~')
    current = os.path.abspath(script_dir)
    for _ in range(5):  # script_dir itself + 4 parents
        for m in markers:
            if os.path.exists(os.path.join(current, m)):
                return current
        parent = os.path.dirname(current)
        if parent == current or os.path.normcase(current) == os.path.normcase(home):
            break
        current = parent
    return os.path.abspath(script_dir)


def _detect_venv(script_dir, project_root):
    """Find the best venv directory.  Returns (path, [alternatives]) or (None, [])."""
    preferred_names = ['.venv', 'venv', 'env', '.env', 'virtualenv']
    candidates = []

    # Check preferred names in script_dir and project_root
    search_dirs = list(dict.fromkeys([script_dir, project_root]))
    for base in search_dirs:
        for name in preferred_names:
            p = os.path.join(base, name)
            if os.path.isdir(p) and _is_valid_venv(p):
                candidates.append(p)

    # Scan direct children of project_root for pyvenv.cfg (max 30)
    seen = {os.path.normcase(c) for c in candidates}
    try:
        children = os.listdir(project_root)
    except OSError:
        children = []
    scanned = 0
    for child in children:
        if scanned >= 30:
            break
        if child.startswith('.') and child.lower() not in ('.venv', '.env'):
            continue
        p = os.path.join(project_root, child)
        if not os.path.isdir(p):
            continue
        scanned += 1
        if os.path.normcase(p) not in seen and _is_valid_venv(p):
            candidates.append(p)

    # VIRTUAL_ENV env var
    env_venv = os.environ.get('VIRTUAL_ENV')
    if env_venv and os.path.isdir(env_venv) and _is_valid_venv(env_venv):
        nc = os.path.normcase(env_venv)
        if nc not in {os.path.normcase(c) for c in candidates}:
            candidates.append(env_venv)

    if not candidates:
        return None, []

    # Rank: prefer .venv > venv > others; prefer matching Python version
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}"

    def _rank(p):
        name = os.path.basename(p).lower()
        name_score = {'.venv': 4, 'venv': 3, 'env': 2}.get(name, 1)
        ver_score = 0
        cfg = os.path.join(p, 'pyvenv.cfg')
        try:
            with open(cfg, 'r') as f:
                for line in f:
                    if line.lower().startswith('version'):
                        if py_ver in line:
                            ver_score = 10
                        break
        except OSError:
            pass
        return (ver_score, name_score)

    candidates.sort(key=_rank, reverse=True)
    return candidates[0], candidates[1:]


def _detect_icon(script_dir, project_root, app_name, script_stem):
    """Find the best icon file.  Returns path or None."""
    icon_exts = {'.ico', '.icns', '.png'}
    asset_dirs = {'assets', 'static', 'img', 'images', 'icons', 'resources', 'res'}
    files = []
    scanned = 0
    max_files = 200

    # Collect candidates from script_dir, project_root, and asset subdirs
    search_dirs = list(dict.fromkeys([script_dir, project_root]))
    scan_dirs = list(search_dirs)
    for base in search_dirs:
        try:
            for child in os.listdir(base):
                if child.lower() in asset_dirs:
                    p = os.path.join(base, child)
                    if os.path.isdir(p):
                        scan_dirs.append(p)
        except OSError:
            pass

    for d in scan_dirs:
        try:
            entries = os.listdir(d)
        except OSError:
            continue
        for entry in entries:
            if scanned >= max_files:
                break
            fp = os.path.join(d, entry)
            if not os.path.isfile(fp):
                continue
            ext = os.path.splitext(entry)[1].lower()
            if ext in icon_exts:
                files.append(fp)
            scanned += 1
        if scanned >= max_files:
            break

    if not files:
        return None

    # Normalize names for comparison
    def _normalize(s):
        return s.lower().replace('-', '').replace('_', '').replace(' ', '')

    norm_app = _normalize(app_name)
    norm_stem = _normalize(script_stem)

    def _score(fp):
        fname = os.path.splitext(os.path.basename(fp))[0]
        norm_fname = _normalize(fname)
        ext = os.path.splitext(fp)[1].lower()
        parent = os.path.basename(os.path.dirname(fp)).lower()
        s = 0
        if norm_fname == norm_app or norm_fname == norm_stem:
            s += 50
        if any(kw in norm_fname for kw in ('icon', 'logo', 'app')):
            s += 30
        if (ext == '.ico' and sys.platform == 'win32') or (ext == '.icns' and sys.platform == 'darwin'):
            s += 20
        elif ext == '.png':
            s += 10
        if parent in asset_dirs:
            s += 10
        # PNG bonus: square and >= 128px
        if ext == '.png':
            try:
                from PIL import Image
                with Image.open(fp) as img:
                    w, h = img.size
                    if w == h and w >= 128:
                        s += 10
            except Exception:
                pass
        return s

    scored = [(f, _score(f)) for f in files]
    scored.sort(key=lambda x: (-x[1], -os.path.getsize(x[0])))
    best_path, best_score = scored[0]
    if best_score < 30:
        return None
    return best_path


def _detect_output_dir(project_root):
    """Propose an output directory."""
    return os.path.join(project_root, 'dist')


def _detect_extra_files(script_dir, project_root, source_text):
    """Find config/env files referenced by the script source."""
    config_names = {'.env', 'config.json', 'config.yaml', 'config.yml',
                    'config.ini', 'settings.json', 'settings.ini'}
    found = []
    search_dirs = list(dict.fromkeys([script_dir, project_root]))
    src_lower = source_text.lower()
    has_dotenv = 'load_dotenv' in source_text or 'from dotenv' in source_text or 'import dotenv' in source_text

    for d in search_dirs:
        try:
            entries = os.listdir(d)
        except OSError:
            continue
        for entry in entries:
            fp = os.path.join(d, entry)
            if not os.path.isfile(fp):
                continue
            name_lower = entry.lower()
            is_env_file = name_lower == '.env' or name_lower.endswith('.env')
            is_config = name_lower in config_names or is_env_file
            if not is_config:
                continue
            # Check if referenced in source
            if is_env_file and has_dotenv:
                found.append(fp)
            elif f"'{entry}'" in source_text or f'"{entry}"' in source_text:
                found.append(fp)

    # Deduplicate by normcase
    seen = set()
    unique = []
    for fp in found:
        nc = os.path.normcase(fp)
        if nc not in seen:
            seen.add(nc)
            unique.append(fp)
    return unique


def _detect_app_name(script_path, project_root):
    """Detect app name from pyproject.toml, setup.py/cfg, or script stem."""
    # 1. pyproject.toml
    toml_path = os.path.join(project_root, 'pyproject.toml')
    if os.path.isfile(toml_path):
        name = _parse_name_from_toml(toml_path)
        if name:
            return name

    # 2. setup.cfg
    setup_cfg = os.path.join(project_root, 'setup.cfg')
    if os.path.isfile(setup_cfg):
        try:
            import re
            with open(setup_cfg, 'r', encoding='utf-8') as f:
                in_metadata = False
                for line in f:
                    stripped = line.strip()
                    if stripped.startswith('['):
                        in_metadata = stripped.lower() == '[metadata]'
                        continue
                    if in_metadata:
                        m = re.match(r'^name\s*=\s*(.+)', stripped)
                        if m:
                            return m.group(1).strip()
        except Exception:
            pass

    # 3. setup.py (best-effort regex)
    setup_py = os.path.join(project_root, 'setup.py')
    if os.path.isfile(setup_py):
        try:
            import re
            with open(setup_py, 'r', encoding='utf-8') as f:
                content = f.read(4096)
            m = re.search(r'''name\s*=\s*['"]([^'"]+)['"]''', content)
            if m:
                return m.group(1)
        except Exception:
            pass

    # 4. Fallback: script stem
    stem = Path(script_path).stem.replace('_', ' ').title()
    return stem


def _parse_name_from_toml(toml_path):
    """Extract project name from pyproject.toml (stdlib tomllib or regex fallback)."""
    try:
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib  # type: ignore[no-redef]
        with open(toml_path, 'rb') as f:
            data = tomllib.load(f)
        name = data.get('project', {}).get('name')
        if not name:
            name = data.get('tool', {}).get('poetry', {}).get('name')
        if name:
            return name.strip()
    except Exception:
        pass
    # Regex fallback for Python < 3.11 without tomli
    import re
    try:
        with open(toml_path, 'r', encoding='utf-8') as f:
            content = f.read(4096)
        section = None
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith('['):
                section = stripped
            elif section in ('[project]', '[tool.poetry]'):
                m = re.match(r'^\s*name\s*=\s*["\'](.+?)["\']', stripped)
                if m:
                    return m.group(1).strip()
    except Exception:
        pass
    return None


def _hide_windows():
    """Return startupinfo and creationflags to hide CMD windows on Windows."""
    if sys.platform == 'win32':
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        return {'startupinfo': si, 'creationflags': subprocess.CREATE_NO_WINDOW}
    return {}


# --- Robust autostart + error logging wrapper ---
# Uses string.Template ($var) to avoid brace escaping issues.
# P0-6: mutex/flock instead of PID file.  P1-1: per-user data dir.
# P1-2: log rotation.  P1-7: re-register on exe move.  P2-11: fast first retry.

AUTOSTART_WRAPPER = Template(r'''
# === AUTOSTART + ERROR LOGGING BOOTSTRAP ===
import os, sys, atexit, signal, threading, traceback, datetime

_APP_NAME = $app_name_repr
_PLATFORM = $platform_repr
_ALLOW_MULTIPLE = $allow_multiple_repr

# --- 0. Prevent duplicate execution (PyInstaller --onefile spawns child process) ---
import multiprocessing
multiprocessing.freeze_support()

def _get_app_dir():
    """Directory where the executable lives."""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

def _get_exe_path():
    if getattr(sys, 'frozen', False):
        return sys.executable
    return os.path.abspath(__file__)

def _get_data_dir():
    """Per-user data directory for logs, locks, and markers."""
    if _PLATFORM == 'windows':
        base = os.environ.get('LOCALAPPDATA', os.environ.get('APPDATA', os.path.expanduser('~')))
    elif _PLATFORM == 'macos':
        base = os.path.expanduser('~/Library/Application Support')
    else:
        base = os.path.join(os.path.expanduser('~'), '.local', 'share')
    d = os.path.join(base, _APP_NAME)
    os.makedirs(d, exist_ok=True)
    return d

_DATA_DIR = _get_data_dir()

# --- 1. Error logging with rotation (1 MB cap, 1 backup) ---
_LOG_PATH = os.path.join(_DATA_DIR, _APP_NAME + '_error.log')

def _log_error(msg):
    try:
        if os.path.exists(_LOG_PATH) and os.path.getsize(_LOG_PATH) > 1_000_000:
            backup = _LOG_PATH + '.1'
            if os.path.exists(backup):
                os.remove(backup)
            os.rename(_LOG_PATH, backup)
        ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with open(_LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(f'[{ts}] {msg}\n')
    except Exception:
        pass

def _excepthook(exc_type, exc_value, exc_tb):
    msg = ''.join(traceback.format_exception(exc_type, exc_value, exc_tb))
    _log_error(f'UNHANDLED EXCEPTION:\n{msg}')
    sys.__excepthook__(exc_type, exc_value, exc_tb)

sys.excepthook = _excepthook

def _thread_excepthook(args):
    msg = ''.join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
    _log_error(f'THREAD EXCEPTION ({args.thread}):\n{msg}')

if hasattr(threading, 'excepthook'):
    threading.excepthook = _thread_excepthook

# --- 2. Single-instance enforcement (mutex on Windows, flock on POSIX) ---
if not _ALLOW_MULTIPLE:
    if _PLATFORM == 'windows':
        import ctypes as _ctypes
        _mutex = _ctypes.windll.kernel32.CreateMutexW(None, False, 'Global\\' + _APP_NAME + '_singleton')
        if _ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            sys.exit(0)
    else:
        import fcntl as _fcntl
        _lock_fh = open(os.path.join(_DATA_DIR, '.' + _APP_NAME + '.lock'), 'w')
        try:
            _fcntl.flock(_lock_fh, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        except OSError:
            sys.exit(0)

def _signal_handler(signum, frame):
    sys.exit(0)

for _sig in (signal.SIGTERM, signal.SIGINT):
    try:
        signal.signal(_sig, _signal_handler)
    except (OSError, ValueError):
        pass

if _PLATFORM == 'windows':
    try:
        signal.signal(signal.SIGBREAK, _signal_handler)
    except (AttributeError, OSError, ValueError):
        pass

# --- 3. Autostart registration (re-registers if exe moved) ---
_MARKER_PATH = os.path.join(_DATA_DIR, '.autostart_installed')

def _register_autostart():
    exe_path = _get_exe_path()

    if _PLATFORM == 'windows':
        try:
            import winreg
            try:
                key = winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Run",
                    0, winreg.KEY_READ
                )
                existing, _ = winreg.QueryValueEx(key, _APP_NAME)
                winreg.CloseKey(key)
                if existing == exe_path:
                    if not os.path.exists(_MARKER_PATH):
                        with open(_MARKER_PATH, 'w') as f:
                            f.write(exe_path)
                    return
            except (FileNotFoundError, OSError):
                pass
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run",
                0, winreg.KEY_SET_VALUE
            )
            winreg.SetValueEx(key, _APP_NAME, 0, winreg.REG_SZ, exe_path)
            winreg.CloseKey(key)
        except Exception as e:
            _log_error(f'Autostart registration FAILED: {e}\n{traceback.format_exc()}')
            return

    elif _PLATFORM == 'macos':
        try:
            import plistlib
            plist_dir = os.path.expanduser('~/Library/LaunchAgents')
            plist_path = os.path.join(plist_dir, _APP_NAME + '.plist')
            if os.path.exists(plist_path):
                try:
                    with open(plist_path, 'rb') as fp:
                        existing = plistlib.load(fp)
                    if existing.get('ProgramArguments', [None])[0] == exe_path:
                        if not os.path.exists(_MARKER_PATH):
                            with open(_MARKER_PATH, 'w') as f:
                                f.write(exe_path)
                        return
                except Exception:
                    pass
            plist = {
                'Label': _APP_NAME,
                'ProgramArguments': [exe_path],
                'RunAtLoad': True,
                'KeepAlive': False,
            }
            os.makedirs(plist_dir, exist_ok=True)
            with open(plist_path, 'wb') as fp:
                plistlib.dump(plist, fp)
        except Exception as e:
            _log_error(f'Autostart registration FAILED: {e}\n{traceback.format_exc()}')
            return
    else:
        return

    with open(_MARKER_PATH, 'w') as f:
        f.write(exe_path)

# --- 4. Run autostart with retry (60 s first, then hourly, max 8) ---
_RETRY_MAX = 8
_stop_retry = threading.Event()
_autostart_done = threading.Event()

def _autostart_worker():
    try:
        _register_autostart()
    except Exception as e:
        _log_error(f'Autostart worker error: {e}')
    finally:
        _autostart_done.set()

def _autostart_retry_loop():
    delays = [60] + [3600] * (_RETRY_MAX - 1)
    for attempt, delay in enumerate(delays, 1):
        if os.path.exists(_MARKER_PATH):
            return
        _stop_retry.wait(timeout=delay)
        if _stop_retry.is_set():
            return
        try:
            _register_autostart()
            if os.path.exists(_MARKER_PATH):
                return
        except Exception as e:
            _log_error(f'Autostart retry {attempt}/{_RETRY_MAX} FAILED: {e}')
    _log_error(f'Autostart gave up after {_RETRY_MAX} retries.')

atexit.register(lambda: _stop_retry.set())

if _PLATFORM in ('windows', 'macos'):
    _autostart_thread = threading.Thread(target=_autostart_worker, daemon=True)
    _autostart_thread.start()
    _autostart_done.wait(timeout=10)
    if not os.path.exists(_MARKER_PATH):
        _retry_thread = threading.Thread(target=_autostart_retry_loop, daemon=True)
        _retry_thread.start()

# --- 5. Fix working directory to exe location ---
os.chdir(_get_app_dir())
if os.path.exists(os.path.join(_get_app_dir(), '.env')):
    os.environ.setdefault('DOTENV_PATH', os.path.join(_get_app_dir(), '.env'))

# === END AUTOSTART BOOTSTRAP ===
''')


class VenvToExeApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Venv to Executable Converter")
        self.root.geometry("750x750")
        self.root.resizable(True, True)

        # Form variables
        self.venv_path = tk.StringVar()
        self.entry_script = tk.StringVar()
        self.icon_path = tk.StringVar()
        self.app_name = tk.StringVar(value="MyApp")
        host_plat = "windows" if sys.platform == "win32" else ("macos" if sys.platform == "darwin" else "android")
        self.platform_var = tk.StringVar(value=host_plat)
        self.output_dir = tk.StringVar()
        self.autostart = tk.BooleanVar(value=True)
        self.onefile = tk.BooleanVar(value=True)
        self.noconsole = tk.BooleanVar(value=True)
        self.allow_multiple = tk.BooleanVar(value=False)
        self.extra_files = tk.StringVar()
        self.hidden_imports_extra = tk.StringVar()  # P2-4
        self.version_var = tk.StringVar(value="1.0.0")  # P2-5
        self.company_var = tk.StringVar()  # P2-5

        # Thread-safety state
        self._proc_lock = threading.Lock()
        self._current_proc = None
        self._cancel_event = threading.Event()
        self._log_lock = threading.Lock()
        self._log_buffer = []
        self._log_flush_pending = False

        # Auto-detection state (AD-0)
        self._touched = set()  # field keys manually edited by the user
        self._last_detected_script = None  # avoid redundant re-detection

        self._build_ui()
        self._load_settings()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill=tk.BOTH, expand=True)

        # --- App name ---
        row = ttk.Frame(main)
        row.pack(fill=tk.X, pady=3)
        ttk.Label(row, text="Nombre de la app:", width=20).pack(side=tk.LEFT)
        e = ttk.Entry(row, textvariable=self.app_name)
        e.pack(side=tk.LEFT, fill=tk.X, expand=True)
        e.bind('<KeyRelease>', lambda _: self._touched.add('app_name'))

        # --- Venv path ---
        row = ttk.Frame(main)
        row.pack(fill=tk.X, pady=3)
        ttk.Label(row, text="Ruta del venv:", width=20).pack(side=tk.LEFT)
        e = ttk.Entry(row, textvariable=self.venv_path)
        e.pack(side=tk.LEFT, fill=tk.X, expand=True)
        e.bind('<KeyRelease>', lambda _: self._touched.add('venv_path'))
        ttk.Button(row, text="Buscar", command=self._browse_venv).pack(side=tk.LEFT, padx=(5, 0))

        # --- Entry script ---
        row = ttk.Frame(main)
        row.pack(fill=tk.X, pady=3)
        ttk.Label(row, text="Script principal (.py):", width=20).pack(side=tk.LEFT)
        self._script_entry = ttk.Entry(row, textvariable=self.entry_script)
        self._script_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._script_entry.bind('<FocusOut>', self._on_script_entry_change)
        self._script_entry.bind('<Return>', self._on_script_entry_change)
        ttk.Button(row, text="Buscar", command=self._browse_script).pack(side=tk.LEFT, padx=(5, 0))
        ttk.Button(row, text="Re-detectar", command=self._redetect).pack(side=tk.LEFT, padx=(5, 0))

        # --- Icon ---
        row = ttk.Frame(main)
        row.pack(fill=tk.X, pady=3)
        ttk.Label(row, text="Icono (.ico/.png):", width=20).pack(side=tk.LEFT)
        e = ttk.Entry(row, textvariable=self.icon_path)
        e.pack(side=tk.LEFT, fill=tk.X, expand=True)
        e.bind('<KeyRelease>', lambda _: self._touched.add('icon_path'))
        ttk.Button(row, text="Buscar", command=self._browse_icon).pack(side=tk.LEFT, padx=(5, 0))

        # --- Icon preview ---
        self.icon_preview_label = ttk.Label(main, text="(Sin icono seleccionado)")
        self.icon_preview_label.pack(pady=3)

        # --- Output dir ---
        row = ttk.Frame(main)
        row.pack(fill=tk.X, pady=3)
        ttk.Label(row, text="Directorio de salida:", width=20).pack(side=tk.LEFT)
        e = ttk.Entry(row, textvariable=self.output_dir)
        e.pack(side=tk.LEFT, fill=tk.X, expand=True)
        e.bind('<KeyRelease>', lambda _: self._touched.add('output_dir'))
        ttk.Button(row, text="Buscar", command=self._browse_output).pack(side=tk.LEFT, padx=(5, 0))

        # --- Platform ---
        plat_frame = ttk.LabelFrame(main, text="Plataforma destino", padding=10)
        plat_frame.pack(fill=tk.X, pady=8)

        platforms = [
            ("Windows (.exe)", "windows"),
            ("macOS (.app)", "macos"),
            ("Android (.apk)", "android"),
        ]
        self._platform_radios = {}
        for text, val in platforms:
            rb = ttk.Radiobutton(plat_frame, text=text, variable=self.platform_var,
                                 value=val, command=self._on_platform_change)
            rb.pack(side=tk.LEFT, padx=15)
            self._platform_radios[val] = rb

        # Disable cross-compilation (P0-4)
        if sys.platform != 'win32':
            self._platform_radios['windows'].config(state=tk.DISABLED)
        if sys.platform != 'darwin':
            self._platform_radios['macos'].config(state=tk.DISABLED)

        ttk.Label(plat_frame, text="(solo el SO actual; Android usa la nube)",
                  foreground="gray").pack(side=tk.LEFT, padx=(10, 0))

        # --- Options ---
        opt_frame = ttk.LabelFrame(main, text="Opciones", padding=10)
        opt_frame.pack(fill=tk.X, pady=5)

        self.autostart_check = ttk.Checkbutton(opt_frame, text="Iniciar con el sistema (primera ejecucion)",
                                                variable=self.autostart)
        self.autostart_check.pack(side=tk.LEFT, padx=10)
        ttk.Checkbutton(opt_frame, text="Un solo archivo", variable=self.onefile).pack(side=tk.LEFT, padx=10)
        ttk.Checkbutton(opt_frame, text="Sin consola (background)", variable=self.noconsole).pack(side=tk.LEFT, padx=10)
        self.multi_check = ttk.Checkbutton(opt_frame, text="Permitir multiples instancias",
                                            variable=self.allow_multiple)
        self.multi_check.pack(side=tk.LEFT, padx=10)

        # --- Metadata (P2-5) ---
        meta_frame = ttk.LabelFrame(main, text="Metadata (Windows)", padding=10)
        meta_frame.pack(fill=tk.X, pady=5)
        row = ttk.Frame(meta_frame)
        row.pack(fill=tk.X)
        ttk.Label(row, text="Version:", width=10).pack(side=tk.LEFT)
        ttk.Entry(row, textvariable=self.version_var, width=12).pack(side=tk.LEFT)
        ttk.Label(row, text="Empresa/Autor:", width=15).pack(side=tk.LEFT, padx=(10, 0))
        ttk.Entry(row, textvariable=self.company_var).pack(side=tk.LEFT, fill=tk.X, expand=True)

        # --- Extra files ---
        row = ttk.Frame(main)
        row.pack(fill=tk.X, pady=3)
        ttk.Label(row, text="Archivos extra:", width=20).pack(side=tk.LEFT)
        e = ttk.Entry(row, textvariable=self.extra_files)
        e.pack(side=tk.LEFT, fill=tk.X, expand=True)
        e.bind('<KeyRelease>', lambda _: self._touched.add('extra_files'))
        ttk.Button(row, text="Agregar", command=self._browse_extra_files).pack(side=tk.LEFT, padx=(5, 0))
        ttk.Label(main, text="  (archivos .env, configs, datos - separados por ;)", foreground="gray").pack(anchor=tk.W)

        # --- Hidden imports extra (P2-4) ---
        row = ttk.Frame(main)
        row.pack(fill=tk.X, pady=3)
        ttk.Label(row, text="Hidden imports extra:", width=20).pack(side=tk.LEFT)
        ttk.Entry(row, textvariable=self.hidden_imports_extra).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Label(main, text="  (modulos Python extra separados por ;)", foreground="gray").pack(anchor=tk.W)

        # --- Build + Cancel buttons (P2-1) ---
        btn_frame = ttk.Frame(main)
        btn_frame.pack(pady=10)
        self.build_btn = ttk.Button(btn_frame, text="COMPILAR", command=self._start_build)
        self.build_btn.pack(side=tk.LEFT, padx=5)
        self.cancel_btn = ttk.Button(btn_frame, text="Cancelar", command=self._cancel_build, state=tk.DISABLED)
        self.cancel_btn.pack(side=tk.LEFT, padx=5)

        # --- Progress ---
        self.progress = ttk.Progressbar(main, mode='indeterminate')
        self.progress.pack(fill=tk.X, pady=3)

        # --- Log ---
        ttk.Label(main, text="Log de compilacion:").pack(anchor=tk.W)
        self.log = scrolledtext.ScrolledText(main, height=12, state=tk.DISABLED, wrap=tk.WORD)
        self.log.pack(fill=tk.BOTH, expand=True, pady=3)

    # ---------------------------------------------------------- Browse helpers

    def _browse_venv(self):
        path = filedialog.askdirectory(title="Seleccionar carpeta del venv")
        if path:
            self.venv_path.set(path)
            self._touched.add('venv_path')
            self._log_venv_info(path)

    def _browse_script(self):
        path = filedialog.askopenfilename(title="Seleccionar script principal",
                                          filetypes=[("Python", "*.py")])
        if path:
            self.entry_script.set(path)
            self._auto_detect(path)

    def _browse_icon(self):
        path = filedialog.askopenfilename(
            title="Seleccionar icono",
            filetypes=[("Iconos", "*.ico *.png *.icns"), ("Todos", "*.*")]
        )
        if path:
            self.icon_path.set(path)
            self._touched.add('icon_path')
            self._update_icon_preview(path)

    def _update_icon_preview(self, path):
        try:
            from PIL import Image, ImageTk
            img = Image.open(path)
            img = img.resize((48, 48), Image.LANCZOS)
            self._icon_photo = ImageTk.PhotoImage(img)
            self.icon_preview_label.config(image=self._icon_photo, text="")
        except Exception:
            self.icon_preview_label.config(text=f"Icono: {os.path.basename(path)}", image="")

    def _browse_extra_files(self):
        paths = filedialog.askopenfilenames(
            title="Seleccionar archivos extra (.env, configs, datos)",
            filetypes=[("Todos", "*.*")]
        )
        if paths:
            self._touched.add('extra_files')
            current = self.extra_files.get()
            new_paths = ";".join(paths)
            if current:
                self.extra_files.set(current + ";" + new_paths)
            else:
                self.extra_files.set(new_paths)

    def _browse_output(self):
        path = filedialog.askdirectory(title="Seleccionar carpeta de salida")
        if path:
            self._touched.add('output_dir')
            self.output_dir.set(path)

    def _on_platform_change(self):
        plat = self.platform_var.get()
        if plat == "android":
            self.autostart_check.config(state=tk.DISABLED)
            self.autostart.set(False)
        else:
            self.autostart_check.config(state=tk.NORMAL)
            self.autostart.set(True)

    # ------------------------------------------------- Auto-detection (AD-*)

    def _log_venv_info(self, path):
        """Log venv Python version from pyvenv.cfg (shared by browse + auto-detect)."""
        cfg_file = os.path.join(path, 'pyvenv.cfg')
        if os.path.isfile(cfg_file):
            try:
                with open(cfg_file, 'r') as f:
                    for line in f:
                        if line.lower().startswith('version'):
                            self._log(f"[INFO] Venv detectado: {line.strip()}")
                            return
            except Exception:
                pass
        else:
            self._log("[AVISO] No se encontro pyvenv.cfg - puede no ser un venv valido.")

    def _on_script_entry_change(self, _event=None):
        """Trigger auto-detection when the entry-script field changes via typing."""
        path = self.entry_script.get()
        if path and os.path.isfile(path) and path != self._last_detected_script:
            self._auto_detect(path)

    def _redetect(self):
        """Re-run auto-detection, clearing touched state for detectable fields."""
        self._touched -= {'venv_path', 'icon_path', 'output_dir', 'extra_files', 'app_name'}
        path = self.entry_script.get()
        if path and os.path.isfile(path):
            self._last_detected_script = None  # force re-run
            self._auto_detect(path)
            self._log("[INFO] Re-deteccion ejecutada.")
        else:
            self._log("[AVISO] Selecciona un script .py valido para re-detectar.")

    def _auto_detect(self, script_path):
        """Run all detectors on the given script path (AD-0 orchestrator)."""
        if script_path == self._last_detected_script:
            return
        self._last_detected_script = script_path

        script_dir = os.path.dirname(os.path.abspath(script_path))
        script_stem = Path(script_path).stem
        project_root = _detect_project_root(script_dir)

        # Parse the script once for AD-5 / AD-7
        source_text = ''
        script_tree = None
        try:
            with open(script_path, 'r', encoding='utf-8') as f:
                source_text = f.read()
            script_tree = ast.parse(source_text)
        except Exception:
            pass

        # AD-6: App name
        if 'app_name' not in self._touched and self.app_name.get().strip() in ('MyApp', ''):
            name = _detect_app_name(script_path, project_root)
            if name:
                self.app_name.set(name)
                self._log(f"[AUTO] Nombre: {name}")

        # AD-2: Venv
        if 'venv_path' not in self._touched and not self.venv_path.get():
            best, alts = _detect_venv(script_dir, project_root)
            if best:
                self.venv_path.set(best)
                msg = f"[AUTO] Venv: {best}"
                if alts:
                    msg += f" (otras opciones: {', '.join(os.path.basename(a) for a in alts[:3])})"
                self._log(msg)
                self._log_venv_info(best)

        # AD-3: Icon
        if 'icon_path' not in self._touched and not self.icon_path.get():
            app_name = self.app_name.get().strip()
            icon = _detect_icon(script_dir, project_root, app_name, script_stem)
            if icon:
                self.icon_path.set(icon)
                self._log(f"[AUTO] Icono: {icon}")
                self._update_icon_preview(icon)

        # AD-4: Output dir
        if 'output_dir' not in self._touched and not self.output_dir.get():
            out = _detect_output_dir(project_root)
            self.output_dir.set(out)
            self._log(f"[AUTO] Directorio de salida: {out}")

        # AD-5: Extra files
        if 'extra_files' not in self._touched and not self.extra_files.get():
            extras = _detect_extra_files(script_dir, project_root, source_text)
            if extras:
                self.extra_files.set(';'.join(extras))
                self._log(f"[AUTO] Archivos extra: {', '.join(os.path.basename(f) for f in extras)}")

        # AD-7: Hidden import hints (informational only)
        if script_tree:
            imports = set()
            for node in ast.walk(script_tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        imports.add(alias.name.split('.')[0])
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imports.add(node.module.split('.')[0])
            hint_map = {'PIL': 'PIL', 'Crypto': 'Crypto', 'nacl': 'nacl', 'cv2': 'cv2',
                        'coincurve': 'coincurve', 'cryptography': 'cryptography',
                        'bcrypt': 'bcrypt', 'argon2': 'argon2', 'lxml': 'lxml', 'cffi': 'cffi'}
            tricky = [k for k in imports if k in hint_map]
            if tricky:
                self._log(
                    f"[AUTO] Imports que suelen necesitar hidden-imports: "
                    f"{', '.join(tricky)} (se resolveran automaticamente en el build)")

    # --------------------------------------------------------- Logging (P1-10)

    def _log(self, msg):
        """Log to widget -- must be called from main thread."""
        self.log.config(state=tk.NORMAL)
        self.log.insert(tk.END, msg + "\n")
        self.log.see(tk.END)
        self.log.config(state=tk.DISABLED)

    def _log_threadsafe(self, msg):
        """Thread-safe buffered logging (flushes every 100 ms)."""
        with self._log_lock:
            self._log_buffer.append(msg)
            if not self._log_flush_pending:
                self._log_flush_pending = True
                self.root.after(100, self._flush_log_buffer)

    def _flush_log_buffer(self):
        """Flush buffered log lines to widget -- runs on main thread."""
        with self._log_lock:
            lines = self._log_buffer[:]
            self._log_buffer.clear()
            self._log_flush_pending = False
        if lines:
            self.log.config(state=tk.NORMAL)
            self.log.insert(tk.END, '\n'.join(lines) + '\n')
            self.log.see(tk.END)
            self.log.config(state=tk.DISABLED)

    def _flush_log_now(self):
        """Force-flush any remaining buffered log lines."""
        self._flush_log_buffer()

    # --------------------------------------------------------- Validation

    def _validate(self):
        if not self.app_name.get().strip():
            messagebox.showerror("Error", "Ingresa un nombre para la aplicacion.")
            return False
        if not self.venv_path.get() or not os.path.isdir(self.venv_path.get()):
            messagebox.showerror("Error", "Selecciona una carpeta de venv valida.")
            return False
        if not self.entry_script.get() or not os.path.isfile(self.entry_script.get()):
            messagebox.showerror("Error", "Selecciona un script principal .py valido.")
            return False
        if not self.output_dir.get():
            messagebox.showerror("Error", "Selecciona un directorio de salida.")
            return False
        # P0-4: reject cross-compilation
        plat = self.platform_var.get()
        if plat == 'windows' and sys.platform != 'win32':
            messagebox.showerror("Error", "Solo se puede compilar para Windows desde Windows.")
            return False
        if plat == 'macos' and sys.platform != 'darwin':
            messagebox.showerror("Error", "Solo se puede compilar para macOS desde macOS.")
            return False
        return True

    # ---------------------------------------------------- Build orchestration

    def _start_build(self):
        if not self._validate():
            return
        # P1-3: snapshot all Tk variables before entering worker thread
        cfg = {
            'app_name': self.app_name.get().strip(),
            'venv_path': self.venv_path.get(),
            'entry_script': self.entry_script.get(),
            'icon_path': self.icon_path.get(),
            'output_dir': self.output_dir.get(),
            'platform': self.platform_var.get(),
            'autostart': self.autostart.get(),
            'onefile': self.onefile.get(),
            'noconsole': self.noconsole.get(),
            'allow_multiple': self.allow_multiple.get(),
            'extra_files': self.extra_files.get(),
            'hidden_imports_extra': self.hidden_imports_extra.get(),
            'version': self.version_var.get(),
            'company': self.company_var.get(),
        }
        # AD-4: create output dir lazily if it doesn't exist yet
        os.makedirs(cfg['output_dir'], exist_ok=True)

        self._cancel_event.clear()
        self.build_btn.config(state=tk.DISABLED)
        self.cancel_btn.config(state=tk.NORMAL)
        self.progress.start(10)
        t = threading.Thread(target=self._build, args=(cfg,), daemon=True)
        t.start()

    def _build(self, cfg):
        try:
            plat = cfg['platform']
            # P2-6: AV false-positive warning
            if plat == 'windows' and cfg['noconsole'] and cfg['onefile'] and cfg['autostart']:
                self._log_threadsafe(
                    "[AVISO] La combinacion noconsole + onefile + autostart suele activar "
                    "heuristicas de antivirus. Se recomienda firmar el binario (signtool).")
            if plat == "windows":
                self._build_windows(cfg)
            elif plat == "macos":
                self._build_macos(cfg)
            elif plat == "android":
                self._build_android(cfg)
            # P2-2: save settings on success
            self.root.after(0, self._save_settings)
        except Exception as e:
            # P0-1: bind error message eagerly to avoid NameError from late-bound `e`
            err_msg = str(e)
            tb = traceback.format_exc()
            self._log_threadsafe(f"[ERROR] {err_msg}\n{tb}")
            self.root.after(0, lambda m=err_msg: messagebox.showerror("Error", m))
        finally:
            self.root.after(0, self._flush_log_now)
            self.root.after(0, self.progress.stop)
            self.root.after(0, lambda: self.build_btn.config(state=tk.NORMAL))
            self.root.after(0, lambda: self.cancel_btn.config(state=tk.DISABLED))

    # -------------------------------------------------- Cancel (P2-1)

    def _cancel_build(self):
        self._cancel_event.set()
        with self._proc_lock:
            proc = self._current_proc
        if proc:
            self._kill_proc_tree(proc)
        self._log("[INFO] Build cancelado.")

    def _kill_proc_tree(self, proc):
        try:
            if sys.platform == 'win32':
                subprocess.run(['taskkill', '/T', '/F', '/PID', str(proc.pid)],
                               capture_output=True, **_hide_windows())
            else:
                proc.kill()
        except Exception:
            pass

    def _check_cancelled(self):
        """Raise if build was cancelled.  Call between build steps."""
        if self._cancel_event.is_set():
            raise RuntimeError("Build cancelado por el usuario.")

    # -------------------------------------------------- Venv helpers

    def _get_venv_python(self, cfg):
        venv = cfg['venv_path']
        py = _venv_python_for(venv)
        if py is None:
            expected = os.path.join(venv, "Scripts", "python.exe") if sys.platform == 'win32' \
                else os.path.join(venv, "bin", "python")
            raise FileNotFoundError(f"No se encontro python en el venv: {expected}")
        return py

    def _get_venv_site_packages(self, cfg):
        venv = cfg['venv_path']
        if sys.platform == 'win32':
            sp = os.path.join(venv, "Lib", "site-packages")
        else:
            lib_dir = os.path.join(venv, "lib")
            if os.path.isdir(lib_dir):
                for d in os.listdir(lib_dir):
                    if d.startswith("python"):
                        sp = os.path.join(lib_dir, d, "site-packages")
                        if os.path.isdir(sp):
                            return sp
            sp = os.path.join(venv, "lib", "site-packages")
        if not os.path.isdir(sp):
            raise FileNotFoundError(f"No se encontro site-packages: {sp}")
        return sp

    # ----------------------------------------- Autostart wrapper (P0-2)

    def _prepare_autostart_wrapper(self, script_path, cfg):
        """Prepend autostart wrapper respecting __future__ imports, encoding, and docstrings."""
        with open(script_path, 'r', encoding='utf-8') as f:
            original_lines = f.readlines()

        source = ''.join(original_lines)

        # 1. Extract shebang / encoding comment lines from first 2 physical lines
        prefix_indices = set()
        for i in range(min(2, len(original_lines))):
            line = original_lines[i].strip()
            if line.startswith('#!') or (line.startswith('#') and 'coding' in line):
                prefix_indices.add(i)

        docstring_indices = set()
        future_indices = set()

        # 2. Use AST to locate module docstring and __future__ imports
        try:
            tree = ast.parse(source)
            if (tree.body and isinstance(tree.body[0], ast.Expr)
                    and isinstance(getattr(tree.body[0], 'value', None), ast.Constant)
                    and isinstance(tree.body[0].value.value, str)):
                node = tree.body[0]
                for ln in range(node.lineno - 1, node.end_lineno):
                    docstring_indices.add(ln)
            for node in tree.body:
                if isinstance(node, ast.ImportFrom) and node.module == '__future__':
                    for ln in range(node.lineno - 1, node.end_lineno):
                        future_indices.add(ln)
        except SyntaxError:
            pass  # fall back to simple prepend

        all_extracted = prefix_indices | docstring_indices | future_indices

        wrapper = AUTOSTART_WRAPPER.substitute(
            app_name_repr=repr(cfg['app_name']),
            platform_repr=repr(cfg['platform']),
            allow_multiple_repr=repr(cfg['allow_multiple']),
        )

        # 3. Reassemble: prefix -> docstring -> future -> wrapper -> rest
        parts = []
        for i in sorted(prefix_indices):
            parts.append(original_lines[i])
        for i in sorted(docstring_indices):
            parts.append(original_lines[i])
        for i in sorted(future_indices):
            parts.append(original_lines[i])
        parts.append(wrapper)
        parts.append('\n')
        for i, line in enumerate(original_lines):
            if i not in all_extracted:
                parts.append(line)

        temp_dir = os.path.join(cfg['output_dir'], "_temp_build")
        os.makedirs(temp_dir, exist_ok=True)
        temp_script = os.path.join(temp_dir, os.path.basename(script_path))

        with open(temp_script, 'w', encoding='utf-8') as f:
            f.write(''.join(parts))

        return temp_script

    # ------------------------------------------------- Extra files

    def _get_extra_files(self, cfg):
        raw = cfg['extra_files'].strip()
        if not raw:
            return []
        return [p.strip() for p in raw.split(";") if p.strip() and os.path.isfile(p.strip())]

    def _copy_extra_files_to_output(self, output_dir, cfg):
        """Copy extra files next to the executable for runtime access (P1-4)."""
        for fpath in self._get_extra_files(cfg):
            dst = os.path.join(output_dir, os.path.basename(fpath))
            try:
                shutil.copy2(fpath, dst)
                self._log_threadsafe(f"[INFO] Archivo extra copiado: {dst}")
            except Exception as e:
                self._log_threadsafe(f"[AVISO] No se pudo copiar archivo extra: {e}")
        # Warn about .env secrets
        env_files = [f for f in self._get_extra_files(cfg)
                     if os.path.basename(f).endswith('.env') or os.path.basename(f) == '.env']
        if env_files:
            self._log_threadsafe(
                "[AVISO] .env contiene secretos: no lo distribuyas junto al binario a terceros.")

    # ----------------------------------------- PyInstaller helpers

    def _patch_pyinstaller_dis_bug(self, site_packages):
        """Patch PyInstaller's modulegraph/util.py to handle Python 3.10 RC dis bug (P1-8)."""
        util_path = os.path.join(site_packages, 'PyInstaller', 'lib', 'modulegraph', 'util.py')
        if not os.path.isfile(util_path):
            return
        with open(util_path, 'r', encoding='utf-8') as f:
            content = f.read()
        sentinel = '# venv_to_exe patch: dis IndexError fix'
        if sentinel in content:
            return
        old = '    yield from (i for i in dis.get_instructions(code_object) if i.opname != "EXTENDED_ARG")'
        if old not in content:
            self._log_threadsafe("[INFO] PyInstaller dis patch not needed (pattern not found)")
            return
        new = (
            f'    {sentinel}\n'
            '    try:\n'
            '        _instructions = [i for i in dis.get_instructions(code_object) if i.opname != "EXTENDED_ARG"]\n'
            '    except IndexError:\n'
            '        return\n'
            '    yield from _instructions'
        )
        content = content.replace(old, new)
        with open(util_path, 'w', encoding='utf-8') as f:
            f.write(content)
        cache_dir = os.path.join(os.path.dirname(util_path), '__pycache__')
        if os.path.isdir(cache_dir):
            shutil.rmtree(cache_dir, ignore_errors=True)
        self._log_threadsafe("[INFO] Parcheado PyInstaller para compatibilidad con Python 3.10 RC")

    def _detect_hidden_imports(self, site_packages):
        """Scan site-packages for native modules PyInstaller often misses."""
        hidden = []
        problem_packages = {
            'coincurve': ['coincurve._cffi_backend', 'coincurve._libsecp256k1'],
            'nacl': ['nacl._sodium'],
            'cryptography': ['cryptography.hazmat.bindings._rust'],
            'bcrypt': ['bcrypt._bcrypt'],
            'argon2': ['argon2._ffi'],
            'lxml': ['lxml._elementpath'],
            'cffi': ['_cffi_backend'],
        }
        for pkg, imports in problem_packages.items():
            pkg_dir = os.path.join(site_packages, pkg)
            if os.path.isdir(pkg_dir):
                hidden.extend(imports)
        return hidden

    def _collect_native_binaries(self, site_packages):
        """Find .pyd/.so files that need to be explicitly included."""
        binaries = []
        problem_packages = ['coincurve', 'nacl', 'cffi']
        for pkg in problem_packages:
            pkg_dir = os.path.join(site_packages, pkg)
            if not os.path.isdir(pkg_dir):
                continue
            for f in os.listdir(pkg_dir):
                if f.endswith(('.pyd', '.so', '.dll')):
                    src = os.path.join(pkg_dir, f)
                    binaries.append((src, pkg))
        return binaries

    def _convert_icon_to_ico(self, icon_path, cfg):
        """Convert png to ico if needed for Windows."""
        if icon_path.lower().endswith('.ico'):
            return icon_path
        try:
            from PIL import Image
            img = Image.open(icon_path)
            ico_path = os.path.join(cfg['output_dir'], "_temp_build", "icon.ico")
            os.makedirs(os.path.dirname(ico_path), exist_ok=True)
            img.save(ico_path, format='ICO', sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
            return ico_path
        except ImportError:
            self._log_threadsafe("[AVISO] Pillow no instalado, no se puede convertir PNG a ICO.")
            return None
        except Exception as e:
            self._log_threadsafe(f"[AVISO] No se pudo convertir icono: {e}")
            return None

    def _convert_icon_to_icns(self, icon_path, cfg):
        """Convert icon to .icns for macOS (P1-6)."""
        if icon_path.lower().endswith('.icns'):
            return icon_path
        try:
            from PIL import Image
            tmp_dir = os.path.join(cfg['output_dir'], "_temp_build")
            os.makedirs(tmp_dir, exist_ok=True)
            png_path = os.path.join(tmp_dir, "icon_1024.png")
            Image.open(icon_path).resize((1024, 1024), Image.LANCZOS).save(png_path, format='PNG')
            icns_path = os.path.join(tmp_dir, "icon.icns")
            r = subprocess.run(['sips', '-s', 'format', 'icns', png_path, '--out', icns_path],
                               capture_output=True, text=True)
            if r.returncode == 0 and os.path.isfile(icns_path):
                return icns_path
            self._log_threadsafe(f"[AVISO] sips fallo al convertir icono: {r.stderr.strip()}")
        except ImportError:
            self._log_threadsafe("[AVISO] Pillow no instalado, no se puede convertir icono a ICNS.")
        except Exception as e:
            self._log_threadsafe(f"[AVISO] No se pudo convertir icono a ICNS: {e}")
        return None

    def _generate_version_file(self, cfg, build_tmp):
        """Generate PyInstaller version-file for Windows exe metadata (P2-5)."""
        version = cfg.get('version', '1.0.0') or '1.0.0'
        company = cfg.get('company', '') or ''
        app_name = cfg['app_name']
        parts = version.split('.')
        while len(parts) < 4:
            parts.append('0')
        ver_tuple = tuple(int(p) if p.isdigit() else 0 for p in parts[:4])
        ver_str = '.'.join(str(v) for v in ver_tuple)
        ver_path = os.path.join(build_tmp, 'version_info.txt')
        with open(ver_path, 'w', encoding='utf-8') as f:
            f.write(
                f"VSVersionInfo(\n"
                f"  ffi=FixedFileInfo(\n"
                f"    filevers={ver_tuple},\n"
                f"    prodvers={ver_tuple},\n"
                f"    mask=0x3f,\n"
                f"    flags=0x0,\n"
                f"    OS=0x40004,\n"
                f"    fileType=0x1,\n"
                f"    subtype=0x0,\n"
                f"    date=(0, 0)\n"
                f"  ),\n"
                f"  kids=[\n"
                f"    StringFileInfo(\n"
                f"      [StringTable('040904B0',\n"
                f"        [StringStruct('CompanyName', {repr(company)}),\n"
                f"         StringStruct('FileDescription', {repr(app_name)}),\n"
                f"         StringStruct('FileVersion', {repr(ver_str)}),\n"
                f"         StringStruct('ProductName', {repr(app_name)}),\n"
                f"         StringStruct('ProductVersion', {repr(ver_str)})])]\n"
                f"    ),\n"
                f"    VarFileInfo([VarStruct('Translation', [1033, 1200])])\n"
                f"  ]\n"
                f")\n"
            )
        return ver_path

    def _detect_script_execution_style(self, script_path):
        """Detect how a script should be executed: 'main_func', 'main_guard', or 'module_level' (P1-5)."""
        try:
            with open(script_path, 'r', encoding='utf-8') as f:
                tree = ast.parse(f.read())
        except SyntaxError:
            return 'module_level'
        has_main_func = False
        has_main_guard = False
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == 'main':
                has_main_func = True
            elif isinstance(node, ast.If):
                test = node.test
                if (isinstance(test, ast.Compare) and len(test.ops) == 1
                        and isinstance(test.ops[0], ast.Eq)
                        and isinstance(test.left, ast.Name) and test.left.id == '__name__'
                        and len(test.comparators) == 1
                        and isinstance(test.comparators[0], ast.Constant)
                        and test.comparators[0].value == '__main__'):
                    has_main_guard = True
        if has_main_func:
            return 'main_func'
        if has_main_guard:
            return 'main_guard'
        return 'module_level'

    # -------------------------------------------------- Run subprocess

    def _run_cmd(self, cmd, cwd=None, env=None):
        # P2-10: pretty-print command
        self._log_threadsafe(f"$ {subprocess.list2cmdline(cmd)}")
        with self._proc_lock:
            self._check_cancelled()
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                cwd=cwd, env=env, text=True, errors='replace',
                **_hide_windows(),
            )
            self._current_proc = proc
        try:
            for line in proc.stdout:
                if self._cancel_event.is_set():
                    self._kill_proc_tree(proc)
                    raise RuntimeError("Build cancelado por el usuario.")
                line = line.rstrip()
                if line:
                    self._log_threadsafe(line)
            proc.wait()
        finally:
            with self._proc_lock:
                self._current_proc = None
        if proc.returncode != 0:
            raise RuntimeError(f"Comando fallo con codigo {proc.returncode}")
        return proc.returncode

    # ================================================= Windows build

    def _build_windows(self, cfg):
        self._log_threadsafe("=== Compilando para Windows ===")

        venv_python = self._get_venv_python(cfg)
        site_packages = self._get_venv_site_packages(cfg)
        script = cfg['entry_script']
        app_name = cfg['app_name']
        output = cfg['output_dir']

        if cfg['autostart']:
            script = self._prepare_autostart_wrapper(script, cfg)
            self._log_threadsafe("[INFO] Autostart para Windows habilitado.")

        self._check_cancelled()
        self._log_threadsafe("[INFO] Verificando PyInstaller en el venv...")
        try:
            self._run_cmd([venv_python, "-m", "pip", "install", "pyinstaller", "--quiet"])
        except Exception:
            self._log_threadsafe("[AVISO] No se pudo instalar PyInstaller en venv, usando el del sistema.")

        build_tmp = tempfile.mkdtemp(prefix=f"{app_name}_build_")
        self._log_threadsafe(f"[INFO] Carpeta temporal de build: {build_tmp}")

        self._patch_pyinstaller_dis_bug(site_packages)
        self._check_cancelled()

        # P0-3: add original script dir so sibling imports are found
        original_script_dir = os.path.dirname(os.path.abspath(cfg['entry_script']))

        cmd = [
            venv_python, "-m", "PyInstaller",
            "--name", app_name,
            "--distpath", build_tmp,
            "--workpath", os.path.join(build_tmp, "work"),
            "--specpath", os.path.join(build_tmp, "work"),
            "--paths", site_packages,
            "--paths", original_script_dir,
            "--noconfirm",
            "--clean",
        ]

        if cfg['onefile']:
            cmd.append("--onefile")
        if cfg['noconsole']:
            cmd.append("--noconsole")

        icon = cfg['icon_path']
        if icon:
            ico = self._convert_icon_to_ico(icon, cfg)
            if ico:
                cmd.extend(["--icon", ico])

        # P2-5: version metadata
        ver_path = self._generate_version_file(cfg, build_tmp)
        cmd.extend(["--version-file", ver_path])

        # Auto-detect hidden imports and native binaries
        hidden_imports = self._detect_hidden_imports(site_packages)
        # P2-4: append user-specified extra hidden imports
        extra_hi = cfg.get('hidden_imports_extra', '')
        if extra_hi:
            hidden_imports.extend(h.strip() for h in extra_hi.split(';') if h.strip())
        for hi in hidden_imports:
            cmd.extend(["--hidden-import", hi])
        if hidden_imports:
            self._log_threadsafe(f"[INFO] Hidden imports: {', '.join(hidden_imports)}")

        native_bins = self._collect_native_binaries(site_packages)
        for src, dest_pkg in native_bins:
            cmd.extend(["--add-binary", f"{src}{os.pathsep}{dest_pkg}"])
        if native_bins:
            self._log_threadsafe(f"[INFO] Binarios nativos incluidos: {len(native_bins)}")

        # P1-4: do NOT embed extra files via --add-data; copy next to exe instead
        if self._get_extra_files(cfg):
            self._log_threadsafe("[INFO] Archivos extra van junto al ejecutable (no embebidos).")

        cmd.append(script)

        self._run_cmd(cmd)

        # Move exe from temp to final output
        exe_name = app_name + ".exe"
        src_exe = os.path.join(build_tmp, exe_name)
        dst_exe = os.path.join(output, exe_name)
        if os.path.isfile(src_exe):
            if os.path.isfile(dst_exe):
                os.remove(dst_exe)
            shutil.move(src_exe, dst_exe)
            self._log_threadsafe(f"[INFO] Ejecutable movido a: {dst_exe}")
        else:
            raise RuntimeError(f"No se encontro el exe generado en: {src_exe}")

        self._copy_extra_files_to_output(output, cfg)

        # Cleanup temp
        temp_dir = os.path.join(output, "_temp_build")
        if os.path.isdir(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)
        shutil.rmtree(build_tmp, ignore_errors=True)

        self._log_threadsafe(f"\n[OK] Ejecutable Windows generado en: {output}")
        self.root.after(0, lambda o=output: messagebox.showinfo("Listo", f"Ejecutable generado en:\n{o}"))

    # ================================================= macOS build

    def _build_macos(self, cfg):
        self._log_threadsafe("=== Compilando para macOS ===")

        venv_python = self._get_venv_python(cfg)
        site_packages = self._get_venv_site_packages(cfg)
        script = cfg['entry_script']
        app_name = cfg['app_name']
        output = cfg['output_dir']

        if cfg['autostart']:
            script = self._prepare_autostart_wrapper(script, cfg)
            self._log_threadsafe("[INFO] Autostart para macOS habilitado.")

        self._check_cancelled()
        self._log_threadsafe("[INFO] Verificando PyInstaller en el venv...")
        try:
            self._run_cmd([venv_python, "-m", "pip", "install", "pyinstaller", "--quiet"])
        except Exception:
            self._log_threadsafe("[AVISO] No se pudo instalar PyInstaller en venv, usando el del sistema.")

        build_tmp = tempfile.mkdtemp(prefix=f"{app_name}_build_")
        self._log_threadsafe(f"[INFO] Carpeta temporal de build: {build_tmp}")

        self._patch_pyinstaller_dis_bug(site_packages)
        self._check_cancelled()

        original_script_dir = os.path.dirname(os.path.abspath(cfg['entry_script']))

        cmd = [
            venv_python, "-m", "PyInstaller",
            "--name", app_name,
            "--distpath", build_tmp,
            "--workpath", os.path.join(build_tmp, "work"),
            "--specpath", os.path.join(build_tmp, "work"),
            "--paths", site_packages,
            "--paths", original_script_dir,
            "--noconfirm",
            "--clean",
            "--windowed",
        ]

        if cfg['onefile']:
            cmd.append("--onefile")

        # P1-6: convert icon to .icns
        icon = cfg['icon_path']
        if icon:
            icns = self._convert_icon_to_icns(icon, cfg)
            if icns:
                cmd.extend(["--icon", icns])

        hidden_imports = self._detect_hidden_imports(site_packages)
        extra_hi = cfg.get('hidden_imports_extra', '')
        if extra_hi:
            hidden_imports.extend(h.strip() for h in extra_hi.split(';') if h.strip())
        for hi in hidden_imports:
            cmd.extend(["--hidden-import", hi])

        native_bins = self._collect_native_binaries(site_packages)
        for src, dest_pkg in native_bins:
            cmd.extend(["--add-binary", f"{src}{os.pathsep}{dest_pkg}"])

        # P1-4: no --add-data for extra files
        if self._get_extra_files(cfg):
            self._log_threadsafe("[INFO] Archivos extra van junto al ejecutable (no embebidos).")

        cmd.append(script)

        self._run_cmd(cmd)

        # Move result to final output
        for item in os.listdir(build_tmp):
            if item == "work":
                continue
            src = os.path.join(build_tmp, item)
            dst = os.path.join(output, item)
            if os.path.isfile(src):
                if os.path.isfile(dst):
                    os.remove(dst)
                shutil.move(src, dst)
            elif os.path.isdir(src):
                if os.path.isdir(dst):
                    shutil.rmtree(dst)
                shutil.move(src, dst)

        self._copy_extra_files_to_output(output, cfg)

        temp_dir = os.path.join(output, "_temp_build")
        if os.path.isdir(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)
        shutil.rmtree(build_tmp, ignore_errors=True)

        self._log_threadsafe(f"\n[OK] Aplicacion macOS generada en: {output}")
        self.root.after(0, lambda o=output: messagebox.showinfo("Listo", f"Aplicacion generada en:\n{o}"))

    # ================================================= Android build

    def _build_android(self, cfg):
        self._log_threadsafe("=== Compilando para Android (APK) ===")
        self._log_threadsafe("[INFO] Usando GitHub Actions (Buildozer en la nube).")

        script = cfg['entry_script']
        app_name = cfg['app_name']
        output = cfg['output_dir']
        pkg_name = app_name.lower().replace(' ', '_').replace('-', '_')
        repo_suffix = str(int(time.time()))[-6:]

        repo_name = None
        build_dir = None
        dl_dir = None

        try:
            # 1. Check gh CLI and scopes (P0-8)
            self._log_threadsafe("[INFO] Verificando GitHub CLI...")
            try:
                r = subprocess.run(["gh", "auth", "status"], capture_output=True,
                                   text=True, errors='replace', **_hide_windows())
                if r.returncode != 0:
                    raise RuntimeError("no auth")
                self._log_threadsafe("[OK] GitHub CLI autenticado.")
                # Check delete_repo scope
                auth_output = (r.stdout or '') + (r.stderr or '')
                if 'delete_repo' not in auth_output:
                    self._log_threadsafe(
                        "[AVISO] El token de gh no tiene el scope delete_repo. "
                        "Los repos temporales no se borraran automaticamente.\n"
                        "  Ejecuta: gh auth refresh -h github.com -s delete_repo")
            except FileNotFoundError:
                raise RuntimeError(
                    "GitHub CLI (gh) no esta instalado.\n"
                    "Instalalo desde https://cli.github.com/ y ejecuta: gh auth login")

            self._check_cancelled()

            # 2. Get top-level requirements (P0-5: importlib.metadata instead of pkg_resources)
            venv_python = self._get_venv_python(cfg)
            code = (
                "import importlib.metadata as md;"
                "dists={d.metadata['Name'].lower(): d for d in md.distributions() if d.metadata['Name']};"
                "deps=set();"
                "import re;"
                "[deps.update(re.split(r'[ ;<>=!~\\\\[\\]]', r)[0].lower() for r in (d.requires or [])) for d in dists.values()];"
                "skip={'pip','setuptools','wheel','pyinstaller','pywin32','pywin32-ctypes','pefile','altgraph','pyinstaller-hooks-contrib'};"
                "[print(n) for n in sorted(dists) if n not in deps and n not in skip]"
            )
            result = subprocess.run([venv_python, "-c", code],
                                    capture_output=True, text=True, **_hide_windows())
            if result.returncode != 0:
                self._log_threadsafe(f"[AVISO] Error detectando paquetes: {result.stderr.strip()}")
            top_reqs = [r.strip() for r in result.stdout.strip().split('\n') if r.strip()]

            # Also scan the script for direct imports
            try:
                with open(script, 'r', encoding='utf-8') as f:
                    tree = ast.parse(f.read())
                script_imports = set()
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            script_imports.add(alias.name.split('.')[0])
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        script_imports.add(node.module.split('.')[0])
            except Exception:
                script_imports = set()

            import_to_pip = {
                'dotenv': 'python-dotenv', 'eth_account': 'eth-account',
                'eth_keyfile': 'eth-keyfile', 'eth_keys': 'eth-keys',
                'eth_rlp': 'eth-rlp', 'eth_typing': 'eth-typing',
                'eth_utils': 'eth-utils', 'eth_hash': 'eth-hash',
                'eth_abi': 'eth-abi', 'bip_utils': 'bip-utils',
                'Crypto': 'pycryptodome', 'Cryptodome': 'pycryptodomex',
                'nacl': 'PyNaCl', 'PIL': 'Pillow', 'yaml': 'PyYAML',
                'cv2': 'opencv-python', 'sklearn': 'scikit-learn',
            }
            extra_reqs = set()
            for imp in script_imports:
                extra_reqs.add(import_to_pip.get(imp, imp))

            all_reqs = list(dict.fromkeys(top_reqs + list(extra_reqs)))
            stdlib = {'os', 'sys', 'time', 'datetime', 'random', 'hashlib', 'hmac', 'json', 're',
                      'threading', 'traceback', 'concurrent', 'email', 'smtplib', 'base64',
                      'collections', 'functools', 'itertools', 'math', 'struct', 'io', 'pathlib',
                      'typing', 'abc', 'copy', 'enum', 'string', 'textwrap', 'unittest', 'logging',
                      'socket', 'ssl', 'http', 'urllib', 'multiprocessing', 'signal', 'atexit',
                      'subprocess', 'shutil', 'tempfile', 'glob', 'fnmatch', 'stat', 'zipfile'}
            all_reqs = [r for r in all_reqs if r.lower().replace('-', '_') not in stdlib]

            reqs_list = ",".join(all_reqs) if all_reqs else "kivy"
            self._log_threadsafe(f"[INFO] Paquetes: {', '.join(all_reqs)}")

            self._check_cancelled()

            # 3. Create temp build directory
            build_dir = tempfile.mkdtemp(prefix=f"{app_name}_apk_")
            self._log_threadsafe(f"[INFO] Preparando proyecto en: {build_dir}")

            # Copy the original script as a worker module
            worker_name = os.path.basename(script).replace('.py', '_worker')
            shutil.copy2(script, os.path.join(build_dir, worker_name + '.py'))

            # Copy extra files (rename dotfiles for Buildozer)
            for fpath in self._get_extra_files(cfg):
                fname = os.path.basename(fpath)
                if fname.startswith('.'):
                    fname = fname[1:] + '.txt'
                shutil.copy2(fpath, os.path.join(build_dir, fname))

            # Copy icon
            icon = cfg['icon_path']
            if icon and os.path.isfile(icon):
                shutil.copy2(icon, os.path.join(build_dir, "icon.png"))

            # Separate native vs pure-Python packages
            p4a_native = {
                'pycryptodome', 'pycryptodomex', 'cryptography', 'cffi', 'pynacl',
                'libsodium', 'openssl', 'pillow', 'numpy', 'scipy', 'opencv-python',
                'lxml', 'bcrypt', 'gevent', 'greenlet', 'netifaces', 'psutil',
                'ujson', 'msgpack', 'cymem', 'preshed',
            }
            import_to_p4a = {
                'Crypto': 'pycryptodome', 'Cryptodome': 'pycryptodomex',
                'nacl': 'pynacl', 'PIL': 'pillow', 'cv2': 'opencv',
            }
            site_packages = self._get_venv_site_packages(cfg)
            detected_native = set()
            for req in all_reqs:
                req_dir = req.lower().replace('-', '_')
                pkg_path = os.path.join(site_packages, req_dir)
                if os.path.isdir(pkg_path):
                    for root_d, _, files in os.walk(pkg_path):
                        if any(f.endswith(('.pyd', '.so')) for f in files):
                            detected_native.add(req)
                            break

            native_reqs = set()
            pure_reqs = []
            for req in all_reqs:
                req_lower = req.lower().replace('-', '_')
                if req_lower in {n.lower().replace('-', '_') for n in p4a_native} or req in detected_native:
                    native_reqs.add(req)
                else:
                    pure_reqs.append(req)

            for imp, p4a_name in import_to_p4a.items():
                if imp in script_imports or any(imp.lower() in r.lower() for r in all_reqs):
                    native_reqs.add(p4a_name)

            if 'pynacl' in {n.lower() for n in native_reqs}:
                native_reqs.add('libsodium')
            if native_reqs & {'pycryptodome', 'pycryptodomex', 'cffi'}:
                native_reqs.add('openssl')
                native_reqs.add('cffi')

            # P2-7: pin p4a.branch to master
            buildozer_reqs = ['python3', 'kivy'] + sorted(native_reqs)
            buildozer_reqs_str = ','.join(buildozer_reqs)

            native_dirs = {'Crypto', 'Cryptodome', 'nacl', 'cffi', '_cffi_backend',
                           'PIL', 'numpy', 'cv2', 'lxml', 'greenlet', 'gevent'}

            self._log_threadsafe(
                f"[INFO] Nativos (p4a): {', '.join(sorted(native_reqs))}\n"
                f"[INFO] Pure-Python (pip): {', '.join(pure_reqs[:10])}{'...' if len(pure_reqs) > 10 else ''}")

            # Create install_deps.sh
            install_deps_content = '#!/bin/bash\nset -e\n\n'
            install_deps_content += '# Install pure-Python packages (native ones compiled by p4a)\n'
            if pure_reqs:
                install_deps_content += 'pip install --target=site-packages --no-deps \\\n'
                install_deps_content += ' \\\n'.join(f'    {p}' for p in pure_reqs)
                install_deps_content += ' 2>&1\n\n'
            install_deps_content += '# Remove native package dirs that conflict with p4a ARM64\n'
            for d in sorted(native_dirs):
                install_deps_content += f'rm -rf site-packages/{d} site-packages/{d.lower()}*\n'
            install_deps_content += '\n# Remove all .so files (x86_64 from pip, not ARM64)\n'
            install_deps_content += 'find site-packages -name "*.so" -delete 2>/dev/null || true\n'
            install_deps_content += 'echo "Installed $(find site-packages -name \'*.py\' | wc -l) .py files"\n'

            with open(os.path.join(build_dir, "install_deps.sh"), 'w', newline='\n') as f:
                f.write(install_deps_content)

            # P1-5: detect execution style and generate appropriate main.py
            exec_style = self._detect_script_execution_style(script)
            self._log_threadsafe(f"[INFO] Modo de ejecucion del script: {exec_style}")

            if exec_style == 'main_guard':
                worker_run_code = (
                    f"            import runpy\n"
                    f"            runpy.run_path(os.path.join(app_dir, '{worker_name}.py'), run_name='__main__')\n"
                )
            elif exec_style == 'main_func':
                worker_run_code = (
                    f"            import {worker_name}\n"
                    f"            {worker_name}.main()\n"
                )
            else:
                worker_run_code = (
                    f"            import {worker_name}\n"
                )

            with open(os.path.join(build_dir, "main.py"), 'w', newline='\n') as f:
                f.write(f'''import os, sys, threading, traceback
app_dir = os.path.dirname(os.path.abspath(__file__))
sp = os.path.join(app_dir, 'site-packages')
if os.path.isdir(sp):
    sys.path.append(sp)
os.chdir(app_dir)

from kivy.app import App
from kivy.uix.label import Label
from kivy.uix.scrollview import ScrollView
from kivy.clock import Clock
from kivy.core.window import Window

class MainApp(App):
    def build(self):
        Window.clearcolor = (0.1, 0.1, 0.1, 1)
        self.label = Label(
            text='{app_name}\\nIniciando...',
            font_size='16sp', halign='center', valign='top',
            size_hint_y=None, text_size=(Window.width - 40, None))
        self.label.bind(texture_size=self.label.setter('size'))
        scroll = ScrollView()
        scroll.add_widget(self.label)
        Clock.schedule_once(self.start_worker, 2)
        return scroll

    def log(self, msg):
        def update(dt):
            self.label.text += '\\n' + msg
        Clock.schedule_once(update)

    def start_worker(self, dt):
        self.log('Ejecutando...')
        threading.Thread(target=self.run_worker, daemon=True).start()

    def run_worker(self):
        try:
            try:
                from dotenv import load_dotenv
                for env_name in ['env.txt', '.env']:
                    env_path = os.path.join(app_dir, env_name)
                    if os.path.exists(env_path):
                        load_dotenv(env_path)
                        self.log(f'Config cargada: {{env_name}}')
                        break
            except Exception:
                pass

{worker_run_code}            self.log('Finalizado.')
        except Exception as e:
            self.log(f'ERROR: {{e}}')
            self.log(traceback.format_exc())

if __name__ == '__main__':
    MainApp().run()
''')

            # Write buildozer.spec
            icon_line = "icon.filename = icon.png" if icon else "# icon.filename ="
            with open(os.path.join(build_dir, "buildozer.spec"), 'w', newline='\n') as f:
                f.write(f"""[app]
title = {app_name}
package.name = {pkg_name}
package.domain = org.test
source.dir = .
source.include_exts = py,pyc,pyd,so,png,jpg,kv,atlas,ico,env,json,txt
source.exclude_dirs = .git,.github,__pycache__,venv,.venv
version = 1.0.0
requirements = {buildozer_reqs_str}
{icon_line}
orientation = portrait
fullscreen = 0
android.permissions = INTERNET
android.api = 33
android.minapi = 24
android.archs = arm64-v8a
p4a.branch = master

[buildozer]
log_level = 2
warn_on_root = 0
""")

            # Build verification module lists
            pure_module_names = [req.lower().replace('-', '_') for req in pure_reqs]
            mod_list_str = ', '.join(f"'{m}'" for m in pure_module_names[:30])
            native_dir_list = ', '.join(f"'{d}'" for d in sorted(native_dirs))

            # Write GitHub Actions workflow
            wf_dir = os.path.join(build_dir, ".github", "workflows")
            os.makedirs(wf_dir, exist_ok=True)

            with open(os.path.join(wf_dir, "build-apk.yml"), 'w', newline='\n') as f:
                f.write(f"""name: Build APK
on:
  push:
  workflow_dispatch:
jobs:
  build:
    runs-on: ubuntu-latest
    timeout-minutes: 90
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.10'
      - name: Install system dependencies
        run: |
          sudo apt-get update -qq
          sudo apt-get install -y -qq build-essential git zip unzip \\
            openjdk-17-jdk autoconf automake libtool libltdl-dev pkg-config \\
            libffi-dev libssl-dev cmake zlib1g-dev ccache lld
      - name: Install buildozer
        run: pip install buildozer==1.5.0 cython
      - name: Pre-install Python packages
        run: bash install_deps.sh
      - name: Build APK
        run: yes | buildozer android debug
      - name: Verify APK contents
        if: always()
        run: |
          APK=$(find bin -name "*.apk" 2>/dev/null | head -1)
          if [ -z "$APK" ]; then echo "NO APK FOUND"; exit 1; fi
          echo "APK: $APK ($(du -h "$APK" | cut -f1))"
          python3 << 'PYEOF'
          import zipfile, io, tarfile, sys, os
          apk = [f for f in os.listdir("bin") if f.endswith(".apk")][0]
          with zipfile.ZipFile(os.path.join("bin", apk)) as z:
              if "assets/private.tar" not in z.namelist():
                  print("FATAL: assets/private.tar not found"); sys.exit(1)
              tar_data = z.read("assets/private.tar")
          with tarfile.open(fileobj=io.BytesIO(tar_data)) as t:
              names = t.getnames()
              print(f"Files in private.tar: {{len(names)}}")
              errors = []
              pure_mods = [{mod_list_str}]
              for mod in pure_mods:
                  found = [n for n in names if n.startswith(f"site-packages/{{mod}}/") or f"/{{mod}}.py" in n or n == f"{{mod}}.py"]
                  print(f"  {{'OK' if found else 'MISSING'}}: {{mod}} ({{len(found)}})")
                  if not found: errors.append(mod)
              for mod in [{native_dir_list}]:
                  bad = [n for n in names if n.startswith(f"site-packages/{{mod}}/")]
                  if bad:
                      print(f"  CONFLICT: {{mod}} in site-packages ({{len(bad)}})")
                      errors.append(f"{{mod}}_conflict")
              so_bad = [n for n in names if 'site-packages' in n and n.endswith('.so')]
              if so_bad:
                  print(f"  BAD: {{len(so_bad)}} .so files in site-packages")
                  errors.append("so_files")
              for f in ['main.py', '{worker_name}.py']:
                  if f not in names: errors.append(f); print(f"  MISSING: {{f}}")
                  else: print(f"  OK: {{f}}")
              if errors:
                  print(f"FAIL: {{errors}}"); sys.exit(1)
              else:
                  print("PASS - APK looks good for ARM64")
          PYEOF
      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: APK
          path: bin/*.apk
          retention-days: 30
""")

            # Write .gitignore
            with open(os.path.join(build_dir, ".gitignore"), 'w', newline='\n') as f:
                f.write(".buildozer/\nbin/\n__pycache__/\n*.pyc\n")

            self._check_cancelled()

            # 4. Create private GitHub repo and push
            repo_name = f"_build-{pkg_name}-{repo_suffix}"
            self._log_threadsafe(f"[INFO] Creando repo temporal: {repo_name}")

            # Delete pre-existing repo (log failure per P0-8)
            dr = subprocess.run(["gh", "repo", "delete", repo_name, "--yes"],
                                capture_output=True, text=True, errors='replace', **_hide_windows())
            if dr.returncode != 0 and 'not found' not in (dr.stderr or '').lower():
                self._log_threadsafe(f"[AVISO] No se pudo eliminar repo previo: {(dr.stderr or '').strip()}")

            # P1-9: set local git identity
            self._run_cmd(["git", "init"], cwd=build_dir)
            self._run_cmd(["git", "config", "user.email", "build@local"], cwd=build_dir)
            self._run_cmd(["git", "config", "user.name", "venv_to_exe"], cwd=build_dir)
            self._run_cmd(["git", "config", "core.autocrlf", "input"], cwd=build_dir)
            self._run_cmd(["git", "add", "-A"], cwd=build_dir)
            self._run_cmd(["git", "commit", "-m", "APK build"], cwd=build_dir)
            self._run_cmd(["gh", "repo", "create", repo_name, "--private", "--source=.", "--push"], cwd=build_dir)

            self._log_threadsafe("[INFO] Codigo subido. Esperando que GitHub Actions compile...")
            self._log_threadsafe("[NOTA] Esto tarda 15-30 minutos la primera vez.")

            # 5. Wait for workflow to complete
            max_wait = 5400
            poll_interval = 30
            waited = 0
            run_id = None
            poll_count = 0

            while waited < max_wait:
                # P2-1: check cancel in polling loop
                if self._cancel_event.is_set():
                    raise RuntimeError("Build cancelado por el usuario.")
                time.sleep(poll_interval)
                waited += poll_interval
                poll_count += 1

                r = subprocess.run(
                    ["gh", "run", "list", "--repo", repo_name, "--limit", "1",
                     "--json", "databaseId,status,conclusion"],
                    capture_output=True, text=True, errors='replace', **_hide_windows()
                )
                try:
                    runs = json.loads(r.stdout)
                    if runs:
                        run = runs[0]
                        run_id = run['databaseId']
                        status = run['status']
                        conclusion = run.get('conclusion', '')
                        mins = waited // 60
                        self._log_threadsafe(f"  [{mins}min] Estado: {status}")

                        # P2-8: fetch current step every 3rd poll
                        if status == 'in_progress' and poll_count % 3 == 0:
                            jr = subprocess.run(
                                ["gh", "run", "view", str(run_id), "--repo", repo_name, "--json", "jobs"],
                                capture_output=True, text=True, errors='replace', **_hide_windows()
                            )
                            try:
                                jobs = json.loads(jr.stdout).get('jobs', [])
                                for job in jobs:
                                    for step in job.get('steps', []):
                                        if step.get('status') == 'in_progress':
                                            self._log_threadsafe(f"    Paso actual: {step.get('name', '?')}")
                            except (json.JSONDecodeError, KeyError):
                                pass

                        if status == 'completed':
                            if conclusion == 'success':
                                self._log_threadsafe("[OK] Build completado con exito!")
                                break
                            else:
                                # P0-7: capture and display failure logs
                                fl = subprocess.run(
                                    ["gh", "run", "view", str(run_id), "--repo", repo_name, "--log-failed"],
                                    capture_output=True, text=True, errors='replace', **_hide_windows()
                                )
                                log_tail = (fl.stdout or '')[-8000:]
                                self._log_threadsafe(f"[ERROR] Log del build fallido:\n{log_tail}")
                                log_file = os.path.join(output, f"{app_name}_apk_build_failed.log")
                                with open(log_file, 'w', encoding='utf-8') as lf:
                                    lf.write(fl.stdout or '')
                                self._log_threadsafe(f"[INFO] Log completo guardado en: {log_file}")
                                # Get repo URL for inspection
                                url_r = subprocess.run(
                                    ["gh", "repo", "view", repo_name, "--json", "url", "--jq", ".url"],
                                    capture_output=True, text=True, errors='replace', **_hide_windows()
                                )
                                repo_url = url_r.stdout.strip() or repo_name
                                self._log_threadsafe(f"[INFO] Repo preservado para inspeccion: {repo_url}")
                                raise RuntimeError(f"Build fallo con: {conclusion}")
                except (json.JSONDecodeError, KeyError, IndexError):
                    pass

            if waited >= max_wait:
                raise RuntimeError("Timeout: el build tardo mas de 90 minutos.")

            # 6. Download the APK artifact
            self._log_threadsafe("[INFO] Descargando APK...")
            dl_dir = os.path.join(output, f"{app_name}_apk_download")
            os.makedirs(dl_dir, exist_ok=True)

            self._run_cmd([
                "gh", "run", "download", str(run_id),
                "--repo", repo_name,
                "--name", "APK",
                "--dir", dl_dir
            ])

            apk_found = False
            for f in os.listdir(dl_dir):
                if f.endswith('.apk'):
                    src = os.path.join(dl_dir, f)
                    dst = os.path.join(output, f"{app_name}.apk")
                    shutil.move(src, dst)
                    self._log_threadsafe(f"[OK] APK descargado: {dst}")
                    apk_found = True
                    break

            # 7. Cleanup on success
            self._log_threadsafe("[INFO] Limpiando repo temporal...")
            dr = subprocess.run(["gh", "repo", "delete", repo_name, "--yes"],
                                capture_output=True, text=True, errors='replace', **_hide_windows())
            if dr.returncode != 0:
                self._log_threadsafe(f"[AVISO] No se pudo eliminar repo: {(dr.stderr or '').strip()}")

            if apk_found:
                self._log_threadsafe(f"\n[OK] APK Android generado en: {output}")
                self.root.after(0, lambda o=output: messagebox.showinfo("Listo", f"APK generado en:\n{o}"))
            else:
                raise RuntimeError("No se encontro el APK en los artifacts de GitHub Actions.")

        except Exception as e:
            # On cancel, try to delete repo; on other failure, preserve it
            if self._cancel_event.is_set() and repo_name:
                dr = subprocess.run(["gh", "repo", "delete", repo_name, "--yes"],
                                    capture_output=True, text=True, errors='replace', **_hide_windows())
                if dr.returncode == 0:
                    self._log_threadsafe(f"[INFO] Repo temporal {repo_name} eliminado.")
            raise
        finally:
            if build_dir and os.path.isdir(build_dir):
                shutil.rmtree(build_dir, ignore_errors=True)
            if dl_dir and os.path.isdir(dl_dir):
                shutil.rmtree(dl_dir, ignore_errors=True)

    # ========================================== Settings persistence (P2-2)

    def _load_settings(self):
        if not os.path.isfile(SETTINGS_FILE):
            return
        try:
            with open(SETTINGS_FILE, 'r') as f:
                s = json.load(f)
        except (json.JSONDecodeError, IOError):
            return
        str_map = {
            'app_name': self.app_name, 'venv_path': self.venv_path,
            'entry_script': self.entry_script, 'icon_path': self.icon_path,
            'output_dir': self.output_dir, 'extra_files': self.extra_files,
            'hidden_imports_extra': self.hidden_imports_extra,
            'version': self.version_var, 'company': self.company_var,
        }
        bool_map = {
            'autostart': self.autostart, 'onefile': self.onefile,
            'noconsole': self.noconsole, 'allow_multiple': self.allow_multiple,
        }
        for key, var in str_map.items():
            if s.get(key):
                var.set(s[key])
                self._touched.add(key)  # AD-8: loaded values count as user-touched
        for key, var in bool_map.items():
            if key in s:
                var.set(s[key])
        # Only load platform if valid for host
        plat = s.get('platform')
        if plat == 'android' or (plat == 'windows' and sys.platform == 'win32') or \
                (plat == 'macos' and sys.platform == 'darwin'):
            self.platform_var.set(plat)
        icon = self.icon_path.get()
        if icon and os.path.isfile(icon):
            self._update_icon_preview(icon)
        self._on_platform_change()

    def _save_settings(self):
        settings = {
            'app_name': self.app_name.get().strip(),
            'venv_path': self.venv_path.get(),
            'entry_script': self.entry_script.get(),
            'icon_path': self.icon_path.get(),
            'output_dir': self.output_dir.get(),
            'platform': self.platform_var.get(),
            'autostart': self.autostart.get(),
            'onefile': self.onefile.get(),
            'noconsole': self.noconsole.get(),
            'allow_multiple': self.allow_multiple.get(),
            'extra_files': self.extra_files.get(),
            'hidden_imports_extra': self.hidden_imports_extra.get(),
            'version': self.version_var.get(),
            'company': self.company_var.get(),
        }
        # Only persist non-empty string values and all booleans
        settings = {k: v for k, v in settings.items() if v or isinstance(v, bool)}
        try:
            os.makedirs(SETTINGS_DIR, exist_ok=True)
            with open(SETTINGS_FILE, 'w') as f:
                json.dump(settings, f, indent=2)
        except Exception:
            pass

    def _on_close(self):
        self._save_settings()
        self.root.destroy()


def main():
    root = tk.Tk()
    VenvToExeApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
