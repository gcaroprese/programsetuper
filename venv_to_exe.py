"""
Venv-to-Executable Converter
Convierte un entorno virtual Python en un unico archivo ejecutable.
Soporta Windows (.exe), macOS (.app) y Android (.apk).
"""

import os
import sys
import shutil
import subprocess
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
from pathlib import Path
from string import Template

# --- Robust autostart + error logging wrapper ---
# Uses string.Template ($var) to avoid brace escaping issues

AUTOSTART_WRAPPER = Template(r'''
# === AUTOSTART + ERROR LOGGING BOOTSTRAP ===
import os, sys, atexit, signal, threading, traceback, datetime

_APP_NAME = $app_name_repr
_PLATFORM = $platform_repr

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

# --- 1. Error logging to file next to executable ---
_LOG_PATH = os.path.join(_get_app_dir(), _APP_NAME + '_error.log')

def _log_error(msg):
    try:
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

# --- 2. Lock file with PID to avoid duplicate instances ---
_LOCK_PATH = os.path.join(_get_app_dir(), '.' + _APP_NAME + '.lock')

def _is_already_running():
    if os.path.exists(_LOCK_PATH):
        try:
            with open(_LOCK_PATH, 'r') as f:
                old_pid = int(f.read().strip())
            if _PLATFORM == 'windows':
                import ctypes
                kernel32 = ctypes.windll.kernel32
                handle = kernel32.OpenProcess(0x1000, False, old_pid)  # PROCESS_QUERY_LIMITED_INFORMATION
                if handle:
                    kernel32.CloseHandle(handle)
                    return True
            else:
                os.kill(old_pid, 0)
                return True
        except (ValueError, OSError, Exception):
            pass
    return False

def _write_lock():
    try:
        with open(_LOCK_PATH, 'w') as f:
            f.write(str(os.getpid()))
    except Exception as e:
        _log_error(f'Failed to write lock file: {e}')

def _remove_lock():
    try:
        if os.path.exists(_LOCK_PATH):
            os.remove(_LOCK_PATH)
    except Exception:
        pass

if _is_already_running():
    _log_error('Another instance is already running. Exiting.')
    sys.exit(0)

_write_lock()
atexit.register(_remove_lock)

def _signal_handler(signum, frame):
    _remove_lock()
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

# --- 3. Autostart registration (first run only) ---
_MARKER_PATH = None
if _PLATFORM == 'windows':
    _MARKER_PATH = os.path.join(
        os.environ.get('APPDATA', os.path.expanduser('~')),
        _APP_NAME, '.autostart_installed'
    )
elif _PLATFORM == 'macos':
    _MARKER_PATH = os.path.expanduser('~/.' + _APP_NAME + '_autostart_installed')

def _register_autostart():
    exe_path = _get_exe_path()
    if _MARKER_PATH is None:
        return
    if os.path.exists(_MARKER_PATH):
        return

    try:
        if _PLATFORM == 'windows':
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run",
                0, winreg.KEY_SET_VALUE
            )
            winreg.SetValueEx(key, _APP_NAME, 0, winreg.REG_SZ, exe_path)
            winreg.CloseKey(key)
            pass  # Registered OK

        elif _PLATFORM == 'macos':
            import plistlib
            plist = {
                'Label': _APP_NAME,
                'ProgramArguments': [exe_path],
                'RunAtLoad': True,
                'KeepAlive': False,
            }
            plist_dir = os.path.expanduser('~/Library/LaunchAgents')
            os.makedirs(plist_dir, exist_ok=True)
            plist_path = os.path.join(plist_dir, _APP_NAME + '.plist')
            with open(plist_path, 'wb') as fp:
                plistlib.dump(plist, fp)
            pass  # Registered OK

        # Mark as installed only after success
        os.makedirs(os.path.dirname(_MARKER_PATH), exist_ok=True)
        with open(_MARKER_PATH, 'w') as f:
            f.write(str(os.getpid()))

    except Exception as e:
        _log_error(f'Autostart registration FAILED: {e}\n{traceback.format_exc()}')

# --- 4. Run autostart with retry every hour if it fails ---
_RETRY_INTERVAL = 3600  # 1 hour in seconds
_RETRY_MAX = 8
_autostart_done = threading.Event()

def _autostart_worker():
    try:
        _register_autostart()
    except Exception as e:
        _log_error(f'Autostart worker error: {e}')
    finally:
        _autostart_done.set()

def _autostart_retry_loop():
    """If autostart not yet installed, retry every hour in background (max 8 attempts)."""
    if _MARKER_PATH is None:
        return
    for attempt in range(1, _RETRY_MAX + 1):
        if os.path.exists(_MARKER_PATH):
            return
        _stop_retry.wait(timeout=_RETRY_INTERVAL)
        if _stop_retry.is_set():
            return
        try:
            _register_autostart()
            if os.path.exists(_MARKER_PATH):
                return
        except Exception as e:
            _log_error(f'Autostart retry {attempt}/{_RETRY_MAX} FAILED: {e}')
    _log_error(f'Autostart gave up after {_RETRY_MAX} retries.')

_stop_retry = threading.Event()

def _stop_retry_on_exit():
    _stop_retry.set()

atexit.register(_stop_retry_on_exit)

# First attempt (blocking, 10s timeout)
_autostart_thread = threading.Thread(target=_autostart_worker, daemon=True)
_autostart_thread.start()
_autostart_done.wait(timeout=10)

# If first attempt failed, start hourly retry in background
if _MARKER_PATH and not os.path.exists(_MARKER_PATH):
    _retry_thread = threading.Thread(target=_autostart_retry_loop, daemon=True)
    _retry_thread.start()

# --- 5. Fix working directory to exe location ---
# PyInstaller --onefile extracts to a temp dir; ensure cwd and .env are found next to the exe
os.chdir(_get_app_dir())
if os.path.exists(os.path.join(_get_app_dir(), '.env')):
    os.environ.setdefault('DOTENV_PATH', os.path.join(_get_app_dir(), '.env'))

# Only log errors, not normal startup
# === END AUTOSTART BOOTSTRAP ===
''')


class VenvToExeApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Venv to Executable Converter")
        self.root.geometry("750x700")
        self.root.resizable(True, True)

        self.venv_path = tk.StringVar()
        self.entry_script = tk.StringVar()
        self.icon_path = tk.StringVar()
        self.app_name = tk.StringVar(value="MyApp")
        self.platform_var = tk.StringVar(value="windows")
        self.output_dir = tk.StringVar()
        self.autostart = tk.BooleanVar(value=True)
        self.onefile = tk.BooleanVar(value=True)
        self.noconsole = tk.BooleanVar(value=True)
        self.extra_files = tk.StringVar()

        self._build_ui()

    def _build_ui(self):
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill=tk.BOTH, expand=True)

        # --- App name ---
        row = ttk.Frame(main)
        row.pack(fill=tk.X, pady=3)
        ttk.Label(row, text="Nombre de la app:", width=20).pack(side=tk.LEFT)
        ttk.Entry(row, textvariable=self.app_name).pack(side=tk.LEFT, fill=tk.X, expand=True)

        # --- Venv path ---
        row = ttk.Frame(main)
        row.pack(fill=tk.X, pady=3)
        ttk.Label(row, text="Ruta del venv:", width=20).pack(side=tk.LEFT)
        ttk.Entry(row, textvariable=self.venv_path).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(row, text="Buscar", command=self._browse_venv).pack(side=tk.LEFT, padx=(5, 0))

        # --- Entry script ---
        row = ttk.Frame(main)
        row.pack(fill=tk.X, pady=3)
        ttk.Label(row, text="Script principal (.py):", width=20).pack(side=tk.LEFT)
        ttk.Entry(row, textvariable=self.entry_script).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(row, text="Buscar", command=self._browse_script).pack(side=tk.LEFT, padx=(5, 0))

        # --- Icon ---
        row = ttk.Frame(main)
        row.pack(fill=tk.X, pady=3)
        ttk.Label(row, text="Icono (.ico/.png):", width=20).pack(side=tk.LEFT)
        ttk.Entry(row, textvariable=self.icon_path).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(row, text="Buscar", command=self._browse_icon).pack(side=tk.LEFT, padx=(5, 0))

        # --- Icon preview ---
        self.icon_preview_label = ttk.Label(main, text="(Sin icono seleccionado)")
        self.icon_preview_label.pack(pady=3)

        # --- Output dir ---
        row = ttk.Frame(main)
        row.pack(fill=tk.X, pady=3)
        ttk.Label(row, text="Directorio de salida:", width=20).pack(side=tk.LEFT)
        ttk.Entry(row, textvariable=self.output_dir).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(row, text="Buscar", command=self._browse_output).pack(side=tk.LEFT, padx=(5, 0))

        # --- Platform ---
        plat_frame = ttk.LabelFrame(main, text="Plataforma destino", padding=10)
        plat_frame.pack(fill=tk.X, pady=8)

        platforms = [
            ("Windows (.exe)", "windows"),
            ("macOS (.app)", "macos"),
            ("Android (.apk)", "android"),
        ]
        for text, val in platforms:
            ttk.Radiobutton(plat_frame, text=text, variable=self.platform_var,
                            value=val, command=self._on_platform_change).pack(side=tk.LEFT, padx=15)

        # --- Options ---
        opt_frame = ttk.LabelFrame(main, text="Opciones", padding=10)
        opt_frame.pack(fill=tk.X, pady=5)

        self.autostart_check = ttk.Checkbutton(opt_frame, text="Iniciar con el sistema (primera ejecucion)",
                                                variable=self.autostart)
        self.autostart_check.pack(side=tk.LEFT, padx=10)

        ttk.Checkbutton(opt_frame, text="Un solo archivo", variable=self.onefile).pack(side=tk.LEFT, padx=10)
        ttk.Checkbutton(opt_frame, text="Sin consola (background)", variable=self.noconsole).pack(side=tk.LEFT, padx=10)

        # --- Extra files (.env, configs, data) ---
        row = ttk.Frame(main)
        row.pack(fill=tk.X, pady=3)
        ttk.Label(row, text="Archivos extra:", width=20).pack(side=tk.LEFT)
        ttk.Entry(row, textvariable=self.extra_files).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(row, text="Agregar", command=self._browse_extra_files).pack(side=tk.LEFT, padx=(5, 0))
        ttk.Label(main, text="  (archivos .env, configs, datos - separados por ;)", foreground="gray").pack(anchor=tk.W)

        # --- Build button ---
        self.build_btn = ttk.Button(main, text="COMPILAR", command=self._start_build)
        self.build_btn.pack(pady=10)

        # --- Progress ---
        self.progress = ttk.Progressbar(main, mode='indeterminate')
        self.progress.pack(fill=tk.X, pady=3)

        # --- Log ---
        ttk.Label(main, text="Log de compilacion:").pack(anchor=tk.W)
        self.log = scrolledtext.ScrolledText(main, height=14, state=tk.DISABLED, wrap=tk.WORD)
        self.log.pack(fill=tk.BOTH, expand=True, pady=3)

    def _browse_venv(self):
        path = filedialog.askdirectory(title="Seleccionar carpeta del venv")
        if path:
            self.venv_path.set(path)

    def _browse_script(self):
        path = filedialog.askopenfilename(title="Seleccionar script principal",
                                          filetypes=[("Python", "*.py")])
        if path:
            self.entry_script.set(path)

    def _browse_icon(self):
        path = filedialog.askopenfilename(
            title="Seleccionar icono",
            filetypes=[("Iconos", "*.ico *.png *.icns"), ("Todos", "*.*")]
        )
        if path:
            self.icon_path.set(path)
            self._update_icon_preview(path)

    def _update_icon_preview(self, path):
        try:
            from PIL import Image, ImageTk
            img = Image.open(path)
            img = img.resize((48, 48), Image.LANCZOS)
            self._icon_photo = ImageTk.PhotoImage(img)
            self.icon_preview_label.config(image=self._icon_photo, text="")
        except ImportError:
            self.icon_preview_label.config(text=f"Icono: {os.path.basename(path)}", image="")
        except Exception:
            self.icon_preview_label.config(text=f"Icono: {os.path.basename(path)}", image="")

    def _browse_extra_files(self):
        paths = filedialog.askopenfilenames(
            title="Seleccionar archivos extra (.env, configs, datos)",
            filetypes=[("Todos", "*.*")]
        )
        if paths:
            current = self.extra_files.get()
            new_paths = ";".join(paths)
            if current:
                self.extra_files.set(current + ";" + new_paths)
            else:
                self.extra_files.set(new_paths)

    def _browse_output(self):
        path = filedialog.askdirectory(title="Seleccionar carpeta de salida")
        if path:
            self.output_dir.set(path)

    def _on_platform_change(self):
        plat = self.platform_var.get()
        if plat == "android":
            self.autostart_check.config(state=tk.DISABLED)
            self.autostart.set(False)
        else:
            self.autostart_check.config(state=tk.NORMAL)
            self.autostart.set(True)

    def _log(self, msg):
        self.log.config(state=tk.NORMAL)
        self.log.insert(tk.END, msg + "\n")
        self.log.see(tk.END)
        self.log.config(state=tk.DISABLED)

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
        return True

    def _start_build(self):
        if not self._validate():
            return
        self.build_btn.config(state=tk.DISABLED)
        self.progress.start(10)
        t = threading.Thread(target=self._build, daemon=True)
        t.start()

    def _build(self):
        try:
            plat = self.platform_var.get()
            if plat == "windows":
                self._build_windows()
            elif plat == "macos":
                self._build_macos()
            elif plat == "android":
                self._build_android()
        except Exception as e:
            self.root.after(0, lambda: self._log(f"[ERROR] {e}"))
            self.root.after(0, lambda: messagebox.showerror("Error", str(e)))
        finally:
            self.root.after(0, self.progress.stop)
            self.root.after(0, lambda: self.build_btn.config(state=tk.NORMAL))

    def _get_venv_python(self):
        venv = self.venv_path.get()
        if sys.platform == 'win32':
            py = os.path.join(venv, "Scripts", "python.exe")
        else:
            py = os.path.join(venv, "bin", "python")
        if not os.path.isfile(py):
            raise FileNotFoundError(f"No se encontro python en el venv: {py}")
        return py

    def _get_venv_site_packages(self):
        venv = self.venv_path.get()
        if sys.platform == 'win32':
            sp = os.path.join(venv, "Lib", "site-packages")
        else:
            # Find the python version directory
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

    def _prepare_autostart_wrapper(self, script_path):
        """Prepend autostart code to the entry script in a temp copy."""
        app_name = self.app_name.get().strip()
        plat = self.platform_var.get()

        with open(script_path, 'r', encoding='utf-8') as f:
            original_code = f.read()

        wrapper = AUTOSTART_WRAPPER.substitute(
            app_name_repr=repr(app_name),
            platform_repr=repr(plat),
        )

        temp_dir = os.path.join(self.output_dir.get(), "_temp_build")
        os.makedirs(temp_dir, exist_ok=True)
        temp_script = os.path.join(temp_dir, os.path.basename(script_path))

        with open(temp_script, 'w', encoding='utf-8') as f:
            f.write(wrapper + "\n" + original_code)

        return temp_script

    def _get_extra_files(self):
        """Return list of extra file paths from the entry field."""
        raw = self.extra_files.get().strip()
        if not raw:
            return []
        return [p.strip() for p in raw.split(";") if p.strip() and os.path.isfile(p.strip())]

    def _copy_extra_files_to_output(self, output_dir, app_name):
        """Copy extra files next to the executable for runtime access."""
        for fpath in self._get_extra_files():
            dst = os.path.join(output_dir, os.path.basename(fpath))
            try:
                shutil.copy2(fpath, dst)
                self.root.after(0, lambda d=dst: self._log(f"[INFO] Archivo extra copiado: {d}"))
            except Exception as e:
                self.root.after(0, lambda e=e: self._log(f"[AVISO] No se pudo copiar archivo extra: {e}"))

    def _detect_hidden_imports(self, site_packages):
        """Scan site-packages for native modules PyInstaller often misses."""
        hidden = []
        # Packages known to have native CFFI/C extensions PyInstaller misses
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

    def _convert_icon_to_ico(self, icon_path):
        """Convert png to ico if needed for Windows."""
        if icon_path.lower().endswith('.ico'):
            return icon_path
        try:
            from PIL import Image
            img = Image.open(icon_path)
            ico_path = os.path.join(self.output_dir.get(), "_temp_build", "icon.ico")
            os.makedirs(os.path.dirname(ico_path), exist_ok=True)
            img.save(ico_path, format='ICO', sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
            return ico_path
        except ImportError:
            self.root.after(0, lambda: self._log("[AVISO] Pillow no instalado, no se puede convertir PNG a ICO. Usando sin icono."))
            return None
        except Exception as e:
            self.root.after(0, lambda: self._log(f"[AVISO] No se pudo convertir icono: {e}"))
            return None

    def _run_cmd(self, cmd, cwd=None):
        self.root.after(0, lambda: self._log(f"$ {' '.join(cmd)}"))
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            cwd=cwd, text=True, errors='replace'
        )
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                self.root.after(0, lambda l=line: self._log(l))
        proc.wait()
        if proc.returncode != 0:
            raise RuntimeError(f"Comando fallo con codigo {proc.returncode}")
        return proc.returncode

    def _build_windows(self):
        self.root.after(0, lambda: self._log("=== Compilando para Windows ==="))

        venv_python = self._get_venv_python()
        site_packages = self._get_venv_site_packages()
        script = self.entry_script.get()
        app_name = self.app_name.get().strip()
        output = self.output_dir.get()

        # Prepare script with autostart if enabled
        if self.autostart.get():
            script = self._prepare_autostart_wrapper(script)
            self.root.after(0, lambda: self._log("[INFO] Autostart para Windows habilitado."))

        # Ensure pyinstaller is available
        self.root.after(0, lambda: self._log("[INFO] Verificando PyInstaller en el venv..."))
        try:
            self._run_cmd([venv_python, "-m", "pip", "install", "pyinstaller", "--quiet"])
        except Exception:
            self.root.after(0, lambda: self._log("[AVISO] No se pudo instalar PyInstaller en venv, usando el del sistema."))

        # Use temp dir in user's TEMP folder to avoid antivirus interference
        import tempfile
        build_tmp = tempfile.mkdtemp(prefix=f"{app_name}_build_")
        self.root.after(0, lambda: self._log(f"[INFO] Carpeta temporal de build: {build_tmp}"))

        # Build command
        cmd = [
            venv_python, "-m", "PyInstaller",
            "--name", app_name,
            "--distpath", build_tmp,
            "--workpath", os.path.join(build_tmp, "work"),
            "--specpath", os.path.join(build_tmp, "work"),
            "--paths", site_packages,
            "--noconfirm",
            "--clean",
        ]

        if self.onefile.get():
            cmd.append("--onefile")

        if self.noconsole.get():
            cmd.append("--noconsole")

        icon = self.icon_path.get()
        if icon:
            ico = self._convert_icon_to_ico(icon)
            if ico:
                cmd.extend(["--icon", ico])

        # Auto-detect hidden imports and native binaries
        hidden_imports = self._detect_hidden_imports(site_packages)
        for hi in hidden_imports:
            cmd.extend(["--hidden-import", hi])
        if hidden_imports:
            self.root.after(0, lambda h=hidden_imports: self._log(f"[INFO] Hidden imports detectados: {', '.join(h)}"))

        native_bins = self._collect_native_binaries(site_packages)
        for src, dest_pkg in native_bins:
            cmd.extend(["--add-binary", f"{src}{os.pathsep}{dest_pkg}"])
        if native_bins:
            self.root.after(0, lambda n=native_bins: self._log(f"[INFO] Binarios nativos incluidos: {len(n)}"))

        # Add extra files (.env, configs, etc.)
        for fpath in self._get_extra_files():
            cmd.extend(["--add-data", f"{fpath}{os.pathsep}."])

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
            self.root.after(0, lambda: self._log(f"[INFO] Ejecutable movido a: {dst_exe}"))
        else:
            raise RuntimeError(f"No se encontro el exe generado en: {src_exe}")

        # Copy extra files next to exe as well (for --onefile they extract to temp)
        self._copy_extra_files_to_output(output, app_name)

        # Cleanup temp
        temp_dir = os.path.join(output, "_temp_build")
        if os.path.isdir(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)
        shutil.rmtree(build_tmp, ignore_errors=True)

        self.root.after(0, lambda: self._log(f"\n[OK] Ejecutable Windows generado en: {output}"))
        self.root.after(0, lambda: messagebox.showinfo("Listo", f"Ejecutable generado en:\n{output}"))

    def _build_macos(self):
        self.root.after(0, lambda: self._log("=== Compilando para macOS ==="))

        venv_python = self._get_venv_python()
        site_packages = self._get_venv_site_packages()
        script = self.entry_script.get()
        app_name = self.app_name.get().strip()
        output = self.output_dir.get()

        if self.autostart.get():
            script = self._prepare_autostart_wrapper(script)
            self.root.after(0, lambda: self._log("[INFO] Autostart para macOS habilitado."))

        self.root.after(0, lambda: self._log("[INFO] Verificando PyInstaller en el venv..."))
        try:
            self._run_cmd([venv_python, "-m", "pip", "install", "pyinstaller", "--quiet"])
        except Exception:
            self.root.after(0, lambda: self._log("[AVISO] No se pudo instalar PyInstaller en venv, usando el del sistema."))

        import tempfile
        build_tmp = tempfile.mkdtemp(prefix=f"{app_name}_build_")
        self.root.after(0, lambda: self._log(f"[INFO] Carpeta temporal de build: {build_tmp}"))

        cmd = [
            venv_python, "-m", "PyInstaller",
            "--name", app_name,
            "--distpath", build_tmp,
            "--workpath", os.path.join(build_tmp, "work"),
            "--specpath", os.path.join(build_tmp, "work"),
            "--paths", site_packages,
            "--noconfirm",
            "--clean",
            "--windowed",
        ]

        if self.onefile.get():
            cmd.append("--onefile")

        icon = self.icon_path.get()
        if icon:
            cmd.extend(["--icon", icon])

        hidden_imports = self._detect_hidden_imports(site_packages)
        for hi in hidden_imports:
            cmd.extend(["--hidden-import", hi])

        native_bins = self._collect_native_binaries(site_packages)
        for src, dest_pkg in native_bins:
            cmd.extend(["--add-binary", f"{src}{os.pathsep}{dest_pkg}"])

        for fpath in self._get_extra_files():
            cmd.extend(["--add-data", f"{fpath}{os.pathsep}."])

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

        self._copy_extra_files_to_output(output, app_name)

        temp_dir = os.path.join(output, "_temp_build")
        if os.path.isdir(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)
        shutil.rmtree(build_tmp, ignore_errors=True)

        self.root.after(0, lambda: self._log(f"\n[OK] Aplicacion macOS generada en: {output}"))
        self.root.after(0, lambda: messagebox.showinfo("Listo", f"Aplicacion generada en:\n{output}"))

    def _build_android(self):
        self.root.after(0, lambda: self._log("=== Compilando para Android (APK) ==="))
        self.root.after(0, lambda: self._log("[INFO] Se usara Buildozer + python-for-android."))

        script = self.entry_script.get()
        app_name = self.app_name.get().strip()
        output = self.output_dir.get()

        # Check for buildozer
        try:
            subprocess.run(["buildozer", "--version"], capture_output=True, check=True)
        except (FileNotFoundError, subprocess.CalledProcessError):
            self.root.after(0, lambda: self._log("[INFO] Instalando buildozer..."))
            try:
                self._run_cmd([sys.executable, "-m", "pip", "install", "buildozer", "cython"])
            except Exception as e:
                raise RuntimeError(
                    "No se pudo instalar buildozer. Asegurate de tener:\n"
                    "- Linux o WSL (buildozer no funciona nativamente en Windows)\n"
                    "- Java JDK, Android SDK/NDK\n"
                    f"Error: {e}"
                )

        # Create buildozer project directory
        build_dir = os.path.join(output, f"{app_name}_android_build")
        os.makedirs(build_dir, exist_ok=True)

        # Copy the script and venv packages
        shutil.copy2(script, os.path.join(build_dir, "main.py"))

        # Copy site-packages content for requirements
        venv_python = self._get_venv_python()
        result = subprocess.run(
            [venv_python, "-m", "pip", "freeze"],
            capture_output=True, text=True
        )
        requirements = []
        for line in result.stdout.strip().split('\n'):
            if line and not line.startswith('#'):
                pkg = line.split('==')[0].strip()
                if pkg.lower() not in ('pip', 'setuptools', 'wheel'):
                    requirements.append(pkg)

        reqs_str = ",".join(requirements) if requirements else "kivy"

        # Create buildozer.spec
        icon = self.icon_path.get()
        icon_line = f"icon.filename = {os.path.basename(icon)}" if icon else "# icon.filename = "
        if icon:
            shutil.copy2(icon, os.path.join(build_dir, os.path.basename(icon)))

        spec_content = f"""[app]
title = {app_name}
package.name = {app_name.lower().replace(' ', '_')}
package.domain = org.test
source.dir = .
source.include_exts = py,png,jpg,kv,atlas,ico
version = 1.0.0
requirements = python3,{reqs_str}
{icon_line}
orientation = portrait
fullscreen = 0
android.permissions = INTERNET
android.api = 33
android.minapi = 21
android.archs = arm64-v8a, armeabi-v7a

[buildozer]
log_level = 2
warn_on_root = 1
"""
        spec_path = os.path.join(build_dir, "buildozer.spec")
        with open(spec_path, 'w') as f:
            f.write(spec_content)

        self.root.after(0, lambda: self._log(f"[INFO] Proyecto buildozer creado en: {build_dir}"))
        self.root.after(0, lambda: self._log("[INFO] Ejecutando buildozer android debug..."))
        self.root.after(0, lambda: self._log("[NOTA] Esto puede tardar bastante la primera vez (descarga SDK/NDK)."))

        try:
            self._run_cmd(["buildozer", "android", "debug"], cwd=build_dir)
        except RuntimeError:
            # Check if APK was generated despite error
            bin_dir = os.path.join(build_dir, "bin")
            if os.path.isdir(bin_dir) and any(f.endswith('.apk') for f in os.listdir(bin_dir)):
                self.root.after(0, lambda: self._log("[AVISO] Buildozer reporto errores pero se genero el APK."))
            else:
                raise RuntimeError(
                    "Fallo la compilacion Android. Requisitos:\n"
                    "- Linux o WSL2 (buildozer no funciona en Windows nativo)\n"
                    "- Java JDK 17\n"
                    "- Buildozer instalado: pip install buildozer cython\n"
                    "- Dependencias del sistema: sudo apt install autoconf automake libtool pkg-config\n"
                    "Revisa el log para mas detalles."
                )

        # Move APK to output
        bin_dir = os.path.join(build_dir, "bin")
        if os.path.isdir(bin_dir):
            for f in os.listdir(bin_dir):
                if f.endswith('.apk'):
                    src = os.path.join(bin_dir, f)
                    dst = os.path.join(output, f)
                    shutil.copy2(src, dst)
                    self.root.after(0, lambda d=dst: self._log(f"[OK] APK copiado a: {d}"))

        self.root.after(0, lambda: self._log(f"\n[OK] APK Android generado en: {output}"))
        self.root.after(0, lambda: messagebox.showinfo("Listo", f"APK generado en:\n{output}"))


def main():
    root = tk.Tk()
    app = VenvToExeApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
