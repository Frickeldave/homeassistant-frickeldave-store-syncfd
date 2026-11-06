"""Tests für Veröffentlichen (Feature ``publish``) in der Erweiterung."""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

import aiohttp
import fake_base
import pytest
from fake_facade import FakeFacade, FakeImage, FakeProduct
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockModule,
    mock_integration,
)

from custom_components.frickeldave_store_syncfd.const import (
    BASE_DOMAIN,
    CATALOG_PATH,
    DOMAIN,
    IMAGES_PATH,
    VERSION,
)
from custom_components.frickeldave_store_syncfd.errors import SyncError
from custom_components.frickeldave_store_syncfd.publish import (
    RETRY_ATTEMPTS,
    RETRY_STATUSES,
    FileChange,
    GitHubClient,
    PublishService,
    RemoteTree,
    blob_sha,
    build_artifacts,
    diff_artifacts,
    publish_removal_guard,
    unchanged_product_counts,
)
from custom_components.frickeldave_store_syncfd.settings import SyncSettings

TOKEN = "ghp-test-geheim"


# --------------------------------------------------------------------------- Hilfen


class FakeGitHub:
    """Zeichnet alle Aufrufe auf; Schreibmethoden zählen als Wirkung."""

    def __init__(
        self,
        *,
        remote: dict[str, str] | None = None,
        files: dict[str, bytes] | None = None,
        prod_remote: dict[str, str] | None = None,
        prod_files: dict[str, bytes] | None = None,
        prod_branch: str = "main",
    ) -> None:
        self.remote = dict(remote or {})
        self.files = dict(files or {})
        self.prod_branch = prod_branch
        # Ohne eigene Prod-Sicht entspricht der Zielbranch dem Dev-Stand.
        self.prod_remote = dict(self.remote if prod_remote is None else prod_remote)
        self.prod_files = dict(self.files if prod_files is None else prod_files)
        self.commits: list[dict[str, Any]] = []
        self.dispatches: list[dict[str, Any]] = []
        self.reads: list[str] = []
        self.read_branches: list[tuple[str, str | None]] = []
        self.tree_reads: list[str | None] = []
        self.closed = False

    def _is_prod(self, branch: str | None) -> bool:
        return branch is not None and branch == self.prod_branch

    async def async_read_tree(self, branch: str | None = None) -> RemoteTree:
        self.tree_reads.append(branch)
        if self._is_prod(branch):
            return RemoteTree(
                head_sha="main-head", tree_sha="main-tree", blobs=dict(self.prod_remote)
            )
        return RemoteTree(head_sha="head-sha", tree_sha="base-tree", blobs=dict(self.remote))

    async def async_read_file(self, path: str, branch: str | None = None) -> bytes | None:
        self.reads.append(path)
        self.read_branches.append((path, branch))
        if self._is_prod(branch):
            return self.prod_files.get(path)
        return self.files.get(path)

    async def async_commit(
        self,
        artifacts: dict[str, bytes],
        changes: list[Any],
        *,
        head_sha: str,
        tree_sha: str,
        message: str,
    ) -> str:
        self.commits.append(
            {
                "artifacts": dict(artifacts),
                "changes": list(changes),
                "head_sha": head_sha,
                "tree_sha": tree_sha,
                "message": message,
            }
        )
        for change in changes:
            if change.status == "removed":
                self.remote.pop(change.path, None)
            else:
                self.remote[change.path] = blob_sha(artifacts[change.path])
        return f"commit-{len(self.commits)}"

    async def async_dispatch_workflow(
        self, workflow: str, *, ref: str, inputs: dict[str, Any]
    ) -> None:
        self.dispatches.append({"workflow": workflow, "ref": ref, "inputs": inputs})

    async def async_close(self) -> None:
        self.closed = True


def _settings(**overrides: Any) -> SyncSettings:
    values: dict[str, Any] = {"token": TOKEN, **overrides}
    return SyncSettings.from_mapping(values)


def _product(
    article: str = "LAABCD",
    *,
    category: str = "LA",
    images: tuple[str, ...] = ("LAABCD-02.jpg", "LAABCD-07.jpg"),
    main: str | None = "LAABCD-07.jpg",
    archived: bool = False,
    name: str = "Schild",
) -> FakeProduct:
    return FakeProduct(
        article_number=article,
        name=name,
        category=category,
        archived=archived,
        stock={"lager": 2, "verkaufsschrank": 1},
        images=tuple(
            FakeImage(name=image, is_main=image == main, order=index)
            for index, image in enumerate(images)
        ),
    )


def _facade(product: FakeProduct | None = None, *, roles: set[str] | None = None) -> FakeFacade:
    products = [product or _product()]
    image_data: dict[tuple[str, str], bytes] = {
        (item.article_number, image.name): f"bild-{image.name}".encode()
        for item in products
        for image in item.images
    }
    return FakeFacade(products, roles=roles, image_data=image_data)


def _service(facade: FakeFacade, fake: FakeGitHub, **overrides: Any) -> PublishService:
    return PublishService(
        facade,
        _settings(**overrides),
        client_factory=lambda _settings: fake,
    )


async def _call(
    hass: HomeAssistant, feature: str, payload: dict[str, Any], facade: FakeFacade
) -> fake_base.CallResult:
    return await fake_base.FakeBase(hass).call(feature, payload, facade=facade)


async def _artifacts(facade: FakeFacade) -> dict[str, bytes]:
    """Veröffentlichungssatz, wie ihn der Service aus der Fassade baut."""

    products = await facade.async_products()
    bundle = await facade.async_build_export()
    return build_artifacts(bundle, products)


def _remote_matching(artifacts: dict[str, bytes]) -> dict[str, str]:
    """Remote-Dateibaum, der exakt dem lokalen Veröffentlichungssatz entspricht."""

    return {path: blob_sha(data) for path, data in artifacts.items()}


async def _setup_entry(hass: HomeAssistant, **options: Any) -> MockConfigEntry:
    mock_integration(hass, MockModule(BASE_DOMAIN), built_in=False)
    hass.config.components.add(BASE_DOMAIN)
    entry = MockConfigEntry(domain=DOMAIN, data={}, options=dict(options))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


# --------------------------------------------------------------------------- Plan


async def test_dry_run_reports_changes_without_any_write() -> None:
    facade = _facade()
    fake = FakeGitHub()
    service = _service(facade, fake)
    before = facade.state_hash()

    result = await service.async_publish("dev", dry_run=True)

    assert facade.state_hash() == before, "Dry-Run darf den Store nicht verändern"
    assert facade.batches == []
    assert fake.commits == []
    assert fake.dispatches == []
    assert result["dry_run"] is True
    assert result["commit"] is None
    assert result["changed"] is False
    assert result["deployment"]["triggered"] is False
    assert result["deployment"]["would_trigger"] is True
    assert result["counts"]["added"] == 3  # Katalog + zwei Bilder
    assert result["products"]["added"] == 1
    assert result["images"]["added"] == 2
    assert "Dry-Run" in result["summary"]
    assert "nichts geschrieben" in result["summary"]
    catalog_file = next(item for item in result["files"] if item["path"] == CATALOG_PATH)
    assert catalog_file["status"] == "added"
    assert catalog_file["kind"] == "catalog"
    image_files = [item for item in result["files"] if item["kind"] == "image"]
    assert [item["articleNumber"] for item in image_files] == ["LAABCD", "LAABCD"]


async def test_dry_run_with_identical_remote_reports_unchanged_products() -> None:
    """Regression #289: byte-identischer Katalog ⇒ alle Produkte unverändert."""

    facade = _facade()
    artifacts = await _artifacts(facade)
    fake = FakeGitHub(remote=_remote_matching(artifacts))
    service = _service(facade, fake)

    result = await service.async_publish("dev", dry_run=True)

    assert fake.commits == []
    assert fake.reads == [], "unveränderter Katalog darf nicht gelesen werden"
    total_files = len(artifacts)
    assert result["counts"] == {
        "added": 0,
        "changed": 0,
        "removed": 0,
        "unchanged": total_files,
        "total": total_files,
    }
    product_total = len(json.loads(artifacts[CATALOG_PATH])["products"])
    assert result["products"] == {
        "added": 0,
        "changed": 0,
        "removed": 0,
        "unchanged": product_total,
        "total": product_total,
    }
    assert {item["status"] for item in result["files"]} == {"unchanged"}
    assert result["deployment"]["would_trigger"] is False


async def test_dry_run_with_changed_catalog_reports_changed_products() -> None:
    facade = _facade()
    artifacts = await _artifacts(facade)
    remote_catalog = json.loads(artifacts[CATALOG_PATH])
    remote_catalog["products"][0]["name"] = "Geänderter Name"
    remote_bytes = json.dumps(remote_catalog, ensure_ascii=False, indent=2).encode("utf-8")
    remote = _remote_matching(artifacts) | {CATALOG_PATH: blob_sha(remote_bytes)}
    fake = FakeGitHub(remote=remote, files={CATALOG_PATH: remote_bytes})
    service = _service(facade, fake)

    result = await service.async_publish("dev", dry_run=True)

    assert fake.reads == [CATALOG_PATH]
    product_total = len(remote_catalog["products"])
    assert result["products"] == {
        "added": 0,
        "changed": product_total,
        "removed": 0,
        "unchanged": 0,
        "total": product_total,
    }
    assert result["counts"]["changed"] == 1
    assert result["counts"]["unchanged"] == len(artifacts) - 1


def test_unchanged_product_counts_matches_catalog_structure() -> None:
    empty = json.dumps({"products": []}).encode("utf-8")
    assert unchanged_product_counts(empty) == {
        "added": 0,
        "changed": 0,
        "removed": 0,
        "unchanged": 0,
        "total": 0,
    }

    catalog = {"products": [{"articleNumber": "LAABCD"}, {"articleNumber": "WO0001"}]}
    expected = len(catalog["products"])
    assert unchanged_product_counts(json.dumps(catalog).encode("utf-8")) == {
        "added": 0,
        "changed": 0,
        "removed": 0,
        "unchanged": expected,
        "total": expected,
    }


async def test_publish_dev_commits_once_and_is_idempotent() -> None:
    facade = _facade()
    fake = FakeGitHub()
    service = _service(facade, fake)

    first = await service.async_publish("dev", message="chore(catalog): test (#289)")
    assert first["changed"] is True
    assert first["commit"] == "commit-1"
    assert first["deployment"]["triggered"] is True
    assert first["deployment"]["mode"] == "push"
    assert first["deployment"]["workflow"] == "fd-deploy-dev.yml"
    assert fake.dispatches == []
    commit = fake.commits[0]
    assert commit["head_sha"] == "head-sha"
    assert commit["tree_sha"] == "base-tree"
    assert commit["message"] == "chore(catalog): test (#289)"
    assert CATALOG_PATH in commit["artifacts"]

    second = await service.async_publish("dev")
    assert second["changed"] is False
    assert second["commit"] is None
    assert second["deployment"]["triggered"] is False
    assert second["deployment"]["would_trigger"] is False
    assert len(fake.commits) == 1, "unveränderter Stand darf keinen zweiten Commit erzeugen"
    assert "Keine Änderungen" in second["summary"]


async def test_publish_prod_commits_to_dev_then_dispatches_deploy_prd() -> None:
    facade = _facade()
    fake = FakeGitHub()
    service = _service(facade, fake)

    result = await service.async_publish("prod")

    assert result["target"] == "prod"
    assert result["branch"] == "dev", "main wird nur über fd-deploy-prd aktualisiert"
    assert result["deployment"]["triggered"] is True
    assert result["deployment"]["mode"] == "workflow_dispatch"
    assert fake.dispatches == [
        {"workflow": "fd-deploy-prd.yml", "ref": "main", "inputs": {"dry_run": False}}
    ]
    assert len(fake.commits) == 1
    assert "fd-deploy-prd.yml wurde ausgelöst" in result["summary"]

    again = await service.async_publish("prod")
    assert again["changed"] is False
    assert len(fake.dispatches) == 2, "main ist im Fake weiterhin leer: erneut Dispatch"


async def test_publish_prod_dry_run_compares_against_the_prod_branch() -> None:
    """Regression #289: prod vergleicht gegen main, committet aber auf dev."""

    facade = _facade()
    artifacts = await _artifacts(facade)
    # dev ist aktuell, main kennt die Produkte noch nicht.
    fake = FakeGitHub(remote=_remote_matching(artifacts), prod_remote={})
    service = _service(facade, fake)

    result = await service.async_publish("prod", dry_run=True)

    assert fake.commits == []
    assert fake.dispatches == []
    assert fake.tree_reads == ["main", "dev"], "dev-Baum genau einmal als Commit-Basis"
    assert result["compare_branch"] == "main"
    assert result["commit_branch"] == "dev"
    assert result["counts"]["added"] == len(artifacts)
    assert result["products"]["added"] == 1
    assert result["products"]["unchanged"] == 0
    assert fake.read_branches == [(CATALOG_PATH, "main")]
    assert "Vergleichsbasis: main" in result["summary"]
    assert result["deployment"]["triggered"] is False
    assert result["deployment"]["would_trigger"] is True


async def test_publish_prod_with_current_dev_skips_commit_but_dispatches() -> None:
    facade = _facade()
    artifacts = await _artifacts(facade)
    fake = FakeGitHub(remote=_remote_matching(artifacts), prod_remote={})
    service = _service(facade, fake)

    result = await service.async_publish("prod")

    assert fake.commits == [], "dev ist bereits aktuell: kein Commit"
    assert fake.remote == _remote_matching(artifacts), "dev-Ref bleibt unverändert"
    assert result["commit"] is None
    assert result["changed"] is False
    assert result["deployment"]["would_trigger"] is True
    assert result["deployment"]["triggered"] is True
    assert len(fake.dispatches) == 1
    assert fake.dispatches[0]["ref"] == "main"
    assert result["counts"]["added"] == len(artifacts), "Bericht zeigt das Prod-Delta"
    assert result["products"]["added"] == 1
    assert "Keine Dateiänderungen auf dev" in result["summary"]
    assert "Vergleichsbasis: main" in result["summary"]
    assert "fd-deploy-prd.yml wurde ausgelöst" in result["summary"]


async def test_publish_prod_dry_run_without_changes_reports_no_deployment() -> None:
    """Regression #289: gleicher Stand wie main ⇒ kein Deployment im Plan."""

    facade = _facade()
    artifacts = await _artifacts(facade)
    matching = _remote_matching(artifacts)
    fake = FakeGitHub(remote=matching, prod_remote=matching)
    service = _service(facade, fake)

    result = await service.async_publish("prod", dry_run=True)

    assert result["counts"] == {
        "added": 0,
        "changed": 0,
        "removed": 0,
        "unchanged": len(artifacts),
        "total": len(artifacts),
    }
    assert result["deployment"]["would_trigger"] is False
    assert result["deployment"]["triggered"] is False
    assert fake.commits == []
    assert fake.dispatches == []
    assert "würde nicht ausgelöst" in result["summary"]
    assert "würde zuerst ein Commit" not in result["summary"]


async def test_publish_prod_without_changes_skips_commit_and_dispatch() -> None:
    """Regression #289: ohne Änderung kein Commit und kein Workflow-Lauf."""

    facade = _facade()
    artifacts = await _artifacts(facade)
    matching = _remote_matching(artifacts)
    fake = FakeGitHub(remote=matching, prod_remote=matching)
    service = _service(facade, fake)

    result = await service.async_publish("prod")

    assert fake.commits == []
    assert fake.dispatches == [], "kein actions/workflows/.../dispatches ohne Änderung"
    assert result["commit"] is None
    assert result["changed"] is False
    assert result["deployment"]["triggered"] is False
    assert result["deployment"]["would_trigger"] is False
    assert "Keine Änderungen: nichts committet, kein Deployment ausgelöst." in result["summary"]
    assert "fd-deploy-prd.yml" not in result["summary"]


async def test_publish_prod_commits_on_dev_when_dev_also_lacks_content() -> None:
    facade = _facade()
    fake = FakeGitHub(remote={}, prod_remote={})
    service = _service(facade, fake)

    result = await service.async_publish("prod")

    assert len(fake.commits) == 1
    commit = fake.commits[0]
    assert commit["head_sha"] == "head-sha", "Commit basiert auf dev, nicht auf main"
    assert commit["tree_sha"] == "base-tree"
    assert result["commit"] == "commit-1"
    assert result["changed"] is True
    assert [dispatch["ref"] for dispatch in fake.dispatches] == ["main"]
    assert result["counts"]["added"] == 3
    assert result["products"]["added"] == 1
    assert "Commit auf dev gepusht" in result["summary"]
    assert "Vergleichsbasis: main" in result["summary"]


async def test_publish_dev_still_compares_against_dev_only() -> None:
    facade = _facade()
    artifacts = await _artifacts(facade)
    # Prod weicht ab: ein dev-Lauf darf main trotzdem nicht lesen.
    fake = FakeGitHub(remote=_remote_matching(artifacts), prod_remote={})
    service = _service(facade, fake)

    result = await service.async_publish("dev", dry_run=True)

    assert fake.tree_reads == ["dev"]
    assert result["compare_branch"] == "dev"
    assert result["commit_branch"] == "dev"
    assert result["counts"]["added"] == 0
    assert result["products"]["unchanged"] == 1
    assert "Vergleichsbasis" not in result["summary"]


async def test_publish_reports_removed_images() -> None:
    facade = _facade()
    stale = "src/assets/handmade/LA/LAOLT1/LAOLT1-01.jpg"
    fake = FakeGitHub(remote={stale: "deadbeef"})
    service = _service(facade, fake)

    result = await service.async_publish("dev", dry_run=True)

    removed = [item for item in result["files"] if item["status"] == "removed"]
    assert [item["path"] for item in removed] == [stale]
    assert result["counts"]["removed"] == 1


# --------------------------------------------------------------------------- Schutz


async def test_empty_catalog_blocks_publish_with_german_message() -> None:
    facade = FakeFacade([])
    fake = FakeGitHub(remote={CATALOG_PATH: "alter-stand"})
    service = _service(facade, fake)

    with pytest.raises(SyncError) as err:
        await service.async_publish("dev")

    assert err.value.code == "publish_empty_catalog"
    assert err.value.status == 409
    assert "keine Produkte" in err.value.message
    assert "löschen" in err.value.message
    assert fake.commits == []


async def test_catalog_with_only_archived_products_is_blocked() -> None:
    facade = _facade(_product(archived=True))
    fake = FakeGitHub(remote={CATALOG_PATH: "alter-stand"})
    service = _service(facade, fake)

    with pytest.raises(SyncError) as err:
        await service.async_publish("dev")

    assert err.value.code == "publish_empty_catalog"
    assert fake.commits == []


async def test_empty_catalog_dry_run_previews_with_warning() -> None:
    facade = FakeFacade([])
    fake = FakeGitHub(remote={CATALOG_PATH: "alter-stand"})
    service = _service(facade, fake)

    result = await service.async_publish("dev", dry_run=True)

    assert result["dry_run"] is True
    assert result["commit"] is None
    assert result["products"]["total"] == 0
    assert result["warning"], "Dry-Run muss den leeren Katalog ankündigen"
    assert "keine Produkte" in result["warning"]
    assert "Hinweis:" in result["summary"]
    assert fake.commits == []


async def test_empty_catalog_allow_empty_publishes_the_lock_away() -> None:
    facade = FakeFacade([])
    stale = "src/assets/handmade/LA/LAOLT1/LAOLT1-01.jpg"
    fake = FakeGitHub(remote={CATALOG_PATH: "alter-stand", stale: "altes-bild"})
    service = _service(facade, fake)

    result = await service.async_publish("dev", allow_empty=True)

    assert result["changed"] is True
    assert len(fake.commits) == 1
    removed = {change.path for change in fake.commits[0]["changes"] if change.status == "removed"}
    assert stale in removed
    assert result["warning"] and "allow_empty" in result["warning"]


async def test_non_empty_catalog_has_no_warning() -> None:
    facade = _facade()
    service = _service(facade, FakeGitHub())

    result = await service.async_publish("dev", dry_run=True)

    assert result["warning"] is None


async def test_removal_guard_blocks_bulk_deletion_and_needs_confirmation() -> None:
    facade = _facade()
    remote = {CATALOG_PATH: "alter-katalog"}
    for index in range(1, 21):
        article = f"LAOL{index:02d}"
        remote[f"{IMAGES_PATH}/LA/{article}/{article}-01.jpg"] = f"alt-{index}"
    fake = FakeGitHub(remote=remote)
    service = _service(facade, fake)

    preview = await service.async_publish("dev", dry_run=True)
    assert preview["removal_guard"]["triggered"] is True
    assert "löschen" in preview["warning"]
    assert preview["counts"]["removed"] == 20

    with pytest.raises(SyncError) as blocked:
        await service.async_publish("dev")
    assert blocked.value.code == "publish_removals_blocked"
    assert blocked.value.status == 409
    assert "20 von 20" in blocked.value.message
    assert fake.commits == []

    confirmed = await service.async_publish("dev", confirm_removals=True)
    assert confirmed["removal_guard"] == {
        "triggered": True,
        "removed": 20,
        "existing": 20,
        "maxFiles": 10,
        "share": 3,
        "confirmed": True,
    }
    assert confirmed["changed"] is True
    assert len(fake.commits) == 1


async def test_removal_guard_ignores_small_targeted_deletions() -> None:
    facade = _facade()
    remote = {
        CATALOG_PATH: "alter-katalog",
        f"{IMAGES_PATH}/LA/LAOL01/LAOL01-01.jpg": "alt-1",
        f"{IMAGES_PATH}/LA/LAOL02/LAOL02-01.jpg": "alt-2",
    }
    service = _service(facade, FakeGitHub(remote=remote))

    result = await service.async_publish("dev")

    assert result["removal_guard"]["triggered"] is False
    assert result["changed"] is True
    assert result["counts"]["removed"] == 2
    assert result["warning"] is None


def test_publish_removal_guard_thresholds() -> None:
    assert publish_removal_guard(0, 100) is False
    assert publish_removal_guard(2, 5) is False
    assert publish_removal_guard(10, 500) is False
    assert publish_removal_guard(11, 500) is True
    assert publish_removal_guard(5, 12) is True
    assert publish_removal_guard(3, 100) is False
    assert publish_removal_guard(3, 3) is True


# --------------------------------------------------------------------------- Bilder


async def test_image_names_stay_stable_across_publications() -> None:
    facade = _facade()
    fake = FakeGitHub()
    service = _service(facade, fake)
    prefix = f"{IMAGES_PATH}/LA/LAABCD/"

    first = await service.async_publish("dev")
    assert first["changed"] is True
    images = {path.removeprefix(prefix) for path in fake.remote if path.startswith(prefix)}
    assert images == {"LAABCD-01.jpg", "LAABCD-02.jpg"}
    # Das Hauptbild bekommt -01, das zweite Bild behält seine eigene Nummer.
    assert first["images"]["total"] == 2

    second = await service.async_publish("dev")
    assert second["changed"] is False
    assert len(fake.commits) == 1, "stabile Namen dürfen keine Umbenennung erzeugen"


async def test_missing_image_file_is_a_structured_error() -> None:
    product = _product()
    facade = FakeFacade([product], image_data={}, roles={"app_admin", "product"})
    service = _service(facade, FakeGitHub())

    with pytest.raises(SyncError) as err:
        await service.async_publish("dev", dry_run=True)

    assert err.value.code == "publish_artifacts_unavailable"
    assert err.value.status == 409
    assert product.article_number in err.value.message


async def test_build_artifacts_keeps_images_with_their_own_product() -> None:
    shared = b"dasselbe-foto"
    facade = FakeFacade(
        [
            _product("3DHP42", category="3D", images=("3DHP42-01.jpg",), main="3DHP42-01.jpg"),
            _product("LA6BPS", category="LA", images=("LA6BPS-01.jpg",), main="LA6BPS-01.jpg"),
        ],
        image_data={
            ("3DHP42", "3DHP42-01.jpg"): shared,
            ("LA6BPS", "LA6BPS-01.jpg"): shared,
        },
    )
    bundle = await facade.async_build_export()
    products = await facade.async_products()

    artifacts = build_artifacts(bundle, products)

    assert artifacts[f"{IMAGES_PATH}/3D/3DHP42/3DHP42-01.jpg"] == shared
    assert artifacts[f"{IMAGES_PATH}/LA/LA6BPS/LA6BPS-01.jpg"] == shared


async def test_incident_leaves_3d_model_sources_untouched() -> None:
    """Regression d13cb2f: der Publish löschte ``3dmodels/**`` (#289)."""

    facade = FakeFacade(
        [
            _product("3DHP42", category="3D", images=("3DHP42-01.jpg",), main="3DHP42-01.jpg"),
            _product("LA6BPS", category="LA", images=("LA6BPS-01.jpg",), main="LA6BPS-01.jpg"),
        ],
        image_data={
            ("3DHP42", "3DHP42-01.jpg"): b"gemeinsames-foto",
            ("LA6BPS", "LA6BPS-01.jpg"): b"gemeinsames-foto",
        },
    )
    bundle = await facade.async_build_export()
    products = await facade.async_products()
    artifacts = build_artifacts(bundle, products)

    remote = {
        CATALOG_PATH: "alter-katalog",
        f"{IMAGES_PATH}/3dmodels/includes/roundedcube.scad": "scad-blob",
        f"{IMAGES_PATH}/3dmodels/models/Verteilerdose.scad": "scad-blob-2",
    }
    for path, content in artifacts.items():
        if path != CATALOG_PATH:
            remote[path] = blob_sha(content)

    changes = diff_artifacts(artifacts, remote)

    assert [change.path for change in changes if change.status == "removed"] == []
    assert all("3dmodels" not in change.path for change in changes)
    assert [change.path for change in changes if change.status == "changed"] == [CATALOG_PATH]


@pytest.mark.parametrize(
    ("path", "managed"),
    [
        (CATALOG_PATH, True),
        (f"{IMAGES_PATH}/LA/LAKBCD/LAKBCD-01.jpg", True),
        (f"{IMAGES_PATH}/3D/3DHP42/3DHP42-01.jpg", True),
        (f"{IMAGES_PATH}/LA/LA7SQL/LA7SQL-01.jpeg", True),
        (f"{IMAGES_PATH}/LA/LAX/LAX-01.jpg", True),
        (f"{IMAGES_PATH}/3dmodels/models/Verteilerdose.scad", False),
        (f"{IMAGES_PATH}/.gitkeep", False),
        (f"{IMAGES_PATH}/README.md", False),
        (f"{IMAGES_PATH}/LA/LAKBCD/sub/LAKBCD-01.jpg", False),
        (f"{IMAGES_PATH}/LA/OTHER/OTHER-01.jpg", False),
        (f"{IMAGES_PATH}/XX/XXABCD/XXABCD-01.jpg", False),
        (f"{IMAGES_PATH}/LA/LAKBCD/LAKBCD-1.jpg", False),
        (f"{IMAGES_PATH}/LA/LAKBCD/LAKBCD-01.txt", False),
        ("public/data/other.json", False),
    ],
)
def test_is_managed_path_only_covers_catalog_and_product_images(path: str, managed: bool) -> None:
    from custom_components.frickeldave_store_syncfd.publish import _is_managed_path

    assert _is_managed_path(path) is managed


# --------------------------------------------------------------------------- GitHub


class FakeHTTPResponse:
    """aiohttp-Antwort mit echtem Body; ``content_length=None`` = chunked."""

    def __init__(
        self, status: int, payload: Any = None, *, content_length: int | None = None
    ) -> None:
        self.status = status
        self._payload = payload
        self.content_length = content_length

    async def read(self) -> bytes:
        if isinstance(self._payload, (bytes, bytearray)):
            return bytes(self._payload)
        if self._payload is None:
            return b""
        return json.dumps(self._payload).encode("utf-8")

    async def __aenter__(self) -> FakeHTTPResponse:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False


class FakeHTTPSession:
    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def _next(self, method: str, url: str, **kwargs: Any) -> FakeHTTPResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def request(self, method: str, url: str, **kwargs: Any) -> FakeHTTPResponse:
        return self._next(method, url, **kwargs)

    def get(self, url: str, **kwargs: Any) -> FakeHTTPResponse:
        return self._next("GET", url, **kwargs)

    async def close(self) -> None:
        return None


def _client(
    responses: list[Any], *, retry_delays: tuple[float, ...] = (0.0, 0.0)
) -> tuple[GitHubClient, FakeHTTPSession]:
    session = FakeHTTPSession(responses)
    # Nullverzögerungen: Tests dürfen für den Retry nicht schlafen.
    client = GitHubClient(
        _settings(),
        session=session,
        retry_delays=retry_delays,  # type: ignore[arg-type]
    )
    return client, session


async def test_github_client_commit_uses_base_tree_and_keeps_other_files() -> None:
    client, session = _client(
        [
            FakeHTTPResponse(201, {"sha": "blob-new"}),
            FakeHTTPResponse(201, {"sha": "blob-new-2"}),
            FakeHTTPResponse(201, {"sha": "tree-new"}),
            FakeHTTPResponse(201, {"sha": "commit-new"}),
            FakeHTTPResponse(200, {"ref": "refs/heads/dev"}),
        ]
    )
    changes = [
        FileChange(CATALOG_PATH, "changed", "catalog"),
        FileChange(f"{IMAGES_PATH}/LA/LA0001/LA0001-01.jpg", "added", "image", "LA0001"),
        FileChange(f"{IMAGES_PATH}/LA/LA0002/LA0002-01.jpg", "removed", "image", "LA0002"),
        FileChange(f"{IMAGES_PATH}/LA/LA0003/LA0003-01.jpg", "unchanged", "image", "LA0003"),
    ]
    artifacts = {
        CATALOG_PATH: b"{}",
        f"{IMAGES_PATH}/LA/LA0001/LA0001-01.jpg": b"bild",
    }

    commit = await client.async_commit(
        artifacts, changes, head_sha="head", tree_sha="base-tree", message="test"
    )

    assert commit == "commit-new"
    blobs = [call for call in session.calls if call["url"].endswith("/git/blobs")]
    assert len(blobs) == 2, "nur geänderte und neue Dateien werden hochgeladen"
    assert blobs[0]["json"]["encoding"] == "base64"
    assert base64.b64decode(blobs[0]["json"]["content"]) == b"{}"

    tree_call = next(call for call in session.calls if call["url"].endswith("/git/trees"))
    assert tree_call["json"]["base_tree"] == "base-tree"
    entries = {entry["path"]: entry for entry in tree_call["json"]["tree"]}
    assert entries[f"{IMAGES_PATH}/LA/LA0002/LA0002-01.jpg"]["sha"] is None
    assert f"{IMAGES_PATH}/LA/LA0003/LA0003-01.jpg" not in entries

    commit_call = next(call for call in session.calls if call["url"].endswith("/git/commits"))
    assert commit_call["json"] == {"message": "test", "tree": "tree-new", "parents": ["head"]}
    ref_call = session.calls[-1]
    assert ref_call["method"] == "PATCH"
    assert ref_call["url"].endswith("/git/refs/heads/dev")
    assert ref_call["json"] == {"sha": "commit-new", "force": False}
    assert session.calls[0]["headers"]["Authorization"] == f"Bearer {TOKEN}"


async def test_github_chunked_json_without_content_length_is_read() -> None:
    """Regression #289: ``content_length`` ist bei chunked Antworten ``None``."""

    response = FakeHTTPResponse(200, {"object": {"sha": "head-1"}})
    assert response.content_length is None, "Fixture muss chunked abbilden"
    client, _ = _client([response])

    body = await client._async_json("GET", "https://api.github.com/repos/o/r/git/ref/heads/dev")

    assert body == {"object": {"sha": "head-1"}}


async def test_github_client_reads_the_requested_branch() -> None:
    """Regression #289: der Branch-Parameter landet in der GitHub-URL."""

    client, session = _client(
        [
            FakeHTTPResponse(200, b"katalog"),
            FakeHTTPResponse(200, {"object": {"sha": "main-head"}}),
            FakeHTTPResponse(200, {"tree": {"sha": "main-tree"}}),
            FakeHTTPResponse(
                200,
                {
                    "tree": [{"path": CATALOG_PATH, "type": "blob", "sha": "x"}],
                    "truncated": False,
                },
            ),
        ]
    )
    base = "https://api.github.com/repos/Frickeldave/frickeldave.github.io"

    assert await client.async_read_file(CATALOG_PATH, "main") == b"katalog"
    tree = await client.async_read_tree("main")

    assert tree.head_sha == "main-head"
    assert [call["url"] for call in session.calls] == [
        f"{base}/contents/{CATALOG_PATH}?ref=main",
        f"{base}/git/ref/heads/main",
        f"{base}/git/commits/main-head",
        f"{base}/git/trees/main-tree?recursive=1",
    ]


async def test_dry_run_reads_chunked_github_responses_end_to_end() -> None:
    facade = _facade()
    remote_catalog = json.dumps({"products": []}).encode("utf-8")
    session = FakeHTTPSession(
        [
            FakeHTTPResponse(200, {"object": {"sha": "head-1"}}),
            FakeHTTPResponse(200, {"tree": {"sha": "tree-1"}}),
            FakeHTTPResponse(
                200,
                {
                    "tree": [{"path": CATALOG_PATH, "type": "blob", "sha": "anderer-stand"}],
                    "truncated": False,
                },
            ),
            FakeHTTPResponse(200, remote_catalog),
        ]
    )
    service = PublishService(
        facade,
        _settings(),
        client_factory=lambda settings: GitHubClient(settings, session=session),  # type: ignore[arg-type]
    )

    result = await service.async_publish("dev", dry_run=True)

    assert result["dry_run"] is True
    assert result["commit"] is None
    assert result["counts"]["total"] == 3
    assert result["products"]["added"] == 1


@pytest.mark.parametrize(
    ("status", "body", "code", "fragment"),
    [
        (401, {"message": "Bad credentials"}, "github_unauthorized", "abgelehnt"),
        (404, {"message": "Not Found"}, "github_not_found", "nicht gefunden"),
        (409, {"message": "Conflict"}, "github_conflict", "erneut starten"),
        (502, b"<html>502</html>", "github_unavailable", "nicht erreichbar"),
    ],
)
async def test_github_errors_map_to_german_codes(
    status: int, body: Any, code: str, fragment: str
) -> None:
    attempts = RETRY_ATTEMPTS if status in RETRY_STATUSES else 1
    client, _ = _client([FakeHTTPResponse(status, body) for _ in range(attempts)])

    with pytest.raises(SyncError) as err:
        await client.async_read_tree()

    assert err.value.code == code
    assert fragment in err.value.message


async def test_dispatch_uses_the_dispatches_endpoint_for_prod() -> None:
    """Regression #289: ``/run`` existiert nicht; GitHub erwartet ``/dispatches``."""

    client, session = _client([FakeHTTPResponse(204, None)])

    await client.async_dispatch_workflow("fd-deploy-prd.yml", ref="main", inputs={"dry_run": False})

    base = "https://api.github.com/repos/Frickeldave/frickeldave.github.io"
    assert len(session.calls) == 1, "genau ein Dispatch, kein zusätzlicher Request"
    call = session.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == f"{base}/actions/workflows/fd-deploy-prd.yml/dispatches"
    assert call["json"] == {"ref": "main", "inputs": {"dry_run": False}}
    assert "/run" not in call["url"]


async def test_dispatch_404_names_workflow_branch_and_actions_permission() -> None:
    session = FakeHTTPSession([FakeHTTPResponse(404, {"message": "Not Found"})])
    client = GitHubClient(
        _settings(prod_workflow="release-prd.yml", prod_branch="production"),
        session=session,  # type: ignore[arg-type]
        retry_delays=(0.0,),
    )

    with pytest.raises(SyncError) as err:
        await client.async_dispatch_workflow(
            "release-prd.yml", ref="production", inputs={"dry_run": False}
        )

    assert err.value.code == "github_not_found"
    message = err.value.message
    assert "release-prd.yml" in message
    assert "production" in message
    assert "Actions: Read and write" in message
    assert "Workflow-Name und Branch prüfen" in message
    assert "fd-deploy-prd.yml" not in message
    assert TOKEN not in message
    # Der aufgerufene Pfad ist der Dispatch-Endpoint, nicht das alte ``/run``.
    assert session.calls[0]["url"].endswith("/actions/workflows/release-prd.yml/dispatches")


async def test_ref_404_names_repository_and_branch_from_path() -> None:
    client, _ = _client([FakeHTTPResponse(404, {"message": "Not Found"})])

    with pytest.raises(SyncError) as err:
        await client.async_read_tree("release/2026")

    assert err.value.code == "github_not_found"
    message = err.value.message
    assert "Frickeldave/frickeldave.github.io" in message
    assert "release/2026" in message
    assert "Contents: Read and write" in message
    assert TOKEN not in message


async def test_blob_404_keeps_the_generic_repository_message() -> None:
    client, _ = _client([FakeHTTPResponse(404, {"message": "Not Found"})])

    with pytest.raises(SyncError) as err:
        await client.async_commit(
            {CATALOG_PATH: b"{}"},
            [FileChange(CATALOG_PATH, "changed", "catalog")],
            head_sha="head",
            tree_sha="base-tree",
            message="test",
        )

    assert err.value.code == "github_not_found"
    assert err.value.message == (
        "Repository „Frickeldave/frickeldave.github.io“ oder Branch nicht gefunden. "
        "Bitte Repository und Token prüfen."
    )


async def test_github_html_success_is_structured_not_a_crash() -> None:
    client, _ = _client([FakeHTTPResponse(200, b"<html>Captive Portal</html>")])

    with pytest.raises(SyncError) as err:
        await client.async_read_tree()

    assert err.value.code == "github_unavailable"
    assert "Störungsseite" in err.value.message


async def test_github_empty_response_is_structured_not_attributeerror() -> None:
    client, _ = _client([FakeHTTPResponse(200, None)])

    with pytest.raises(SyncError) as err:
        await client.async_read_tree()

    assert err.value.code == "github_failed"
    assert "erwarteten Format" in err.value.message


async def test_commit_rejects_change_without_artifact() -> None:
    client, _ = _client([])

    with pytest.raises(SyncError) as err:
        await client.async_commit(
            {},
            [FileChange(CATALOG_PATH, "changed", "catalog")],
            head_sha="head",
            tree_sha="base",
            message="test",
        )

    assert err.value.code == "publish_artifacts_unavailable"


def _read_tree_responses() -> list[FakeHTTPResponse]:
    return [
        FakeHTTPResponse(200, {"object": {"sha": "head-1"}}),
        FakeHTTPResponse(200, {"tree": {"sha": "tree-1"}}),
        FakeHTTPResponse(200, {"tree": [], "truncated": False}),
    ]


async def test_transient_5xx_is_retried_until_success() -> None:
    """Regression #289: zwei 503 hintereinander dürfen die Veröffentlichung nicht kippen."""

    client, session = _client(
        [
            FakeHTTPResponse(503, {"message": "Service Unavailable"}),
            FakeHTTPResponse(503, {"message": "Service Unavailable"}),
            *_read_tree_responses(),
        ]
    )

    tree = await client.async_read_tree()

    assert tree.head_sha == "head-1"
    assert [call["url"].rsplit("/", 1)[-1] for call in session.calls[:3]] == ["dev"] * 3
    assert len(session.calls) == 5


async def test_persistent_5xx_reports_status_and_github_message() -> None:
    client, session = _client(
        [FakeHTTPResponse(503, {"message": "Service Unavailable"}) for _ in range(RETRY_ATTEMPTS)]
    )

    with pytest.raises(SyncError) as err:
        await client.async_read_tree()

    assert err.value.code == "github_unavailable"
    assert "HTTP 503: Service Unavailable" in err.value.message
    assert "Bitte später erneut versuchen." in err.value.message
    assert len(session.calls) == RETRY_ATTEMPTS, "genau drei Versuche insgesamt"


async def test_transport_error_is_retried() -> None:
    client, session = _client([aiohttp.ClientError("Verbindung verloren"), *_read_tree_responses()])

    tree = await client.async_read_tree()

    assert tree.head_sha == "head-1"
    assert len(session.calls) == 4


async def test_ref_update_is_not_retried_on_5xx() -> None:
    """Der Ref-Update ist nicht idempotent: genau ein PATCH, kein Retry."""

    client, session = _client(
        [
            FakeHTTPResponse(201, {"sha": "blob-new"}),
            FakeHTTPResponse(201, {"sha": "tree-new"}),
            FakeHTTPResponse(201, {"sha": "commit-new"}),
            FakeHTTPResponse(500, {"message": "Internal Server Error"}),
        ]
    )

    with pytest.raises(SyncError) as err:
        await client.async_commit(
            {CATALOG_PATH: b"{}"},
            [FileChange(CATALOG_PATH, "changed", "catalog")],
            head_sha="head",
            tree_sha="base",
            message="test",
        )

    assert err.value.code == "github_unavailable"
    assert "HTTP 500: Internal Server Error" in err.value.message
    patches = [call for call in session.calls if call["method"] == "PATCH"]
    assert len(patches) == 1
    assert len(session.calls) == 4


async def test_5xx_message_is_sanitized_to_one_line_without_token() -> None:
    noisy = "Server   Error\n\n  bitte warten  " + "ü" * 400
    client, _ = _client([FakeHTTPResponse(503, {"message": noisy}) for _ in range(RETRY_ATTEMPTS)])

    with pytest.raises(SyncError) as err:
        await client.async_read_tree()

    message = err.value.message
    assert message.startswith("GitHub ist momentan nicht erreichbar (HTTP 503: Server Error")
    assert "\n" not in message
    # Detail hoechstens MAX_ERROR_DETAIL_LENGTH (200) Zeichen, alles in einer Zeile.
    detail = message.split("HTTP 503: ", 1)[1].split("). Bitte", 1)[0]
    assert len(detail) == 200
    assert TOKEN not in message


async def test_5xx_without_message_reports_status_only() -> None:
    client, _ = _client(
        [FakeHTTPResponse(504, b"<html>Gateway Timeout</html>") for _ in range(RETRY_ATTEMPTS)]
    )

    with pytest.raises(SyncError) as err:
        await client.async_read_tree()

    assert "(HTTP 504)" in err.value.message


# --------------------------------------------------------------------------- Feature


async def test_publish_without_token_is_unavailable(hass: HomeAssistant) -> None:
    await _setup_entry(hass, token="")
    result = await _call(hass, "publish", {"target": "dev"}, _facade())

    assert result.status == 409
    assert result.error["code"] == "feature_unavailable"
    assert "kein GitHub-Token" in result.error["message"]


async def test_publish_feature_requires_app_admin(hass: HomeAssistant) -> None:
    await _setup_entry(hass, token=TOKEN)
    facade = _facade(roles={"product"})

    result = await _call(hass, "publish", {"target": "dev"}, facade)

    assert result.status == 403
    assert result.error["code"] == "forbidden"


@pytest.mark.parametrize(
    ("payload", "fragment"),
    [
        ({}, "target"),
        ({"target": "staging"}, "target"),
        ({"target": "dev", "dry_run": "ja"}, "dry_run"),
        ({"target": "dev", "allow_empty": 1}, "allow_empty"),
        ({"target": "dev", "confirm_removals": "ja"}, "confirm_removals"),
        ({"target": "dev", "message": 5}, "message"),
        ({"target": "dev", "unbekannt": True}, "Unbekannte Felder"),
    ],
)
async def test_publish_payload_validation_returns_400(
    hass: HomeAssistant, payload: dict[str, Any], fragment: str
) -> None:
    await _setup_entry(hass, token=TOKEN)

    result = await _call(hass, "publish", payload, _facade())

    assert result.status == 400
    assert result.error["code"] == "validation_error"
    assert fragment in result.error["message"]


def _api() -> Any:
    from custom_components.frickeldave_store_syncfd.base_api import api_from_module

    return api_from_module(fake_base.build_module())


def _features(fake: FakeGitHub) -> dict[str, Any]:
    from custom_components.frickeldave_store_syncfd.features import build_features

    return build_features(_api(), _settings, client_factory=lambda _settings: fake)


async def _invoke(fake: FakeGitHub, payload: dict[str, Any], facade: FakeFacade) -> Any:
    feature = _features(fake)["publish"]
    return await feature.handler(
        fake_base.FeatureRequest("publish", payload, "user-1", "Test", facade)
    )


async def test_publish_handler_returns_the_plan_and_no_token() -> None:
    facade = _facade()
    fake = FakeGitHub()

    response = await _invoke(fake, {"target": "dev", "dry_run": True}, facade)

    payload = dict(response.payload)
    assert response.status == 200
    assert payload["dry_run"] is True
    assert payload["counts"]["total"] == 3
    assert payload["repository"] == "Frickeldave/frickeldave.github.io"
    assert TOKEN not in json.dumps(payload)
    assert "token" not in json.dumps(payload)


async def test_publish_handler_maps_removal_guard_to_409() -> None:
    facade = _facade()
    remote = {CATALOG_PATH: "alter-katalog"}
    for index in range(1, 21):
        article = f"LAOL{index:02d}"
        remote[f"{IMAGES_PATH}/LA/{article}/{article}-01.jpg"] = f"alt-{index}"
    fake = FakeGitHub(remote=remote)

    with pytest.raises(fake_base.ExtensionError) as err:
        await _invoke(fake, {"target": "dev"}, facade)

    assert err.value.code == "publish_removals_blocked"
    assert err.value.status == 409
    assert fake.commits == []


async def test_publish_handler_without_token_raises_extension_error() -> None:
    from custom_components.frickeldave_store_syncfd.features import build_features

    api = _api()
    feature = build_features(
        api, lambda: SyncSettings.from_mapping({}), client_factory=lambda _s: FakeGitHub()
    )["publish"]

    with pytest.raises(fake_base.ExtensionError) as err:
        await feature.handler(
            fake_base.FeatureRequest("publish", {"target": "dev"}, "user", "Test", _facade())
        )

    assert err.value.code == "publish_token_missing"
    assert err.value.status == 400


async def test_publish_logs_without_the_token(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    await _setup_entry(hass, token=TOKEN)
    caplog.set_level(logging.DEBUG)
    facade = _facade()
    service = _service(facade, FakeGitHub())

    await service.async_publish("dev", dry_run=True)

    assert TOKEN not in caplog.text


# --------------------------------------------------------------------------- Kleinkram


def test_settings_use_the_frickeldave_defaults() -> None:
    settings = SyncSettings()

    assert settings.repository == "Frickeldave/frickeldave.github.io"
    assert settings.dev_branch == "dev"
    assert settings.prod_branch == "main"
    assert settings.prod_workflow == "fd-deploy-prd.yml"
    assert settings.catalog_url == "https://frickeldave.de/data/handmade.json"
    assert VERSION == "0.1.6"
