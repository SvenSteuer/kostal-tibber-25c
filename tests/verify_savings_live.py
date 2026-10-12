#!/usr/bin/env python3
"""
v1.3.7 - Abgleich „Nutzen PV + Akku" mit echten HA-Daten (nur lesend, kein Unit-Test)

1) Im HA-Container die Stundenstatistik aus der Recorder-DB lesen (mode=ro, nur Standardbibliothek):
     ssh hassio@192.168.8.177 'sudo docker exec -i homeassistant python3 - dump 2026-10-06 2026-10-11' \
         < tests/verify_savings_live.py > /tmp/savings_dump.json
2) Lokal mit core/savings.py nachrechnen und mit den Referenzwerten vergleichen:
     python3 tests/verify_savings_live.py compute /tmp/savings_dump.json [Einspeisevergütung, Standard 0.066]

Preise ohne Langzeitstatistik werden aus der States-Tabelle genommen (History-Fallback).
"""

import json
import os
import sys
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

IMPORT = 'sensor.ksem_total_active_energy_from_grid'
EXPORT = 'sensor.ksem_total_active_energy_to_grid'
PRICE = 'sensor.tibber_prices'
LOAD = 'sensor.ksem_home_consumption'
TZ = ZoneInfo('Europe/Berlin')
RECORDER_DB = 'file:/config/home-assistant_v2.db?mode=ro'

# 12.10.2026 aus den HA-Zählern nachgerechnet: (tatsächliche Netzkosten, Kosten ohne PV & Akku)
REFERENCE = {date(2026, 10, 6): (1.61, 10.5), date(2026, 10, 7): (2.52, None),
             date(2026, 10, 10): (1.29, None), date(2026, 10, 11): (1.03, 7.8)}
TOLERANCE = 0.15


def dump(first, last):
    import sqlite3
    start = datetime.combine(first, time.min, TZ) - timedelta(days=1)
    end = datetime.combine(last + timedelta(days=1), time.min, TZ)
    out = {'first': first.isoformat(), 'last': last.isoformat(), 'units': {}, 'statistics': {}, 'states': {}}
    conn = sqlite3.connect(RECORDER_DB, uri=True)
    try:
        for entity in (IMPORT, EXPORT, PRICE, LOAD):
            meta = conn.execute("SELECT id, unit_of_measurement FROM statistics_meta WHERE statistic_id = ?",
                                (entity,)).fetchone()
            if meta:
                out['units'][entity] = meta[1]
                out['statistics'][entity] = conn.execute(
                    "SELECT start_ts, sum, mean FROM statistics WHERE metadata_id = ? AND start_ts >= ? "
                    "AND start_ts < ? ORDER BY start_ts", (meta[0], start.timestamp(), end.timestamp())).fetchall()
        meta = conn.execute("SELECT metadata_id FROM states_meta WHERE entity_id = ?", (PRICE,)).fetchone()
        if meta:
            initial = conn.execute("SELECT state, last_updated_ts FROM states WHERE metadata_id = ? AND "
                                   "last_updated_ts < ? ORDER BY last_updated_ts DESC LIMIT 1",
                                   (meta[0], start.timestamp())).fetchall()
            rows = conn.execute("SELECT state, last_updated_ts FROM states WHERE metadata_id = ? AND "
                                "last_updated_ts >= ? AND last_updated_ts < ? ORDER BY last_updated_ts",
                                (meta[0], start.timestamp(), end.timestamp())).fetchall()
            out['states'][PRICE] = initial + rows
    finally:
        conn.close()
    json.dump(out, sys.stdout)


class DumpHA:
    """HA-Client-Attrappe über dem Dump (Statistik und Preis-History)."""

    FACTORS = {'Wh': 0.001, 'kW': 1000.0}      # -> kWh bzw. W wie mit units={'energy': 'kWh', 'power': 'W'}

    def __init__(self, data):
        self.data = data

    def get_statistics_during_period(self, statistic_ids, start_time, end_time, period='hour',
                                     types=('mean',), units=None):
        result = {}
        for entity in statistic_ids:
            factor = self.FACTORS.get(self.data['units'].get(entity), 1.0)
            rows = [{'start': ts * 1000,
                     'sum': None if s is None else s * factor,
                     'mean': None if m is None else m * factor}
                    for ts, s, m in self.data['statistics'].get(entity, [])
                    if start_time.timestamp() <= ts < end_time.timestamp()]
            if rows:
                result[entity] = rows
        return result

    def get_history(self, entity_id, start_time, end_time=None, minimal_response=False):
        states = self.data['states'].get(entity_id, [])
        before = [s for s in states if s[1] <= start_time.timestamp()]
        inside = [s for s in states if start_time.timestamp() < s[1] < end_time.timestamp()]
        return [{'state': state, 'last_changed': datetime.fromtimestamp(ts, timezone.utc).isoformat()}
                for state, ts in ([before[-1]] if before else []) + inside]

    def get_attributes(self, entity_id):
        return None


def compute(path, feed_in):
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'battery_manager'))
    from core.savings import SavingsData, daily_totals, day_bounds, present

    with open(path, encoding='utf-8') as f:
        data = json.load(f)
    first, last = date.fromisoformat(data['first']), date.fromisoformat(data['last'])
    start, _ = day_bounds(first, TZ)
    _, end = day_bounds(last, TZ)
    source = SavingsData(DumpHA(data), IMPORT, EXPORT, PRICE, LOAD, load_db_path='/nonexistent')
    series = source.hourly(start, end, now=end + timedelta(hours=1))
    print(f"Quellen: {series['sources']}, Preis übernommen: {series['price_filled']} h, "
          f"Einheiten: {data['units']}, Vergütung {feed_in} €/kWh")
    print(f"{'Tag':10} {'ohne':>7} {'Netz':>7} {'Nutzen':>7} {'%':>6} {'Bezug':>6} {'Einsp':>6} "
          f"{'Last':>6} {'h':>5}  Referenz")
    failed = False
    for day, totals in daily_totals(series, feed_in, TZ).items():
        r = present(totals, TZ)
        if not r:
            print(f"{day}  keine Daten")
            continue
        check = ''
        if day in REFERENCE:
            ref_with, ref_without = REFERENCE[day]
            ok = abs(r['cost_with'] - ref_with) <= TOLERANCE and (
                ref_without is None or abs(r['cost_without'] - ref_without) <= TOLERANCE)
            failed |= not ok
            check = f"Netz {ref_with:.2f}" + (f", ohne {ref_without:.2f}" if ref_without else '') + \
                    (' OK' if ok else ' ABWEICHUNG')
        print(f"{day} {r['cost_without']:7.2f} {r['cost_with']:7.2f} {r['saved']:7.2f} {r['saved_percent']:6.1f} "
              f"{r['import_kwh']:6.2f} {r['export_kwh']:6.2f} {r['load_kwh']:6.2f} "
              f"{r['hours']:>2}/{r['hours_expected']:<2}  {check}")
    return 1 if failed else 0


if __name__ == '__main__':
    args = sys.argv[1:]
    if args[:1] == ['dump'] and len(args) == 3:
        dump(date.fromisoformat(args[1]), date.fromisoformat(args[2]))
    elif args[:1] == ['compute'] and len(args) in (2, 3):
        sys.exit(compute(args[1], float(args[2]) if len(args) == 3 else 0.066))
    else:
        print(__doc__)
        sys.exit(2)
