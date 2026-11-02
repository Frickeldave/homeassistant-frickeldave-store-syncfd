"""Config Flow und Options Flow der Erweiterung ``frickeldave_store_syncfd``.

Das Gerüst hat noch keine Felder. Repository, Branches, Workflow und Token der
Veröffentlichung sowie die Quellen des Katalogimports kommen in Arbeitspaket
5/6; bis dahin speichert der Options Flow die übergebenen Werte unverändert,
damit ``options`` erhalten bleibt.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult

from .const import BASE_DOMAIN, DOMAIN, NAME

VERSION = 1
MINOR_VERSION = 1


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
            return self.async_create_entry(title=NAME, data={})

        return self.async_show_form(step_id="user")

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Liefert den Options Flow der Erweiterung."""
        return FrickeldaveStoreSyncOptionsFlow(config_entry)


class FrickeldaveStoreSyncOptionsFlow(OptionsFlow):
    """Platzhalter für Token, Repository und Importquellen."""

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
        """Speichert die Optionen unverändert; Felder folgen in Arbeitspaket 5/6."""
        if user_input is not None:
            return self.async_create_entry(title="", data={**self.entry.options, **user_input})

        return self.async_show_form(step_id="init")
