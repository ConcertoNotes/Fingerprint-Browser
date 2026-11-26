"""Portable JSON / Netscape cookie parsing, validation, and local vault helpers.

Live Chromium session cookies are read and written directly by Electron via
``ses.cookies`` IPC. This module provides credential-safe JSON & Netscape
parsing/serialization, preview validation, and an atomic local cookie vault
(``<profile_dir>/cookies.json``) for closed/headless REST workflows.
"""

from __future__ import annotations

import ipaddress
import json
import math
import os
import re
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from .database import StaleWriteError
from .fs_permissions import protect_path, reject_reparse_paths
from .leases import make_lease_holder
from .profile_operations import profile_data_path, profile_operation, run_file_operation

MAX_BYTES = 2 * 1024 * 1024
MAX_COOKIES = 10_000
CookieFormat = Literal["json", "netscape"]
_VAULT_NAME = "cookies.json"
_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


class CookieTransferError(ValueError):
    """Credential-safe error: messages never include cookie input."""


@dataclass
class CookieBatch:
    cookies: list[dict[str, Any]]
    total: int
    skipped: list[dict[str, Any]]

    def preview(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "importable": len(self.cookies),
            "session_count": sum(cookie["expires"] == -1 for cookie in self.cookies),
            "skipped": self.skipped,
        }


def _fail(row: int, reason: str) -> None:
    raise CookieTransferError(f"Row {row}: {reason}")


def _domain(value: Any, row: int) -> str:
    if not isinstance(value, str) or not value or any(char.isspace() for char in value):
        _fail(row, "A cookie domain is required")
    host = value.lstrip(".")
    if any(char in host for char in "/@?#%\\:"):
        _fail(row, "Invalid cookie domain")
    try:
        host = host.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError:
        _fail(row, "Invalid cookie domain")
    if len(host) > 253 or not all(
        re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in host.split(".")
    ):
        _fail(row, "Invalid cookie domain")
    return ("." if value.startswith(".") else "") + host


def _boolean(value: Any, row: int) -> bool:
    if not isinstance(value, bool):
        _fail(row, "Cookie flags must be true or false")
    return value


def _normalize(item: Any, row: int) -> dict[str, Any]:
    if not isinstance(item, dict):
        _fail(row, "Each cookie must be an object")
    if (
        item.get("partitionKey") is not None
        or item.get("partitioned")
        or item.get("firstPartyDomain")
    ):
        _fail(row, "Partitioned cookies need a full profile archive")
    if item.get("originAttributes") or item.get("storeId") not in (None, "0", "firefox-default"):
        _fail(row, "Container cookies need a full profile archive")
    name, value = item.get("name"), item.get("value")
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        _fail(row, "Invalid cookie name")
    if not isinstance(value, str) or any(ord(char) < 32 or ord(char) == 127 for char in value):
        _fail(row, "Invalid cookie value")
    try:
        value_size = len((name + value).encode("utf-8"))
    except UnicodeError:
        _fail(row, "Cookie value contains invalid Unicode")
    if value_size > 4096:
        _fail(row, "Cookie name and value exceed 4096 bytes")
    domain = item.get("domain")
    url_path, url_secure = "/", False
    if "url" in item:
        try:
            raw_url = item["url"]
            if (
                not isinstance(raw_url, str)
                or domain is not None
                or "path" in item
                or any(ord(char) <= 32 or ord(char) == 127 for char in raw_url)
            ):
                raise ValueError
            url = urlsplit(raw_url)
            if url.scheme not in ("http", "https") or url.username is not None or not url.hostname:
                raise ValueError
            _ = url.port
            domain = url.hostname
            url_path = url.path[: url.path.rfind("/") + 1] or "/"
            url_secure = url.scheme == "https"
        except ValueError:
            _fail(row, "Invalid cookie URL")
    domain = _domain(domain, row)
    host_only = _boolean(item.get("hostOnly", not domain.startswith(".")), row)
    domain = domain.lstrip(".") if host_only else "." + domain.lstrip(".")
    if not host_only:
        if "." not in domain.lstrip("."):
            _fail(row, "Single-label cookie domains must be host-only")
        try:
            ipaddress.ip_address(domain.lstrip("."))
        except ValueError:
            pass
        else:
            _fail(row, "IP-address cookies must be host-only")
    path = item.get("path", url_path)
    if not isinstance(path, str) or not path.startswith("/"):
        _fail(row, "Cookie path must begin with a slash")
    try:
        path_size = len(path.encode("utf-8"))
    except UnicodeError:
        _fail(row, "Cookie path contains invalid Unicode")
    if path_size > 1024:
        _fail(row, "Cookie path must begin with a slash and fit within 1024 bytes")
    if any(ord(char) < 32 or ord(char) == 127 for char in path):
        _fail(row, "Invalid cookie path")
    secure = _boolean(item.get("secure", False), row)
    if "url" in item:
        secure = url_secure
    http_only = _boolean(item.get("httpOnly", False), row)
    raw_same_site = item.get("sameSite", "Lax")
    sites = {
        "strict": "Strict",
        "lax": "Lax",
        "none": "None",
        "no_restriction": "None",
        "unspecified": "Lax",
    }
    if not isinstance(raw_same_site, str) or raw_same_site.lower() not in sites:
        _fail(row, "SameSite must be Strict, Lax or None")
    same_site = sites[raw_same_site.lower()]
    if same_site == "None" and not secure:
        _fail(row, "SameSite=None requires a secure cookie")
    if name.startswith("__Secure-") and not secure:
        _fail(row, "A __Secure- cookie must be secure")
    if name.startswith("__Host-") and (not secure or not host_only or path != "/"):
        _fail(row, "A __Host- cookie must be secure, host-only and use path /")
    session = _boolean(item["session"], row) if "session" in item else False
    expiry = -1 if session else item.get("expires", item.get("expirationDate", -1))
    if (
        isinstance(expiry, bool)
        or not isinstance(expiry, (int, float))
        or not math.isfinite(expiry)
    ):
        _fail(row, "Cookie expiry must be a finite Unix timestamp")
    if expiry != -1 and not 0 <= expiry <= 253402300799:
        _fail(row, "Invalid cookie expiry")
    return {
        "name": name,
        "value": value,
        "domain": domain,
        "path": path,
        "expires": expiry,
        "httpOnly": http_only,
        "secure": secure,
        "sameSite": same_site,
    }


def _key(cookie: dict[str, Any]) -> tuple[str, str, str]:
    return cookie["name"], cookie["domain"], cookie["path"]


def parse_cookies(text: str, format: CookieFormat, *, now: float | None = None) -> CookieBatch:
    try:
        input_size = len(text.encode("utf-8"))
    except UnicodeError:
        raise CookieTransferError("Cookie input contains invalid Unicode") from None
    if input_size > MAX_BYTES:
        raise CookieTransferError("Cookie input exceeds 2 MiB")
    text = text.removeprefix("\ufeff")
    if format == "json":
        try:
            items = json.loads(text)
        except (ValueError, RecursionError):
            raise CookieTransferError("Cookie JSON must be an array of cookie objects") from None
        if not isinstance(items, list):
            raise CookieTransferError("Cookie JSON must be an array of cookie objects")
        rows = list(enumerate(items, 1))
    elif format == "netscape":
        rows = []
        for number, line in enumerate(text.splitlines(), 1):
            if not line.strip() or (line.startswith("#") and not line.startswith("#HttpOnly_")):
                continue
            fields = line.split("\t")
            if len(fields) != 7:
                _fail(number, "Netscape cookies require seven tab-separated fields")
            domain, include_subdomains, path, secure, expiry, name, value = fields
            http_only = domain.startswith("#HttpOnly_")
            domain = domain.removeprefix("#HttpOnly_")
            if include_subdomains.upper() not in ("TRUE", "FALSE") or secure.upper() not in (
                "TRUE",
                "FALSE",
            ):
                _fail(number, "Netscape flags must be TRUE or FALSE")
            if len(expiry) > 12 or not re.fullmatch(r"[0-9]+", expiry):
                _fail(number, "Netscape expiry must be a nonnegative Unix timestamp")
            rows.append(
                (
                    number,
                    {
                        "domain": domain,
                        "hostOnly": include_subdomains.upper() == "FALSE",
                        "path": path,
                        "secure": secure.upper() == "TRUE",
                        "httpOnly": http_only,
                        "expires": -1 if expiry == "0" else int(expiry),
                        "name": name,
                        "value": value,
                    },
                )
            )
    else:
        raise CookieTransferError("Select JSON or Netscape cookie format")
    if len(rows) > MAX_COOKIES:
        raise CookieTransferError("Import at most 10000 cookies")
    normalized = [(row, _normalize(item, row)) for row, item in rows]
    clock = time.time() if now is None else now
    selected: dict[tuple[str, str, str], tuple[int, dict[str, Any]]] = {}
    skipped = []
    for row, cookie in normalized:
        if cookie["expires"] != -1 and cookie["expires"] <= clock:
            skipped.append({"row": row, "reason": "Expired cookie"})
            continue
        key = _key(cookie)
        if key in selected:
            skipped.append(
                {"row": selected[key][0], "reason": "Duplicate cookie; later entry wins"}
            )
        selected[key] = row, cookie
    return CookieBatch([cookie for _, cookie in selected.values()], len(rows), skipped)


def serialize_cookies(cookies: list[dict[str, Any]], format: CookieFormat) -> str:
    if format == "json":
        return json.dumps(cookies, ensure_ascii=False, indent=2)
    lines = [
        "# Netscape HTTP Cookie File",
        "# Cookie values are sensitive. SameSite is not represented in this format.",
    ]
    for cookie in cookies:
        if any(
            "\t" in str(cookie[field]) or "\n" in str(cookie[field])
            for field in ("domain", "path", "name", "value")
        ):
            raise CookieTransferError("A cookie cannot be represented in Netscape format; use JSON")
        domain = ("#HttpOnly_" if cookie["httpOnly"] else "") + cookie["domain"]
        lines.append(
            "\t".join(
                [
                    domain,
                    "TRUE" if cookie["domain"].startswith(".") else "FALSE",
                    cookie["path"],
                    "TRUE" if cookie["secure"] else "FALSE",
                    "0" if cookie["expires"] == -1 else str(int(cookie["expires"])),
                    cookie["name"],
                    cookie["value"],
                ]
            )
        )
    return "\n".join(lines) + "\n"


def _read_vault(vault_path: Path) -> list[dict[str, Any]]:
    if not vault_path.exists():
        return []
    reject_reparse_paths(vault_path)
    raw = vault_path.read_text(encoding="utf-8")
    if not raw.strip():
        return []
    return parse_cookies(raw, "json", now=0).cookies


def _write_vault_atomic(vault_path: Path, cookies: list[dict[str, Any]]) -> None:
    vault_path.parent.mkdir(parents=True, exist_ok=True)
    reject_reparse_paths(vault_path)
    encoded = json.dumps(cookies, ensure_ascii=False, indent=2).encode("utf-8")
    if len(encoded) > MAX_BYTES:
        raise CookieTransferError("Cookie vault exceeds 2 MiB; import a smaller batch")
    with tempfile.NamedTemporaryFile(
        dir=vault_path.parent, prefix=".cookies-", delete=False
    ) as handle:
        tmp_path = Path(handle.name)
        try:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
            protect_path(tmp_path)
            tmp_path.replace(vault_path)
        finally:
            tmp_path.unlink(missing_ok=True)


class CookieTransfer:
    def __init__(self, manager: Any, backups: Any = None):
        self.manager = manager
        self.backups = backups

    async def preview(self, profile_id: str, text: str, format: CookieFormat) -> dict[str, Any]:
        if await self.manager.get_profile(profile_id) is None:
            raise CookieTransferError("Profile not found")
        return parse_cookies(text, format).preview()

    async def import_cookies(
        self,
        profile_id: str,
        text: str,
        format: CookieFormat,
        mode: Literal["merge", "replace"],
        row_version: int,
        confirm_replace: bool = False,
    ) -> dict[str, Any]:
        batch = parse_cookies(text, format)
        if mode not in ("merge", "replace"):
            raise CookieTransferError("Select merge or replace cookie import behavior")
        if mode == "replace" and not confirm_replace:
            raise CookieTransferError("Confirm replacement before importing cookies")
        holder = make_lease_holder()
        async with profile_operation(self.manager, profile_id, holder=holder) as profile:
            if profile.row_version != row_version:
                raise StaleWriteError(profile_id, row_version)
            vault = profile_data_path(self.manager, profile).resolve() / _VAULT_NAME
            existing = await run_file_operation(_read_vault, vault)
            merged_map = {} if mode == "replace" else {_key(cookie): cookie for cookie in existing}
            for cookie in batch.cookies:
                merged_map[_key(cookie)] = cookie
            final_cookies = list(merged_map.values())
            with self.manager.storage.db._connection as connection:
                changed = connection.execute(
                    "UPDATE profiles SET row_version=row_version+1, updated_at=? "
                    "WHERE id=? AND row_version=? AND deleted_at IS NULL AND locked_by=?",
                    (datetime.now().isoformat(), profile_id, row_version, holder),
                )
                if not changed.rowcount:
                    raise StaleWriteError(profile_id, row_version)
            await run_file_operation(_write_vault_atomic, vault, final_cookies)
            return {
                "imported": len(batch.cookies),
                "skipped": batch.skipped,
                "session_count": batch.preview()["session_count"],
                "row_version": row_version + 1,
                "backup_id": None,
            }

    async def export_cookies(self, profile_id: str, format: CookieFormat, row_version: int) -> str:
        async with profile_operation(self.manager, profile_id) as profile:
            if profile.row_version != row_version:
                raise StaleWriteError(profile_id, row_version)
            vault = profile_data_path(self.manager, profile).resolve() / _VAULT_NAME
            cookies = await run_file_operation(_read_vault, vault)
            current = await self.manager.get_profile(profile_id)
            if current is None or current.row_version != row_version:
                raise StaleWriteError(profile_id, row_version)
            return serialize_cookies(cookies, format)
