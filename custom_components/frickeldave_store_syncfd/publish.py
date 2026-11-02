"""Veröffentlichung des Website-Katalogs nach ``dev`` und ``prod``.

Portiert aus ``custom_components.frickeldave_store.publish`` (Basis 0.1.16) in
die Erweiterung. Der Store wird ausschließlich über die Fassade angefasst:
:meth:`~custom_components.frickeldave_store.extension.StoreFacade.async_build_export`
liefert Katalogbytes und Bildartefakte im Speicher, Produkte kommen aus
``async_products``. Es gibt keinen Dateisystemzugriff und keinen Import von
Interna der Basis.

Mechanismus (unverändert gegenüber der Basis):

* ``dev`` ist der Integrationsbranch. Ein Commit auf ``dev`` löst ``fd-ci`` aus;
  nach einem grünen Lauf deployt ``fd-deploy-dev``.
* ``main`` wird ausschließlich von ``fd-deploy-prd`` aktualisiert. Auch die
  Produktion committet daher zuerst nach ``dev`` und dispatcht danach den
  Workflow.

Alle schreibenden Aufrufe liegen hinter ``if dry_run`` bzw. hinter dem
Änderungsvergleich. Ein Dry-Run liest ausschließlich (Branch, Baum, ggf. den
entfernten Katalog) und berührt weder Store noch Repository.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Protocol
from urllib.parse import quote

import aiohttp

from .const import CATALOG_PATH, CATEGORIES, DEFAULT_DEV_WORKFLOW, IMAGES_PATH
from .errors import SyncError
from .settings import SyncSettings

_LOGGER = logging.getLogger(__name__)

PUBLISH_TARGETS: Final[tuple[str, ...]] = ("dev", "prod")
GITHUB_API_URL: Final = "https://api.github.com"
GITHUB_TIMEOUT: Final = 30
MAX_MESSAGE_LENGTH: Final = 200
#: Obergrenze für die Dateiliste in der Antwort (Zählungen bleiben vollständig).
MAX_CHANGE_ENTRIES: Final = 500
#: Status ohne Body: hier ist ``None`` das korrekte Ergebnis, nicht ein Fehler.
NO_BODY_STATUSES: Final[tuple[int, ...]] = (204, 205, 304)

DEFAULT_COMMIT_MESSAGE: Final = "chore(catalog): Katalog aus Home Assistant veröffentlicht (#289)"

#: Löschschutz: mehr entfernte Bilder als diese Zahl gelten als Unfall.
REMOVAL_GUARD_MAX_FILES: Final = 10
#: Löschschutz: mehr als ein Drittel des bestehenden Bildbestands gilt als Unfall.
REMOVAL_GUARD_SHARE: Final = 3
#: Kleinere, offensichtlich gezielte Löschungen werden nie blockiert.
REMOVAL_GUARD_MIN_FILES: Final = 3

STATUS_ADDED: Final = "added"
STATUS_CHANGED: Final = "changed"
STATUS_REMOVED: Final = "removed"
STATUS_UNCHANGED: Final = "unchanged"
FILE_STATUSES: Final[tuple[str, ...]] = (
    STATUS_ADDED,
    STATUS_CHANGED,
    STATUS_REMOVED,
    STATUS_UNCHANGED,
)

KIND_CATALOG: Final = "catalog"
KIND_IMAGE: Final = "image"

#: Endung einer Bilddatei, wie sie in Store und Website vorkommt.
IMAGE_SUFFIX_PATTERN: Final = r"(?:jpg|jpeg|png|webp|gif)"
#: Nummer im Bildnamen eines Produkts (``<artikelnummer>-NN``).
_IMAGE_NUMBER_RE: Final = re.compile(r"-(\d{2})(?:\.[A-Za-z]+)$")

#: Verwaltete Produktbilder: ``<kategorie>/<artikelnummer>/<artikelnummer>-NN.<endung>``.
#: Die Artikelnummer muss mit ihrer Kategorie beginnen, jede Tiefe darunter ist
#: fremd (z. B. ``3dmodels/…``) und wird nie als verwaltet betrachtet.
_CATEGORY_PATTERN: Final = "|".join(re.escape(category) for category in CATEGORIES)
_MANAGED_IMAGE_RE: Final = re.compile(
    rf"^{re.escape(IMAGES_PATH)}"
    rf"/(?P<category>{_CATEGORY_PATTERN})"
    rf"/(?P<article>(?P=category)[A-Z0-9]{{1,5}})"
    rf"/(?P=article)-\d{{2}}\.{IMAGE_SUFFIX_PATTERN}$"
)

EMPTY_CATALOG_MESSAGE: Final = (
    "Der lokale Store enthält keine Produkte. Eine Veröffentlichung würde den "
    "kompletten Website-Katalog löschen. Bitte zuerst Produkte importieren oder "
    "anlegen. Ein bewusstes Leeren ist nur mit allow_empty möglich."
)


def _error(code: str, message: str) -> SyncError:
    return SyncError(message, code=code)


def normalise_message(value: Any) -> str:
    """Commit-Nachricht prüfen und trimmen (max. :data:`MAX_MESSAGE_LENGTH`)."""

    if value is None:
        return ""
    if not isinstance(value, str):
        raise _error("validation_error", "message muss eine Zeichenkette sein.")
    text = value.strip()
    if len(text) > MAX_MESSAGE_LENGTH:
        raise _error(
            "validation_error",
            f"message ist zu lang (max. {MAX_MESSAGE_LENGTH} Zeichen).",
        )
    return text


def publish_removal_guard(removed: int, existing: int) -> bool:
    """Löschschutz: erkennt einen Lauf, der den Bildbestand massenhaft entfernt.

    ``removed`` sind die entfernten Bilder, ``existing`` der bestehende
    Bildbestand des Zielbranches. Blockiert wird, wenn mehr als
    :data:`REMOVAL_GUARD_MAX_FILES` Bilder verschwinden oder mehr als ein Drittel
    des Bestands. Gezielte Einzellöschungen bleiben möglich.
    """

    if removed < REMOVAL_GUARD_MIN_FILES:
        return False
    if removed > REMOVAL_GUARD_MAX_FILES:
        return True
    return existing > 0 and removed * REMOVAL_GUARD_SHARE > existing


def removal_guard_message(removed: int, existing: int) -> str:
    return (
        f"Die Veröffentlichung würde {removed} von {existing} vorhandenen Bildern "
        "löschen. Das sieht nach einem Fehler aus (unvollständiger Store oder falsche "
        "Zuordnung). Bitte zuerst den Dry-Run prüfen. Wenn die Löschung gewollt ist, "
        "den Vorgang ausdrücklich mit confirm_removals bestätigen."
    )


def empty_catalog_warning(*, allow_empty: bool, dry_run: bool) -> str:
    if dry_run:
        return (
            "Der lokale Store enthält keine Produkte. Ein Veröffentlichen würde den "
            "Website-Katalog vollständig leeren."
        )
    if allow_empty:
        return (
            "Der lokale Store enthält keine Produkte. Der Website-Katalog wurde bewusst "
            "geleert (allow_empty)."
        )
    return EMPTY_CATALOG_MESSAGE


def _summary_with_warning(summary: str, warning: str | None) -> str:
    return f"{summary} Hinweis: {warning}" if warning else summary


def _decoded_or_empty(raw: bytes) -> dict[str, Any]:
    """Fehlerantwort-Body für ``_raise_for_status``; kaputtes JSON zählt als leer."""

    if not raw:
        return {}
    try:
        body = json.loads(raw)
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _as_mapping(value: Any, message: str) -> dict[str, Any]:
    """Antwort als Objekt erwarten: nie ``AttributeError`` auf ``None``/Listen."""

    if not isinstance(value, dict):
        raise _error("github_failed", message)
    return value


@dataclass(slots=True)
class RemoteTree:
    """Kopf-Commit und Blob-SHAs des Ziel-Branches."""

    head_sha: str
    tree_sha: str
    blobs: dict[str, str] = field(default_factory=dict)
    truncated: bool = False


@dataclass(slots=True)
class FileChange:
    """Eine Datei im Veröffentlichungs-Diff."""

    path: str
    status: str
    kind: str
    article_number: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "status": self.status,
            "kind": self.kind,
            "articleNumber": self.article_number,
        }


def blob_sha(data: bytes) -> str:
    """Git-Blob-SHA (sha1 über ``blob <größe>\\0`` plus Inhalt)."""

    header = f"blob {len(data)}\0".encode()
    return hashlib.sha1(header + data).hexdigest()  # noqa: S324 - Git verlangt SHA-1


# --------------------------------------------------------------------------- Bilder


def _image_name(image: Any) -> str:
    return str(getattr(image, "name", "") or "")


def ordered_images(product: Any) -> list[Any]:
    """Bilder in Veröffentlichungsreihenfolge: Hauptbild zuerst, Rest nach Namen."""

    images = list(getattr(product, "images", ()) or ())
    main = [image for image in images if getattr(image, "is_main", False)]
    rest = sorted(
        (image for image in images if not getattr(image, "is_main", False)),
        key=_image_name,
    )
    ordered = main[:1] + rest
    if not ordered:
        ordered = sorted(images, key=_image_name)
    return ordered


def _own_image_number(product: Any, image: Any) -> int | None:
    prefix = f"{getattr(product, 'article_number', '')}-"
    name = _image_name(image)
    if not name.startswith(prefix):
        return None
    match = _IMAGE_NUMBER_RE.search(name)
    if match is None:
        return None
    number = int(match.group(1))
    return number if 1 <= number <= 99 else None


def _free_image_number(used: set[int]) -> int:
    for candidate in range(1, 100):
        if candidate not in used:
            return candidate
    raise _error("publish_failed", "Kein freier Bildnummernbereich verfügbar.")


def published_image_names(product: Any) -> list[tuple[Any, str]]:
    """Dateiname je veröffentlichtem Bild – stabil über Veröffentlichungen hinweg.

    Das Hauptbild erhält immer ``-01``: die Website zeigt das lexikografisch erste
    Bild eines Produkts als Hauptbild. Alle übrigen Bilder behalten ihre eigene
    Nummer aus dem Store, solange sie frei ist. Dadurch bleiben die Dateinamen
    gleich, solange dasselbe Bild im Produkt bleibt; nur ein neues Hauptbild oder
    eine echte Nummernkollision verschiebt einzelne Namen.
    """

    article_number = str(getattr(product, "article_number", ""))
    used: set[int] = set()
    names: list[tuple[Any, str]] = []
    for position, image in enumerate(ordered_images(product)):
        suffix = Path(_image_name(image)).suffix.casefold() or ".jpg"
        number = 1 if position == 0 else _own_image_number(product, image)
        if number is None or number in used:
            number = _free_image_number(used)
        used.add(number)
        names.append((image, f"{article_number}-{number:02d}{suffix}"))
    return names


def _posix_join(*parts: str) -> str:
    return "/".join(part.strip("/") for part in parts if part and part.strip("/"))


def build_artifacts(bundle: Any, products: Sequence[Any]) -> dict[str, bytes]:
    """Veröffentlichungssatz aus dem Exportbündel der Fassade bauen (read-only).

    Die Fassade liefert Bilder unter ihrem Store-Namen. Veröffentlicht werden sie
    unter dem stabilen Namen aus :func:`published_image_names`. Fehlt ein Bild im
    Bündel (unlesbar oder verschwunden), bricht die Veröffentlichung mit einem
    fachlichen Fehler ab: ein unvollständiger Bildsatz würde sonst still Bilder
    aus dem Website-Katalog entfernen.
    """

    catalog_path = str(getattr(bundle, "catalog_path", CATALOG_PATH))
    images_path = str(getattr(bundle, "images_path", IMAGES_PATH))
    available = {
        str(getattr(artifact, "path", "")): bytes(getattr(artifact, "data", b""))
        for artifact in getattr(bundle, "artifacts", ())
        if getattr(artifact, "kind", "") == KIND_IMAGE
    }
    artifacts: dict[str, bytes] = {catalog_path: bytes(getattr(bundle, "catalog_bytes", b""))}
    problems: list[str] = []
    for product in sorted(products, key=lambda item: str(getattr(item, "article_number", ""))):
        if getattr(product, "archived", False):
            continue
        article_number = str(getattr(product, "article_number", ""))
        category = str(getattr(product, "category", ""))
        for image, target_name in published_image_names(product):
            source = _posix_join(images_path, article_number, _image_name(image))
            data = available.get(source)
            if data is None:
                problems.append(f"{article_number}/{_image_name(image)} (fehlt)")
                continue
            artifacts[_posix_join(images_path, category, article_number, target_name)] = data
    if problems:
        preview = ", ".join(problems[:3])
        more = "" if len(problems) <= 3 else f" und {len(problems) - 3} weitere"
        if len(problems) == 1:
            detail = f"Eine Bilddatei fehlt oder ist nicht lesbar: {problems[0]}."
        else:
            detail = f"{len(problems)} Bilddateien fehlen oder sind nicht lesbar: {preview}{more}."
        raise _error(
            "publish_artifacts_unavailable",
            f"{detail} Bitte die betroffenen Bilder erneut hochladen oder aus dem "
            "Produkt entfernen und erneut veröffentlichen.",
        )
    return artifacts


def _article_from_path(path: str) -> str:
    if path == CATALOG_PATH:
        return ""
    parts = Path(path).parts
    return parts[-2] if len(parts) >= 2 else ""


def _is_managed_path(path: str) -> bool:
    """Gehört der Pfad zu den Dateien, die diese Integration verantwortet?

    Nur der Katalog und Produktbilder im Muster
    ``src/assets/handmade/<Kategorie>/<Artikelnummer>/<Artikelnummer>-NN.<Endung>``.
    Alles andere unterhalb von ``src/assets/handmade`` gehört der Website selbst –
    etwa die 3D-Modellquellen unter ``3dmodels/`` oder ``.gitkeep`` – und darf
    niemals als entfernt gelten, weil eine Veröffentlichung sie sonst löscht.
    """

    return path == CATALOG_PATH or _MANAGED_IMAGE_RE.match(path) is not None


def _managed_image_paths(paths: Iterable[str]) -> list[str]:
    return [path for path in paths if path != CATALOG_PATH and _is_managed_path(path)]


def diff_artifacts(local: Mapping[str, bytes], remote: Mapping[str, str]) -> list[FileChange]:
    """Dateien vergleichen; nur verwaltete Pfade gelten als ``removed``."""

    changes: list[FileChange] = []
    for path in sorted(local):
        kind = KIND_CATALOG if path == CATALOG_PATH else KIND_IMAGE
        article = _article_from_path(path)
        remote_sha = remote.get(path)
        if remote_sha is None:
            status = STATUS_ADDED
        elif remote_sha == blob_sha(local[path]):
            status = STATUS_UNCHANGED
        else:
            status = STATUS_CHANGED
        changes.append(FileChange(path, status, kind, article))
    for path in sorted(remote):
        if path in local or not _is_managed_path(path):
            continue
        kind = KIND_CATALOG if path == CATALOG_PATH else KIND_IMAGE
        changes.append(FileChange(path, STATUS_REMOVED, kind, _article_from_path(path)))
    return changes


def _status_counts(changes: Iterable[FileChange]) -> dict[str, int]:
    counts = {status: 0 for status in FILE_STATUSES}
    for change in changes:
        counts[change.status] = counts.get(change.status, 0) + 1
    counts["total"] = sum(value for key, value in counts.items() if key != "total")
    return counts


def _catalog_products(raw: bytes | None) -> dict[str, dict[str, Any]]:
    if not raw:
        return {}
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, TypeError):
        return {}
    entries = document.get("products") if isinstance(document, dict) else None
    if not isinstance(entries, list):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if isinstance(entry, dict) and entry.get("articleNumber"):
            result[str(entry["articleNumber"])] = entry
    return result


def diff_products(local_catalog: bytes, remote_catalog: bytes | None) -> dict[str, int]:
    """Produkt-Diff über Artikelnummern (Grundlage für „was würde sich ändern“)."""

    local = _catalog_products(local_catalog)
    remote = _catalog_products(remote_catalog)
    counts = {"added": 0, "changed": 0, "removed": 0, "unchanged": 0, "total": len(local)}
    for article, entry in local.items():
        if article not in remote:
            counts["added"] += 1
        elif remote[article] != entry:
            counts["changed"] += 1
        else:
            counts["unchanged"] += 1
    counts["removed"] = sum(1 for article in remote if article not in local)
    return counts


def diff_images(changes: Iterable[FileChange], local_total: int) -> dict[str, int]:
    counts = {"added": 0, "changed": 0, "removed": 0, "unchanged": 0, "total": local_total}
    for change in changes:
        if change.kind == KIND_IMAGE:
            counts[change.status] += 1
    return counts


# --------------------------------------------------------------------------- GitHub


class GitHubClientProtocol(Protocol):
    """Von :class:`PublishService` genutzte GitHub-Operationen (testbar)."""

    async def async_read_tree(self) -> RemoteTree: ...

    async def async_read_file(self, path: str) -> bytes | None: ...

    async def async_commit(
        self,
        artifacts: Mapping[str, bytes],
        changes: list[FileChange],
        *,
        head_sha: str,
        tree_sha: str,
        message: str,
    ) -> str: ...

    async def async_dispatch_workflow(
        self, workflow: str, *, ref: str, inputs: dict[str, Any]
    ) -> None: ...

    async def async_close(self) -> None: ...


class GitHubClient:
    """Minimaler GitHub-REST-Client (Git Data API + Actions-Dispatch)."""

    def __init__(
        self,
        settings: SyncSettings,
        *,
        session: aiohttp.ClientSession | None = None,
        timeout: int = GITHUB_TIMEOUT,
        api_url: str = GITHUB_API_URL,
    ) -> None:
        self._settings = settings
        self._session = session
        self._owns_session = session is None
        self._timeout = timeout
        self._api_url = api_url.rstrip("/")

    # ------------------------------------------------------------------ HTTP
    def _url(self, path: str) -> str:
        return f"{self._api_url}/repos/{self._settings.repository}/{path.lstrip('/')}"

    def _client_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession()
            self._owns_session = True
        return self._session

    def _headers(self) -> dict[str, str]:
        # Der Token wird ausschließlich im Header verwendet: nie loggen, nie zurückgeben.
        return {
            "Authorization": f"Bearer {self._settings.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def _async_json(
        self, method: str, url: str, *, payload: dict[str, Any] | None = None
    ) -> Any:
        session = self._client_session()
        try:
            async with session.request(
                method,
                url,
                json=payload,
                headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=self._timeout),
            ) as response:
                # Body immer lesen: ``response.content_length`` ist bei chunked
                # Antworten (``Transfer-Encoding: chunked``) ``None`` und taugt
                # nicht als Leer-Kriterium. GitHub antwortet regelmäßig so.
                raw = await response.read()
                if response.status >= 400:
                    self._raise_for_status(response.status, _decoded_or_empty(raw))
                if response.status in NO_BODY_STATUSES or not raw:
                    return None
                try:
                    return json.loads(raw)
                except ValueError as err:
                    raise _error(
                        "github_unavailable",
                        "GitHub hat keine gültige Antwort geliefert (vermutlich eine "
                        "Störungsseite). Bitte später erneut versuchen.",
                    ) from err
        except (TimeoutError, aiohttp.ClientError) as err:
            raise _error(
                "github_unavailable",
                "GitHub ist momentan nicht erreichbar. Bitte später erneut versuchen.",
            ) from err

    async def _async_bytes(self, url: str) -> bytes | None:
        session = self._client_session()
        try:
            headers = {**self._headers(), "Accept": "application/vnd.github.raw+json"}
            async with session.get(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=self._timeout)
            ) as response:
                if response.status == 404:
                    return None
                if response.status >= 400:
                    self._raise_for_status(response.status, {})
                data: bytes = await response.read()
                return data
        except (TimeoutError, aiohttp.ClientError) as err:
            raise _error(
                "github_unavailable",
                "GitHub ist momentan nicht erreichbar. Bitte später erneut versuchen.",
            ) from err

    def _raise_for_status(self, status: int, body: Any) -> None:
        if status < 400:
            return
        message = ""
        if isinstance(body, dict):
            message = str(body.get("message", "") or "")
        _LOGGER.debug("GitHub-Antwort %s: %s", status, message)
        if status in (401, 403):
            raise _error(
                "github_unauthorized",
                "Der GitHub-Token wurde abgelehnt oder hat keine Schreibrechte für "
                f"„{self._settings.repository}“.",
            )
        if status == 404:
            raise _error(
                "github_not_found",
                f"Repository „{self._settings.repository}“ oder Branch nicht gefunden. "
                "Bitte Repository und Token prüfen.",
            )
        if status in (409, 422):
            raise _error(
                "github_conflict",
                "Der Branch wurde während der Veröffentlichung geändert. "
                "Bitte den Vorgang erneut starten.",
            )
        if status >= 500:
            raise _error(
                "github_unavailable",
                "GitHub ist momentan nicht erreichbar (Störung auf der GitHub-Seite). "
                "Bitte später erneut versuchen.",
            )
        raise _error("github_failed", f"GitHub hat die Anfrage abgelehnt (HTTP {status}).")

    # ------------------------------------------------------------------ Lesen
    async def async_read_tree(self) -> RemoteTree:
        branch = quote(self._settings.dev_branch, safe="")
        ref = _as_mapping(
            await self._async_json("GET", self._url(f"git/ref/heads/{branch}")),
            "GitHub hat den Branch-Status nicht im erwarteten Format geliefert. "
            "Bitte später erneut versuchen.",
        )
        head_sha = str((ref.get("object") or {}).get("sha", ""))
        if not head_sha:
            raise _error("github_failed", "Der Branch hat keinen lesbaren Kopf-Commit.")
        commit = _as_mapping(
            await self._async_json("GET", self._url(f"git/commits/{head_sha}")),
            "GitHub hat den Commit nicht im erwarteten Format geliefert. "
            "Bitte später erneut versuchen.",
        )
        tree_sha = str((commit.get("tree") or {}).get("sha", ""))
        if not tree_sha:
            raise _error("github_failed", "Der Branch hat keinen lesbaren Baum.")
        tree = _as_mapping(
            await self._async_json("GET", self._url(f"git/trees/{tree_sha}?recursive=1")),
            "GitHub hat den Dateibaum nicht im erwarteten Format geliefert. "
            "Bitte später erneut versuchen.",
        )
        blobs: dict[str, str] = {}
        for entry in tree.get("tree") or []:
            if isinstance(entry, dict) and entry.get("type") == "blob":
                blobs[str(entry.get("path", ""))] = str(entry.get("sha", ""))
        return RemoteTree(
            head_sha=head_sha,
            tree_sha=tree_sha,
            blobs=blobs,
            truncated=bool(tree.get("truncated", False)),
        )

    async def async_read_file(self, path: str) -> bytes | None:
        branch = quote(self._settings.dev_branch, safe="")
        return await self._async_bytes(self._url(f"contents/{path}?ref={branch}"))

    # ------------------------------------------------------------------ Schreiben
    async def async_commit(
        self,
        artifacts: Mapping[str, bytes],
        changes: list[FileChange],
        *,
        head_sha: str,
        tree_sha: str,
        message: str,
    ) -> str:
        tree_entries: list[dict[str, Any]] = []
        for change in changes:
            if change.status == STATUS_REMOVED:
                tree_entries.append(
                    {"path": change.path, "mode": "100644", "type": "blob", "sha": None}
                )
                continue
            if change.status == STATUS_UNCHANGED:
                continue
            data = artifacts.get(change.path)
            if data is None:
                raise _error(
                    "publish_artifacts_unavailable",
                    f"Die Datei „{change.path}“ fehlt im Veröffentlichungssatz. "
                    "Bitte den Vorgang erneut starten.",
                )
            blob = _as_mapping(
                await self._async_json(
                    "POST",
                    self._url("git/blobs"),
                    payload={
                        "content": base64.b64encode(data).decode("ascii"),
                        "encoding": "base64",
                    },
                ),
                "GitHub hat den Blob nicht im erwarteten Format geliefert. "
                "Bitte den Vorgang erneut starten.",
            )
            tree_entries.append(
                {
                    "path": change.path,
                    "mode": "100644",
                    "type": "blob",
                    "sha": str(blob.get("sha", "")),
                }
            )
        tree = _as_mapping(
            await self._async_json(
                "POST",
                self._url("git/trees"),
                # base_tree hält alle nicht genannten Dateien unverändert.
                payload={"base_tree": tree_sha, "tree": tree_entries},
            ),
            "GitHub hat den Baum nicht im erwarteten Format geliefert. "
            "Bitte den Vorgang erneut starten.",
        )
        new_tree_sha = str(tree.get("sha", ""))
        commit = _as_mapping(
            await self._async_json(
                "POST",
                self._url("git/commits"),
                payload={"message": message, "tree": new_tree_sha, "parents": [head_sha]},
            ),
            "GitHub hat den Commit nicht im erwarteten Format geliefert. "
            "Bitte den Vorgang erneut starten.",
        )
        commit_sha = str(commit.get("sha", ""))
        branch = quote(self._settings.dev_branch, safe="")
        await self._async_json(
            "PATCH",
            self._url(f"git/refs/heads/{branch}"),
            payload={"sha": commit_sha, "force": False},
        )
        return commit_sha

    async def async_dispatch_workflow(
        self, workflow: str, *, ref: str, inputs: dict[str, Any]
    ) -> None:
        await self._async_json(
            "POST",
            self._url(f"actions/workflows/{quote(workflow, safe='')}/run"),
            payload={"ref": ref, "inputs": inputs},
        )

    async def async_close(self) -> None:
        if self._session is None or not self._owns_session:
            return
        session = self._session
        self._session = None
        await session.close()


# --------------------------------------------------------------------------- Service


class PublishService:
    """Baut den Veröffentlichungsplan und führt ihn (außer im Dry-Run) aus."""

    def __init__(
        self,
        facade: Any,
        settings: SyncSettings,
        *,
        client_factory: Callable[[SyncSettings], GitHubClientProtocol] | None = None,
    ) -> None:
        self._facade = facade
        self._settings = settings
        self._client_factory = client_factory

    def _client(self, settings: SyncSettings) -> GitHubClientProtocol:
        if self._client_factory is not None:
            return self._client_factory(settings)
        return GitHubClient(settings)

    async def async_publish(
        self,
        target: str,
        *,
        dry_run: bool = False,
        message: str = "",
        user_id: str = "",
        allow_empty: bool = False,
        confirm_removals: bool = False,
    ) -> dict[str, Any]:
        if target not in PUBLISH_TARGETS:
            raise _error("validation_error", "target muss 'dev' oder 'prod' sein.")
        settings = self._settings
        if not settings.is_configured:
            raise _error(
                "publish_not_configured",
                "Die Veröffentlichung ist nicht konfiguriert. Bitte Repository und "
                "Workflow in den Einstellungen ergänzen.",
            )
        settings.require_token()
        commit_message = normalise_message(message) or DEFAULT_COMMIT_MESSAGE

        # Lesen über die Fassade: Produktstand und Exportbündel entstehen im
        # Speicher der Basis. Hier wird nichts geschrieben.
        bundle = await self._facade.async_build_export(
            catalog_path=CATALOG_PATH, images_path=IMAGES_PATH
        )
        products = await self._facade.async_products()
        # Archivierte Produkte landen nicht im Katalog: auch ein Store, der nur noch
        # archivierte Produkte enthält, würde den Website-Katalog leeren.
        empty_catalog = bool(getattr(bundle, "empty_catalog", False))
        if empty_catalog and not dry_run and not allow_empty:
            _LOGGER.warning(
                "Veröffentlichen nach %s abgelehnt: lokaler Store ohne Produkte (Benutzer %s)",
                target,
                user_id or "-",
            )
            raise _error("publish_empty_catalog", EMPTY_CATALOG_MESSAGE)
        warnings: list[str] = []
        if empty_catalog:
            warnings.append(empty_catalog_warning(allow_empty=allow_empty, dry_run=dry_run))
        if empty_catalog and not dry_run:
            _LOGGER.warning(
                "Veröffentlichen nach %s mit leerem Katalog (allow_empty, Benutzer %s)",
                target,
                user_id or "-",
            )
        artifacts = build_artifacts(bundle, products)

        client = self._client(settings)
        try:
            tree = await client.async_read_tree()
            if tree.truncated:  # pragma: no cover - nur bei sehr großen Repositories
                raise _error(
                    "github_failed",
                    "Der Dateibaum des Repositorys ist zu groß für einen Vergleich.",
                )
            changes = diff_artifacts(artifacts, tree.blobs)
            counts = _status_counts(changes)

            # Löschschutz: Absolut- und Anteilsgrenze gelten gemeinsam. Im Dry-Run
            # bleibt es bei einer Warnung, damit der Plan vollständig sichtbar ist.
            removed_images = sum(
                1
                for change in changes
                if change.kind == KIND_IMAGE and change.status == STATUS_REMOVED
            )
            existing_images = len(_managed_image_paths(tree.blobs))
            guard = publish_removal_guard(removed_images, existing_images)
            if guard:
                guard_message = removal_guard_message(removed_images, existing_images)
                if not dry_run and not confirm_removals:
                    _LOGGER.warning(
                        "Veröffentlichen nach %s abgelehnt: Löschschutz (%s Bilder, Benutzer %s)",
                        target,
                        removed_images,
                        user_id or "-",
                    )
                    raise _error("publish_removals_blocked", guard_message)
                warnings.append(guard_message)

            local_catalog = artifacts[CATALOG_PATH]
            remote_catalog: bytes | None = None
            catalog_change = next((item for item in changes if item.kind == KIND_CATALOG), None)
            if catalog_change is not None and catalog_change.status != STATUS_UNCHANGED:
                remote_catalog = await client.async_read_file(CATALOG_PATH)
            product_counts = diff_products(local_catalog, remote_catalog)
            image_counts = diff_images(changes, len(artifacts) - 1)
            warning = " ".join(warnings) or None
            removal_guard = {
                "triggered": guard,
                "removed": removed_images,
                "existing": existing_images,
                "maxFiles": REMOVAL_GUARD_MAX_FILES,
                "share": REMOVAL_GUARD_SHARE,
                "confirmed": bool(guard and not dry_run and confirm_removals),
            }

            result = self._result(
                target=target,
                settings=settings,
                dry_run=dry_run,
                changes=changes,
                counts=counts,
                product_counts=product_counts,
                image_counts=image_counts,
                warning=warning,
                removal_guard=removal_guard,
            )

            if dry_run:
                # Strukturell garantiert: bis hierher wurde ausschließlich gelesen.
                return result

            commit_sha: str | None = None
            if counts[STATUS_ADDED] or counts[STATUS_CHANGED] or counts[STATUS_REMOVED]:
                commit_sha = await client.async_commit(
                    artifacts,
                    changes,
                    head_sha=tree.head_sha,
                    tree_sha=tree.tree_sha,
                    message=commit_message,
                )
                _LOGGER.info(
                    "Katalog nach %s veröffentlicht (%s Dateien, Commit %s, Benutzer %s)",
                    settings.dev_branch,
                    counts["total"],
                    commit_sha[:7],
                    user_id or "-",
                )

            if target == "prod":
                await client.async_dispatch_workflow(
                    settings.prod_workflow,
                    ref=settings.prod_branch,
                    inputs={"dry_run": False},
                )
                result["deployment"].update(
                    {
                        "triggered": True,
                        "workflow_run": True,
                        "mode": "workflow_dispatch",
                    }
                )
            else:
                result["deployment"]["triggered"] = commit_sha is not None
            result["commit"] = commit_sha
            result["changed"] = commit_sha is not None
            result["summary"] = _summary_with_warning(
                self._summary(
                    target,
                    settings,
                    counts,
                    product_counts,
                    image_counts,
                    dry_run=False,
                    committed=commit_sha is not None,
                ),
                warning,
            )
            return result
        finally:
            await client.async_close()

    def _result(
        self,
        *,
        target: str,
        settings: SyncSettings,
        dry_run: bool,
        changes: list[FileChange],
        counts: dict[str, int],
        product_counts: dict[str, int],
        image_counts: dict[str, int],
        warning: str | None = None,
        removal_guard: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        files = changes[:MAX_CHANGE_ENTRIES]
        deployment = {
            "triggered": False,
            "would_trigger": bool(
                target == "prod"
                or counts[STATUS_ADDED]
                or counts[STATUS_CHANGED]
                or counts[STATUS_REMOVED]
            ),
            "mode": "workflow_dispatch" if target == "prod" else "push",
            "workflow": settings.prod_workflow if target == "prod" else DEFAULT_DEV_WORKFLOW,
            "branch": settings.prod_branch if target == "prod" else settings.dev_branch,
        }
        return {
            "target": target,
            "dry_run": dry_run,
            "repository": settings.repository,
            "branch": settings.dev_branch,
            "commit": None,
            "changed": False,
            "counts": counts,
            "products": product_counts,
            "images": image_counts,
            "files": [change.to_dict() for change in files],
            "files_truncated": len(changes) > MAX_CHANGE_ENTRIES,
            "deployment": deployment,
            "removal_guard": removal_guard,
            "warning": warning,
            "summary": _summary_with_warning(
                self._summary(
                    target,
                    settings,
                    counts,
                    product_counts,
                    image_counts,
                    dry_run=dry_run,
                    committed=False,
                ),
                warning,
            ),
        }

    @staticmethod
    def _summary(
        target: str,
        settings: SyncSettings,
        counts: dict[str, int],
        product_counts: dict[str, int],
        image_counts: dict[str, int],
        *,
        dry_run: bool,
        committed: bool,
    ) -> str:
        def _describe(values: dict[str, int], noun: str) -> str:
            return (
                f"{values['added']} {noun} neu, {values['changed']} geändert, "
                f"{values['removed']} entfernt, {values['unchanged']} unverändert"
            )

        files = (
            f"{counts['added']} Dateien neu, {counts['changed']} geändert, "
            f"{counts['removed']} entfernt, {counts['unchanged']} unverändert"
        )
        parts = [
            f"{_describe(product_counts, 'Produkte')} ({product_counts['total']} gesamt)",
            f"{_describe(image_counts, 'Bilder')} ({image_counts['total']} gesamt)",
            files,
        ]
        if dry_run:
            if target == "prod":
                parts.append(
                    "Dry-Run: Es wird nichts geschrieben. Beim Veröffentlichen würde "
                    f"zuerst ein Commit auf {settings.dev_branch} gepusht und danach "
                    f"{settings.prod_workflow} ausgelöst (main wird nur über diesen "
                    "Workflow aktualisiert)."
                )
            else:
                parts.append(
                    "Dry-Run: Es wird nichts geschrieben. Beim Veröffentlichen würde ein "
                    f"Commit auf {settings.dev_branch} gepusht und damit fd-ci sowie "
                    "fd-deploy-dev ausgelöst."
                )
            return "; ".join(parts) + "."
        if target == "prod":
            prefix = "Katalog auf dev committet" if committed else "Keine Dateiänderungen auf dev"
            return "; ".join(parts) + f". {prefix}; {settings.prod_workflow} wurde ausgelöst."
        if committed:
            return (
                "; ".join(parts)
                + f". Commit auf {settings.dev_branch} gepusht; fd-ci und fd-deploy-dev "
                "wurden damit ausgelöst."
            )
        return "; ".join(parts) + ". Keine Änderungen: nichts committet, kein Deployment."


__all__ = [
    "DEFAULT_COMMIT_MESSAGE",
    "EMPTY_CATALOG_MESSAGE",
    "FILE_STATUSES",
    "GitHubClient",
    "GitHubClientProtocol",
    "PublishService",
    "REMOVAL_GUARD_MAX_FILES",
    "REMOVAL_GUARD_MIN_FILES",
    "REMOVAL_GUARD_SHARE",
    "RemoteTree",
    "blob_sha",
    "build_artifacts",
    "diff_artifacts",
    "diff_images",
    "diff_products",
    "empty_catalog_warning",
    "normalise_message",
    "ordered_images",
    "publish_removal_guard",
    "published_image_names",
    "removal_guard_message",
]
