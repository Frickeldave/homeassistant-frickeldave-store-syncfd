# Frickeldave Store Sync

Diese HACS-Integration ist die frickeldave-spezifische Erweiterung des
Frickeldave Stores: Sie veröffentlicht den Katalog nach `dev` beziehungsweise
`prod` und importiert Produkte samt Bildern von der Website. Sie arbeitet
ausschließlich über den Erweiterungspunkt der Basis-Integration
`frickeldave_store` und enthält selbst keine Store-Daten.

## Status

**Gerüst, Funktionalität folgt.** Version `0.1.0` ist das Repo- und CI-Skelett
(Manifest, Config Flow, Übersetzungen, Tests, Leitlinien). Die Features
`publish` und `import` entstehen in den Arbeitspaketen 5 und 6; bis dahin
registriert sich die Erweiterung ohne Handler und tut fachlich nichts.

## Voraussetzung: Basis-Integration

Die Basis-Integration **`frickeldave_store`** ist Pflicht. Sie liefert Store,
Bilder, Rechte, Snapshots und die Fassade, gegen die diese Erweiterung arbeitet,
und muss zuerst installiert und eingerichtet sein:

- Repository: <https://github.com/Frickeldave/frickeldave.home.net>
- Benötigter Erweiterungspunkt: Contract `1` (Feld `dependencies` im Manifest)

HACS löst keine Abhängigkeiten zwischen Integrationen auf. Ist die Basis nicht
eingerichtet, bricht der Config Flow mit `base_missing` ab; ein Start ohne Basis
scheitert bereits an `dependencies`. Die Reihenfolge lautet deshalb immer: Basis
installieren, Basis einrichten, danach diese Erweiterung installieren und
einrichten. `after_dependencies: ["http"]` sortiert nur die Einrichtung hinter
den HTTP-Baustein von Home Assistant und ersetzt die Basis nicht. `after_dependencies: ["http"]` sortiert nur die Einrichtung hinter
den HTTP-Baustein von Home Assistant und ersetzt die Basis nicht.

## Installation über HACS

1. HACS öffnen und dort **Benutzerdefinierte Repositories** (Custom
   Repositories) aufrufen.
2. Repository `Frickeldave/homeassistant-frickeldave-store-syncfd` eintragen und
   als Typ **Integration** wählen.
3. Die Integration `Frickeldave Store Sync` installieren.
4. Home Assistant neu starten.
5. Unter **Einstellungen → Geräte & Dienste → Integration hinzufügen** die
   Integration `Frickeldave Store Sync` einrichten.

## Einrichtung

Der Config Flow prüft, dass die Basis eingerichtet ist, und legt genau einen
Eintrag an. Der Options Flow ist vorhanden, hat in `0.1.0` aber noch keine
Felder: Repository, Branches, Workflow, Token und Importquellen kommen mit den
Arbeitspaketen 5 und 6. Der GitHub-Token wird write-only gespeichert und nie
protokolliert.

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

## Mitwirken

Fehler und Wünsche bitte über den
[Issue-Tracker](https://github.com/Frickeldave/homeassistant-frickeldave-store-syncfd/issues)
melden. Fachlicher Kontext und Architektur stehen in
[Issue #289](https://github.com/Frickeldave/frickeldave.github.io/issues/289);
das ADR wird nach Abnahme als `docs/adr/0001-sync-extension.md` in dieses
Repository übernommen.

## Lizenz

[MIT](LICENSE)
