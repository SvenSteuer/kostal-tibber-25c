# Kostal Battery Manager

Ein professionelles Home Assistant Add-on für die intelligente Batteriesteuerung von Kostal Plenticore Plus Wechselrichtern mit dynamischer Preisoptimierung (Tibber, Awattar, etc.).

## 🎯 Features

- ✅ **Direkte Kostal-Steuerung:** Umgeht den Firmware-Bug beim Timeout der externen Steuerung
- ✅ **Tibber-Integration:** Automatische Optimierung basierend auf dynamischen Strompreisen
- ✅ **PV-Forecast:** Integration von Forecast.Solar für intelligente Ladeplanung
- ✅ **Benutzerfreundliche GUI:** Moderne Web-Oberfläche zur Konfiguration und Steuerung
- ✅ **Multi-Instanz:** Unterstützt mehrere Wechselrichter parallel
- ✅ **Open Source:** Community-driven Development

## 📋 Voraussetzungen

- Home Assistant OS (empfohlen) oder Home Assistant Supervised
- Kostal Plenticore Plus Wechselrichter mit Firmware 01.30.x oder neuer
- Pylontech Batterie (Force H2 oder kompatibel)
- Installer-Passwort und Master-Passwort für den Wechselrichter
- (Optional) Tibber Integration in Home Assistant
- (Optional) Forecast.Solar Integration in Home Assistant

## 🚀 Installation

### Methode 1: Über eigenes Repository (empfohlen für Testing)

1. **Repository in Home Assistant hinzufügen:**
   - Einstellungen → Add-ons → Add-on Store → ⋮ (oben rechts) → Repositories
   - Fügen Sie hinzu: `https://github.com/IHR_USERNAME/kostal-battery-manager`

2. **Add-on installieren:**
   - Suchen Sie nach "Kostal Battery Manager"
   - Klicken Sie auf "Installieren"

3. **Konfigurieren:**
   - Öffnen Sie die Add-on Konfiguration
   - Tragen Sie Ihre Wechselrichter-Daten ein
   - Speichern und starten Sie das Add-on

### Methode 2: Lokale Installation (für Entwicklung)

1. **Dateien kopieren:**
   ```bash
   cd /addons
   git clone https://github.com/IHR_USERNAME/kostal-battery-manager.git
   ```

2. **In Home Assistant:**
   - Einstellungen → Add-ons → Add-on Store → ⋮ → "Lokale Add-ons überprüfen"
   - "Kostal Battery Manager" sollte nun erscheinen

## ⚙️ Konfiguration

### Pflichtfelder:

```yaml
inverter_ip: "192.168.80.76"              # IP-Adresse des Wechselrichters
inverter_port: 1502                       # Modbus Port (Standard: 1502)
installer_password: "ihr_passwort"        # Installer-Passwort
master_password: "ihr_master_passwort"    # Master-Passwort
max_charge_power: 3900                    # Max. Ladeleistung in Watt
battery_capacity: 10.6                    # Batteriekapazität in kWh
```

### Optionale Felder:

```yaml
min_soc: 20                               # Minimum SOC (%)
max_soc: 95                               # Maximum SOC (%)
log_level: "info"                         # Log Level (debug|info|warning|error)
control_interval: 30                      # Steuerungs-Intervall in Sekunden
enable_tibber_optimization: true          # Tibber-Optimierung aktivieren
price_threshold: 0.85                     # Preisschwelle (85% des Durchschnitts)
battery_soc_sensor: "sensor.zwh8_8500_battery_soc"  # HA Batterie SOC Sensor
forecast_sensor_1: "sensor.energy_production_today"  # PV Forecast Sensor 1
forecast_sensor_2: "sensor.energy_production_today_2"  # PV Forecast Sensor 2
```

## 🎮 Verwendung

### Web-GUI

Nach der Installation ist das Add-on über das Home Assistant Menü erreichbar:
- **Dashboard:** Zeigt aktuellen Status, Batterie-SOC, Preise
- **Konfiguration:** Alle Einstellungen anpassen
- **Logs:** Live-Logs zur Fehlersuche

### Manuelle Steuerung

Im Dashboard können Sie:
- ⏯️ **Laden starten:** Batterie mit eingestellter Leistung laden
- ⏹️ **Laden stoppen:** Zurück zur internen Steuerung
- 🔄 **Automatik:** Tibber-basierte Optimierung aktivieren

### Automatik-Modus

Im Automatik-Modus:
1. Liest das Add-on die aktuellen Tibber-Preise
2. Vergleicht mit Durchschnittspreis und Schwelle
3. Prüft PV-Forecast für heute
4. Entscheidet automatisch wann geladen wird
5. Optimiert Ladeleistung basierend auf SOC

## 🔧 Troubleshooting

### Problem: Add-on startet nicht

**Lösung:**
- Prüfen Sie die Logs: Add-on → Log Tab
- Verifizieren Sie die Konfiguration
- Stellen Sie sicher, dass alle Passwörter korrekt sind

### Problem: Keine Verbindung zum Wechselrichter

**Lösung:**
- Prüfen Sie IP-Adresse und Port
- Testen Sie: `ping 192.168.80.76`
- Prüfen Sie ob Modbus TCP am Wechselrichter aktiviert ist
- Firewall-Regeln prüfen

### Problem: Externe Steuerung funktioniert nicht

**Lösung:**
- Prüfen Sie ob "Battery:ExternControl" im Wechselrichter aktiviert ist
- Kostal Webinterface: Service → Battery → ExternControl = "External via protocol (Modbus TCP)"
- Timeout auf 60 Sekunden setzen

### Problem: Batterieladung startet nicht

**Lösung:**
- Prüfen Sie Battery SOC (muss < max_soc sein)
- Prüfen Sie ob genug PV-Leistung verfügbar ist
- Schauen Sie in die Logs für Fehlermeldungen
- Testen Sie die Verbindung im Dashboard

## 📊 Home Assistant Integration

Das Add-on kann mit folgenden Home Assistant Integrationen zusammenarbeiten:

- **Tibber:** Dynamische Strompreise
- **Forecast.Solar:** PV-Ertragsprognose
- **Kostal Plenticore:** Sensoren für Battery SOC, Power, etc.

## 🛡️ Sicherheitshinweise

⚠️ **WICHTIG:**

- Dieses Add-on greift direkt auf Ihren Wechselrichter zu
- Falsche Einstellungen können die Batterie beschädigen
- Verwenden Sie nur getestete Werte
- Beachten Sie die Garantiebedingungen Ihres Herstellers
- Erstellen Sie regelmäßige Backups Ihrer Home Assistant Konfiguration

## 📖 Dokumentation

### Kostal API

Das Add-on nutzt die undokumentierte REST API von Kostal:
- Authentifizierung via PBKDF2 + AES
- Session-Management
- Setting "Battery:ExternControl" auf 0 (intern) oder 2 (extern)

### Modbus Register

- **Register 1034:** Battery charge power setpoint (Float32)
  - Negativ = Laden (z.B. -3900 = 3900W laden)
  - Positiv = Entladen (z.B. 2000 = 2000W entladen)
  - 0 = Automatischer Modus

- **Register 1066:** Battery Power (Float32, read-only)
- **Register 1068:** Battery SOC (Float32, read-only)

## 🤝 Beitragen

Beiträge sind willkommen! 

1. Fork das Repository
2. Erstelle einen Feature Branch (`git checkout -b feature/AmazingFeature`)
3. Commit deine Änderungen (`git commit -m 'Add some AmazingFeature'`)
4. Push zum Branch (`git push origin feature/AmazingFeature`)
5. Öffne einen Pull Request

## 📝 Changelog

### Version 0.1.0 (TBD)

- ✨ Erste öffentliche Version
- ✅ Basis-Funktionalität für Kostal-Steuerung
- ✅ Tibber-Integration
- ✅ Web-GUI
- ✅ Logging und Monitoring

## 📄 Lizenz

Dieses Projekt ist unter der MIT Lizenz lizenziert - siehe [LICENSE](LICENSE) Datei für Details.

## 🙏 Credits

- **Kilian Knoll:** Für die ursprüngliche batctl.py Implementierung der Kostal REST API
- **Home Assistant Community:** Für die hervorragende Plattform
- **Kostal Solar Electric:** Für den Wechselrichter

## 📧 Support

Bei Fragen oder Problemen:
- GitHub Issues: [Issues](https://github.com/IHR_USERNAME/kostal-battery-manager/issues)
- Home Assistant Community: [Forum Thread](LINK)

---

**Made with ❤️ for Home Assistant**
