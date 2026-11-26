"""Curated real-device desktop Chrome hardware fingerprint preset pool.

Provides 66 realistic, software-renderer-free desktop profiles across macOS,
Windows, and Linux designed for Prism Browser's embedded Chromium + CDP
isolation engine. Zero external browser binaries or third-party fingerprint
packages are required to allocate, preview, deduplicate, or pin a device.
"""

from __future__ import annotations

import re
import secrets
from typing import Any, Literal

OSName = Literal["macos", "windows", "linux"]

DEFAULT_CHROME_MAJOR = 136

_CHROME_MAJOR_RE = re.compile(r"Chrome/(\d+)")
_FIREFOX_MAJOR_RE = re.compile(r"(?:Firefox/|rv:)(\d+)")

_MACOS_FONTS: tuple[str, ...] = (
    "SF Pro Text",
    "SF Pro Display",
    "Helvetica Neue",
    "Helvetica",
    "Arial",
    "Arial Unicode MS",
    "Apple Color Emoji",
    "Avenir",
    "Avenir Next",
    "Courier New",
    "Geneva",
    "Georgia",
    "Gill Sans",
    "Hiragino Sans",
    "Lucida Grande",
    "Menlo",
    "Monaco",
    "Optima",
    "Palatino",
    "PingFang SC",
    "Times",
    "Times New Roman",
    "Trebuchet MS",
    "Verdana",
)

_WINDOWS_FONTS: tuple[str, ...] = (
    "Segoe UI",
    "Segoe UI Emoji",
    "Segoe UI Symbol",
    "Arial",
    "Bahnschrift",
    "Calibri",
    "Cambria",
    "Candara",
    "Comic Sans MS",
    "Consolas",
    "Constantia",
    "Corbel",
    "Courier New",
    "Ebrima",
    "Franklin Gothic Medium",
    "Gabriola",
    "Georgia",
    "Impact",
    "Lucida Console",
    "Microsoft YaHei",
    "Palatino Linotype",
    "Tahoma",
    "Times New Roman",
    "Trebuchet MS",
    "Verdana",
)

_LINUX_FONTS: tuple[str, ...] = (
    "DejaVu Sans",
    "DejaVu Serif",
    "DejaVu Sans Mono",
    "Liberation Sans",
    "Liberation Serif",
    "Liberation Mono",
    "Noto Sans",
    "Noto Serif",
    "Noto Color Emoji",
    "Noto Sans CJK SC",
    "Ubuntu",
    "Ubuntu Mono",
    "Cantarell",
    "Droid Sans",
    "FreeSans",
    "FreeSerif",
    "FreeMono",
    "Nimbus Sans",
    "URW Gothic",
)


def build_chrome_user_agent(os_name: str, chrome_major: int = DEFAULT_CHROME_MAJOR) -> str:
    """Return a clean desktop Chrome User-Agent string with zero Electron/Prism tokens."""
    normalized = os_name.lower()
    if normalized == "macos":
        return (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{chrome_major}.0.0.0 Safari/537.36"
        )
    if normalized == "windows":
        return (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{chrome_major}.0.0.0 Safari/537.36"
        )
    return (
        "Mozilla/5.0 (X11; Linux x86_64) "
        f"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{chrome_major}.0.0.0 Safari/537.36"
    )


def _mac(
    idx: int,
    *,
    w: int,
    h: int,
    dpr: float,
    cores: int,
    mem: int,
    vendor: str,
    renderer: str,
    arch: str = "arm",
    os_ver: str = "15.4.0",
    menu_h: int = 38,
) -> dict[str, Any]:
    return {
        "id": f"macos:{idx}",
        "os": "macos",
        "platform": "MacIntel",
        "platform_version": os_ver,
        "architecture": arch,
        "bitness": "64",
        "model": "",
        "mobile": False,
        "screen_width": w,
        "screen_height": h,
        "avail_width": w,
        "avail_height": h - menu_h,
        "color_depth": 24,
        "device_pixel_ratio": dpr,
        "hardware_concurrency": cores,
        "device_memory": mem,
        "webgl_vendor": vendor,
        "webgl_renderer": renderer,
        "fonts": list(_MACOS_FONTS),
    }


def _win(
    idx: int,
    *,
    w: int,
    h: int,
    dpr: float,
    cores: int,
    mem: int,
    vendor: str,
    renderer: str,
    os_ver: str = "15.0.0",
    taskbar_h: int = 48,
) -> dict[str, Any]:
    return {
        "id": f"windows:{idx}",
        "os": "windows",
        "platform": "Win32",
        "platform_version": os_ver,
        "architecture": "x86",
        "bitness": "64",
        "model": "",
        "mobile": False,
        "screen_width": w,
        "screen_height": h,
        "avail_width": w,
        "avail_height": h - taskbar_h,
        "color_depth": 24,
        "device_pixel_ratio": dpr,
        "hardware_concurrency": cores,
        "device_memory": mem,
        "webgl_vendor": vendor,
        "webgl_renderer": renderer,
        "fonts": list(_WINDOWS_FONTS),
    }


def _lin(
    idx: int,
    *,
    w: int,
    h: int,
    dpr: float,
    cores: int,
    mem: int,
    vendor: str,
    renderer: str,
    os_ver: str = "6.8.0",
    panel_h: int = 32,
) -> dict[str, Any]:
    return {
        "id": f"linux:{idx}",
        "os": "linux",
        "platform": "Linux x86_64",
        "platform_version": os_ver,
        "architecture": "x86",
        "bitness": "64",
        "model": "",
        "mobile": False,
        "screen_width": w,
        "screen_height": h,
        "avail_width": w,
        "avail_height": h - panel_h,
        "color_depth": 24,
        "device_pixel_ratio": dpr,
        "hardware_concurrency": cores,
        "device_memory": mem,
        "webgl_vendor": vendor,
        "webgl_renderer": renderer,
        "fonts": list(_LINUX_FONTS),
    }


# 22 macOS real-device presets (Apple Silicon M1-M4 + Intel MacBook Pro / iMac)
_MACOS_PRESETS: tuple[dict[str, Any], ...] = (
    _mac(
        0,
        w=1512,
        h=982,
        dpr=2,
        cores=10,
        mem=8,
        vendor="Google Inc. (Apple)",
        renderer="ANGLE (Apple, ANGLE Metal Renderer: Apple M3 Pro, Unspecified Version)",
        os_ver="15.4.0",
    ),
    _mac(
        1,
        w=1728,
        h=1117,
        dpr=2,
        cores=12,
        mem=8,
        vendor="Google Inc. (Apple)",
        renderer="ANGLE (Apple, ANGLE Metal Renderer: Apple M3 Max, Unspecified Version)",
        os_ver="15.4.0",
    ),
    _mac(
        2,
        w=1470,
        h=956,
        dpr=2,
        cores=8,
        mem=8,
        vendor="Google Inc. (Apple)",
        renderer="ANGLE (Apple, ANGLE Metal Renderer: Apple M3, Unspecified Version)",
        os_ver="15.3.2",
    ),
    _mac(
        3,
        w=1512,
        h=982,
        dpr=2,
        cores=14,
        mem=8,
        vendor="Google Inc. (Apple)",
        renderer="ANGLE (Apple, ANGLE Metal Renderer: Apple M4 Pro, Unspecified Version)",
        os_ver="15.4.1",
    ),
    _mac(
        4,
        w=1728,
        h=1117,
        dpr=2,
        cores=16,
        mem=8,
        vendor="Google Inc. (Apple)",
        renderer="ANGLE (Apple, ANGLE Metal Renderer: Apple M4 Max, Unspecified Version)",
        os_ver="15.4.1",
    ),
    _mac(
        5,
        w=1470,
        h=956,
        dpr=2,
        cores=10,
        mem=8,
        vendor="Google Inc. (Apple)",
        renderer="ANGLE (Apple, ANGLE Metal Renderer: Apple M4, Unspecified Version)",
        os_ver="15.4.0",
    ),
    _mac(
        6,
        w=1680,
        h=1050,
        dpr=2,
        cores=10,
        mem=8,
        vendor="Google Inc. (Apple)",
        renderer="ANGLE (Apple, ANGLE Metal Renderer: Apple M2 Pro, Unspecified Version)",
        os_ver="14.7.2",
    ),
    _mac(
        7,
        w=1728,
        h=1117,
        dpr=2,
        cores=12,
        mem=8,
        vendor="Google Inc. (Apple)",
        renderer="ANGLE (Apple, ANGLE Metal Renderer: Apple M2 Max, Unspecified Version)",
        os_ver="15.2.0",
    ),
    _mac(
        8,
        w=1470,
        h=956,
        dpr=2,
        cores=8,
        mem=8,
        vendor="Google Inc. (Apple)",
        renderer="ANGLE (Apple, ANGLE Metal Renderer: Apple M2, Unspecified Version)",
        os_ver="14.6.1",
    ),
    _mac(
        9,
        w=1440,
        h=900,
        dpr=2,
        cores=8,
        mem=8,
        vendor="Google Inc. (Apple)",
        renderer="ANGLE (Apple, ANGLE Metal Renderer: Apple M1, Unspecified Version)",
        os_ver="14.7.1",
    ),
    _mac(
        10,
        w=1512,
        h=982,
        dpr=2,
        cores=10,
        mem=8,
        vendor="Google Inc. (Apple)",
        renderer="ANGLE (Apple, ANGLE Metal Renderer: Apple M1 Pro, Unspecified Version)",
        os_ver="14.7.0",
    ),
    _mac(
        11,
        w=1728,
        h=1117,
        dpr=2,
        cores=10,
        mem=8,
        vendor="Google Inc. (Apple)",
        renderer="ANGLE (Apple, ANGLE Metal Renderer: Apple M1 Max, Unspecified Version)",
        os_ver="15.1.1",
    ),
    _mac(
        12,
        w=2560,
        h=1440,
        dpr=2,
        cores=12,
        mem=8,
        vendor="Google Inc. (Apple)",
        renderer="ANGLE (Apple, ANGLE Metal Renderer: Apple M2 Max, Unspecified Version)",
        os_ver="15.3.0",
        menu_h=25,
    ),
    _mac(
        13,
        w=2560,
        h=1440,
        dpr=2,
        cores=14,
        mem=8,
        vendor="Google Inc. (Apple)",
        renderer="ANGLE (Apple, ANGLE Metal Renderer: Apple M4 Pro, Unspecified Version)",
        os_ver="15.4.0",
        menu_h=25,
    ),
    _mac(
        14,
        w=2048,
        h=1152,
        dpr=2,
        cores=8,
        mem=8,
        vendor="Google Inc. (Apple)",
        renderer="ANGLE (Apple, ANGLE Metal Renderer: Apple M3, Unspecified Version)",
        os_ver="15.2.0",
        menu_h=25,
    ),
    _mac(
        15,
        w=1920,
        h=1080,
        dpr=2,
        cores=10,
        mem=8,
        vendor="Google Inc. (Apple)",
        renderer="ANGLE (Apple, ANGLE Metal Renderer: Apple M4, Unspecified Version)",
        os_ver="15.3.1",
        menu_h=25,
    ),
    _mac(
        16,
        w=2240,
        h=1260,
        dpr=2,
        cores=8,
        mem=8,
        vendor="Google Inc. (Apple)",
        renderer="ANGLE (Apple, ANGLE Metal Renderer: Apple M1, Unspecified Version)",
        os_ver="14.5.0",
        menu_h=25,
    ),
    _mac(
        17,
        w=2560,
        h=1440,
        dpr=2,
        cores=16,
        mem=8,
        vendor="Google Inc. (Apple)",
        renderer="ANGLE (Apple, ANGLE Metal Renderer: Apple M3 Max, Unspecified Version)",
        os_ver="15.4.0",
        menu_h=25,
    ),
    _mac(
        18,
        w=1680,
        h=1050,
        dpr=2,
        cores=8,
        mem=8,
        vendor="Google Inc. (Apple)",
        renderer="ANGLE (Apple, ANGLE Metal Renderer: Apple M2, Unspecified Version)",
        os_ver="15.1.0",
    ),
    _mac(
        19,
        w=1680,
        h=1050,
        dpr=2,
        cores=12,
        mem=8,
        vendor="Google Inc. (AMD)",
        renderer="ANGLE (AMD, AMD Radeon Pro 5500M OpenGL Engine, OpenGL 4.1)",
        arch="x86",
        os_ver="14.6.1",
    ),
    _mac(
        20,
        w=1440,
        h=900,
        dpr=2,
        cores=8,
        mem=8,
        vendor="Google Inc. (Intel Inc.)",
        renderer="ANGLE (Intel Inc., Intel(R) Iris(TM) Plus Graphics OpenGL Engine, OpenGL 4.1)",
        arch="x86",
        os_ver="14.5.0",
    ),
    _mac(
        21,
        w=2560,
        h=1440,
        dpr=2,
        cores=16,
        mem=8,
        vendor="Google Inc. (AMD)",
        renderer="ANGLE (AMD, AMD Radeon Pro 5700 XT OpenGL Engine, OpenGL 4.1)",
        arch="x86",
        os_ver="14.7.1",
        menu_h=25,
    ),
)

# 26 Windows real-device presets (NVIDIA RTX/GTX, AMD Radeon RX, Intel Iris Xe/UHD/Arc)
_WINDOWS_PRESETS: tuple[dict[str, Any], ...] = (
    _win(
        0,
        w=1920,
        h=1080,
        dpr=1,
        cores=12,
        mem=8,
        vendor="Google Inc. (NVIDIA)",
        renderer="ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 (0x00002882) Direct3D11 vs_5_0 ps_5_0, D3D11)",
        os_ver="15.0.0",
    ),
    _win(
        1,
        w=2560,
        h=1440,
        dpr=1,
        cores=16,
        mem=8,
        vendor="Google Inc. (NVIDIA)",
        renderer="ANGLE (NVIDIA, NVIDIA GeForce RTX 4070 (0x00002786) Direct3D11 vs_5_0 ps_5_0, D3D11)",
        os_ver="15.0.0",
    ),
    _win(
        2,
        w=2560,
        h=1440,
        dpr=1.25,
        cores=16,
        mem=8,
        vendor="Google Inc. (NVIDIA)",
        renderer="ANGLE (NVIDIA, NVIDIA GeForce RTX 4070 SUPER (0x00002783) Direct3D11 vs_5_0 ps_5_0, D3D11)",
        os_ver="15.0.0",
    ),
    _win(
        3,
        w=3840,
        h=2160,
        dpr=1.5,
        cores=16,
        mem=8,
        vendor="Google Inc. (NVIDIA)",
        renderer="ANGLE (NVIDIA, NVIDIA GeForce RTX 4080 (0x00002704) Direct3D11 vs_5_0 ps_5_0, D3D11)",
        os_ver="15.0.0",
    ),
    _win(
        4,
        w=3440,
        h=1440,
        dpr=1,
        cores=16,
        mem=8,
        vendor="Google Inc. (NVIDIA)",
        renderer="ANGLE (NVIDIA, NVIDIA GeForce RTX 4090 (0x00002684) Direct3D11 vs_5_0 ps_5_0, D3D11)",
        os_ver="15.0.0",
    ),
    _win(
        5,
        w=1920,
        h=1080,
        dpr=1,
        cores=12,
        mem=8,
        vendor="Google Inc. (NVIDIA)",
        renderer="ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 (0x00002504) Direct3D11 vs_5_0 ps_5_0, D3D11)",
        os_ver="15.0.0",
    ),
    _win(
        6,
        w=2560,
        h=1440,
        dpr=1,
        cores=12,
        mem=8,
        vendor="Google Inc. (NVIDIA)",
        renderer="ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Ti (0x00002489) Direct3D11 vs_5_0 ps_5_0, D3D11)",
        os_ver="10.0.0",
        taskbar_h=40,
    ),
    _win(
        7,
        w=2560,
        h=1440,
        dpr=1,
        cores=16,
        mem=8,
        vendor="Google Inc. (NVIDIA)",
        renderer="ANGLE (NVIDIA, NVIDIA GeForce RTX 3070 (0x00002484) Direct3D11 vs_5_0 ps_5_0, D3D11)",
        os_ver="15.0.0",
    ),
    _win(
        8,
        w=2560,
        h=1440,
        dpr=1.25,
        cores=16,
        mem=8,
        vendor="Google Inc. (NVIDIA)",
        renderer="ANGLE (NVIDIA, NVIDIA GeForce RTX 3080 (0x00002206) Direct3D11 vs_5_0 ps_5_0, D3D11)",
        os_ver="15.0.0",
    ),
    _win(
        9,
        w=1920,
        h=1080,
        dpr=1.25,
        cores=8,
        mem=8,
        vendor="Google Inc. (NVIDIA)",
        renderer="ANGLE (NVIDIA, NVIDIA GeForce RTX 3050 Laptop GPU (0x000025A2) Direct3D11 vs_5_0 ps_5_0, D3D11)",
        os_ver="15.0.0",
    ),
    _win(
        10,
        w=1920,
        h=1080,
        dpr=1,
        cores=8,
        mem=8,
        vendor="Google Inc. (NVIDIA)",
        renderer="ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 Ti (0x00002803) Direct3D11 vs_5_0 ps_5_0, D3D11)",
        os_ver="15.0.0",
    ),
    _win(
        11,
        w=1920,
        h=1080,
        dpr=1,
        cores=6,
        mem=8,
        vendor="Google Inc. (NVIDIA)",
        renderer="ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 SUPER (0x000021C4) Direct3D11 vs_5_0 ps_5_0, D3D11)",
        os_ver="10.0.0",
        taskbar_h=40,
    ),
    _win(
        12,
        w=1920,
        h=1080,
        dpr=1,
        cores=8,
        mem=8,
        vendor="Google Inc. (NVIDIA)",
        renderer="ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Ti (0x00002182) Direct3D11 vs_5_0 ps_5_0, D3D11)",
        os_ver="10.0.0",
        taskbar_h=40,
    ),
    _win(
        13,
        w=1600,
        h=900,
        dpr=1,
        cores=6,
        mem=8,
        vendor="Google Inc. (NVIDIA)",
        renderer="ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 (0x00001F82) Direct3D11 vs_5_0 ps_5_0, D3D11)",
        os_ver="10.0.0",
        taskbar_h=40,
    ),
    _win(
        14,
        w=1920,
        h=1080,
        dpr=1,
        cores=12,
        mem=8,
        vendor="Google Inc. (AMD)",
        renderer="ANGLE (AMD, AMD Radeon RX 6600 (0x000073FF) Direct3D11 vs_5_0 ps_5_0, D3D11)",
        os_ver="15.0.0",
    ),
    _win(
        15,
        w=2560,
        h=1440,
        dpr=1,
        cores=12,
        mem=8,
        vendor="Google Inc. (AMD)",
        renderer="ANGLE (AMD, AMD Radeon RX 6700 XT (0x000073DF) Direct3D11 vs_5_0 ps_5_0, D3D11)",
        os_ver="15.0.0",
    ),
    _win(
        16,
        w=2560,
        h=1440,
        dpr=1,
        cores=16,
        mem=8,
        vendor="Google Inc. (AMD)",
        renderer="ANGLE (AMD, AMD Radeon RX 6800 XT (0x000073BF) Direct3D11 vs_5_0 ps_5_0, D3D11)",
        os_ver="15.0.0",
    ),
    _win(
        17,
        w=1920,
        h=1080,
        dpr=1,
        cores=12,
        mem=8,
        vendor="Google Inc. (AMD)",
        renderer="ANGLE (AMD, AMD Radeon RX 7600 (0x00007480) Direct3D11 vs_5_0 ps_5_0, D3D11)",
        os_ver="15.0.0",
    ),
    _win(
        18,
        w=2560,
        h=1440,
        dpr=1,
        cores=16,
        mem=8,
        vendor="Google Inc. (AMD)",
        renderer="ANGLE (AMD, AMD Radeon RX 7700 XT (0x0000747E) Direct3D11 vs_5_0 ps_5_0, D3D11)",
        os_ver="15.0.0",
    ),
    _win(
        19,
        w=2560,
        h=1440,
        dpr=1.25,
        cores=16,
        mem=8,
        vendor="Google Inc. (AMD)",
        renderer="ANGLE (AMD, AMD Radeon RX 7800 XT (0x0000747E) Direct3D11 vs_5_0 ps_5_0, D3D11)",
        os_ver="15.0.0",
    ),
    _win(
        20,
        w=3840,
        h=2160,
        dpr=1.5,
        cores=16,
        mem=8,
        vendor="Google Inc. (AMD)",
        renderer="ANGLE (AMD, AMD Radeon RX 7900 XTX (0x0000744C) Direct3D11 vs_5_0 ps_5_0, D3D11)",
        os_ver="15.0.0",
    ),
    _win(
        21,
        w=1920,
        h=1200,
        dpr=1.25,
        cores=12,
        mem=8,
        vendor="Google Inc. (AMD)",
        renderer="ANGLE (AMD, AMD Radeon 780M Graphics (0x000015BF) Direct3D11 vs_5_0 ps_5_0, D3D11)",
        os_ver="15.0.0",
    ),
    _win(
        22,
        w=1920,
        h=1080,
        dpr=1.25,
        cores=8,
        mem=8,
        vendor="Google Inc. (Intel)",
        renderer="ANGLE (Intel, Intel(R) Iris(R) Xe Graphics (0x00009A49) Direct3D11 vs_5_0 ps_5_0, D3D11)",
        os_ver="15.0.0",
    ),
    _win(
        23,
        w=1536,
        h=864,
        dpr=1.25,
        cores=8,
        mem=8,
        vendor="Google Inc. (Intel)",
        renderer="ANGLE (Intel, Intel(R) Iris(R) Xe Graphics (0x000046A6) Direct3D11 vs_5_0 ps_5_0, D3D11)",
        os_ver="15.0.0",
    ),
    _win(
        24,
        w=1920,
        h=1080,
        dpr=1,
        cores=8,
        mem=8,
        vendor="Google Inc. (Intel)",
        renderer="ANGLE (Intel, Intel(R) UHD Graphics 770 (0x00004680) Direct3D11 vs_5_0 ps_5_0, D3D11)",
        os_ver="15.0.0",
    ),
    _win(
        25,
        w=2560,
        h=1440,
        dpr=1,
        cores=12,
        mem=8,
        vendor="Google Inc. (Intel)",
        renderer="ANGLE (Intel, Intel(R) Arc(TM) A750 Graphics (0x000056A1) Direct3D11 vs_5_0 ps_5_0, D3D11)",
        os_ver="15.0.0",
    ),
)

# 18 Linux real-device presets (AMD Mesa, NVIDIA proprietary, Intel Mesa)
_LINUX_PRESETS: tuple[dict[str, Any], ...] = (
    _lin(
        0,
        w=1920,
        h=1080,
        dpr=1,
        cores=8,
        mem=8,
        vendor="Google Inc. (AMD)",
        renderer="ANGLE (AMD, AMD Radeon RX 6600 (radeonsi, navi23, LLVM 18.1.3, DRM 3.57, 6.8.0), OpenGL 4.6)",
        os_ver="6.8.0",
    ),
    _lin(
        1,
        w=2560,
        h=1440,
        dpr=1,
        cores=12,
        mem=8,
        vendor="Google Inc. (AMD)",
        renderer="ANGLE (AMD, AMD Radeon RX 6700 XT (radeonsi, navi22, LLVM 18.1.3, DRM 3.57, 6.8.0), OpenGL 4.6)",
        os_ver="6.8.0",
    ),
    _lin(
        2,
        w=2560,
        h=1440,
        dpr=1,
        cores=16,
        mem=8,
        vendor="Google Inc. (AMD)",
        renderer="ANGLE (AMD, AMD Radeon RX 6800 (radeonsi, navi21, LLVM 18.1.8, DRM 3.58, 6.11.0), OpenGL 4.6)",
        os_ver="6.11.0",
    ),
    _lin(
        3,
        w=2560,
        h=1440,
        dpr=1,
        cores=16,
        mem=8,
        vendor="Google Inc. (AMD)",
        renderer="ANGLE (AMD, AMD Radeon RX 7800 XT (radeonsi, navi32, LLVM 18.1.8, DRM 3.58, 6.11.0), OpenGL 4.6)",
        os_ver="6.11.0",
    ),
    _lin(
        4,
        w=1920,
        h=1200,
        dpr=1,
        cores=12,
        mem=8,
        vendor="Google Inc. (AMD)",
        renderer="ANGLE (AMD, AMD Radeon 780M (radeonsi, gfx1103_r1, LLVM 18.1.3, DRM 3.57, 6.8.0), OpenGL 4.6)",
        os_ver="6.8.0",
    ),
    _lin(
        5,
        w=3840,
        h=2160,
        dpr=2,
        cores=16,
        mem=8,
        vendor="Google Inc. (AMD)",
        renderer="ANGLE (AMD, AMD Radeon RX 7900 XT (radeonsi, navi31, LLVM 18.1.8, DRM 3.58, 6.11.0), OpenGL 4.6)",
        os_ver="6.11.0",
    ),
    _lin(
        6,
        w=1920,
        h=1080,
        dpr=1,
        cores=8,
        mem=8,
        vendor="Google Inc. (NVIDIA Corporation)",
        renderer="ANGLE (NVIDIA Corporation, NVIDIA GeForce RTX 3060/PCIe/SSE2, OpenGL 4.5.0)",
        os_ver="6.8.0",
    ),
    _lin(
        7,
        w=2560,
        h=1440,
        dpr=1,
        cores=12,
        mem=8,
        vendor="Google Inc. (NVIDIA Corporation)",
        renderer="ANGLE (NVIDIA Corporation, NVIDIA GeForce RTX 3070/PCIe/SSE2, OpenGL 4.5.0)",
        os_ver="6.8.0",
    ),
    _lin(
        8,
        w=2560,
        h=1440,
        dpr=1,
        cores=16,
        mem=8,
        vendor="Google Inc. (NVIDIA Corporation)",
        renderer="ANGLE (NVIDIA Corporation, NVIDIA GeForce RTX 3080/PCIe/SSE2, OpenGL 4.5.0)",
        os_ver="6.6.0",
    ),
    _lin(
        9,
        w=1920,
        h=1080,
        dpr=1,
        cores=12,
        mem=8,
        vendor="Google Inc. (NVIDIA Corporation)",
        renderer="ANGLE (NVIDIA Corporation, NVIDIA GeForce RTX 4060/PCIe/SSE2, OpenGL 4.5.0)",
        os_ver="6.8.0",
    ),
    _lin(
        10,
        w=2560,
        h=1440,
        dpr=1,
        cores=16,
        mem=8,
        vendor="Google Inc. (NVIDIA Corporation)",
        renderer="ANGLE (NVIDIA Corporation, NVIDIA GeForce RTX 4070/PCIe/SSE2, OpenGL 4.5.0)",
        os_ver="6.11.0",
    ),
    _lin(
        11,
        w=3840,
        h=2160,
        dpr=2,
        cores=16,
        mem=8,
        vendor="Google Inc. (NVIDIA Corporation)",
        renderer="ANGLE (NVIDIA Corporation, NVIDIA GeForce RTX 4080/PCIe/SSE2, OpenGL 4.5.0)",
        os_ver="6.11.0",
    ),
    _lin(
        12,
        w=1920,
        h=1080,
        dpr=1,
        cores=6,
        mem=8,
        vendor="Google Inc. (NVIDIA Corporation)",
        renderer="ANGLE (NVIDIA Corporation, NVIDIA GeForce GTX 1660 SUPER/PCIe/SSE2, OpenGL 4.5.0)",
        os_ver="6.6.0",
    ),
    _lin(
        13,
        w=1920,
        h=1080,
        dpr=1,
        cores=8,
        mem=8,
        vendor="Google Inc. (Intel)",
        renderer="ANGLE (Intel, Mesa Intel(R) Iris(R) Xe Graphics (ADL GT2), OpenGL 4.6)",
        os_ver="6.8.0",
    ),
    _lin(
        14,
        w=1920,
        h=1200,
        dpr=1,
        cores=12,
        mem=8,
        vendor="Google Inc. (Intel)",
        renderer="ANGLE (Intel, Mesa Intel(R) Iris(R) Xe Graphics (TGL GT2), OpenGL 4.6)",
        os_ver="6.8.0",
    ),
    _lin(
        15,
        w=1920,
        h=1080,
        dpr=1,
        cores=8,
        mem=8,
        vendor="Google Inc. (Intel)",
        renderer="ANGLE (Intel, Mesa Intel(R) UHD Graphics 770 (ADL-S GT1), OpenGL 4.6)",
        os_ver="6.8.0",
    ),
    _lin(
        16,
        w=2560,
        h=1440,
        dpr=1,
        cores=12,
        mem=8,
        vendor="Google Inc. (Intel)",
        renderer="ANGLE (Intel, Mesa Intel(R) Arc(tm) A770 Graphics (DG2), OpenGL 4.6)",
        os_ver="6.11.0",
    ),
    _lin(
        17,
        w=1600,
        h=900,
        dpr=1,
        cores=4,
        mem=4,
        vendor="Google Inc. (Intel)",
        renderer="ANGLE (Intel, Mesa Intel(R) UHD Graphics 630 (CFL GT2), OpenGL 4.6)",
        os_ver="6.6.0",
    ),
)

CHROME_PRESET_POOL: dict[OSName, tuple[dict[str, Any], ...]] = {
    "macos": _MACOS_PRESETS,
    "windows": _WINDOWS_PRESETS,
    "linux": _LINUX_PRESETS,
}

_PRESETS_BY_ID: dict[str, dict[str, Any]] = {
    preset["id"]: preset for group in CHROME_PRESET_POOL.values() for preset in group
}


def get_chrome_preset(preset_id: str) -> dict[str, Any] | None:
    """Look up a built-in real-device Chrome preset by ID."""
    found = _PRESETS_BY_ID.get(preset_id)
    return dict(found) if found is not None else None


def list_chrome_presets(os_name: str | None = None) -> list[dict[str, Any]]:
    """Return built-in Chrome presets for one OS or all desktop OSes."""
    if os_name:
        key = os_name.lower()
        if key not in CHROME_PRESET_POOL:
            return []
        return [dict(item) for item in CHROME_PRESET_POOL[key]]  # type: ignore[index]
    return [dict(item) for group in CHROME_PRESET_POOL.values() for item in group]


def preset_from_curated(raw: dict[str, Any], fallback_id: str) -> dict[str, Any]:
    """Normalize a cloud-curated preset (flat Chrome or legacy nested) into canonical Chrome format."""
    os_name = str(raw.get("os") or "windows").lower()
    if os_name not in {"macos", "windows", "linux"}:
        os_name = "windows"

    if "screen_width" in raw or "webgl_renderer" in raw:
        w = int(raw.get("screen_width") or 1920)
        h = int(raw.get("screen_height") or 1080)
        default_platform = {
            "macos": "MacIntel",
            "windows": "Win32",
            "linux": "Linux x86_64",
        }[os_name]
        default_fonts = {
            "macos": list(_MACOS_FONTS),
            "windows": list(_WINDOWS_FONTS),
            "linux": list(_LINUX_FONTS),
        }[os_name]
        return {
            "id": str(raw.get("id") or fallback_id),
            "os": os_name,
            "user_agent": raw.get("user_agent"),
            "chrome_major": int(raw.get("chrome_major") or DEFAULT_CHROME_MAJOR),
            "platform": str(raw.get("platform") or default_platform),
            "platform_version": str(
                raw.get("platform_version")
                or (
                    "15.4.0"
                    if os_name == "macos"
                    else "15.0.0"
                    if os_name == "windows"
                    else "6.8.0"
                )
            ),
            "architecture": str(
                raw.get("architecture") or ("arm" if os_name == "macos" else "x86")
            ),
            "bitness": str(raw.get("bitness") or "64"),
            "model": str(raw.get("model") or ""),
            "mobile": bool(raw.get("mobile", False)),
            "screen_width": w,
            "screen_height": h,
            "avail_width": int(raw.get("avail_width") or w),
            "avail_height": int(raw.get("avail_height") or (h - 40)),
            "color_depth": int(raw.get("color_depth") or 24),
            "device_pixel_ratio": float(
                raw.get("device_pixel_ratio") or (2 if os_name == "macos" else 1)
            ),
            "hardware_concurrency": int(raw.get("hardware_concurrency") or 8),
            "device_memory": int(raw.get("device_memory") or 8),
            "webgl_vendor": str(raw.get("webgl_vendor") or "Google Inc."),
            "webgl_renderer": str(raw.get("webgl_renderer") or ""),
            "fonts": list(raw["fonts"]) if isinstance(raw.get("fonts"), list) else default_fonts,
        }

    # Legacy nested navigator/screen/webgl shape.
    raw_nav = raw.get("navigator")
    nav: dict[str, Any] = raw_nav if isinstance(raw_nav, dict) else {}
    raw_screen = raw.get("screen")
    screen: dict[str, Any] = raw_screen if isinstance(raw_screen, dict) else {}
    raw_webgl = raw.get("webgl")
    webgl: dict[str, Any] = raw_webgl if isinstance(raw_webgl, dict) else {}
    w = int(screen.get("width") or 1920)
    h = int(screen.get("height") or 1080)
    default_platform = {
        "macos": "MacIntel",
        "windows": "Win32",
        "linux": "Linux x86_64",
    }[os_name]
    default_fonts = {
        "macos": list(_MACOS_FONTS),
        "windows": list(_WINDOWS_FONTS),
        "linux": list(_LINUX_FONTS),
    }[os_name]
    return {
        "id": str(raw.get("id") or fallback_id),
        "os": os_name,
        "user_agent": nav.get("userAgent"),
        "chrome_major": DEFAULT_CHROME_MAJOR,
        "platform": str(nav.get("platform") or default_platform),
        "platform_version": (
            "15.4.0" if os_name == "macos" else "15.0.0" if os_name == "windows" else "6.8.0"
        ),
        "architecture": "arm" if os_name == "macos" else "x86",
        "bitness": "64",
        "model": "",
        "mobile": False,
        "screen_width": w,
        "screen_height": h,
        "avail_width": int(screen.get("availWidth") or w),
        "avail_height": int(screen.get("availHeight") or (h - 40)),
        "color_depth": int(screen.get("colorDepth") or 24),
        "device_pixel_ratio": 2 if os_name == "macos" else 1,
        "hardware_concurrency": int(nav.get("hardwareConcurrency") or 8),
        "device_memory": int(nav.get("deviceMemory") or 8),
        "webgl_vendor": str(webgl.get("unmaskedVendor") or "Google Inc."),
        "webgl_renderer": str(webgl.get("unmaskedRenderer") or ""),
        "fonts": list(raw["fonts"]) if isinstance(raw.get("fonts"), list) else default_fonts,
    }


def build_pinned_fingerprint(
    preset: dict[str, Any],
    *,
    chrome_major: int = DEFAULT_CHROME_MAJOR,
    seeds: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Convert a canonical Chrome preset into a pinned profile fingerprint with 16-byte seeds."""
    os_name = str(preset.get("os") or "windows").lower()
    major = int(chrome_major or preset.get("chrome_major") or DEFAULT_CHROME_MAJOR)
    ua = build_chrome_user_agent(os_name, major)
    seed_map = seeds or {}
    return {
        "user_agent": ua,
        "chrome_major": major,
        "platform": preset["platform"],
        "platform_version": preset["platform_version"],
        "architecture": preset["architecture"],
        "bitness": preset["bitness"],
        "model": preset.get("model", ""),
        "mobile": bool(preset.get("mobile", False)),
        "screen_width": int(preset["screen_width"]),
        "screen_height": int(preset["screen_height"]),
        "avail_width": int(preset["avail_width"]),
        "avail_height": int(preset["avail_height"]),
        "color_depth": int(preset.get("color_depth", 24)),
        "device_pixel_ratio": preset.get("device_pixel_ratio", 1),
        "hardware_concurrency": int(preset["hardware_concurrency"]),
        "device_memory": int(preset.get("device_memory", 8)),
        "webgl_vendor": preset["webgl_vendor"],
        "webgl_renderer": preset["webgl_renderer"],
        "canvas_seed": seed_map.get("canvas_seed") or secrets.token_hex(16),
        "audio_seed": seed_map.get("audio_seed") or secrets.token_hex(16),
        "webgl_seed": seed_map.get("webgl_seed") or secrets.token_hex(16),
        "fonts": list(preset.get("fonts") or []),
    }


def pin_user_agent(pin: dict[str, Any] | None) -> str | None:
    if not pin:
        return None
    val = pin.get("user_agent") or pin.get("navigator.userAgent")
    return str(val) if isinstance(val, str) and val.strip() else None


def pin_platform(pin: dict[str, Any] | None) -> str | None:
    if not pin:
        return None
    val = pin.get("platform") or pin.get("navigator.platform")
    return str(val) if isinstance(val, str) and val.strip() else None


def pin_screen(pin: dict[str, Any] | None) -> tuple[int | None, int | None]:
    if not pin:
        return None, None
    w = pin.get("screen_width") if "screen_width" in pin else pin.get("screen.width")
    h = pin.get("screen_height") if "screen_height" in pin else pin.get("screen.height")
    return (
        int(w) if isinstance(w, int) and not isinstance(w, bool) else None,
        int(h) if isinstance(h, int) and not isinstance(h, bool) else None,
    )


def pin_cores(pin: dict[str, Any] | None) -> int | None:
    if not pin:
        return None
    val = (
        pin.get("hardware_concurrency")
        if "hardware_concurrency" in pin
        else pin.get("navigator.hardwareConcurrency")
    )
    return int(val) if isinstance(val, int) and not isinstance(val, bool) else None


def pin_gpu(pin: dict[str, Any] | None) -> tuple[str | None, str | None]:
    if not pin:
        return None, None
    vendor = pin.get("webgl_vendor") or pin.get("webGl:vendor")
    renderer = pin.get("webgl_renderer") or pin.get("webGl:renderer")
    return (
        str(vendor) if isinstance(vendor, str) and vendor.strip() else None,
        str(renderer) if isinstance(renderer, str) and renderer.strip() else None,
    )


def pinned_os(pin: dict[str, Any] | None) -> OSName | None:
    """Infer the pinned desktop OS from platform / oscpu / User-Agent."""
    if not pin:
        return None
    platform_val = (pin_platform(pin) or "").lower()
    ua_val = (pin_user_agent(pin) or "").lower()
    oscpu = str(pin.get("navigator.oscpu") or "").lower()
    for haystack in (platform_val, oscpu, ua_val):
        if not haystack:
            continue
        if "mac" in haystack:
            return "macos"
        if "win" in haystack:
            return "windows"
        if "linux" in haystack or "x11" in haystack:
            return "linux"
    return None


def browser_major(pin: dict[str, Any] | None) -> int | None:
    """Return the browser major version from a Chrome or legacy Firefox pin."""
    if not pin:
        return None
    explicit = pin.get("chrome_major")
    if isinstance(explicit, int) and not isinstance(explicit, bool) and explicit > 0:
        return explicit
    ua = pin_user_agent(pin)
    if not ua:
        return None
    match = _CHROME_MAJOR_RE.search(ua) or _FIREFOX_MAJOR_RE.search(ua)
    return int(match.group(1)) if match else None


def is_outdated(
    pin: dict[str, Any] | None, target_major: int | None = DEFAULT_CHROME_MAJOR
) -> bool:
    if not pin or target_major is None:
        return False
    major = browser_major(pin)
    if major is None:
        return False
    return major < target_major


def normalize_pinned_fingerprint(
    pin: dict[str, Any],
    *,
    os_hint: str | None = None,
    chrome_major: int = DEFAULT_CHROME_MAJOR,
) -> dict[str, Any]:
    """Ensure a pinned fingerprint carries all Chromium CDP fields + deterministic seeds."""
    if not pin:
        return {}
    out = dict(pin)
    inferred_os = pinned_os(out) or (os_hint.lower() if os_hint else None) or "windows"
    if inferred_os not in {"macos", "windows", "linux"}:
        inferred_os = "windows"

    # Migrate legacy Camoufox dotted keys into canonical Chromium keys while keeping hardware!
    ua = pin_user_agent(out)
    if not ua or "Firefox/" in ua:
        ua = build_chrome_user_agent(inferred_os, chrome_major)
    out["user_agent"] = ua
    out["chrome_major"] = browser_major({"user_agent": ua}) or chrome_major

    default_platform = {"macos": "MacIntel", "windows": "Win32", "linux": "Linux x86_64"}[
        inferred_os
    ]
    out["platform"] = pin_platform(out) or default_platform
    out.setdefault(
        "platform_version",
        "15.4.0" if inferred_os == "macos" else "15.0.0" if inferred_os == "windows" else "6.8.0",
    )
    out.setdefault("architecture", "arm" if inferred_os == "macos" else "x86")
    out.setdefault("bitness", "64")
    out.setdefault("model", "")
    out.setdefault("mobile", False)

    w, h = pin_screen(out)
    w = w or 1920
    h = h or 1080
    out["screen_width"] = w
    out["screen_height"] = h
    out["avail_width"] = int(out.get("avail_width") or out.get("screen.availWidth") or w)
    out["avail_height"] = int(
        out.get("avail_height") or out.get("screen.availHeight") or max(480, h - 40)
    )
    out.setdefault("color_depth", int(out.get("screen.colorDepth") or 24))
    out.setdefault("device_pixel_ratio", 2 if inferred_os == "macos" else 1)

    cores = pin_cores(out) or 8
    out["hardware_concurrency"] = cores
    out.setdefault("device_memory", 8)

    vendor, renderer = pin_gpu(out)
    out["webgl_vendor"] = vendor or (
        "Google Inc. (Apple)" if inferred_os == "macos" else "Google Inc."
    )
    out["webgl_renderer"] = renderer or (
        "ANGLE (Apple, ANGLE Metal Renderer: Apple M3, Unspecified Version)"
        if inferred_os == "macos"
        else "ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 Direct3D11 vs_5_0 ps_5_0, D3D11)"
    )

    out.setdefault("canvas_seed", secrets.token_hex(16))
    out.setdefault("audio_seed", secrets.token_hex(16))
    out.setdefault("webgl_seed", secrets.token_hex(16))
    if not isinstance(out.get("fonts"), list):
        out["fonts"] = list(
            _MACOS_FONTS
            if inferred_os == "macos"
            else _WINDOWS_FONTS
            if inferred_os == "windows"
            else _LINUX_FONTS
        )
    return out


def refresh_chrome_version(
    pin: dict[str, Any],
    *,
    target_major: int = DEFAULT_CHROME_MAJOR,
    os_hint: str | None = None,
) -> dict[str, Any]:
    """Advance only the Chrome version/User-Agent of a pinned fingerprint, keeping all hardware."""
    updated = dict(pin)
    os_name = pinned_os(updated) or (os_hint.lower() if os_hint else None) or "windows"
    new_ua = build_chrome_user_agent(os_name, target_major)
    updated["user_agent"] = new_ua
    updated["chrome_major"] = target_major
    if "navigator.userAgent" in updated:
        updated["navigator.userAgent"] = new_ua
    return updated


def summarize_fingerprint(
    pin: dict[str, Any] | None,
    settings_os: str | None = None,
    *,
    target_major: int = DEFAULT_CHROME_MAJOR,
) -> dict[str, Any] | None:
    """Build the compact fingerprint summary exposed on ``ProfileResponse.fingerprint``."""
    if not pin:
        return None
    w, h = pin_screen(pin)
    _, renderer = pin_gpu(pin)
    fonts = pin.get("fonts")
    p_os = pinned_os(pin)
    major = browser_major(pin)
    return {
        "user_agent": pin_user_agent(pin),
        "platform": pin_platform(pin),
        "hardware_concurrency": pin_cores(pin),
        "screen": f"{w}x{h}" if w and h else None,
        "gpu": renderer,
        "font_count": len(fonts) if isinstance(fonts, list) else None,
        "property_count": len(pin),
        "browser_major": major,
        "installed_major": target_major,
        "browser_outdated": bool(major is not None and major < target_major),
        "pinned_os": p_os,
        "settings_os": settings_os,
        "os_mismatch": bool(p_os and settings_os and p_os != settings_os),
    }
