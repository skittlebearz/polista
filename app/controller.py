from __future__ import annotations

import asyncio


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

    async def connect(self, ingress, egress, force=False) -> dict:
        async with self._lock:
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
                await self._call(self.backend.delete_entry, self._to_dev(other))
            await self._call(self.backend.write_entry, ingress_dev, egress_dev)

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
            ingress = self._validate_port(ingress)
            egress = self._validate_port(egress)
            if self.mappings.get(ingress) != egress:
                raise ValueError("mapping does not exist")

            await self._call(self.backend.delete_entry, self._to_dev(ingress))
            self.mappings.pop(ingress, None)
            await self._persist()
            return {"status": "ok", "sync_state": self.sync}

    async def refresh(self) -> dict:
        async with self._lock:
            entries = await self._call(self.backend.read_all)
            self.mappings = {
                self._to_ui(ingress_dev): self._to_ui(egress_dev)
                for ingress_dev, egress_dev in entries
            }
            await self._persist()
            return {"status": "ok", "source": "tofino"}

    async def reconcile(self) -> None:
        async with self._lock:
            try:
                mappings, labels = await self._call(self.store.load_state)
            except Exception:
                self.health = "unhealthy"
                return

            if not await self._backend_reachable():
                self.health = "unhealthy"
                return

            try:
                await self._call(self.backend.clear_all)
            except Exception:
                self.health = "unhealthy"
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
                except ValueError:
                    self.health = "unhealthy"
                    return
                except Exception:
                    if await self._backend_reachable():
                        self.sync = "partial_sync"
                    else:
                        self.health = "unhealthy"
                    return

            self.health = "healthy"
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

    async def _persist(self) -> None:
        try:
            await self._call(self.store.save_state, self.mappings, self.labels)
        except Exception:
            self.sync = "out_of_sync"
            return

        if self.health == "healthy":
            self.sync = "in_sync"

    async def _backend_reachable(self) -> bool:
        for attempt in range(3):
            try:
                if await self._call(self.backend.status):
                    return True
            except Exception:
                pass
            if attempt < 2:
                await asyncio.sleep(0.01)
        return False

    async def _call(self, func, *args):
        return await asyncio.to_thread(func, *args)
