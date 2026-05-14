"""
Venv-to-Executable Converter
Convierte un entorno virtual Python en un unico archivo ejecutable.
Soporta Windows (.exe), macOS (.app) y Android (.apk).
"""

import os
import sys
import json
import shutil
import subprocess
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
from pathlib import Path

# --- Autostart helpers ---

AUTOSTART_SCRIPT_TEMPLATE_WIN = r'''
import os, sys, winreg

def register_autostart(app_name, exe_path):
    key = winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        r"Software\Microsoft\Windows\CurrentVersion\Run",
        0, winreg.KEY_SET_VALUE
    )
    winreg.SetValueEx(key, app_name, 0, winreg.REG_SZ, exe_path)
    winreg.CloseKey(key)

def is_first_run(marker_path):
    if not os.path.exists(marker_path):
        os.makedirs(os.path.dirname(marker_path), exist_ok=True)
        with open(marker_path, 'w') as f:
            f.write('installed')
        return True
    return False

if __name__ == '__main__':
    pass
'''

AUTOSTART_SCRIPT_TEMPLATE_MAC = '''
import os, plistlib

def register_autostart(app_name, exe_path):
    plist = {
        'Label': app_name,
        'ProgramArguments': [exe_path],
        'RunAtLoad': True,
    }
    plist_path = os.path.expanduser(f'~/Library/LaunchAgents/{app_name}.plist')
    with open(plist_path, 'wb') as f:
        plistlib.dump(plist, f)

def is_first_run(marker_path):
    if not os.path.exists(marker_path):
        os.makedirs(os.path.dirname(marker_path), exist_ok=True)
        with open(marker_path, 'w') as f:
            f.write('installed')
        return True
    return False

if __name__ == '__main__':
    pass
'''

AUTOSTART_WRAPPER = '''
import os, sys

_ORIGINAL_MAIN = {main_module!r}
_APP_NAME = {app_name!r}
_PLATFORM = {platform!r}

def _setup_autostart():
    exe_path = sys.executable if getattr(sys, 'frozen', False) else os.path.abspath(__file__)
    if _PLATFORM == 'windows':
        import winreg
        marker = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), _APP_NAME, '.installed')
        if not os.path.exists(marker):
            os.makedirs(os.path.dirname(marker), exist_ok=True)
            with open(marker, 'w') as f:
                f.write('1')
            try:
                key = winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\\Microsoft\\Windows\\CurrentVersion\\Run",
                    0, winreg.KEY_SET_VALUE
                )
                winreg.SetValueEx(key, _APP_NAME, 0, winreg.REG_SZ, exe_path)
                winreg.CloseKey(key)
            except Exception:
                pass
    elif _PLATFORM == 'macos':
        import plistlib
        marker = os.path.expanduser(f'~/.{_APP_NAME}_installed')
        if not os.path.exists(marker):
            with open(marker, 'w') as f:
                f.write('1')
            plist = {{
                'Label': _APP_NAME,
                'ProgramArguments': [exe_path],
                'RunAtLoad': True,
            }}
            plist_path = os.path.expanduser(f'~/Library/LaunchAgents/{_APP_NAME}.plist')
            try:
                with open(plist_path, 'wb') as fp:
                    plistlib.dump(plist, fp)
            except Exception:
                pass

_setup_autostart()
'''


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

        wrapper = AUTOSTART_WRAPPER.format(
            main_module=os.path.basename(script_path),
            app_name=app_name,
            platform=plat,
        )

        temp_dir = os.path.join(self.output_dir.get(), "_temp_build")
        os.makedirs(temp_dir, exist_ok=True)
        temp_script = os.path.join(temp_dir, os.path.basename(script_path))

        with open(temp_script, 'w', encoding='utf-8') as f:
            f.write(wrapper + "\n" + original_code)

        return temp_script

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

        # Build command
        cmd = [
            venv_python, "-m", "PyInstaller",
            "--name", app_name,
            "--distpath", output,
            "--workpath", os.path.join(output, "_build_work"),
            "--specpath", os.path.join(output, "_build_work"),
            "--paths", site_packages,
            "--noconfirm",
            "--clean",
        ]

        if self.onefile.get():
            cmd.append("--onefile")

        icon = self.icon_path.get()
        if icon:
            ico = self._convert_icon_to_ico(icon)
            if ico:
                cmd.extend(["--icon", ico])

        cmd.append(script)

        self._run_cmd(cmd)

        # Cleanup temp
        temp_dir = os.path.join(output, "_temp_build")
        if os.path.isdir(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)
        work_dir = os.path.join(output, "_build_work")
        if os.path.isdir(work_dir):
            shutil.rmtree(work_dir, ignore_errors=True)

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

        cmd = [
            venv_python, "-m", "PyInstaller",
            "--name", app_name,
            "--distpath", output,
            "--workpath", os.path.join(output, "_build_work"),
            "--specpath", os.path.join(output, "_build_work"),
            "--paths", site_packages,
            "--noconfirm",
            "--clean",
            "--windowed",
        ]

        if self.onefile.get():
            cmd.append("--onefile")

        icon = self.icon_path.get()
        if icon:
            # macOS uses .icns format
            if icon.lower().endswith('.icns'):
                cmd.extend(["--icon", icon])
            elif icon.lower().endswith('.png'):
                cmd.extend(["--icon", icon])  # PyInstaller can handle png on mac
            else:
                cmd.extend(["--icon", icon])

        cmd.append(script)

        self._run_cmd(cmd)

        temp_dir = os.path.join(output, "_temp_build")
        if os.path.isdir(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)
        work_dir = os.path.join(output, "_build_work")
        if os.path.isdir(work_dir):
            shutil.rmtree(work_dir, ignore_errors=True)

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
