"""Setup der Erweiterung ``frickeldave_store_syncfd``.

Die Erweiterung ist ohne die Basis ``frickeldave_store`` funktionslos. Sie
registriert ihre Features beim Erweiterungspunkt der Basis (Contract 1) und
bekommt von dort eine Fassade auf den Store. Die fachlichen Handler für
``publish`` und ``import`` entstehen in Arbeitspaket 5/6; dieses Gerüst prüft
die Basis, nimmt die Registrierung vor, sobald der Erweiterungspunkt vorhanden
ist, und meldet sich beim Entladen wieder ab.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine
from typing import Any, Final

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady

from .const import (
    BASE_DOMAIN,
    CONTRACT_VERSION,
    DOMAIN,
    MIN_BASE_CONTRACT_VERSION,
    NAME,
    VERSION,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS: Final[list[str]] = []


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Prüft die Basis und registriert die Erweiterung beim Erweiterungspunkt."""
    if BASE_DOMAIN not in hass.config.components:
        raise ConfigEntryNotReady(
            f"Die Basis-Integration {BASE_DOMAIN} ist nicht eingerichtet. "
            f"{NAME} benötigt sie, bevor die Erweiterung starten kann."
        )

    unregister = await _async_register_with_base(hass)
    if unregister is not None:
        entry.async_on_unload(unregister)

    _LOGGER.debug("%s %s eingerichtet", NAME, VERSION)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Entlädt die Erweiterung; die Abmeldung läuft über ``entry.async_on_unload``."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_register_with_base(
    hass: HomeAssistant,
) -> Callable[[], Coroutine[Any, Any, None]] | None:
    """Registriert die Erweiterung bei der Basis und liefert die Abmeldefunktion.

    Der Erweiterungspunkt entsteht in der Basis mit Arbeitspaket 2. Fehlt er
    noch, startet das Gerüst ohne Registrierung weiter, damit die Erweiterung
    installierbar und prüfbar bleibt. Arbeitspaket 5 ersetzt den leeren
    ``features``-Satz durch die Handler für ``publish`` und ``import``.
    """
    try:
        from custom_components.frickeldave_store.extension import (  # type: ignore[import-not-found]
            ExtensionError,
            ExtensionRegistration,
            async_register_extension,
            async_unregister_extension,
        )
    except ImportError:
        _LOGGER.warning(
            "Die Basis %s stellt den Erweiterungspunkt (Contract %s) noch nicht "
            "bereit; %s startet ohne registrierte Features.",
            BASE_DOMAIN,
            MIN_BASE_CONTRACT_VERSION,
            NAME,
        )
        return None

    registration = ExtensionRegistration(
        name=DOMAIN,
        version=VERSION,
        contract_version=CONTRACT_VERSION,
        features={},
    )
    try:
        await async_register_extension(hass, registration)
    except ExtensionError as err:
        raise ConfigEntryError(
            f"Die Registrierung bei {BASE_DOMAIN} ist fehlgeschlagen: {err}"
        ) from err

    async def _async_unregister() -> None:
        try:
            await async_unregister_extension(hass, DOMAIN)
        except ExtensionError:
            # Beim Entladen ist die Abmeldung idempotent; eine bereits
            # abgemeldete Erweiterung darf den Unload nicht scheitern lassen.
            _LOGGER.debug("%s war beim Entladen nicht registriert", DOMAIN)

    return _async_unregister
