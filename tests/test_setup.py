"""Setup, Config Flow und Manifest der Erweiterung (Gerüst, Arbeitspaket 1)."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockModule,
    mock_integration,
)

from custom_components.frickeldave_store_syncfd.config_flow import (
    FrickeldaveStoreSyncConfigFlow,
    FrickeldaveStoreSyncOptionsFlow,
)
from custom_components.frickeldave_store_syncfd.const import (
    BASE_DOMAIN,
    CONTRACT_VERSION,
    DOMAIN,
    ISSUE_URL,
    MIN_BASE_CONTRACT_VERSION,
    NAME,
    REPOSITORY_URL,
    VERSION,
)

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / DOMAIN


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _flatten_keys(value: Any, prefix: str = "") -> dict[str, Any]:
    """Liefert alle Blatt-Schlüssel eines verschachtelten Objekts mit vollem Pfad."""
    if not isinstance(value, dict):
        return {prefix: value}
    keys: dict[str, Any] = {}
    for key, item in value.items():
        keys.update(_flatten_keys(item, f"{prefix}.{key}" if prefix else key))
    return keys


def _install_base(hass: HomeAssistant) -> None:
    """Stubbt die Basis-Integration, damit der Eintrag ohne sie nicht scheitert."""
    mock_integration(hass, MockModule(BASE_DOMAIN), built_in=False)
    hass.config.components.add(BASE_DOMAIN)


def test_manifest_matches_constants() -> None:
    """Manifest, hacs.json und pyproject nennen dieselbe Domain und Version."""
    manifest = _load_json(COMPONENT / "manifest.json")
    hacs = _load_json(ROOT / "hacs.json")
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]

    assert manifest["domain"] == DOMAIN
    assert manifest["name"] == NAME
    assert manifest["version"] == VERSION
    assert project["version"] == VERSION
    assert hacs["name"] == NAME

    assert manifest["dependencies"] == [BASE_DOMAIN]
    assert manifest["config_flow"] is True
    assert manifest["integration_type"] == "service"
    assert manifest["codeowners"] == ["@Frickeldave"]
    assert manifest["documentation"] == REPOSITORY_URL
    assert manifest["issue_tracker"] == f"{REPOSITORY_URL}/issues"
    assert manifest["iot_class"] == "cloud_push"
    assert manifest["requirements"] == []

    assert hacs["render_readme"] is True
    assert hacs["homeassistant"]
    assert MIN_BASE_CONTRACT_VERSION <= CONTRACT_VERSION
    assert ISSUE_URL.endswith("/289")


def test_translations_match_strings() -> None:
    """strings.json ist die Quelle; de und en haben dieselben Schlüssel."""
    strings = _flatten_keys(_load_json(COMPONENT / "strings.json"))
    for language in ("de", "en"):
        translated = _flatten_keys(_load_json(COMPONENT / "translations" / f"{language}.json"))
        assert translated.keys() == strings.keys(), f"Schlüssel weichen in {language} ab"
        assert all(isinstance(value, str) and value for value in translated.values())

    strings_source = _load_json(COMPONENT / "strings.json")
    assert strings_source["config"]["abort"]["base_missing"]


async def test_setup_entry_loads(hass: HomeAssistant) -> None:
    """Einrichtung lädt mit vorhandener Basis und entlädt wieder."""
    _install_base(hass)
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_setup_without_base_fails(hass: HomeAssistant) -> None:
    """Ohne Basis-Integration startet die Erweiterung nicht."""
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    # Der Loader lehnt die Auflösung von ``dependencies`` ab, bevor Setup läuft.
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_config_flow_creates_entry(hass: HomeAssistant) -> None:
    """Der Config Flow ist registriert, zeigt den User-Schritt und legt den Eintrag an."""
    _install_base(hass)

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input={})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == NAME
    assert result["data"] == {}


async def test_options_flow_keeps_options(hass: HomeAssistant) -> None:
    """Der Options Flow ist registriert und verliert keine gespeicherten Optionen."""
    _install_base(hass)
    options = {"repository": "Frickeldave/frickeldave.github.io"}
    entry = MockConfigEntry(domain=DOMAIN, data={}, options=dict(options))
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(result["flow_id"], user_input={})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == options


def test_options_flow_class_is_wired() -> None:
    """Der Options Flow ist der Klasse des Config Flows zugeordnet."""
    entry = MockConfigEntry(domain=DOMAIN)

    assert isinstance(
        FrickeldaveStoreSyncConfigFlow.async_get_options_flow(entry),
        FrickeldaveStoreSyncOptionsFlow,
    )
