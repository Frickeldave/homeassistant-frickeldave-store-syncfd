"""Konstanten der Erweiterung ``frickeldave_store_syncfd``."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "frickeldave_store_syncfd"
NAME: Final = "Frickeldave Store Sync"
VERSION: Final = "0.1.0"
ISSUE_URL: Final = "https://github.com/Frickeldave/frickeldave.github.io/issues/289"
REPOSITORY_URL: Final = "https://github.com/Frickeldave/homeassistant-frickeldave-store-syncfd"

#: Basis-Integration: liefert Store, Rechte und Fassade (Contract 1).
BASE_DOMAIN: Final = "frickeldave_store"
BASE_REPOSITORY_URL: Final = "https://github.com/Frickeldave/frickeldave.home.net"

#: Höhe (Version) des Erweiterungspunkts, den diese Erweiterung erwartet bzw. anbietet.
MIN_BASE_CONTRACT_VERSION: Final = 1
CONTRACT_VERSION: Final = 1

#: Reservierte Feature-Namen des Erweiterungspunkts; Handler folgen in Arbeitspaket 5/6.
FEATURE_PUBLISH: Final = "publish"
FEATURE_IMPORT: Final = "import"

CONFIG_ENTRY_TITLE: Final = NAME
