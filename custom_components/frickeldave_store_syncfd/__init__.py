"""Setup der Erweiterung ``frickeldave_store_syncfd``.

Die Erweiterung ist ohne die Basis ``frickeldave_store`` funktionslos. Sie
prüft die Basis und den Erweiterungspunkt (Contract 1), registriert dort die
Features ``publish`` und ``import`` und meldet sich beim Entladen wieder ab.
Fehlt die Basis oder passt der Vertrag nicht, scheitert das Setup mit einer
deutschen Meldung als ``ConfigEntryNotReady``.
"""

from __future__ import annotations

import logging
from functools import partial
from typing import Final

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady

from .base_api import BaseApi, BaseUnavailable, load_base_api
from .const import (
    BASE_DOMAIN,
    CONTRACT_VERSION,
    DOMAIN,
    MIN_BASE_CONTRACT_VERSION,
    NAME,
    VERSION,
)
from .features import build_features
from .settings import SyncSettings

_LOGGER = logging.getLogger(__name__)

PLATFORMS: Final[list[str]] = []


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Basis prüfen und die Features beim Erweiterungspunkt registrieren."""
    if BASE_DOMAIN not in hass.config.components:
        raise ConfigEntryNotReady(
            f"Die Basis-Integration {BASE_DOMAIN} ist nicht eingerichtet. "
            f"{NAME} benötigt sie, bevor die Erweiterung starten kann."
        )

    try:
        api = load_base_api()
    except BaseUnavailable as err:
        raise ConfigEntryNotReady(str(err)) from err

    if api.contract_version != CONTRACT_VERSION:
        if api.contract_version < MIN_BASE_CONTRACT_VERSION:
            detail = (
                f"Die Basis spricht Vertrag {api.contract_version}, diese Erweiterung "
                f"benötigt mindestens Vertrag {MIN_BASE_CONTRACT_VERSION}."
            )
        else:
            detail = (
                f"Die Basis spricht Vertrag {api.contract_version}, diese Erweiterung "
                f"unterstützt Vertrag {CONTRACT_VERSION}. Bitte die Erweiterung "
                "aktualisieren."
            )
        raise ConfigEntryNotReady(detail)

    features = build_features(api, lambda: SyncSettings.from_entry(entry))
    registration = api.registration(
        name=DOMAIN,
        version=VERSION,
        contract_version=CONTRACT_VERSION,
        features=features,
    )
    try:
        await api.register(hass, registration)
    except Exception as err:
        raise ConfigEntryError(
            f"Die Registrierung bei {BASE_DOMAIN} ist fehlgeschlagen: {err}"
        ) from err

    entry.async_on_unload(partial(_async_unregister, api, hass))
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    _LOGGER.debug("%s %s eingerichtet", NAME, VERSION)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Entlädt die Erweiterung; die Abmeldung läuft über ``entry.async_on_unload``."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_unregister(api: BaseApi, hass: HomeAssistant) -> None:
    """Abmeldung beim Erweiterungspunkt; beim Entladen idempotent."""

    try:
        await api.unregister(hass, DOMAIN)
    except Exception:
        _LOGGER.debug("%s war beim Entladen nicht registriert", DOMAIN)


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Options geändert: Eintrag neu laden, damit ``ready``/``reason`` stimmen."""

    await hass.config_entries.async_reload(entry.entry_id)
