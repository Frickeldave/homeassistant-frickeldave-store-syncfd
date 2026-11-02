"""Feature-Handler der Erweiterung: ``publish`` und ``import``.

Die Basis routet und authentifiziert; hier liegt die fachliche Validierung. Die
Payloads und Antworten sind identisch zu den bisherigen Endpunkten
``POST /api/frickeldave_store/publish`` und ``POST /api/frickeldave_store/import/catalog``,
damit das Panel nach der Umstellung auf
``POST /api/frickeldave_store/features/{feature}`` unverändert funktioniert.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Final

from .base_api import BaseApi
from .const import (
    FEATURE_IMPORT,
    FEATURE_PUBLISH,
    FEATURE_TITLE_IMPORT,
    FEATURE_TITLE_PUBLISH,
)
from .errors import SyncError
from .importer import ImportOptions, async_import_catalog
from .publish import PUBLISH_TARGETS, PublishService
from .settings import SyncSettings

PUBLISH_FIELDS: Final[frozenset[str]] = frozenset(
    {"target", "dry_run", "message", "allow_empty", "confirm_removals"}
)
IMPORT_FIELDS: Final[frozenset[str]] = frozenset(
    {"source_url", "image_base_url", "import_images", "dry_run", "overwrite"}
)

MISSING_TOKEN_REASON: Final = (
    "Es ist kein GitHub-Token hinterlegt. Bitte unter Einstellungen → "
    "Frickeldave Store Sync ergänzen."
)


def _error(code: str, message: str) -> SyncError:
    return SyncError(message, code=code)


def payload_mapping(payload: Any) -> dict[str, Any]:
    """Body der Anfrage: nur ein JSON-Objekt ist zulässig."""

    if payload is None:
        return {}
    if not isinstance(payload, Mapping):
        raise _error("validation_error", "Anfragekörper muss ein JSON-Objekt sein.")
    return dict(payload)


def _reject_unknown(payload: Mapping[str, Any], allowed: frozenset[str]) -> None:
    unknown = set(payload) - allowed
    if unknown:
        raise _error("validation_error", f"Unbekannte Felder: {', '.join(sorted(unknown))}")


def _flag(payload: Mapping[str, Any], key: str, default: bool = False) -> bool:
    value = payload.get(key, default)
    if not isinstance(value, bool):
        raise _error("validation_error", f"{key} muss ein Wahrheitswert sein.")
    return value


def validate_publish_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Payload für Veröffentlichen prüfen (identisch für Alt- und Feature-Route)."""

    _reject_unknown(payload, PUBLISH_FIELDS)
    target = payload.get("target")
    if target not in PUBLISH_TARGETS:
        raise _error("validation_error", "target muss 'dev' oder 'prod' sein.")
    message = payload.get("message", "")
    if not isinstance(message, str):
        raise _error("validation_error", "message muss eine Zeichenkette sein.")
    return {
        "target": str(target),
        "dry_run": _flag(payload, "dry_run"),
        "message": message,
        "allow_empty": _flag(payload, "allow_empty"),
        # Löschschutz übergehen: eigene, ausdrückliche Bestätigung je Lauf.
        "confirm_removals": _flag(payload, "confirm_removals"),
    }


def _optional_url(payload: Mapping[str, Any], key: str) -> str:
    if key not in payload:
        return ""
    value = payload[key]
    if not isinstance(value, str) or not value.strip():
        raise _error("validation_error", f"{key} muss eine absolute https-URL sein.")
    return value.strip()


def import_requires_admin(payload: Mapping[str, Any]) -> bool:
    """Eigene Quellen können interne Adressen ansprechen: nur App-Admins."""

    return "source_url" in payload or "image_base_url" in payload


def validate_import_payload(payload: Mapping[str, Any]) -> ImportOptions:
    """Payload für den Katalogimport prüfen (identisch für Alt- und Feature-Route)."""

    _reject_unknown(payload, IMPORT_FIELDS)
    return ImportOptions(
        source_url=_optional_url(payload, "source_url"),
        image_base_url=_optional_url(payload, "image_base_url"),
        import_images=_flag(payload, "import_images", True),
        dry_run=_flag(payload, "dry_run"),
        # Überschreiben bleibt bei der Rolle ``product``: es ist ein Sammel-Update
        # auf Daten, die diese Rolle einzeln ändern darf. Die Basis legt vor dem
        # ersten Schreiben einen Snapshot an.
        overwrite=_flag(payload, "overwrite"),
    )


def _handled(api: BaseApi, handler: Callable[[Any], Any]) -> Callable[[Any], Any]:
    """Fehler in den ``ExtensionError`` des Vertrags übersetzen und antworten."""

    async def _run(request: Any) -> Any:
        try:
            payload = await handler(request)
        except SyncError as err:
            raise api.error(err.code, err.message, err.status) from err
        return api.response(payload)

    return _run


def build_features(
    api: BaseApi,
    settings_provider: Callable[[], SyncSettings],
    *,
    client_factory: Callable[[SyncSettings], Any] | None = None,
    session_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Feature-Specs für ``publish`` und ``import`` inklusive Handler."""

    async def _publish(request: Any) -> dict[str, Any]:
        payload = validate_publish_payload(payload_mapping(request.payload))
        service = PublishService(request.facade, settings_provider(), client_factory=client_factory)
        result: dict[str, Any] = await service.async_publish(
            payload["target"],
            dry_run=payload["dry_run"],
            message=payload["message"],
            user_id=str(getattr(request, "user_id", "")),
            allow_empty=payload["allow_empty"],
            confirm_removals=payload["confirm_removals"],
        )
        return result

    async def _import(request: Any) -> dict[str, Any]:
        raw = payload_mapping(request.payload)
        options = validate_import_payload(raw)
        if import_requires_admin(raw) and not request.facade.can(api.role_app_admin):
            raise _error(
                "forbidden",
                "Eigene Quellen (source_url/image_base_url) darf nur die Rolle "
                "app_admin importieren.",
            )
        result: dict[str, Any] = await async_import_catalog(
            request.facade,
            settings_provider(),
            options,
            api=api,
            session=session_factory() if session_factory is not None else None,
        )
        return result

    settings = settings_provider()
    publish_ready = settings.token_set
    return {
        FEATURE_PUBLISH: api.spec(
            name=FEATURE_PUBLISH,
            title=FEATURE_TITLE_PUBLISH,
            required_role=api.role_app_admin,
            provides_panel_routes=True,
            ready=publish_ready,
            reason="" if publish_ready else MISSING_TOKEN_REASON,
            handler=_handled(api, _publish),
        ),
        FEATURE_IMPORT: api.spec(
            name=FEATURE_IMPORT,
            title=FEATURE_TITLE_IMPORT,
            required_role=api.role_product,
            provides_panel_routes=True,
            ready=True,
            reason="",
            handler=_handled(api, _import),
        ),
    }


__all__ = [
    "IMPORT_FIELDS",
    "MISSING_TOKEN_REASON",
    "PUBLISH_FIELDS",
    "build_features",
    "import_requires_admin",
    "payload_mapping",
    "validate_import_payload",
    "validate_publish_payload",
]
