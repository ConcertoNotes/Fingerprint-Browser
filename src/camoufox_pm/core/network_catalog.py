"""Subscriptions and their nodes: sealed storage, atomic sync, measured exits.

Node configurations, subscription URLs and provider keys are sealed with the
manager's encryption key and never leave this module except as a runtime
configuration handed to Mihomo. Metadata reads never decrypt.

A sync is all-or-nothing and additive: a node keeps its ID (and therefore its
bindings and measurements) for as long as the subscription keeps listing it;
one that disappears is *retired*, never deleted, so a profile bound to it fails
loudly at its next launch instead of moving to another exit. A failed fetch or
parse leaves every stored node exactly as it was.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from camoufox_pm.config import get_settings

from .crypto import decrypt, encrypt
from .network_subscription import (
    ParsedSubscription,
    SubscriptionError,
    fetch_subscription,
    parse_subscription,
    validate_subscription_url,
)

STALE_AFTER = timedelta(hours=24)
# invalid_reason for panel entries that only carry account information.
INFORMATIONAL = "informational entry"
_ID_ALPHABET = "abcdefghijkmnpqrstuvwxyz23456789"


class NetworkSecretsUnavailable(RuntimeError):
    """Node and subscription secrets must never be stored or used in clear text."""


class NetworkNotFound(LookupError):
    pass


class NodeInUse(RuntimeError):
    def __init__(self, count: int):
        self.count = count
        super().__init__(f"{count} profile(s) are bound to this node")


class SubscriptionHasBindings(RuntimeError):
    def __init__(self, count: int):
        self.count = count
        super().__init__(f"{count} profile(s) are bound to nodes of this subscription")


def new_id(prefix: str) -> str:
    return prefix + "".join(secrets.choice(_ID_ALPHABET) for _ in range(12))


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def seal(value: str) -> str:
    if not get_settings().secret_key:
        raise NetworkSecretsUnavailable(
            "An encryption key is required to store subscriptions and nodes. "
            "Restore secret.key or configure CPM_SECRET_KEY."
        )
    try:
        return encrypt(value)
    except (ValueError, TypeError):
        raise NetworkSecretsUnavailable("The configured encryption key is invalid.") from None


def unseal(value: str) -> str:
    try:
        clear = decrypt(value)
    except (ValueError, TypeError):
        raise NetworkSecretsUnavailable("The configured encryption key is invalid.") from None
    if not value.startswith("enc:") or clear == value:
        raise NetworkSecretsUnavailable(
            "Stored node secrets could not be decrypted. Restore the original "
            "secret.key or CPM_SECRET_KEY."
        )
    return clear


def _check_public(row: Any) -> dict[str, Any] | None:
    if row is None or row["checked_at"] is None:
        return None
    checked = datetime.fromisoformat(row["checked_at"])
    return {
        "health": row["health"],
        "exit_ip": row["exit_ip"],
        "country_iso": row["country_iso"],
        "city": row["city"],
        "region": row["region"],
        "latitude": row["latitude"],
        "longitude": row["longitude"],
        "timezone": row["timezone"],
        "precision": row["precision"],
        "location_source": row["location_source"],
        "latency_ms": row["latency_ms"],
        "error_code": row["error_code"],
        "error": row["error"],
        "checked_at": row["checked_at"],
        "stale": datetime.now(timezone.utc) - checked > STALE_AFTER,
    }


_NODE_SELECT = """
SELECT n.id, n.subscription_id, n.display_name, n.protocol, n.entry, n.status,
       n.invalid_reason, n.position, n.created_at, n.updated_at, n.retired_at,
       s.name AS subscription_name,
       c.health, c.exit_ip, c.country_iso, c.city, c.region, c.latitude, c.longitude,
       c.timezone, c.precision, c.location_source, c.latency_ms, c.error_code, c.error,
       c.checked_at,
       (SELECT COUNT(*) FROM profile_network_bindings b WHERE b.node_id = n.id) AS profile_count,
       (SELECT COUNT(*) FROM network_node_checks o
          WHERE c.exit_ip IS NOT NULL AND o.exit_ip = c.exit_ip) AS shared_exit_count,
       i.data AS intel, i.checked_at AS intel_checked_at
FROM network_nodes n
JOIN network_subscriptions s ON s.id = n.subscription_id
LEFT JOIN network_node_checks c ON c.node_id = n.id
LEFT JOIN network_ip_intel i ON i.ip = c.exit_ip
"""


def _node_public(row: Any) -> dict[str, Any]:
    intel = json.loads(row["intel"]) if row["intel"] else None
    return {
        "id": row["id"],
        "subscription_id": row["subscription_id"],
        "subscription_name": row["subscription_name"],
        "name": row["display_name"],
        "protocol": row["protocol"],
        "entry": row["entry"],
        "status": row["status"],
        "invalid_reason": row["invalid_reason"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "retired_at": row["retired_at"],
        "profile_count": row["profile_count"],
        "shared_exit_count": row["shared_exit_count"],
        "check": _check_public(row),
        "intel": intel,
        "intel_checked_at": row["intel_checked_at"],
    }


class NetworkCatalog:
    def __init__(self, manager: Any):
        self.manager = manager
        self.connection = manager.storage.db._connection

    # --- Subscriptions ---------------------------------------------------

    def _subscription_row(self, subscription_id: str, include_deleted: bool = False):
        row = self.connection.execute(
            "SELECT * FROM network_subscriptions WHERE id = ?"
            + ("" if include_deleted else " AND deleted_at IS NULL"),
            (subscription_id,),
        ).fetchone()
        if row is None:
            raise NetworkNotFound("Subscription not found")
        return row

    def _subscription_public(self, row: Any) -> dict[str, Any]:
        counts = dict(
            self.connection.execute(
                "SELECT status, COUNT(*) FROM network_nodes WHERE subscription_id = ? "
                "GROUP BY status",
                (row["id"],),
            ).fetchall()
        )
        bound = self.connection.execute(
            "SELECT COUNT(*) FROM profile_network_bindings b JOIN network_nodes n "
            "ON n.id = b.node_id WHERE n.subscription_id = ?",
            (row["id"],),
        ).fetchone()[0]
        return {
            "id": row["id"],
            "name": row["name"],
            "source_kind": row["source_kind"],
            # Host only: the path and query of a subscription URL are its token.
            "source_display": (
                f"https://{row['source_host']}/…" if row["source_host"] else row["source_file_name"]
            ),
            "manual": row["source_kind"] == "manual",
            "source_file_name": row["source_file_name"],
            "status": row["status"],
            "last_synced_at": row["last_synced_at"],
            "last_error_code": row["last_error_code"],
            "last_error": row["last_error"],
            "usage": json.loads(row["usage"]) if row["usage"] else None,
            "revision": row["revision"],
            "row_version": row["row_version"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "node_counts": {
                "active": counts.get("active", 0),
                "retired": counts.get("retired", 0),
                "invalid": counts.get("invalid", 0),
            },
            "bound_profiles": bound,
        }

    def list_subscriptions(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM network_subscriptions WHERE deleted_at IS NULL "
            "ORDER BY created_at DESC, id"
        ).fetchall()
        return [self._subscription_public(row) for row in rows]

    def get_subscription(self, subscription_id: str) -> dict[str, Any]:
        return self._subscription_public(self._subscription_row(subscription_id))

    async def create_subscription(
        self,
        name: str | None,
        *,
        url: str | None = None,
        content: bytes | None = None,
        file_name: str | None = None,
        fetch=fetch_subscription,
    ) -> dict[str, Any]:
        """Create from a URL or a file, importing it at once. Nothing is stored on failure."""
        if (url is None) == (content is None):
            raise SubscriptionError("invalid_source", "Provide either a URL or a file.")
        if url is not None:
            url = validate_subscription_url(url)
            sealed = seal(url)
            fetched = await fetch(url)
            parsed = parse_subscription(fetched.content)
            parsed.usage, parsed.suggested_name = fetched.usage, fetched.suggested_name
        else:
            seal("")  # refuse early, before parsing, when no key exists
            assert content is not None
            parsed = parse_subscription(content)
        from urllib.parse import urlsplit

        subscription_id = new_id("s")
        stamp = now()
        display = (
            (name or "").strip()[:80]
            or parsed.suggested_name
            or (urlsplit(url).hostname if url else (file_name or "Imported configuration"))
        )
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            self.connection.execute(
                "INSERT INTO network_subscriptions (id, name, source_kind, source_url_enc, "
                "source_host, source_file_name, status, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,'idle',?,?)",
                (
                    subscription_id,
                    display,
                    "url" if url else "file",
                    sealed if url else None,
                    urlsplit(url).hostname if url else None,
                    (file_name or "")[:128] or None if not url else None,
                    stamp,
                    stamp,
                ),
            )
            report = self._apply(subscription_id, parsed)
        return {"subscription": self.get_subscription(subscription_id), "report": report}

    async def sync_subscription(
        self, subscription_id: str, *, content: bytes | None = None, fetch=fetch_subscription
    ) -> dict[str, Any]:
        row = self._subscription_row(subscription_id)
        if row["source_kind"] == "manual":
            raise SubscriptionError(
                "manual_group", "Manual node groups have no source to refresh; edit their nodes."
            )
        try:
            if row["source_kind"] == "url":
                if content is not None:
                    raise SubscriptionError(
                        "invalid_source", "URL subscriptions refresh from their URL."
                    )
                url = unseal(row["source_url_enc"])
                fetched = await fetch(url)
                parsed = parse_subscription(fetched.content)
                parsed.usage = fetched.usage
            else:
                if content is None:
                    raise SubscriptionError(
                        "file_required", "Choose the updated file to refresh this import."
                    )
                parsed = parse_subscription(content)
        except SubscriptionError as exc:
            with self.connection:
                self.connection.execute(
                    "UPDATE network_subscriptions SET status='error', last_error_code=?, "
                    "last_error=?, updated_at=? WHERE id=?",
                    (exc.code, str(exc), now(), subscription_id),
                )
            raise
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            report = self._apply(subscription_id, parsed)
        return {"subscription": self.get_subscription(subscription_id), "report": report}

    def _apply(self, subscription_id: str, parsed: ParsedSubscription) -> dict[str, Any]:
        """Merge a parsed document. Runs inside the caller's write transaction."""
        stamp = now()
        existing = {
            row["node_key"]: row
            for row in self.connection.execute(
                "SELECT id, node_key, display_name, config_enc, status, invalid_reason, position, entry, protocol "
                "FROM network_nodes WHERE subscription_id = ?",
                (subscription_id,),
            )
        }
        created = updated = unchanged = 0
        seen: set[str] = set()
        for position, node in enumerate(parsed.nodes):
            seen.add(node.key)
            config_json = json.dumps(node.config, sort_keys=True, ensure_ascii=False)
            status, reason = ("invalid", INFORMATIONAL) if node.informational else ("active", None)
            current = existing.get(node.key)
            if current is None:
                self.connection.execute(
                    "INSERT INTO network_nodes (id, subscription_id, node_key, display_name, "
                    "protocol, entry, config_enc, status, invalid_reason, position, created_at, "
                    "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        new_id("n"),
                        subscription_id,
                        node.key,
                        node.name,
                        node.protocol,
                        node.entry,
                        seal(config_json),
                        status,
                        reason,
                        position,
                        stamp,
                        stamp,
                    ),
                )
                created += 1
                continue
            same_config = unseal(current["config_enc"]) == config_json
            if same_config and current["status"] == status and current["position"] == position:
                unchanged += 1
                continue
            self.connection.execute(
                "UPDATE network_nodes SET display_name=?, entry=?, config_enc=?, status=?, "
                "invalid_reason=?, retired_at=NULL, position=?, updated_at=? WHERE id=?",
                (
                    node.name,
                    node.entry,
                    current["config_enc"] if same_config else seal(config_json),
                    status,
                    reason,
                    position,
                    stamp,
                    current["id"],
                ),
            )
            if not same_config:
                # A changed configuration may come out somewhere else entirely.
                self.connection.execute(
                    "DELETE FROM network_node_checks WHERE node_id = ?", (current["id"],)
                )
            if same_config and current["status"] == status:
                unchanged += 1
            else:
                updated += 1
        retired = 0
        for key, row in existing.items():
            if key not in seen and self._stale_informational(row):
                # Panels rewrite lines such as "更新時間：…" on every update; an old one
                # is dropped instead of piling up as retired.
                self.connection.execute("DELETE FROM network_nodes WHERE id = ?", (row["id"],))
            elif key not in seen and row["status"] != "retired":
                self.connection.execute(
                    "UPDATE network_nodes SET status='retired', retired_at=?, updated_at=? "
                    "WHERE id=?",
                    (stamp, stamp, row["id"]),
                )
                retired += 1
        self.connection.execute(
            "UPDATE network_subscriptions SET status='ok', last_synced_at=?, last_error_code=NULL, "
            "last_error=NULL, usage=COALESCE(?, usage), revision=revision+1, updated_at=? "
            "WHERE id=?",
            (
                stamp,
                json.dumps(parsed.usage) if parsed.usage else None,
                stamp,
                subscription_id,
            ),
        )
        return {
            "created": created,
            "updated": updated,
            "retired": retired,
            "unchanged": unchanged,
            "warnings": parsed.warnings,
            "last_synced_at": stamp,
        }

    # --- Manual node groups ---------------------------------------------------
    #
    # Fixed proxies a user bought (host, port, username, password) or single share
    # links live in groups of kind 'manual'. They have no source to refresh;
    # their nodes are added, edited and deleted one by one.

    def _stale_informational(self, row: Any) -> bool:
        """An unlisted panel line, including ones stored before they were recognised."""
        from .network_subscription import is_informational

        if row["invalid_reason"] == INFORMATIONAL:
            return True
        if not is_informational(row["display_name"]):
            return False
        bound = self.connection.execute(
            "SELECT 1 FROM profile_network_bindings WHERE node_id = ?", (row["id"],)
        ).fetchone()
        return bound is None

    def create_manual_group(self, name: str) -> dict[str, Any]:
        name = " ".join((name or "").split())[:80] or "Manual nodes"
        seal("")  # refuse early, before anything is stored, when no key exists
        group_id = new_id("s")
        stamp = now()
        with self.connection:
            self.connection.execute(
                "INSERT INTO network_subscriptions (id, name, source_kind, status, last_synced_at, "
                "created_at, updated_at) VALUES (?,?,'manual','ok',?,?,?)",
                (group_id, name, stamp, stamp, stamp),
            )
        return self.get_subscription(group_id)

    def _manual_group(self, group_id: str):
        row = self._subscription_row(group_id)
        if row["source_kind"] != "manual":
            raise SubscriptionError("not_manual", "Nodes can only be added to manual node groups.")
        return row

    def add_manual_nodes(
        self, group_id: str, candidates: list[tuple[int, Any, str | None]]
    ) -> dict[str, Any]:
        """Insert validated nodes; existing identical nodes and bad lines are skipped."""
        from .network_subscription import node_key, validate_node

        self._manual_group(group_id)
        created: list[str] = []
        skipped: list[dict[str, Any]] = []
        stamp = now()
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            position = self.connection.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 FROM network_nodes WHERE subscription_id = ?",
                (group_id,),
            ).fetchone()[0]
            for line, raw, failure in candidates:
                if failure is not None:
                    skipped.append({"line": line, "reason": failure})
                    continue
                try:
                    node = validate_node(raw)
                except ValueError as exc:
                    skipped.append({"line": line, "reason": str(exc)})
                    continue
                key = node_key(node)
                exists = self.connection.execute(
                    "SELECT 1 FROM network_nodes WHERE subscription_id = ? AND node_key = ?",
                    (group_id, key),
                ).fetchone()
                if exists:
                    skipped.append({"line": line, "reason": "already exists"})
                    continue
                node_id = new_id("n")
                self.connection.execute(
                    "INSERT INTO network_nodes (id, subscription_id, node_key, display_name, "
                    "protocol, entry, config_enc, status, position, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,'active',?,?,?)",
                    (
                        node_id,
                        group_id,
                        key,
                        node.name,
                        node.protocol,
                        node.entry,
                        seal(json.dumps(node.config, sort_keys=True, ensure_ascii=False)),
                        position,
                        stamp,
                        stamp,
                    ),
                )
                position += 1
                created.append(node_id)
            self.connection.execute(
                "UPDATE network_subscriptions SET revision = revision + 1, updated_at = ?, "
                "last_synced_at = ? WHERE id = ?",
                (stamp, stamp, group_id),
            )
        return {
            "group": self.get_subscription(group_id),
            "created": [self.get_node(node_id) for node_id in created],
            "skipped": skipped,
        }

    def update_manual_node(self, node_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        """Rename any manual node; change the connection of HTTP/SOCKS5 ones.

        An omitted password keeps the stored one and an empty one clears it, the
        same write-only convention as the proxy library.
        """
        from .network_subscription import node_key, validate_node

        row = self.connection.execute(
            "SELECT n.id, n.subscription_id, n.protocol, n.config_enc, n.display_name, s.source_kind "
            "FROM network_nodes n JOIN network_subscriptions s ON s.id = n.subscription_id "
            "WHERE n.id = ?",
            (node_id,),
        ).fetchone()
        if row is None:
            raise NetworkNotFound("Node not found")
        if row["source_kind"] != "manual":
            raise SubscriptionError("not_manual", "Only manually added nodes can be edited.")
        config = json.loads(unseal(row["config_enc"]))
        connection_fields = {"server", "port", "username", "password"}
        if connection_fields & set(changes) and row["protocol"] not in {"http", "socks5"}:
            raise SubscriptionError(
                "not_editable", "Only HTTP and SOCKS5 nodes can change their connection."
            )
        for field in connection_fields & set(changes):
            value = changes[field]
            if field in {"username", "password"} and value in (None, ""):
                config.pop(field, None)
            else:
                config[field] = value
        name = changes.get("name") or row["display_name"]
        try:
            node = validate_node({**config, "name": name, "type": row["protocol"]})
        except ValueError as exc:
            raise SubscriptionError("invalid_node", f"The node is not valid ({exc}).") from None
        config_json = json.dumps(node.config, sort_keys=True, ensure_ascii=False)
        stamp = now()
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            clash = self.connection.execute(
                "SELECT 1 FROM network_nodes WHERE subscription_id = ? AND node_key = ? AND id != ?",
                (row["subscription_id"], node_key(node), node_id),
            ).fetchone()
            if clash:
                raise SubscriptionError("duplicate", "Another node in this group is identical.")
            changed = unseal(row["config_enc"]) != config_json
            self.connection.execute(
                "UPDATE network_nodes SET display_name=?, node_key=?, entry=?, config_enc=?, "
                "updated_at=? WHERE id=?",
                (
                    node.name,
                    node_key(node),
                    node.entry,
                    seal(config_json) if changed else row["config_enc"],
                    stamp,
                    node_id,
                ),
            )
            if changed:
                self.connection.execute(
                    "DELETE FROM network_node_checks WHERE node_id = ?", (node_id,)
                )
        return self.get_node(node_id)

    def delete_manual_node(self, node_id: str) -> None:
        row = self.connection.execute(
            "SELECT s.source_kind FROM network_nodes n JOIN network_subscriptions s "
            "ON s.id = n.subscription_id WHERE n.id = ?",
            (node_id,),
        ).fetchone()
        if row is None:
            raise NetworkNotFound("Node not found")
        if row["source_kind"] != "manual":
            raise SubscriptionError(
                "not_manual",
                "Subscription nodes follow their subscription; delete the subscription instead.",
            )
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            bound = self.connection.execute(
                "SELECT COUNT(*) FROM profile_network_bindings WHERE node_id = ?", (node_id,)
            ).fetchone()[0]
            if bound:
                raise NodeInUse(bound)
            self.connection.execute("DELETE FROM network_nodes WHERE id = ?", (node_id,))

    def rename_subscription(self, subscription_id: str, name: str, row_version: int) -> dict:
        from .database import StaleWriteError

        self._subscription_row(subscription_id)
        name = " ".join(name.split())[:80]
        if not name:
            raise ValueError("A subscription needs a name")
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE network_subscriptions SET name=?, row_version=row_version+1, updated_at=? "
                "WHERE id=? AND row_version=? AND deleted_at IS NULL",
                (name, now(), subscription_id, row_version),
            )
            if not cursor.rowcount:
                raise StaleWriteError(subscription_id, row_version)
        return self.get_subscription(subscription_id)

    def delete_subscription(self, subscription_id: str, retire_bound: bool = False) -> dict:
        """Delete outright when nothing is bound; otherwise only on request, by retiring."""
        self._subscription_row(subscription_id)
        stamp = now()
        with self.connection:
            # Counted under the write lock: a binding made after the count could
            # otherwise be cascaded away with its node.
            self.connection.execute("BEGIN IMMEDIATE")
            bound = self.connection.execute(
                "SELECT COUNT(*) FROM profile_network_bindings b JOIN network_nodes n "
                "ON n.id = b.node_id WHERE n.subscription_id = ?",
                (subscription_id,),
            ).fetchone()[0]
            if bound and not retire_bound:
                raise SubscriptionHasBindings(bound)
            if bound:
                # Bound nodes must survive for their bindings to report "retired".
                self.connection.execute(
                    "UPDATE network_nodes SET status='retired', retired_at=COALESCE(retired_at, ?), "
                    "updated_at=? WHERE subscription_id=?",
                    (stamp, stamp, subscription_id),
                )
                self.connection.execute(
                    "DELETE FROM network_nodes WHERE subscription_id=? AND id NOT IN "
                    "(SELECT node_id FROM profile_network_bindings)",
                    (subscription_id,),
                )
                self.connection.execute(
                    "UPDATE network_subscriptions SET deleted_at=?, source_url_enc=NULL, "
                    "updated_at=? WHERE id=?",
                    (stamp, stamp, subscription_id),
                )
                return {"deleted": False, "retired_bound_nodes": bound}
            self.connection.execute(
                "DELETE FROM network_subscriptions WHERE id=?", (subscription_id,)
            )
        return {"deleted": True, "retired_bound_nodes": 0}

    # --- Nodes -----------------------------------------------------------

    def list_nodes(
        self,
        *,
        subscription_id: str | None = None,
        status: str | None = None,
        country: str | None = None,
        health: str | None = None,
        search: str | None = None,
        located: bool | None = None,
        offset: int = 0,
        limit: int = 200,
    ) -> dict[str, Any]:
        clauses, values = ["1=1"], []
        if subscription_id:
            clauses.append("n.subscription_id = ?")
            values.append(subscription_id)
        if status:
            clauses.append("n.status = ?")
            values.append(status)
        if country:
            clauses.append("c.country_iso = ?")
            values.append(country.upper())
        if health:
            if health == "unknown":
                clauses.append("(c.health IS NULL OR c.health = 'unknown')")
            else:
                clauses.append("c.health = ?")
                values.append(health)
        if located is True:
            clauses.append("c.latitude IS NOT NULL AND c.longitude IS NOT NULL")
        elif located is False:
            clauses.append("(c.latitude IS NULL OR c.longitude IS NULL)")
        if search:
            pattern = (
                "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            )
            clauses.append(
                "(n.display_name LIKE ? ESCAPE '\\' OR c.exit_ip LIKE ? ESCAPE '\\' "
                "OR c.country_iso LIKE ? ESCAPE '\\' OR c.city LIKE ? ESCAPE '\\')"
            )
            values.extend([pattern] * 4)
        where = " AND ".join(clauses)
        base = (
            "FROM network_nodes n JOIN network_subscriptions s ON s.id = n.subscription_id "
            "LEFT JOIN network_node_checks c ON c.node_id = n.id WHERE " + where
        )
        total = self.connection.execute(f"SELECT COUNT(*) {base}", values).fetchone()[0]
        rows = self.connection.execute(
            f"{_NODE_SELECT} WHERE {where} ORDER BY s.created_at DESC, n.position, n.id "
            "LIMIT ? OFFSET ?",
            [*values, max(1, min(limit, 1000)), max(0, offset)],
        ).fetchall()
        return {"nodes": [_node_public(row) for row in rows], "total": total}

    def get_node(self, node_id: str) -> dict[str, Any]:
        row = self.connection.execute(f"{_NODE_SELECT} WHERE n.id = ?", (node_id,)).fetchone()
        if row is None:
            raise NetworkNotFound("Node not found")
        return _node_public(row)

    def node_outbound(self, node_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """(public summary, decrypted Mihomo outbound) — for runtime use only."""
        row = self.connection.execute(
            "SELECT id, protocol, status, config_enc FROM network_nodes WHERE id = ?", (node_id,)
        ).fetchone()
        if row is None:
            raise NetworkNotFound("Node not found")
        outbound = json.loads(unseal(row["config_enc"]))
        outbound["type"] = row["protocol"]
        return self.get_node(node_id), outbound

    def nodes_by_ids(self, node_ids: list[str]) -> list[str]:
        if not node_ids:
            return []
        marks = ",".join("?" * len(node_ids))
        return [
            row[0]
            for row in self.connection.execute(
                f"SELECT id FROM network_nodes WHERE id IN ({marks}) AND status = 'active'",
                node_ids,
            )
        ]

    # --- Measurements ----------------------------------------------------

    def save_check(self, node_id: str, check: dict[str, Any]) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO network_node_checks (node_id, health, exit_ip, country_iso, city, "
                "region, latitude, longitude, timezone, precision, location_source, latency_ms, "
                "error_code, error, checked_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(node_id) DO UPDATE SET health=excluded.health, "
                "exit_ip=excluded.exit_ip, country_iso=excluded.country_iso, city=excluded.city, "
                "region=excluded.region, latitude=excluded.latitude, longitude=excluded.longitude, "
                "timezone=excluded.timezone, precision=excluded.precision, "
                "location_source=excluded.location_source, latency_ms=excluded.latency_ms, "
                "error_code=excluded.error_code, error=excluded.error, checked_at=excluded.checked_at "
                "WHERE EXISTS (SELECT 1 FROM network_nodes WHERE id = excluded.node_id)",
                (
                    node_id,
                    check.get("health", "unknown"),
                    check.get("exit_ip"),
                    check.get("country_iso"),
                    check.get("city"),
                    check.get("region"),
                    check.get("latitude"),
                    check.get("longitude"),
                    check.get("timezone"),
                    check.get("precision", "unknown"),
                    check.get("location_source"),
                    check.get("latency_ms"),
                    check.get("error_code"),
                    check.get("error"),
                    check.get("checked_at") or now(),
                ),
            )

    def get_intel(self, ip: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT data, checked_at FROM network_ip_intel WHERE ip = ?", (ip,)
        ).fetchone()
        if row is None:
            return None
        return {**json.loads(row["data"]), "checked_at": row["checked_at"]}

    def save_intel(self, ip: str, data: dict[str, Any]) -> None:
        stamp = data.get("checked_at") or now()
        with self.connection:
            self.connection.execute(
                "INSERT INTO network_ip_intel (ip, data, checked_at) VALUES (?,?,?) "
                "ON CONFLICT(ip) DO UPDATE SET data=excluded.data, checked_at=excluded.checked_at",
                (ip, json.dumps({k: v for k, v in data.items() if k != "checked_at"}), stamp),
            )

    # --- Map ---------------------------------------------------------------

    def map_data(self) -> dict[str, Any]:
        """GeoJSON of measured exits, plus counts for what cannot be placed."""
        rows = self.connection.execute(
            f"{_NODE_SELECT} WHERE n.status != 'invalid' ORDER BY n.position, n.id"
        ).fetchall()
        features, unknown = [], []
        for row in rows:
            node = _node_public(row)
            check = node["check"]
            if not check or check["latitude"] is None or check["longitude"] is None:
                unknown.append(
                    {
                        "node_id": node["id"],
                        "subscription_id": node["subscription_id"],
                        "label": node["name"],
                        "status": node["status"],
                        "health": check["health"] if check else "unknown",
                        "country_iso": check["country_iso"] if check else None,
                        "exit_ip": check["exit_ip"] if check else None,
                    }
                )
                continue
            composite = (node["intel"] or {}).get("composite") or {}
            features.append(
                {
                    "type": "Feature",
                    "id": node["id"],
                    "geometry": {
                        "type": "Point",
                        "coordinates": [check["longitude"], check["latitude"]],
                    },
                    "properties": {
                        "node_id": node["id"],
                        "label": node["name"],
                        "subscription_id": node["subscription_id"],
                        "subscription_name": node["subscription_name"],
                        "protocol": node["protocol"],
                        "status": node["status"],
                        "country_iso": check["country_iso"],
                        "city": check["city"],
                        "region": check["region"],
                        "precision": check["precision"],
                        "location_source": check["location_source"],
                        "timezone": check["timezone"],
                        "health": check["health"],
                        "latency_ms": check["latency_ms"],
                        "exit_ip": check["exit_ip"],
                        "checked_at": check["checked_at"],
                        "stale": check["stale"],
                        "profile_count": node["profile_count"],
                        "shared_exit_count": node["shared_exit_count"],
                        "purity_score": composite.get("purity_score"),
                        "risk_score": composite.get("risk_score"),
                        "grade": composite.get("grade"),
                    },
                }
            )
        return {
            "features": {"type": "FeatureCollection", "features": features},
            "unknown_location": unknown,
            "unknown_location_count": len(unknown),
            "checked_count": sum(1 for row in rows if row["checked_at"]),
            "total_nodes": len(rows),
        }
