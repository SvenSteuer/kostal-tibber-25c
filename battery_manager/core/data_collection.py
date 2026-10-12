#!/usr/bin/env python3
"""
v1.3.6 - Datensammlung (Phase 1b) für die Kalibrierung des neuen Planers

Sammelt nur Daten und ändert kein Ladeverhalten:
- pv_fc_archive: jede Forecast.Solar-Antwort (rohe watthours je Ebene) mit Abrufzeit
- load_hourly:   Stundenmittel der Hauslast (KSEM, W) je Stunde [h, h+1), beschriftet
                 mit dem Stundenbeginn h; Ausfälle als NULL, nie als 0
- Planarchiv:    jede Planberechnung als Zeile in /data/plans/YYYY-MM-DD.jsonl

Fehler beim Archivieren werden nur geloggt und blockieren nie Prognose oder Planung.
Zeitstempel werden zeitzonenbewusst gespeichert (UTC und Ortszeit mit Offset), damit
die doppelte Stunde am Ende der Sommerzeit (25.10.2026, 02:00-02:59) eindeutig bleibt.
"""

import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

CALIBRATION_DB_PATH = '/data/calibration_data.db'
PLAN_ARCHIVE_DIR = '/data/plans'
DEFAULT_TZ = 'Europe/Berlin'

PV_FC_RETENTION_DAYS = 60
PLAN_RETENTION_DAYS = 14
LOAD_RETENTION_DAYS = 400
LOAD_BACKFILL_DAYS = 10
LOAD_RECHECK_HOURS = 3          # jüngste Stunden erneut abfragen (Statistik wird erst hh:00:10 erstellt)
HISTORY_MIN_COVERAGE = 0.5      # History-Fallback: weniger gültige Zeit in der Stunde -> NULL
HISTORY_CHUNK_HOURS = 6         # KSEM schreibt ~1 State/s - History in Blöcken abfragen
PURGE_INTERVAL_SECONDS = 3600

_PLAN_FILE_RE = re.compile(r'^(\d{4}-\d{2}-\d{2})\.jsonl$')


def _zone(tz_name):
    """ZoneInfo für tz_name, bei fehlendem/ungültigem Namen Europe/Berlin."""
    if tz_name:
        try:
            return ZoneInfo(tz_name)
        except Exception:
            logger.debug(f"Unknown time zone {tz_name!r} - using {DEFAULT_TZ}")
    return ZoneInfo(DEFAULT_TZ)


def localize_naive_series(naive_times, tz):
    """Naive Ortszeiten (in Lieferreihenfolge) in aware datetimes umwandeln.

    Mehrdeutige Zeiten am Ende der Sommerzeit (25.10.2026: 02:00-02:59 doppelt): erstes
    Auftreten fold=0 (CEST), sobald die Wanduhr innerhalb der doppelten Stunde zurückspringt
    fold=1 (CET). Nicht existierende Zeiten (Beginn der Sommerzeit) behalten fold=0, d. h.
    den Offset vor dem Sprung (zoneinfo-Verhalten).
    """
    result = []
    prev = None
    second_pass = False
    for naive in naive_times:
        first = naive.replace(tzinfo=tz, fold=0)
        repeated = naive.replace(tzinfo=tz, fold=1)
        if first.utcoffset() > repeated.utcoffset():     # mehrdeutig (doppelte Stunde)
            if prev is not None and naive <= prev:
                second_pass = True
            result.append(repeated if second_pass else first)
        else:
            second_pass = False
            result.append(first)
        prev = naive
    return result


def floor_hour(dt):
    """Auf den Stundenbeginn abrunden (aware, UTC)."""
    return dt.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def _utc_iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


def _connect(db_path):
    return sqlite3.connect(db_path, timeout=10)


def _round_floats(obj, digits=4):
    if isinstance(obj, float):
        return round(obj, digits)
    if isinstance(obj, dict):
        return {k: _round_floats(v, digits) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_round_floats(v, digits) for v in obj]
    return obj


# ==============================================================================
# pv_fc_archive
# ==============================================================================

class PVForecastArchive:
    """Rohe Forecast.Solar-watthours je Ebene und Abruf (SQLite, Aufbewahrung 60 Tage).

    Ein Abruf, dessen Ergebnis für die Ebene identisch mit dem zuletzt gespeicherten ist,
    erzeugt keine neuen Zeilen (der 15-min-Cache wird sonst ~100x am Tag unverändert
    gespeichert). Für eine leckfreie Auswertung gilt: maßgeblich ist die jüngste Zeile mit
    issued_at <= Entscheidungszeitpunkt.
    """

    def __init__(self, db_path=CALIBRATION_DB_PATH, retention_days=PV_FC_RETENTION_DAYS):
        self.db_path = db_path
        self.retention_days = retention_days
        self._schema_ok = False
        self._last_digest = {}      # plane index -> digest of the last stored result
        self._last_purge = None     # time.monotonic()

    def _ensure_schema(self, conn):
        if self._schema_ok:
            return
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pv_fc_archive (
                issued_at TEXT NOT NULL,     -- Abrufzeit (UTC, ISO)
                plane INTEGER NOT NULL,      -- Index in forecast_solar_planes
                plane_cfg TEXT,              -- declination/azimuth/kwp
                ts_utc TEXT NOT NULL,        -- Zeitstempel des Werts (UTC, ISO)
                ts_local TEXT NOT NULL,      -- dto. Ortszeit mit Offset
                wh REAL,                     -- Wh wie geliefert (estimate/watthours: kumuliert ab Tagesbeginn)
                PRIMARY KEY (issued_at, plane, ts_utc)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pv_fc_ts ON pv_fc_archive(ts_utc)")
        self._schema_ok = True

    def store(self, plane_index, plane, watt_hours, tz_name=None, issued_at=None):
        """Rohe watthours einer Ebene archivieren.

        Args:
            plane_index: Index der Ebene
            plane: dict mit 'declination', 'azimuth', 'kwp'
            watt_hours: data['result'] der API ({"YYYY-MM-DD HH:MM:SS": Wh}, naive Ortszeit)
            tz_name: Zeitzone der Zeitstempel (message.info.timezone), sonst Europe/Berlin
            issued_at: Abrufzeit (aware), Standard jetzt

        Returns:
            Anzahl gespeicherter Zeilen. Wirft nie eine Exception.
        """
        try:
            issued_at = issued_at or datetime.now(timezone.utc)
            entries = []
            for key, value in watt_hours.items():
                try:
                    entries.append((datetime.strptime(key, "%Y-%m-%d %H:%M:%S"), float(value)))
                except (TypeError, ValueError):
                    continue
            if not entries:
                return 0

            plane_cfg = f"{plane.get('declination')}/{plane.get('azimuth')}/{plane.get('kwp')}"
            digest = hashlib.sha1(json.dumps([plane_cfg, tz_name, entries], default=str)
                                  .encode('utf-8')).hexdigest()
            if self._last_digest.get(plane_index) == digest:
                return 0

            stamps = localize_naive_series([naive for naive, _ in entries], _zone(tz_name))
            issued = issued_at.astimezone(timezone.utc).isoformat(timespec='seconds')
            rows = [(issued, plane_index, plane_cfg, _utc_iso(aware), aware.isoformat(), wh)
                    for aware, (_, wh) in zip(stamps, entries)]
            with closing(_connect(self.db_path)) as conn, conn:
                self._ensure_schema(conn)
                conn.executemany("INSERT OR IGNORE INTO pv_fc_archive VALUES (?, ?, ?, ?, ?, ?)", rows)
            self._last_digest[plane_index] = digest
            self._purge_if_due(issued_at)
            return len(rows)
        except Exception as e:
            logger.warning(f"PV forecast archive: store failed ({e}) - forecast not affected")
            return 0

    def _purge_if_due(self, now):
        if self._last_purge is not None and time.monotonic() - self._last_purge < PURGE_INTERVAL_SECONDS:
            return
        self.purge(now)

    def purge(self, now=None):
        """Abrufe älter als retention_days löschen. Wirft nie eine Exception."""
        try:
            now = now or datetime.now(timezone.utc)
            cutoff = (now - timedelta(days=self.retention_days)).astimezone(timezone.utc)
            with closing(_connect(self.db_path)) as conn, conn:
                self._ensure_schema(conn)
                deleted = conn.execute("DELETE FROM pv_fc_archive WHERE issued_at < ?",
                                       (cutoff.isoformat(timespec='seconds'),)).rowcount
            self._last_purge = time.monotonic()
            if deleted:
                logger.info(f"PV forecast archive: purged {deleted} rows older than {self.retention_days} days")
        except Exception as e:
            logger.warning(f"PV forecast archive: purge failed ({e})")


# ==============================================================================
# load_hourly
# ==============================================================================

def hourly_means_from_states(states, start, end, min_coverage=HISTORY_MIN_COVERAGE):
    """Zeitgewichtete Stundenmittel aus HA-History für jede Stunde in [start, end).

    Args:
        states: [(aware datetime, float oder None)], chronologisch; None = unavailable/unknown.
                Ein Wert gilt bis zur nächsten Änderung, Zeiten mit None zählen nicht.
        start, end: Stundengrenzen (aware)
        min_coverage: Mindestanteil gültiger Zeit, sonst Mittelwert None

    Returns:
        {Stundenbeginn (UTC): (mean_w oder None, coverage 0..1)}
    """
    start = floor_hour(start)
    n_hours = max(0, int((end - start).total_seconds() // 3600))
    limit = start + timedelta(hours=n_hours)
    sums = [0.0] * n_hours
    secs = [0.0] * n_hours
    for idx, (ts, value) in enumerate(states):
        seg_end = states[idx + 1][0] if idx + 1 < len(states) else limit
        cur, seg_end = max(ts, start), min(seg_end, limit)
        if value is None:
            continue
        while cur < seg_end:
            k = int((cur - start).total_seconds() // 3600)
            piece_end = min(seg_end, start + timedelta(hours=k + 1))
            dt = (piece_end - cur).total_seconds()
            sums[k] += value * dt
            secs[k] += dt
            cur = piece_end
    result = {}
    for k in range(n_hours):
        coverage = secs[k] / 3600.0
        mean = sums[k] / secs[k] if secs[k] > 0 and coverage >= min_coverage else None
        result[start + timedelta(hours=k)] = (mean, round(coverage, 4))
    return result


def _parse_stat_start(value):
    """'start' aus recorder/statistics_during_period: Epoch-ms (HA >= 2023.3) oder ISO-String."""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000.0, timezone.utc)
    return datetime.fromisoformat(str(value).replace('Z', '+00:00')).astimezone(timezone.utc)


def parse_history_states(history):
    """HA-History-Einträge -> [(aware datetime UTC, float oder None)], chronologisch.

    unavailable/unknown/nicht numerisch -> None (Lücke, nie 0); Einträge ohne Zeitstempel fehlen.
    """
    states = []
    for entry in history or []:
        stamp = entry.get('last_changed') or entry.get('last_updated')
        if not stamp:
            continue
        try:
            value = float(entry.get('state'))
        except (TypeError, ValueError):
            value = None
        ts = datetime.fromisoformat(stamp.replace('Z', '+00:00')).astimezone(timezone.utc)
        states.append((ts, value))
    states.sort(key=lambda s: s[0])
    return states


def history_hourly_means(ha_client, entity_id, start, end, min_coverage=HISTORY_MIN_COVERAGE):
    """{Stundenbeginn: (mean oder None, coverage)} aus /api/history/period, in Blöcken abgefragt.

    Blöcke ohne Antwort fehlen im Ergebnis (KSEM schreibt ~1 State/s, deshalb Blöcke von
    HISTORY_CHUNK_HOURS und minimal_response).
    """
    hours = {}
    chunk_start = start
    while chunk_start < end:
        chunk_end = min(end, chunk_start + timedelta(hours=HISTORY_CHUNK_HOURS))
        history = ha_client.get_history(entity_id, chunk_start, chunk_end, minimal_response=True)
        if history:
            hours.update(hourly_means_from_states(parse_history_states(history), chunk_start,
                                                  chunk_end, min_coverage))
        chunk_start = chunk_end
    return hours


def read_load_hourly(start, end, entity_id, db_path=CALIBRATION_DB_PATH):
    """v1.3.7: {Stundenbeginn (UTC): mean_w oder None} aus load_hourly für [start, end).

    Nur lesend (mode=ro, legt keine Datei an). Fehlt DB oder Tabelle: {}. Wirft nie.
    """
    try:
        uri = f"file:{db_path}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=10)) as conn:
            rows = conn.execute(
                "SELECT hour_start_utc, mean_w FROM load_hourly "
                "WHERE hour_start_utc >= ? AND hour_start_utc < ? AND entity_id = ?",
                (_utc_iso(start), _utc_iso(end), entity_id)).fetchall()
        return {datetime.fromisoformat(hour): mean for hour, mean in rows}
    except Exception as e:
        logger.debug(f"load_hourly not readable ({e})")
        return {}


class LoadHourlyCollector:
    """Stundenmittel der Hauslast je [h, h+1) in SQLite (Tabelle load_hourly).

    Quelle: HA-Langzeitstatistik (recorder/statistics_during_period, Websocket über den
    Supervisor-Proxy), Fallback: Integration von /api/history/period. Beim ersten Lauf
    Backfill über backfill_days, danach stündlich die fehlenden und die jüngsten Stunden.
    Stunden ohne Daten werden als NULL gespeichert; ist die Quelle nicht erreichbar, wird
    nichts geschrieben (nächster Lauf holt es nach). Ein vorhandener Wert wird nie durch
    NULL überschrieben.
    """

    def __init__(self, ha_client, db_path=CALIBRATION_DB_PATH,
                 entity_id='sensor.ksem_home_consumption',
                 backfill_days=LOAD_BACKFILL_DAYS, retention_days=LOAD_RETENTION_DAYS,
                 tz_name=DEFAULT_TZ):
        self.ha_client = ha_client
        self.db_path = db_path
        self.entity_id = entity_id
        self.backfill_days = backfill_days
        self.retention_days = retention_days
        self.tz_name = tz_name
        self._schema_ok = False
        self._last_purge = None

    def _ensure_schema(self, conn):
        if self._schema_ok:
            return
        conn.execute("""
            CREATE TABLE IF NOT EXISTS load_hourly (
                hour_start_utc TEXT PRIMARY KEY,   -- Stundenbeginn h (UTC, ISO); Wert gilt für [h, h+1)
                hour_start_local TEXT NOT NULL,    -- dto. Ortszeit mit Offset
                local_hour INTEGER NOT NULL,       -- Stunde des Tages (Ortszeit) des Stundenbeginns
                mean_w REAL,                       -- Stundenmittel in W (= Wh in der Stunde); NULL = Ausfall
                coverage REAL,                     -- History-Fallback: Anteil gültiger Zeit; Statistik: NULL
                source TEXT NOT NULL,              -- 'statistics' oder 'history'
                entity_id TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        self._schema_ok = True

    def collect(self, now=None):
        """Fehlende Stunden nachtragen. Wirft nie eine Exception.

        Returns:
            Anzahl geschriebener Stunden (0, wenn nichts fällig war oder die Quelle fehlte)
        """
        try:
            now = now or datetime.now(timezone.utc)
            end = floor_hour(now)
            earliest = end - timedelta(days=self.backfill_days)
            with closing(_connect(self.db_path)) as conn, conn:
                self._ensure_schema(conn)
                newest = conn.execute("SELECT MAX(hour_start_utc) FROM load_hourly").fetchone()[0]
            if newest is None:
                start = earliest
                logger.info(f"load_hourly: backfill {self.backfill_days} days for {self.entity_id}")
            else:
                newest = datetime.fromisoformat(newest)
                start = max(earliest, min(newest + timedelta(hours=1),
                                          end - timedelta(hours=LOAD_RECHECK_HOURS)))
            if start >= end:
                return 0

            hours, source = self._fetch_statistics(start, end), 'statistics'
            if hours is None:
                hours, source = self._fetch_history(start, end), 'history'
            if not hours:
                logger.warning(f"load_hourly: no data source reachable for {start:%Y-%m-%d %H:%M}Z - "
                               f"{end:%Y-%m-%d %H:%M}Z, retrying next hour")
                return 0

            written = self._store(hours, source, now)
            nulls = sum(1 for mean, _ in hours.values() if mean is None)
            logger.info(f"load_hourly: {written} hours from {source} ({nulls} NULL)")
            self._purge_if_due(now)
            return written
        except Exception as e:
            logger.warning(f"load_hourly: collect failed ({e})")
            return 0

    def _fetch_statistics(self, start, end):
        """{Stundenbeginn: (mean_w oder None, None)} oder None, wenn die Statistik nicht verfügbar ist."""
        try:
            result = self.ha_client.get_statistics_during_period(
                [self.entity_id], start, end, period='hour', types=['mean'], units={'power': 'W'})
        except Exception as e:
            logger.warning(f"load_hourly: HA statistics not available ({e}) - using history fallback")
            return None
        rows = (result or {}).get(self.entity_id)
        if not rows:
            # Keine einzige Statistikzeile im Fenster: Sensor ohne Statistik (state_class) oder
            # durchgehend ausgefallen - die History entscheidet zwischen beidem
            logger.info(f"load_hourly: no statistics rows for {self.entity_id} - using history fallback")
            return None
        hours = {start + timedelta(hours=k): (None, None)
                 for k in range(int((end - start).total_seconds() // 3600))}
        for row in rows:
            hour = _parse_stat_start(row.get('start'))
            mean = row.get('mean')
            if hour in hours:
                hours[hour] = (float(mean) if mean is not None else None, None)
        return hours

    def _fetch_history(self, start, end):
        """{Stundenbeginn: (mean_w oder None, coverage)}; Blöcke ohne Antwort fehlen im Ergebnis."""
        return history_hourly_means(self.ha_client, self.entity_id, start, end)

    def _store(self, hours, source, now):
        updated = now.astimezone(timezone.utc).isoformat(timespec='seconds')
        tz = _zone(self.tz_name)
        rows = []
        for hour, (mean, coverage) in sorted(hours.items()):
            local = hour.astimezone(tz)
            rows.append((_utc_iso(hour), local.isoformat(), local.hour, mean, coverage,
                         source, self.entity_id, updated))
        with closing(_connect(self.db_path)) as conn, conn:
            self._ensure_schema(conn)
            conn.executemany("""
                INSERT INTO load_hourly
                    (hour_start_utc, hour_start_local, local_hour, mean_w, coverage, source, entity_id, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(hour_start_utc) DO UPDATE SET
                    mean_w = excluded.mean_w, coverage = excluded.coverage, source = excluded.source,
                    entity_id = excluded.entity_id, updated_at = excluded.updated_at
                WHERE excluded.mean_w IS NOT NULL OR load_hourly.mean_w IS NULL
            """, rows)
        return len(rows)

    def _purge_if_due(self, now):
        if self._last_purge is not None and time.monotonic() - self._last_purge < 24 * 3600:
            return
        cutoff = now - timedelta(days=self.retention_days)
        with closing(_connect(self.db_path)) as conn, conn:
            conn.execute("DELETE FROM load_hourly WHERE hour_start_utc < ?", (_utc_iso(cutoff),))
        self._last_purge = time.monotonic()


# ==============================================================================
# Planarchiv
# ==============================================================================

class PlanArchive:
    """Jede Planberechnung als JSON-Zeile in <directory>/YYYY-MM-DD.jsonl (14 Tage)."""

    PARAM_KEYS = ('battery_capacity', 'auto_safety_soc', 'auto_charge_below_soc', 'max_charge_power',
                  'pv_forecast_bias_correction', 'enable_smart_grid_charge', 'grid_arbitrage_min_spread_pct')

    def __init__(self, directory=PLAN_ARCHIVE_DIR, retention_days=PLAN_RETENTION_DAYS):
        self.directory = directory
        self.retention_days = retention_days
        self._last_purge_date = None

    def write(self, schedule, current_soc, config=None, now=None):
        """Eine Planberechnung archivieren (schedule None = kein Plan). Wirft nie eine Exception.

        Returns:
            True, wenn die Zeile geschrieben wurde
        """
        try:
            now = now or datetime.now().astimezone()
            record = {'ts': now.isoformat(timespec='seconds'), 'soc': current_soc,
                      'ok': schedule is not None}
            if config:
                record['params'] = {k: config.get(k) for k in self.PARAM_KEYS if k in config}
            if schedule:
                record.update({
                    'start_time': schedule.get('start_time'),
                    'prices': schedule.get('hourly_prices'),
                    'pv': schedule.get('hourly_pv'),
                    'consumption': schedule.get('hourly_consumption'),
                    'charging': schedule.get('hourly_charging'),
                    'soc_plan': schedule.get('hourly_soc'),
                    'windows': schedule.get('charging_windows'),
                    'min_soc_reached': schedule.get('min_soc_reached'),
                    'total_charging_kwh': schedule.get('total_charging_kwh'),
                })
            line = json.dumps(_round_floats(record), separators=(',', ':'), default=str)
            os.makedirs(self.directory, exist_ok=True)
            with open(os.path.join(self.directory, f"{now.date().isoformat()}.jsonl"), 'a',
                      encoding='utf-8') as f:
                f.write(line + '\n')
            if self._last_purge_date != now.date():
                self.purge(now.date())
                self._last_purge_date = now.date()
            return True
        except Exception as e:
            logger.warning(f"Plan archive: write failed ({e}) - plan not affected")
            return False

    def purge(self, today=None):
        """Tagesdateien älter als retention_days löschen (andere Dateien bleiben). Wirft nie."""
        try:
            today = today or datetime.now().astimezone().date()
            cutoff = today - timedelta(days=self.retention_days)
            for name in os.listdir(self.directory):
                match = _PLAN_FILE_RE.match(name)
                if not match:
                    continue
                try:
                    day = datetime.strptime(match.group(1), '%Y-%m-%d').date()
                except ValueError:
                    continue
                if day < cutoff:
                    os.remove(os.path.join(self.directory, name))
                    logger.info(f"Plan archive: removed {name}")
        except Exception as e:
            logger.warning(f"Plan archive: purge failed ({e})")
