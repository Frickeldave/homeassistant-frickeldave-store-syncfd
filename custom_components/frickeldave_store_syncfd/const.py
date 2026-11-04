"""Konstanten der Erweiterung ``frickeldave_store_syncfd``."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "frickeldave_store_syncfd"
NAME: Final = "Frickeldave Store Sync"
VERSION: Final = "0.1.2"
ISSUE_URL: Final = "https://github.com/Frickeldave/frickeldave.github.io/issues/289"
REPOSITORY_URL: Final = "https://github.com/Frickeldave/homeassistant-frickeldave-store-syncfd"

#: Basis-Integration: liefert Store, Rechte und Fassade (Contract 1).
BASE_DOMAIN: Final = "frickeldave_store"
BASE_REPOSITORY_URL: Final = "https://github.com/Frickeldave/frickeldave.home.net"

#: Höhe (Version) des Erweiterungspunkts, den diese Erweiterung erwartet.
MIN_BASE_CONTRACT_VERSION: Final = 1
CONTRACT_VERSION: Final = 1

#: Feature-Namen des Erweiterungspunkts, die diese Erweiterung übernimmt.
FEATURE_PUBLISH: Final = "publish"
FEATURE_IMPORT: Final = "import"
FEATURE_TITLE_PUBLISH: Final = "Veröffentlichen"
FEATURE_TITLE_IMPORT: Final = "Katalogimport"

#: Rollen des Erweiterungspunkts (Default Deny in der Basis).
ROLE_PRODUCT: Final = "product"
ROLE_APP_ADMIN: Final = "app_admin"

#: Zielpfade im Website-Repository.
CATALOG_PATH: Final = "public/data/handmade.json"
IMAGES_PATH: Final = "src/assets/handmade"

#: Veröffentlichung: ``dev`` ist der einzige Schreibpunkt, ``main`` wird
#: ausschließlich vom Workflow ``fd-deploy-prd`` aktualisiert.
DEFAULT_PUBLISH_REPOSITORY: Final = "Frickeldave/frickeldave.github.io"
DEFAULT_PUBLISH_DEV_BRANCH: Final = "dev"
DEFAULT_PUBLISH_PROD_BRANCH: Final = "main"
DEFAULT_PUBLISH_PROD_WORKFLOW: Final = "fd-deploy-prd.yml"
DEFAULT_DEV_WORKFLOW: Final = "fd-deploy-dev.yml"

#: Katalogimport: vertrauenswürdige Quellen und SSRF-Allowlist.
DEFAULT_CATALOG_URL: Final = "https://frickeldave.de/data/handmade.json"
DEFAULT_IMAGE_BASE_URL: Final = (
    "https://raw.githubusercontent.com/Frickeldave/frickeldave.github.io/dev/src/assets/handmade"
)
IMPORT_ALLOWED_HOSTS: Final[tuple[str, ...]] = ("frickeldave.de", "raw.githubusercontent.com")

#: Kategorien des Website-Katalogs (erste Stufe des Bildpfads).
CATEGORIES: Final[tuple[str, ...]] = ("3D", "EP", "LA", "SB", "WO")
#: Obergrenze der Produktbilder je Produkt.
MAX_IMAGES: Final = 10

CONFIG_ENTRY_TITLE: Final = NAME
