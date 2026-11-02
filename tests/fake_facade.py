"""Fake-Fassade für die Tests der Erweiterung.

Bildet die öffentliche Form von ``StoreFacade`` (Contract 1) nach: Produkte und
Lagerstätten lesen, Rechte prüfen, Export bauen, Snapshot anlegen und einen
Import-Batch anwenden. Der Zustand ist rein speicherbasiert; ``state_hash``
belegt, dass ein Dry-Run nichts verändert.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

CATALOG_PATH = "public/data/handmade.json"
IMAGES_PATH = "src/assets/handmade"
CATEGORIES = ("3D", "EP", "LA", "SB", "WO")
ARTICLE_RE = re.compile(r"^[A-Z0-9]{1,12}$")
MAX_IMAGES = 10
OVERWRITE_FIELDS = (
    "name",
    "description",
    "price",
    "size",
    "tags",
    "visible",
    "customizable",
    "stock",
)


class FakePermissionDenied(Exception):
    """Wie ``PermissionDenied`` der Basis: Code ``forbidden``, Status 403."""

    code = "forbidden"
    status = 403


@dataclass(slots=True)
class FakeImage:
    name: str
    is_main: bool = False
    order: int = 0


@dataclass(slots=True)
class FakeProduct:
    article_number: str
    name: str
    description: str = ""
    price: float = 10.0
    size: str = ""
    category: str = "LA"
    tags: tuple[str, ...] = ()
    visible: bool = True
    customizable: bool = True
    archived: bool = False
    stock: dict[str, int] = field(default_factory=dict)
    images: tuple[FakeImage, ...] = ()
    created_at: str = "2026-01-01T00:00:00Z"
    updated_at: str = "2026-01-01T00:00:00Z"


@dataclass(slots=True)
class FakeArtifact:
    path: str
    data: bytes
    kind: str
    article_number: str = ""


@dataclass(slots=True)
class FakeBundle:
    catalog_path: str
    images_path: str
    catalog_bytes: bytes
    artifacts: tuple[FakeArtifact, ...]
    product_count: int
    image_count: int
    empty_catalog: bool
    generated_at: str = "2026-01-01T00:00:00Z"


@dataclass(slots=True)
class FakeItemError:
    article_number: str
    message: str


@dataclass(slots=True)
class FakeBatchResult:
    created: tuple[str, ...] = ()
    updated: tuple[str, ...] = ()
    unchanged: int = 0
    skipped: int = 0
    failed: int = 0
    images_written: int = 0
    errors: tuple[FakeItemError, ...] = ()
    snapshot: str | None = None
    summary: str = ""


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().casefold()
        if lowered in {"1", "true", "yes", "ja", "on"}:
            return True
        if lowered in {"0", "false", "no", "nein", "off"}:
            return False
    if value is None:
        return default
    raise ValueError(f"Ungültiger Wahrheitswert: {value!r}")


def _number(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("Bestand muss eine ganze Zahl sein.")
    amount = int(value)
    if amount < 0:
        raise ValueError("Bestand darf nicht negativ sein.")
    return amount


class FakeFacade:
    """Fassade über einem In-Memory-Store."""

    def __init__(
        self,
        products: list[FakeProduct] | None = None,
        *,
        roles: set[str] | None = None,
        locations: list[dict[str, str]] | None = None,
        image_data: dict[tuple[str, str], bytes] | None = None,
    ) -> None:
        self.products: dict[str, FakeProduct] = {
            product.article_number: product for product in products or []
        }
        self.roles = set(roles if roles is not None else {"product", "app_admin"})
        self.locations = list(
            locations
            if locations is not None
            else [
                {"id": "lager", "name": "Lager"},
                {"id": "verkaufsschrank", "name": "Verkaufsschrank"},
            ]
        )
        self.image_data = dict(image_data or {})
        self.calls: list[str] = []
        self.batches: list[tuple[list[Any], bool]] = []
        self.snapshots: list[str] = []
        self.user_id = "user"
        self.user_name = "Test"
        self.settings: dict[str, Any] = {}

    # ------------------------------------------------------------ Rechte
    def can(self, role: str) -> bool:
        return role in self.roles

    def require(self, role: str) -> None:
        if not self.can(role):
            raise FakePermissionDenied(f"Die Rolle {role} fehlt.")

    # ------------------------------------------------------------ Lesen
    async def async_products(self) -> tuple[FakeProduct, ...]:
        self.calls.append("products")
        return tuple(sorted(self.products.values(), key=lambda product: product.article_number))

    async def async_locations(self) -> tuple[dict[str, str], ...]:
        self.calls.append("locations")
        return tuple(dict(item) for item in self.locations)

    async def async_article_numbers(self) -> frozenset[str]:
        self.calls.append("article_numbers")
        return frozenset(self.products)

    async def async_create_snapshot(self, reason: str = "manual") -> str | None:
        self.snapshots.append(reason)
        return f"frickeldave_store-{len(self.snapshots)}.json"

    # ------------------------------------------------------------ Export
    def _location_ids(self) -> list[str]:
        return [item["id"] for item in self.locations]

    def _catalog_entry(self, product: FakeProduct) -> dict[str, Any]:
        ids = self._location_ids()
        return {
            "name": product.name,
            "description": product.description,
            "tags": list(product.tags),
            "price": product.price,
            "size": product.size,
            "visible": product.visible,
            "customizable": product.customizable,
            "salesCabinet": product.stock.get(ids[1], 0) if len(ids) > 1 else 0,
            "stock": product.stock.get(ids[0], 0) if ids else 0,
            "articleNumber": product.article_number,
            "category": product.category,
        }

    async def async_build_export(
        self,
        *,
        catalog_path: str = CATALOG_PATH,
        images_path: str = IMAGES_PATH,
        visible_only: bool = False,
    ) -> FakeBundle:
        self.calls.append("build_export")
        entries: list[dict[str, Any]] = []
        artifacts: list[FakeArtifact] = []
        for product in sorted(self.products.values(), key=lambda item: item.article_number):
            if product.archived:
                continue
            if visible_only and not product.visible:
                continue
            entries.append(self._catalog_entry(product))
            for image in product.images:
                data = self.image_data.get((product.article_number, image.name))
                if data is None:
                    continue
                artifacts.append(
                    FakeArtifact(
                        path=f"{images_path}/{product.article_number}/{image.name}",
                        data=data,
                        kind="image",
                        article_number=product.article_number,
                    )
                )
        catalog = {
            "$schema": "http://json-schema.org/draft-04/schema#",
            "description": "Generated by Frickeldave Admin",
            "products": entries,
        }
        catalog_bytes = (json.dumps(catalog, ensure_ascii=False, indent=2) + "\n").encode()
        return FakeBundle(
            catalog_path=catalog_path,
            images_path=images_path,
            catalog_bytes=catalog_bytes,
            artifacts=tuple(artifacts),
            product_count=len(entries),
            image_count=len(artifacts),
            empty_catalog=not entries,
        )

    # ------------------------------------------------------------ Import
    def _normalise(self, payload: dict[str, Any]) -> dict[str, Any]:
        article = _text(payload.get("articleNumber")).upper()
        if not ARTICLE_RE.match(article):
            raise ValueError("Artikelnummer muss aus 1–12 Großbuchstaben oder Ziffern bestehen.")
        category = _text(payload.get("category")).upper()
        if category not in CATEGORIES:
            raise ValueError(f"Kategorie muss eine von {', '.join(CATEGORIES)} sein.")
        name = _text(payload.get("name"))
        if not name:
            raise ValueError("Name ist ein Pflichtfeld.")
        ids = self._location_ids()
        stock = {location_id: 0 for location_id in ids}
        raw_stock = payload.get("stock", 0)
        if isinstance(raw_stock, dict):
            for key, value in raw_stock.items():
                stock[str(key)] = _number(value)
        elif ids:
            stock[ids[0]] = _number(raw_stock)
        if payload.get("salesCabinet") is not None and len(ids) > 1:
            stock[ids[1]] = _number(payload["salesCabinet"])
        return {
            "articleNumber": article,
            "category": category,
            "name": name,
            "description": _text(payload.get("description")),
            "price": round(float(payload.get("price", 0) or 0), 2),
            "size": _text(payload.get("size")),
            "tags": [str(tag) for tag in payload.get("tags", []) or []],
            "visible": _bool(payload.get("visible"), True),
            "customizable": _bool(payload.get("customizable"), True),
            "stock": stock,
        }

    @staticmethod
    def _signature(values: dict[str, Any]) -> tuple[Any, ...]:
        comparison: list[Any] = []
        for field_name in OVERWRITE_FIELDS:
            value = values.get(field_name)
            if field_name == "tags":
                value = tuple(sorted(str(tag).casefold() for tag in value or ()))
            elif field_name == "stock":
                value = tuple(
                    sorted((str(key), int(amount)) for key, amount in (value or {}).items())
                )
            comparison.append(value)
        return tuple(comparison)

    async def async_apply_import_batch(
        self, items: list[Any], *, overwrite: bool = False
    ) -> FakeBatchResult:
        self.calls.append("apply_import_batch")
        self.batches.append((list(items), overwrite))
        self.snapshots.append("pre-import-batch")

        created: list[str] = []
        updated: list[str] = []
        errors: list[FakeItemError] = []
        skipped = 0
        failed = 0
        seen: set[str] = set()
        for item in items:
            payload = dict(getattr(item, "payload", {}) or {})
            raw_article = _text(payload.get("articleNumber")).upper()
            try:
                values = self._normalise(payload)
            except (TypeError, ValueError) as err:
                failed += 1
                errors.append(FakeItemError(raw_article, str(err)))
                continue
            article = str(values["articleNumber"])
            if article in seen:
                failed += 1
                errors.append(FakeItemError(article, "Doppelte Artikelnummer im Katalog."))
                continue
            seen.add(article)
            existing = self.products.get(article)
            if existing is None:
                self.products[article] = FakeProduct(
                    article_number=article,
                    name=str(values["name"]),
                    description=str(values["description"]),
                    price=float(values["price"]),
                    size=str(values["size"]),
                    category=str(values["category"]),
                    tags=tuple(values["tags"]),
                    visible=bool(values["visible"]),
                    customizable=bool(values["customizable"]),
                    stock=dict(values["stock"]),
                )
                created.append(article)
                continue
            if existing.archived:
                skipped += 1
                continue
            if not overwrite:
                skipped += 1
                continue
            if self._signature(self._snapshot(existing)) == self._signature(values):
                skipped += 1
                continue
            existing.name = str(values["name"])
            existing.description = str(values["description"])
            existing.price = float(values["price"])
            existing.size = str(values["size"])
            existing.tags = tuple(values["tags"])
            existing.visible = bool(values["visible"])
            existing.customizable = bool(values["customizable"])
            existing.stock = dict(values["stock"])
            updated.append(article)

        images_written = 0
        for item in items:
            uploads = tuple(getattr(item, "images", ()) or ())
            if not uploads:
                continue
            payload = dict(getattr(item, "payload", {}) or {})
            article = _text(payload.get("articleNumber")).upper()
            product = self.products.get(article)
            if product is None or product.archived:
                continue
            names = [image.name for image in product.images]
            for upload in uploads:
                if upload.name in names or len(names) >= MAX_IMAGES:
                    continue
                self.image_data[(article, upload.name)] = bytes(upload.data)
                names.append(upload.name)
                product.images = (
                    *product.images,
                    FakeImage(
                        name=upload.name,
                        is_main=upload.is_main or not product.images,
                        order=len(product.images),
                    ),
                )
                images_written += 1

        summary = (
            f"Import abgeschlossen: {len(created)} neu angelegt, {len(updated)} "
            f"überschrieben, {skipped} unverändert, {failed} fehlerhaft, "
            f"{images_written} Bilder importiert."
        )
        return FakeBatchResult(
            created=tuple(created),
            updated=tuple(updated),
            skipped=skipped,
            failed=failed,
            images_written=images_written,
            errors=tuple(errors),
            snapshot=self.snapshots[-1] if self.snapshots else None,
            summary=summary,
        )

    @staticmethod
    def _snapshot(product: FakeProduct) -> dict[str, Any]:
        return {
            "name": product.name,
            "description": product.description,
            "price": product.price,
            "size": product.size,
            "tags": list(product.tags),
            "visible": product.visible,
            "customizable": product.customizable,
            "stock": dict(product.stock),
        }

    # ------------------------------------------------------------ Nachweis
    def state_hash(self) -> str:
        """Hash über Store, Bilder, Batches und Snapshots (Dry-Run-Nachweis)."""

        payload = {
            "products": {
                article: {
                    "name": product.name,
                    "description": product.description,
                    "price": product.price,
                    "size": product.size,
                    "tags": list(product.tags),
                    "visible": product.visible,
                    "customizable": product.customizable,
                    "archived": product.archived,
                    "stock": product.stock,
                    "images": [[image.name, image.is_main] for image in product.images],
                }
                for article, product in sorted(self.products.items())
            },
            "images": sorted(
                [f"{article}/{name}", hashlib.sha256(data).hexdigest()]
                for (article, name), data in self.image_data.items()
            ),
            "batches": len(self.batches),
            "snapshots": list(self.snapshots),
        }
        encoded = json.dumps(payload, sort_keys=True, default=str).encode()
        return hashlib.sha256(encoded).hexdigest()
