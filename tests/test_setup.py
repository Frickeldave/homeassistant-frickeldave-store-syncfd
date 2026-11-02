"""Setup, Registrierung, Config Flow und Options Flow der Erweiterung."""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path
from typing import Any

import fake_base
import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockModule,
    mock_integration,
)

from custom_components.frickeldave_store_syncfd.config_flow import (
    DEFAULT_OPTIONS,
    FrickeldaveStoreSyncConfigFlow,
    FrickeldaveStoreSyncOptionsFlow,
    _suggested,
    validate_options,
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
from custom_components.frickeldave_store_syncfd.errors import SyncError
from custom_components.frickeldave_store_syncfd.features import MISSING_TOKEN_REASON
from custom_components.frickeldave_store_syncfd.settings import SyncSettings

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / DOMAIN

VALID_OPTIONS: dict[str, Any] = {
    "repository": "Frickeldave/frickeldave.github.io",
    "dev_branch": "dev",
    "prod_branch": "main",
    "prod_workflow": "fd-deploy-prd.yml",
    "token": "",
    "catalog_url": "https://frickeldave.de/data/handmade.json",
    "image_base_url": (
        "https://raw.githubusercontent.com/Frickeldave/frickeldave.github.io/dev/"
        "src/assets/handmade"
    ),
}


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


def _options(token: str = "") -> dict[str, Any]:
    return {**VALID_OPTIONS, "token": token}


async def _setup_entry(hass: HomeAssistant, **options: Any) -> MockConfigEntry:
    _install_base(hass)
    entry = MockConfigEntry(domain=DOMAIN, data={}, options=dict(options))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


def test_manifest_matches_constants() -> None:
    """Manifest, hacs.json und pyproject nennen dieselbe Domain und Version."""
    manifest = _load_json(COMPONENT / "manifest.json")
    hacs = _load_json(ROOT / "hacs.json")
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]

    assert manifest["domain"] == DOMAIN
    assert manifest["name"] == NAME
    assert manifest["version"] == VERSION == "0.1.0"
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

    source = _load_json(COMPONENT / "strings.json")
    assert source["config"]["abort"]["base_missing"]
    options = source["options"]["step"]["init"]
    for field in DEFAULT_OPTIONS:
        assert field in options["data"], f"Optionsfeld {field} fehlt in strings.json"
        assert field in options["data_description"]
    german = _load_json(COMPONENT / "translations" / "de.json")
    assert german["options"]["step"]["init"]["data"]["token"] == "GitHub-Token"


async def test_setup_entry_loads_and_unloads(hass: HomeAssistant) -> None:
    """Einrichtung lädt mit vorhandener Basis und meldet sich beim Unload ab."""
    entry = await _setup_entry(hass)
    fake = fake_base.FakeBase(hass)
    assert DOMAIN in fake.registry

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert fake.registry == {}


async def test_setup_without_base_fails(hass: HomeAssistant) -> None:
    """Ohne Basis-Integration startet die Erweiterung nicht."""
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    # Der Loader lehnt die Auflösung von ``dependencies`` ab, bevor Setup läuft.
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_setup_without_extension_point_retries(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fehlt der Erweiterungspunkt, gibt es eine deutsche Meldung und SETUP_RETRY."""
    _install_base(hass)
    monkeypatch.delitem(sys.modules, "custom_components.frickeldave_store.extension")
    monkeypatch.delitem(sys.modules, "custom_components.frickeldave_store")

    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_with_foreign_contract_retries(
    hass: HomeAssistant, fake_extension_point: Any
) -> None:
    """Ein zu neuer Vertrag verhindert den Start mit klarer Meldung."""
    fake_extension_point.CONTRACT_VERSION = 2
    _install_base(hass)
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_registers_publish_and_import(hass: HomeAssistant) -> None:
    """Contract 1 mit beiden Features, Rollen und ohne Token-Gate."""
    entry = await _setup_entry(hass, **DEFAULT_OPTIONS)
    fake = fake_base.FakeBase(hass)
    registration = fake.registration(DOMAIN)

    assert registration.version == VERSION
    assert registration.contract_version == CONTRACT_VERSION
    assert set(registration.features) == {"publish", "import"}

    publish = registration.features["publish"]
    assert publish.required_role == fake_base.ROLE_APP_ADMIN
    assert publish.provides_panel_routes is True
    # Ohne Token ist Veröffentlichen installiert, aber nicht eingerichtet.
    assert publish.ready is False
    assert publish.reason == MISSING_TOKEN_REASON

    katalogimport = registration.features["import"]
    assert katalogimport.required_role == fake_base.ROLE_PRODUCT
    assert katalogimport.ready is True
    assert katalogimport.reason == ""

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_publish_ready_with_token(hass: HomeAssistant) -> None:
    """Mit hinterlegtem Token ist Veröffentlichen verfügbar."""
    await _setup_entry(hass, **_options("ghp-secret"))
    fake = fake_base.FakeBase(hass)
    publish = fake.feature(DOMAIN, "publish")
    assert publish.ready is True
    assert publish.reason == ""


async def test_config_flow_creates_entry_with_defaults(hass: HomeAssistant) -> None:
    """Der Config Flow legt den Eintrag mit den frickeldave-Vorgaben an."""
    _install_base(hass)

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input={})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == NAME
    assert result["data"] == {}
    assert result["options"] == DEFAULT_OPTIONS


async def test_config_flow_requires_base_dependency(hass: HomeAssistant) -> None:
    """Ohne installierte Basis startet der Flow gar nicht (Manifest-Abhängigkeit)."""
    with pytest.raises(Exception) as err:
        await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert BASE_DOMAIN in str(err.value)


async def test_options_flow_shows_fields_and_never_the_token(hass: HomeAssistant) -> None:
    """Das Formular kennt alle Felder und zeigt den gespeicherten Token nicht."""
    entry = await _setup_entry(hass, **_options("ghp-secret"))

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    markers = list(result["data_schema"].schema)
    names = {getattr(marker, "schema", marker) for marker in markers}
    assert names == set(DEFAULT_OPTIONS)
    for marker in markers:
        suggested = (getattr(marker, "description", None) or {}).get("suggested_value")
        assert suggested != "ghp-secret", "Der gespeicherte Token darf nicht vorbelegt werden"

    assert await hass.config_entries.async_unload(entry.entry_id)


def test_options_form_never_suggests_the_token() -> None:
    """Die Vorbelegung enthält den Token nie, nur die übrigen Werte."""
    suggested = _suggested(SyncSettings(**{**DEFAULT_OPTIONS, "token": "ghp-secret"}))

    assert suggested["token"] == ""
    assert suggested["repository"] == VALID_OPTIONS["repository"]
    assert "ghp-secret" not in json.dumps(suggested)


async def test_options_flow_keeps_token_when_field_is_empty(hass: HomeAssistant) -> None:
    """Ein leeres Token-Feld behält den gespeicherten Wert."""
    entry = await _setup_entry(hass, **_options("ghp-secret"))

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={**VALID_OPTIONS, "token": ""}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["token"] == "ghp-secret"
    assert entry.options["repository"] == VALID_OPTIONS["repository"]


async def test_options_flow_stores_new_token(hass: HomeAssistant) -> None:
    """Ein neuer Token ersetzt den gespeicherten."""
    entry = await _setup_entry(hass, **_options("ghp-alt"))

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={**VALID_OPTIONS, "token": "ghp-neu"}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["token"] == "ghp-neu"


@pytest.mark.parametrize(
    ("field", "value", "error_key"),
    [
        ("repository", "ohne-slash", "repository"),
        ("repository", "owner/name/extra", "repository"),
        ("dev_branch", "dev branch", "dev_branch"),
        ("prod_branch", "", "prod_branch"),
        ("prod_workflow", "deploy", "prod_workflow"),
        ("catalog_url", "http://frickeldave.de/x.json", "catalog_url"),
        ("catalog_url", "https://evil.example/x.json", "catalog_url"),
        ("image_base_url", "https://127.0.0.1/x", "image_base_url"),
    ],
)
async def test_options_flow_rejects_invalid_values(
    hass: HomeAssistant, field: str, value: str, error_key: str
) -> None:
    """Ungültige Felder werden feldbezogen mit deutschem Schlüssel gemeldet."""
    entry = await _setup_entry(hass, **VALID_OPTIONS)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={**VALID_OPTIONS, field: value}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {error_key: result["errors"][error_key]}
    assert entry.options["repository"] == VALID_OPTIONS["repository"]


def test_validate_options_accepts_the_defaults() -> None:
    """Die Vorgaben der Erweiterung sind gültig und bleiben erhalten."""
    defaults = SyncSettings.from_mapping(DEFAULT_OPTIONS)
    values = validate_options(_options(), defaults)

    assert values["repository"] == "Frickeldave/frickeldave.github.io"
    assert values["dev_branch"] == "dev"
    assert values["prod_branch"] == "main"
    assert values["prod_workflow"] == "fd-deploy-prd.yml"
    assert values["catalog_url"] == "https://frickeldave.de/data/handmade.json"
    assert values["token"] == ""


def test_settings_public_dict_hides_token() -> None:
    """``public_dict`` nennt den Token nur als Kennzeichen."""
    settings = SyncSettings(token="ghp-secret")

    payload = settings.public_dict()
    assert payload["token_set"] is True
    assert "token" not in payload
    assert "ghp-secret" not in json.dumps(payload)


def test_settings_require_token_raises_sync_error() -> None:
    with pytest.raises(SyncError) as err:
        SyncSettings(token="").require_token()
    assert err.value.code == "publish_token_missing"
    assert "kein GitHub-Token" in err.value.message


def test_options_flow_class_is_wired() -> None:
    """Der Options Flow ist der Klasse des Config Flows zugeordnet."""
    entry = MockConfigEntry(domain=DOMAIN)

    assert isinstance(
        FrickeldaveStoreSyncConfigFlow.async_get_options_flow(entry),
        FrickeldaveStoreSyncOptionsFlow,
    )
