"""Testkonfiguration: Repository-Wurzel importierbar machen, Fake-Basis setzen."""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

import fake_base
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

pytest_plugins = ["pytest_homeassistant_custom_component"]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Lädt ``custom_components`` aus diesem Repository in die Testumgebung."""
    yield


@pytest.fixture(autouse=True)
def fake_extension_point(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Der Erweiterungspunkt der Basis ist in jedem Test als Fake vorhanden."""
    return fake_base.install(monkeypatch)
