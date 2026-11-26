# Ultra-slim PyInstaller spec for Prism Browser Core Sidecar (prism-backend).
#
# Bundles ONLY the headless FastAPI/Mihomo/GeoIP/Isolation sidecar + static WebUI
# (~25 MB compressed), with ZERO camoufox, playwright, fpgen, or pywebview.
#
# Built via:
#   pyinstaller packaging/prism-backend.spec --distpath build --workpath build/pyinstaller --noconfirm --clean
#
# Produces build/prism-backend/prism-backend(.exe) which electron-builder copies
# into resources/backend/.

import os
import sys
from importlib.metadata import version
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules, copy_metadata

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
sys.path.insert(0, SPECPATH)
from build_version import write_runtime_version_hook

version_hook = Path(ROOT) / "build" / "camoufox_pm_version_hook.py"
write_runtime_version_hook(version_hook, version("camoufox-profile-manager"))

datas = []
webui_dir = os.path.join(ROOT, "src", "camoufox_pm", "webui")
if os.path.isdir(webui_dir):
    datas.append((webui_dir, "camoufox_pm/webui"))
datas += copy_metadata("camoufox-profile-manager")

binaries = []
hiddenimports = collect_submodules("uvicorn")
hiddenimports += collect_submodules("camoufox_pm.core.isolation")
hiddenimports += collect_submodules("camoufox_pm.core.cloud")
hiddenimports += [
    "camoufox_pm.core.bundled_runtime",
    "camoufox_pm.core.geoip",
    "camoufox_pm.core.isolated_terminal",
    "maxminddb",
    "zstandard",
]

excludes = [
    "camoufox",
    "playwright",
    "fpgen",
    "webview",
    "pywebview",
    "PyQt6",
    "PySide6",
    "numpy",
    "scipy",
    "pandas",
    "mypy",
    "mypyc",
    "Cython",
    "pytest",
    "_pytest",
    "ruff",
    "pip",
    "setuptools",
    "wheel",
    "tkinter",
    "unittest",
    "pydoc_data",
    "test",
]

a = Analysis(
    [os.path.join(ROOT, "src", "camoufox_pm", "sidecar.py")],
    pathex=[os.path.join(ROOT, "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=excludes,
    runtime_hooks=[str(version_hook)],
    noarchive=False,
)

pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="prism-backend",
    console=True,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    name="prism-backend",
)
