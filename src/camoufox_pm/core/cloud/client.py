"""SSRF-safe HTTPS client for the Prism Browser cloud configuration protocol."""

from __future__ import annotations

import ipaddress
import json
import ssl
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Generic, TypeVar
from urllib.parse import quote, urlsplit

import httpcore
import httpx
from pydantic import ValidationError

from camoufox_pm.core.network_subscription import (
    GuardedBackend,
    Resolver,
    SubscriptionError,
    address_is_public,
)

from .protocol import (
    CloudIsolationPolicyPayload,
    CloudManifest,
    CloudProfilesListPayload,
    CloudProfileSnapshot,
    CloudSubscriptionsPayload,
)

MAX_CLOUD_RESPONSE_BYTES = 10 * 1024 * 1024
MAX_CLOUD_ERROR_BYTES = 64 * 1024
DEFAULT_CLOUD_TIMEOUT = 15.0
CLOUD_USER_AGENT = "Prism-Browser/1.0"

T = TypeVar("T")


def _safe_header_value(value: str | None, *, limit: int = 128) -> str | None:
    """Return an HTTP-header-safe ASCII string, percent-encoding non-ASCII text."""
    cleaned = " ".join((value or "").replace("\r", " ").replace("\n", " ").split())[:limit]
    if not cleaned:
        return None
    if cleaned.isascii():
        return cleaned
    return quote(cleaned, safe=" -_.~:@()[]")[: limit * 3]


def _parse_auth_error_body(
    raw_bytes: bytes, status: int, *, has_token: bool
) -> tuple[str, str, dict[str, Any]]:
    """Extract structured ``(code, message, details)`` from a 401/403 JSON response."""
    default_code = "unauthorized" if status == 401 else "forbidden"
    default_message = (
        "Cloud server rejected the authentication token."
        if status == 401
        else "Cloud server denied access for this account or device."
    )
    parsed: Any = None
    if raw_bytes:
        try:
            parsed = json.loads(raw_bytes.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            parsed = None

    if not isinstance(parsed, dict):
        return default_code, default_message, {}

    detail_obj = parsed.get("detail") if isinstance(parsed.get("detail"), dict) else None
    raw_code = parsed.get("code")
    if not isinstance(raw_code, str) or not raw_code.strip():
        raw_code = detail_obj.get("code") if detail_obj else None

    raw_message = parsed.get("message")
    if not isinstance(raw_message, str) or not raw_message.strip():
        if (
            detail_obj
            and isinstance(detail_obj.get("message"), str)
            and detail_obj["message"].strip()
        ):
            raw_message = detail_obj["message"]
        elif isinstance(parsed.get("detail"), str) and parsed["detail"].strip():
            raw_message = parsed["detail"]
        else:
            raw_message = None

    details: dict[str, Any] = {}
    if detail_obj:
        for k, v in detail_obj.items():
            if k not in {"code", "message"}:
                details[k] = v
    if isinstance(parsed.get("details"), dict):
        details.update(parsed["details"])
    for k, v in parsed.items():
        if k not in {"code", "message", "detail", "details"}:
            details[k] = v

    extracted_code = raw_code.strip() if isinstance(raw_code, str) and raw_code.strip() else None
    # Preserve backwards-compatible 'unauthorized' when no Bearer token was sent at all.
    if (
        status == 401
        and not has_token
        and extracted_code in {None, "invalid_token", "missing_token"}
    ):
        final_code = "unauthorized"
    else:
        final_code = extracted_code or default_code

    final_message = (
        raw_message.strip()
        if isinstance(raw_message, str) and raw_message.strip()
        else default_message
    )
    return final_code, final_message, details


class CloudClientError(RuntimeError):
    """Raised when a cloud HTTP request fails validation, network, or protocol checks."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.status_code = status_code
        self.details: dict[str, Any] = dict(details) if isinstance(details, dict) else {}
        super().__init__(message)


@dataclass
class CloudResponse(Generic[T]):
    """Parsed cloud response alongside ETag and raw JSON payload."""

    data: T | None
    raw: dict[str, Any] | None = None
    etag: str | None = None
    not_modified: bool = False


def is_local_host(host: str) -> bool:
    """Return True if ``host`` names loopback (``localhost``, ``127.0.0.1``, ``::1``, etc.)."""
    clean = host.strip().strip("[]").lower()
    if clean == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(clean.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return bool(ip.is_loopback)


def address_allowed(address: str, *, allow_local: bool = False) -> bool:
    """Whether a resolved IP address may be dialed by the cloud client."""
    if address_is_public(address):
        return True
    if not allow_local:
        return False
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return bool(ip.is_loopback)


def validate_cloud_base_url(url: str, *, allow_local: bool = False) -> str:
    """Validate and normalize a cloud server base URL before any network access."""
    cleaned = (url or "").strip().rstrip("/")
    if not cleaned or len(cleaned) > 2048 or any(ch.isspace() or ord(ch) < 32 for ch in cleaned):
        raise CloudClientError("invalid_url", "Enter a valid cloud server URL.")
    try:
        parts = urlsplit(cleaned)
        port = parts.port
    except ValueError:
        raise CloudClientError("invalid_url", "Enter a valid cloud server URL.") from None
    scheme = parts.scheme.lower()
    host = parts.hostname or ""
    if not host or parts.username or parts.password or parts.query or parts.fragment:
        raise CloudClientError(
            "invalid_url",
            "Cloud server URL must not contain credentials, query strings, or fragments.",
        )
    if port is not None and not 1 <= port <= 65535:
        raise CloudClientError("invalid_url", "Cloud server URL has an invalid port.")
    if scheme == "https":
        pass
    elif scheme == "http" and allow_local and is_local_host(host):
        pass
    else:
        raise CloudClientError(
            "https_required",
            "Cloud server URL must use HTTPS (enable CPM_CLOUD_ALLOW_LOCAL=1 for http://127.0.0.1 / localhost).",
        )
    if host.lower() == "localhost" and not allow_local:
        raise CloudClientError(
            "blocked_address",
            "Localhost cloud URLs require CPM_CLOUD_ALLOW_LOCAL=1.",
        )
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        pass
    else:
        if not address_allowed(host.strip("[]"), allow_local=allow_local):
            raise CloudClientError(
                "blocked_address",
                "Cloud server URL points to a private, local, or reserved address.",
            )
    return cleaned


class _HttpcoreAsyncByteStream(httpx.AsyncByteStream):
    def __init__(self, response: httpcore.Response) -> None:
        self._response = response

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._response.aiter_stream():
            yield chunk

    async def aclose(self) -> None:
        await self._response.aclose()


class _GuardedHTTPTransport(httpx.AsyncBaseTransport):
    """httpx transport backed by ``httpcore.AsyncConnectionPool`` and :class:`GuardedBackend`."""

    def __init__(
        self,
        *,
        allow_local: bool = False,
        resolver: Resolver | None = None,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        policy = lambda addr: address_allowed(addr, allow_local=allow_local)  # noqa: E731
        backend = (
            GuardedBackend(resolver=resolver, policy=policy)
            if resolver is not None
            else GuardedBackend(policy=policy)
        )
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl_context or httpx.create_ssl_context(),
            network_backend=backend,
            max_connections=4,
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        hc_request = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=body,
            extensions=request.extensions,
        )
        try:
            hc_response = await self._pool.handle_async_request(hc_request)
        except SubscriptionError as exc:
            raise CloudClientError(exc.code, str(exc)) from None
        except ssl.SSLError:
            raise CloudClientError(
                "tls_failed",
                "The cloud server's TLS certificate could not be verified.",
            ) from None
        except (httpcore.TimeoutException, TimeoutError):
            raise CloudClientError("timeout", "The cloud server did not answer in time.") from None
        except (httpcore.NetworkError, httpcore.ProtocolError, OSError) as exc:
            raise CloudClientError(
                "connection_failed",
                f"The cloud server could not be reached ({type(exc).__name__}).",
            ) from None
        return httpx.Response(
            status_code=hc_response.status,
            headers=hc_response.headers,
            stream=_HttpcoreAsyncByteStream(hc_response),
            extensions=hc_response.extensions,
        )

    async def aclose(self) -> None:
        await self._pool.aclose()


class CloudClient:
    """Bounded, SSRF-safe async HTTP client for the Prism cloud protocol."""

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        device_id: str | None = None,
        device_label: str | None = None,
        client_version: str | None = None,
        allow_local: bool = False,
        timeout: float = DEFAULT_CLOUD_TIMEOUT,
        max_bytes: int = MAX_CLOUD_RESPONSE_BYTES,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver | None = None,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self.base_url = validate_cloud_base_url(base_url, allow_local=allow_local)
        self._token = (token or "").strip() or None
        self._device_id = _safe_header_value(device_id, limit=128)
        self._device_label = _safe_header_value(device_label, limit=64)
        self._client_version = _safe_header_value(client_version, limit=64)
        self.allow_local = allow_local
        self.timeout = timeout
        self.max_bytes = max_bytes
        self._transport = transport
        self._resolver = resolver
        self._ssl_context = ssl_context

    def _headers(self, etag: str | None = None) -> dict[str, str]:
        headers: dict[str, str] = {
            "Accept": "application/json",
            "User-Agent": CLOUD_USER_AGENT,
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        if self._device_id:
            headers["X-Prism-Device-Id"] = self._device_id
        if self._device_label:
            headers["X-Prism-Device-Label"] = self._device_label
        if self._client_version:
            headers["X-Prism-Client-Version"] = self._client_version
        if etag:
            headers["If-None-Match"] = etag
        return headers

    def _build_transport(self) -> tuple[httpx.AsyncBaseTransport, bool]:
        if self._transport is not None:
            return self._transport, False
        return (
            _GuardedHTTPTransport(
                allow_local=self.allow_local,
                resolver=self._resolver,
                ssl_context=self._ssl_context,
            ),
            True,
        )

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        etag: str | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str | None, bool]:
        url = f"{self.base_url}{path}"
        transport, owns_transport = self._build_transport()
        try:
            async with httpx.AsyncClient(
                transport=transport,
                timeout=self.timeout,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                try:
                    async with client.stream(
                        method,
                        url,
                        headers=self._headers(etag=etag),
                        json=json_body,
                    ) as response:
                        status = response.status_code
                        response_etag = response.headers.get("etag")
                        if status == 304:
                            return None, response_etag or etag, True
                        if status in {301, 302, 303, 307, 308}:
                            raise CloudClientError(
                                "redirect_blocked",
                                "Cloud server redirects are not followed.",
                                status_code=status,
                            )
                        if status in {401, 403}:
                            err_buf = bytearray()
                            async for chunk in response.aiter_bytes():
                                err_buf.extend(chunk)
                                if len(err_buf) >= MAX_CLOUD_ERROR_BYTES:
                                    break
                            err_code, err_msg, err_details = _parse_auth_error_body(
                                bytes(err_buf),
                                status,
                                has_token=bool(self._token),
                            )
                            raise CloudClientError(
                                err_code,
                                err_msg,
                                status_code=status,
                                details=err_details,
                            )
                        if status == 404:
                            raise CloudClientError(
                                "not_found",
                                "Requested resource was not found on the cloud server.",
                                status_code=404,
                            )
                        if status >= 400:
                            raise CloudClientError(
                                "http_status",
                                f"Cloud server returned HTTP {status}.",
                                status_code=status,
                            )
                        declared = response.headers.get("content-length", "")
                        if declared.isdigit() and int(declared) > self.max_bytes:
                            raise CloudClientError(
                                "too_large",
                                f"Cloud server response exceeds {self.max_bytes} bytes.",
                            )
                        buf = bytearray()
                        async for chunk in response.aiter_bytes():
                            buf.extend(chunk)
                            if len(buf) > self.max_bytes:
                                raise CloudClientError(
                                    "too_large",
                                    f"Cloud server response exceeds {self.max_bytes} bytes.",
                                )
                except CloudClientError:
                    raise
                except httpx.TimeoutException:
                    raise CloudClientError(
                        "timeout", "The cloud server did not answer in time."
                    ) from None
                except httpx.HTTPError as exc:
                    raise CloudClientError(
                        "connection_failed",
                        f"The cloud server could not be reached ({type(exc).__name__}).",
                    ) from None
        finally:
            if owns_transport:
                await transport.aclose()

        try:
            parsed = json.loads(bytes(buf).decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise CloudClientError(
                "invalid_json", "Cloud server returned malformed JSON."
            ) from None
        if not isinstance(parsed, dict):
            raise CloudClientError("invalid_json", "Cloud server response must be a JSON object.")
        return parsed, response_etag, False

    async def get_manifest(self, *, etag: str | None = None) -> CloudResponse[CloudManifest]:
        raw, response_etag, not_modified = await self._request_json(
            "GET", "/v1/manifest", etag=etag
        )
        if not_modified or raw is None:
            return CloudResponse(data=None, raw=None, etag=response_etag, not_modified=True)
        try:
            model = CloudManifest.model_validate(raw)
        except ValidationError as exc:
            raise CloudClientError(
                "invalid_manifest", f"Invalid cloud manifest payload: {exc.errors()[0]['msg']}"
            ) from None
        return CloudResponse(data=model, raw=raw, etag=response_etag, not_modified=False)

    async def get_isolation_policy(
        self, *, etag: str | None = None
    ) -> CloudResponse[CloudIsolationPolicyPayload]:
        raw, response_etag, not_modified = await self._request_json(
            "GET", "/v1/isolation-policy", etag=etag
        )
        if not_modified or raw is None:
            return CloudResponse(data=None, raw=None, etag=response_etag, not_modified=True)
        try:
            model = CloudIsolationPolicyPayload.model_validate(raw)
        except ValidationError as exc:
            raise CloudClientError(
                "invalid_policy_payload",
                f"Invalid isolation policy payload: {exc.errors()[0]['msg']}",
            ) from None
        return CloudResponse(data=model, raw=raw, etag=response_etag, not_modified=False)

    async def get_subscriptions(
        self, *, etag: str | None = None
    ) -> CloudResponse[CloudSubscriptionsPayload]:
        raw, response_etag, not_modified = await self._request_json(
            "GET", "/v1/subscriptions", etag=etag
        )
        if not_modified or raw is None:
            return CloudResponse(data=None, raw=None, etag=response_etag, not_modified=True)
        try:
            model = CloudSubscriptionsPayload.model_validate(raw)
        except ValidationError as exc:
            raise CloudClientError(
                "invalid_subscriptions_payload",
                f"Invalid subscriptions payload: {exc.errors()[0]['msg']}",
            ) from None
        return CloudResponse(data=model, raw=raw, etag=response_etag, not_modified=False)

    async def list_profiles(self) -> CloudProfilesListPayload:
        raw, _, _ = await self._request_json("GET", "/v1/profiles")
        assert raw is not None
        try:
            return CloudProfilesListPayload.model_validate(raw)
        except ValidationError as exc:
            raise CloudClientError(
                "invalid_profiles_payload",
                f"Invalid profiles payload: {exc.errors()[0]['msg']}",
            ) from None

    async def get_profile(self, profile_id: str) -> CloudProfileSnapshot:
        safe_id = quote(profile_id, safe="")
        raw, _, _ = await self._request_json("GET", f"/v1/profiles/{safe_id}")
        assert raw is not None
        try:
            return CloudProfileSnapshot.model_validate(raw)
        except ValidationError as exc:
            raise CloudClientError(
                "invalid_profile_snapshot",
                f"Invalid profile snapshot: {exc.errors()[0]['msg']}",
            ) from None

    async def put_profile(
        self, profile_id: str, snapshot: CloudProfileSnapshot
    ) -> CloudProfileSnapshot:
        safe_id = quote(profile_id, safe="")
        raw, _, _ = await self._request_json(
            "PUT",
            f"/v1/profiles/{safe_id}",
            json_body=snapshot.model_dump(mode="json"),
        )
        assert raw is not None
        try:
            return CloudProfileSnapshot.model_validate(raw)
        except ValidationError as exc:
            raise CloudClientError(
                "invalid_profile_snapshot",
                f"Invalid profile snapshot response: {exc.errors()[0]['msg']}",
            ) from None

    async def delete_profile(self, profile_id: str) -> bool:
        safe_id = quote(profile_id, safe="")
        raw, _, _ = await self._request_json("DELETE", f"/v1/profiles/{safe_id}")
        return bool(raw and raw.get("deleted"))
