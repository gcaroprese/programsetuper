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

    def _run_cmd(self, cmd, cwd=None, env=None):
        self.root.after(0, lambda: self._log(f"$ {' '.join(cmd)}"))
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            cwd=cwd, env=env, text=True, errors='replace'
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

    def _find_java_home(self):
        """Find a suitable JDK (17+) for Android builds."""
        # Check JAVA_HOME first
        jh = os.environ.get('JAVA_HOME')
        if jh and os.path.isdir(jh):
            return jh
        # Search common locations on Windows
        if sys.platform == 'win32':
            search_dirs = [
                os.path.join(os.environ.get('ProgramFiles', 'C:\\Program Files'), 'Eclipse Adoptium'),
                os.path.join(os.environ.get('ProgramFiles', 'C:\\Program Files'), 'Java'),
                os.path.join(os.environ.get('ProgramFiles', 'C:\\Program Files'), 'Microsoft'),
            ]
            for base in search_dirs:
                if not os.path.isdir(base):
                    continue
                for d in sorted(os.listdir(base), reverse=True):
                    candidate = os.path.join(base, d)
                    javac = os.path.join(candidate, 'bin', 'javac.exe')
                    if os.path.isfile(javac):
                        return candidate
        # Fallback: use 'where javac' / 'which javac'
        try:
            r = subprocess.run(['where' if sys.platform == 'win32' else 'which', 'javac'],
                                capture_output=True, text=True)
            if r.returncode == 0:
                javac_path = r.stdout.strip().split('\n')[0].strip()
                return str(Path(javac_path).parent.parent)
        except Exception:
            pass
        return None

    def _setup_android_sdk(self, sdk_root):
        """Download and set up Android SDK command-line tools."""
        import urllib.request
        import zipfile

        os.makedirs(sdk_root, exist_ok=True)
        cmdline_dir = os.path.join(sdk_root, 'cmdline-tools', 'latest')

        if os.path.isdir(cmdline_dir) and os.path.isfile(os.path.join(cmdline_dir, 'bin', 'sdkmanager.bat' if sys.platform == 'win32' else 'sdkmanager')):
            return  # Already installed

        self.root.after(0, lambda: self._log("[INFO] Descargando Android SDK command-line tools..."))

        if sys.platform == 'win32':
            url = "https://dl.google.com/android/repository/commandlinetools-win-11076708_latest.zip"
        elif sys.platform == 'darwin':
            url = "https://dl.google.com/android/repository/commandlinetools-mac-11076708_latest.zip"
        else:
            url = "https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip"

        zip_path = os.path.join(sdk_root, 'cmdline-tools.zip')
        urllib.request.urlretrieve(url, zip_path)

        self.root.after(0, lambda: self._log("[INFO] Extrayendo SDK tools..."))
        with zipfile.ZipFile(zip_path, 'r') as zf:
            zf.extractall(os.path.join(sdk_root, 'cmdline-tools'))

        # The zip extracts to cmdline-tools/cmdline-tools, rename to 'latest'
        extracted = os.path.join(sdk_root, 'cmdline-tools', 'cmdline-tools')
        if os.path.isdir(extracted):
            if os.path.isdir(cmdline_dir):
                shutil.rmtree(cmdline_dir)
            os.rename(extracted, cmdline_dir)

        os.remove(zip_path)

    def _accept_licenses_and_install(self, sdk_root, java_home):
        """Install required SDK packages and accept licenses."""
        sdkmanager = os.path.join(sdk_root, 'cmdline-tools', 'latest', 'bin',
                                   'sdkmanager.bat' if sys.platform == 'win32' else 'sdkmanager')

        env = os.environ.copy()
        env['JAVA_HOME'] = java_home
        env['ANDROID_SDK_ROOT'] = sdk_root

        # Accept licenses
        self.root.after(0, lambda: self._log("[INFO] Aceptando licencias de Android SDK..."))
        proc = subprocess.Popen(
            [sdkmanager, '--licenses', f'--sdk_root={sdk_root}'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors='replace', env=env
        )
        # Send 'y' repeatedly to accept all licenses
        try:
            out, _ = proc.communicate(input='y\n' * 20, timeout=120)
            for line in out.split('\n'):
                if line.strip():
                    self.root.after(0, lambda l=line.strip(): self._log(l))
        except subprocess.TimeoutExpired:
            proc.kill()

        # Install platform, build-tools, and platform-tools
        packages = ['platform-tools', 'platforms;android-35', 'build-tools;35.0.1']
        for pkg in packages:
            self.root.after(0, lambda p=pkg: self._log(f"[INFO] Instalando {p}..."))
            self._run_cmd([sdkmanager, pkg, f'--sdk_root={sdk_root}'], cwd=sdk_root, env=env)

    def _build_android(self):
        self.root.after(0, lambda: self._log("=== Compilando para Android (APK) ==="))
        self.root.after(0, lambda: self._log("[INFO] Usando Gradle + Chaquopy (nativo, sin WSL)."))

        script = self.entry_script.get()
        app_name = self.app_name.get().strip()
        output = self.output_dir.get()
        pkg_name = app_name.lower().replace(' ', '_').replace('-', '_')
        site_packages = self._get_venv_site_packages()

        # 1. Find JDK
        java_home = self._find_java_home()
        if not java_home:
            raise RuntimeError("No se encontro JDK. Instala JDK 17+ desde https://adoptium.net/")
        self.root.after(0, lambda: self._log(f"[INFO] JAVA_HOME: {java_home}"))

        # 2. Setup Android SDK
        sdk_root = os.path.join(os.path.expanduser('~'), '.android-sdk')
        self._setup_android_sdk(sdk_root)
        self._accept_licenses_and_install(sdk_root, java_home)
        self.root.after(0, lambda: self._log(f"[INFO] ANDROID_SDK_ROOT: {sdk_root}"))

        # 3. Get requirements from venv
        venv_python = self._get_venv_python()
        result = subprocess.run(
            [venv_python, "-m", "pip", "freeze"],
            capture_output=True, text=True
        )
        # --- Analyze package compatibility ---
        skip_pkgs = {
            'pip', 'setuptools', 'wheel', 'pyinstaller', 'pyinstaller-hooks-contrib',
            'pywin32', 'pywin32-ctypes', 'pefile', 'altgraph', 'colorama', 'pyreadline3',
        }
        chaquopy_native = {
            'cffi', 'coincurve', 'bitarray', 'cytoolz', 'greenlet', 'regex',
            'pycryptodome', 'pycryptodomex', 'aiohttp', 'frozenlist', 'multidict',
            'yarl', 'pynacl', 'numpy', 'cryptography', 'bcrypt', 'pillow',
            'lxml', 'ujson', 'pyyaml', 'markupsafe', 'sqlalchemy', 'websockets',
            'tgcrypto', 'cbor2',
        }

        compatible = []
        incompatible = []
        pip_reqs = []

        for line in result.stdout.strip().split('\n'):
            if line and not line.startswith('#') and not line.startswith('-'):
                pkg = line.strip()
                name = pkg.split('==')[0].split('>=')[0].split('<=')[0].strip().lower()
                name_norm = name.replace('-', '_')
                if name_norm in skip_pkgs or name in skip_pkgs:
                    continue
                compatible.append(name)
                if name_norm in chaquopy_native or name in chaquopy_native:
                    pip_reqs.append(f'            install "{name}"')
                else:
                    pip_reqs.append(f'            install "{pkg}"')

        pip_block = '\n'.join(pip_reqs) if pip_reqs else '            install "kivy"'

        # --- Pre-build compatibility check: do a dry-run pip install ---
        self.root.after(0, lambda: self._log("[INFO] Verificando compatibilidad de paquetes con Android..."))

        # Try installing requirements in a temp venv to detect failures
        import tempfile
        test_dir = tempfile.mkdtemp(prefix=f"{app_name}_compat_")
        test_script = os.path.join(test_dir, "test_reqs.py")
        with open(test_script, 'w') as f:
            f.write("# compatibility test\n")

        # We can't fully test without building, so instead we classify and warn
        # Check which packages have .pyd/.so (C extensions) in site-packages
        c_ext_pkgs = []
        for name in compatible:
            name_norm = name.replace('-', '_')
            pkg_dir = os.path.join(site_packages, name_norm)
            if not os.path.isdir(pkg_dir):
                pkg_dir = os.path.join(site_packages, name)
            if os.path.isdir(pkg_dir):
                has_native = any(
                    f.endswith(('.pyd', '.so', '.dll'))
                    for f in os.listdir(pkg_dir)
                )
                if has_native and name_norm not in chaquopy_native and name not in chaquopy_native:
                    incompatible.append(name)

        shutil.rmtree(test_dir, ignore_errors=True)

        # --- Show compatibility report and ask for confirmation ---
        if incompatible:
            report = (
                f"Se detectaron {len(incompatible)} paquete(s) con extensiones nativas "
                f"que podrian no funcionar en Android:\n\n"
            )
            for pkg in incompatible:
                report += f"  - {pkg}\n"
            report += (
                f"\n{len(compatible) - len(incompatible)} de {len(compatible)} paquetes son compatibles.\n\n"
                "Las funciones que dependan de estos paquetes no van a funcionar en la app Android.\n"
                "El resto de la app funcionara normalmente.\n\n"
                "Deseas continuar con la compilacion?"
            )

            proceed = threading.Event()
            user_choice = [False]

            def _ask():
                user_choice[0] = messagebox.askyesno(
                    "Compatibilidad Android",
                    report
                )
                proceed.set()

            self.root.after(0, _ask)
            proceed.wait()

            if not user_choice[0]:
                self.root.after(0, lambda: self._log("[INFO] Compilacion cancelada por el usuario."))
                return

            # Remove incompatible packages from requirements
            pip_reqs_filtered = []
            for req_line in pip_reqs:
                pkg_in_line = req_line.strip().replace('install "', '').rstrip('"')
                pkg_name_check = pkg_in_line.split('==')[0].split('>=')[0].strip().lower().replace('-', '_')
                if pkg_name_check not in [p.replace('-', '_') for p in incompatible]:
                    pip_reqs_filtered.append(req_line)
                else:
                    self.root.after(0, lambda p=pkg_in_line: self._log(f"[AVISO] Omitido (sin soporte Android): {p}"))
            pip_reqs = pip_reqs_filtered
            pip_block = '\n'.join(pip_reqs) if pip_reqs else '            install "kivy"'
        else:
            self.root.after(0, lambda: self._log(
                f"[OK] Todos los {len(compatible)} paquetes son compatibles con Android."))

        # 4. Create Gradle + Chaquopy project
        build_dir = os.path.join(output, f"{app_name}_android_build")
        if os.path.isdir(build_dir):
            shutil.rmtree(build_dir)

        app_dir = os.path.join(build_dir, 'app')
        src_dir = os.path.join(app_dir, 'src', 'main')
        python_dir = os.path.join(src_dir, 'python')
        res_dir = os.path.join(src_dir, 'res')
        os.makedirs(python_dir, exist_ok=True)
        os.makedirs(os.path.join(res_dir, 'mipmap-xxxhdpi'), exist_ok=True)
        os.makedirs(os.path.join(res_dir, 'values'), exist_ok=True)

        # Copy Python script
        shutil.copy2(script, os.path.join(python_dir, 'main.py'))

        # Copy extra files
        for fpath in self._get_extra_files():
            shutil.copy2(fpath, os.path.join(python_dir, os.path.basename(fpath)))

        # Copy icon
        icon = self.icon_path.get()
        if icon and os.path.isfile(icon):
            try:
                from PIL import Image
                img = Image.open(icon)
                for size, folder in [(192, 'mipmap-xxxhdpi'), (144, 'mipmap-xxhdpi'),
                                      (96, 'mipmap-xhdpi'), (72, 'mipmap-hdpi'), (48, 'mipmap-mdpi')]:
                    d = os.path.join(res_dir, folder)
                    os.makedirs(d, exist_ok=True)
                    img.resize((size, size), Image.LANCZOS).save(os.path.join(d, 'ic_launcher.png'))
            except Exception:
                shutil.copy2(icon, os.path.join(res_dir, 'mipmap-xxxhdpi', 'ic_launcher.png'))

        # settings.gradle
        with open(os.path.join(build_dir, 'settings.gradle'), 'w', newline='\n') as f:
            f.write(f"""pluginManagement {{
    repositories {{
        google()
        mavenCentral()
        maven {{ url "https://chaquo.com/maven" }}
    }}
}}
dependencyResolutionManagement {{
    repositories {{
        google()
        mavenCentral()
        maven {{ url "https://chaquo.com/maven" }}
    }}
}}
rootProject.name = "{app_name}"
include ':app'
""")

        # Top-level build.gradle
        with open(os.path.join(build_dir, 'build.gradle'), 'w', newline='\n') as f:
            f.write("""plugins {
    id 'com.android.application' version '8.7.0' apply false
    id 'com.chaquo.python' version '16.1.0' apply false
}
""")

        # gradle.properties
        with open(os.path.join(build_dir, 'gradle.properties'), 'w', newline='\n') as f:
            f.write("""android.useAndroidX=true
org.gradle.jvmargs=-Xmx2048m
""")

        # app/build.gradle
        with open(os.path.join(app_dir, 'build.gradle'), 'w', newline='\n') as f:
            f.write(f"""plugins {{
    id 'com.android.application'
    id 'com.chaquo.python'
}}

android {{
    namespace 'org.test.{pkg_name}'
    compileSdk 35

    defaultConfig {{
        applicationId "org.test.{pkg_name}"
        minSdk 24
        targetSdk 35
        versionCode 1
        versionName "1.0"

        ndk {{
            abiFilters "arm64-v8a", "armeabi-v7a", "x86_64"
        }}

        python {{
            version "3.11"
            pip {{
{pip_block}
            }}
        }}
    }}

    buildTypes {{
        release {{
            minifyEnabled false
        }}
    }}
}}
""")

        # AndroidManifest.xml
        os.makedirs(os.path.dirname(os.path.join(src_dir, 'AndroidManifest.xml')), exist_ok=True)
        with open(os.path.join(src_dir, 'AndroidManifest.xml'), 'w', newline='\n') as f:
            f.write(f"""<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android">
    <uses-permission android:name="android.permission.INTERNET" />
    <application
        android:allowBackup="true"
        android:icon="@mipmap/ic_launcher"
        android:label="{app_name}"
        android:theme="@style/AppTheme">
        <activity
            android:name="com.chaquo.python.android.PyApplication"
            android:exported="true">
            <intent-filter>
                <action android:name="android.intent.action.MAIN" />
                <category android:name="android.intent.category.LAUNCHER" />
            </intent-filter>
        </activity>
    </application>
</manifest>
""")

        # styles.xml
        with open(os.path.join(res_dir, 'values', 'styles.xml'), 'w', newline='\n') as f:
            f.write("""<?xml version="1.0" encoding="utf-8"?>
<resources>
    <style name="AppTheme" parent="android:Theme.Material.Light.NoActionBar" />
</resources>
""")

        # Gradle wrapper
        wrapper_dir = os.path.join(build_dir, 'gradle', 'wrapper')
        os.makedirs(wrapper_dir, exist_ok=True)
        with open(os.path.join(wrapper_dir, 'gradle-wrapper.properties'), 'w', newline='\n') as f:
            f.write("""distributionBase=GRADLE_USER_HOME
distributionPath=wrapper/dists
distributionUrl=https\\://services.gradle.org/distributions/gradle-8.9-bin.zip
zipStoreBase=GRADLE_USER_HOME
zipStorePath=wrapper/dists
""")

        # Download gradle wrapper jar and script
        self.root.after(0, lambda: self._log("[INFO] Configurando Gradle wrapper..."))
        gradlew = os.path.join(build_dir, 'gradlew.bat' if sys.platform == 'win32' else 'gradlew')
        if sys.platform == 'win32':
            with open(gradlew, 'w', newline='\r\n') as f:
                f.write("""@rem Gradle wrapper
@echo off
set DIRNAME=%~dp0
set JAVA_EXE=java.exe
if defined JAVA_HOME set JAVA_EXE=%JAVA_HOME%\\bin\\java.exe
set CLASSPATH=%DIRNAME%\\gradle\\wrapper\\gradle-wrapper.jar
"%JAVA_EXE%" %JAVA_OPTS% -classpath "%CLASSPATH%" org.gradle.wrapper.GradleWrapperMain %*
""")
        else:
            with open(gradlew, 'w', newline='\n') as f:
                f.write("""#!/bin/sh
DIRNAME=$(cd "$(dirname "$0")" && pwd)
JAVA_EXE=java
[ -n "$JAVA_HOME" ] && JAVA_EXE="$JAVA_HOME/bin/java"
CLASSPATH="$DIRNAME/gradle/wrapper/gradle-wrapper.jar"
exec "$JAVA_EXE" $JAVA_OPTS -classpath "$CLASSPATH" org.gradle.wrapper.GradleWrapperMain "$@"
""")
            os.chmod(gradlew, 0o755)

        # Download gradle-wrapper.jar
        wrapper_jar = os.path.join(wrapper_dir, 'gradle-wrapper.jar')
        if not os.path.isfile(wrapper_jar):
            import urllib.request
            self.root.after(0, lambda: self._log("[INFO] Descargando gradle-wrapper.jar..."))
            urllib.request.urlretrieve(
                "https://raw.githubusercontent.com/gradle/gradle/v8.9.0/gradle/wrapper/gradle-wrapper.jar",
                wrapper_jar
            )

        # 5. Build APK
        self.root.after(0, lambda: self._log("[INFO] Compilando APK con Gradle + Chaquopy..."))
        self.root.after(0, lambda: self._log("[NOTA] La primera vez descarga dependencias (~500MB)."))

        env = os.environ.copy()
        env['JAVA_HOME'] = java_home
        env['ANDROID_SDK_ROOT'] = sdk_root
        env['ANDROID_HOME'] = sdk_root

        self._run_cmd([gradlew, 'assembleDebug', '--stacktrace'], cwd=build_dir, env=env)

        # 6. Find and copy APK
        apk_found = False
        apk_search = os.path.join(app_dir, 'build', 'outputs', 'apk', 'debug')
        if os.path.isdir(apk_search):
            for f in os.listdir(apk_search):
                if f.endswith('.apk'):
                    src = os.path.join(apk_search, f)
                    dst = os.path.join(output, f"{app_name}.apk")
                    shutil.copy2(src, dst)
                    self.root.after(0, lambda d=dst: self._log(f"[OK] APK copiado a: {d}"))
                    apk_found = True
                    break

        if apk_found:
            self.root.after(0, lambda: self._log(f"\n[OK] APK Android generado en: {output}"))
            self.root.after(0, lambda: messagebox.showinfo("Listo", f"APK generado en:\n{output}"))
        else:
            raise RuntimeError("No se encontro el APK generado. Revisa el log para detalles.")


def main():
    root = tk.Tk()
    app = VenvToExeApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
