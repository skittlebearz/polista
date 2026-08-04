from __future__ import annotations

import asyncio
import logging

log = logging.getLogger("polista.controller")


class BackendError(Exception):
    """A device/backend operation failed after controller validation."""


class Controller:
    def __init__(self, backend, port_map, store, port_count: int):
        self._lock = asyncio.Lock()
        self.backend = backend
        self.port_map = port_map
        self.store = store
        self.port_count = port_count
        self.mappings: dict[int, int] = {}
        self.labels: dict[int, str] = {}
        self.health = "healthy"
        self.sync = "in_sync"
        # Why the controller is unhealthy, in operator terms. A bare
        # "unhealthy" makes a missing SDE module, an unreachable switchd, and a
        # bad port map look identical; this is what turns that into a diagnosis.
        self.health_reason: str | None = None
        self._last_status_error: BaseException | None = None

    def mark_unhealthy(self, reason: str, exc: BaseException | None = None) -> None:
        self.health = "unhealthy"
        self.health_reason = reason
        if exc is not None:
            log.error("controller unhealthy: %s: %s", reason, exc, exc_info=exc)
        else:
            log.error("controller unhealthy: %s", reason)

    def mark_healthy(self) -> None:
        self.health = "healthy"
        self.health_reason = None

    async def connect(self, ingress, egress, force=False) -> dict:
        async with self._lock:
            self._require_healthy()
            ingress = self._validate_port(ingress)
            egress = self._validate_port(egress)
            ingress_dev = self._to_dev(ingress)
            egress_dev = self._to_dev(egress)

            old = self.mappings.get(ingress)
            other = self._ingress_for_egress(egress, exclude=ingress)

            if old == egress and other is None:
                return {
                    "status": "ok",
                    "removed": [],
                    "added": {"ingress": ingress, "egress": egress},
                    "sync_state": self.sync,
                }

            removed = self._removal_preview(ingress, old, other)
            if removed and not force:
                return {"status": "conflict", "would_remove": removed}

            if other is not None:
                try:
                    await self._call(self.backend.delete_entry, self._to_dev(other))
                except Exception as exc:
                    raise BackendError(str(exc)) from exc
            try:
                await self._call(self.backend.write_entry, ingress_dev, egress_dev)
            except Exception as exc:
                # The conflicting device entry is already gone.  Make the
                # desired-state file reflect that live truth before reporting
                # the failed replacement.
                if other is not None:
                    self.mappings.pop(other, None)
                    await self._persist()
                raise BackendError(str(exc)) from exc

            if other is not None:
                self.mappings.pop(other, None)
            self.mappings[ingress] = egress
            await self._persist()

            return {
                "status": "ok",
                "removed": removed,
                "added": {"ingress": ingress, "egress": egress},
                "sync_state": self.sync,
            }

    async def disconnect(self, ingress, egress) -> dict:
        async with self._lock:
            self._require_healthy()
            ingress = self._validate_port(ingress)
            egress = self._validate_port(egress)
            if self.mappings.get(ingress) != egress:
                raise ValueError("mapping does not exist")

            try:
                await self._call(self.backend.delete_entry, self._to_dev(ingress))
            except Exception as exc:
                raise BackendError(str(exc)) from exc
            self.mappings.pop(ingress, None)
            await self._persist()
            return {"status": "ok", "sync_state": self.sync}

    async def refresh(self) -> dict:
        async with self._lock:
            self._require_healthy()
            try:
                entries = await self._call(self.backend.read_all)
            except Exception as exc:
                raise BackendError(str(exc)) from exc

            mappings = {}
            for ingress_dev, egress_dev in entries:
                try:
                    ingress = self._to_ui(ingress_dev)
                except ValueError as exc:
                    raise BackendError(
                        f"device port {ingress_dev} has no UI port mapping"
                    ) from exc
                try:
                    egress = self._to_ui(egress_dev)
                except ValueError as exc:
                    raise BackendError(
                        f"device port {egress_dev} has no UI port mapping"
                    ) from exc
                mappings[ingress] = egress

            self.mappings = mappings
            if await self._persist():
                # Refresh constructs mappings from the live device, so a
                # successful save establishes a fresh, complete sync point.
                self.sync = "in_sync"
            return {"status": "ok", "source": "tofino"}

    async def reconcile(self) -> None:
        async with self._lock:
            try:
                mappings, labels = await self._call(self.store.load_state)
            except Exception as exc:
                self.mark_unhealthy(
                    f"could not read the desired-state file {self.store.path}", exc
                )
                return

            try:
                for ingress, egress in mappings.items():
                    self._validate_port(ingress)
                    self._validate_port(egress)
                    self._to_dev(ingress)
                    self._to_dev(egress)
            except ValueError as exc:
                self.mark_unhealthy(
                    f"the saved mappings do not fit the current port map: {exc}", exc
                )
                return

            if not await self._backend_reachable():
                self.mark_unhealthy(self._unreachable_reason(), self._last_status_error)
                return

            try:
                await self._call(self.backend.clear_all)
            except Exception as exc:
                self.mark_unhealthy(f"could not clear the device table: {exc}", exc)
                return

            self.mappings = dict(mappings)
            self.labels = dict(labels)

            for ingress, egress in self.mappings.items():
                try:
                    ingress = self._validate_port(ingress)
                    egress = self._validate_port(egress)
                    await self._call(
                        self.backend.write_entry,
                        self._to_dev(ingress),
                        self._to_dev(egress),
                    )
                except ValueError as exc:
                    self.mark_unhealthy(f"cannot replay mapping onto the device: {exc}", exc)
                    return
                except Exception as exc:
                    if await self._backend_reachable():
                        log.warning(
                            "replaying %s->%s failed; sync is partial: %s", ingress, egress, exc
                        )
                        self.sync = "partial_sync"
                    else:
                        self.mark_unhealthy(
                            f"device became unreachable while replaying state: {exc}", exc
                        )
                    return

            self.mark_healthy()
            self.sync = "in_sync"

    async def set_label(self, port, label) -> dict:
        async with self._lock:
            port = self._validate_port(port)
            self.labels[port] = str(label)
            await self._persist()
            return {"status": "ok", "sync_state": self.sync}

    def _validate_port(self, port) -> int:
        try:
            port = int(port)
        except (TypeError, ValueError) as exc:
            raise ValueError("port must be an integer") from exc

        if port < 1 or port > self.port_count:
            raise ValueError(f"port must be in range 1..{self.port_count}")
        return port

    def _require_healthy(self) -> None:
        if self.health == "unhealthy":
            detail = f": {self.health_reason}" if self.health_reason else ""
            raise ValueError(
                f"controller is unhealthy; device mutations are unavailable{detail}"
            )

    def _to_dev(self, ui_port: int) -> int:
        try:
            return self.port_map.to_dev(ui_port)
        except Exception as exc:
            raise ValueError(f"no device mapping for UI port {ui_port}") from exc

    def _to_ui(self, dev_port: int) -> int:
        try:
            return self.port_map.to_ui(dev_port)
        except Exception as exc:
            raise ValueError(f"no UI mapping for device port {dev_port}") from exc

    def _ingress_for_egress(self, egress: int, exclude: int | None = None) -> int | None:
        for ingress, mapped_egress in self.mappings.items():
            if ingress != exclude and mapped_egress == egress:
                return ingress
        return None

    def _removal_preview(self, ingress: int, old: int | None, other: int | None) -> list[dict]:
        removed = []
        seen = set()
        for pair in ((ingress, old), (other, self.mappings.get(other) if other is not None else None)):
            remove_ingress, remove_egress = pair
            if remove_ingress is None or remove_egress is None:
                continue
            if remove_ingress in seen:
                continue
            removed.append({"ingress": remove_ingress, "egress": remove_egress})
            seen.add(remove_ingress)
        return removed

    async def _persist(self) -> bool:
        try:
            await self._call(self.store.save_state, self.mappings, self.labels)
        except Exception:
            self.sync = "out_of_sync"
            return False

        if self.sync == "out_of_sync":
            self.sync = "in_sync"
        return True

    async def _backend_reachable(self) -> bool:
        self._last_status_error = None
        for attempt in range(3):
            try:
                if await self._call(self.backend.status):
                    return True
                # A backend that reports False rather than raising may still
                # have kept the underlying cause.
                self._last_status_error = getattr(self.backend, "last_error", None)
            except Exception as exc:
                self._last_status_error = exc
            if attempt < 2:
                await asyncio.sleep(0.01)
        return False

    def _unreachable_reason(self) -> str:
        """Describe an unreachable backend, naming the usual bfrt causes."""
        target = getattr(self.backend, "grpc_target", None)
        where = f" at {target}" if target else ""
        exc = self._last_status_error

        if isinstance(exc, ModuleNotFoundError) and "bfrt_grpc" in str(exc):
            # The SDE ships bfrt_grpc; it is not on PyPI. This is the failure
            # every first-time bfrt user hits, so name the fix, not the symptom.
            return (
                "the SDE's bfrt_grpc module is not importable — run Polista with the "
                "SDE's Python and set PYTHONPATH="
                "$SDE_INSTALL/lib/python3.10/site-packages/tofino"
            )
        if exc is not None:
            return f"device backend{where} is unreachable: {exc}"
        return (
            f"device backend{where} is unreachable — check that bf_switchd is running "
            "and serving BF Runtime gRPC"
        )

    async def _call(self, func, *args):
        return await asyncio.to_thread(func, *args)
