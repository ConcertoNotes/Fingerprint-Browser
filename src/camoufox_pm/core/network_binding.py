"""Which node a profile uses, changed only while the profile is closed.

A binding is a reference, unlike a proxy-library assignment, which copies: the
node's configuration is read at each launch. Changing it therefore takes the
same closed-profile operation lease as any other edit of a profile's identity —
a running (or leased) profile is refused with ``ProfileLocked`` instead of being
re-routed underneath its browser — and is version-checked, so two people
choosing different nodes for one profile cannot silently overwrite each other.
"""

from __future__ import annotations

from typing import Any

from .database import StaleWriteError
from .leases import make_lease_holder
from .network_catalog import NetworkCatalog, now
from .profile_operations import profile_operation


class NodeUnavailable(ValueError):
    """The node exists but cannot be bound (retired or invalid)."""


class NetworkBindings:
    def __init__(self, manager: Any):
        self.manager = manager
        self.connection = manager.storage.db._connection
        self.catalog = NetworkCatalog(manager)

    def _row(self, profile_id: str):
        return self.connection.execute(
            "SELECT profile_id, node_id, row_version, bound_at FROM profile_network_bindings "
            "WHERE profile_id = ?",
            (profile_id,),
        ).fetchone()

    def _public(self, row: Any) -> dict[str, Any]:
        node = self.catalog.get_node(row["node_id"])
        return {
            "profile_id": row["profile_id"],
            "node_id": row["node_id"],
            "row_version": row["row_version"],
            "bound_at": row["bound_at"],
            "node": node,
            "usable": node["status"] == "active",
        }

    def get(self, profile_id: str) -> dict[str, Any] | None:
        row = self._row(profile_id)
        return self._public(row) if row else None

    def list(self, profile_ids: set[str] | None = None) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT profile_id, node_id, row_version, bound_at FROM profile_network_bindings "
            "ORDER BY profile_id"
        ).fetchall()
        return [
            self._public(row)
            for row in rows
            if profile_ids is None or row["profile_id"] in profile_ids
        ]

    async def bind(
        self,
        profile_id: str,
        node_id: str,
        *,
        binding_row_version: int | None,
        profile_row_version: int | None = None,
    ) -> dict[str, Any]:
        node = self.catalog.get_node(node_id)
        if node["status"] != "active":
            raise NodeUnavailable("This node is retired or invalid and cannot be bound")
        holder = make_lease_holder()
        async with profile_operation(self.manager, profile_id, holder=holder) as profile:
            if profile_row_version is not None and profile.row_version != profile_row_version:
                raise StaleWriteError(profile_id, profile_row_version)
            with self.connection:
                self.connection.execute("BEGIN IMMEDIATE")
                current = self._row(profile_id)
                expected = current["row_version"] if current else None
                if binding_row_version != expected:
                    raise StaleWriteError(profile_id, binding_row_version)
                # The node must still be bindable at write time, under the lock.
                status = self.connection.execute(
                    "SELECT status FROM network_nodes WHERE id = ?", (node_id,)
                ).fetchone()
                if status is None or status[0] != "active":
                    raise NodeUnavailable("This node is retired or invalid and cannot be bound")
                if current is None:
                    self.connection.execute(
                        "INSERT INTO profile_network_bindings (profile_id, node_id, row_version, "
                        "bound_at) VALUES (?,?,0,?)",
                        (profile_id, node_id, now()),
                    )
                else:
                    self.connection.execute(
                        "UPDATE profile_network_bindings SET node_id=?, row_version=row_version+1, "
                        "bound_at=? WHERE profile_id=?",
                        (node_id, now(), profile_id),
                    )
            result = self._public(self._row(profile_id))
            # Reported, never acted on: the user decides about stated geography.
            result["geography_conflict"] = profile.browser_settings.has_geography()
            result["overrides_manual_proxy"] = profile.proxy is not None
            return result

    async def unbind(self, profile_id: str, *, binding_row_version: int) -> bool:
        holder = make_lease_holder()
        async with profile_operation(self.manager, profile_id, holder=holder):
            with self.connection:
                self.connection.execute("BEGIN IMMEDIATE")
                cursor = self.connection.execute(
                    "DELETE FROM profile_network_bindings WHERE profile_id = ? AND row_version = ?",
                    (profile_id, binding_row_version),
                )
                if not cursor.rowcount:
                    if self._row(profile_id) is None:
                        return False
                    raise StaleWriteError(profile_id, binding_row_version)
        return True
