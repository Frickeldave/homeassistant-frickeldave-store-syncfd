# Frickeldave Store Sync

Diese HACS-Integration ist die frickeldave-spezifische Erweiterung des
Frickeldave Stores. Sie veröffentlicht den Katalog nach `dev` beziehungsweise
`prod` und importiert Produkte samt Bildern von der Website. Sie arbeitet
ausschließlich über den Erweiterungspunkt der Basis-Integration
`frickeldave_store` und enthält selbst keine Store-Daten, keine Bilder und keine
Dateipfade der Basis.

## Funktionsumfang (0.1.6)

| Fähigkeit | Rolle | Was sie tut |
| --- | --- | --- |
| `publish` | `app_admin` | Baut den Website-Export über die Fassade, vergleicht ihn mit dem Zielbranch und committet die Änderungen auf `dev`. `prod` committet ebenfalls auf `dev` und löst danach den Produktions-Workflow aus. Dry-Run liest nur. |
| `import` | `product` | Lädt den Website-Katalog und die Produktbilder, prüft Quellen gegen eine feste Allowlist und übergibt einen validierten Batch an die Basis. `overwrite` und Dry-Run wie bisher. Eigene Quellen nur `app_admin`. |

Beide Fähigkeiten registrieren sich beim Erweiterungspunkt der Basis
(Contract `1`) und werden über
`POST /api/frickeldave_store/features/{feature}` aufgerufen. Die Basis
authentifiziert, prüft `required_role` und routet; die fachliche Validierung
liegt hier.

**GitHub-Token:** Es wird ausschließlich im Options Flow dieser Integration
gespeichert und nur als `Authorization`-Header verwendet. Er wird nie
zurückgegeben, protokolliert oder exportiert; `token_set` ist das einzige
Kennzeichen nach außen. Ein leeres Token-Feld behält den gespeicherten Wert.

## Voraussetzung: Basis-Integration

Die Basis-Integration **`frickeldave_store`** ist Pflicht. Sie liefert Store,
Bilder, Rechte, Snapshots und die Fassade, gegen die diese Erweiterung arbeitet,
und muss zuerst installiert und eingerichtet sein:

- Repository: <https://github.com/Frickeldave/homeassistant-frickeldave-store>
- Benötigter Erweiterungspunkt: Contract `1` (Feld `dependencies` im Manifest)

HACS löst keine Abhängigkeiten zwischen Integrationen auf. Ist die Basis nicht
eingerichtet, bricht der Config Flow ab; ein Start ohne Basis scheitert bereits
an `dependencies`. Fehlt der Erweiterungspunkt oder spricht die Basis einen
anderen Vertrag, startet die Erweiterung nicht und meldet den Grund als
`ConfigEntryNotReady`:

> Die Basis spricht Vertrag 2, diese Erweiterung unterstützt Vertrag 1. Bitte die
> Erweiterung aktualisieren.

Die Reihenfolge lautet deshalb immer: Basis installieren, Basis einrichten,
danach diese Erweiterung installieren und einrichten.
`after_dependencies: ["http"]` sortiert nur die Einrichtung hinter den
HTTP-Baustein von Home Assistant und ersetzt die Basis nicht.

## Installation über HACS

1. HACS öffnen und dort **Benutzerdefinierte Repositories** (Custom
   Repositories) aufrufen.
2. Repository `Frickeldave/homeassistant-frickeldave-store-syncfd` eintragen und
   als Typ **Integration** wählen.
3. Die Integration `Frickeldave Store Sync` installieren.
4. Home Assistant neu starten.
5. Unter **Einstellungen → Geräte & Dienste → Integration hinzufügen** die
   Integration `Frickeldave Store Sync` einrichten.

## Konfiguration

Der Config Flow prüft nur, dass die Basis eingerichtet ist, und legt genau einen
Eintrag mit den frickeldave-Vorgaben an. Alle Einstellungen liegen im Options
Flow (Zahnrad am Integrationseintrag) und werden dort geprüft:

| Feld | Vorgabe | Bedeutung |
| --- | --- | --- |
| `repository` | `Frickeldave/frickeldave.github.io` | Ziel-Repository (`owner/name`). |
| `dev_branch` | `dev` | Integrationsbranch. Ein Commit hier löst `fd-ci` und `fd-deploy-dev` aus. |
| `prod_branch` | `main` | Branch, auf den der Produktions-Workflow befördert. Direkte Commits sind unzulässig. |
| `prod_workflow` | `fd-deploy-prd.yml` | Workflow, der `dev` nach `main` befördert und die Produktion deployt. |
| `token` | *(leer)* | Fine-grained Token, write-only. Leer lassen behält den gespeicherten Token. |
| `catalog_url` | `https://frickeldave.de/data/handmade.json` | Katalogquelle des Imports. |
| `image_base_url` | `https://raw.githubusercontent.com/Frickeldave/frickeldave.github.io/dev/src/assets/handmade` | Bildbasis des Imports. |

Quellen-URLs müssen `https` auf Port 443 mit einem Host der Allowlist
(`frickeldave.de`, `raw.githubusercontent.com`) ohne Zugangsdaten sein. Abweichende
Werte werden mit einer deutschen Feldmeldung abgelehnt.

### Token-Berechtigungen

Der Token wird als **Fine-grained personal access token** nur für
`Frickeldave/frickeldave.github.io` angelegt:

- **Contents: Read and write** – Blobs, Bäume, Commits und der Branch-Ref.
- **Actions: Read and write** – `workflow_dispatch` für `fd-deploy-prd.yml`.

Mehr Rechte braucht die Erweiterung nicht. Der alte Token aus der Basis wird
**nicht** übernommen: Nach dem Update auf die Erweiterung einen neuen Token
anlegen, hier eintragen und den alten widerrufen.

## Veröffentlichen: dev und prod

`publish` schreibt ausschließlich auf `dev`. Ein Produktions-Publish committet
zuerst, was auf `dev` noch fehlt, und stößt danach `fd-deploy-prd.yml` an.
Dieser Workflow lässt `fd-ci` auf `dev` laufen, befördert `dev` nach `main` und
deployt die Pages. **Ein Prod-Publish committet also ebenfalls auf `dev`; ein
separater Dev-Publish ist nicht nötig.** Die Vorschau auf `dev` aktualisiert
sich durch diesen Push automatisch.

| Ziel | Vergleichsbasis | Commit auf | Was läuft |
| --- | --- | --- | --- |
| `dev` | `dev` | `dev` | `fd-ci` und `fd-deploy-dev` |
| `prod` | `main` | `dev` | `fd-deploy-prd.yml` (CI, Merge, Pages) |

Die Vergleichsbasis bestimmt, was sich in der Produktion ändern würde: `prod`
vergleicht gegen `main`, `dev` gegen `dev`. Der Commit landet in beiden Fällen
auf `dev`; `main` beschreibt nur der Workflow.

Ein Release (Tag und GitHub-Release) entsteht nur, wenn die Website-Version in
`package.json` höher ist als das letzte stabile Release. Reine Katalog- und
deploy-neutrale Änderungen werden ohne Release deployt, die Version im Footer
bleibt gleich.

Deploy-neutral sind etwa `docs/**`, `.squad/**`, `.github/**`, `CHANGELOG` und
Tooling-Konfigurationen; sie werden zusammen mit dem Katalog befördert.
**Seitenrelevante** Änderungen – `src/**`, `public/**` außer dem Katalog,
Build-Konfigurationen, `scripts/build-*` und `scripts/install-*`,
`package.json` samt Lockfile – brauchen weiterhin einen Versions-Bump. Ohne ihn
meldet die Promotion „nichts zu promoten" und führt keinen Merge aus.

Gibt es gegenüber dem Zielbranch überhaupt keine Änderung, entstehen weder
Commit noch Workflow-Lauf – unabhängig vom Ziel.

## Feature-Vertrag

Die Payloads sind identisch zu den bisherigen Endpunkten der Basis; das Panel
kann deshalb unverändert weiterarbeiten.

### `publish`

Anfrage:

```json
{"target": "dev", "dry_run": false, "message": "", "allow_empty": false, "confirm_removals": false}
```

Antwort (`200`): `target`, `dry_run`, `repository`, `branch`, `commit`,
`changed`, `counts` (`added`/`changed`/`removed`/`unchanged`/`total`),
`products`, `images`, `files`, `files_truncated`, `deployment`,
`removal_guard`, `warning`, `summary`.

`target: dev` vergleicht gegen `dev` und committet dorthin; der Push löst
`fd-ci` und `fd-deploy-dev` aus. `target: prod` vergleicht gegen `main`,
committet die dort noch fehlenden Änderungen auf `dev` und löst danach
`fd-deploy-prd.yml` über
`POST /repos/{repo}/actions/workflows/{workflow}/dispatches` auf `main` aus.
Ohne Änderungen entstehen weder Commit noch Workflow-Lauf. Details im Abschnitt
[Veröffentlichen: dev und prod](#veröffentlichen-dev-und-prod).

Der Löschschutz blockiert mehr als 10 entfernte Bilder oder mehr als ein Drittel
des Bildbestands; ein Dry-Run zeigt den Plan mit Warnung, ein echter Lauf
verlangt `confirm_removals`. Ein Store ohne veröffentlichbare Produkte wird nur
mit `allow_empty` geleert.

### `import`

Anfrage (alle Felder optional):

```json
{"source_url": "https://…", "image_base_url": "https://…", "import_images": true, "dry_run": false, "overwrite": false}
```

Antwort (`200`): `created`, `skipped`, `failed`, `images_imported`,
`products_total`, `errors` (`articleNumber`, `message`), `dry_run`,
`would_create`, `would_overwrite`, `overwritten`, `unchanged`, `summary`.

Ein Dry-Run lädt Katalog und Bilder, schreibt aber nichts: kein Batch, kein
Snapshot, keine Bilddatei. `overwrite` übernimmt ausschließlich die
Katalogfelder (`name`, `description`, `price`, `size`, `tags`, `visible`,
`customizable`, `stock`); Artikelnummer, Anlagedatum, Bilder, Buchungen und
Archiv-Status bleiben lokal.

### Fehlercodes

Antwortformat bei Fehlern: `{"error": {"code": "...", "message": "..."}}`.

| Code | Status | Bedeutung |
| --- | --- | --- |
| `validation_error` | 400 | Ungültige Felder, Werte oder URLs. |
| `forbidden` | 403 | Rolle fehlt (eigene Importquellen brauchen `app_admin`). |
| `publish_not_configured` | 400 | Repository oder Workflow fehlt. |
| `publish_token_missing` | 400 | Kein GitHub-Token hinterlegt. |
| `publish_empty_catalog` | 409 | Store ohne Produkte, `allow_empty` fehlt. |
| `publish_artifacts_unavailable` | 409 | Bild fehlt oder ist nicht lesbar. |
| `publish_removals_blocked` | 409 | Löschschutz ausgelöst, `confirm_removals` fehlt. |
| `github_unauthorized` | 502 | Token abgelehnt oder ohne Schreibrechte. |
| `github_not_found` | 404 | Repository oder Branch nicht gefunden. |
| `github_conflict` | 409 | Branch während der Veröffentlichung geändert. |
| `github_failed` | 502 | Unerwartete GitHub-Antwort. |
| `github_unavailable` | 502 | GitHub nicht erreichbar oder Störungsseite. |
| `catalog_unavailable` | 502 | Katalog nicht erreichbar. |
| `catalog_invalid` | 422 | Katalog ist kein gültiges JSON oder ohne `products`. |
| `feature_unavailable` | 409 | Fähigkeit installiert, aber nicht eingerichtet (z. B. Token fehlt). |
| `internal_error` | 500 | Unerwarteter Serverfehler (Traceback im HA-Protokoll). |

## Fassade und Grenzen

Die Erweiterung liest Produkte und Lagerstätten über `async_products` und
`async_locations`, baut den Export über `async_build_export` (Katalogbytes und
Bildartefakte im Speicher) und schreibt Importe ausschließlich über
`async_apply_import_batch`. Die stabilen Bildnamen (`<artikel>-NN.<endung>`)
entstehen hier aus den Bildmetadaten; die Dateien selbst liest und schreibt die
Basis.

Kein Zugriff auf `hass.data["frickeldave_store"]`, auf `store`, `models`,
`views`, `permissions` oder Dateipfade der Basis. Keine eigenen HTTP-Routen und
keine Persistenz von Store-Daten. Ein Snapshot entsteht beim Import implizit im
Batch (`pre-import-batch`); `async_create_snapshot` wird deshalb nicht zusätzlich
aufgerufen, und Veröffentlichen schreibt nie in den Store.

## Entwicklung

```bash
pip install ruff==0.17.3 mypy pytest==8.3.4 pytest-asyncio==0.24.0 \
  pytest-homeassistant-custom-component==0.13.205 homeassistant==2025.1.4 \
  aiohttp==3.11.11 voluptuous==0.15.2

ruff check .
ruff format --check .
mypy custom_components
pytest -q
```

Die CI führt zusätzlich `hassfest` und die HACS-Validierung
(`category: integration`) aus. Ein Node-Schritt existiert bewusst nicht: Die
Erweiterung bringt kein eigenes Frontend mit.

Die Tests nutzen einen Fake des Erweiterungspunkts (`tests/fake_base.py`) und
eine Fake-Fassade (`tests/fake_facade.py`), weil die Basis im CI dieser
Erweiterung nicht installiert ist.

## Mitwirken

Fehler und Wünsche bitte über den
[Issue-Tracker](https://github.com/Frickeldave/homeassistant-frickeldave-store-syncfd/issues)
melden. Fachlicher Kontext und Architektur stehen in
[Issue #289](https://github.com/Frickeldave/frickeldave.github.io/issues/289).

## Lizenz

[MIT](LICENSE)
