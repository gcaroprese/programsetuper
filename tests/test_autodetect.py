"""Tests for auto-detection helpers (no Tk dependency)."""

import os
import sys
import stat
import tempfile
import unittest
from pathlib import Path

# Add parent dir to path so we can import the module under test
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from venv_to_exe import (
    _detect_project_root,
    _detect_venv,
    _detect_icon,
    _detect_output_dir,
    _detect_extra_files,
    _detect_app_name,
    _parse_name_from_toml,
    _is_valid_venv,
    _venv_python_for,
)


def _make_fake_venv(venv_dir):
    """Create a minimal fake venv with pyvenv.cfg and a python binary."""
    os.makedirs(venv_dir, exist_ok=True)
    cfg = os.path.join(venv_dir, 'pyvenv.cfg')
    with open(cfg, 'w') as f:
        f.write(f'version = {sys.version_info.major}.{sys.version_info.minor}.0\n')
    if sys.platform == 'win32':
        py_dir = os.path.join(venv_dir, 'Scripts')
        os.makedirs(py_dir, exist_ok=True)
        py = os.path.join(py_dir, 'python.exe')
    else:
        py_dir = os.path.join(venv_dir, 'bin')
        os.makedirs(py_dir, exist_ok=True)
        py = os.path.join(py_dir, 'python')
    with open(py, 'w') as f:
        f.write('')
    if sys.platform != 'win32':
        os.chmod(py, stat.S_IRWXU)
    return venv_dir


class TestProjectRoot(unittest.TestCase):
    def test_git_marker_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, 'project')
            src = os.path.join(root, 'src', 'app')
            os.makedirs(src)
            os.makedirs(os.path.join(root, '.git'))
            result = _detect_project_root(src)
            self.assertEqual(os.path.normcase(result), os.path.normcase(root))

    def test_pyproject_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, 'proj')
            sub = os.path.join(root, 'pkg')
            os.makedirs(sub)
            with open(os.path.join(root, 'pyproject.toml'), 'w') as f:
                f.write('')
            result = _detect_project_root(sub)
            self.assertEqual(os.path.normcase(result), os.path.normcase(root))

    def test_no_markers_returns_script_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            sub = os.path.join(tmp, 'a', 'b')
            os.makedirs(sub)
            result = _detect_project_root(sub)
            self.assertEqual(os.path.normcase(result), os.path.normcase(sub))

    def test_max_depth(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Place .git 6 levels up -- should NOT be found (max 4 parents)
            deep = os.path.join(tmp, 'a', 'b', 'c', 'd', 'e', 'f')
            os.makedirs(deep)
            os.makedirs(os.path.join(tmp, '.git'))
            result = _detect_project_root(deep)
            self.assertEqual(os.path.normcase(result), os.path.normcase(deep))


class TestDetectVenv(unittest.TestCase):
    def test_dotenv_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            venv = _make_fake_venv(os.path.join(tmp, '.venv'))
            best, alts = _detect_venv(tmp, tmp)
            self.assertIsNotNone(best)
            self.assertEqual(os.path.normcase(best), os.path.normcase(venv))

    def test_venv_name_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            venv = _make_fake_venv(os.path.join(tmp, 'venv'))
            best, _ = _detect_venv(tmp, tmp)
            self.assertEqual(os.path.normcase(best), os.path.normcase(venv))

    def test_dir_without_pyvenv_cfg_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = os.path.join(tmp, 'venv')
            os.makedirs(bad)
            best, _ = _detect_venv(tmp, tmp)
            self.assertIsNone(best)

    def test_dotenv_file_not_returned_as_venv(self):
        with tempfile.TemporaryDirectory() as tmp:
            # .env is a FILE, not a dir
            with open(os.path.join(tmp, '.env'), 'w') as f:
                f.write('SECRET=abc')
            best, _ = _detect_venv(tmp, tmp)
            self.assertIsNone(best)

    def test_prefers_dotvenv_over_venv(self):
        with tempfile.TemporaryDirectory() as tmp:
            _make_fake_venv(os.path.join(tmp, 'venv'))
            _make_fake_venv(os.path.join(tmp, '.venv'))
            best, alts = _detect_venv(tmp, tmp)
            self.assertEqual(os.path.basename(best), '.venv')
            self.assertTrue(len(alts) >= 1)


class TestDetectIcon(unittest.TestCase):
    def test_icon_in_assets_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            assets = os.path.join(tmp, 'assets')
            os.makedirs(assets)
            icon_path = os.path.join(assets, 'icon.png')
            # Create a minimal 1-byte file
            with open(icon_path, 'wb') as f:
                f.write(b'\x00')
            result = _detect_icon(tmp, tmp, 'MyApp', 'main')
            self.assertIsNotNone(result)
            self.assertEqual(os.path.normcase(result), os.path.normcase(icon_path))

    def test_no_icon_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = _detect_icon(tmp, tmp, 'MyApp', 'main')
            self.assertIsNone(result)

    def test_random_png_below_threshold(self):
        with tempfile.TemporaryDirectory() as tmp:
            # A random png with no icon/logo/app in the name and no stem match
            with open(os.path.join(tmp, 'screenshot.png'), 'wb') as f:
                f.write(b'\x00')
            result = _detect_icon(tmp, tmp, 'MyApp', 'main')
            # screenshot.png gets only +10 for .png -- below threshold of 30
            self.assertIsNone(result)

    def test_matching_app_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            icon_path = os.path.join(tmp, 'my_app.ico')
            with open(icon_path, 'wb') as f:
                f.write(b'\x00')
            result = _detect_icon(tmp, tmp, 'My App', 'main')
            self.assertIsNotNone(result)


class TestDetectOutputDir(unittest.TestCase):
    def test_proposes_dist(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = _detect_output_dir(tmp)
            self.assertEqual(result, os.path.join(tmp, 'dist'))


class TestDetectExtraFiles(unittest.TestCase):
    def test_env_included_with_load_dotenv(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = os.path.join(tmp, '.env')
            with open(env_path, 'w') as f:
                f.write('SECRET=abc')
            source = "from dotenv import load_dotenv\nload_dotenv()\n"
            result = _detect_extra_files(tmp, tmp, source)
            self.assertEqual(len(result), 1)
            self.assertEqual(os.path.normcase(result[0]), os.path.normcase(env_path))

    def test_env_excluded_without_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, '.env'), 'w') as f:
                f.write('SECRET=abc')
            source = "print('hello')\n"
            result = _detect_extra_files(tmp, tmp, source)
            self.assertEqual(len(result), 0)

    def test_config_json_included_when_referenced(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, 'config.json')
            with open(cfg_path, 'w') as f:
                f.write('{}')
            source = "open('config.json')\n"
            result = _detect_extra_files(tmp, tmp, source)
            self.assertEqual(len(result), 1)

    def test_unreferenced_config_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, 'config.json'), 'w') as f:
                f.write('{}')
            source = "print('no config here')\n"
            result = _detect_extra_files(tmp, tmp, source)
            self.assertEqual(len(result), 0)


class TestDetectAppName(unittest.TestCase):
    def test_from_pyproject_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, 'pyproject.toml'), 'w') as f:
                f.write('[project]\nname = "cool-tool"\n')
            script = os.path.join(tmp, 'main.py')
            with open(script, 'w') as f:
                f.write('')
            result = _detect_app_name(script, tmp)
            self.assertEqual(result, 'cool-tool')

    def test_from_pyproject_poetry(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, 'pyproject.toml'), 'w') as f:
                f.write('[tool.poetry]\nname = "poetry-app"\n')
            script = os.path.join(tmp, 'main.py')
            with open(script, 'w') as f:
                f.write('')
            result = _detect_app_name(script, tmp)
            self.assertEqual(result, 'poetry-app')

    def test_malformed_toml_falls_back_to_stem(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, 'pyproject.toml'), 'w') as f:
                f.write('this is not valid toml {{{\n')
            script = os.path.join(tmp, 'my_cool_app.py')
            with open(script, 'w') as f:
                f.write('')
            result = _detect_app_name(script, tmp)
            self.assertEqual(result, 'My Cool App')

    def test_fallback_to_script_stem(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = os.path.join(tmp, 'video_maker.py')
            with open(script, 'w') as f:
                f.write('')
            result = _detect_app_name(script, tmp)
            self.assertEqual(result, 'Video Maker')

    def test_from_setup_cfg(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, 'setup.cfg'), 'w') as f:
                f.write('[metadata]\nname = setup-app\n')
            script = os.path.join(tmp, 'main.py')
            with open(script, 'w') as f:
                f.write('')
            result = _detect_app_name(script, tmp)
            self.assertEqual(result, 'setup-app')


class TestParseNameFromToml(unittest.TestCase):
    def test_project_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, 'pyproject.toml')
            with open(p, 'w') as f:
                f.write('[project]\nname = "my-proj"\nversion = "1.0"\n')
            self.assertEqual(_parse_name_from_toml(p), 'my-proj')

    def test_no_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, 'pyproject.toml')
            with open(p, 'w') as f:
                f.write('[build-system]\nrequires = ["setuptools"]\n')
            self.assertIsNone(_parse_name_from_toml(p))


if __name__ == '__main__':
    unittest.main()
