"""Fake des Erweiterungspunkts der Basis (Contract 1) für die Tests.

Die echte Basis ist im CI dieser Erweiterung nicht installiert. Dieser Fake
bildet die öffentliche Form nach, gegen die die Handler laufen: Registrierung,
Rollenprüfung, Feature-Aufruf und strukturierte Fehler.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import pytest

CONTRACT_VERSION = 1
ROLE_PRODUCT = "product"
ROLE_FINANCE = "finance"
ROLE_APP_ADMIN = "app_admin"

#: Ablage der Registrierungen in ``hass.data`` (wie in der Basis).
STATE_KEY = "frickeldave_store_extensions"


class ExtensionError(Exception):
    """Strukturierter Fehler des Erweiterungsvertrags."""

    def __init__(self, code: str, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = 409 if code == "feature_unavailable" else (status or 400)

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True, slots=True)
class FeatureRequest:
    feature: str
    payload: Mapping[str, Any]
    user_id: str
    user_name: str
    facade: Any


@dataclass(frozen=True, slots=True)
class FeatureResponse:
    payload: Mapping[str, Any]
    status: int = 200


@dataclass(frozen=True, slots=True)
class FeatureSpec:
    name: str
    title: str
    required_role: str | None = None
    provides_panel_routes: bool = False
    ready: bool = True
    reason: str = ""
    handler: Any = None


@dataclass(frozen=True, slots=True)
class ExtensionRegistration:
    name: str
    version: str
    contract_version: int = CONTRACT_VERSION
    features: Mapping[str, Any] = field(default_factory=dict)
    panel_actions: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ImageUpload:
    name: str
    data: bytes
    is_main: bool = False


@dataclass(frozen=True, slots=True)
class ImportItem:
    article_number: str | None
    payload: Mapping[str, Any]
    images: tuple[ImageUpload, ...] = ()


async def async_register_extension(hass: Any, registration: Any) -> Any:
    """Registrierung mit denselben Prüfungen wie die Basis."""

    if not isinstance(registration, ExtensionRegistration):
        raise ExtensionError("invalid_registration", "Registrierung fehlt.")
    if registration.contract_version != CONTRACT_VERSION:
        raise ExtensionError(
            "contract_mismatch",
            f"Vertrag {registration.contract_version} wird nicht unterstützt.",
        )
    for name, spec in registration.features.items():
        if not isinstance(spec, FeatureSpec):
            raise ExtensionError("invalid_registration", f"Feature '{name}' ist keine Spec.")
        if spec.name != name:
            raise ExtensionError("invalid_registration", f"Feature '{name}' nennt sich anders.")
        if not str(spec.title).strip():
            raise ExtensionError("invalid_registration", f"Feature '{name}' braucht einen Titel.")
    registry: dict[str, Any] = hass.data.setdefault(STATE_KEY, {})
    registry[registration.name] = registration
    return registration


async def async_unregister_extension(hass: Any, name: str) -> None:
    registry: dict[str, Any] = hass.data.setdefault(STATE_KEY, {})
    if registry.pop(str(name), None) is None:
        raise ExtensionError(
            "not_registered",
            f"Es ist keine Erweiterung mit dem Namen '{name}' registriert.",
            status=404,
        )


@dataclass(slots=True)
class CallResult:
    """Ergebnis eines Feature-Aufrufs wie aus ``POST /features/{feature}``."""

    status: int
    payload: dict[str, Any]

    @property
    def error(self) -> dict[str, str]:
        return dict(self.payload.get("error", {}))


class FakeBase:
    """Zugriff auf die Registrierungen und Aufruf der Handler."""

    def __init__(self, hass: Any) -> None:
        self.hass = hass

    @property
    def registry(self) -> dict[str, Any]:
        return self.hass.data.get(STATE_KEY, {})

    def registration(self, name: str) -> Any:
        return self.registry[name]

    def feature(self, extension: str, feature: str) -> Any:
        return self.registry[extension].features[feature]

    async def call(
        self,
        feature: str,
        payload: Mapping[str, Any] | None = None,
        *,
        facade: Any,
        user_id: str = "user",
        user_name: str = "Test",
    ) -> CallResult:
        """Feature aufrufen; Rollenprüfung und Antwort wie in der Basis."""

        spec = None
        for registration in self.registry.values():
            if feature in registration.features:
                spec = registration.features[feature]
                break
        if spec is None:
            return CallResult(
                404,
                {
                    "error": {
                        "code": "feature_unknown",
                        "message": f"Unbekannte Fähigkeit: {feature!r}.",
                    }
                },
            )
        try:
            if not spec.ready:
                raise ExtensionError(
                    "feature_unavailable",
                    spec.reason or "Diese Fähigkeit ist derzeit nicht verfügbar.",
                )
            if spec.handler is None:
                raise ExtensionError(
                    "feature_unavailable", "Diese Fähigkeit nimmt keine Anfragen entgegen."
                )
            if spec.required_role is not None:
                facade.require(spec.required_role)
            response = await spec.handler(
                FeatureRequest(feature, payload or {}, user_id, user_name, facade)
            )
            if not isinstance(response, FeatureResponse):
                raise ExtensionError(
                    "handler_failed",
                    "Die Erweiterung hat eine ungültige Antwort geliefert.",
                    status=502,
                )
            return CallResult(response.status, dict(response.payload))
        except ExtensionError as err:
            return CallResult(err.status, {"error": err.as_dict()})
        except Exception as err:  # noqa: BLE001 - wie die Basis: fremde Fehler strukturieren
            code = getattr(err, "code", None)
            if not code:
                raise
            status = 403 if code == "forbidden" else int(getattr(err, "status", 400))
            return CallResult(status, {"error": {"code": code, "message": str(err)}})


def build_module() -> types.ModuleType:
    """Modul mit derselben öffentlichen Form wie ``frickeldave_store.extension``."""

    module = types.ModuleType("custom_components.frickeldave_store.extension")
    module.CONTRACT_VERSION = CONTRACT_VERSION
    module.ROLE_PRODUCT = ROLE_PRODUCT
    module.ROLE_FINANCE = ROLE_FINANCE
    module.ROLE_APP_ADMIN = ROLE_APP_ADMIN
    module.ExtensionError = ExtensionError
    module.FeatureRequest = FeatureRequest
    module.FeatureResponse = FeatureResponse
    module.FeatureSpec = FeatureSpec
    module.ExtensionRegistration = ExtensionRegistration
    module.ImageUpload = ImageUpload
    module.ImportItem = ImportItem
    module.async_register_extension = async_register_extension
    module.async_unregister_extension = async_unregister_extension
    return module


def install(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """Fake als ``custom_components.frickeldave_store.extension`` registrieren."""

    module = build_module()
    package = types.ModuleType("custom_components.frickeldave_store")
    package.extension = module
    monkeypatch.setitem(sys.modules, "custom_components.frickeldave_store", package)
    monkeypatch.setitem(sys.modules, "custom_components.frickeldave_store.extension", module)
    return module
