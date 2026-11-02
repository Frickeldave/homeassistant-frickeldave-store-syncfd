# Changelog

Alle nennenswerten Änderungen dieser Integration. Das Format orientiert sich an
[Keep a Changelog](https://keepachangelog.com/de/1.1.0/); die Versionen folgen
[Semantic Versioning](https://semver.org/lang/de/).

## [0.1.0] - 2026-11-02

Erste Version: Die frickeldave-spezifische Synchronisation lebt vollständig in
dieser Erweiterung. Sie registriert ihre Fähigkeiten beim Erweiterungspunkt der
Basis `frickeldave_store` (Contract 1) und arbeitet ausschließlich über deren
Fassade.

### Hinzugefügt

- Feature `publish` (Rolle `app_admin`): Veröffentlichen nach `dev` und `prod`
  wie bisher – `dev` ist der einzige Schreibpunkt, `prod` committet auf `dev` und
  dispatcht `fd-deploy-prd.yml`; Dry-Run ohne Schreibwirkung, Löschschutz
  (`confirm_removals`), `allow_empty`, stabile Bildnamen und strukturierte
  Fehlercodes.
- Feature `import` (Rolle `product`): Katalogimport inklusive Bilder mit
  Dry-Run, `overwrite`, Zählern und SSRF-Allowlist; eigene `source_url` und
  `image_base_url` nur für `app_admin`. Geschrieben wird ausschließlich über
  `async_apply_import_batch`.
- Options Flow mit Repository, Dev-/Prod-Branch, Prod-Workflow, write-only
  GitHub-Token und Importquellen; deutsche Feldmeldungen.
- Deutsche und englische Übersetzungen für Config- und Options-Flow.
- Tests mit Fake-Erweiterungspunkt und Fake-Fassade: Registrierung, Rollen,
  Payload-Vertrag, Dry-Run ohne Schreibwirkung, Löschschutz, Allowlist,
  Fehlercodes und der Nachweis, dass der Token nie in Antworten oder Logs
  erscheint.

### Geändert

- Die Erweiterung startet nicht mehr ohne Erweiterungspunkt: Fehlt die Basis oder
  passt die Vertragsversion nicht, meldet das Setup den Grund als
  `ConfigEntryNotReady` statt still ohne Features weiterzulaufen.
- Options-Änderungen laden den Eintrag neu, damit `ready` und `reason` der
  Fähigkeiten (z. B. fehlender Token) sofort stimmen.

### Entfernt

- Nichts. Die Basis `frickeldave_store` bleibt unverändert; ihr eingebauter
  Fallback für `publish` und `import` läuft weiter, bis er in einem eigenen
  Release entfernt wird.
