"""Server-side subscription fetcher, sanitizer, and node filter for Prism Cloud Server.

All upstream airport URLs, Clash YAML payloads, and manual proxy lists are resolved
strictly on the cloud server by reusing :mod:`camoufox_pm.core.network_subscription`
(including its SSRF-guarded socket backend and strict outbound whitelist) and
:mod:`camoufox_pm.core.network_uri`.

Clients only ever receive clean ``sealed_nodes`` outbound dicts — upstream subscription
URLs and informational traffic/expiry pseudo-nodes are stripped on the server.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from camoufox_pm.core.network_subscription import (
    FetchedSubscription,
    ParsedNode,
    SubscriptionError,
    fetch_subscription,
    is_informational,
    mask_url,
    node_key,
    parse_subscription,
    validate_node,
    validate_subscription_url,
)
from camoufox_pm.core.network_uri import parse_manual_text

FetcherCallable = Callable[[str], Awaitable[FetchedSubscription | bytes | str]]

_KEYWORD_SPLIT_RE = re.compile(r"[|,，\r\n]+")


class ResolveError(ValueError):
    """Raised when an upstream subscription source cannot be fetched or parsed."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass
class ResolvedSourceResult:
    """Result of resolving a ``url``, ``content``, or ``manual`` subscription source."""

    nodes: list[dict[str, Any]]
    warnings: list[dict[str, Any]] = field(default_factory=list)
    suggested_name: str | None = None


def outbound_from_parsed(node: ParsedNode) -> dict[str, Any]:
    """Convert a validated :class:`ParsedNode` into a clean Clash/Mihomo outbound dict."""
    return {**node.config, "name": node.name, "type": node.protocol}


def annotate_node_keys(nodes: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    """Compute deterministic per-subscription node keys matching ``network_subscription``."""
    seen: dict[str, int] = {}
    annotated: list[tuple[str, dict[str, Any]]] = []
    for raw in nodes:
        parsed = validate_node(raw)
        base_key = node_key(parsed)
        seen[base_key] = seen.get(base_key, 0) + 1
        full_key = base_key if seen[base_key] == 1 else f"{base_key}#{seen[base_key]}"
        annotated.append((full_key, outbound_from_parsed(parsed)))
    return annotated


def summarize_nodes(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build secret-free node preview summaries for the admin UI and API."""
    summaries: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    for raw in nodes:
        try:
            parsed = validate_node(raw)
        except ValueError:
            continue
        base_key = node_key(parsed)
        seen[base_key] = seen.get(base_key, 0) + 1
        full_key = base_key if seen[base_key] == 1 else f"{base_key}#{seen[base_key]}"
        summaries.append(
            {
                "key": full_key,
                "name": parsed.name,
                "type": parsed.protocol,
                "server": parsed.server,
                "port": parsed.port,
                "insecure": parsed.insecure,
            }
        )
    return summaries


def _normalize_string_list(value: Any, *, split_delimiters: bool = False) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = _KEYWORD_SPLIT_RE.split(value) if split_delimiters else [value]
        return [item.strip() for item in parts if item.strip()]
    if isinstance(value, list):
        cleaned: list[str] = []
        for item in value:
            if not isinstance(item, str):
                raise ValueError("Node filter items must be strings")
            stripped = item.strip()
            if stripped:
                cleaned.append(stripped)
        return cleaned
    raise ValueError("Node filter entries must be a list of strings")


def normalize_node_filter(node_filter: dict[str, Any] | None) -> dict[str, list[str]]:
    """Validate and normalize a ``node_filter_json`` structure."""
    if not node_filter:
        return {"include_keywords": [], "exclude_keywords": [], "node_keys": []}
    if not isinstance(node_filter, dict):
        raise ValueError("node_filter must be a JSON object")
    allowed_keys = {"include_keywords", "exclude_keywords", "node_keys"}
    unknown = set(node_filter.keys()) - allowed_keys
    if unknown:
        raise ValueError(f"Unsupported node_filter keys: {', '.join(sorted(unknown))}")
    return {
        "include_keywords": _normalize_string_list(
            node_filter.get("include_keywords"), split_delimiters=True
        ),
        "exclude_keywords": _normalize_string_list(
            node_filter.get("exclude_keywords"), split_delimiters=True
        ),
        "node_keys": _normalize_string_list(node_filter.get("node_keys"), split_delimiters=False),
    }


def filter_nodes(
    nodes: list[dict[str, Any]],
    node_filter: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Filter sanitized outbound dicts using ``node_keys``, ``include_keywords``, and ``exclude_keywords``."""
    if not nodes:
        return []
    normalized = normalize_node_filter(node_filter)
    include_kw = [kw.casefold() for kw in normalized["include_keywords"]]
    exclude_kw = [kw.casefold() for kw in normalized["exclude_keywords"]]
    allowed_keys = set(normalized["node_keys"])

    if not include_kw and not exclude_kw and not allowed_keys:
        return list(nodes)

    annotated = annotate_node_keys(nodes)
    result: list[dict[str, Any]] = []
    for full_key, outbound in annotated:
        base_key = full_key.partition("#")[0]
        if allowed_keys and full_key not in allowed_keys and base_key not in allowed_keys:
            continue
        name_lower = str(outbound.get("name") or "").casefold()
        if include_kw and not any(kw in name_lower for kw in include_kw):
            continue
        if exclude_kw and any(kw in name_lower for kw in exclude_kw):
            continue
        result.append(outbound)
    return result


def mask_source_payload(kind: str, payload: dict[str, Any]) -> str:
    """Return a non-sensitive preview label for an upstream source payload."""
    if kind == "url":
        return mask_url(str(payload.get("url") or ""))
    if kind == "content":
        content = str(payload.get("content") or "")
        size_kb = max(1, (len(content.encode("utf-8")) + 1023) // 1024)
        return f"订阅配置文本 (~{size_kb} KB)"
    if kind == "manual":
        text = str(payload.get("text") or "")
        if not text and isinstance(payload.get("manual_nodes"), list):
            text = "\n".join(str(line) for line in payload["manual_nodes"])
        lines = [
            line for line in text.splitlines() if line.strip() and not line.strip().startswith("#")
        ]
        return f"手动 / 静态节点 ({len(lines)} 行)"
    return kind


async def resolve_source(
    kind: str,
    payload: dict[str, Any] | str | list[str],
    *,
    fetcher: FetcherCallable | None = None,
    default_manual_type: str = "http",
) -> ResolvedSourceResult:
    """Resolve an upstream ``url``, ``content``, or ``manual`` source into clean outbound dicts."""
    clean_kind = (kind or "").strip().lower()
    try:
        if clean_kind == "url":
            raw_url = str(payload.get("url") or "") if isinstance(payload, dict) else str(payload)
            validated_url = validate_subscription_url(raw_url)
            suggested_name: str | None = None
            if fetcher is not None:
                fetched_any = await fetcher(validated_url)
                if isinstance(fetched_any, FetchedSubscription):
                    content: bytes | str = fetched_any.content
                    suggested_name = fetched_any.suggested_name
                else:
                    content = fetched_any
            else:
                fetched = await fetch_subscription(validated_url)
                content = fetched.content
                suggested_name = fetched.suggested_name
            parsed = parse_subscription(content)
            outbounds = [outbound_from_parsed(n) for n in parsed.nodes if not n.informational]
            if not outbounds:
                raise ResolveError("no_valid_proxies", "未解析出任何可用节点。")
            return ResolvedSourceResult(
                nodes=outbounds,
                warnings=parsed.warnings,
                suggested_name=suggested_name or parsed.suggested_name,
            )

        if clean_kind == "content":
            raw_content = (
                str(payload.get("content") or "") if isinstance(payload, dict) else str(payload)
            )
            parsed = parse_subscription(raw_content)
            outbounds = [outbound_from_parsed(n) for n in parsed.nodes if not n.informational]
            if not outbounds:
                raise ResolveError("no_valid_proxies", "未解析出任何可用节点。")
            return ResolvedSourceResult(
                nodes=outbounds,
                warnings=parsed.warnings,
                suggested_name=parsed.suggested_name,
            )

        if clean_kind == "manual":
            resolved_type = default_manual_type
            if isinstance(payload, dict):
                resolved_type = str(payload.get("default_type") or default_manual_type)
                text = str(payload.get("text") or "")
                if not text and isinstance(payload.get("manual_nodes"), list):
                    text = "\n".join(str(item) for item in payload["manual_nodes"])
            elif isinstance(payload, list):
                text = "\n".join(str(item) for item in payload)
            else:
                text = str(payload)

            if not text.strip():
                raise ResolveError("empty", "节点列表不能为空。")
            entries = parse_manual_text(text, default_type=resolved_type)
            outbounds = []
            warnings: list[dict[str, Any]] = []
            for index, mapping, failure in entries:
                if failure is not None:
                    warnings.append({"index": index, "reason": failure})
                    continue
                try:
                    node = validate_node(mapping)
                except ValueError as exc:
                    warnings.append({"index": index, "reason": str(exc)})
                    continue
                if is_informational(node.name):
                    continue
                outbounds.append(outbound_from_parsed(node))
            if not outbounds:
                raise ResolveError("no_valid_proxies", "未解析出任何可用节点。")
            return ResolvedSourceResult(nodes=outbounds, warnings=warnings)

        raise ResolveError("invalid_kind", f"不支持的上游源类型: {clean_kind}")
    except ResolveError:
        raise
    except SubscriptionError as exc:
        raise ResolveError(exc.code, str(exc)) from None
