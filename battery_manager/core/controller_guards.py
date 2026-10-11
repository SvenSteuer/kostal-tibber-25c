#!/usr/bin/env python3
"""
v1.3.5 Hotfix - Entscheidungs-Helfer für den Controller-Loop

Reine Logik ohne I/O (nur Standardbibliothek), damit sie ohne Flask,
Wechselrichter oder Home Assistant unit-getestet werden kann
(tests/test_controller_guards.py).

Zeitvergleiche laufen über time.monotonic()-Werte, die der Aufrufer übergibt:
Eine naive Ortszeit springt am Ende der Sommerzeit (25.10.2026) eine Stunde zurück.
"""

import math
from datetime import datetime, timedelta

# H1: Steht das Add-on so lange ohne gültigen SOC auf extern, wird freigegeben
SOC_INVALID_RELEASE_SECONDS = 300

# H2: Ältere Pläne werden nicht mehr ausgeführt (Planer läuft alle 300 s)
PLAN_MAX_AGE_SECONDS = 900

# H3: SAFETY-Ladung
SAFETY_CONFIRM_READINGS = 3        # aufeinanderfolgende gültige Messungen < auto_safety_soc
SAFETY_CHARGE_POWER_W = 1500       # statt max_charge_power
SAFETY_TARGET_MARGIN_PCT = 3       # laden bis auto_safety_soc + 3
SAFETY_MIN_RUNTIME_SECONDS = 120   # danach blockiert die Hysterese den Stopp nicht mehr

# Sicherheit: Maske für Passwörter/Tokens/API-Keys in GET /api/config und /config
SECRET_MASK = '********'
_SECRET_MARKERS = ('password', 'token', 'secret', 'api_key')


def parse_soc(raw):
    """SOC aus einem HA-State lesen.

    Returns:
        float im Bereich 0-100 oder None, wenn der Wert fehlt oder ungültig ist
        (None, 'unavailable', 'unknown', '', NaN, außerhalb 0-100).
        Ein HTTP-Fehler darf nie als SOC 0 gewertet werden.
    """
    if raw is None or isinstance(raw, bool):
        return None
    try:
        soc = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(soc) or not 0.0 <= soc <= 100.0:
        return None
    return soc


def plan_is_fresh(plan_ts, now_monotonic, max_age_seconds=PLAN_MAX_AGE_SECONDS):
    """True, wenn der Plan höchstens max_age_seconds alt ist (Zeitstempel aus time.monotonic())."""
    if plan_ts is None:
        return False
    return now_monotonic - plan_ts <= max_age_seconds


def safety_charge_needed(current_soc, min_soc, below_count, in_safety_charge,
                         confirm_readings=SAFETY_CONFIRM_READINGS,
                         margin_pct=SAFETY_TARGET_MARGIN_PCT):
    """SAFETY-Entscheidung mit Bestätigung und Marge.

    Args:
        current_soc: gültiger SOC (float)
        min_soc: auto_safety_soc
        below_count: bisherige Anzahl aufeinanderfolgender Messungen < min_soc
        in_safety_charge: läuft bereits eine SAFETY-Ladung?

    Returns:
        (needed, new_below_count)
        - Start erst nach confirm_readings Messungen in Folge unter min_soc
        - eine laufende SAFETY-Ladung läuft weiter bis min_soc + margin_pct
    """
    below_count = below_count + 1 if current_soc < min_soc else 0
    if below_count >= confirm_readings:
        return True, below_count
    if in_safety_charge and current_soc < min_soc + margin_pct:
        return True, below_count
    return False, below_count


def safety_stop_allowed(should_charge, in_safety_charge, seconds_since_switch,
                        min_runtime_seconds=SAFETY_MIN_RUNTIME_SECONDS):
    """True, wenn das Ende einer SAFETY-Ladung an der Hysterese vorbei erfolgen darf.

    Nur nach einer Mindestlaufzeit, damit BMS-Sprünge (z. B. 14 -> 10 % in 20 s)
    kein schnelles Hin- und Herschalten mit persistenten REST-PUTs auslösen.
    """
    return (not should_charge and in_safety_charge and
            seconds_since_switch is not None and
            seconds_since_switch >= min_runtime_seconds)


def prices_plausible(prices, now=None):
    """Plausibilität der Tibber-Preisliste vor dem Planeraufruf (H7).

    Verlangt: jedes startsAt zeitzonenbewusst, aufsteigend und lückenlos im
    Abstand von genau 3600 s (aware-Datetimes -> auch am 25-h-Tag korrekt),
    numerischer Preis 'total' und - falls now übergeben - Abdeckung der
    aktuellen Stunde. 15-min-Preise würde der Legacy-Planer als Stunden lesen,
    sie werden deshalb ebenfalls abgelehnt.

    Args:
        prices: Liste der Tibber-Preis-Dicts (today + tomorrow)
        now: zeitzonenbewusster Zeitpunkt oder None

    Returns:
        (ok, reason)
    """
    if not isinstance(prices, list) or len(prices) < 2:
        return False, "weniger als 2 Preise"

    starts = []
    for p in prices:
        try:
            start = datetime.fromisoformat(str(p['startsAt']).replace('Z', '+00:00'))
        except (KeyError, TypeError, ValueError, AttributeError):
            return False, f"startsAt fehlt oder ungültig: {str(p)[:80]}"
        if start.tzinfo is None or start.utcoffset() is None:
            return False, f"startsAt ohne Zeitzone: {p['startsAt']}"
        total = p.get('total')
        if isinstance(total, bool) or not isinstance(total, (int, float)):
            return False, f"Preis 'total' ungültig bei {p['startsAt']}: {total!r}"
        starts.append(start)

    for a, b in zip(starts, starts[1:]):
        gap = (b - a).total_seconds()
        if gap != 3600:
            return False, (f"Abstand {gap:.0f} s zwischen {a.isoformat()} und "
                           f"{b.isoformat()} (erwartet 3600 s)")

    if now is not None and not (starts[0] <= now < starts[-1] + timedelta(hours=1)):
        return False, (f"aktuelle Stunde nicht abgedeckt "
                       f"({starts[0].isoformat()} - {starts[-1].isoformat()})")

    return True, f"{len(starts)} Stundenpreise lückenlos"


def is_secret_key(key):
    """True für Config-Keys mit Passwörtern, Tokens oder API-Keys."""
    return isinstance(key, str) and any(m in key.lower() for m in _SECRET_MARKERS)


def mask_secrets(cfg):
    """Kopie der Config mit maskierten Secrets (leere Werte bleiben leer)."""
    masked = dict(cfg)
    for key, value in cfg.items():
        if is_secret_key(key) and value not in (None, ''):
            masked[key] = SECRET_MASK
    return masked


def drop_masked_secrets(new_cfg):
    """Secrets, die unverändert als Maske zurückkommen, aus einem POST entfernen.

    So überschreibt das Speichern der Konfigurationsseite die gespeicherten
    Passwörter/Keys nicht mit dem Maskenwert.
    """
    return {k: v for k, v in new_cfg.items()
            if not (is_secret_key(k) and v == SECRET_MASK)}
