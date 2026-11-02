"""Einstellungen der Erweiterung inklusive der frickeldave-spezifischen Vorgaben.

Repository, Branches, Workflow, Token und Importquellen liegen im Options Flow
dieser Integration – nicht mehr im Store der Basis. Der GitHub-Token ist
write-only: Er wird gespeichert, aber nie zurückgegeben, geloggt oder in einer
API-Antwort verwendet (:meth:`SyncSettings.public_dict` liefert nur
``token_set``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from .const import (
    DEFAULT_CATALOG_URL,
    DEFAULT_IMAGE_BASE_URL,
    DEFAULT_PUBLISH_DEV_BRANCH,
    DEFAULT_PUBLISH_PROD_BRANCH,
    DEFAULT_PUBLISH_PROD_WORKFLOW,
    DEFAULT_PUBLISH_REPOSITORY,
)
from .errors import SyncError

TOKEN_SAVED_PLACEHOLDER: Final = "********"


def _text(values: Mapping[str, Any], key: str, default: str) -> str:
    """Wert als Zeichenkette; leere oder fehlende Werte fallen auf ``default``."""

    raw = values.get(key)
    if isinstance(raw, str):
        cleaned = raw.strip()
        if cleaned:
            return cleaned
    return default


@dataclass(frozen=True, slots=True)
class SyncSettings:
    """Ziel-Repository, Zugangsdaten und Importquellen."""

    repository: str = DEFAULT_PUBLISH_REPOSITORY
    dev_branch: str = DEFAULT_PUBLISH_DEV_BRANCH
    prod_branch: str = DEFAULT_PUBLISH_PROD_BRANCH
    prod_workflow: str = DEFAULT_PUBLISH_PROD_WORKFLOW
    token: str = ""
    catalog_url: str = DEFAULT_CATALOG_URL
    image_base_url: str = DEFAULT_IMAGE_BASE_URL

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> SyncSettings:
        """Einstellungen aus ``data``/``options`` eines Config Entry lesen."""

        token = values.get("token")
        return cls(
            repository=_text(values, "repository", DEFAULT_PUBLISH_REPOSITORY),
            dev_branch=_text(values, "dev_branch", DEFAULT_PUBLISH_DEV_BRANCH),
            prod_branch=_text(values, "prod_branch", DEFAULT_PUBLISH_PROD_BRANCH),
            prod_workflow=_text(values, "prod_workflow", DEFAULT_PUBLISH_PROD_WORKFLOW),
            token=token.strip() if isinstance(token, str) else "",
            catalog_url=_text(values, "catalog_url", DEFAULT_CATALOG_URL),
            image_base_url=_text(values, "image_base_url", DEFAULT_IMAGE_BASE_URL),
        )

    @classmethod
    def from_entry(cls, entry: Any) -> SyncSettings:
        """Einstellungen eines Config Entry (``options`` haben Vorrang)."""

        data = getattr(entry, "data", None) or {}
        options = getattr(entry, "options", None) or {}
        merged: dict[str, Any] = {**dict(data), **dict(options)}
        return cls.from_mapping(merged)

    @property
    def is_configured(self) -> bool:
        """Repository und Produktions-Workflow sind Pflicht."""

        return bool(self.repository.strip() and self.prod_workflow.strip())

    @property
    def token_set(self) -> bool:
        return bool(self.token)

    def require_token(self) -> None:
        """Ohne Token ist keine Veröffentlichung möglich (deutsche Meldung)."""

        if not self.token:
            raise SyncError(
                "Es ist kein GitHub-Token hinterlegt. Bitte unter Einstellungen → "
                "Frickeldave Store Sync ergänzen.",
                code="publish_token_missing",
            )

    def public_dict(self) -> dict[str, Any]:
        """Einstellungen ohne Geheimnis – Grundlage für Antworten und Logs."""

        return {
            "repository": self.repository,
            "dev_branch": self.dev_branch,
            "prod_branch": self.prod_branch,
            "prod_workflow": self.prod_workflow,
            "catalog_url": self.catalog_url,
            "image_base_url": self.image_base_url,
            "token_set": self.token_set,
        }
