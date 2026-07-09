import base64
import json
import urllib.error
import urllib.request


class PortofinoError(Exception):
    pass


class ConflictError(PortofinoError):
    def __init__(self, would_remove):
        super().__init__("mapping conflict")
        self.would_remove = would_remove


class PortofinoClient:
    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = base_url.rstrip("/")
        token = f"{username}:{password}".encode("utf-8")
        self.authorization = "Basic " + base64.b64encode(token).decode("ascii")

    def get_health(self) -> dict:
        return self._request("GET", "/health")

    def get_ports(self) -> dict:
        return self._request("GET", "/ports")

    def get_mappings(self) -> list[dict]:
        return self._request("GET", "/mappings")["mappings"]

    def connect(self, ingress, egress, force=False) -> dict:
        return self._request(
            "POST",
            "/mappings",
            {"ingress": ingress, "egress": egress, "force": force},
        )

    def disconnect(self, ingress, egress) -> dict:
        return self._request(
            "DELETE",
            "/mappings",
            {"ingress": ingress, "egress": egress},
        )

    def refresh(self) -> dict:
        return self._request("POST", "/refresh")

    def get_labels(self) -> dict:
        return self._request("GET", "/labels")

    def set_label(self, port, label) -> dict:
        return self._request("PUT", f"/labels/{port}", {"label": label})

    def _request(self, method: str, path: str, body=None):
        data = None
        headers = {
            "Authorization": self.authorization,
            "Accept": "application/json",
        }

        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers=headers,
            method=method,
        )

        try:
            with urllib.request.urlopen(request) as response:
                response_text = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            response_text = exc.read().decode("utf-8", errors="replace")
            if exc.code == 409 and method == "POST" and path == "/mappings":
                conflict_body = self._load_json(response_text)
                raise ConflictError(conflict_body["would_remove"]) from exc
            raise PortofinoError(
                f"HTTP {exc.code}: {response_text}"
            ) from exc
        except urllib.error.URLError as exc:
            raise PortofinoError(str(exc)) from exc

        return self._load_json(response_text)

    def _load_json(self, response_text: str):
        if not response_text:
            return {}
        return json.loads(response_text)
