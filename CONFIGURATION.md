# Konfigurations-Anleitung

## Wichtige Parameter erklärt

### Battery SOC Sensor

**Parameter:** `battery_soc_sensor`
**Beispiel:** `sensor.zwh8_8500_battery_soc`

**Wichtig:** Dieser Sensor muss korrekt konfiguriert sein! Das Addon liest daraus den aktuellen Batterie-Ladestand (SOC).

**Wo finde ich meinen Sensor?**
1. Home Assistant → Entwicklerwerkzeuge → Zustände
2. Suche nach "battery" oder "soc"
3. Finde deinen Batterie-SOC-Sensor (sollte Werte zwischen 0-100% liefern)

### Auto-Optimierungs-Parameter

#### `auto_safety_soc` (Standard: 20%)
**Bedeutung:** Sicherheits-Minimum SOC
**Funktion:** Lädt **SOFORT** wenn der SOC unter diesen Wert fällt, unabhängig von Preisen oder Zeiten.

**Beispiel:**
- Einstellung: `auto_safety_soc: 10`
- Wenn SOC auf 9% fällt → Sofortige Ladung startet!

#### `auto_charge_below_soc` (Standard: 95%)
**Bedeutung:** Ziel-SOC für geplante Ladung
**Funktion:** Die intelligente Ladesteuerung lädt **BIS ZU** diesem SOC-Wert.

**ACHTUNG:** Der Name ist verwirrend! Es bedeutet **NICHT** "lade nur wenn unter X%", sondern **"lade BIS X%"**.

**Beispiel:**
- Einstellung: `auto_charge_below_soc: 95`
- Geplante Ladung: Lädt bis 95% erreicht sind
- Wenn SOC bereits bei 96% → Keine Ladung

#### `auto_pv_threshold` (Standard: 5.0 kWh)
**Bedeutung:** PV-Schwelle
**Funktion:** Lädt nur aus dem Netz wenn weniger als X kWh PV-Produktion erwartet wird.

**Beispiel:**
- Einstellung: `auto_pv_threshold: 5.0`
- Erwartete PV heute: 12 kWh → Keine Netzladung (PV reicht)
- Erwartete PV heute: 3 kWh → Netzladung möglich (PV zu wenig)

### Tibber Smart Charging Parameter

#### `tibber_price_threshold_1h` (Standard: 8%)
**Bedeutung:** Preisanstieg zur vorherigen Stunde
**Funktion:** Erkennt Preisanstieg wenn aktueller Preis > Preis vor 1h * (1 + Schwelle)

**Beispiel:**
- Einstellung: `tibber_price_threshold_1h: 8`
- Preis vor 1h: 20 Cent/kWh
- Aktueller Preis: 22 Cent/kWh
- Anstieg: 10% → Trigger! (> 8%)

#### `tibber_price_threshold_3h` (Standard: 8%)
**Bedeutung:** 3-Stunden-Block Vergleich
**Funktion:** Vergleicht Summe der letzten 3h mit Summe der nächsten 3h

**Beispiel:**
- Einstellung: `tibber_price_threshold_3h: 8`
- Letzte 3h Summe: 60 Cent
- Nächste 3h Summe: 72 Cent
- Anstieg: 20% → Trigger! (> 8%)

#### `charge_duration_per_10_percent` (Standard: 18 Minuten)
**Bedeutung:** Ladedauer pro 10% SOC
**Funktion:** Zur Berechnung des Ladebeginns

**Beispiel:**
- Einstellung: `charge_duration_per_10_percent: 18`
- Benötigte Ladung: 50% (von 40% auf 90%)
- Benötigte Zeit: 5 × 18 = 90 Minuten

**Wie ermitteln?**
1. Starte Ladung bei bekanntem SOC
2. Stoppe nach 10% Zunahme
3. Miss die Dauer
4. Konfiguriere den Wert

## Zusammenfassung der Ladelogik

### Sofort-Ladung (Sicherheit)
```
Wenn: SOC < auto_safety_soc
Dann: Lade sofort (unabhängig von Preis/Zeit)
```

### Geplante Ladung (Tibber-optimiert)
```
1. Analysiere Tibber-Preise (heute + morgen)
2. Finde optimalen Ladeend-Zeitpunkt (wenn Preis steigt)
3. Berechne Ladebeginn rückwärts (basierend auf SOC-Differenz)
4. Lade von [Ladebeginn] bis [Ladeende] oder SOC >= auto_charge_below_soc
```

### Beispiel-Szenario
```
Aktueller SOC: 45%
auto_charge_below_soc: 90%
auto_safety_soc: 20%
charge_duration_per_10_percent: 18 min

Optimaler Ladeend-Zeitpunkt: 04:00 Uhr (Preis steigt dann)
Benötigte Ladung: 90% - 45% = 45%
Benötigte Zeit: 4.5 × 18 = 81 Minuten
Ladebeginn: 04:00 - 81min = 02:39 Uhr

→ Lädt von 02:39 bis 04:00 (oder bis 90% erreicht)
```

## Häufige Fehler

### "SOC wird nicht aktualisiert"
**Problem:** `battery_soc_sensor` ist falsch konfiguriert
**Lösung:** Prüfe den Sensor-Namen in Home Assistant

### "Lädt nicht zur geplanten Zeit"
**Mögliche Ursachen:**
1. SOC bereits >= `auto_charge_below_soc`
2. Genug PV erwartet (> `auto_pv_threshold`)
3. Geplante Zeit noch nicht erreicht

**Lösung:** Prüfe die Logs für genaue Gründe

### "Lädt sofort, obwohl Preis hoch"
**Ursache:** SOC < `auto_safety_soc`
**Lösung:** Das ist gewolltes Verhalten (Sicherheitsfunktion)
