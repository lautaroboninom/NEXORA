from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import requests
from django.conf import settings


class BejermanBridgeConfigError(RuntimeError):
    pass


class BejermanBridgeUnavailable(RuntimeError):
    pass


class BejermanBridgeResponseError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, response: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.response = response


@dataclass(frozen=True)
class BejermanBridgeClient:
    base_url: str
    token: str
    timeout: int

    @classmethod
    def from_settings(cls) -> "BejermanBridgeClient":
        base_url = (getattr(settings, "BEJERMAN_BRIDGE_BASE_URL", "") or "").strip().rstrip("/")
        token = (getattr(settings, "BEJERMAN_BRIDGE_SERVICE_TOKEN", "") or "").strip()
        timeout = int(getattr(settings, "BEJERMAN_BRIDGE_TIMEOUT", 30) or 30)
        if not base_url or not token:
            raise BejermanBridgeConfigError("Falta configurar BEJERMAN_BRIDGE_BASE_URL o BEJERMAN_BRIDGE_SERVICE_TOKEN")
        return cls(base_url=base_url, token=token, timeout=timeout)

    def _url(self, path: str, params: dict[str, Any] | None = None) -> str:
        clean_path = "/" + str(path or "").lstrip("/")
        url = f"{self.base_url}{clean_path}"
        if params:
            query = urlencode({k: v for k, v in params.items() if v is not None and str(v) != ""})
            if query:
                url = f"{url}?{query}"
        return url

    def _headers(self, accept: str = "application/json") -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": accept,
        }

    def get_json(self, path: str, params: dict[str, Any] | None = None, *, not_found_as_none: bool = False) -> dict[str, Any] | None:
        try:
            response = requests.get(
                self._url(path, params),
                headers=self._headers(),
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise BejermanBridgeUnavailable(f"No se pudo conectar con el bridge Bejerman: {exc}") from exc
        return self._parse_json_response(response, not_found_as_none=not_found_as_none)

    def post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        headers = self._headers()
        headers["Content-Type"] = "application/json"
        try:
            response = requests.post(
                self._url(path),
                headers=headers,
                json=payload,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise BejermanBridgeUnavailable(f"No se pudo conectar con el bridge Bejerman: {exc}") from exc
        parsed = self._parse_json_response(response, not_found_as_none=False)
        return parsed or {}

    def get_pdf(self, path: str, params: dict[str, Any]) -> tuple[bytes, str]:
        try:
            response = requests.get(
                self._url(path, params),
                headers=self._headers("application/pdf"),
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise BejermanBridgeUnavailable(f"No se pudo descargar el PDF Bejerman: {exc}") from exc
        if not response.ok:
            raise BejermanBridgeResponseError(
                _response_error_message(response),
                status_code=response.status_code,
                response=_safe_json(response),
            )
        return response.content, response.headers.get("content-type") or "application/pdf"

    def _parse_json_response(self, response: requests.Response, *, not_found_as_none: bool) -> dict[str, Any] | None:
        if response.status_code == 404 and not_found_as_none:
            return None
        parsed = _safe_json(response)
        if not response.ok:
            raise BejermanBridgeResponseError(
                _response_error_message(response, parsed),
                status_code=response.status_code,
                response=parsed,
            )
        if isinstance(parsed, dict):
            return parsed
        raise BejermanBridgeResponseError("El bridge Bejerman devolvió una respuesta inválida", status_code=response.status_code)


def _safe_json(response: requests.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _response_error_message(response: requests.Response, parsed: Any = None) -> str:
    if parsed is None:
        parsed = _safe_json(response)
    if isinstance(parsed, dict):
        detail = parsed.get("detail") or parsed.get("error")
        if detail:
            return str(detail)
    text = (response.text or "").strip()
    return text or f"Error del bridge Bejerman ({response.status_code})"
