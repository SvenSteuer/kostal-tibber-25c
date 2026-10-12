#!/usr/bin/env python3
"""
v1.3.7 - Auswertung „Nutzen PV + Akku" (nur Anzeige, Controller und Planer unberührt)

Je Stunde [h, h+1), gezählt werden nur Stunden, für die alle vier Werte vorliegen:
- Kosten ohne PV & Akku   = Hauslast_h × Preis_h      (KSEM-Hauslast inkl. Pool)
- Tatsächliche Netzkosten = Bezug_h × Preis_h − Einspeisung_h × Einspeisevergütung
- Nutzen PV + Akku        = Kosten ohne PV & Akku − tatsächliche Netzkosten

Quellen: HA-Langzeitstatistik, ein Websocket-Aufruf für alle vier Sensoren (KSEM-Zähler:
Differenzen der Spalte sum; Preis: Spalte state, denn sensor.tibber_prices hat
state_class total und damit kein mean, sonst mean; Hauslast: mean). Fallbacks: load_hourly
(v1.3.6) und /api/history/period, die History nur für die letzten HISTORY_FALLBACK_DAYS
Tage (länger hält der Recorder keine States). Fehlende Preise je Stunde: History, dann
Tibber-Attribute today/yesterday, dann der letzte bekannte Preis (max.
PRICE_CARRY_MAX_HOURS), nie ein fester Ersatzpreis.

Den Mehrwert des Add-ons gegenüber der Kostal-internen Regelung misst das nicht, dafür
fehlt ein Vergleichslauf. Er folgt mit dem neuen Planer.
"""

import logging
import math
from bisect import bisect_left, bisect_right
from datetime import datetime, time, timedelta, timezone

from .data_collection import (
    CALIBRATION_DB_PATH, HISTORY_CHUNK_HOURS, floor_hour, hourly_means_from_states,
    history_hourly_means, parse_history_states, read_load_hourly, _parse_stat_start)

logger = logging.getLogger(__name__)

HOUR = timedelta(hours=1)
DEFAULT_FEED_IN_EUR_PER_KWH = 0.066
MAX_FEED_IN_EUR_PER_KWH = 1.0          # darüber war es vermutlich eine Eingabe in ct
DEFAULT_LOAD_ENTITY = 'sensor.ksem_home_consumption'
SUM_LOOKBACK = timedelta(hours=24)     # letzte sum-Zeile vor dem Zeitraum als Startwert
HISTORY_FALLBACK_DAYS = 10             # Recorder-Standard purge_keep_days
COUNTER_END_MARGIN = timedelta(minutes=1)
PRICE_CARRY_MAX_HOURS = 24

_SUM_KEYS = ('cost_without', 'import_cost', 'export_revenue',
             'import_kwh', 'export_kwh', 'load_kwh', 'hours', 'hours_expected')
_SERIES_KEYS = ('price', 'import_kwh', 'export_kwh', 'load_kwh')


# ==============================================================================
# Reine Funktionen
# ==============================================================================

def parse_feed_in_tariff(value, default=DEFAULT_FEED_IN_EUR_PER_KWH):
    """Einspeisevergütung in €/kWh; fehlend, ungültig, negativ oder > 1 €/kWh -> default."""
    try:
        tariff = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(tariff) or tariff < 0 or tariff > MAX_FEED_IN_EUR_PER_KWH:
        logger.warning(f"Feed-in tariff {value!r} implausible - using {default} EUR/kWh")
        return default
    return tariff


def hour_starts(start, end):
    """Volle Stunden [h, h+1) in [start, end) als aware UTC-datetimes."""
    start = floor_hour(start)
    n_hours = max(0, int((end - start).total_seconds() // 3600))
    return [start + k * HOUR for k in range(n_hours)]


def day_bounds(day, tz=None):
    """[Mitternacht, nächste Mitternacht) Ortszeit; 23 bzw. 25 h an den Umstellungstagen.

    tz None = Zeitzone des Systems (wie datetime.now().astimezone() im Rest des Add-ons).
    """
    def midnight(d):
        naive = datetime.combine(d, time.min)
        return naive.replace(tzinfo=tz) if tz else naive.astimezone()
    return midnight(day), midnight(day + timedelta(days=1))


def local_date(hour, tz=None):
    return (hour.astimezone(tz) if tz else hour.astimezone()).date()


def _delta(start_value, end_value):
    """Zählerdifferenz; None, wenn ein Stand fehlt oder der Zähler zurückläuft (Tausch/Reset)."""
    if start_value is None or end_value is None:
        return None
    delta = end_value - start_value
    return delta if delta >= 0 else None


def counter_deltas(points, hours):
    """Stundenwerte eines kumulierten Zählers aus der History.

    Stand am Stundenbeginn und am Stundenende ist jeweils der letzte Wert <= Grenze (ein
    Wert gilt bis zur nächsten Änderung). Eine Stunde ohne Zähleränderung ergibt 0, nicht
    eine negative Differenz.

    Args:
        points: [(aware datetime, float)], chronologisch, nur gültige Werte
        hours: Stundenanfänge

    Returns:
        [kWh oder None]: None ohne bekannten Stand vor dem Stundenbeginn bzw. bei Reset
    """
    times = [ts for ts, _ in points]

    def value_at(t):
        i = bisect_right(times, t)
        return points[i - 1][1] if i else None

    return [_delta(value_at(h), value_at(h + HOUR)) for h in hours]


def deltas_from_sums(sums, hours):
    """Stundenwerte aus der Statistik-Spalte sum (Zählerstand am Ende der Stunde).

    Wert der Stunde h = sum(h) − sum der letzten Zeile vor h. Fehlt eine Zeile (Sensor
    ausgefallen), landet die Energie in der nächsten vorhandenen Stunde, wie bei 'change'.

    Args:
        sums: {Stundenbeginn: sum oder None}
        hours: Stundenanfänge

    Returns:
        [kWh oder None]
    """
    known = sorted((h, s) for h, s in sums.items() if s is not None)
    times = [h for h, _ in known]
    by_hour = dict(known)
    result = []
    for h in hours:
        i = bisect_left(times, h)
        result.append(_delta(known[i - 1][1] if i else None, by_hour.get(h)))
    return result


def prices_from_tibber_attributes(attributes, hours):
    """{Stundenbeginn: €/kWh} aus den Attributen yesterday/today (Liste mit startsAt/total).

    Viertelstundenpreise werden je Stunde gemittelt. Nur Stunden aus hours.
    """
    wanted = set(hours)
    buckets = {}
    for key in ('yesterday', 'today'):
        for entry in (attributes or {}).get(key) or []:
            try:
                starts = datetime.fromisoformat(str(entry['startsAt']).replace('Z', '+00:00'))
                price = float(entry['total'])
            except (KeyError, TypeError, ValueError):
                continue
            if starts.tzinfo is None:
                continue
            hour = floor_hour(starts)
            if hour in wanted:
                buckets.setdefault(hour, []).append(price)
    return {hour: sum(prices) / len(prices) for hour, prices in buckets.items()}


def fill_price_gaps(prices, max_gap=PRICE_CARRY_MAX_HOURS):
    """Fehlende Stundenpreise mit dem letzten bekannten Preis füllen.

    Führende Lücken bekommen den ersten bekannten Preis. Höchstens max_gap Stunden je
    Lücke, darüber bleibt None (die Stunde zählt dann nicht).

    Returns:
        (Liste, Anzahl gefüllter Stunden)
    """
    filled = list(prices)
    count = 0
    last, gap = None, 0
    for k, price in enumerate(prices):
        if price is not None:
            last, gap = price, 0
            continue
        gap += 1
        if last is not None and gap <= max_gap:
            filled[k] = last
            count += 1
    first = next((k for k, price in enumerate(prices) if price is not None), None)
    if first is not None:
        for k in range(max(0, first - max_gap), first):
            filled[k] = prices[first]
            count += 1
    return filled, count


def period_totals(series, feed_in, indices=None):
    """Rohsummen über die Stunden indices (Standard: alle), die vollständig sind.

    Args:
        series: dict mit 'hours' und den Listen 'price', 'import_kwh', 'export_kwh', 'load_kwh'
        feed_in: Einspeisevergütung €/kWh

    Returns:
        dict (_SUM_KEYS + 'until' = Ende der letzten gezählten Stunde) oder None ohne
        vollständige Stunde
    """
    hours = series['hours']
    totals = dict.fromkeys(_SUM_KEYS, 0.0)
    totals['hours'] = totals['hours_expected'] = 0
    totals['until'] = None
    for k in (range(len(hours)) if indices is None else indices):
        totals['hours_expected'] += 1
        price, imp, exp, load = (series[key][k] for key in _SERIES_KEYS)
        if price is None or imp is None or exp is None or load is None:
            continue
        totals['hours'] += 1
        totals['cost_without'] += load * price
        totals['import_cost'] += imp * price
        totals['export_revenue'] += exp * feed_in
        totals['import_kwh'] += imp
        totals['export_kwh'] += exp
        totals['load_kwh'] += load
        end = hours[k] + HOUR
        if totals['until'] is None or end > totals['until']:
            totals['until'] = end
    return totals if totals['hours'] else None


def merge_totals(parts):
    """Rohsummen mehrerer Zeiträume addieren (None-Einträge werden übersprungen)."""
    parts = [p for p in parts if p]
    if not parts:
        return None
    merged = {key: sum(p[key] for p in parts) for key in _SUM_KEYS}
    merged['until'] = max(p['until'] for p in parts)
    return merged


def saved_percent(saved, cost_without):
    """Nutzen in % der Kosten ohne PV & Akku, nach oben auf 100 % begrenzt.

    Über 100 % hieße: die Einspeisung bringt mehr, als der Netzbezug kostet (tatsächliche
    Netzkosten < 0). Das wird als 100 % mit capped = True ausgegeben.

    Returns:
        (Prozent, capped)
    """
    if cost_without <= 0:
        return 0.0, False
    percent = saved / cost_without * 100.0
    if percent > 100.0:
        return 100.0, True
    return percent, False


def empty_result():
    """Platzhalter für Zeiträume ohne Daten (Schlüssel wie bis v1.3.6, Werte 0)."""
    return {'available': False, 'cost_without': 0.0, 'cost_with': 0.0, 'saved': 0.0,
            'saved_percent': 0.0, 'saved_percent_capped': False}


def present(totals, tz=None):
    """Rohsummen -> gerundetes Ergebnis für API und Templates (None bleibt None)."""
    if not totals:
        return None
    cost_with = totals['import_cost'] - totals['export_revenue']
    saved = totals['cost_without'] - cost_with
    percent, capped = saved_percent(saved, totals['cost_without'])
    until = None
    if totals.get('until'):
        local = totals['until'].astimezone(tz) if tz else totals['until'].astimezone()
        until = '24:00' if (local.hour, local.minute) == (0, 0) else local.strftime('%H:%M')
    return {
        'available': True,
        'cost_without': round(totals['cost_without'], 2),       # Kosten ohne PV & Akku
        'cost_with': round(cost_with, 2),                       # tatsächliche Netzkosten
        'saved': round(saved, 2),                               # Nutzen PV + Akku
        'saved_percent': round(percent, 1),
        'saved_percent_capped': capped,
        'import_cost': round(totals['import_cost'], 2),
        'export_revenue': round(totals['export_revenue'], 2),
        'import_kwh': round(totals['import_kwh'], 2),
        'export_kwh': round(totals['export_kwh'], 2),
        'load_kwh': round(totals['load_kwh'], 2),
        'hours': int(totals['hours']),
        'hours_expected': int(totals['hours_expected']),
        'complete': totals['hours'] == totals['hours_expected'],
        'until': until,
    }


def daily_totals(series, feed_in, tz=None):
    """{Datum (Ortszeit): Rohsummen oder None} für jeden Tag der Reihe, chronologisch."""
    by_day = {}
    for k, hour in enumerate(series['hours']):
        by_day.setdefault(local_date(hour, tz), []).append(k)
    return {day: period_totals(series, feed_in, indices) for day, indices in by_day.items()}


# ==============================================================================
# Datenabruf
# ==============================================================================

class SavingsData:
    """Stundenreihen für Preis, Netzbezug, Einspeisung und Hauslast aus Home Assistant."""

    def __init__(self, ha_client, import_entity, export_entity, price_entity,
                 load_entity=DEFAULT_LOAD_ENTITY, load_db_path=CALIBRATION_DB_PATH,
                 history_days=HISTORY_FALLBACK_DAYS):
        self.ha_client = ha_client
        self.import_entity = import_entity
        self.export_entity = export_entity
        self.price_entity = price_entity
        self.load_entity = load_entity
        self.load_db_path = load_db_path
        self.history_days = history_days

    def hourly(self, start, end, now=None):
        """Stundenreihen für die vollen Stunden in [start, end).

        Returns:
            dict: 'hours' (UTC), 'price' (€/kWh), 'import_kwh', 'export_kwh', 'load_kwh'
                  (Listen, None = keine Daten), 'sources' {Reihe: Quelle oder None},
                  'price_filled' (Stunden mit übernommenem Preis)
        """
        hours = hour_starts(start, end)
        series = {'hours': hours, 'sources': {}, 'price_filled': 0}
        for key in _SERIES_KEYS:
            series[key] = [None] * len(hours)
        if not hours:
            return series
        now = now or datetime.now(timezone.utc)
        end = hours[-1] + HOUR
        # History nur für die letzten Tage (ältere States hat der Recorder gelöscht)
        history_start = max(hours[0], floor_hour(now - timedelta(days=self.history_days)))
        stats = self._statistics(hours[0], end)

        for key, entity in (('import_kwh', self.import_entity), ('export_kwh', self.export_entity)):
            rows = stats.get(entity)
            if rows:
                series[key] = deltas_from_sums({h: r.get('sum') for h, r in rows.items()}, hours)
                series['sources'][key] = 'statistics'
            else:
                series[key], series['sources'][key] = self._counter_history(entity, hours, history_start)

        series['load_kwh'], series['sources']['load_kwh'] = self._load(stats.get(self.load_entity),
                                                                       hours, history_start)
        prices, series['sources']['price'] = self._prices(stats.get(self.price_entity),
                                                          hours, history_start)
        series['price'], series['price_filled'] = fill_price_gaps(prices)

        logger.info(f"Savings data {hours[0]:%Y-%m-%d %H:%M}Z - {end:%Y-%m-%d %H:%M}Z: "
                    f"sources {series['sources']}, {series['price_filled']} hours with carried price")
        return series

    def _statistics(self, start, end):
        """{entity: {Stundenbeginn: Zeile}} aus der Langzeitstatistik; {} wenn nicht verfügbar."""
        ids = list(dict.fromkeys(e for e in (self.import_entity, self.export_entity,
                                             self.price_entity, self.load_entity) if e))
        try:
            result = self.ha_client.get_statistics_during_period(
                ids, start - SUM_LOOKBACK, end, period='hour', types=['sum', 'mean', 'state'],
                units={'energy': 'kWh', 'power': 'W'})
        except Exception as e:
            logger.warning(f"Savings: HA statistics not available ({e}) - using history fallback")
            return {}
        parsed = {}
        for entity, rows in (result or {}).items():
            parsed[entity] = {_parse_stat_start(row['start']): row
                              for row in rows or [] if row.get('start') is not None}
        return parsed

    def _history_hours(self, hours, history_start):
        return [h for h in hours if h >= history_start]

    def _counter_history(self, entity, hours, history_start):
        """Zähler-Stundenwerte aus der History (in Blöcken, nur die jüngsten Tage)."""
        window = self._history_hours(hours, history_start)
        if not window:
            return [None] * len(hours), None
        points = []
        # HA liefert nur States mit last_updated < end_time: etwas über das Ende hinaus abfragen,
        # damit ein Zählerstand genau zur letzten Stundengrenze mitkommt (gezählt wird nur <= Grenze)
        chunk_start, end = window[0], window[-1] + HOUR + COUNTER_END_MARGIN
        while chunk_start < end:
            chunk_end = min(end, chunk_start + timedelta(hours=HISTORY_CHUNK_HOURS))
            history = self.ha_client.get_history(entity, chunk_start, chunk_end, minimal_response=True)
            points.extend((ts, v) for ts, v in parse_history_states(history) if v is not None)
            chunk_start = chunk_end
        points.sort(key=lambda p: p[0])
        values = [None] * (len(hours) - len(window)) + counter_deltas(points, window)
        return values, ('history' if any(v is not None for v in values) else None)

    def _load(self, rows, hours, history_start):
        """Hauslast in kWh je Stunde (Mittelwert in W / 1000)."""
        if rows:
            means = {h: r.get('mean') for h, r in rows.items()}
            source = 'statistics'
        else:
            means = read_load_hourly(hours[0], hours[-1] + HOUR, self.load_entity, self.load_db_path)
            source = 'load_hourly' if any(v is not None for v in means.values()) else None
            missing = [h for h in self._history_hours(hours, history_start) if means.get(h) is None]
            if missing:
                history = history_hourly_means(self.ha_client, self.load_entity,
                                               missing[0], missing[-1] + HOUR)
                for h in missing:
                    mean = history.get(h, (None, None))[0]
                    if mean is not None:
                        means[h] = mean
                        source = source or 'history'
        values = [means[h] / 1000.0 if means.get(h) is not None else None for h in hours]
        return values, source

    def _prices(self, rows, hours, history_start):
        """Stundenpreise (€/kWh) mit Lücken (None); gefüllt wird erst in fill_price_gaps.

        Je Stunde: Statistik-Spalte state (Preis der Stunde; der Tibber-Sensor hat state_class
        total, also sum/state und kein mean), sonst mean (Sensor mit state_class measurement),
        sonst zeitgewichtet aus der History (nur jüngste Tage), sonst Tibber-Attribute.

        Returns:
            (Preise, Quellen wie 'statistics+history' oder None)
        """
        rows = rows or {}
        prices = []
        for h in hours:
            row = rows.get(h) or {}
            prices.append(row.get('state') if row.get('state') is not None else row.get('mean'))
        used = ['statistics'] if any(p is not None for p in prices) else []

        missing = [k for k, h in enumerate(hours) if prices[k] is None and h >= history_start]
        if missing:
            first, last = hours[missing[0]], hours[missing[-1]] + HOUR
            history = self.ha_client.get_history(self.price_entity, first, last, minimal_response=True)
            means = hourly_means_from_states(parse_history_states(history), first, last, min_coverage=0.0)
            filled = 0
            for k in missing:
                mean = means.get(hours[k], (None, None))[0]
                if mean is not None:
                    prices[k] = mean
                    filled += 1
            if filled:
                used.append('history')

        if any(p is None for p in prices):
            attributes = None
            try:
                attributes = self.ha_client.get_attributes(self.price_entity)
            except Exception as e:
                logger.debug(f"Savings: price attributes not available ({e})")
            from_attributes = prices_from_tibber_attributes(attributes, hours)
            filled = 0
            for k, h in enumerate(hours):
                if prices[k] is None and h in from_attributes:
                    prices[k] = from_attributes[h]
                    filled += 1
            if filled:
                used.append('attributes')
        return prices, '+'.join(used) or None
