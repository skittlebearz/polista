import json
import os
import tempfile
from pathlib import Path


class Store:
    def __init__(self, mappings_file):
        self.path = Path(mappings_file)

    def load_state(self):
        if not self.path.exists():
            return {}, {}

        with self.path.open() as f:
            data = json.load(f)

        mappings = {
            int(item["ingress"]): int(item["egress"])
            for item in data.get("mappings", [])
        }
        labels = {int(port): label for port, label in data.get("labels", {}).items()}
        return mappings, labels

    def save_state(self, mappings, labels):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_pattern = f".{self.path.name}.*.tmp"
        for stale_temp in self.path.parent.glob(temp_pattern):
            if stale_temp.is_file():
                stale_temp.unlink()

        data = {
            "mappings": [
                {"ingress": ingress, "egress": egress}
                for ingress, egress in mappings.items()
            ],
            "labels": {str(port): label for port, label in labels.items()},
            "last_sync_status": "ok",
        }

        temp_name = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as f:
                temp_name = f.name
                json.dump(data, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_name, self.path)
            directory_fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temp_name is not None and os.path.exists(temp_name):
                os.unlink(temp_name)


def load_auth(path):
    path = Path(path)
    if not path.exists():
        return None

    with path.open() as f:
        return json.load(f)
