from app.tofino.backend import TofinoBackend


class FakeBackend(TofinoBackend):
    def __init__(self) -> None:
        self._entries: dict[int, int] = {}

    def status(self) -> bool:
        return True

    def read_all(self) -> list[tuple[int, int]]:
        return list(self._entries.items())

    def write_entry(self, ingress_dev: int, egress_dev: int) -> None:
        self._entries[ingress_dev] = egress_dev

    def delete_entry(self, ingress_dev: int) -> None:
        del self._entries[ingress_dev]

    def clear_all(self) -> None:
        self._entries.clear()
