"""Tests für den Katalogimport (Feature ``import``) in der Erweiterung."""

from __future__ import annotations

import json
from typing import Any

import fake_base
import pytest
from fake_facade import FakeFacade, FakeImage, FakeProduct

from custom_components.frickeldave_store_syncfd.base_api import api_from_module
from custom_components.frickeldave_store_syncfd.const import (
    DEFAULT_CATALOG_URL,
    DEFAULT_IMAGE_BASE_URL,
    MAX_IMAGES,
)
from custom_components.frickeldave_store_syncfd.features import build_features
from custom_components.frickeldave_store_syncfd.importer import (
    ImportOptions,
    async_import_catalog,
    validate_import_url,
)
from custom_components.frickeldave_store_syncfd.settings import SyncSettings

CATEGORIES = ("3D", "EP", "LA", "SB", "WO")
JPEG = b"\xff\xd8\xff\xe0" + b"x" * 32


# --------------------------------------------------------------------------- Fakes


class FakeResponse:
    """Minimaler Ersatz für ``aiohttp.ClientResponse`` (async context manager)."""

    def __init__(self, status: int, body: bytes = b"") -> None:
        self.status = status
        self._body = body

    async def read(self) -> bytes:
        return self._body

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class FakeSession:
    """Antwortet auf Katalog- und Bild-URLs aus vorbereiteten Daten."""

    def __init__(
        self,
        catalog: Any = None,
        *,
        images: dict[str, tuple[int, bytes]] | None = None,
        catalog_status: int = 200,
        catalog_body: bytes | None = None,
    ) -> None:
        self.catalog = catalog
        self.images = dict(images or {})
        self.catalog_status = catalog_status
        self.catalog_body = catalog_body
        self.requests: list[str] = []
        self.closed = False

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.requests.append(url)
        if url.startswith(DEFAULT_IMAGE_BASE_URL):
            status, body = self.images.get(url, (404, b""))
            return FakeResponse(status, body)
        if self.catalog_body is not None:
            return FakeResponse(self.catalog_status, self.catalog_body)
        return FakeResponse(self.catalog_status, json.dumps(self.catalog).encode("utf-8"))

    async def close(self) -> None:  # pragma: no cover - wird nie aufgerufen
        self.closed = True


def _catalog(count: int = 2, *, with_sales_cabinet: bool = True) -> dict[str, Any]:
    products: list[dict[str, Any]] = []
    for index in range(count):
        category = CATEGORIES[index % len(CATEGORIES)]
        entry: dict[str, Any] = {
            "articleNumber": f"{category}{index:04d}",
            "category": category,
            "name": f"Produkt {index}",
            "description": f"Beschreibung {index}",
            "price": 10.0 + index,
            "size": "10cm",
            "tags": ["Handmade"],
            "stock": 1,
            "visible": True,
            "customizable": True,
        }
        if with_sales_cabinet:
            entry["salesCabinet"] = 2
        products.append(entry)
    return {"products": products}


def _single(
    article: str = "LA0001", *, name: str = "Produkt 1", **overrides: Any
) -> dict[str, Any]:
    values: dict[str, Any] = {
        "articleNumber": article,
        "category": article[:2],
        "name": name,
        "description": "Beschreibung",
        "price": 11.0,
        "size": "10cm",
        "tags": ["Handmade"],
        "stock": 1,
        "salesCabinet": 2,
        "visible": True,
        "customizable": True,
    }
    values.update(overrides)
    return values


def _image_url(category: str, article: str, name: str) -> str:
    return f"{DEFAULT_IMAGE_BASE_URL}/{category}/{article}/{name}"


def _settings(**overrides: Any) -> SyncSettings:
    return SyncSettings.from_mapping({"token": "ghp-test", **overrides})


def _features(session: FakeSession, settings: SyncSettings | None = None) -> dict[str, Any]:
    return build_features(
        api_from_module(fake_base.build_module()),
        (lambda: settings) if settings is not None else _settings,
        session_factory=lambda: session,
    )


async def _import(
    facade: FakeFacade,
    session: FakeSession,
    *,
    payload: dict[str, Any] | None = None,
    settings: SyncSettings | None = None,
) -> dict[str, Any]:
    feature = _features(session, settings)["import"]
    response = await feature.handler(
        fake_base.FeatureRequest("import", payload or {}, "user-1", "Test", facade)
    )
    return dict(response.payload)


def _seeded_facade(**overrides: Any) -> FakeFacade:
    values: dict[str, Any] = {
        "article_number": "LA0001",
        "name": "Lokal",
        "category": "LA",
        "price": 1.0,
        "description": "alter Text",
        "size": "5cm",
        "tags": ("Alt",),
        "visible": False,
        "customizable": False,
        "stock": {"lager": 7, "verkaufsschrank": 2},
        "images": (FakeImage("LA0001-01.jpg", is_main=True),),
        "created_at": "2020-01-01T00:00:00Z",
        "updated_at": "2020-01-01T00:00:00Z",
    }
    values.update(overrides)
    return FakeFacade(
        [FakeProduct(**values)],
        image_data={("LA0001", "LA0001-01.jpg"): JPEG},
    )


# --------------------------------------------------------------------------- Import


async def test_import_creates_products_and_images() -> None:
    facade = FakeFacade([])
    catalog = _catalog(2)
    session = FakeSession(
        catalog,
        images={
            _image_url("3D", "3D0000", "3D0000-01.jpg"): (200, JPEG),
            _image_url("EP", "EP0001", "EP0001-01.jpg"): (200, JPEG),
        },
    )

    result = await _import(facade, session)

    assert result["created"] == 2
    assert result["overwritten"] == 0
    assert result["failed"] == 0
    assert result["skipped"] == 0
    assert result["products_total"] == 2
    assert result["images_imported"] == 2
    assert result["dry_run"] is False
    assert result["errors"] == []
    assert "Import abgeschlossen" in result["summary"]
    assert facade.products["3D0000"].name == "Produkt 0"
    assert ("3D0000", "3D0000-01.jpg") in facade.image_data


async def test_response_contract_matches_the_old_endpoint() -> None:
    facade = FakeFacade([])
    session = FakeSession(_catalog(1), images={})

    result = await _import(facade, session)

    assert set(result) == {
        "created",
        "skipped",
        "failed",
        "images_imported",
        "products_total",
        "errors",
        "dry_run",
        "would_create",
        "would_overwrite",
        "overwritten",
        "unchanged",
        "summary",
    }
    assert isinstance(result["created"], int)
    assert isinstance(result["errors"], list)


async def test_second_run_creates_nothing_and_never_overwrites() -> None:
    facade = FakeFacade([])
    session = FakeSession(_catalog(2), images={})
    await _import(facade, session)

    result = await _import(facade, FakeSession(_catalog(2), images={}))

    assert result["created"] == 0
    assert result["skipped"] == 2
    assert result["overwritten"] == 0


async def test_dry_run_writes_nothing_and_reports_full_preview() -> None:
    facade = _seeded_facade()
    session = FakeSession(
        {"products": [_single("LA0001", name="Lokal neu"), _single("LA0002", name="Neu")]},
        images={
            _image_url("LA", "LA0001", "LA0001-02.jpg"): (200, JPEG),
            _image_url("LA", "LA0002", "LA0002-01.jpg"): (200, JPEG),
        },
    )
    before = facade.state_hash()

    result = await _import(facade, session, payload={"dry_run": True, "overwrite": True})

    assert facade.state_hash() == before, "Dry-Run darf nichts verändern"
    assert facade.batches == [], "Dry-Run darf keinen Batch anwenden"
    assert facade.snapshots == [], "Dry-Run darf keinen Snapshot anlegen"
    assert result["dry_run"] is True
    assert result["created"] == 0
    assert result["overwritten"] == 0
    assert result["would_create"] == 1
    assert result["would_overwrite"] == 1
    assert result["unchanged"] == 0
    assert result["skipped"] == 0
    assert result["images_imported"] == 2
    assert result["errors"] == []
    assert result["summary"] == (
        "Vorschau: 2 Produkte im Katalog, 1 neu, 1 zu überschreiben, 0 unverändert, "
        "0 fehlerhaft, 2 Bilder würden importiert. Es wurde nichts geschrieben."
    )


async def test_dry_run_without_overwrite_never_proposes_overwrites() -> None:
    facade = _seeded_facade()
    session = FakeSession({"products": [_single("LA0001")]}, images={})
    before = facade.state_hash()

    result = await _import(facade, session, payload={"dry_run": True})

    assert facade.state_hash() == before
    assert result["would_create"] == 0
    assert result["would_overwrite"] == 0
    assert result["unchanged"] == 1
    assert "Es wurde nichts geschrieben." in result["summary"]


async def test_overwrite_replaces_catalog_fields_and_keeps_local_state() -> None:
    facade = _seeded_facade()
    session = FakeSession({"products": [_single("LA0001")]}, images={})

    result = await _import(facade, session, payload={"overwrite": True})

    assert result["overwritten"] == 1
    assert result["created"] == 0
    local = facade.products["LA0001"]
    assert local.name == "Produkt 1"
    assert local.price == 11.0
    assert local.stock == {"lager": 1, "verkaufsschrank": 2}
    assert local.archived is False
    assert local.created_at == "2020-01-01T00:00:00Z"
    assert [image.name for image in local.images] == ["LA0001-01.jpg"]


async def test_overwrite_false_never_changes_existing_products() -> None:
    facade = _seeded_facade()
    session = FakeSession({"products": [_single("LA0001")]}, images={})

    result = await _import(facade, session)

    assert result["created"] == 0
    assert result["overwritten"] == 0
    assert result["skipped"] == 1
    assert facade.products["LA0001"].name == "Lokal"


async def test_overwrite_is_idempotent_for_unchanged_products() -> None:
    facade = _seeded_facade()
    catalog = {"products": [_single("LA0001")]}
    await _import(facade, FakeSession(catalog, images={}), payload={"overwrite": True})

    result = await _import(facade, FakeSession(catalog, images={}), payload={"overwrite": True})

    assert result["overwritten"] == 0
    assert result["skipped"] == 1


async def test_archived_products_are_skipped() -> None:
    facade = _seeded_facade(archived=True)
    session = FakeSession({"products": [_single("LA0001")]}, images={})

    result = await _import(facade, session, payload={"overwrite": True})

    assert result["overwritten"] == 0
    assert result["skipped"] == 1
    assert facade.products["LA0001"].archived is True
    assert facade.products["LA0001"].name == "Lokal"


async def test_duplicates_and_invalid_products_are_reported() -> None:
    facade = FakeFacade([])
    products = _catalog(1)["products"]
    broken = dict(products[0])
    broken["category"] = "XX"
    catalog = {"products": [products[0], dict(products[0]), broken]}

    result = await _import(facade, FakeSession(catalog, images={}))

    assert result["created"] == 1
    assert result["failed"] == 2
    codes = [error["message"] for error in result["errors"]]
    assert any("Doppelte Artikelnummer" in message for message in codes)
    assert any("Kategorie" in message for message in codes)


async def test_non_object_entries_are_counted_as_failed() -> None:
    facade = FakeFacade([])

    result = await _import(
        facade, FakeSession({"products": ["kaputt", _single("LA0001")]}, images={})
    )

    assert result["created"] == 1
    assert result["failed"] == 1
    assert result["errors"][0]["articleNumber"] == ""


async def test_empty_catalog_writes_nothing() -> None:
    facade = FakeFacade([])
    before = facade.state_hash()

    result = await _import(facade, FakeSession({"products": []}, images={}))

    assert result["products_total"] == 0
    assert result["created"] == 0
    assert facade.batches == [], "leerer Katalog braucht keinen Batch"
    assert facade.state_hash() == before
    assert "Import abgeschlossen" in result["summary"]


async def test_product_errors_are_reported_once() -> None:
    """Fehler erscheinen genau einmal: Batch-Fehler plus Bildfehler, nicht doppelt."""

    facade = FakeFacade([])
    broken = _single("LA0001")
    broken["name"] = ""

    result = await _import(facade, FakeSession({"products": [broken]}, images={}))

    assert result["failed"] == 1
    assert len(result["errors"]) == 1
    assert result["errors"][0]["articleNumber"] == "LA0001"


async def test_product_and_image_errors_are_merged() -> None:
    facade = FakeFacade([])
    broken = _single("LA0002")
    broken["category"] = "XX"
    session = FakeSession(
        {"products": [_single("LA0001"), broken]},
        images={_image_url("LA", "LA0001", "LA0001-01.jpg"): (500, b"kaputt")},
    )

    result = await _import(facade, session)

    assert result["created"] == 1
    assert result["failed"] == 1
    assert len(result["errors"]) == 2
    assert result["errors"][0]["articleNumber"] == "LA0002"
    assert "Bild" in result["errors"][1]["message"]


# --------------------------------------------------------------------------- Bilder


async def test_image_gaps_are_probed_and_all_images_imported() -> None:
    facade = FakeFacade([])
    catalog = {"products": _catalog(1)["products"]}
    session = FakeSession(
        catalog,
        images={
            _image_url("3D", "3D0000", "3D0000-01.jpg"): (200, JPEG),
            _image_url("3D", "3D0000", "3D0000-03.jpg"): (200, JPEG),
        },
    )

    result = await _import(facade, session)

    names = [image.name for image in facade.products["3D0000"].images]
    assert names == ["3D0000-01.jpg", "3D0000-03.jpg"]
    assert facade.products["3D0000"].images[0].is_main is True
    assert result["images_imported"] == 2


async def test_jpeg_extension_is_used_when_jpg_is_missing() -> None:
    facade = FakeFacade([])
    session = FakeSession(
        {"products": _catalog(1)["products"]},
        images={_image_url("3D", "3D0000", "3D0000-01.jpeg"): (200, JPEG)},
    )

    result = await _import(facade, session)

    assert [image.name for image in facade.products["3D0000"].images] == ["3D0000-01.jpeg"]
    assert result["images_imported"] == 1


async def test_image_error_is_collected_and_import_continues() -> None:
    facade = FakeFacade([])
    session = FakeSession(
        {"products": _catalog(1)["products"]},
        images={_image_url("3D", "3D0000", "3D0000-01.jpg"): (500, b"kaputt")},
    )

    result = await _import(facade, session)

    assert result["created"] == 1
    assert result["images_imported"] == 0
    assert any("HTTP 500" in error["message"] for error in result["errors"])


async def test_non_image_response_is_reported() -> None:
    facade = FakeFacade([])
    session = FakeSession(
        {"products": _catalog(1)["products"]},
        images={_image_url("3D", "3D0000", "3D0000-01.jpg"): (200, b"<html>kein Bild</html>")},
    )

    result = await _import(facade, session)

    assert result["images_imported"] == 0
    assert any("kein gültiges Bild" in error["message"] for error in result["errors"])


async def test_at_most_ten_images_are_imported() -> None:
    facade = FakeFacade([])
    session = FakeSession(
        {"products": _catalog(1)["products"]},
        images={
            _image_url("3D", "3D0000", f"3D0000-{index:02d}.jpg"): (200, JPEG)
            for index in range(1, MAX_IMAGES + 3)
        },
    )

    result = await _import(facade, session)

    assert result["images_imported"] == MAX_IMAGES
    assert len(facade.products["3D0000"].images) == MAX_IMAGES


async def test_existing_local_images_are_not_fetched_again() -> None:
    facade = _seeded_facade()
    session = FakeSession(
        {"products": [_single("LA0001")]},
        images={_image_url("LA", "LA0001", "LA0001-02.jpg"): (200, JPEG)},
    )

    await _import(facade, session, payload={"overwrite": True})

    assert _image_url("LA", "LA0001", "LA0001-01.jpg") not in session.requests
    assert _image_url("LA", "LA0001", "LA0001-02.jpg") in session.requests


async def test_import_images_false_skips_the_image_requests() -> None:
    facade = FakeFacade([])
    session = FakeSession({"products": _catalog(1)["products"]})

    result = await _import(facade, session, payload={"import_images": False})

    assert result["created"] == 1
    assert result["images_imported"] == 0
    assert session.requests == [DEFAULT_CATALOG_URL]


# --------------------------------------------------------------------------- Fehler


async def test_broken_catalog_aborts_without_touching_the_store() -> None:
    facade = FakeFacade([])
    before = facade.state_hash()
    session = FakeSession(catalog_body=b"kein json")

    with pytest.raises(fake_base.ExtensionError) as err:
        await _import(facade, session)

    assert err.value.code == "catalog_invalid"
    assert err.value.status == 422
    assert facade.state_hash() == before
    assert facade.batches == []


async def test_unreachable_catalog_is_a_structured_error() -> None:
    facade = FakeFacade([])
    session = FakeSession(catalog_body=b"", catalog_status=503)

    with pytest.raises(fake_base.ExtensionError) as err:
        await _import(facade, session)

    assert err.value.code == "catalog_unavailable"
    assert err.value.status == 502
    assert "nicht erreichbar" in err.value.message


async def test_catalog_without_products_list_is_rejected() -> None:
    facade = FakeFacade([])
    session = FakeSession({"artikel": []})

    with pytest.raises(fake_base.ExtensionError) as err:
        await _import(facade, session)

    assert err.value.code == "catalog_invalid"
    assert "products" in err.value.message


@pytest.mark.parametrize(
    "url",
    [
        "http://frickeldave.de/data/handmade.json",
        "https://example.com/data/handmade.json",
        "https://127.0.0.1/data/handmade.json",
        "https://raw.githubusercontent.com.evil.example/x.jpg",
        "https://user:pass@frickeldave.de/data/handmade.json",
        "https://frickeldave.de:8443/data/handmade.json",
        "ftp://frickeldave.de/data/handmade.json",
        "",
        "   ",
    ],
)
async def test_custom_urls_outside_the_allowlist_are_rejected(url: str) -> None:
    facade = FakeFacade([])

    with pytest.raises(fake_base.ExtensionError) as err:
        await _import(facade, FakeSession(_catalog(1)), payload={"source_url": url})

    assert err.value.code == "validation_error"
    assert err.value.status == 400
    assert facade.batches == []


@pytest.mark.parametrize(
    "url",
    [
        DEFAULT_CATALOG_URL,
        DEFAULT_IMAGE_BASE_URL,
        "https://frickeldave.de/data/handmade.json",
        "https://raw.githubusercontent.com/Frickeldave/frickeldave.github.io/dev/"
        "src/assets/handmade",
    ],
)
def test_default_and_allowed_urls_pass(url: str) -> None:
    assert validate_import_url(url, label="source_url") == url


async def test_custom_source_requires_app_admin() -> None:
    facade = FakeFacade([], roles={"product"})

    with pytest.raises(fake_base.ExtensionError) as err:
        await _import(
            facade,
            FakeSession(_catalog(1)),
            payload={"source_url": DEFAULT_CATALOG_URL, "dry_run": True},
        )

    assert err.value.code == "forbidden"
    assert err.value.status == 403
    assert facade.batches == []


async def test_default_source_needs_only_the_product_role() -> None:
    facade = FakeFacade([], roles={"product"})

    result = await _import(facade, FakeSession(_catalog(1), images={}), payload={"dry_run": True})

    assert result["would_create"] == 1


@pytest.mark.parametrize(
    ("payload", "fragment"),
    [
        ({"unbekannt": True}, "Unbekannte Felder"),
        ({"dry_run": "ja"}, "dry_run"),
        ({"overwrite": 1}, "overwrite"),
        ({"import_images": "nein"}, "import_images"),
        ({"source_url": 5}, "source_url"),
    ],
)
async def test_import_payload_validation_returns_400(
    payload: dict[str, Any], fragment: str
) -> None:
    facade = FakeFacade([])

    with pytest.raises(fake_base.ExtensionError) as err:
        await _import(facade, FakeSession(_catalog(1)), payload=payload)

    assert err.value.code == "validation_error"
    assert err.value.status == 400
    assert fragment in err.value.message


def test_feature_requires_product_role() -> None:
    """Die Basis prüft ``required_role=product`` vor dem Handler."""

    feature = _features(FakeSession(_catalog(1)))["import"]

    assert feature.required_role == "product"
    assert feature.ready is True
    assert feature.provides_panel_routes is True


async def test_direct_import_uses_the_settings_default_urls() -> None:
    """Ohne Payload-Quellen gelten die Vorgaben der Erweiterung."""

    facade = FakeFacade([])
    session = FakeSession({"products": _catalog(1)["products"]})

    result = await async_import_catalog(
        facade,
        _settings(),
        ImportOptions(dry_run=True, import_images=False),
        api=api_from_module(fake_base.build_module()),
        session=session,
    )

    assert session.requests == [DEFAULT_CATALOG_URL]
    assert result["would_create"] == 1


def test_catalog_and_image_defaults_are_frickeldave() -> None:
    assert DEFAULT_CATALOG_URL == "https://frickeldave.de/data/handmade.json"
    assert DEFAULT_IMAGE_BASE_URL == (
        "https://raw.githubusercontent.com/Frickeldave/frickeldave.github.io/dev/"
        "src/assets/handmade"
    )
