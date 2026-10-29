"""Subscriptions, nodes, measured exits, the map and per-profile bindings.

Everything lives under /network so the existing /proxies and /profiles routes
keep their contracts. Node secrets never appear in a response. Responses carry
exit addresses and are therefore never cached.
"""

from typing import Any

from fastapi import APIRouter, Depends, Query, Response

from camoufox_pm.api.access import record_audit, require_admin, require_profile_access
from camoufox_pm.api.dependencies import get_profile_manager
from camoufox_pm.api.errors import ApiError
from camoufox_pm.api.models.network import (
    BatchCheckRequest,
    BindingRequest,
    CheckSiteRequest,
    ManualNodesRequest,
    NetworkSettingsUpdate,
    NodeCheckRequest,
    NodeUpdateRequest,
    SubscriptionCreateRequest,
    SubscriptionSyncRequest,
    SubscriptionUpdateRequest,
)
from camoufox_pm.api.models.system import ApiResponse
from camoufox_pm.core.ip_intel import check_site_url
from camoufox_pm.core.mihomo_manager import MihomoUnavailable
from camoufox_pm.core.network_adapter import NetworkLaunchError
from camoufox_pm.core.network_binding import NetworkBindings, NodeUnavailable
from camoufox_pm.core.network_catalog import (
    NetworkCatalog,
    NetworkNotFound,
    NetworkSecretsUnavailable,
    NodeInUse,
    SubscriptionHasBindings,
)
from camoufox_pm.core.network_probe import ProbeBusy
from camoufox_pm.core.network_settings import NetworkSettings
from camoufox_pm.core.network_subscription import SubscriptionError


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(prefix="/network", dependencies=[Depends(_no_store)])
Data = ApiResponse[dict[str, Any]]


def _manager():
    return get_profile_manager()


def _catalog() -> NetworkCatalog:
    return NetworkCatalog(_manager())


async def _perform(operation):
    try:
        return await operation
    except SubscriptionError as exc:
        raise ApiError(422, str(exc), exc.code) from None
    except NetworkNotFound as exc:
        raise ApiError(404, str(exc)) from None
    except NetworkSecretsUnavailable as exc:
        raise ApiError(409, str(exc), "secrets_unavailable") from None
    except SubscriptionHasBindings as exc:
        raise ApiError(
            409,
            "Profiles are bound to nodes of this subscription. Retire them explicitly to delete it.",
            "subscription_bound",
            [{"bound_profiles": exc.count}],
        ) from None
    except NodeInUse as exc:
        raise ApiError(
            409,
            "Profiles are bound to this node. Bind them elsewhere before deleting it.",
            "node_bound",
            [{"bound_profiles": exc.count}],
        ) from None
    except NodeUnavailable as exc:
        raise ApiError(422, str(exc), "node_unavailable") from None
    except ProbeBusy as exc:
        raise ApiError(409, str(exc), "probe_busy") from None
    except MihomoUnavailable as exc:
        raise ApiError(503, str(exc), exc.code) from None
    except NetworkLaunchError as exc:
        raise ApiError(exc.status, str(exc), exc.code) from None


async def _value(value):
    return value


# --- Engine and settings --------------------------------------------------------


@router.get("/status", response_model=Data, operation_id="network_status")
async def network_status():
    await require_admin()
    manager = _manager()
    settings = NetworkSettings(manager.storage.db._connection)
    try:
        binary = manager.network.binary()
        engine = {"available": True, "path": str(binary.path), "version": binary.version}
    except MihomoUnavailable as exc:
        engine = {"available": False, "code": exc.code, "message": str(exc)}
    geoip: dict[str, Any] = {"available": False}
    try:
        from camoufox.geolocation import get_mmdb_path

        path = get_mmdb_path()
        if path.exists():
            stat = path.stat()
            geoip = {
                "available": True,
                "name": path.name,
                "size": stat.st_size,
                "modified_at": stat.st_mtime,
            }
    except Exception:  # noqa: BLE001 - status reporting must not fail
        pass
    from camoufox_pm.config import get_settings

    return Data(
        success=True,
        data={
            "engine": engine,
            "geoip": geoip,
            "encryption_available": bool(get_settings().secret_key),
            "settings": settings.public(),
            "active_runtimes": len(manager.network.runtimes),
        },
        message="Network status",
    )


@router.get("/settings", response_model=Data, operation_id="get_network_settings")
async def get_network_settings():
    await require_admin()
    return Data(
        success=True,
        data=NetworkSettings(_manager().storage.db._connection).public(),
        message="Network settings",
    )


@router.patch("/settings", response_model=Data, operation_id="update_network_settings")
async def update_network_settings(request: NetworkSettingsUpdate):
    await require_admin()
    changes = request.model_dump(exclude_unset=True)
    nullable = {
        "mihomo_path",
        "intel_ipinfo_token",
        "intel_scamalytics_url",
        "intel_scamalytics_key",
    }
    changes = {k: v for k, v in changes.items() if v is not None or k in nullable}
    try:
        data = await _perform(
            _value(NetworkSettings(_manager().storage.db._connection).update(changes))
        )
    except ValueError as exc:
        raise ApiError(422, str(exc)) from None
    await record_audit("update_network_settings", None)
    return Data(success=True, data=data, message="Network settings saved")


# --- Subscriptions ------------------------------------------------------------------


@router.get("/subscriptions", response_model=Data, operation_id="list_network_subscriptions")
async def list_subscriptions():
    await require_admin()
    return Data(
        success=True,
        data={"subscriptions": _catalog().list_subscriptions()},
        message="Subscriptions",
    )


@router.post(
    "/subscriptions",
    response_model=Data,
    status_code=201,
    operation_id="create_network_subscription",
)
async def create_subscription(request: SubscriptionCreateRequest):
    await require_admin()
    result = await _perform(
        _catalog().create_subscription(
            request.name,
            url=request.url,
            content=request.content.encode("utf-8") if request.content is not None else None,
            file_name=request.file_name,
        )
    )
    await record_audit("create_network_subscription", result["subscription"]["id"])
    return Data(success=True, data=result, message="Subscription imported")


@router.get(
    "/subscriptions/{subscription_id}", response_model=Data, operation_id="get_network_subscription"
)
async def get_subscription(subscription_id: str):
    await require_admin()
    return Data(
        success=True,
        data=await _perform(_value_call(_catalog().get_subscription, subscription_id)),
        message="Subscription",
    )


async def _value_call(function, *args, **kwargs):
    return function(*args, **kwargs)


@router.patch(
    "/subscriptions/{subscription_id}",
    response_model=Data,
    operation_id="update_network_subscription",
)
async def update_subscription(subscription_id: str, request: SubscriptionUpdateRequest):
    await require_admin()
    try:
        data = await _perform(
            _value_call(
                _catalog().rename_subscription, subscription_id, request.name, request.row_version
            )
        )
    except ValueError as exc:
        raise ApiError(422, str(exc)) from None
    return Data(success=True, data=data, message="Subscription renamed")


@router.post(
    "/subscriptions/{subscription_id}/sync",
    response_model=Data,
    operation_id="sync_network_subscription",
)
async def sync_subscription(subscription_id: str, request: SubscriptionSyncRequest | None = None):
    await require_admin()
    content = request.content.encode("utf-8") if request and request.content is not None else None
    result = await _perform(_catalog().sync_subscription(subscription_id, content=content))
    await record_audit("sync_network_subscription", subscription_id)
    return Data(success=True, data=result, message="Subscription synchronized")


@router.delete(
    "/subscriptions/{subscription_id}",
    response_model=Data,
    operation_id="delete_network_subscription",
)
async def delete_subscription(subscription_id: str, retire_bound: bool = Query(False)):
    await require_admin()
    result = await _perform(
        _value_call(_catalog().delete_subscription, subscription_id, retire_bound)
    )
    await record_audit("delete_network_subscription", subscription_id)
    return Data(success=True, data=result, message="Subscription removed")


# --- Nodes and checks --------------------------------------------------------------


@router.get("/nodes", response_model=Data, operation_id="list_network_nodes")
async def list_nodes(
    subscription_id: str | None = None,
    status: str | None = Query(None, pattern="^(active|retired|invalid)$"),
    country: str | None = Query(None, pattern="^[A-Za-z]{2}$"),
    health: str | None = Query(None, pattern="^(ok|error|unknown)$"),
    search: str | None = Query(None, max_length=100),
    located: bool | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=1000),
):
    await require_admin()
    return Data(
        success=True,
        data=_catalog().list_nodes(
            subscription_id=subscription_id,
            status=status,
            country=country,
            health=health,
            search=search,
            located=located,
            offset=offset,
            limit=limit,
        ),
        message="Nodes",
    )


@router.get("/nodes/{node_id}", response_model=Data, operation_id="get_network_node")
async def get_node(node_id: str):
    await require_admin()
    return Data(
        success=True, data=await _perform(_value_call(_catalog().get_node, node_id)), message="Node"
    )


@router.post(
    "/manual-nodes", response_model=Data, status_code=201, operation_id="add_manual_network_nodes"
)
async def add_manual_nodes(request: ManualNodesRequest):
    """Fixed nodes: a form entry, or pasted host:port:user:pass lines, share links or YAML."""
    from camoufox_pm.core.network_uri import parse_manual_text

    await require_admin()
    catalog = _catalog()

    async def run():
        group_id = request.group_id or catalog.create_manual_group(request.group_name or "")["id"]
        if request.node is not None:
            node = request.node
            mapping = {
                "name": node.name or f"{node.server}:{node.port}",
                "type": "http" if node.type == "https" else node.type,
                "server": node.server.strip().strip("[]"),
                "port": node.port,
                **({"tls": True} if node.type == "https" else {}),
                **({"username": node.username} if node.username else {}),
                **({"password": node.password} if node.password else {}),
            }
            candidates = [(1, mapping, None)]
        else:
            candidates = parse_manual_text(request.text or "", request.default_type)
            if not candidates:
                raise SubscriptionError("empty", "Enter at least one node.")
        return catalog.add_manual_nodes(group_id, candidates)

    result = await _perform(run())
    await record_audit("add_manual_network_nodes", result["group"]["id"])
    return Data(success=True, data=result, message="Nodes added")


@router.patch("/nodes/{node_id}", response_model=Data, operation_id="update_network_node")
async def update_node(node_id: str, request: NodeUpdateRequest):
    await require_admin()
    changes = request.model_dump(exclude_unset=True)
    node = await _perform(_value_call(_catalog().update_manual_node, node_id, changes))
    return Data(success=True, data=node, message="Node updated")


@router.delete("/nodes/{node_id}", response_model=Data, operation_id="delete_network_node")
async def delete_node(node_id: str):
    await require_admin()
    await _perform(_value_call(_catalog().delete_manual_node, node_id))
    await record_audit("delete_network_node", node_id)
    return Data(success=True, data={"id": node_id}, message="Node deleted")


@router.get("/jobs/current", response_model=Data, operation_id="current_network_job")
async def current_job():
    """The running batch check, if any, so every page can show its progress."""
    await require_admin()
    probe = _manager().network_probe
    running = [job for job in probe.jobs.values() if job["state"] in {"queued", "running"}]
    return Data(success=True, data={"job": dict(running[0]) if running else None}, message="Job")


@router.post("/nodes/{node_id}/check", response_model=Data, operation_id="check_network_node")
async def check_node(node_id: str, request: NodeCheckRequest | None = None):
    await require_admin()
    options = request or NodeCheckRequest()
    node = await _perform(
        _manager().network_probe.check_node(
            node_id, include_intel=options.include_intel, refresh_intel=options.refresh_intel
        )
    )
    return Data(success=True, data=node, message="Node checked")


@router.post("/nodes/check-batch", response_model=Data, operation_id="check_network_nodes")
async def check_batch(request: BatchCheckRequest):
    await require_admin()
    try:
        job = await _perform(_value_call(_manager().network_probe.start_batch, request.node_ids))
    except ValueError as exc:
        raise ApiError(422, str(exc)) from None
    return Data(success=True, data=job, message="Batch check started")


@router.get("/jobs/{job_id}", response_model=Data, operation_id="get_network_job")
async def get_job(job_id: str):
    await require_admin()
    return Data(
        success=True,
        data=await _perform(_value_call(_manager().network_probe.get_job, job_id)),
        message="Job",
    )


@router.post("/jobs/{job_id}/cancel", response_model=Data, operation_id="cancel_network_job")
async def cancel_job(job_id: str):
    await require_admin()
    return Data(
        success=True,
        data=await _perform(_manager().network_probe.cancel_job(job_id)),
        message="Job cancelled",
    )


@router.get("/map", response_model=Data, operation_id="network_map")
async def network_map():
    await require_admin()
    return Data(success=True, data=_catalog().map_data(), message="Network map")


# --- Bindings -------------------------------------------------------------------------


@router.get("/bindings", response_model=Data, operation_id="list_network_bindings")
async def list_bindings():
    await require_admin()
    manager = _manager()
    bindings = NetworkBindings(manager).list()
    for binding in bindings:
        binding["runtime"] = manager.network.info(binding["profile_id"])
    return Data(success=True, data={"bindings": bindings}, message="Bindings")


@router.get("/bindings/{profile_id}", response_model=Data, operation_id="get_network_binding")
async def get_binding(profile_id: str):
    await require_profile_access(profile_id, "view")
    await require_admin()
    manager = _manager()
    if await manager.storage.get_profile(profile_id) is None:
        raise ApiError(404, "Profile not found")
    binding = NetworkBindings(manager).get(profile_id)
    return Data(
        success=True,
        data={"binding": binding, "runtime": manager.network.info(profile_id)},
        message="Binding",
    )


@router.put("/bindings/{profile_id}", response_model=Data, operation_id="set_network_binding")
async def set_binding(profile_id: str, request: BindingRequest):
    await require_profile_access(profile_id, "edit")
    await require_admin()
    try:
        binding = await _perform(
            NetworkBindings(_manager()).bind(
                profile_id,
                request.node_id,
                binding_row_version=request.binding_row_version,
                profile_row_version=request.profile_row_version,
            )
        )
    except ValueError as exc:  # profile missing or in Trash
        raise ApiError(404, str(exc)) from None
    await record_audit("bind_network_node", profile_id)
    return Data(success=True, data=binding, message="Node bound; it applies from the next launch")


@router.delete("/bindings/{profile_id}", response_model=Data, operation_id="delete_network_binding")
async def delete_binding(profile_id: str, binding_row_version: int = Query(..., ge=0)):
    await require_profile_access(profile_id, "edit")
    await require_admin()
    try:
        removed = await _perform(
            NetworkBindings(_manager()).unbind(profile_id, binding_row_version=binding_row_version)
        )
    except ValueError as exc:
        raise ApiError(404, str(exc)) from None
    if not removed:
        raise ApiError(404, "This profile has no binding")
    await record_audit("unbind_network_node", profile_id)
    return Data(success=True, data={"profile_id": profile_id}, message="Binding removed")


# --- Runtime --------------------------------------------------------------------------


@router.get("/runtime", response_model=Data, operation_id="list_network_runtimes")
async def list_runtimes():
    await require_admin()
    return Data(success=True, data={"runtimes": _manager().network.list_info()}, message="Runtimes")


@router.get("/runtime/{profile_id}", response_model=Data, operation_id="get_network_runtime")
async def get_runtime(profile_id: str):
    await require_profile_access(profile_id, "view")
    return Data(success=True, data=_manager().network.info(profile_id), message="Runtime")


@router.post(
    "/runtime/{profile_id}/reconnect", response_model=Data, operation_id="reconnect_network"
)
async def reconnect(profile_id: str):
    await require_profile_access(profile_id, "launch")
    runtime = _manager().network.runtimes.get(profile_id)
    if runtime is None:
        raise ApiError(409, "This profile has no running bound network", "not_running")
    await _perform(runtime.reconnect())
    return Data(success=True, data=runtime.info(), message="Network reconnected to the same node")


@router.post("/runtime/{profile_id}/open-site", response_model=Data, operation_id="open_check_site")
async def open_check_site(profile_id: str, request: CheckSiteRequest):
    """Open an IP-check site in the profile's own running browser (and so through its exit)."""
    await require_profile_access(profile_id, "launch")
    manager = _manager()
    session = manager.browser_sessions.active_sessions.get(profile_id)
    if session is None or session.context is None:
        raise ApiError(
            409, "Start the profile first; the page opens in its own browser", "not_running"
        )
    runtime = manager.network.runtimes.get(profile_id)
    exit_ip = runtime.exit_ip if runtime else None
    if exit_ip is None:
        # A profile on its own manual proxy: its last proxy check knows the exit.
        profile = await manager.storage.get_profile(profile_id)
        exit_ip = profile.proxy_check.ip if profile and profile.proxy_check else None
    try:
        url = check_site_url(request.site, exit_ip)
    except ValueError as exc:
        raise ApiError(422, str(exc)) from None
    page = await session.context.new_page()
    try:
        await page.goto(url, wait_until="commit", timeout=20000)
    except Exception:  # noqa: BLE001 - the tab stays open; the user sees the browser's own error
        pass
    await record_audit("open_check_site", profile_id)
    return Data(success=True, data={"url": url}, message="Opened in the profile's browser")
