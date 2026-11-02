"""Katalogimport der Website inklusive Bilder (Erweiterung).

Portiert aus ``custom_components.frickeldave_store.importer`` (Basis 0.1.16).
Der Import lädt den veröffentlichten Katalog und die Produktbilder, prüft die
Quellen gegen eine feste https-Allowlist und übergibt das Ergebnis als
validierten Batch an die Fassade (:meth:`StoreFacade.async_apply_import_batch`).
Die Erweiterung schreibt selbst nie in den Store und kennt keine Dateipfade der
Basis.

Standardmäßig ist der Import rein additiv und idempotent: Bestehende
Artikelnummern werden nie überschrieben, sondern als ``skipped`` gemeldet. Es
gibt keinen Hard-Delete-Pfad. Mit ``overwrite=True`` werden vorhandene Produkte
zusätzlich mit dem Katalogstand überschrieben (Artikelnummer, ``createdAt``,
Bilder, Buchungen und Archiv-Status bleiben lokal). Mit ``dry_run=True`` wird
der Katalog zwar geladen, geprüft und die Bilder werden abgerufen, aber nichts
geschrieben: kein Batch, kein Snapshot, kein ``updatedAt``.

Fachliche Fehler einzelner Produkte oder Bilder landen in ``errors`` und brechen
den Lauf nicht ab; nur ein nicht lesbares oder strukturell ungültiges
Katalogdokument lehnt die Anfrage vollständig ab, bevor der Store verändert
wird.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final
from urllib.parse import urlparse

import aiohttp

from .const import CATEGORIES, IMPORT_ALLOWED_HOSTS, MAX_IMAGES
from .errors import SyncError
from .settings import SyncSettings

_LOGGER = logging.getLogger(__name__)

#: Zeitlimit je HTTP-Anfrage (Katalog und Bilder).
IMPORT_TIMEOUT: Final = 30.0
#: Obergrenze gleichzeitiger Bildabrufe.
IMPORT_MAX_CONCURRENCY: Final = 5
#: Bildendungen in der Reihenfolge, in der sie probiert werden.
IMAGE_SUFFIXES: Final[tuple[str, ...]] = (".jpg", ".jpeg")
#: Der Altbestand enthält einzelne größere Dateien (bis ~12 MB); Panel-Uploads
#: bleiben bei 10 MB. Der Import erlaubt daher bewusst etwas mehr.
MAX_IMPORT_IMAGE_BYTES: Final = 25 * 1024 * 1024
#: Signaturprüfung statt Content-Type: GitHub liefert große Binärdateien als
#: ``application/octet-stream`` aus.
IMAGE_SIGNATURES: Final[tuple[bytes, ...]] = (
    b"\xff\xd8\xff",
    b"\x89PNG\r\n\x1a\n",
    b"GIF87a",
    b"GIF89a",
)
#: Panel-generierte Artikelnummern sind sechsstellig; der Import akzeptiert
#: zusätzlich kurze Altbestandsnummern (z. B. ``LAX``), damit der bestehende
#: Katalog vollständig übernommen wird.
ARTICLE_NUMBER_RE: Final = re.compile(r"^[A-Z0-9]{1,12}$")
#: Dateinamenschema der Bildablage (``<artikel>-NN.jpg``).
_IMAGE_INDEX_RE: Final = re.compile(r"-(\d{2})\.[A-Za-z]+$")

#: Felder, die beim Überschreiben aus dem Katalog übernommen werden.
OVERWRITE_FIELDS: Final[tuple[str, ...]] = (
    "name",
    "description",
    "price",
    "size",
    "tags",
    "visible",
    "customizable",
    "stock",
)


def _error(code: str, message: str) -> SyncError:
    return SyncError(message, code=code)


def _count(value: int, singular: str, plural: str) -> str:
    return f"{value} {singular if value == 1 else plural}"


# --------------------------------------------------------------------------- Payload


@dataclass(frozen=True, slots=True)
class ImportOptions:
    """Geprüfte Felder der Import-Anfrage.

    Leere Quellen bedeuten „Vorgabe aus den Einstellungen“; die Einstellungen
    selbst fallen auf die frickeldave-spezifischen Standardwerte zurück.
    """

    source_url: str = ""
    image_base_url: str = ""
    import_images: bool = True
    dry_run: bool = False
    overwrite: bool = False


# --------------------------------------------------------------------------- URLs


def validate_import_url(url: Any, *, label: str) -> str:
    """Feste https-Allowlist gegen SSRF; liefert die bereinigte URL."""

    if not isinstance(url, str) or not url.strip():
        raise _error("validation_error", f"{label} muss eine absolute https-URL sein.")
    value = url.strip()
    parsed = urlparse(value)
    if parsed.scheme != "https":
        raise _error("validation_error", f"{label} muss https verwenden.")
    if parsed.username or parsed.password:
        raise _error("validation_error", f"{label} darf keine Zugangsdaten enthalten.")
    host = (parsed.hostname or "").casefold()
    if host not in IMPORT_ALLOWED_HOSTS:
        allowed = ", ".join(sorted(IMPORT_ALLOWED_HOSTS))
        raise _error("validation_error", f"{label} muss auf eine dieser Hosts zeigen: {allowed}.")
    if parsed.port not in (None, 443):
        raise _error("validation_error", f"{label} muss Port 443 verwenden.")
    return value


# --------------------------------------------------------------------------- Normalisierung


def _normalise_text(value: Any, field_name: str, *, max_length: int = 20_000) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise _error("validation_error", f"{field_name} muss eine Zeichenkette sein.")
    text = value.strip()
    if len(text) > max_length:
        raise _error("validation_error", f"{field_name} ist zu lang (max. {max_length} Zeichen).")
    return text


def _normalise_price(value: Any) -> float:
    if isinstance(value, bool):
        raise _error("validation_error", "Preis muss eine Zahl sein.")
    try:
        price = round(float(value), 2)
    except (TypeError, ValueError) as err:
        raise _error("validation_error", "Preis muss eine Zahl sein.") from err
    if price < 0:
        raise _error("validation_error", "Preis darf nicht negativ sein.")
    if price > 1_000_000:
        raise _error("validation_error", "Preis ist unrealistisch hoch.")
    return price


def _normalise_stock(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise _error("validation_error", f"{field_name} muss eine ganze Zahl sein.")
    try:
        stock = int(value)
    except (TypeError, ValueError) as err:
        raise _error("validation_error", f"{field_name} muss eine ganze Zahl sein.") from err
    if stock < 0:
        raise _error("validation_error", f"{field_name} darf nicht negativ sein.")
    return stock


def _normalise_stock_map(
    value: Any, location_ids: Sequence[str], *, field_name: str
) -> dict[str, int]:
    """Bestände lesen; eine Zahl bei ``stock`` gilt als Bestand der ersten Lagerstätte."""

    result: dict[str, int] = {location_id: 0 for location_id in location_ids}
    if value is None:
        return result
    if isinstance(value, dict):
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            if key not in location_ids:
                raise _error("validation_error", f"Unbekannte Lagerstätte: {key!r}.")
            result[key] = _normalise_stock(raw_value, f"{field_name}.{key}")
        return result
    if isinstance(value, (list, tuple, set)):
        raise _error("validation_error", f"{field_name} muss eine Zuordnung von Lagerstätten sein.")
    if not location_ids:
        raise _error("validation_error", "Es ist keine Lagerstätte konfiguriert.")
    result[location_ids[0]] = _normalise_stock(value, field_name)
    return result


def _normalise_tags(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = values.split(",")
    if not isinstance(values, (list, tuple, set)):
        raise _error("validation_error", "Tags müssen eine Liste von Zeichenketten sein.")
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        if not isinstance(raw, str):
            raise _error("validation_error", "Tags müssen Zeichenketten sein.")
        tag = " ".join(raw.split())
        if not tag:
            continue
        key = tag.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(tag)
    return result


def _normalise_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().casefold()
        if lowered in {"1", "true", "yes", "ja", "on"}:
            return True
        if lowered in {"0", "false", "no", "nein", "off"}:
            return False
    raise _error("validation_error", f"Ungültiger Wahrheitswert: {value!r}")


def map_catalog_product(raw: Any, location_ids: Sequence[str]) -> dict[str, Any]:
    """Ein Katalogprodukt prüfen und normalisiert zurückgeben (Schema-Version 2).

    Die Prüfregeln sind identisch zur Basis, damit ``dry_run`` dieselben Zähler
    liefert wie ein echter Lauf. Der Batch bekommt später den Original-Payload;
    normalisiert wird nur für Planung und Vergleich.
    """

    if not isinstance(raw, Mapping):
        raise _error("validation_error", "Produkt muss ein JSON-Objekt sein.")

    article_number = str(raw.get("articleNumber", "")).strip().upper()
    if not ARTICLE_NUMBER_RE.match(article_number):
        raise _error(
            "validation_error",
            "Artikelnummer muss aus 1–12 Großbuchstaben oder Ziffern bestehen.",
        )

    category = str(raw.get("category", "")).strip().upper()
    if category not in CATEGORIES:
        allowed = ", ".join(CATEGORIES)
        raise _error("validation_error", f"Kategorie muss eine von {allowed} sein.")

    name = _normalise_text(raw.get("name"), "name", max_length=200)
    if not name:
        raise _error("validation_error", "Name ist ein Pflichtfeld.")

    stock = _normalise_stock_map(raw.get("stock", 0), location_ids, field_name="stock")
    if raw.get("salesCabinet") is not None and len(location_ids) > 1:
        stock[location_ids[1]] = _normalise_stock(raw.get("salesCabinet"), "salesCabinet")

    return {
        "articleNumber": article_number,
        "category": category,
        "name": name,
        "description": _normalise_text(raw.get("description"), "description"),
        "price": _normalise_price(raw.get("price", 0)),
        "size": _normalise_text(raw.get("size"), "size", max_length=200),
        "tags": _normalise_tags(raw.get("tags", [])),
        "customizable": _normalise_bool(raw.get("customizable", True)),
        "visible": _normalise_bool(raw.get("visible", True)),
        "stock": stock,
    }


def _catalog_signature(values: Mapping[str, Any]) -> tuple[Any, ...]:
    """Vergleichbare Katalogfelder (ohne lokale Felder wie Bilder/Archiv)."""

    comparison: list[Any] = []
    for field_name in OVERWRITE_FIELDS:
        value = values.get(field_name)
        if field_name == "tags":
            value = tuple(sorted(str(tag).casefold() for tag in value or ()))
        elif field_name == "stock":
            value = tuple(sorted((str(key), int(amount)) for key, amount in (value or {}).items()))
        comparison.append(value)
    return tuple(comparison)


# --------------------------------------------------------------------------- Planung


@dataclass(slots=True)
class ImportPlan:
    """Vorberechneter, noch nicht geschriebener Importplan."""

    create: list[str] = field(default_factory=list)
    update: list[str] = field(default_factory=list)
    categories: dict[str, str] = field(default_factory=dict)
    unchanged: int = 0
    archived_skipped: int = 0
    failed: int = 0
    #: Validierungsfehler aus der Planung (ein echter Lauf meldet sie über den Batch).
    errors: list[dict[str, str]] = field(default_factory=list)
    #: Fehler beim Bildabruf; sie entstehen nur in der Erweiterung.
    image_errors: list[dict[str, str]] = field(default_factory=list)

    def add_error(self, article_number: str, message: str) -> None:
        self.errors.append({"articleNumber": article_number, "message": message})

    def add_image_error(self, article_number: str, message: str) -> None:
        self.image_errors.append({"articleNumber": article_number, "message": message})


def _plan_import(
    raw_products: Sequence[Any],
    products: Mapping[str, Any],
    location_ids: Sequence[str],
    *,
    overwrite: bool,
) -> ImportPlan:
    """Payload prüfen und den Schreibplan berechnen – ohne irgendetwas zu schreiben."""

    plan = ImportPlan()
    seen: set[str] = set()
    for raw in raw_products:
        article_number = ""
        if isinstance(raw, Mapping):
            article_number = str(raw.get("articleNumber", "")).strip().upper()
        try:
            product = map_catalog_product(raw, location_ids)
        except SyncError as err:
            plan.failed += 1
            plan.add_error(article_number, err.message)
            continue
        article = str(product["articleNumber"])
        if article in seen:
            plan.failed += 1
            plan.add_error(article, "Doppelte Artikelnummer im Katalog.")
            continue
        seen.add(article)
        plan.categories[article] = str(product["category"])
        local = products.get(article)
        if local is None:
            plan.create.append(article)
            continue
        if getattr(local, "archived", False):
            # Archivierte Produkte werden nie reaktiviert.
            plan.archived_skipped += 1
            continue
        if not overwrite:
            plan.unchanged += 1
            continue
        if _catalog_signature(_snapshot_values(local)) == _catalog_signature(product):
            plan.unchanged += 1
            continue
        plan.update.append(article)
    return plan


def _snapshot_values(product: Any) -> dict[str, Any]:
    return {
        "name": getattr(product, "name", ""),
        "description": getattr(product, "description", ""),
        "price": float(getattr(product, "price", 0.0)),
        "size": getattr(product, "size", ""),
        "tags": list(getattr(product, "tags", ()) or ()),
        "visible": bool(getattr(product, "visible", True)),
        "customizable": bool(getattr(product, "customizable", True)),
        "stock": dict(getattr(product, "stock", {}) or {}),
    }


@dataclass(slots=True)
class ImportPreview:
    """Zähler der Antwort – Form wie :meth:`ImportBatchResult.as_dict` der Basis."""

    created: int = 0
    skipped: int = 0
    failed: int = 0
    images_imported: int = 0
    products_total: int = 0
    errors: list[dict[str, str]] = field(default_factory=list)
    dry_run: bool = False
    would_create: int = 0
    would_overwrite: int = 0
    overwritten: int = 0
    unchanged: int = 0
    summary: str = ""
    archived_skipped: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "created": self.created,
            "skipped": self.skipped,
            "failed": self.failed,
            "images_imported": self.images_imported,
            "products_total": self.products_total,
            "errors": list(self.errors),
            "dry_run": self.dry_run,
            "would_create": self.would_create,
            "would_overwrite": self.would_overwrite,
            "overwritten": self.overwritten,
            "unchanged": self.unchanged,
            "summary": self.summary,
        }


def build_summary(result: ImportPreview) -> str:
    """Deutsche Zusammenfassung des Laufs für die API-Antwort."""

    if result.images_imported == 1:
        planned, imported = "1 Bild würde importiert", "1 Bild importiert"
    else:
        images = _count(result.images_imported, "Bild", "Bilder")
        planned, imported = f"{images} würden importiert", f"{images} importiert"
    if result.dry_run:
        text = (
            f"Vorschau: {_count(result.products_total, 'Produkt', 'Produkte')} im Katalog, "
            f"{result.would_create} neu, {result.would_overwrite} zu überschreiben, "
            f"{result.unchanged} unverändert, {result.failed} fehlerhaft, "
            f"{planned}. Es wurde nichts geschrieben."
        )
    else:
        text = (
            f"Import abgeschlossen: {result.created} neu angelegt, "
            f"{result.overwritten} überschrieben, {result.skipped} unverändert, "
            f"{result.failed} fehlerhaft, {imported}."
        )
    if result.archived_skipped:
        text += f" Davon archiviert und übersprungen: {result.archived_skipped}."
    return text


# --------------------------------------------------------------------------- HTTP


async def _async_fetch_document(client: Any, url: str, *, request_timeout: float) -> dict[str, Any]:
    """Katalogdokument laden und strukturell prüfen (vor jeder Store-Änderung)."""

    try:
        async with client.get(
            url, timeout=aiohttp.ClientTimeout(total=request_timeout)
        ) as response:
            if response.status != 200:
                raise _error(
                    "catalog_unavailable",
                    f"Der Katalog ist nicht erreichbar (HTTP {response.status}).",
                )
            body = await response.read()
    except (TimeoutError, aiohttp.ClientError) as err:
        raise _error(
            "catalog_unavailable",
            "Zeitüberschreitung oder Netzwerkfehler beim Katalogabruf.",
        ) from err

    try:
        document = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as err:
        raise _error("catalog_invalid", "Der Katalog ist kein gültiges JSON-Dokument.") from err
    if not isinstance(document, dict) or not isinstance(document.get("products"), list):
        raise _error("catalog_invalid", "Der Katalog enthält keine Liste 'products'.")
    return document


@dataclass(slots=True)
class _ImageOutcome:
    """Ergebnis eines einzelnen Bildabrufs."""

    status: int | None
    data: bytes | None = None
    error: str = ""


async def _async_fetch_image(
    client: Any,
    url: str,
    *,
    request_timeout: float,
    semaphore: asyncio.Semaphore,
) -> _ImageOutcome:
    async with semaphore:
        try:
            async with client.get(
                url, timeout=aiohttp.ClientTimeout(total=request_timeout)
            ) as response:
                if response.status != 200:
                    return _ImageOutcome(status=response.status)
                data: bytes = await response.read()
        except (TimeoutError, aiohttp.ClientError):
            return _ImageOutcome(
                status=None,
                error="Zeitüberschreitung oder Netzwerkfehler beim Bildabruf.",
            )
    if not data:
        return _ImageOutcome(status=None, error="leere Antwort.")
    if len(data) > MAX_IMPORT_IMAGE_BYTES:
        return _ImageOutcome(status=None, error="Datei ist größer als 25 MB.")
    if not _looks_like_image(data):
        return _ImageOutcome(status=None, error="kein gültiges Bild (JPEG, PNG, WebP oder GIF).")
    return _ImageOutcome(status=200, data=data)


def _looks_like_image(data: bytes) -> bool:
    """Signatur prüfen; der Content-Type ist bei GitHub nicht verlässlich."""

    if data.startswith(IMAGE_SIGNATURES):
        return True
    return data[:4] == b"RIFF" and data[8:12] == b"WEBP"


@dataclass(frozen=True, slots=True)
class FetchedImage:
    """Ein geladenes Bild, das als Upload in den Batch wandert."""

    name: str
    data: bytes
    is_main: bool = False


async def _async_product_images(
    article_number: str,
    category: str,
    existing: Sequence[str],
    image_base: str,
    *,
    client: Any,
    request_timeout: float,
    semaphore: asyncio.Semaphore,
    plan: ImportPlan,
) -> list[FetchedImage]:
    """``<artikel>-01`` bis ``<artikel>-10`` prüfen; fehlende Indizes werden übersprungen.

    Bereits lokal vorhandene Bilder bleiben unangetastet und werden nicht erneut
    geladen. Zurückgegeben werden die in diesem Lauf geladenen Bilder; Fehler
    landen als Meldung im Plan (der Lauf bricht nicht ab).
    """

    names: list[str] = list(existing)
    used_indices = {
        int(match.group(1))
        for match in (_IMAGE_INDEX_RE.search(name) for name in existing)
        if match is not None
    }
    fetched: list[FetchedImage] = []
    for index in range(1, MAX_IMAGES + 1):
        if len(names) >= MAX_IMAGES:
            break
        if index in used_indices:
            # Lokales Bild bleibt erhalten; nichts nachladen oder schreiben.
            continue
        found: tuple[str, bytes] | None = None
        for suffix in IMAGE_SUFFIXES:
            name = f"{article_number}-{index:02d}{suffix}"
            url = f"{image_base}/{category}/{article_number}/{name}"
            outcome = await _async_fetch_image(
                client, url, request_timeout=request_timeout, semaphore=semaphore
            )
            if outcome.error:
                plan.add_image_error(article_number, f"Bild {name}: {outcome.error}")
                return fetched
            if outcome.status == 404:
                continue
            if outcome.status != 200 or outcome.data is None:
                plan.add_image_error(
                    article_number,
                    f"Bild {name} konnte nicht geladen werden (HTTP {outcome.status}).",
                )
                return fetched
            found = (name, outcome.data)
            break
        if found is None:
            continue
        name, data = found
        fetched.append(FetchedImage(name=name, data=data, is_main=not names))
        names.append(name)
    return fetched


async def _async_collect_images(
    plan: ImportPlan,
    products: Mapping[str, Any],
    image_base: str,
    *,
    client: Any,
    request_timeout: float,
    max_concurrency: int,
) -> dict[str, list[FetchedImage]]:
    """Bilder mit begrenzter Parallelität laden; Fehler je Produkt sammeln."""

    semaphore = asyncio.Semaphore(max(1, max_concurrency))
    result: dict[str, list[FetchedImage]] = {}

    async def _load(article: str) -> None:
        local = products.get(article)
        existing = (
            [str(getattr(image, "name", "")) for image in getattr(local, "images", ()) or ()]
            if local is not None
            else []
        )
        result[article] = await _async_product_images(
            article,
            plan.categories[article],
            existing,
            image_base,
            client=client,
            request_timeout=request_timeout,
            semaphore=semaphore,
            plan=plan,
        )

    await asyncio.gather(*(_load(article) for article in (*plan.create, *plan.update)))
    return result


# --------------------------------------------------------------------------- Lauf


def _upload_count(uploads: Mapping[str, Sequence[FetchedImage]]) -> int:
    return sum(len(images) for images in uploads.values())


async def async_import_catalog(
    facade: Any,
    settings: SyncSettings,
    options: ImportOptions,
    *,
    api: Any = None,
    session: Any | None = None,
    request_timeout: float = IMPORT_TIMEOUT,
    max_concurrency: int = IMPORT_MAX_CONCURRENCY,
) -> dict[str, Any]:
    """Katalog importieren und als Antwortform des bisherigen Endpunkts liefern.

    ``api`` liefert die Vertragsklassen ``ImportItem`` und ``ImageUpload``; ohne
    ``api`` wird der Erweiterungspunkt der Basis geladen.
    """

    if api is None:
        from .base_api import load_base_api

        api = load_base_api()

    source_url = validate_import_url(options.source_url or settings.catalog_url, label="source_url")
    image_base = validate_import_url(
        options.image_base_url or settings.image_base_url, label="image_base_url"
    ).rstrip("/")

    own_session = session is None
    client: Any = session or aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=request_timeout)
    )
    try:
        document = await _async_fetch_document(client, source_url, request_timeout=request_timeout)
        raw_products: list[Any] = list(document["products"])
        locations = await facade.async_locations()
        location_ids = [str(item.get("id", "")) for item in locations]
        products = {
            str(getattr(product, "article_number", "")): product
            for product in await facade.async_products()
        }
        plan = _plan_import(raw_products, products, location_ids, overwrite=options.overwrite)

        uploads: dict[str, list[FetchedImage]] = {}
        if options.import_images:
            uploads = await _async_collect_images(
                plan,
                products,
                image_base,
                client=client,
                request_timeout=request_timeout,
                max_concurrency=max_concurrency,
            )

        if not raw_products:
            # Leerer Katalog: nichts zu planen, kein Batch, kein Snapshot.
            empty = ImportPreview(products_total=0)
            empty.summary = build_summary(empty)
            return empty.to_dict()

        if options.dry_run:
            preview = ImportPreview(
                products_total=len(raw_products),
                errors=[*plan.errors, *plan.image_errors],
                dry_run=True,
                would_create=len(plan.create),
                would_overwrite=len(plan.update),
                failed=plan.failed,
                images_imported=_upload_count(uploads),
                archived_skipped=plan.archived_skipped,
            )
            preview.unchanged = plan.unchanged + plan.archived_skipped
            preview.skipped = preview.unchanged
            preview.summary = build_summary(preview)
            # Strukturell garantiert: bis hierher wurde nichts geschrieben und
            # kein Batch an die Fassade übergeben.
            return preview.to_dict()

        items = _build_items(raw_products, uploads, api)
        result = await facade.async_apply_import_batch(items, overwrite=options.overwrite)
        return _applied_result(raw_products, plan, result)
    finally:
        if own_session:
            await client.close()


def _build_items(
    raw_products: Sequence[Any],
    uploads: Mapping[str, Sequence[FetchedImage]],
    api: Any,
) -> list[Any]:
    """Batch-Items bauen; Bilder hängen nur an geplanten Produkten."""

    items: list[Any] = []
    attached: set[str] = set()
    for raw in raw_products:
        if not isinstance(raw, Mapping):
            # Kein Objekt: unverändert weiterreichen, die Basis meldet den Fehler.
            items.append(raw)
            continue
        article = str(raw.get("articleNumber", "")).strip().upper() or None
        images: tuple[Any, ...] = ()
        if article is not None and article in uploads and article not in attached:
            attached.add(article)
            images = tuple(
                api.image_upload(image.name, image.data, is_main=image.is_main)
                for image in uploads[article]
            )
        items.append(api.import_item(article, dict(raw), images=images))
    return items


def _applied_result(raw_products: Sequence[Any], plan: ImportPlan, result: Any) -> dict[str, Any]:
    """Antwort aus dem Batch-Ergebnis der Fassade bauen (Format wie bisher)."""

    # Produktfehler kommen aus dem Batch (dieselben Regeln wie die Basis),
    # Bildfehler entstehen bei uns – Reihenfolge wie in der Basis.
    errors: list[dict[str, str]] = []
    for error in getattr(result, "errors", ()) or ():
        if isinstance(error, Mapping):
            errors.append(dict(error))
        else:
            errors.append(
                {
                    "articleNumber": str(getattr(error, "article_number", "")),
                    "message": str(getattr(error, "message", "")),
                }
            )
    errors.extend(dict(item) for item in plan.image_errors)
    preview = ImportPreview(
        created=len(getattr(result, "created", ()) or ()),
        overwritten=len(getattr(result, "updated", ()) or ()),
        skipped=int(getattr(result, "skipped", 0) or 0),
        failed=int(getattr(result, "failed", 0) or 0),
        images_imported=int(getattr(result, "images_written", 0) or 0),
        products_total=len(raw_products),
        errors=errors,
        dry_run=False,
        unchanged=int(getattr(result, "unchanged", 0) or 0),
        archived_skipped=plan.archived_skipped,
    )
    preview.summary = str(getattr(result, "summary", "") or "") or build_summary(preview)
    return preview.to_dict()


__all__ = [
    "ARTICLE_NUMBER_RE",
    "IMAGE_SIGNATURES",
    "IMAGE_SUFFIXES",
    "IMPORT_MAX_CONCURRENCY",
    "IMPORT_TIMEOUT",
    "MAX_IMPORT_IMAGE_BYTES",
    "OVERWRITE_FIELDS",
    "FetchedImage",
    "ImportOptions",
    "ImportPlan",
    "ImportPreview",
    "async_import_catalog",
    "build_summary",
    "map_catalog_product",
    "validate_import_url",
]
