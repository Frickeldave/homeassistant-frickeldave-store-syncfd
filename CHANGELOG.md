# Changelog

Alle nennenswerten Änderungen dieser Integration. Das Format orientiert sich an
[Keep a Changelog](https://keepachangelog.com/de/1.1.0/); die Versionen folgen
[Semantic Versioning](https://semver.org/lang/de/).

## [Unreleased]

### Fixed

- **Ein 404 beim Prod-Workflow nennt jetzt Workflow, Branch und Token-Recht (#289).**
  Antwortet GitHub auf `POST actions/workflows/<workflow>/run` mit 404, nennt die
  Meldung den konfigurierten Workflow und Produktionsbranch sowie die
  wahrscheinlichsten Ursachen (falscher Workflow-Name oder Branch, fehlendes
  Recht „Actions: Read and write“) — statt pauschal „Repository oder Branch nicht
  gefunden“. Ein 404 auf `git/ref(s)` nennt Repository, den Branch aus dem Pfad und
  das Recht „Contents: Read and write“; andere 404-Antworten behalten die
  bisherige Meldung.
- **Prod-Dry-Run vergleicht jetzt gegen `main` (#289).**
  Der Bericht für „Nach Prod veröffentlichen“ liest den Dateibaum und den
  Katalog des Produktionsbranchs (`main`) statt immer `dev`; Zähler, Produkte,
  Bilder, Dateiliste und Löschschutz spiegeln damit das, was nach der Promotion
  tatsächlich in Produktion ankommt. Der Commit bleibt auf `dev`, und
  `fd-deploy-prd.yml` wird unverändert auf `main` ausgelöst. Die Antwort enthält
  zusätzlich `compare_branch` (Vergleichsbasis des Berichts) und `commit_branch`
  (Zielbranch des Commits), und die Zusammenfassung nennt die Vergleichsbasis.
- **Der Dry-Run meldet Produkte nicht mehr fälschlich als neu (#289).**
  Ist `handmade.json` byte-identisch, gelten alle Produkte als unverändert und
  GitHub wird für den Katalogvergleich nicht mehr gelesen. Die Produktzähler
  passen damit zu den Dateizählern.
- **GitHub-Fehler beim Veröffentlichen sind jetzt diagnostizierbar (#289).**
  Antwortet GitHub mit 5xx, nennt die Meldung zusätzlich den HTTP-Status und
  GitHubs `message` (einzeilig, gekürzt, ohne Token oder Payload) — statt nur
  „Störung auf der GitHub-Seite“. Jede fehlgeschlagene Antwort wird mit Methode,
  Pfad (ohne Query), Status und Meldung als Warnung protokolliert. Vorübergehende
  Fehler (Timeout, `aiohttp.ClientError`, HTTP 500/502/503/504) werden bis zu drei
  Mal versucht (Backoff 0,5 s, dann 1,5 s); das abschließende
  `PATCH git/refs/heads/<branch>` bleibt bewusst ohne Retry, weil es nicht
  idempotent ist.

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
