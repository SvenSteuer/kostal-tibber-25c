#!/usr/bin/env python3
"""
v1.3.6 Phase 1b - Unit-Tests für die Datensammlung (nur Standardbibliothek).

Aufruf aus dem Repo-Root:
    python3 -m unittest discover -s tests -v
"""

import json
import os
import socket
import sqlite3
import struct
import sys
import tempfile
import threading
import types
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'battery_manager'))

# forecast_solar_api / ha_client importieren 'requests'. Lokal ohne das Paket genügt ein
# Platzhalter, weil alle Netzwerkaufrufe in den Tests gemockt werden.
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

from core.data_collection import (  # noqa: E402
    localize_naive_series, hourly_means_from_states, PVForecastArchive, LoadHourlyCollector,
    PlanArchive)
from core.ha_websocket import ws_command, HAWebsocketError  # noqa: E402
from core.ha_client import HomeAssistantClient  # noqa: E402
from core.forecast_solar_api import ForecastSolarAPI  # noqa: E402

BERLIN = ZoneInfo('Europe/Berlin')
UTC = timezone.utc
H1, H2 = timedelta(hours=1), timedelta(hours=2)
ENTITY = 'sensor.ksem_home_consumption'


def ms(dt):
    return int(dt.timestamp() * 1000)


def utc(*args):
    return datetime(*args, tzinfo=UTC)


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.db = os.path.join(self.tmp, 'calibration_data.db')

    def tearDown(self):
        self._tmp.cleanup()

    def query(self, sql, args=()):
        with sqlite3.connect(self.db) as conn:
            return conn.execute(sql, args).fetchall()


class FakeHA:
    """HA-Client-Attrappe: Statistik-Zeilen bzw. History-States oder Exceptions."""

    def __init__(self, stats=None, stats_error=None, history=None):
        self.stats = stats
        self.stats_error = stats_error
        self.history = history or []          # [(iso, state)] für alle Abfragen
        self.stats_calls = []
        self.history_calls = []

    def get_statistics_during_period(self, ids, start, end, **kwargs):
        self.stats_calls.append((ids, start, end, kwargs))
        if self.stats_error:
            raise self.stats_error
        return {ENTITY: self.stats} if self.stats is not None else {}

    def get_history(self, entity_id, start, end, minimal_response=False):
        self.history_calls.append((start, end, minimal_response))
        return [{'state': state, 'last_changed': stamp} for stamp, state in self.history]


# ==============================================================================
# DST-Umwandlung
# ==============================================================================

class TestDstConversion(TempDirTestCase):
    """Forecast.Solar liefert naive Ortszeit -> aware mit fold (25.10.2026: 02:00 doppelt)."""

    def test_repeated_hour_at_dst_end(self):
        naive = [datetime(2026, 10, 25, h, m) for h, m in ((1, 0), (2, 0), (2, 30), (2, 0), (2, 30), (3, 0))]
        aware = localize_naive_series(naive, BERLIN)
        self.assertEqual([a.astimezone(UTC).strftime('%H:%M') for a in aware],
                         ['23:00', '00:00', '00:30', '01:00', '01:30', '02:00'])
        self.assertEqual([a.utcoffset() for a in aware], [H2, H2, H2, H1, H1, H1])
        self.assertEqual(len({a.astimezone(UTC) for a in aware}), 6)   # alle eindeutig

    def test_single_occurrence_is_summer_time(self):
        aware = localize_naive_series([datetime(2026, 10, 25, 2, 0), datetime(2026, 10, 25, 3, 0)], BERLIN)
        self.assertEqual([a.astimezone(UTC) for a in aware], [utc(2026, 10, 25, 0), utc(2026, 10, 25, 2)])

    def test_normal_days_and_dst_start(self):
        aware = localize_naive_series([datetime(2026, 7, 1, 12), datetime(2026, 11, 2, 12),
                                       datetime(2026, 3, 29, 1), datetime(2026, 3, 29, 3)], BERLIN)
        self.assertEqual([a.astimezone(UTC) for a in aware],
                         [utc(2026, 7, 1, 10), utc(2026, 11, 2, 11), utc(2026, 3, 29, 0), utc(2026, 3, 29, 1)])
        # 02:30 am 29.03. existiert nicht: zoneinfo-Verhalten (Offset vor dem Sprung), keine Exception
        gap = localize_naive_series([datetime(2026, 3, 29, 2, 30)], BERLIN)[0]
        self.assertEqual(gap.utcoffset(), H1)

    def test_archive_stores_aware_timestamps(self):
        archive = PVForecastArchive(self.db)
        wh = {'2026-10-25 07:43:00': 0, '2026-10-25 08:00:00': 120, '2026-10-26 08:00:00': 90, 'meta': 1}
        n = archive.store(0, {'declination': 22, 'azimuth': 45, 'kwp': 8.96}, wh,
                          tz_name='Europe/Berlin', issued_at=datetime(2026, 10, 24, 20, 0, tzinfo=BERLIN))
        self.assertEqual(n, 3)
        rows = self.query("SELECT issued_at, plane, plane_cfg, ts_utc, ts_local, wh FROM pv_fc_archive ORDER BY ts_utc")
        self.assertEqual(rows[0][0], '2026-10-24T18:00:00+00:00')
        self.assertEqual(rows[0][1:3], (0, '22/45/8.96'))
        # 25.10. 08:00 ist schon Winterzeit (+01:00), am 24.10. wäre es +02:00
        self.assertEqual(rows[1][3:], ('2026-10-25T07:00:00+00:00', '2026-10-25T08:00:00+01:00', 120.0))

    def test_archive_time_zone_from_api_or_berlin(self):
        archive = PVForecastArchive(self.db)
        issued = utc(2026, 10, 12, 9)
        archive.store(0, {}, {'2026-10-12 12:00:00': 1}, tz_name='Europe/Lisbon', issued_at=issued)
        archive.store(1, {}, {'2026-10-12 12:00:00': 1}, tz_name='Mars/Olympus', issued_at=issued)
        archive.store(2, {}, {'2026-10-12 12:00:00': 1}, tz_name=None, issued_at=issued)
        self.assertEqual(self.query("SELECT plane, ts_utc FROM pv_fc_archive ORDER BY plane"),
                         [(0, '2026-10-12T11:00:00+00:00'), (1, '2026-10-12T10:00:00+00:00'),
                          (2, '2026-10-12T10:00:00+00:00')])

    def test_unchanged_fetch_is_not_stored_again(self):
        archive = PVForecastArchive(self.db)
        wh = {'2026-10-12 12:00:00': 1000, '2026-10-12 13:00:00': 1800}
        self.assertEqual(archive.store(0, {}, wh, issued_at=utc(2026, 10, 12, 9)), 2)
        self.assertEqual(archive.store(0, {}, dict(wh), issued_at=utc(2026, 10, 12, 9, 15)), 0)
        self.assertEqual(archive.store(1, {}, dict(wh), issued_at=utc(2026, 10, 12, 9, 15)), 2)  # andere Ebene
        wh['2026-10-12 13:00:00'] = 1900
        self.assertEqual(archive.store(0, {}, wh, issued_at=utc(2026, 10, 12, 9, 30)), 2)
        self.assertEqual(self.query("SELECT COUNT(DISTINCT issued_at) FROM pv_fc_archive WHERE plane = 0"), [(2,)])


# ==============================================================================
# Stundenbeschriftung
# ==============================================================================

class TestLoadHourLabels(TempDirTestCase):
    """load_hourly: Mittel über [h, h+1) wird mit h beschriftet (nicht mit h+1)."""

    def rows(self):
        return self.query("SELECT hour_start_utc, hour_start_local, local_hour, mean_w, source "
                          "FROM load_hourly ORDER BY hour_start_utc")

    def test_statistics_row_labelled_with_hour_start(self):
        ha = FakeHA(stats=[{'start': ms(utc(2026, 10, 12, 5)), 'end': ms(utc(2026, 10, 12, 6)), 'mean': 1806.05}])
        collector = LoadHourlyCollector(ha, self.db, backfill_days=1)
        self.assertEqual(collector.collect(now=utc(2026, 10, 12, 6, 7)), 24)
        ids, start, end, kwargs = ha.stats_calls[0]
        self.assertEqual((ids, start, end), ([ENTITY], utc(2026, 10, 11, 6), utc(2026, 10, 12, 6)))
        self.assertEqual(kwargs, {'period': 'hour', 'types': ['mean'], 'units': {'power': 'W'}})
        self.assertEqual(self.rows()[-1], ('2026-10-12T05:00:00+00:00', '2026-10-12T07:00:00+02:00', 7,
                                           1806.05, 'statistics'))

    def test_iso_start_from_older_ha(self):
        ha = FakeHA(stats=[{'start': '2026-10-12T05:00:00+00:00', 'mean': 500.0}])
        LoadHourlyCollector(ha, self.db, backfill_days=1).collect(now=utc(2026, 10, 12, 6, 7))
        self.assertEqual(self.rows()[-1][3], 500.0)

    def test_history_means_are_not_shifted(self):
        states = [(utc(2026, 10, 12, 5, 30), 1000.0), (utc(2026, 10, 12, 7), 2000.0),
                  (utc(2026, 10, 12, 7, 15), 3000.0)]
        means = hourly_means_from_states(states, utc(2026, 10, 12, 6), utc(2026, 10, 12, 8))
        self.assertEqual(means, {utc(2026, 10, 12, 6): (1000.0, 1.0),
                                 utc(2026, 10, 12, 7): (0.25 * 2000 + 0.75 * 3000, 1.0)})

    def test_dst_end_gives_two_rows_for_local_hour_2(self):
        stats = [{'start': ms(utc(2026, 10, 24, 23) + k * H1), 'mean': 100.0 * (k + 1)} for k in range(4)]
        collector = LoadHourlyCollector(FakeHA(stats=stats), self.db, backfill_days=1)
        collector.collect(now=utc(2026, 10, 25, 3, 10))
        rows = self.rows()[-4:]
        self.assertEqual([r[2] for r in rows], [1, 2, 2, 3])
        self.assertEqual([r[1][-6:] for r in rows], ['+02:00', '+02:00', '+01:00', '+01:00'])
        self.assertEqual([r[3] for r in rows], [100.0, 200.0, 300.0, 400.0])

    def test_hourly_run_fetches_only_recent_hours(self):
        ha = FakeHA(stats=[{'start': ms(utc(2026, 10, 12, 5)), 'mean': 1.0}])
        collector = LoadHourlyCollector(ha, self.db, backfill_days=10)
        collector.collect(now=utc(2026, 10, 12, 6, 7))
        collector.collect(now=utc(2026, 10, 12, 7, 7))
        _, start, end, _ = ha.stats_calls[1]
        self.assertEqual((start, end), (utc(2026, 10, 12, 4), utc(2026, 10, 12, 7)))   # 3 h Nachprüfung
        self.assertEqual(self.query("SELECT COUNT(*) FROM load_hourly"), [(241,)])


# ==============================================================================
# NULL statt 0
# ==============================================================================

class TestNullNotZero(TempDirTestCase):
    """Ausfälle werden als NULL gespeichert, nie als 0; ohne Quelle wird nichts geschrieben."""

    def mean(self, hour):
        return self.query("SELECT mean_w FROM load_hourly WHERE hour_start_utc = ?", (hour.isoformat(),))

    def test_missing_statistics_hour_is_null(self):
        stats = [{'start': ms(utc(2026, 10, 12, 3)), 'mean': 700.0}, {'start': ms(utc(2026, 10, 12, 5)), 'mean': 0.0}]
        LoadHourlyCollector(FakeHA(stats=stats), self.db, backfill_days=1).collect(now=utc(2026, 10, 12, 6, 7))
        self.assertEqual(self.mean(utc(2026, 10, 12, 3)), [(700.0,)])
        self.assertEqual(self.mean(utc(2026, 10, 12, 4)), [(None,)])
        self.assertEqual(self.mean(utc(2026, 10, 12, 5)), [(0.0,)])              # echte 0 bleibt 0

    def test_unavailable_history_is_null(self):
        history = [('2026-10-12T03:00:00+00:00', 'unavailable'),
                   ('2026-10-12T04:00:00+00:00', '800'), ('2026-10-12T04:20:00+00:00', 'unknown'),
                   ('2026-10-12T05:00:00+00:00', 'unavailable'), ('2026-10-12T05:20:00+00:00', '600')]
        ha = FakeHA(stats_error=HAWebsocketError('connect failed'), history=history)
        LoadHourlyCollector(ha, self.db, backfill_days=1).collect(now=utc(2026, 10, 12, 6, 7))
        self.assertTrue(all(call[2] for call in ha.history_calls))                 # minimal_response
        self.assertEqual(self.mean(utc(2026, 10, 12, 3)), [(None,)])               # ganz ausgefallen
        self.assertEqual(self.mean(utc(2026, 10, 12, 4)), [(None,)])               # nur 20 min gültig
        self.assertEqual(self.mean(utc(2026, 10, 12, 5)), [(600.0,)])              # 40 min gültig
        self.assertEqual(self.query("SELECT source, coverage FROM load_hourly WHERE hour_start_utc = ?",
                                    (utc(2026, 10, 12, 5).isoformat(),)), [('history', 0.6667)])

    def test_empty_statistics_fall_back_to_history(self):
        ha = FakeHA(stats=[], history=[('2026-10-12T00:00:00+00:00', '900')])
        LoadHourlyCollector(ha, self.db, backfill_days=1).collect(now=utc(2026, 10, 12, 6, 7))
        self.assertEqual(self.mean(utc(2026, 10, 12, 5)), [(900.0,)])

    def test_null_never_overwrites_a_value(self):
        hour = utc(2026, 10, 12, 5)
        LoadHourlyCollector(FakeHA(stats=[{'start': ms(hour), 'mean': 1200.0}]), self.db,
                            backfill_days=1).collect(now=utc(2026, 10, 12, 6, 7))
        LoadHourlyCollector(FakeHA(stats=[{'start': ms(hour - H1), 'mean': 5.0}]), self.db,
                            backfill_days=1).collect(now=utc(2026, 10, 12, 7, 7))
        self.assertEqual(self.mean(hour), [(1200.0,)])
        self.assertEqual(self.mean(utc(2026, 10, 12, 6)), [(None,)])
        # ... aber NULL wird durch einen später erstellten Wert ersetzt
        LoadHourlyCollector(FakeHA(stats=[{'start': ms(utc(2026, 10, 12, 6)), 'mean': 950.0}]), self.db,
                            backfill_days=1).collect(now=utc(2026, 10, 12, 8, 7))
        self.assertEqual(self.mean(utc(2026, 10, 12, 6)), [(950.0,)])

    def test_unreachable_source_writes_nothing(self):
        ha = FakeHA(stats_error=HAWebsocketError('auth failed'), history=[])
        self.assertEqual(LoadHourlyCollector(ha, self.db, backfill_days=1).collect(now=utc(2026, 10, 12, 6, 7)), 0)
        self.assertEqual(self.query("SELECT COUNT(*) FROM load_hourly"), [(0,)])


# ==============================================================================
# Archivfehler blockieren nichts
# ==============================================================================

class TestArchiveErrorsDoNotBlock(TempDirTestCase):
    PLANES = [{'declination': 22, 'azimuth': 45, 'kwp': 8.96},
              {'declination': 22, 'azimuth': -135, 'kwp': 10.665}]

    @staticmethod
    def responses():
        base = datetime.now().astimezone().date()
        wh = {f"{base + timedelta(days=d)} {h:02d}:00:00": (h - 7) * 500 for d in (0, 1) for h in range(8, 18)}
        out = []
        for _ in range(2):
            resp = mock.Mock(status_code=200, text='')
            resp.json.return_value = {'result': dict(wh),
                                      'message': {'info': {'timezone': 'Europe/Berlin'}}}
            out.append(resp)
        return out

    def forecast_with(self, archive):
        api = ForecastSolarAPI('SECRETKEY123', 51.0, 7.0)
        api.set_archive(archive)
        with mock.patch('core.forecast_solar_api.requests.get', side_effect=self.responses()):
            return api.get_hourly_forecast(self.PLANES, include_tomorrow=True)

    def test_forecast_unchanged_when_archive_raises(self):
        broken = mock.Mock()
        broken.store.side_effect = RuntimeError('disk full')
        with self.assertLogs('core.forecast_solar_api', level='WARNING'):
            forecast = self.forecast_with(broken)
        self.assertEqual(forecast, self.forecast_with(None))
        self.assertEqual(sorted(forecast), list(range(8, 18)) + list(range(32, 42)))

    def test_forecast_unchanged_with_unwritable_archive(self):
        archive = PVForecastArchive(os.path.join(self.tmp, 'missing', 'dir', 'x.db'))
        with self.assertLogs('core.data_collection', level='WARNING'):
            forecast = self.forecast_with(archive)
        self.assertEqual(sorted(forecast), list(range(8, 18)) + list(range(32, 42)))

    def test_forecast_archived_per_plane(self):
        archive = PVForecastArchive(self.db)
        self.assertTrue(self.forecast_with(archive))
        self.assertEqual(self.query("SELECT plane, COUNT(*) FROM pv_fc_archive GROUP BY plane"), [(0, 20), (1, 20)])

    def test_plan_archive_error_returns_false(self):
        not_a_dir = os.path.join(self.tmp, 'file')
        open(not_a_dir, 'w').close()
        with self.assertLogs('core.data_collection', level='WARNING'):
            self.assertFalse(PlanArchive(not_a_dir).write({'hourly_soc': [50.0]}, 50.0, {}))

    def test_load_collector_errors_are_swallowed(self):
        ha = mock.Mock()
        ha.get_statistics_during_period.side_effect = RuntimeError('boom')
        ha.get_history.side_effect = RuntimeError('boom')
        with self.assertLogs('core.data_collection', level='WARNING'):
            self.assertEqual(LoadHourlyCollector(ha, self.db).collect(), 0)
        unwritable = LoadHourlyCollector(FakeHA(stats=[]), os.path.join(self.tmp, 'missing', 'x.db'))
        with self.assertLogs('core.data_collection', level='WARNING'):
            self.assertEqual(unwritable.collect(), 0)


# ==============================================================================
# Aufbewahrung und Planarchiv-Inhalt
# ==============================================================================

class TestRetention(TempDirTestCase):

    def test_pv_archive_keeps_60_days(self):
        archive = PVForecastArchive(self.db)
        now = utc(2026, 10, 12, 9)
        for days, wh in ((61, 1), (59, 2), (0, 3)):
            archive.store(0, {}, {'2026-10-12 12:00:00': wh}, issued_at=now - timedelta(days=days))
        archive.purge(now)
        self.assertEqual(self.query("SELECT wh FROM pv_fc_archive ORDER BY issued_at"), [(2.0,), (3.0,)])

    def test_plan_archive_keeps_14_days(self):
        plans = os.path.join(self.tmp, 'plans')
        os.makedirs(plans)
        today = date(2026, 10, 12)
        for days in (20, 15, 14, 1):
            open(os.path.join(plans, f"{today - timedelta(days=days)}.jsonl"), 'w').close()
        open(os.path.join(plans, 'notes.txt'), 'w').close()
        PlanArchive(plans).write(None, 42.0, {}, now=datetime(2026, 10, 12, 9, 0, tzinfo=BERLIN))
        self.assertEqual(sorted(os.listdir(plans)),
                         ['2026-09-28.jsonl', '2026-10-11.jsonl', '2026-10-12.jsonl', 'notes.txt'])

    def test_load_hourly_retention(self):
        collector = LoadHourlyCollector(FakeHA(stats=[{'start': ms(utc(2026, 10, 12, 5)), 'mean': 1.0}]),
                                        self.db, backfill_days=1, retention_days=1)
        collector.collect(now=utc(2026, 10, 12, 6, 7))
        collector._last_purge = None
        collector.collect(now=utc(2026, 10, 13, 12, 7))
        oldest = self.query("SELECT MIN(hour_start_utc) FROM load_hourly")[0][0]
        self.assertEqual(oldest, '2026-10-12T13:00:00+00:00')   # Grenze: now - 1 Tag (12:07)

    def test_plan_line_content(self):
        plans = os.path.join(self.tmp, 'plans')
        schedule = {'hourly_soc': [55.123456, 50.0], 'hourly_charging': [0.0, 1.5],
                    'hourly_pv': [0.31234567, 0.0], 'hourly_consumption': [0.7, 0.9],
                    'hourly_prices': [0.33741234, 0.2911], 'start_time': '2026-10-12T09:03:00+02:00',
                    'charging_windows': [{'hour': 1, 'charge_kwh': 1.5, 'price': 0.2911, 'reason': 'x'}],
                    'min_soc_reached': 50.0, 'total_charging_kwh': 1.5}
        config = {'auto_safety_soc': 7, 'battery_capacity': 10.6, 'forecast_solar_api_key': 'SECRET',
                  'installer_password': 'SECRET'}
        now = datetime(2026, 10, 12, 9, 3, 7, tzinfo=BERLIN)
        archive = PlanArchive(plans)
        self.assertTrue(archive.write(schedule, 55.1, config, now=now))
        self.assertTrue(archive.write(None, 55.1, config, now=now))
        with open(os.path.join(plans, '2026-10-12.jsonl'), encoding='utf-8') as f:
            raw = f.read()
        self.assertNotIn('SECRET', raw)
        first, second = [json.loads(line) for line in raw.splitlines()]
        self.assertEqual(first['ts'], '2026-10-12T09:03:07+02:00')
        self.assertEqual((first['soc'], first['ok'], first['min_soc_reached']), (55.1, True, 50.0))
        self.assertEqual(first['prices'], [0.3374, 0.2911])
        self.assertEqual(first['pv'], [0.3123, 0.0])
        self.assertEqual(first['consumption'], [0.7, 0.9])
        self.assertEqual(first['windows'][0]['hour'], 1)
        self.assertEqual(first['params'], {'auto_safety_soc': 7, 'battery_capacity': 10.6})
        self.assertEqual((second['ok'], 'prices' in second), (False, False))


# ==============================================================================
# Websocket-Client (gegen einen lokalen Fake-Server)
# ==============================================================================

def _server_frame(payload, opcode=0x1, fin=True):
    head = bytes([(0x80 if fin else 0) | opcode])
    n = len(payload)
    if n < 126:
        return head + bytes([n]) + payload
    return head + bytes([126]) + struct.pack('!H', n) + payload


def _read_client_frame(conn):
    def exact(n):
        data = b''
        while len(data) < n:
            data += conn.recv(n - len(data))
        return data
    b1, b2 = exact(2)
    n = b2 & 0x7F
    if n == 126:
        n = struct.unpack('!H', exact(2))[0]
    elif n == 127:
        n = struct.unpack('!Q', exact(8))[0]
    assert b2 & 0x80, "client frames must be masked"
    mask = exact(4)
    return b1 & 0x0F, bytes(b ^ mask[i % 4] for i, b in enumerate(exact(n)))


class TestWebsocketClient(unittest.TestCase):

    def serve(self, script):
        srv = socket.socket()
        srv.bind(('127.0.0.1', 0))
        srv.listen(1)
        self.seen = []

        def run():
            conn, _ = srv.accept()
            with conn:
                request = b''
                while b'\r\n\r\n' not in request:
                    request += conn.recv(4096)
                self.seen.append(request.decode())
                conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                             b"Connection: Upgrade\r\n\r\n" + _server_frame(b'{"type":"auth_required"}'))
                script(conn)
            srv.close()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        return f"http://127.0.0.1:{srv.getsockname()[1]}/core"

    def test_statistics_command_roundtrip(self):
        result = {ENTITY: [{'start': 1, 'mean': 1806.05, 'pad': 'x' * 300}]}

        def script(conn):
            _, auth = _read_client_frame(conn)
            self.seen.append(json.loads(auth))
            conn.sendall(_server_frame(b'{"type":"auth_ok"}'))
            _, cmd = _read_client_frame(conn)
            self.seen.append(json.loads(cmd))
            conn.sendall(_server_frame(b'hb', opcode=0x9))                  # Ping
            self.seen.append(_read_client_frame(conn))                       # Pong erwartet
            body = json.dumps({'id': 1, 'type': 'result', 'success': True, 'result': result}).encode()
            conn.sendall(_server_frame(body[:10], fin=False) + _server_frame(body[10:], opcode=0x0))

        url = self.serve(script)
        self.assertEqual(ws_command(url, 'TOKEN', {'type': 'recorder/statistics_during_period'}, timeout=5), result)
        self.assertIn('GET /core/websocket HTTP/1.1', self.seen[0])
        self.assertEqual(self.seen[1], {'type': 'auth', 'access_token': 'TOKEN'})
        self.assertEqual(self.seen[2], {'type': 'recorder/statistics_during_period', 'id': 1})
        self.assertEqual(self.seen[3], (0xA, b'hb'))

    def test_auth_invalid_raises(self):
        def script(conn):
            _read_client_frame(conn)
            conn.sendall(_server_frame(b'{"type":"auth_invalid"}'))

        with self.assertRaises(HAWebsocketError):
            ws_command(self.serve(script), 'BAD', {'type': 'x'}, timeout=5)

    def test_command_error_raises(self):
        def script(conn):
            _read_client_frame(conn)
            conn.sendall(_server_frame(b'{"type":"auth_ok"}'))
            _read_client_frame(conn)
            conn.sendall(_server_frame(b'{"id":1,"type":"result","success":false,"error":{"code":"x"}}'))

        with self.assertRaises(HAWebsocketError):
            ws_command(self.serve(script), 'TOKEN', {'type': 'x'}, timeout=5)

    def test_no_token_or_no_server_raises(self):
        with self.assertRaises(HAWebsocketError):
            ws_command('http://127.0.0.1:9/core', None, {'type': 'x'})
        with self.assertRaises(HAWebsocketError):
            ws_command('http://127.0.0.1:9/core', 'TOKEN', {'type': 'x'}, timeout=2)

    def test_ha_client_builds_statistics_command(self):
        client = HomeAssistantClient()
        with mock.patch('core.ha_client.ws_command', return_value={}) as ws:
            client.get_statistics_during_period([ENTITY], utc(2026, 10, 2), utc(2026, 10, 12),
                                                types=['mean'], units={'power': 'W'})
        command = ws.call_args[0][2]
        self.assertEqual(command, {'type': 'recorder/statistics_during_period',
                                   'start_time': '2026-10-02T00:00:00+00:00',
                                   'end_time': '2026-10-12T00:00:00+00:00',
                                   'statistic_ids': [ENTITY], 'period': 'hour', 'types': ['mean'],
                                   'units': {'power': 'W'}})


if __name__ == '__main__':
    unittest.main()
