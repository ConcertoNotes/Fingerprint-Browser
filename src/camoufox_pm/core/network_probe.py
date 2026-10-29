"""Measure where nodes really come out, one at a time or as a pooled batch.

The exit address is always asked for *through the node* — the entry server's
own address, the node's name and its flag emoji say nothing about where its
traffic leaves. The address is then placed with the same GeoIP database the
browser launch uses; when that fails the node stays "unknown" rather than being
put on the map from its label.

A batch never starts one process per node: it runs a single temporary Mihomo
with one authenticated listener per node, probes the listeners concurrently up
to a configured limit and destroys the process at the end. A node whose
configuration Mihomo rejects is set aside and the pool restarts without it.
"""

from __future__ import annotations

import asyncio
import re
import secrets
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from . import proxy_check
from .ip_intel import gather_intel
from .mihomo_manager import MihomoProcess, MihomoUnavailable
from .models import ProxyConfig, ProxyType
from .network_catalog import NetworkCatalog, NetworkNotFound

MAX_BATCH = 200
_single_checks = asyncio.Semaphore(3)


class ProbeBusy(RuntimeError):
    pass


def _listener_proxy(listener) -> ProxyConfig:
    return ProxyConfig(
        type=ProxyType.HTTP,
        server=f"127.0.0.1:{listener.port}",
        username=listener.username,
        password=listener.password,
    )


def _proxy_url(proxy: ProxyConfig) -> str:
    return proxy_check.proxy_url(proxy)


async def measure_exit(
    proxy: ProxyConfig, timeout: float, endpoints: tuple[str, ...] | None = None
) -> dict[str, Any]:
    checked_at = datetime.now(timezone.utc).isoformat()
    try:
        ip, latency = await proxy_check.resolve_exit_ip(proxy, timeout=timeout, endpoints=endpoints)
    except (ConnectionError, asyncio.TimeoutError):
        return {
            "health": "error",
            "error_code": "exit_unreachable",
            "error": "The node did not reach the internet.",
            "precision": "unknown",
            "checked_at": checked_at,
        }
    result: dict[str, Any] = {
        "health": "ok",
        "exit_ip": ip,
        "latency_ms": latency,
        "precision": "unknown",
        "checked_at": checked_at,
    }
    try:
        location = await asyncio.to_thread(proxy_check.locate, ip)
    except proxy_check.LocationUnavailable:
        result["error_code"] = "location_unavailable"
        result["error"] = "The exit address is not in the local GeoIP database."
        return result
    result.update(
        {
            "country_iso": location.country,
            "timezone": location.timezone,
            "latitude": location.latitude,
            "longitude": location.longitude,
            "location_source": "geoip",
        }
    )
    if location.latitude is not None and location.longitude is not None:
        result["precision"] = "coordinates"
    elif location.country:
        result["precision"] = "country"
    return result


# A provider's city names the measured point only if it is about the same place.
# IP geolocation databases routinely disagree by whole states; a city from one
# must never label coordinates from another.
CITY_AGREEMENT_KM = 150.0


def _apply_intel_location(check: dict[str, Any], intel: dict[str, Any] | None) -> None:
    """Name the city when a provider agrees with GeoIP; place the node only if GeoIP could not."""
    location = ((intel or {}).get("composite") or {}).get("location") or {}
    has_point = check.get("latitude") is not None and check.get("longitude") is not None
    if location.get("city") and has_point and location.get("latitude") is not None:
        distance = proxy_check._distance_km(
            check["latitude"], check["longitude"], location["latitude"], location["longitude"]
        )
        if distance <= CITY_AGREEMENT_KM:
            check["city"] = location["city"]
            check["region"] = location.get("region")
        else:
            check["city"] = check["region"] = None
    if check.get("precision") == "unknown" and location.get("latitude") is not None:
        check.update(
            {
                "latitude": location["latitude"],
                "longitude": location["longitude"],
                "city": location.get("city"),
                "region": location.get("region"),
                "country_iso": location.get("country_iso") or check.get("country_iso"),
                "timezone": location.get("timezone") or check.get("timezone"),
                "precision": "city" if location.get("city") else "coordinates",
                "location_source": "intel",
                "error_code": None,
                "error": None,
            }
        )


class NetworkProbe:
    def __init__(self, manager: Any):
        self.manager = manager
        self.jobs: dict[str, dict[str, Any]] = {}
        self._job_tasks: dict[str, asyncio.Task[None]] = {}
        self._batch_lock = asyncio.Lock()
        self.endpoints: tuple[str, ...] | None = None
        self.intel_urls: dict[str, str] | None = None

    @property
    def adapter(self):
        return self.manager.network

    def _timeout(self) -> float:
        return float(self.adapter.settings().values()["probe_timeout_seconds"])

    async def check_node(
        self, node_id: str, *, include_intel: bool = True, refresh_intel: bool = False
    ) -> dict[str, Any]:
        catalog = NetworkCatalog(self.manager)
        node, outbound = catalog.node_outbound(node_id)
        async with _single_checks:
            # Reuse a running profile's own listener for its node: measuring its
            # exit must not open a second path to the same node.
            running = next(
                (
                    runtime
                    for runtime in self.adapter.runtimes.values()
                    if runtime.node["id"] == node_id and runtime.engine.alive
                ),
                None,
            )
            engine = None
            try:
                if running is not None:
                    proxy = running.proxy_config
                else:
                    try:
                        engine = await MihomoProcess(
                            self.adapter.binary(),
                            {"node-0": outbound},
                            ["node-0"],
                            self.adapter.runtime_root,
                            purpose="probe",
                        ).start()
                    except MihomoUnavailable as exc:
                        if exc.code.startswith("binary_"):
                            raise
                        check = {
                            "health": "error",
                            "error_code": exc.code,
                            "error": str(exc),
                            "precision": "unknown",
                            "checked_at": datetime.now(timezone.utc).isoformat(),
                        }
                        catalog.save_check(node_id, check)
                        return catalog.get_node(node_id)
                    proxy = _listener_proxy(engine.listeners[0])
                check = await measure_exit(proxy, self._timeout(), self.endpoints)
                if check["health"] == "error" and engine is not None:
                    classified = engine.last_error()
                    if classified:
                        check["error_code"], check["error"] = classified
                intel = None
                if include_intel and check.get("exit_ip"):
                    intel = catalog.get_intel(check["exit_ip"])
                    if refresh_intel or not _fresh(intel, self.adapter.settings().values()):
                        intel = await self._gather(proxy, check["exit_ip"])
                        catalog.save_intel(check["exit_ip"], intel)
                elif check.get("exit_ip"):
                    intel = catalog.get_intel(check["exit_ip"])
                _apply_intel_location(check, intel)
                catalog.save_check(node_id, check)
            finally:
                if engine is not None:
                    await engine.close()
        return catalog.get_node(node_id)

    async def _gather(self, proxy: ProxyConfig, exit_ip: str) -> dict[str, Any]:
        settings = self.adapter.settings()
        values = settings.values()
        secrets_ = {
            key: settings.secret(key)
            for key in ("intel_ipinfo_token", "intel_scamalytics_url", "intel_scamalytics_key")
        }
        return await gather_intel(
            _proxy_url(proxy),
            exit_ip,
            values,
            secrets_,
            timeout=self._timeout(),
            urls=self.intel_urls,
        )

    # --- Batches -----------------------------------------------------------

    def start_batch(self, node_ids: list[str]) -> dict[str, Any]:
        if self._batch_lock.locked() or any(
            job["state"] in {"queued", "running"} for job in self.jobs.values()
        ):
            raise ProbeBusy("A batch check is already running")
        unique = list(dict.fromkeys(node_ids))
        if not 1 <= len(unique) <= MAX_BATCH:
            raise ValueError(f"Choose between 1 and {MAX_BATCH} nodes")
        catalog = NetworkCatalog(self.manager)
        active = set(catalog.nodes_by_ids(unique))
        targets = [node_id for node_id in unique if node_id in active]
        if not targets:
            raise NetworkNotFound("None of the selected nodes are active")
        job_id = "j" + secrets.token_hex(6)
        job = {
            "id": job_id,
            "state": "queued",
            "total": len(targets),
            "done": 0,
            "ok": 0,
            "failed": 0,
            "skipped": len(unique) - len(targets),
            "engine_processes": 0,
            "error": None,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
        }
        self.jobs = {
            key: value
            for key, value in self.jobs.items()
            if value["state"] in {"queued", "running"}
        } | {job_id: job}
        task = asyncio.create_task(self._run_batch(job, targets))
        self._job_tasks[job_id] = task
        task.add_done_callback(lambda _t: self._job_tasks.pop(job_id, None))
        return dict(job)

    def get_job(self, job_id: str) -> dict[str, Any]:
        if job_id not in self.jobs:
            raise NetworkNotFound("Job not found")
        return dict(self.jobs[job_id])

    async def cancel_job(self, job_id: str) -> dict[str, Any]:
        task = self._job_tasks.get(job_id)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return self.get_job(job_id)

    async def _run_batch(self, job: dict[str, Any], targets: list[str]) -> None:
        catalog = NetworkCatalog(self.manager)
        engine: MihomoProcess | None = None
        async with self._batch_lock:
            job["state"] = "running"
            try:
                outbounds: dict[str, dict[str, Any]] = {}
                names: dict[str, str] = {}
                for index, node_id in enumerate(targets):
                    try:
                        _, outbound = catalog.node_outbound(node_id)
                    except Exception:  # noqa: BLE001 - one unreadable node fails alone
                        self._record_failure(
                            catalog,
                            job,
                            node_id,
                            "node_unreadable",
                            "The node configuration could not be read.",
                        )
                        continue
                    name = f"node-{index}"
                    outbounds[name] = outbound
                    names[name] = node_id
                engine = await self._start_pool(catalog, job, outbounds, names)
                if engine is None:
                    return
                limit = asyncio.Semaphore(
                    int(self.adapter.settings().values()["probe_concurrency"])
                )
                timeout = self._timeout()

                async def probe(listener) -> None:
                    node_id = names[listener.outbound]
                    async with limit:
                        check = await measure_exit(
                            _listener_proxy(listener), timeout, self.endpoints
                        )
                    # Cached third-party details still name the city; none are fetched here.
                    if check.get("exit_ip"):
                        _apply_intel_location(check, catalog.get_intel(check["exit_ip"]))
                    catalog.save_check(node_id, check)
                    job["done"] += 1
                    job["ok" if check["health"] == "ok" else "failed"] += 1

                await asyncio.gather(*(probe(listener) for listener in engine.listeners))
                job["state"] = "finished"
            except asyncio.CancelledError:
                job["state"] = "cancelled"
                raise
            except MihomoUnavailable as exc:
                job["state"], job["error"] = "failed", {"code": exc.code, "message": str(exc)}
            except Exception as exc:  # noqa: BLE001 - reported on the job, never raised into the loop
                logger.warning(f"Batch check failed: {type(exc).__name__}")
                job["state"], job["error"] = (
                    "failed",
                    {"code": "internal_error", "message": "The batch check failed."},
                )
            finally:
                if engine is not None:
                    await engine.close()
                job["finished_at"] = datetime.now(timezone.utc).isoformat()

    async def _start_pool(self, catalog, job, outbounds, names) -> MihomoProcess | None:
        """One engine for the whole batch, dropping nodes Mihomo refuses to load."""
        binary = self.adapter.binary()
        for _ in range(6):
            if not outbounds:
                job["state"] = "finished"
                return None
            engine = MihomoProcess(
                binary, outbounds, list(outbounds), self.adapter.runtime_root, purpose="batch"
            )
            job["engine_processes"] += 1
            try:
                return await engine.start()
            except MihomoUnavailable as exc:
                if exc.code != "config_invalid":
                    raise
                rejected = _rejected_outbound(engine, list(outbounds))
                if rejected is None:
                    raise
                self._record_failure(
                    catalog,
                    job,
                    names[rejected],
                    "config_invalid",
                    "Mihomo rejected this node's configuration.",
                )
                outbounds = {k: v for k, v in outbounds.items() if k != rejected}
        raise MihomoUnavailable("config_invalid", "Too many nodes were rejected by Mihomo.")

    @staticmethod
    def _record_failure(catalog, job, node_id: str, code: str, message: str) -> None:
        catalog.save_check(
            node_id,
            {
                "health": "error",
                "error_code": code,
                "error": message,
                "precision": "unknown",
                "checked_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        job["done"] += 1
        job["failed"] += 1

    async def shutdown(self) -> None:
        for task in list(self._job_tasks.values()):
            task.cancel()
        await asyncio.gather(*self._job_tasks.values(), return_exceptions=True)


def _rejected_outbound(engine: MihomoProcess, names: list[str]) -> str | None:
    """Mihomo names the failing entry as ``proxy N`` (0-based) when it refuses a config."""
    text = "\n".join(engine._log)
    match = re.search(r"proxy (\d+)", text)
    if match and int(match.group(1)) < len(names):
        return names[int(match.group(1))]
    for name in names:
        if re.search(rf"\b{re.escape(name)}\b", text):
            return name
    return None


def _fresh(intel: dict[str, Any] | None, settings: dict[str, Any]) -> bool:
    if not intel or not intel.get("checked_at"):
        return False
    age = datetime.now(timezone.utc) - datetime.fromisoformat(intel["checked_at"])
    return age.total_seconds() < int(settings["intel_cache_hours"]) * 3600
