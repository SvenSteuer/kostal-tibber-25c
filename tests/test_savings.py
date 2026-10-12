#!/usr/bin/env python3
"""
v1.3.7 - Unit-Tests für „Nutzen PV + Akku" (nur Standardbibliothek).

Aufruf aus dem Repo-Root:
    python3 -m unittest discover -s tests -v

Die Tagesfixture (06./07./10./11.10.2026) ist synthetisch: plausible Stundenwerte für Preis,
Hauslast, Netzbezug und Einspeisung, so gewählt, dass die Formel die am 12.10. aus der
HA-Stundenstatistik nachgerechneten Referenzwerte ergibt. Die Preiszeilen der Statistik sind
wie bei sensor.tibber_prices (state_class total): sum/state, kein mean.
"""

import os
import sqlite3
import sys
import tempfile
import types
import unittest
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'battery_manager'))

# ha_client importiert 'requests'. Lokal ohne das Paket genügt ein Platzhalter, weil alle
# Netzwerkaufrufe in den Tests gemockt werden.
try:
    import requests  # noqa: F401
except ImportError:
    _requests = types.ModuleType('requests')
    _requests.get = _requests.put = _requests.post = None
    sys.modules['requests'] = _requests

from core import savings  # noqa: E402
from core.data_collection import LoadHourlyCollector  # noqa: E402
from core.savings import (  # noqa: E402
    SavingsData, counter_deltas, daily_totals, day_bounds, deltas_from_sums, fill_price_gaps,
    hour_starts, merge_totals, parse_feed_in_tariff, period_totals, present,
    prices_from_tibber_attributes)

BERLIN = ZoneInfo('Europe/Berlin')
UTC = timezone.utc
H = timedelta(hours=1)
FEED_IN = 0.066
TOLERANCE = 0.15            # Abnahmekriterium: ±0,15 € gegenüber den HA-Referenzwerten

IMPORT = 'sensor.ksem_total_active_energy_from_grid'
EXPORT = 'sensor.ksem_total_active_energy_to_grid'
PRICE_ENTITY = 'sensor.tibber_prices'
LOAD = 'sensor.ksem_home_consumption'

# Tibber-Stundenpreise (€/kWh), für alle Fixture-Tage gleich
PRICE = [0.301, 0.296, 0.292, 0.290, 0.291, 0.298, 0.322, 0.361, 0.372, 0.335, 0.298, 0.276,
         0.268, 0.265, 0.270, 0.284, 0.318, 0.368, 0.405, 0.398, 0.362, 0.334, 0.318, 0.305]

# Stundenwerte in kWh. Hauslast inkl. Pool (10-16 Uhr), Akku leer am Morgen, voll ab Mittag,
# danach Einspeisung, abends leer.
FIXTURE = {
    date(2026, 10, 6): {
        'ref_with': 1.61, 'ref_without': 10.53,
        'load': [0.715, 0.553, 0.519, 0.507, 0.53, 0.634, 1.095, 1.499, 1.211, 1.038, 2.133, 2.248,
                 2.364, 2.248, 2.191, 2.075, 1.384, 1.787, 1.96, 1.845, 1.556, 1.211, 0.98, 0.807],
        'imp': [0.715, 0.553, 0.519, 0.507, 0.53, 0.634, 1.095, 1.499, 0.329, 0.0, 0.0, 0.0,
                0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.788, 0.98, 0.807],
        'exp': [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.001,
                3.585, 3.952, 3.758, 3.141, 2.676, 0.789, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
    },
    date(2026, 10, 7): {
        'ref_with': 2.52, 'ref_without': None,
        'load': [0.652, 0.505, 0.473, 0.462, 0.484, 0.578, 0.999, 1.366, 1.104, 0.946, 1.945, 2.05,
                 2.155, 2.05, 1.997, 1.892, 1.261, 1.629, 1.787, 1.682, 1.419, 1.104, 0.893, 0.736],
        'imp': [0.652, 0.505, 0.473, 0.462, 0.484, 0.578, 0.999, 1.366, 0.449, 0.0, 0.0, 0.0,
                0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.02, 1.419, 1.104, 0.893, 0.736],
        'exp': [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.755,
                2.259, 2.55, 2.417, 1.978, 1.751, 0.282, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
    },
    date(2026, 10, 10): {
        'ref_with': 1.29, 'ref_without': None,
        'load': [0.611, 0.473, 0.443, 0.434, 0.453, 0.542, 0.936, 1.281, 1.035, 0.887, 1.823, 1.922,
                 2.02, 1.922, 1.872, 1.774, 1.183, 1.527, 1.675, 1.577, 1.33, 1.035, 0.838, 0.69],
        'imp': [0.611, 0.473, 0.443, 0.434, 0.453, 0.542, 0.936, 1.281, 0.238, 0.0, 0.0, 0.0,
                0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.929, 0.838, 0.69],
        'exp': [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.761,
                3.353, 3.678, 3.501, 2.937, 2.484, 0.799, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
    },
    date(2026, 10, 11): {
        'ref_with': 1.03, 'ref_without': 7.82,
        'load': [0.531, 0.411, 0.385, 0.377, 0.394, 0.471, 0.813, 1.113, 0.899, 0.771, 1.584, 1.67,
                 1.755, 1.67, 1.627, 1.541, 1.027, 1.327, 1.456, 1.37, 1.156, 0.899, 0.728, 0.599],
        'imp': [0.531, 0.411, 0.385, 0.377, 0.394, 0.471, 0.813, 1.113, 0.13, 0.0, 0.0, 0.0,
                0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.543, 0.899, 0.728, 0.599],
        'exp': [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.466, 2.873,
                3.426, 3.73, 3.554, 3.002, 2.509, 0.916, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
    },
}


def utc(*args):
    return datetime(*args, tzinfo=UTC)


def ms(dt):
    return int(dt.timestamp() * 1000)


def day_hours(day):
    start, end = day_bounds(day, BERLIN)
    return hour_starts(start, end)


def fixture_series(day):
    f = FIXTURE[day]
    return {'hours': day_hours(day), 'price': list(PRICE), 'import_kwh': list(f['imp']),
            'export_kwh': list(f['exp']), 'load_kwh': list(f['load'])}


def stats_rows(day, counter_base=1000.0, price_column='state', following_row=True):
    """HA-Langzeitstatistik eines Fixture-Tages wie von recorder/statistics_during_period.

    Preis wie sensor.tibber_prices (state_class total): sum/state, mean None. Mit
    price_column='mean' wie ein Preissensor mit state_class measurement. following_row: auch
    die Zählerzeile der Stunde nach Mitternacht (zeigt, dass der Tag nicht mit einer Lücke endet).
    """
    f = FIXTURE[day]
    hours = day_hours(day)
    rows = {IMPORT: [], EXPORT: [], PRICE_ENTITY: [], LOAD: []}
    imp_sum = exp_sum = counter_base
    price_sum = 0.0
    rows[IMPORT].append({'start': ms(hours[0] - H), 'sum': imp_sum, 'mean': None})
    rows[EXPORT].append({'start': ms(hours[0] - H), 'sum': exp_sum, 'mean': None})
    for k, h in enumerate(hours):
        imp_sum += f['imp'][k]
        exp_sum += f['exp'][k]
        rows[IMPORT].append({'start': ms(h), 'end': ms(h + H), 'sum': imp_sum, 'state': imp_sum, 'mean': None})
        rows[EXPORT].append({'start': ms(h), 'end': ms(h + H), 'sum': exp_sum, 'state': exp_sum, 'mean': None})
        price_sum += PRICE[k]
        if price_column == 'state':
            rows[PRICE_ENTITY].append({'start': ms(h), 'end': ms(h + H), 'sum': price_sum,
                                       'state': PRICE[k], 'mean': None})
        else:
            rows[PRICE_ENTITY].append({'start': ms(h), 'end': ms(h + H), 'sum': None,
                                       'state': None, 'mean': PRICE[k]})
        rows[LOAD].append({'start': ms(h), 'end': ms(h + H), 'sum': None, 'state': None,
                           'mean': f['load'][k] * 1000.0})
    if following_row:
        after = hours[-1] + H
        rows[IMPORT].append({'start': ms(after), 'end': ms(after + H), 'sum': imp_sum + 0.2,
                             'state': imp_sum + 0.2, 'mean': None})
        rows[EXPORT].append({'start': ms(after), 'end': ms(after + H), 'sum': exp_sum,
                             'state': exp_sum, 'mean': None})
    return rows


def history_states(day):
    """History wie vom Recorder: Zähler je Stunde mit Zwischenstand, Stunden ohne Zuwachs
    ohne State; Preis nur bei Änderung; Hauslast mehrere States je Stunde."""
    f = FIXTURE[day]
    hours = day_hours(day)
    states = {IMPORT: [(hours[0] - timedelta(minutes=7), '1000.0')],
              EXPORT: [(hours[0] - timedelta(minutes=7), '500.0')],
              PRICE_ENTITY: [(hours[0] - timedelta(minutes=50), '0.299')],
              LOAD: []}
    for entity, key, value in ((IMPORT, 'imp', 1000.0), (EXPORT, 'exp', 500.0)):
        for k, h in enumerate(hours):
            if f[key][k] == 0:
                continue
            states[entity].append((h + timedelta(minutes=30), f"{value + f[key][k] / 2:.4f}"))
            value += f[key][k]
            states[entity].append((h + H, f"{value:.4f}"))
    for k, h in enumerate(hours):
        states[PRICE_ENTITY].append((h + timedelta(seconds=2), str(PRICE[k])))
        watts = f['load'][k] * 1000.0
        states[LOAD] += [(h, str(watts - 100)), (h + timedelta(minutes=30), str(watts + 100))]
    return states


class FakeHA:
    """HA-Client-Attrappe für Statistik, History und Attribute."""

    def __init__(self, stats=None, stats_error=None, states=None, attributes=None):
        self.stats = stats or {}
        self.stats_error = stats_error
        self.states = states or {}
        self.attributes = attributes
        self.stats_calls = []
        self.history_calls = []

    def get_statistics_during_period(self, statistic_ids, start_time, end_time, period='hour',
                                     types=('mean',), units=None):
        self.stats_calls.append({'ids': list(statistic_ids), 'start': start_time, 'end': end_time,
                                 'period': period, 'types': list(types), 'units': units})
        if self.stats_error:
            raise self.stats_error
        keep = {'start', 'end'} | set(types)
        return {entity: [{k: v for k, v in r.items() if k in keep} for r in rows
                         if start_time <= datetime.fromtimestamp(r['start'] / 1000, UTC) < end_time]
                for entity, rows in self.stats.items() if entity in statistic_ids}

    def get_history(self, entity_id, start_time, end_time=None, minimal_response=False):
        """Wie /api/history/period: Zustand zum Startzeitpunkt plus Änderungen im Fenster."""
        self.history_calls.append((entity_id, start_time, end_time))
        states = self.states.get(entity_id, [])
        before = [s for s in states if s[0] <= start_time]
        inside = [s for s in states if start_time < s[0] < end_time]
        return [{'state': value, 'last_changed': ts.isoformat()}
                for ts, value in ([before[-1]] if before else []) + inside]

    def get_attributes(self, entity_id):
        return self.attributes


def make_source(ha, **kwargs):
    return SavingsData(ha, IMPORT, EXPORT, PRICE_ENTITY, LOAD,
                       load_db_path=kwargs.pop('load_db_path', '/nonexistent/calibration_data.db'),
                       **kwargs)


# ==============================================================================
# Fehler 1 + 4: Kostenformel
# ==============================================================================

class TestCostFormula(unittest.TestCase):

    def test_hand_calculated_example(self):
        hours = [utc(2026, 10, 6, 10) + k * H for k in range(3)]
        series = {'hours': hours, 'price': [0.30, 0.40, 0.20], 'load_kwh': [1.0, 2.0, 3.0],
                  'import_kwh': [1.0, 0.5, 0.0], 'export_kwh': [0.0, 0.0, 4.0]}
        result = present(period_totals(series, FEED_IN), BERLIN)
        self.assertAlmostEqual(result['cost_without'], 1.70)          # 0.30 + 0.80 + 0.60
        self.assertAlmostEqual(result['import_cost'], 0.50)           # 0.30 + 0.20
        self.assertAlmostEqual(result['export_revenue'], 0.26)        # 4 kWh x 0.066, nicht x 0.20
        self.assertAlmostEqual(result['cost_with'], 0.24)             # 0.50 - 0.264
        self.assertAlmostEqual(result['saved'], 1.46)                 # 1.70 - 0.236
        self.assertAlmostEqual(result['saved_percent'], 86.1)
        self.assertEqual((result['hours'], result['hours_expected'], result['complete']), (3, 3, True))
        self.assertEqual(result['until'], '15:00')

    def test_reference_days(self):
        for day, f in FIXTURE.items():
            with self.subTest(day=day):
                result = present(period_totals(fixture_series(day), FEED_IN), BERLIN)
                self.assertLessEqual(abs(result['cost_with'] - f['ref_with']), TOLERANCE)
                if f['ref_without'] is not None:
                    self.assertLessEqual(abs(result['cost_without'] - f['ref_without']), TOLERANCE)
                self.assertGreater(result['cost_with'], 0)
                self.assertAlmostEqual(result['saved'], result['cost_without'] - result['cost_with'], delta=0.011)
                self.assertTrue(result['complete'])
                self.assertEqual(result['until'], '24:00')

    def test_old_formula_made_sunny_days_negative(self):
        # bis v1.3.6: Einspeisung zum Tibber-Preis gutgeschrieben -> 'Mit Addon' negativ
        f = FIXTURE[date(2026, 10, 6)]
        old = sum(i * p for i, p in zip(f['imp'], PRICE)) - sum(e * p for e, p in zip(f['exp'], PRICE))
        self.assertLess(old, 0)
        new = present(period_totals(fixture_series(date(2026, 10, 6)), FEED_IN))['cost_with']
        self.assertAlmostEqual(new, 1.61, delta=0.01)

    def test_cost_without_is_full_house_load(self):
        # Hauslast inkl. Pool, nichts abgezogen
        series = fixture_series(date(2026, 10, 11))
        totals = period_totals(series, FEED_IN)
        self.assertAlmostEqual(totals['cost_without'],
                               sum(l * p for l, p in zip(series['load_kwh'], PRICE)), places=9)
        self.assertAlmostEqual(totals['load_kwh'], sum(series['load_kwh']), places=9)

    def test_incomplete_hours_are_excluded_from_all_sums(self):
        series = fixture_series(date(2026, 10, 6))
        series['load_kwh'][18] = None
        series['import_kwh'][2] = None
        totals = period_totals(series, FEED_IN)
        self.assertEqual((totals['hours'], totals['hours_expected']), (22, 24))
        f = FIXTURE[date(2026, 10, 6)]
        used = [k for k in range(24) if k not in (2, 18)]
        self.assertAlmostEqual(totals['import_cost'], sum(f['imp'][k] * PRICE[k] for k in used), places=9)
        self.assertAlmostEqual(totals['cost_without'], sum(f['load'][k] * PRICE[k] for k in used), places=9)
        self.assertFalse(present(totals)['complete'])

    def test_no_complete_hour_gives_no_result(self):
        series = {'hours': [utc(2026, 10, 6, 10)], 'price': [None], 'load_kwh': [1.0],
                  'import_kwh': [1.0], 'export_kwh': [0.0]}
        totals = period_totals(series, FEED_IN)
        self.assertEqual((totals['hours'], totals['hours_expected'], totals['until']), (0, 1, None))
        self.assertIsNone(present(totals))
        self.assertIsNone(present(None))
        self.assertEqual(savings.empty_result('no_full_hour')['reason'], 'no_full_hour')
        self.assertFalse(savings.empty_result()['available'])

    def test_percent_capped_at_100_when_export_exceeds_import_costs(self):
        series = {'hours': [utc(2026, 6, 1, 10), utc(2026, 6, 1, 11)], 'price': [0.25, 0.25],
                  'load_kwh': [1.0, 1.0], 'import_kwh': [0.1, 0.0], 'export_kwh': [5.0, 6.0]}
        result = present(period_totals(series, FEED_IN))
        self.assertLess(result['cost_with'], 0)
        self.assertGreater(result['saved'], result['cost_without'])
        self.assertEqual(result['saved_percent'], 100.0)
        self.assertTrue(result['saved_percent_capped'])

    def test_percent_without_costs_is_zero(self):
        self.assertEqual(savings.saved_percent(0.0, 0.0), (0.0, False))
        self.assertEqual(savings.saved_percent(-0.5, 2.0), (-25.0, False))

    def test_merge_totals_for_week(self):
        parts = [period_totals(fixture_series(day), FEED_IN) for day in FIXTURE] + [None]
        merged = present(merge_totals(parts))
        self.assertAlmostEqual(merged['cost_with'], sum(f['ref_with'] for f in FIXTURE.values()), delta=0.03)
        self.assertEqual(merged['hours'], 96)
        self.assertIsNone(merge_totals([None, None]))

    def test_merge_keeps_expected_hours_of_days_without_data(self):
        # Woche/Monat mit einem Tag ohne vollständige Stunde darf nicht als vollständig gelten
        days = [date(2026, 10, 6), date(2026, 10, 7), date(2026, 10, 10)]
        parts = [period_totals(fixture_series(day), FEED_IN) for day in days]
        empty = fixture_series(date(2026, 10, 11))
        empty['load_kwh'] = [None] * 24
        parts.append(period_totals(empty, FEED_IN))
        merged = present(merge_totals(parts), BERLIN)
        self.assertEqual((merged['hours'], merged['hours_expected'], merged['complete']), (72, 96, False))
        self.assertEqual(merged['until'], '24:00')

    def test_feed_in_default_is_the_same_everywhere(self):
        # v1.3.7: 0,066 €/kWh in Ersparnis, Default-Config, Konfigseite und Device-Scheduler
        from device_scheduler import DeviceScheduler
        self.assertEqual(savings.DEFAULT_FEED_IN_EUR_PER_KWH, 0.066)
        root = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'battery_manager')
        with open(os.path.join(root, 'app.py'), encoding='utf-8') as f:
            self.assertIn("'einspeise_verguetung_eur_per_kwh': 0.066,", f.read())
        with open(os.path.join(root, 'templates', 'config.html'), encoding='utf-8') as f:
            self.assertIn('is defined else 0.066 }}', f.read())
        hour = utc(2026, 10, 6, 12)
        effective = DeviceScheduler({}, None)._effective_price_for_hour(hour, 0.25, 1000, {hour: 2.0})
        self.assertAlmostEqual(effective, 0.066)      # volle PV-Deckung: nur entgangene Einspeisung

    def test_feed_in_tariff_parsing(self):
        self.assertEqual(parse_feed_in_tariff(None), 0.066)
        self.assertEqual(parse_feed_in_tariff('0.066'), 0.066)
        self.assertEqual(parse_feed_in_tariff(0.082), 0.082)
        self.assertEqual(parse_feed_in_tariff(0), 0.0)
        for invalid in ('abc', -0.1, 6.6, float('nan'), float('inf')):
            with self.subTest(value=invalid):
                self.assertEqual(parse_feed_in_tariff(invalid), 0.066)


# ==============================================================================
# Fehler 2: Stundenwerte aus Zählern
# ==============================================================================

class TestCounterDeltas(unittest.TestCase):

    def test_start_value_is_last_value_before_hour(self):
        points = [(utc(2026, 10, 6, 9, 58), 99.9), (utc(2026, 10, 6, 10, 0, 30), 100.0),
                  (utc(2026, 10, 6, 10, 59), 101.0), (utc(2026, 10, 6, 12, 10), 101.5)]
        hours = [utc(2026, 10, 6, 10), utc(2026, 10, 6, 11), utc(2026, 10, 6, 12)]
        # bis v1.3.6: Stunde 10 = 1,0 (Start = erster Wert >= 10:00), Stunde 11 = 101,0 - 101,5
        # = -0,5 -> „reset detected" -> 0
        self.assertEqual([round(v, 6) for v in counter_deltas(points, hours)], [1.1, 0.0, 0.5])

    def test_hour_without_counter_change_is_zero(self):
        points = [(utc(2026, 10, 6, 1, 0), 500.0)]   # nachts keine Einspeisung, kein State
        self.assertEqual(counter_deltas(points, [utc(2026, 10, 6, k) for k in range(2, 6)]), [0.0] * 4)

    def test_value_exactly_at_boundary_counts(self):
        points = [(utc(2026, 10, 6, 10), 10.0), (utc(2026, 10, 6, 11), 12.0)]
        self.assertEqual(counter_deltas(points, [utc(2026, 10, 6, 10)]), [2.0])

    def test_no_value_before_hour_is_none(self):
        points = [(utc(2026, 10, 6, 10, 5), 10.0), (utc(2026, 10, 6, 11, 5), 12.0)]
        self.assertEqual(counter_deltas(points, [utc(2026, 10, 6, 10), utc(2026, 10, 6, 11)]), [None, 2.0])

    def test_counter_reset_is_none_not_zero(self):
        points = [(utc(2026, 10, 6, 9), 1000.0), (utc(2026, 10, 6, 10, 30), 0.4), (utc(2026, 10, 6, 11), 0.9)]
        self.assertEqual(counter_deltas(points, [utc(2026, 10, 6, 10)]), [None])

    def test_deltas_from_statistic_sums(self):
        h = utc(2026, 10, 6, 10)
        sums = {h - H: 99.0, h: 100.5, h + H: 100.7, h + 3 * H: 103.0, h + 4 * H: 103.4}
        # h+2 fehlt (HA lief nicht): h+1 endet mit dem Stopp (abgeschnitten), h+3 enthielte die
        # Energie der Lücke -> h+1 bis h+3 None; h+4 wieder normal
        result = deltas_from_sums(sums, [h + k * H for k in range(5)])
        self.assertEqual([None if v is None else round(v, 6) for v in result], [1.5, None, None, None, 0.4])
        # Jüngste Stunde ohne Folgezeile (noch nicht kompiliert) ist keine Lücke
        self.assertEqual([round(v, 6) for v in deltas_from_sums({h - H: 1.0, h: 1.5}, [h])], [0.5])
        self.assertEqual(deltas_from_sums({h: 5.0}, [h]), [None])           # kein Startwert
        self.assertEqual(deltas_from_sums({h - 2 * H: 4.0, h: 5.0}, [h]), [None])  # Lücke davor
        self.assertEqual(deltas_from_sums({h - H: 5.0, h: 1.0}, [h]), [None])  # rückläufig


# ==============================================================================
# Fehler 3: Preise ohne 0,30-€-Ersatzwert
# ==============================================================================

class TestPriceCarryForward(unittest.TestCase):

    def test_gap_takes_last_known_price(self):
        self.assertEqual(fill_price_gaps([0.31, None, None, 0.25]), ([0.31, 0.31, 0.31, 0.25], 2))

    def test_leading_gap_takes_first_known_price(self):
        self.assertEqual(fill_price_gaps([None, None, 0.27, None]), ([0.27, 0.27, 0.27, 0.27], 3))

    def test_no_price_at_all_stays_none(self):
        self.assertEqual(fill_price_gaps([None, None]), ([None, None], 0))

    def test_gap_longer_than_limit_stays_none(self):
        filled, count = fill_price_gaps([0.3] + [None] * 3, max_gap=2)
        self.assertEqual((filled, count), ([0.3, 0.3, 0.3, None], 2))

    def test_attributes_today_and_quarter_hours(self):
        hours = [utc(2026, 10, 12, 8), utc(2026, 10, 12, 9)]
        attributes = {
            'today': [{'startsAt': '2026-10-12T10:00:00.000+02:00', 'total': 0.30},
                      {'startsAt': '2026-10-12T11:00:00.000+02:00', 'total': 0.20},
                      {'startsAt': '2026-10-12T11:15:00.000+02:00', 'total': 0.24},
                      {'startsAt': 'kaputt', 'total': 0.99},
                      {'startsAt': '2026-10-12T13:00:00+02:00', 'total': 0.50}],
            'tomorrow': [{'startsAt': '2026-10-13T10:00:00+02:00', 'total': 0.11}],
        }
        result = prices_from_tibber_attributes(attributes, hours)
        self.assertEqual(set(result), set(hours))
        self.assertAlmostEqual(result[hours[0]], 0.30)
        self.assertAlmostEqual(result[hours[1]], 0.22)
        self.assertEqual(prices_from_tibber_attributes(None, hours), {})

    def test_history_hour_without_state_change_keeps_price(self):
        # Gleicher Preis in zwei Stunden -> HA speichert keinen neuen State (bis v1.3.6: 0,30 €)
        h = utc(2026, 10, 12, 8)
        ha = FakeHA(stats_error=RuntimeError('ws down'), states={
            PRICE_ENTITY: [(h - timedelta(minutes=30), '0.251'), (h + timedelta(seconds=3), '0.284'),
                           (h + 2 * H + timedelta(seconds=3), '0.312')]})
        prices, source = make_source(ha)._prices(None, [h, h + H, h + 2 * H], h)
        self.assertEqual(source, 'history')
        self.assertAlmostEqual(prices[1], 0.284)
        self.assertNotEqual(prices[1], 0.30)

    def test_attributes_fill_missing_statistics_hour(self):
        h = utc(2026, 10, 12, 8)
        rows = {h: {'mean': 0.25}}
        ha = FakeHA(attributes={'today': [{'startsAt': '2026-10-12T11:00:00+02:00', 'total': 0.33}]})
        prices, source = make_source(ha)._prices(rows, [h, h + H], h + 2 * H)
        self.assertEqual((prices, source), ([0.25, 0.33], 'statistics+attributes'))


# ==============================================================================
# Datenquellen: Statistik, History-Fallback, load_hourly
# ==============================================================================

class TestSavingsData(unittest.TestCase):

    def assert_reference(self, series, day):
        result = present(daily_totals(series, FEED_IN, BERLIN)[day], BERLIN)
        f = FIXTURE[day]
        self.assertAlmostEqual(result['cost_with'], f['ref_with'], delta=0.011)
        if f['ref_without'] is not None:
            self.assertAlmostEqual(result['cost_without'], f['ref_without'], delta=0.011)
        self.assertEqual((result['hours'], result['hours_expected']), (24, 24))
        return result

    def test_statistics_single_call_with_all_sensors(self):
        day = date(2026, 10, 6)
        ha = FakeHA(stats=stats_rows(day))
        start, end = day_bounds(day, BERLIN)
        series = make_source(ha).hourly(start, end, now=end + 2 * H)
        self.assert_reference(series, day)
        self.assertEqual(len(ha.stats_calls), 1)
        call = ha.stats_calls[0]
        self.assertEqual(set(call['ids']), {IMPORT, EXPORT, PRICE_ENTITY, LOAD})
        self.assertEqual(set(call['types']), {'sum', 'mean', 'state'})
        self.assertEqual(call['units'], {'energy': 'kWh', 'power': 'W'})
        self.assertLess(call['start'], start)                 # Vorlauf für den ersten sum-Startwert
        self.assertEqual(ha.history_calls, [])
        self.assertEqual(series['sources'], {'import_kwh': 'statistics', 'export_kwh': 'statistics',
                                             'load_kwh': 'statistics', 'price': 'statistics'})

    def test_price_from_state_column_without_mean(self):
        # Live 12.10.: sensor.tibber_prices hat state_class total -> Statistik nur sum/state, mean None.
        # Bis 28b0641 kamen dadurch für vergangene Tage keine Preise ("keine Daten").
        day = date(2026, 10, 6)
        stats = stats_rows(day)
        self.assertTrue(all(r['mean'] is None for r in stats[PRICE_ENTITY]))
        ha = FakeHA(stats=stats)
        start, end = day_bounds(day, BERLIN)
        series = make_source(ha).hourly(start, end, now=end + 30 * 24 * H)    # außerhalb der History
        self.assert_reference(series, day)
        self.assertEqual(series['price'], PRICE)
        self.assertEqual(series['sources']['price'], 'statistics')
        self.assertEqual(ha.history_calls, [])

    def test_price_mean_for_measurement_sensor(self):
        day = date(2026, 10, 11)
        ha = FakeHA(stats=stats_rows(day, price_column='mean'))
        start, end = day_bounds(day, BERLIN)
        series = make_source(ha).hourly(start, end, now=end + 2 * H)
        self.assert_reference(series, day)
        self.assertEqual(series['sources']['price'], 'statistics')

    def test_missing_price_hours_fall_back_per_hour(self):
        # Zeilen vorhanden, aber für einzelne Stunden weder state noch mean -> History je Stunde
        day = date(2026, 10, 7)
        stats = stats_rows(day)
        hours = day_hours(day)
        for row in stats[PRICE_ENTITY]:
            if row['start'] in (ms(hours[5]), ms(hours[6]), ms(hours[18])):
                row['state'] = None
        states = {PRICE_ENTITY: history_states(day)[PRICE_ENTITY]}
        ha = FakeHA(stats=stats, states=states)
        start, end = day_bounds(day, BERLIN)
        series = make_source(ha).hourly(start, end, now=end + 2 * H)
        self.assert_reference(series, day)
        self.assertEqual(series['sources']['price'], 'statistics+history')
        self.assertEqual(ha.history_calls, [(PRICE_ENTITY, hours[5], hours[19])])
        self.assertAlmostEqual(series['price'][18], PRICE[18], places=3)

    def test_price_rows_without_values_use_history(self):
        day = date(2026, 10, 10)
        stats = stats_rows(day)
        for row in stats[PRICE_ENTITY]:
            row['state'] = None
        ha = FakeHA(stats=stats, states={PRICE_ENTITY: history_states(day)[PRICE_ENTITY]})
        start, end = day_bounds(day, BERLIN)
        series = make_source(ha).hourly(start, end, now=end + 2 * H)
        self.assert_reference(series, day)
        self.assertEqual(series['sources']['price'], 'history')

    def test_last_hour_of_past_range_checks_the_following_row(self):
        # Statistik wird bis eine Stunde nach dem Zeitraum abgefragt: fehlt die Folgezeile
        # (HA stoppte in der letzten Stunde des Tages), zählt die letzte Stunde nicht
        day = date(2026, 10, 6)
        start, end = day_bounds(day, BERLIN)
        ha = FakeHA(stats=stats_rows(day, following_row=False))   # Zeile 00-01 Uhr fehlt
        series = make_source(ha).hourly(start, end, now=end + 30 * 24 * H)
        self.assertEqual(ha.stats_calls[0]['end'], end + H)
        self.assertIsNone(series['import_kwh'][23])
        self.assertEqual(daily_totals(series, FEED_IN, BERLIN)[day]['hours'], 23)
        # Kurz nach Mitternacht ist die Zeile 00-01 Uhr noch nicht kompiliert: keine Lücke
        series = make_source(FakeHA(stats=stats_rows(day, following_row=False))).hourly(
            start, end, now=end + timedelta(minutes=30))
        self.assertEqual(daily_totals(series, FEED_IN, BERLIN)[day]['hours'], 24)

    def test_statistics_gap_excludes_hour_after_gap_in_all_sums(self):
        # Live 08.10.: Stunde 16-17 Uhr fehlt in allen vier Reihen; die sum-Differenz der
        # 17-Uhr-Zeile umfasst zwei Stunden. Beide Stunden fallen aus allen Summen.
        day = date(2026, 10, 6)
        hours = day_hours(day)
        stats = stats_rows(day)
        for entity in stats:
            stats[entity] = [r for r in stats[entity] if r['start'] != ms(hours[16])]
        ha = FakeHA(stats=stats)
        start, end = day_bounds(day, BERLIN)
        series = make_source(ha).hourly(start, end, now=end + 30 * 24 * H)
        self.assertIsNone(series['export_kwh'][15])     # HA stoppte in dieser Stunde
        self.assertIsNone(series['import_kwh'][16])
        self.assertIsNone(series['export_kwh'][17])
        totals = daily_totals(series, FEED_IN, BERLIN)[day]
        self.assertEqual((totals['hours'], totals['hours_expected']), (21, 24))
        f = FIXTURE[day]
        used = [k for k in range(24) if k not in (15, 16, 17)]
        self.assertAlmostEqual(totals['export_kwh'], sum(f['exp'][k] for k in used), places=6)
        self.assertAlmostEqual(totals['cost_without'], sum(f['load'][k] * PRICE[k] for k in used), places=6)
        self.assertAlmostEqual(totals['import_cost'], sum(f['imp'][k] * PRICE[k] for k in used), places=6)

    def test_history_fallback_matches_statistics(self):
        for day in FIXTURE:
            with self.subTest(day=day):
                ha = FakeHA(stats_error=RuntimeError('ws down'), states=history_states(day))
                start, end = day_bounds(day, BERLIN)
                series = make_source(ha).hourly(start, end, now=end + 2 * H)
                self.assert_reference(series, day)
                self.assertEqual(series['sources'], {'import_kwh': 'history', 'export_kwh': 'history',
                                                     'load_kwh': 'history', 'price': 'history'})

    def test_load_from_load_hourly_table(self):
        day = date(2026, 10, 11)
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, 'calibration_data.db')
            hours = day_hours(day)
            collector = LoadHourlyCollector(None, db_path=db, entity_id=LOAD)
            collector._store({h: (FIXTURE[day]['load'][k] * 1000.0, None) for k, h in enumerate(hours)},
                             'statistics', hours[-1] + 2 * H)
            states = history_states(day)
            del states[LOAD]
            ha = FakeHA(stats_error=RuntimeError('ws down'), states=states)
            start, end = day_bounds(day, BERLIN)
            series = make_source(ha, load_db_path=db).hourly(start, end, now=end + 2 * H)
            self.assert_reference(series, day)
            self.assertEqual(series['sources']['load_kwh'], 'load_hourly')
            self.assertNotIn(LOAD, [entity for entity, _, _ in ha.history_calls])

    def test_history_only_for_recent_days(self):
        day = date(2026, 9, 1)
        ha = FakeHA(stats_error=RuntimeError('ws down'), states=history_states(date(2026, 10, 6)))
        start, end = day_bounds(day, BERLIN)
        series = make_source(ha).hourly(start, end, now=utc(2026, 10, 12, 10))
        self.assertEqual(ha.history_calls, [])
        self.assertIsNone(present(daily_totals(series, FEED_IN, BERLIN)[day]))

    def test_load_history_only_for_contiguous_gaps(self):
        # Alte NULL-Stunde in load_hourly + fehlende jüngste Stunde: zwei kleine History-Abfragen
        # statt KSEM-History über den ganzen Zeitraum dazwischen
        day = date(2026, 10, 11)
        hours = day_hours(day)
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, 'calibration_data.db')
            stored = {h: (FIXTURE[day]['load'][k] * 1000.0, None) for k, h in enumerate(hours[:-1])}
            stored[hours[3]] = (None, None)
            LoadHourlyCollector(None, db_path=db, entity_id=LOAD)._store(stored, 'statistics', hours[-1] + 2 * H)
            ha = FakeHA(stats_error=RuntimeError('ws down'), states=history_states(day))
            start, end = day_bounds(day, BERLIN)
            series = make_source(ha, load_db_path=db).hourly(start, end, now=end + 2 * H)
        load_calls = [(s, e) for entity, s, e in ha.history_calls if entity == LOAD]
        self.assertEqual(load_calls, [(hours[3], hours[4]), (hours[23], hours[23] + H)])
        self.assertAlmostEqual(series['load_kwh'][3], FIXTURE[day]['load'][3], places=6)
        self.assertEqual(series['sources']['load_kwh'], 'load_hourly')

    def test_last_complete_hour_waits_for_statistics(self):
        # HA kompiliert die Stunde h erst ab h+1:00:10 - bis 5 min nach der vollen Stunde zählt sie nicht
        self.assertEqual(savings.last_complete_hour(utc(2026, 10, 12, 14, 3)), utc(2026, 10, 12, 13))
        self.assertEqual(savings.last_complete_hour(utc(2026, 10, 12, 14, 5)), utc(2026, 10, 12, 14))
        self.assertEqual(savings.last_complete_hour(datetime(2026, 10, 25, 2, 30, tzinfo=BERLIN, fold=1)),
                         utc(2026, 10, 25, 1))

    def test_today_counts_completed_hours_only(self):
        day = date(2026, 10, 6)
        stats = stats_rows(day)
        hours = day_hours(day)
        now = hours[14] + timedelta(minutes=3)              # 14:03, Statistik für 13-14 Uhr fehlt noch
        for entity in stats:
            stats[entity] = [r for r in stats[entity] if r['start'] < ms(hours[13])]
        ha = FakeHA(stats=stats)
        start, _ = day_bounds(day, BERLIN)
        series = make_source(ha).hourly(start, hours[14], now=now)
        result = present(daily_totals(series, FEED_IN, BERLIN)[day], BERLIN)
        self.assertEqual((result['hours'], result['hours_expected']), (13, 14))
        self.assertEqual(result['until'], '13:00')

    def test_dst_days(self):
        self.assertEqual(len(day_hours(date(2026, 10, 25))), 25)
        self.assertEqual(len(day_hours(date(2026, 3, 29))), 23)
        self.assertEqual(len(day_hours(date(2026, 10, 6))), 24)
        start, end = day_bounds(date(2026, 10, 25), BERLIN)
        series = {'hours': hour_starts(start, end)}
        for key in ('price', 'import_kwh', 'export_kwh', 'load_kwh'):
            series[key] = [1.0] * 25
        totals = daily_totals(series, FEED_IN, BERLIN)
        self.assertEqual(list(totals), [date(2026, 10, 25)])
        self.assertEqual(totals[date(2026, 10, 25)]['hours'], 25)

    def test_daily_totals_split_by_local_day(self):
        days = [date(2026, 10, 6), date(2026, 10, 7)]
        series = {'hours': day_hours(days[0]) + day_hours(days[1])}
        for key, fixture_key in (('import_kwh', 'imp'), ('export_kwh', 'exp'), ('load_kwh', 'load')):
            series[key] = FIXTURE[days[0]][fixture_key] + FIXTURE[days[1]][fixture_key]
        series['price'] = PRICE + PRICE
        totals = daily_totals(series, FEED_IN, BERLIN)
        self.assertEqual(list(totals), days)
        for day in days:
            self.assertAlmostEqual(present(totals[day])['cost_with'], FIXTURE[day]['ref_with'], delta=0.011)


if __name__ == '__main__':
    unittest.main()
