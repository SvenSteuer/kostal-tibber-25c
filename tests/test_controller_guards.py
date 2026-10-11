#!/usr/bin/env python3
"""
v1.3.5 Hotfix - Unit-Tests für die Entscheidungslogik (nur Standardbibliothek).

Aufruf aus dem Repo-Root:
    python3 -m unittest discover -s tests -v
"""

import os
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'battery_manager'))

# kostal_api / forecast_solar_api importieren 'requests' bzw. 'Crypto'. Lokal ohne diese
# Pakete genügen Platzhalter, weil alle Netzwerkaufrufe in den Tests gemockt werden.
try:
    import requests  # noqa: F401
except ImportError:
    _requests = types.ModuleType('requests')

    class _RequestException(Exception):
        pass

    _requests.RequestException = _RequestException
    _requests.exceptions = types.SimpleNamespace(Timeout=_RequestException,
                                                 ConnectionError=_RequestException)
    _requests.get = _requests.put = _requests.post = None
    sys.modules['requests'] = _requests
try:
    from Crypto.Cipher import AES  # noqa: F401
except ImportError:
    _crypto = types.ModuleType('Crypto')
    _cipher = types.ModuleType('Crypto.Cipher')
    _cipher.AES = None
    _crypto.Cipher = _cipher
    sys.modules['Crypto'] = _crypto
    sys.modules['Crypto.Cipher'] = _cipher

from core.controller_guards import (  # noqa: E402
    parse_soc, plan_is_fresh, safety_charge_needed, safety_stop_allowed, prices_plausible,
    mask_secrets, drop_masked_secrets, SECRET_MASK, PLAN_MAX_AGE_SECONDS)
from core.tibber_optimizer import TibberOptimizer  # noqa: E402
from core.forecast_solar_api import ForecastSolarAPI  # noqa: E402
from core.kostal_api import KostalAPI  # noqa: E402

CEST = timezone(timedelta(hours=2))
CET = timezone(timedelta(hours=1))


def hourly_prices(start, count, step_seconds=3600):
    return [{'startsAt': (start + timedelta(seconds=i * step_seconds)).isoformat(), 'total': 0.30}
            for i in range(count)]


class TestParseSoc(unittest.TestCase):
    """H1: ungültiger SOC wird nie zu 0."""

    def test_valid_values(self):
        self.assertEqual(parse_soc('73'), 73.0)
        self.assertEqual(parse_soc('73.5'), 73.5)
        self.assertEqual(parse_soc(12), 12.0)
        self.assertEqual(parse_soc('0'), 0.0)      # echte 0 ist gültig (SAFETY braucht trotzdem 3 Messungen)
        self.assertEqual(parse_soc('100'), 100.0)

    def test_invalid_values_are_none(self):
        for raw in (None, '', 'unavailable', 'unknown', 'nan', 'inf', '-1', '100.5', True, False, [], {}):
            with self.subTest(raw=raw):
                self.assertIsNone(parse_soc(raw))


class TestPlanFreshness(unittest.TestCase):
    """H2: Pläne älter als 900 s werden nicht ausgeführt."""

    def test_no_timestamp_is_stale(self):
        self.assertFalse(plan_is_fresh(None, 1000.0))

    def test_age_boundaries(self):
        self.assertEqual(PLAN_MAX_AGE_SECONDS, 900)
        self.assertTrue(plan_is_fresh(1000.0, 1000.0))
        self.assertTrue(plan_is_fresh(1000.0, 1900.0))
        self.assertFalse(plan_is_fresh(1000.0, 1900.5))


class TestSafetyDecision(unittest.TestCase):
    """H3: SAFETY nach 3 Messungen, Laden bis Schwelle + 3, Stopp nach >= 120 s."""

    def run_readings(self, readings, min_soc=7, in_safety=False):
        count, results = 0, []
        for soc in readings:
            needed, count = safety_charge_needed(soc, min_soc, count, in_safety)
            results.append(needed)
        return results

    def test_needs_three_consecutive_readings(self):
        self.assertEqual(self.run_readings([6, 6, 6]), [False, False, True])

    def test_single_glitch_does_not_trigger(self):
        # HA liefert einmal '0' (z. B. nach Neustart) -> keine Ladung
        self.assertEqual(self.run_readings([50, 0, 50, 0, 0, 50]), [False] * 6)

    def test_reading_above_threshold_resets_counter(self):
        self.assertEqual(self.run_readings([6, 6, 7, 6, 6, 6]), [False, False, False, False, False, True])

    def test_running_safety_charge_continues_to_margin(self):
        for soc, expected in ((6, True), (7, True), (9.9, True), (10, False), (12, False)):
            with self.subTest(soc=soc):
                needed, _ = safety_charge_needed(soc, 7, 0, in_safety_charge=True)
                self.assertEqual(needed, expected)

    def test_no_margin_without_running_safety_charge(self):
        needed, count = safety_charge_needed(8, 7, 0, in_safety_charge=False)
        self.assertFalse(needed)
        self.assertEqual(count, 0)

    def test_stop_bypasses_hysteresis_only_after_min_runtime(self):
        self.assertFalse(safety_stop_allowed(False, True, 60))
        self.assertFalse(safety_stop_allowed(False, True, 119.9))
        self.assertTrue(safety_stop_allowed(False, True, 120))
        self.assertTrue(safety_stop_allowed(False, True, 600))

    def test_stop_bypass_only_for_safety_stop(self):
        self.assertFalse(safety_stop_allowed(True, True, 600))     # will weiter laden
        self.assertFalse(safety_stop_allowed(False, False, 600))   # Planladung -> Hysterese gilt
        self.assertFalse(safety_stop_allowed(False, True, None))   # kein Umschaltzeitpunkt bekannt


class TestPricesPlausible(unittest.TestCase):
    """H7: startsAt zeitzonenbewusst und lückenlos im Abstand von 3600 s."""

    def setUp(self):
        self.start = datetime(2026, 10, 11, 0, 0, tzinfo=CEST)
        self.now = datetime(2026, 10, 11, 14, 30, tzinfo=CEST)

    def test_hourly_today_and_tomorrow_ok(self):
        ok, reason = prices_plausible(hourly_prices(self.start, 48), self.now)
        self.assertTrue(ok, reason)

    def test_utc_z_suffix_ok(self):
        prices = [{'startsAt': f'2026-10-11T{h:02d}:00:00.000Z', 'total': 0.3} for h in range(24)]
        ok, reason = prices_plausible(prices, datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc))
        self.assertTrue(ok, reason)

    def test_dst_end_day_with_25_hours_ok(self):
        # 25.10.2026: 02:00 CEST -> 02:00 CET, aware-Zeitstempel bleiben lückenlos
        prices = hourly_prices(datetime(2026, 10, 25, 0, 0, tzinfo=CEST), 3)          # 00, 01, 02 CEST
        prices += hourly_prices(datetime(2026, 10, 25, 2, 0, tzinfo=CET), 22)         # 02 CET .. 23 CET
        self.assertEqual(len(prices), 25)
        ok, reason = prices_plausible(prices, datetime(2026, 10, 25, 2, 30, tzinfo=CET))
        self.assertTrue(ok, reason)

    def test_quarter_hour_prices_rejected(self):
        ok, reason = prices_plausible(hourly_prices(self.start, 96, step_seconds=900), self.now)
        self.assertFalse(ok)
        self.assertIn('900', reason)

    def test_naive_timestamps_rejected(self):
        prices = [{'startsAt': f'2026-10-11T{h:02d}:00:00', 'total': 0.3} for h in range(24)]
        self.assertFalse(prices_plausible(prices, self.now)[0])

    def test_gap_rejected(self):
        prices = hourly_prices(self.start, 48)
        del prices[20]
        self.assertFalse(prices_plausible(prices, self.now)[0])

    def test_unsorted_or_duplicate_rejected(self):
        prices = hourly_prices(self.start, 24)
        self.assertFalse(prices_plausible(list(reversed(prices)), self.now)[0])
        self.assertFalse(prices_plausible(prices + prices, self.now)[0])

    def test_invalid_entries_rejected(self):
        for bad in ({'total': 0.3}, {'startsAt': None, 'total': 0.3}, {'startsAt': 'gestern', 'total': 0.3},
                    {'startsAt': '2026-10-11T23:00:00+02:00', 'total': None},
                    {'startsAt': '2026-10-11T23:00:00+02:00', 'total': '0.3'}, 'kein dict'):
            with self.subTest(bad=bad):
                prices = hourly_prices(self.start, 23) + [bad]
                self.assertFalse(prices_plausible(prices, self.now)[0])

    def test_too_few_or_no_prices_rejected(self):
        self.assertFalse(prices_plausible([], self.now)[0])
        self.assertFalse(prices_plausible(None, self.now)[0])
        self.assertFalse(prices_plausible(hourly_prices(self.start, 1), self.now)[0])

    def test_current_hour_must_be_covered(self):
        yesterday = hourly_prices(self.start - timedelta(days=1), 24)   # veraltete 'today'-Liste
        self.assertFalse(prices_plausible(yesterday, self.now)[0])
        self.assertTrue(prices_plausible(yesterday, None)[0])          # ohne now nur Struktur


class TestPvForecastComplete(unittest.TestCase):
    """H8: fehlende oder unvollständige PV-Prognose -> kein Plan."""

    check = staticmethod(TibberOptimizer._pv_forecast_complete)

    def test_empty_forecast(self):
        self.assertFalse(self.check({}, 14, 24)[0])

    def test_today_and_tomorrow_ok(self):
        pv = {h: 1.0 for h in list(range(8, 18)) + list(range(32, 42))}
        self.assertTrue(self.check(pv, 14, 24)[0])

    def test_missing_tomorrow_rejected_when_window_crosses_midnight(self):
        pv = {h: 1.0 for h in range(8, 18)}
        self.assertFalse(self.check(pv, 14, 24)[0])
        self.assertTrue(self.check(pv, 0, 24)[0])      # 00:00 + 24 h endet um Mitternacht

    def test_missing_today_rejected(self):
        self.assertFalse(self.check({h: 1.0 for h in range(32, 42)}, 14, 24)[0])

    def test_planner_returns_none_without_pv_forecast(self):
        opt = TibberOptimizer({})
        opt.consumption_learner = mock.Mock()
        opt.get_hourly_pv_forecast = mock.Mock(return_value={})
        prices = hourly_prices(datetime.now().astimezone().replace(minute=0, second=0, microsecond=0), 24)
        with self.assertLogs('core.tibber_optimizer', level='WARNING'):
            self.assertIsNone(opt.plan_battery_schedule_rolling(None, {}, 50.0, prices))
        opt.consumption_learner.get_average_consumption.assert_not_called()


class TestForecastSolarPartial(unittest.TestCase):
    """H8: fehlt eine Dachfläche, gibt es keine (Teil-)Prognose; API-Key nie im Log."""

    PLANES = [{'declination': 22, 'azimuth': 45, 'kwp': 8.96},
              {'declination': 22, 'azimuth': -135, 'kwp': 10.665}]

    @staticmethod
    def response(status, result=None):
        resp = mock.Mock(status_code=status, text='error')
        resp.json.return_value = {'result': result} if result is not None else {}
        return resp

    @staticmethod
    def watthours(days):
        base = datetime.now().astimezone().date()
        out = {}
        for d in days:
            day = base + timedelta(days=d)
            for i, h in enumerate(range(8, 18)):
                out[f"{day} {h:02d}:00:00"] = (i + 1) * 500
        return out

    def test_second_plane_http_error_gives_no_forecast(self):
        api = ForecastSolarAPI('SECRETKEY123', 51.0, 7.0)
        responses = [self.response(200, self.watthours([0, 1])), self.response(429)]
        with mock.patch('core.forecast_solar_api.requests.get', side_effect=responses), \
                self.assertLogs('core.forecast_solar_api', level='INFO') as logs:
            self.assertEqual(api.get_hourly_forecast(self.PLANES, include_tomorrow=True), {})
        self.assertFalse(api._cache)                  # Teilergebnis wird nicht gecacht
        self.assertNotIn('SECRETKEY123', '\n'.join(logs.output))

    def test_plane_without_tomorrow_gives_no_forecast(self):
        api = ForecastSolarAPI('SECRETKEY123', 51.0, 7.0)
        responses = [self.response(200, self.watthours([0, 1])), self.response(200, self.watthours([0]))]
        with mock.patch('core.forecast_solar_api.requests.get', side_effect=responses), \
                self.assertLogs('core.forecast_solar_api', level='ERROR'):
            self.assertEqual(api.get_hourly_forecast(self.PLANES, include_tomorrow=True), {})

    def test_complete_forecast_is_returned(self):
        api = ForecastSolarAPI('SECRETKEY123', 51.0, 7.0)
        responses = [self.response(200, self.watthours([0, 1])), self.response(200, self.watthours([0, 1]))]
        with mock.patch('core.forecast_solar_api.requests.get', side_effect=responses):
            forecast = api.get_hourly_forecast(self.PLANES, include_tomorrow=True)
        self.assertEqual(sorted(forecast), list(range(8, 18)) + list(range(32, 42)))

    def test_backoff_after_failure(self):
        # Teilergebnisse werden nicht mehr gecacht -> nach einem Fehler 300 s keine API-Aufrufe
        # (das Dashboard pollt /api/status alle 2 s)
        api = ForecastSolarAPI('SECRETKEY123', 51.0, 7.0)
        ok = [self.response(200, self.watthours([0, 1])), self.response(200, self.watthours([0, 1]))]
        with mock.patch('core.forecast_solar_api.requests.get', side_effect=[self.response(500)]), \
                mock.patch('core.forecast_solar_api.time.monotonic', return_value=1000.0), \
                self.assertLogs('core.forecast_solar_api', level='ERROR'):
            self.assertEqual(api.get_hourly_forecast(self.PLANES, include_tomorrow=True), {})
        with mock.patch('core.forecast_solar_api.requests.get', side_effect=ok) as get, \
                mock.patch('core.forecast_solar_api.time.monotonic', return_value=1299.0):
            self.assertEqual(api.get_hourly_forecast(self.PLANES, include_tomorrow=True), {})
            get.assert_not_called()
        with mock.patch('core.forecast_solar_api.requests.get', side_effect=ok) as get, \
                mock.patch('core.forecast_solar_api.time.monotonic', return_value=1300.0):
            self.assertTrue(api.get_hourly_forecast(self.PLANES, include_tomorrow=True))
            self.assertEqual(get.call_count, 2)
        self.assertIsNone(api._last_failure)

    def test_mask_hides_api_key(self):
        api = ForecastSolarAPI('SECRETKEY123', 51.0, 7.0)
        url = api._build_url('estimate/watthours', 22, 45, 8.96)
        self.assertIn('SECRETKEY123', url)
        self.assertNotIn('SECRETKEY123', api._mask(url))
        self.assertNotIn('SECRETKEY123', api._mask(Exception(f"Max retries exceeded with url: {url}")))


class TestKostalExternControlWrite(unittest.TestCase):
    """H9: ExternControl-PUT nur bei echter Änderung."""

    def setUp(self):
        self.api = KostalAPI('192.0.2.1', 'pw', 'code')
        self.calls = []

    def fake_call(self, current_json):
        def call(method, url, **kwargs):
            self.calls.append(method)
            resp = mock.Mock(status_code=200, text='')
            if method == 'get':
                if isinstance(current_json, Exception):
                    resp.json.side_effect = current_json
                else:
                    resp.json.return_value = current_json
            return resp
        return call

    def test_no_put_when_value_unchanged(self):
        for enabled, value in ((False, '0'), (True, '2')):
            with self.subTest(enabled=enabled):
                self.calls = []
                current = [{'id': 'Battery:ExternControl', 'value': value}]
                with mock.patch.object(self.api, '_api_call_with_retry', side_effect=self.fake_call(current)):
                    self.assertTrue(self.api.set_external_control(enabled))
                self.assertEqual(self.calls, ['get'])

    def test_put_when_value_differs(self):
        current = [{'moduleid': 'devices:local',
                    'settings': [{'id': 'Battery:ExternControl', 'value': '2'}]}]
        with mock.patch.object(self.api, '_api_call_with_retry', side_effect=self.fake_call(current)):
            self.assertTrue(self.api.set_external_control(False))
        self.assertEqual(self.calls, ['get', 'put'])

    def test_put_when_current_value_unknown(self):
        for current in ([], {'unexpected': True}, ValueError('no json')):
            with self.subTest(current=current):
                self.calls = []
                with mock.patch.object(self.api, '_api_call_with_retry', side_effect=self.fake_call(current)):
                    self.assertTrue(self.api.set_external_control(False))
                self.assertEqual(self.calls, ['get', 'put'])


class TestSecretMasking(unittest.TestCase):
    """GET /api/config maskiert Secrets, POST überschreibt sie nicht mit der Maske."""

    CONFIG = {'installer_password': 'geheim', 'master_password': '12345', 'forecast_solar_api_key': 'abc',
              'some_token': 'tok', 'empty_password': '', 'enable_forecast_solar_api': True,
              'battery_soc_sensor': 'sensor.soc', 'max_charge_power': 3900}

    def test_mask_secrets(self):
        masked = mask_secrets(self.CONFIG)
        for key in ('installer_password', 'master_password', 'forecast_solar_api_key', 'some_token'):
            self.assertEqual(masked[key], SECRET_MASK)
        self.assertEqual(masked['empty_password'], '')
        self.assertIs(masked['enable_forecast_solar_api'], True)
        self.assertEqual(masked['battery_soc_sensor'], 'sensor.soc')
        self.assertEqual(self.CONFIG['installer_password'], 'geheim')   # Original unverändert

    def test_post_with_mask_keeps_stored_secret(self):
        stored = dict(self.CONFIG)
        stored.update(drop_masked_secrets({'installer_password': SECRET_MASK,
                                           'forecast_solar_api_key': SECRET_MASK,
                                           'max_charge_power': 3000}))
        self.assertEqual(stored['installer_password'], 'geheim')
        self.assertEqual(stored['forecast_solar_api_key'], 'abc')
        self.assertEqual(stored['max_charge_power'], 3000)

    def test_post_with_new_secret_overwrites(self):
        stored = dict(self.CONFIG)
        stored.update(drop_masked_secrets({'installer_password': 'neu'}))
        self.assertEqual(stored['installer_password'], 'neu')


if __name__ == '__main__':
    unittest.main()
