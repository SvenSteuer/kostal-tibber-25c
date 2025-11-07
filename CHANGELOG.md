# Changelog

## [1.2.0-beta.10] - 2025-11-06

### Fixed
- **Import berechnet jetzt korrekt Hausverbrauch** - Import verwendet nun Grid + PV Sensoren
  - Problem: Manueller Import verwendete nur einen Sensor (oft falscher Wert)
  - Lösung: Neue Funktion `import_calculated_consumption_from_ha` berechnet Home = PV + Grid
  - Konsistent mit automatischer Aufzeichnung seit v1.2.0-beta.8
  - Tabelle und Diagramm zeigen jetzt identische, korrekte Werte nach Import

### Technical
- Added `ConsumptionLearner.import_calculated_consumption_from_ha()` function
- Updated API endpoint `/api/consumption_import_ha` to use calculated import
- Marked old `import_from_home_assistant()` as DEPRECATED

## [1.1.2] - 2025-11-06

### Fixed
- **48h Schedule beim Start** - Schedule wird jetzt sofort beim Start berechnet
  - Behebt: "No daily battery schedule available" Warnung
  - Ladesteuerung funktioniert ab der ersten Sekunde
- **Economic Charging Timing** - Lädt nicht mehr in der aktuellen Stunde
  - Problem: Um 8:46 Uhr plante es Laden "um 8:00" (zu spät!)
  - Lösung: Economic Charging startet jetzt bei `current_hour + 1`
- **Realistischere Vergangenheits-Schätzung** - SOC-Rückwärtsberechnung verbessert
  - Problem: Große SOC-Sprünge (29% → 91%) verwirrten das System
  - Lösung: Sanity-Check verhindert unrealistische Schätzungen (>50% Abweichung)
  - Fallback: Verwendet 70% als typischen Mitternachts-Wert

### Technical
- Added initial 48h schedule calculation in controller_loop startup
- Economic charging loop: `range(current_hour + 1, 48)` statt `range(current_hour, 48)`
- Sanity check in both `baseline_soc` and `final_soc` calculations
- Improved debug logging for large SOC deviations

## [1.1.1] - 2025-11-05

### Fixed
- **Kritischer Ladefehler behoben** - Batterie lud zur falschen Zeit aufgrund 48h-Logik-Konflikt
  - Problem: Ladelogik prüfte alle 48 Stunden (0-47) statt nur heute (0-23)
  - Folge: Batterie konnte zu teuren Zeiten laden, wenn morgige günstige Stunde mit heutiger übereinstimmte
  - Lösung: Ladeentscheidung prüft jetzt explizit nur `window['hour'] < 24`
- **Veralteter Code entfernt** - Alte 24h-Fallback-Logik komplett entfernt
  - Entfernt: `should_charge_now()` Methode (nicht mehr benötigt)
  - System nutzt jetzt ausschließlich den 48h-Plan für Ladeentscheidungen
- **Verbessertes Debug-Logging** - Zeigt jetzt warum geladen/nicht geladen wird

### Changed
- Charging-Logik nutzt nur noch `plan_daily_battery_schedule()` mit 48h-Fenstern
- Keine Fallback-Methoden mehr - klare, konsistente Ladesteuerung

## [1.1.0] - 2025-11-05

### Added
- **📊 48-Stunden Diagramme** - Alle Dashboard-Grafiken zeigen jetzt 2 Tage (heute + morgen)
- **Erweiterte Tibber-Preisanzeige** - Zeigt Preise für heute und morgen
  - Labels: "Heute HH:00" und "Morgen HH:00"
  - Fehlende morgige Preise (vor 13 Uhr) werden als grau angezeigt
- **48h Batterie-Prognose** - SOC-Verlauf und Ladeplanung über 2 Tage
  - Verwendet aktuellen SOC als Ankerpunkt für präzise Prognose
  - Vergangenheit wird rückwärts geschätzt, Zukunft vorwärts simuliert
- **48h Verbrauchsprognose** - Prognostizierter Verbrauch für heute und morgen
  - Berücksichtigt Wochentag-spezifische Profile

### Fixed
- **Kritischer SOC-Berechnungsfehler behoben** - Prognose ignorierte vorher den aktuellen SOC
  - Problem: Simulation startete von Mitternacht, nutzte aber nicht den echten aktuellen SOC
  - Lösung: Aktuelle Stunde verwendet jetzt den tatsächlichen SOC-Wert als Anker
  - Beispiel: Um 23:33 mit 80% SOC zeigt die Grafik jetzt korrekt 80% an (vorher 30%)
- **SOC-Initialisierung beim Start** - SOC wird jetzt vor erster Planung geladen
- **Physik-Verletzung behoben** - Laden erhöht jetzt korrekt den SOC (vorher konnte SOC beim Laden fallen)

### Changed
- API-Endpunkt `/api/battery_schedule` erweitert auf 48h
  - Lädt heute + morgen Tibber-Preise
  - Liefert 48 Werte für SOC, Ladung, PV, Verbrauch
- API-Endpunkt `/api/tibber_price_chart` erweitert auf 48h
- API-Endpunkt `/api/consumption_forecast_chart` erweitert auf 48h
- Batterie-Simulationslogik umgeschrieben für 48-Stunden-Zeitraum
- Chart-Labels zeigen jetzt Tag + Uhrzeit (z.B. "Heute 14:00", "Morgen 06:00")

### Technical
- `plan_daily_battery_schedule()` simuliert jetzt 48 Stunden statt 24
- `get_hourly_pv_forecast()` mit `include_tomorrow=True` Parameter
- Stunden-Indexierung: 0-23 = heute, 24-47 = morgen
- Alle Arrays erweitert von 24 auf 48 Elemente
- SOC-Simulation nutzt aktuellen SOC als Referenzpunkt (hour=current_hour)
- Verbesserte Fehlerbehandlung für fehlende SOC-Sensordaten

### Why This Matters
- **Bessere Planung** - Sehe den kompletten Lade- und Verbrauchsplan für 2 Tage
- **Morgige Preise** - Plane optimal für günstige Stunden am nächsten Tag
- **Realitätsnähe** - SOC-Prognose entspricht jetzt der Realität (nutzt aktuellen Wert)
- **Vollständiger Überblick** - Alle drei Grafiken konsistent über 48 Stunden

## [0.6.4] - 2025-11-04

### Changed
- **📊 Verbesserte Grafiken** - Optimierte Darstellung nach Benutzerwunsch
- **Tibber Preise als Balkendiagramm** - Besser erkennbare Preisunterschiede
  - Aktuelle Stunde rot hervorgehoben
  - Alle anderen Balken in Gelb
- **Verbrauchsdiagramm mit zwei Linien**:
  - **Gelbe gefüllte Linie**: Prognostizierter Verbrauch (basierend auf gelernten Daten)
  - **Blaue Linie**: Tatsächlicher Verbrauch heute (Live-Daten aus Home Assistant)
  - Beide Linien im gleichen Diagramm für direkten Vergleich
- **Tatsächlicher Verbrauch heute** wird automatisch aus Home Assistant abgerufen
  - Nutzt `home_consumption_sensor` Konfiguration
  - Zeigt nur bereits vergangene Stunden
  - Automatische Watt→kW Konvertierung
  - Aktualisierung alle 5 Minuten

### Technical
- API-Endpunkt `/api/consumption_forecast_chart` erweitert
  - Liefert jetzt sowohl `forecast` als auch `actual` Daten
  - Ruft History-Daten für heute ab
  - Gruppiert nach Stunden und berechnet Durchschnitte
- Chart-Typ für Preise von `line` zu `bar` geändert
- Chart-Typ für Verbrauch von `bar` zu `line` mit zwei Datasets geändert
- `spanGaps: true` für tatsächlichen Verbrauch (verbindet Linie auch bei fehlenden Stunden)

### Why This Matters
- **Besserer Vergleich** - Prognose vs. Realität direkt sichtbar
- **Genauere Planung** - Sehe wie genau deine Prognosen sind
- **Optimierung möglich** - Erkenne Abweichungen und passe dein Verhalten an
- **Live-Feedback** - Aktueller Verbrauch zeigt wie der Tag verläuft

### Example
Verbrauchsdiagramm zeigt:
- 06:00 Uhr: Prognose 2.0 kW (gelb), Tatsächlich 1.8 kW (blau) → Unter Prognose!
- 12:00 Uhr: Prognose 1.2 kW (gelb), Tatsächlich 1.5 kW (blau) → Über Prognose!
- 18:00 Uhr: Prognose 2.0 kW (gelb), noch keine Daten (blau nicht sichtbar)

→ Du siehst sofort ob du mehr oder weniger verbrauchst als erwartet!

## [0.6.3] - 2025-11-04

### Added
- **📊 Grafische Darstellungen im Dashboard** - Zwei neue interaktive Charts
- **Tibber Preisverlauf-Grafik** - Zeigt stündliche Strompreise für heute
  - Liniendiagramm mit allen 24 Stunden
  - Aktuelle Stunde hervorgehoben (roter Punkt)
  - Preise in Cent/kWh dargestellt
  - Automatische Aktualisierung alle 5 Minuten
- **Verbrauchsprognose-Grafik** - Zeigt prognostizierten Verbrauch basierend auf historischen Daten
  - Balkendiagramm mit stündlichen Verbrauchswerten
  - Aktuelle Stunde hervorgehoben (gelb)
  - Verbrauch in kW dargestellt
  - Basiert auf den gelernten Verbrauchsmustern
- **Chart.js Integration** - Moderne, responsive Diagramme
- Neue API-Endpunkte:
  - `GET /api/tibber_price_chart` - Preisverlauf für Grafik
  - `GET /api/consumption_forecast_chart` - Verbrauchsprognose für Grafik

### Technical
- Chart.js 4.4.0 von CDN eingebunden
- Responsive Charts mit Dark-Mode-Support
- Automatische Aktualisierung der Grafiken alle 5 Minuten
- Highlighting der aktuellen Stunde in beiden Grafiken
- Optimierte Chart-Performance mit `maintainAspectRatio: false`

### Why This Matters
- **Visuelle Übersicht** - Schnell erkennbare Muster im Preisverlauf
- **Bessere Planung** - Sehe wann die Preise steigen/fallen
- **Verbrauchseinblick** - Verstehe deine Verbrauchsmuster über den Tag
- **Datenbasierte Entscheidungen** - Kombiniere Preis + Verbrauch für optimale Ladezeiten

### Example
Preisgrafik zeigt:
- 00:00-06:00: Niedrige Preise (grün) → Optimal zum Laden
- 06:00-20:00: Hohe Preise (gelb/rot) → Batterie nutzen
- 20:00-24:00: Mittlere Preise

Verbrauchsgrafik zeigt:
- Morgens 06:00-08:00: Hoher Verbrauch (Frühstück, Kaffee)
- Mittags 12:00-14:00: Mittlerer Verbrauch (Kochen)
- Abends 17:00-21:00: Hoher Verbrauch (Abendessen, TV)

→ Kombiniert: Batterie vorher laden wenn Preise niedrig sind!

## [0.6.2] - 2025-11-04

### Fixed
- **🔧 UI Display Fix** - "undefined Tage importiert" zeigt jetzt die korrekte Anzahl
- Import-Response enthält jetzt `imported_days` Feld in allen Funktionen
- CSV-Import und HA-Import zeigen beide die importierten Tage korrekt an
- Alle Error-Responses enthalten jetzt konsistent alle Felder

### Technical
- `import_from_home_assistant()` fügt `imported_days` zur Response hinzu
- `import_from_csv()` fügt `imported_days` zur Response hinzu
- Alle Error-Responses enthalten: `imported_hours`, `imported_days`, `skipped_days`
- Konsistente Response-Struktur für bessere UI-Integration

## [0.6.1] - 2025-11-04

### Fixed
- **🔧 Watt-Sensor Unterstützung** - Automatische Umrechnung von Watt zu kW
- Sensoren die Leistung in Watt (W) statt kWh liefern werden nun korrekt verarbeitet
- Werte > 50 werden automatisch als Watt erkannt und durch 1000 geteilt (W → kW)
- Filter-Schwelle von 50 kWh auf 50.000 W (50 kW) erhöht für realistische Hausverbräuche
- Mindest-Daten-Schwelle von 12 auf 3 Stunden pro Tag reduziert (für spärliche History-Daten)
- Detailliertes Logging: Zeigt genau welche Einträge warum gefiltert wurden
- Zeigt verfügbare Stunden pro Tag für besseres Debugging

### Technical
- Automatische Einheit-Erkennung: Werte > 50 = Watt, Werte ≤ 50 = kWh
- Neue Logging-Counter: skipped_unavailable, skipped_not_numeric, skipped_negative, skipped_too_high
- Log zeigt jetzt für jeden Tag: Anzahl Stunden und welche Stunden vorhanden sind
- Beispiel: 865 W → 0.865 kW automatisch konvertiert

### Why This Matters
- **Funktioniert mit Standard-Sensoren** - Die meisten HA Verbrauchssensoren liefern Watt, nicht kWh
- **Bessere Datennutzung** - 3 Stunden pro Tag reichen jetzt (vorher 12), mehr Tage werden importiert
- **Besseres Debugging** - Klare Logs zeigen genau, was mit den Daten passiert

## [0.6.0] - 2025-11-04

### Added
- **🏠 Automatischer Home Assistant History Import** - Importiere Verbrauchsdaten direkt aus Home Assistant
- Neuer Button "Aus Home Assistant importieren" auf Import-Seite
- Automatische Datenverarbeitung der letzten 28 Tage aus dem konfigurierten Sensor
- Intelligente Handhabung hochauflösender Daten (mehrere Werte pro Stunde werden gemittelt)
- Ältere Daten (nur stündlich) werden direkt übernommen
- Neue API-Endpunkte:
  - `POST /api/consumption_import_ha` - Import aus Home Assistant History
- Erweiterte HA Client-Funktionen:
  - `get_history()` - Abrufen historischer Daten über HA REST API
- Erweiterte ConsumptionLearner-Funktionen:
  - `import_from_home_assistant()` - Vollautomatischer Import mit Datenverarbeitung

### Technical
- Nutzt Home Assistant History API (`/api/history/period/{start_time}`)
- Gruppiert Datenpunkte nach (Datum, Stunde) und berechnet Durchschnitt
- Filtert negative Werte und unrealistische Werte (> 50 kWh)
- Überspringt Tage mit weniger als 12 Stunden Daten
- Füllt fehlende Stunden innerhalb eines Tages mit Tagesdurchschnitt
- Löscht alte manuelle Daten vor neuem Import (verhindert Datenkonflikte)
- Konfigurierbar über `home_consumption_sensor` in config.yaml

### Why This Matters
- **Kein manueller CSV-Export mehr nötig** - Direkter Zugriff auf HA-Verlaufsdaten
- **Hochauflösende Daten optimal genutzt** - Mehrfachwerte pro Stunde → präziser Durchschnitt
- **Robuste Datenverarbeitung** - Filtert Ausreißer, Fehler und unrealistische Werte
- **Ein-Klick-Import** - 28 Tage Historie mit einem Klick importiert
- **Intelligente Lückenbehandlung** - Fehlende Stunden werden mit Tagesdurchschnitt gefüllt

### Example
Statt CSV manuell erstellen:
```
1. Daten aus HA exportieren
2. CSV formatieren
3. Hochladen
```

Jetzt:
```
1. Button klicken
2. Fertig!
```

Sensor `sensor.ksem_home_consumption` liefert:
- Montag 7:00-8:00: [2.1, 2.3, 2.0, 2.4, ...] (300 Werte) → Ø 2.2 kWh
- Dienstag 7:00-8:00: [1.9] (1 Wert) → 1.9 kWh
→ System verarbeitet beide Fälle korrekt!

## [0.5.9] - 2025-11-04

### Fixed
- **🗑️ CSV-Import löscht alte Daten** - Verhindert, dass alte manuelle Daten erhalten bleiben
- Neue Funktionen: `clear_all_manual_data()` und `clear_all_data()`
- Vor jedem CSV-Import werden alte manuelle Daten automatisch gelöscht
- Behebt Problem: CSV ohne 7.10. hochladen zeigt trotzdem den 7.10.

### Added
- **🔍 HTML Debug-Seite** - `/debug_consumption` zeigt Daten als lesbare Tabelle
- Zeigt für jedes Datum: Anzahl Stunden, erste/letzte Stunde, manuell/gelernt
- Total-Übersicht: Alle Stunden (manuell + automatisch gelernt)
- Einfacher Link statt JSON-API

### Technical
- `consumption_learner.clear_all_manual_data()` - Löscht nur manuelle Daten
- `consumption_learner.clear_all_data()` - Löscht ALLE Daten
- CSV-Import ruft automatisch `clear_all_manual_data()` auf

## [0.5.8] - 2025-11-04

### Added
- **🔍 Debug-Endpoint** - `/api/debug_consumption/<date>` für Import-Debugging
- Zeigt Rohdaten aus der Datenbank für ein bestimmtes Datum
- Hilft bei der Diagnose von Import-Problemen

### Technical
- Endpoint zeigt timestamp, hour, consumption_kwh, is_manual, created_at
- Beispiel: `/api/debug_consumption/2025-10-07`

## [0.5.7] - 2025-11-04

### Fixed
- **🔧 API-Routen im JavaScript** - Verwenden dynamischen basePath statt url_for()
- JavaScript ermittelt basePath aus aktueller URL
- Alle fetch() Aufrufe nutzen `basePath + '/api/...'`
- Behebt JSON.parse Fehler beim Laden der Import-Seite
- API-Calls funktionieren korrekt mit /ingress Routing

### Technical
- basePath = `window.location.pathname.replace(/\/[^\/]*$/, '')`
- Von `/ingress/consumption_import` → basePath = `/ingress`
- fetch: `basePath + '/api/consumption_data'` → `/ingress/api/consumption_data`

## [0.5.6] - 2025-11-04

### Fixed
- **🔗 Relative Links für /ingress Routing** - Links verwenden nun relative Pfade
- Import-Link im Dashboard: `consumption_import` statt `{{ url_for(...) }}`
- Zurück-Link: `./` statt `{{ url_for('dashboard') }}`
- Behebt 404-Fehler durch fehlenden `/ingress` Präfix in generierten URLs
- Funktioniert korrekt mit HA Ingress unter `/addon_slug/ingress/` Pfad

### Technical
- Relative Links funktionieren unabhängig vom Ingress-Pfad
- Dashboard: `/ingress` → Link: `consumption_import` → Ziel: `/ingress/consumption_import`
- Import: `/ingress/consumption_import` → Link: `./` → Ziel: `/ingress`

## [0.5.5] - 2025-11-04

### Fixed
- **🎨 Template-Struktur korrigiert** - consumption_import.html verwendet jetzt base.html
- Extends base.html wie alle anderen Seiten (dashboard, logs, etc.)
- Konsistente Template-Struktur für korrektes Rendering im HA Ingress
- Behebt Problem mit HA-Frontend-Overlay das die Seite überdeckte
- Route nutzt wieder render_template() statt direktes File-Reading

### Technical
- Template-Struktur: `{% extends "base.html" %}` + `{% block content %}`
- Inline-Styles im content-Block für Import-spezifisches Styling
- Verwendet url_for() für alle API-Routen und Links
- Funktioniert nun konsistent mit HA Ingress-Architektur

## [0.5.4] - 2025-11-04

### Fixed
- **📄 Direktes HTML-Serving** - consumption_import.html wird nun direkt gelesen und gesendet
- Umgehung von render_template() für standalone HTML-Datei
- Vermeidet potenzielle Jinja2-Rendering-Probleme
- Explizite UTF-8 Encoding beim Lesen der Datei
- Fehlerbehandlung mit aussagekräftigen Fehlermeldungen

### Technical
- Route liest HTML-Datei direkt mit open() und return f.read()
- Try-catch Block für besseres Error-Handling
- Loggt Fehler für einfacheres Debugging

## [0.5.3] - 2025-11-04

### Fixed
- **🔧 ProxyFix für url_for() Ingress-Support** - Werkzeug ProxyFix Middleware hinzugefügt
- Flask app.wsgi_app mit ProxyFix konfiguriert für korrekte URL-Generierung
- url_for() generiert nun URLs mit korrektem Ingress-Präfix
- Verarbeitet X-Forwarded-* Header von Home Assistant Ingress-Proxy
- Dashboard Import-Link zeigt nun korrekte URL beim Mouseover

### Technical
- Importiert werkzeug.middleware.proxy_fix.ProxyFix
- Konfiguration: x_for=1, x_proto=1, x_host=1, x_prefix=1
- Ermöglicht Flask, hinter Reverse-Proxy korrekt zu arbeiten

## [0.5.2] - 2025-11-04

### Fixed
- **🔗 Dashboard Import-Link** - Verwendung von url_for() für korrektes Ingress-Routing
- Import-Link im Dashboard verwendet nun Flask url_for('consumption_import_page')
- Statt hardcodiertem '/consumption_import' nun dynamische URL-Generierung
- Gewährleistet korrektes Routing durch Home Assistant Ingress-Proxy

### Technical
- Änderung in dashboard.html: href="{{ url_for('consumption_import_page') }}"
- Funktioniert mit allen Ingress-URL-Präfixen

## [0.5.1] - 2025-11-04

### Fixed
- **🔧 Ingress-Kompatibilität für Import-Seite** - Konvertierung zu Standalone-HTML
- Entfernung von Jinja2-Template-Vererbung ({% extends %}, {% block %})
- Alle CSS-Styles inline in `<head>` eingebettet
- JavaScript inline integriert zur Vermeidung von Static-File-Problemen
- Behebt weißen Bildschirm bei Zugriff über Home Assistant Ingress
- Relative Pfade für "Zurück zum Dashboard" Link

### Technical
- Template consumption_import.html vollständig eigenständig
- Keine Abhängigkeiten von base.html oder static files
- Funktioniert korrekt mit HA Ingress URL-Präfix

## [0.5.0] - 2025-11-04

### Added
- **📊 CSV-Import für detaillierte Verbrauchsdaten** - Importiere 28 Tage mit individuellen Tagesprofilen
- **✏️ Web-basierter Tabellen-Editor** - Bearbeite Verbrauchsdaten direkt im Browser
- Neue Import-Seite `/consumption_import` mit vollem Import/Editor Interface
- CSV-Import unterstützt:
  - Detaillierte historische Daten (28 Tage × 24 Stunden = 672 Datenpunkte)
  - Deutsches Zahlenformat (Komma als Dezimaltrennzeichen)
  - Flexible Datumsformate (YYYY-MM-DD oder DD.MM.YYYY)
  - Automatische Wochentagserkennung aus Datum
  - Echtzeit-Validierung und Fehlerbehandlung
- CSV-Vorlagen-Download-Funktion für einfachen Einstieg
- Web-Editor Features:
  - 28×24 Daten-Matrix mit vollständiger Bearbeitung
  - Zeilen hinzufügen/löschen
  - Automatische Wochentagsberechnung
  - Laden vorhandener Daten aus Datenbank
  - Speichern bearbeiteter Daten
- Dashboard-Link zur Import-Seite
- Neue API-Endpunkte:
  - `POST /api/consumption_import_csv` - CSV-Datei Upload
  - `GET /api/consumption_data` - Vorhandene Daten laden
  - `POST /api/consumption_data` - Bearbeitete Daten speichern
- Erweiterte ConsumptionLearner-Funktionen:
  - `import_detailed_history()` - Import mit individuellen Tagesprofilen
  - `import_from_csv()` - Robustes CSV-Parsing mit Fehlerbehandlung

### Changed
- Verbrauchslernen unterscheidet jetzt zwischen Wochentagen und Wochenende
- Detailliertere Datenbasis ermöglicht präzisere Vorhersagen
- Verbesserte Validierung für negative und unrealistische Werte

### Technical
- CSV-Parser mit `io.StringIO` und `csv.DictReader`
- Unterstützung für beide Dezimaltrennzeichen (Komma/Punkt)
- Flexible Datumsformatierung mit Fallback
- Vollständige Fehlerbehandlung mit detaillierten Log-Meldungen
- `is_manual` Flag zur Unterscheidung manueller vs. gelernter Daten
- Automatische Bereinigung alter Daten über Lernzeitraum

### Why This Matters
- **Wochenend-Muster**: Samstag/Sonntag haben oft andere Verbrauchsmuster als Wochentage
- **Präzisere Vorhersagen**: 28 individuelle Tagesprofile statt 1 generisches Profil
- **Schneller Start**: Mit vorhandenen Daten sofort optimale Ladeentscheidungen
- **Flexibilität**: CSV-Import für Masse, Web-Editor für Feintuning

### Example
Statt ein generisches Tagesprofil für alle 28 Tage:
```
Jeden Tag: 7-8 Uhr = 2.0 kWh
```

Jetzt individuelle Profile pro Wochentag:
```
Montag 7-8 Uhr: 2.5 kWh (Arbeitstag, Homeoffice)
Samstag 7-8 Uhr: 0.8 kWh (Wochenende, länger geschlafen)
```
→ Bessere Vorhersagen, präzisere Ladesteuerung!

## [0.4.0] - 2025-11-04

### Added
- **🎓 Consumption Learning System** - Self-learning household consumption patterns
- SQLite-based consumption learning with 4-week rolling window (configurable 7-90 days)
- Manual load profile initialization for immediate baseline (24-hour profile)
- Automatic hourly consumption recording from Home Assistant sensor
- Intelligent energy deficit prediction based on learned consumption patterns
- Consumption-aware charging optimization (replaces simple PV threshold)
- New dashboard card "Verbrauchslernen" showing:
  - Learning progress percentage (manual vs. learned data)
  - Total data records and learned hours
  - Time period of collected data
- New API endpoint `/api/consumption_learning` for statistics and hourly profile
- Configuration parameters:
  - `enable_consumption_learning`: Enable/disable learning (default: true)
  - `learning_period_days`: Learning period in days (default: 28, range: 7-90)
  - `home_consumption_sensor`: HA sensor for consumption recording
  - `manual_load_profile`: Initial 24-hour baseline profile (0-23 hours with kW values)
  - `average_daily_consumption`: Alternative - daily consumption in kWh (divided by 24 for fallback)
  - `default_hourly_consumption_fallback`: Fallback value when no data (default: 1.0 kWh/h)

### Changed
- **Improved charging logic** now considers hourly consumption patterns vs. hourly PV forecast
- Simple daily PV threshold replaced with sophisticated hourly energy balance calculation
- TibberOptimizer now uses `predict_energy_deficit()` method for better decisions
- Charging decisions now account for morning consumption peaks even when daily PV total is sufficient
- Status explanations updated to show energy balance information
- ConsumptionLearner integrated into TibberOptimizer for real-time predictions
- **Flexible fallback configuration**: Choose between manual 24h profile OR simple daily average
  - Priority: 1) `default_hourly_consumption_fallback`, 2) `average_daily_consumption / 24`, 3) 1.0 kWh/h
  - No error if no baseline data provided - system starts learning from zero with sensible fallback

### Technical
- Created `ConsumptionLearner` class with SQLite backend (`/data/consumption_learning.db`)
- Database schema with `hourly_consumption` table tracking manual/learned data
- `add_manual_profile()` generates 28 days of baseline from user's 24-hour profile
- `record_consumption()` replaces old data automatically (rolling window)
- `get_average_consumption()` returns learned average per hour
- `predict_consumption_until()` predicts total consumption to target hour
- `get_statistics()` provides learning progress metrics
- Automatic cleanup of data older than learning period
- Hourly consumption recording in controller loop
- Dashboard auto-updates learning statistics every 30 seconds

### Why This Matters
The simple "daily PV threshold" (e.g., 16.9 kWh PV forecast) doesn't account for time distribution:
- **Problem**: Morning 7-10am has only 1.07 kWh PV but 3-5 kWh consumption → Battery depletes!
- **Solution**: Learning system analyzes hourly patterns and charges battery to bridge morning gap

### Example
Before (v0.3.x):
- Daily PV: 16.9 kWh > 5 kWh threshold → ✅ Don't charge
- Reality: Morning deficit drains battery → ❌ Problem!

After (v0.4.0):
- Hourly analysis: PV 7-10am = 1.07 kWh, Consumption 7-10am = 4.5 kWh
- Predicted deficit: 3.43 kWh → 🔋 Charge battery during night!
- Result: Battery ready for morning consumption peak → ✅ Success!

## [0.3.7] - 2025-11-03

### Fixed
- Improved condition labels to be more positive and intuitive
- "SOC unter Sicherheitsminimum" → "Sicherheits-SOC nicht unterschritten" (when OK)
- "Batterie bereits voll" → "Lade-Limit nicht erreicht/erreicht"
- Added actual values to all condition labels for better transparency
- Fixed logic error where 10% was shown as "< 10%"

### Changed
- Removed redundant "Geplante Ladezeit erreicht" condition
- Conditions now use: ✅ = Normal/OK, ❌ = Problem/Action needed
- All labels now show actual values in comparison (e.g., "17% ≥ 10%")

### Examples
Before:
- ❌ SOC unter Sicherheitsminimum (10% < 10%) ← Wrong!
- ❌ Batterie bereits voll (10% ≥ 100%) ← Confusing!
- ❌ Geplante Ladezeit erreicht ← Redundant

After:
- ✅ Sicherheits-SOC nicht unterschritten (17% ≥ 10%) ← Clear!
- ✅ Lade-Limit nicht erreicht (45% < 95%) ← Better!
- ✅ PV-Ertrag ausreichend (12.0 kWh > 5.0 kWh) ← Informative!

## [0.3.6] - 2025-11-03

### Added
- **Dynamic charging status explanation** on dashboard showing WHY and WHEN battery will be charged
- New "Ladestatus" card with human-readable explanation
- Visual condition checkboxes with green checkmarks (✅) and red crosses (❌)
- Shows all relevant conditions:
  - SOC below safety minimum
  - Battery already full
  - Sufficient PV expected
  - Planned charging time reached
  - Charging plan available
- Auto-updates every 5 seconds for real-time status
- New API endpoint `/api/charging_status` for detailed charging decision logic

### Examples
Status texts dynamically generated:
- "⚡ Der Speicher wird SOFORT geladen, weil der SOC (15%) unter dem Sicherheitsminimum von 20% liegt."
- "⏳ Der Speicher wird ab 01:34 Uhr geladen, sodass er bis 04:00 Uhr bei 95% ist."
- "☀️ Der Speicher wird nicht aus dem Netz geladen, weil der prognostizierte Solarertrag mit 12 kWh über dem Schwellwert von 5 kWh liegt."
- "✅ Der Speicher wird nicht geladen, weil er bereits bei 96% liegt (Ziel: 95%)."

### Technical
- Added `get_charging_status_explanation()` function for status generation
- Condition evaluation with priority system
- Integrated with existing charging decision logic

## [0.3.5] - 2025-11-03

### Added
- Comprehensive CONFIGURATION.md documentation explaining all parameters
- Detailed inline comments for all automation parameters
- Better explanation of `auto_charge_below_soc` (means "charge UP TO this SOC", not "charge only when below")

### Changed
- `battery_soc_sensor` is now visible and required in configuration (was hidden/optional before)
- Improved parameter descriptions with German explanations
- Added section headers in config.yaml for better organization

### Documentation
- Created detailed CONFIGURATION.md with:
  - Explanation of all SOC parameters and their meaning
  - Tibber smart charging parameter details
  - Example scenarios and calculations
  - Troubleshooting common issues
- Clarified that `auto_charge_below_soc` is the TARGET SOC (charge UP TO), not a condition
- Explained `auto_safety_soc` as immediate charging trigger (charge WHEN BELOW)

## [0.3.4] - 2025-11-03

### Fixed
- Removed redundant `min_soc` and `max_soc` parameters that were conflicting with existing parameters
- Now consistently uses `auto_safety_soc` as safety minimum (default 20%)
- Now consistently uses `auto_charge_below_soc` as target SOC (default 95%)

### Removed
- Config parameters `min_soc` and `max_soc` (use existing `auto_safety_soc` and `auto_charge_below_soc` instead)

### Changed
- Charging plan calculation and controller now use the same SOC parameters as other automation logic
- Better consistency across the entire application

## [0.3.3] - 2025-11-03

### Fixed
- **Critical:** Fixed timezone comparison error preventing charging plan calculation
- Changed `datetime.now()` to `datetime.now().astimezone()` for timezone-aware comparisons
- Resolved "can't compare offset-naive and offset-aware datetimes" error
- Charging plan calculation now works correctly with Tibber price data

### Technical
- All datetime comparisons in TibberOptimizer are now timezone-aware
- Properly handles timezone information from Tibber sensor data (UTC/ISO format)

## [0.3.2] - 2025-11-03

### Fixed
- Significantly improved logging for charging plan calculation to identify issues
- Added detailed error messages when calculation fails
- Now logs each step: checking prerequisites, fetching price data, analyzing prices
- Marks `last_calculated` even when no optimal plan is found

### Added
- Manual "Neu berechnen" button in charging plan card for testing
- New API endpoint `/api/recalculate_plan` to manually trigger calculation
- Better visibility of why charging plan calculation succeeds or fails

### Improved
- Logging now shows: number of prices (today/tomorrow), sensor names, missing data
- Error messages appear in system logs AND in dashboard logs
- Helps diagnose issues with Tibber sensor or missing price data

## [0.3.1] - 2025-11-03

### Changed
- Charging plan calculation now runs immediately on startup (not after 5 minutes)
- Improved documentation for `input_datetime` helpers in config.yaml

### Documentation
- Added detailed explanation of optional Home Assistant `input_datetime` integration
- Explained that input_datetime helpers must be created manually in HA configuration.yaml
- Added example YAML configuration for creating the helpers
- Clarified that input_datetime integration is optional and addon works without it

## [0.3.0] - 2025-11-03

### Added
- **Intelligent Tibber-based charging optimization** - Advanced price analysis for optimal charging
- Automatic detection of price increase point (end of cheap period)
- Backward calculation of optimal charging start time based on battery SOC
- Charging plan display in dashboard showing planned start/end times and last calculation
- New `TibberOptimizer` core module for smart charging logic
- Support for configurable price thresholds:
  - `tibber_price_threshold_1h`: Price increase threshold vs previous hour (default 8%)
  - `tibber_price_threshold_3h`: 3-hour block comparison threshold (default 8%)
  - `charge_duration_per_10_percent`: Charging time per 10% SOC (default 18 minutes)
  - `min_soc`: Minimum safety SOC (default 20%)
  - `max_soc`: Maximum target SOC (default 95%)
- Optional Home Assistant input_datetime integration for charging plan visualization
- New API endpoint `/api/charging_plan` for charging schedule information
- Periodic charging plan updates (every 5 minutes)

### Changed
- Auto-optimization mode now uses sophisticated price trend analysis instead of simple price levels
- Controller considers both price trends (1h and 3h windows) and PV forecast
- Charging starts automatically at calculated optimal time
- Charging stops when price increases or battery reaches max SOC
- Enhanced `/api/status` endpoint now includes charging plan information

### Technical
- Ported Home Assistant automation logic to Python for standalone operation
- Added charging plan calculation with timezone-aware datetime handling
- Integration with Home Assistant `input_datetime` helpers (optional)
- Improved error handling for missing/invalid price data
- Fallback behavior when no optimal charging time is found
- Comprehensive logging for all charging decisions
- Manual charging control remains fully functional alongside automatic optimization

## [0.2.7] - 2025-11-03

### Fixed
- Dashboard now displays correct SOC parameters (`auto_safety_soc` and `auto_charge_below_soc` instead of removed `min_soc`/`max_soc`)
- Updated labels: "Sicherheits-SOC" and "Lade-Limit" for better clarity

## [0.2.6] - 2025-11-03

### Changed
- Removed duplicate SOC parameters `min_soc` and `max_soc` (now only using `auto_safety_soc` and `auto_charge_below_soc` for clarity)
- Renamed "Modus" to "Status" in status overview with German labels:
  - "Standby" (statt "automatic")
  - "Lädt (manuell)" (statt "manual_charging")
  - "Lädt (Auto)" (statt "auto_charging")
- Removed redundant "Steuerung" display from status overview

### Removed
- Config parameters `min_soc` and `max_soc` (replaced by clearer `auto_safety_soc` and `auto_charge_below_soc`)

## [0.2.5] - 2025-11-03

### Added
- Automation status display in status overview
- Toggle switch for automation (replaces button)
- Configurable automation parameters:
  - `auto_pv_threshold`: PV forecast threshold (default 5.0 kWh)
  - `auto_charge_below_soc`: Maximum SOC for charging (default 95%)
  - `auto_safety_soc`: Safety minimum SOC (default 20%)
- New API endpoint: `/api/control` with `toggle_automation` action

### Changed
- Automation is now ON by default on startup
- Controller logic uses configurable parameters instead of hardcoded values
- Improved automation status visibility with toggle switch and status indicator
- Button replaced with professional toggle switch for better UX

### UI
- Real-time automation status display (AN/AUS with colored dot)
- Toggle switch shows current state and allows easy on/off control
- Automation parameters now configurable in addon configuration

## [0.2.4] - 2025-11-03

### Fixed
- Charging power slider value now correctly applied when starting charge
- Previously always used max_charge_power, now uses slider value
- Dark mode text visibility significantly improved with white text

### Changed
- Improved dark mode: All text now white (#ffffff) for better readability
- Labels and secondary text in light gray (#cccccc) in dark mode

## [0.2.3] - 2025-11-03

### Added
- Automatic connection test on startup
- Intelligent battery status display with charging/discharging/standby states

### Changed
- Price display now in Cents instead of Euro for better readability
- Removed navigation menu for cleaner UI (Dashboard, Konfiguration, Logs links)
- Removed "Verbindung testen" button - now automatic on startup
- Improved dark mode contrast (darker background, pure white text)

### UI
- Battery power status: "Batterie wird geladen/entladen: xxxx W" or "Batterie in Standby"
- Price display: "XX.XX Cent/kWh" instead of "0.XXXX €/kWh"
- Better visibility in dark mode with improved contrast
- Simplified header with only title

## [0.2.2] - 2025-11-03

### Fixed
- Tibber current price now correctly read from sensor state
- Tibber price level correctly read from German level sensor
- Average price calculation from Tibber attributes working
- PV forecast tomorrow now displays correctly (sum of both roofs)

### Changed
- Simplified Tibber price reading logic (removed complex timezone parsing)
- Controller now supports both German and English price levels
- Added automatic dark/light mode detection
- Light mode is now the default for better readability

### UI
- Automatic dark mode activation when system prefers dark color scheme
- Better contrast in both light and dark mode
- Improved overall readability

## [0.2.1] - 2025-11-03

### Changed
- Update interval reduced from 10s to 2s for more responsive UI
- Improved Tibber price parsing to correctly show current price from hourly price array
- Added support for dual-roof PV systems (separate sensors for each roof orientation)
- PV forecast now sums production from both roof orientations
- Price level strings now use English format (CHEAP, EXPENSIVE, etc.)

### Removed
- SOC synchronization feature removed (min/max SOC should be configured directly in inverter)
- Removed `/api/sync_soc` endpoint
- Removed `set_battery_soc_limits()` method from kostal_api
- Removed SOC sync button from dashboard

### Fixed
- Current electricity price now correctly displayed from Tibber sensor attributes
- PV forecast calculation for systems with multiple roof orientations
- Timezone handling for Tibber price matching

### Technical
- Added `get_state_with_attributes()` method to ha_client for full entity data retrieval
- New PV sensor configuration: `pv_power_now_roof1/2`, `pv_remaining_today_roof1/2`, etc.
- Removed legacy `pv_forecast_sensor` and `consumption_sensor` options

## [0.2.0] - 2025-11-03

### Added
- Live battery power display from Home Assistant sensor
- Battery voltage sensor integration (optional)
- SOC limit synchronization to inverter (min/max SOC)
- Live charging power adjustment during active charging
- Automatic optimization mode based on Tibber price levels
- PV forecast integration for smart charging decisions
- New configuration options for sensors and automation:
  - `battery_power_sensor`: Real-time battery power monitoring
  - `battery_voltage_sensor`: Battery voltage monitoring (optional)
  - `tibber_price_sensor`: Tibber price data
  - `tibber_price_level_sensor`: Price level classification
  - `pv_forecast_sensor`: PV generation forecast
  - `consumption_sensor`: Consumption data
  - `auto_optimization_enabled`: Enable/disable automatic optimization
- New API endpoints:
  - `/api/sync_soc`: Synchronize SOC limits to inverter
  - `/api/adjust_power`: Adjust charging power during active charging
- SOC synchronization button in dashboard

### Changed
- Dashboard now shows real-time battery power
- Power slider can adjust charging power during active charging sessions
- Controller loop now includes intelligent auto-optimization logic
- Improved error handling in API endpoints
- Enhanced sensor integration with fallback mechanisms

### Fixed
- Improved error handling for missing or unavailable sensors
- Better state management for charging modes

## [0.1.1] - 2025-10-XX

### Fixed
- Connection test and CORS issues
- Authentication flow improvements

## [0.1.0] - 2025-10-XX

### Added
- Initial release
- Basic battery control via Kostal API
- Modbus TCP integration for charging control
- Home Assistant integration
- Manual charging control
- Tibber integration for price optimization
- Web dashboard with real-time status
