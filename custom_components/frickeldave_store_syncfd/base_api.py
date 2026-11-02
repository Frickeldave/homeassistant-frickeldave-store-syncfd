"""Einziger Zugriff auf den Erweiterungspunkt der Basis (Contract 1).

Die Erweiterung importiert ausschließlich
``custom_components.frickeldave_store.extension``. Damit die Handler auch ohne
installierte Basis prüfbar bleiben, werden die öffentlichen Bausteine einmalig
in :class:`BaseApi` gebündelt; die Handler bekommen diese Instanz übergeben und
erzeugen ihre Antworten und Fehler damit.
"""

from __future__ import annotations

import importlib
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from types import ModuleType
from typing import Any, Final

from homeassistant.core import HomeAssistant

from .const import BASE_DOMAIN, MIN_BASE_CONTRACT_VERSION

BASE_MODULE: Final = f"custom_components.{BASE_DOMAIN}.extension"

#: Namen, ohne die keine Registrierung möglich ist (Vertrag 1).
REQUIRED_NAMES: Final[tuple[str, ...]] = (
    "CONTRACT_VERSION",
    "ROLE_PRODUCT",
    "ROLE_APP_ADMIN",
    "ExtensionError",
    "ExtensionRegistration",
    "FeatureRequest",
    "FeatureResponse",
    "FeatureSpec",
    "ImageUpload",
    "ImportItem",
    "async_register_extension",
    "async_unregister_extension",
)


class BaseUnavailable(RuntimeError):
    """Die Basis stellt den Erweiterungspunkt nicht (passend) bereit."""


@dataclass(frozen=True, slots=True)
class BaseApi:
    """Öffentliche Bausteine des Erweiterungspunkts als schmale Fassade."""

    contract_version: int
    role_product: str
    role_app_admin: str
    error: Callable[[str, str, int], Exception]
    response: Callable[..., Any]
    spec: Callable[..., Any]
    registration: Callable[..., Any]
    image_upload: Callable[..., Any]
    import_item: Callable[..., Any]
    register: Callable[[HomeAssistant, Any], Awaitable[Any]]
    unregister: Callable[[HomeAssistant, str], Awaitable[None]]


def load_base_api() -> BaseApi:
    """Erweiterungspunkt importieren; fehlt er, gibt es eine deutsche Meldung."""

    try:
        module = importlib.import_module(BASE_MODULE)
    except ImportError as err:
        raise BaseUnavailable(
            f"Die Basis-Integration {BASE_DOMAIN} stellt den Erweiterungspunkt "
            f"(Vertrag {MIN_BASE_CONTRACT_VERSION}) nicht bereit. Bitte die Basis "
            "installieren und einrichten."
        ) from err
    return api_from_module(module)


def _attr(module: ModuleType, name: str) -> Any:
    """Öffentlichen Namen aus dem Basismodul lesen (ohne ``getattr``-Konstante)."""

    return vars(module)[name]


def api_from_module(module: ModuleType) -> BaseApi:
    """:class:`BaseApi` aus einem geladenen Modul bauen (auch für Tests)."""

    missing = [name for name in REQUIRED_NAMES if name not in vars(module)]
    if missing:
        raise BaseUnavailable(
            "Der Erweiterungspunkt der Basis ist unvollständig (fehlt: "
            f"{', '.join(missing)}). Bitte die Basis aktualisieren."
        )

    contract_version = int(_attr(module, "CONTRACT_VERSION"))
    extension_error = _attr(module, "ExtensionError")
    feature_spec = _attr(module, "FeatureSpec")
    feature_response = _attr(module, "FeatureResponse")
    registration_class = _attr(module, "ExtensionRegistration")
    image_upload = _attr(module, "ImageUpload")
    import_item = _attr(module, "ImportItem")
    register_function = _attr(module, "async_register_extension")
    unregister_function = _attr(module, "async_unregister_extension")

    async def _register(hass: HomeAssistant, registration: Any) -> Any:
        result: Any = await register_function(hass, registration)
        return result

    async def _unregister(hass: HomeAssistant, name: str) -> None:
        await unregister_function(hass, name)

    def _error(code: str, message: str, status: int) -> Exception:
        error: Exception = extension_error(code, message, status=status)
        return error

    def _response(payload: Mapping[str, Any], status: int = 200) -> Any:
        return feature_response(payload, status)

    return BaseApi(
        contract_version=contract_version,
        role_product=str(_attr(module, "ROLE_PRODUCT")),
        role_app_admin=str(_attr(module, "ROLE_APP_ADMIN")),
        error=_error,
        response=_response,
        spec=feature_spec,
        registration=registration_class,
        image_upload=image_upload,
        import_item=import_item,
        register=_register,
        unregister=_unregister,
    )
