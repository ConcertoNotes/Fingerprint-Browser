"""棱镜浏览器 (Prism Browser) — strictly isolated antidetect browser manager.

Derived from Camoufox Profile Manager (https://github.com/polyackiy/camoufox-profile-manager,
MIT). Internal identifiers (the ``camoufox_pm`` package, the ``camoufox-pm`` command,
``CPM_*`` settings and the desktop data folder) are kept so existing installs,
scripts and local data continue working seamlessly.
"""

import sys
from importlib.metadata import PackageNotFoundError, version

# Read from the installed package rather than repeating the number here. The two
# had already drifted: a wheel built as 0.2.0 reported 0.1.1 through /health and
# in the OpenAPI schema, because releasing means editing pyproject.toml and it is
# the second copy that gets forgotten.
_build_version = (
    getattr(sys, "_camoufox_pm_build_version", None) if getattr(sys, "frozen", False) else None
)
if _build_version:
    # Desktop runtime hook is compiled into this executable. Old dist-info may
    # remain alongside it after an upgrade and must not determine its version.
    __version__ = _build_version
else:
    try:
        __version__ = version("camoufox-profile-manager")
    except PackageNotFoundError:  # pragma: no cover - only when running from a source tree
        __version__ = "0.0.0+unknown"

# The product name people see. Internal identifiers deliberately stay unchanged.
APP_NAME = "棱镜浏览器"
# For consoles and logs: a Windows console without UTF-8 cannot print APP_NAME.
APP_NAME_ASCII = "Prism Browser"
REPOSITORY_URL = "https://github.com/ConcertoNotes/Fingerprint-Browser"
UPSTREAM_URL = "https://github.com/polyackiy/camoufox-profile-manager"

__description__ = "棱镜浏览器 (Prism Browser): local-first isolated antidetect browser manager"
