"""Fachliche Fehler der Erweiterung: Code, deutscher Text und HTTP-Status.

Codes und Status sind identisch zu den bisherigen Endpunkten der Basis
(``POST /api/frickeldave_store/publish`` und ``.../import/catalog``), damit das
Panel unverändert weiterarbeiten kann, sobald es auf
``POST /api/frickeldave_store/features/{feature}`` umgestellt ist.

Bereitgestellt wird immer ein :class:`SyncError`; die Feature-Handler wandeln
ihn in den ``ExtensionError`` des Erweiterungspunkts um, weil nur dort Code und
Status in die API-Antwort wandern.
"""

from __future__ import annotations

from typing import Final

#: Status je Fehlercode – deckungsgleich mit ``STATUS_BY_CODE`` der Basis.
STATUS_BY_CODE: Final[dict[str, int]] = {
    "validation_error": 400,
    "forbidden": 403,
    "not_found": 404,
    "catalog_unavailable": 502,
    "catalog_invalid": 422,
    "publish_not_configured": 400,
    "publish_token_missing": 400,
    "publish_failed": 502,
    "publish_empty_catalog": 409,
    "publish_artifacts_unavailable": 409,
    "publish_removals_blocked": 409,
    "github_unauthorized": 502,
    "github_not_found": 404,
    "github_conflict": 409,
    "github_failed": 502,
    "github_unavailable": 502,
    "internal_error": 500,
}

#: Unbekannte Codes gelten als Client-Fehler.
DEFAULT_STATUS: Final = 400


class SyncError(Exception):
    """Fachlicher Fehler mit maschinenlesbarem Code und HTTP-Status."""

    def __init__(
        self, message: str, *, code: str = "publish_failed", status: int | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = STATUS_BY_CODE.get(code, DEFAULT_STATUS) if status is None else int(status)

    @property
    def message(self) -> str:
        """Deutsche Meldung für das Panel."""
        return str(self)
