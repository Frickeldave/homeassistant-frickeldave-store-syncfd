"""Config Flow und Options Flow der Erweiterung ``frickeldave_store_syncfd``.

Repository, Branches, Workflow, Token und Importquellen liegen hier – nicht mehr
im Store der Basis. Der Token ist write-only: Das Formular zeigt ihn nie an, ein
leeres Feld behält den gespeicherten Wert, und weder Antwort noch Protokoll
enthalten ihn.
"""

from __future__ import annotations

import re
from typing import Any, Final

try:  # HA 2026.10+ ships probatio as the schema library
    import probatio as schema_lib
except ImportError:  # pragma: no cover - HA <= 2026.9
    import voluptuous as schema_lib

from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    BASE_DOMAIN,
    DEFAULT_CATALOG_URL,
    DEFAULT_IMAGE_BASE_URL,
    DEFAULT_PUBLISH_DEV_BRANCH,
    DEFAULT_PUBLISH_PROD_BRANCH,
    DEFAULT_PUBLISH_PROD_WORKFLOW,
    DEFAULT_PUBLISH_REPOSITORY,
    DOMAIN,
    NAME,
)
from .errors import SyncError
from .importer import validate_import_url
from .settings import SyncSettings

VERSION = 1
MINOR_VERSION = 1

_REPOSITORY_RE: Final = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_REF_RE: Final = re.compile(r"^[A-Za-z0-9._/-]+$")
_WORKFLOW_RE: Final = re.compile(r"^[A-Za-z0-9._/-]+\.ya?ml$")

#: Reihenfolge der Optionsfelder (bestimmt die Oberfläche).
OPTION_FIELDS: Final[tuple[str, ...]] = (
    "repository",
    "dev_branch",
    "prod_branch",
    "prod_workflow",
    "token",
    "catalog_url",
    "image_base_url",
)

DEFAULT_OPTIONS: Final[dict[str, str]] = {
    "repository": DEFAULT_PUBLISH_REPOSITORY,
    "dev_branch": DEFAULT_PUBLISH_DEV_BRANCH,
    "prod_branch": DEFAULT_PUBLISH_PROD_BRANCH,
    "prod_workflow": DEFAULT_PUBLISH_PROD_WORKFLOW,
    "token": "",
    "catalog_url": DEFAULT_CATALOG_URL,
    "image_base_url": DEFAULT_IMAGE_BASE_URL,
}


class _OptionsInvalid(Exception):
    """Feldbezogene Fehler des Options Flow (deutsche Übersetzungsschlüssel)."""

    def __init__(self, errors: dict[str, str]) -> None:
        super().__init__(", ".join(sorted(errors)))
        self.errors = errors


def _schema() -> Any:
    """Felder des Options Flow in fester Reihenfolge; der Token ist ein Passwortfeld."""
    fields: dict[Any, Any] = {}
    for name in OPTION_FIELDS:
        if name == "token":
            fields[schema_lib.Optional(name)] = TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD)
            )
        else:
            fields[schema_lib.Optional(name)] = TextSelector()
    return schema_lib.Schema(fields)


def _suggested(current: SyncSettings) -> dict[str, Any]:
    """Vorbelegung des Formulars – ohne den gespeicherten Token."""

    return {
        "repository": current.repository,
        "dev_branch": current.dev_branch,
        "prod_branch": current.prod_branch,
        "prod_workflow": current.prod_workflow,
        "token": "",
        "catalog_url": current.catalog_url,
        "image_base_url": current.image_base_url,
    }


def validate_options(user_input: Any, current: SyncSettings) -> dict[str, str]:
    """Options prüfen; liefert die zu speichernden Werte (Token nur bei Eingabe)."""

    values = dict(user_input or {})
    errors: dict[str, str] = {}

    repository = str(values.get("repository", "") or "").strip()
    if not _REPOSITORY_RE.match(repository):
        errors["repository"] = "invalid_repository"

    branches: dict[str, str] = {}
    for key in ("dev_branch", "prod_branch"):
        value = str(values.get(key, "") or "").strip()
        if not _REF_RE.match(value):
            errors[key] = "invalid_branch"
        branches[key] = value

    workflow = str(values.get("prod_workflow", "") or "").strip()
    if not _WORKFLOW_RE.match(workflow):
        errors["prod_workflow"] = "invalid_workflow"

    catalog_url = str(values.get("catalog_url", "") or "").strip()
    try:
        catalog_url = validate_import_url(catalog_url, label="catalog_url")
    except SyncError:
        errors["catalog_url"] = "invalid_source_url"

    image_base_url = str(values.get("image_base_url", "") or "").strip()
    try:
        image_base_url = validate_import_url(image_base_url, label="image_base_url")
    except SyncError:
        errors["image_base_url"] = "invalid_image_url"

    if errors:
        raise _OptionsInvalid(errors)

    token_input = str(values.get("token", "") or "").strip()
    return {
        "repository": repository,
        "dev_branch": branches["dev_branch"],
        "prod_branch": branches["prod_branch"],
        "prod_workflow": workflow,
        # Ein leeres Feld behält den gespeicherten Token.
        "token": token_input or current.token,
        "catalog_url": catalog_url,
        "image_base_url": image_base_url,
    }


class FrickeldaveStoreSyncConfigFlow(ConfigFlow, domain=DOMAIN):  # type: ignore[call-arg]
    """Einrichtung über die HA-Oberfläche."""

    VERSION = VERSION
    MINOR_VERSION = MINOR_VERSION

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Prüft die Basis und legt genau einen Eintrag an."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        if BASE_DOMAIN not in self.hass.config.components:
            return self.async_abort(reason="base_missing")

        if user_input is not None:
            return self.async_create_entry(title=NAME, data={}, options=dict(DEFAULT_OPTIONS))

        return self.async_show_form(step_id="user")

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Liefert den Options Flow der Erweiterung."""
        return FrickeldaveStoreSyncOptionsFlow(config_entry)


class FrickeldaveStoreSyncOptionsFlow(OptionsFlow):
    """Repository, Branches, Workflow, Token und Importquellen."""

    def __init__(self, config_entry: ConfigEntry | None = None) -> None:
        super().__init__()
        self._entry = config_entry

    @property
    def entry(self) -> ConfigEntry:
        """Config Entry des Flows, unabhängig von der HA-Version."""
        if self._entry is not None:
            return self._entry
        entry = getattr(self, "config_entry", None)
        if entry is None:  # pragma: no cover - von HA immer gesetzt
            raise RuntimeError("Config Entry fehlt")
        return entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Einstellungen prüfen und speichern."""
        current = SyncSettings.from_entry(self.entry)
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                values = validate_options(user_input, current)
            except _OptionsInvalid as err:
                errors = dict(err.errors)
            else:
                return self.async_create_entry(title="", data=values)

        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                _schema(), {**_suggested(current), **dict(user_input or {})}
            ),
            errors=errors,
        )


__all__ = [
    "DEFAULT_OPTIONS",
    "FrickeldaveStoreSyncConfigFlow",
    "FrickeldaveStoreSyncOptionsFlow",
    "OPTION_FIELDS",
    "validate_options",
]
