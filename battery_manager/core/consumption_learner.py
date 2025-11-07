#!/usr/bin/env python3
"""
Consumption Learning System
Learns household consumption patterns over time
"""

import logging
import sqlite3
import csv
import io
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from pathlib import Path

logger = logging.getLogger(__name__)


class ConsumptionLearner:
    """Learns and predicts household consumption patterns"""

    def __init__(self, db_path: str, learning_days: int = 28,
                 default_fallback: float = 1.0):
        """
        Initialize consumption learner

        Args:
            db_path: Path to SQLite database
            learning_days: Number of days to keep in history (default 28 = 4 weeks)
            default_fallback: Default hourly consumption if no data available (kWh)
        """
        self.db_path = db_path
        self.learning_days = learning_days
        self.default_fallback = default_fallback
        self._init_database()
        logger.info(f"Consumption Learner initialized (learning period: {learning_days} days, "
                   f"fallback: {default_fallback} kWh/h)")

    def _init_database(self):
        """Initialize SQLite database with schema"""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS hourly_consumption (
                    timestamp TEXT PRIMARY KEY,
                    hour INTEGER NOT NULL,
                    consumption_kwh REAL NOT NULL,
                    is_manual BOOLEAN DEFAULT 0,
                    created_at TEXT NOT NULL
                )
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_hour
                ON hourly_consumption(hour)
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_timestamp
                ON hourly_consumption(timestamp DESC)
            """)

            conn.commit()
            logger.info("Database initialized successfully")

    def add_manual_profile(self, profile: Dict[str, float]):
        """
        Add manual load profile as baseline (initial 4 weeks)

        Args:
            profile: Dict with hour (0-23) as key and consumption in kW as value
                Example: {"0": 0.2, "1": 0.2, "7": 2.0, ...}
        """
        logger.info("Adding manual load profile as baseline...")

        with sqlite3.connect(self.db_path) as conn:
            # Generate 28 days of baseline data
            now = datetime.now()
            start_date = now - timedelta(days=self.learning_days)

            count = 0
            for day in range(self.learning_days):
                date = start_date + timedelta(days=day)

                for hour in range(24):
                    hour_str = str(hour)
                    if hour_str not in profile:
                        logger.warning(f"Hour {hour} missing in manual profile, using 0.2 kW")
                        consumption = 0.2
                    else:
                        consumption = float(profile[hour_str])

                    timestamp = date.replace(hour=hour, minute=0, second=0, microsecond=0)

                    conn.execute("""
                        INSERT OR REPLACE INTO hourly_consumption
                        (timestamp, hour, consumption_kwh, is_manual, created_at)
                        VALUES (?, ?, ?, 1, ?)
                    """, (
                        timestamp.isoformat(),
                        hour,
                        consumption,
                        datetime.now().isoformat()
                    ))
                    count += 1

            conn.commit()
            logger.info(f"Added {count} hours of manual baseline data")

    def import_detailed_history(self, daily_data: List[Dict]):
        """
        Import detailed historical data with individual daily profiles

        Args:
            daily_data: List of daily profiles, each containing:
                {
                    'date': 'YYYY-MM-DD' or datetime object,
                    'weekday': 'Montag'|'Dienstag'|...|'Sonntag' (optional),
                    'hours': [h0, h1, h2, ..., h23]  # 24 hourly consumption values in kWh
                }

        Example:
            [
                {
                    'date': '2024-10-07',
                    'weekday': 'Montag',
                    'hours': [0.2, 0.2, 0.15, ..., 0.3]  # 24 values
                },
                ...
            ]
        """
        logger.info(f"Importing detailed historical data for {len(daily_data)} days...")

        if len(daily_data) > self.learning_days:
            logger.warning(f"Provided {len(daily_data)} days but learning period is {self.learning_days} days. "
                          f"Only the most recent {self.learning_days} days will be kept.")

        imported_count = 0
        skipped_count = 0

        with sqlite3.connect(self.db_path) as conn:
            for day_entry in daily_data:
                try:
                    # Parse date
                    if isinstance(day_entry['date'], str):
                        date = datetime.fromisoformat(day_entry['date'])
                    else:
                        date = day_entry['date']

                    hours = day_entry['hours']

                    # Validate: must have exactly 24 values
                    if len(hours) != 24:
                        logger.error(f"Invalid data for {date.strftime('%Y-%m-%d')}: "
                                    f"Expected 24 hourly values, got {len(hours)}. Skipping.")
                        skipped_count += 1
                        continue

                    # Import each hour
                    for hour in range(24):
                        consumption = float(hours[hour])

                        # Validate value
                        if consumption < 0:
                            logger.warning(f"Negative value {consumption} kWh at {date.strftime('%Y-%m-%d')} hour {hour}, using 0")
                            consumption = 0
                        elif consumption > 50:
                            logger.warning(f"Unrealistic value {consumption} kWh at {date.strftime('%Y-%m-%d')} hour {hour}, capping at 50")
                            consumption = 50

                        timestamp = date.replace(hour=hour, minute=0, second=0, microsecond=0)

                        conn.execute("""
                            INSERT OR REPLACE INTO hourly_consumption
                            (timestamp, hour, consumption_kwh, is_manual, created_at)
                            VALUES (?, ?, ?, 1, ?)
                        """, (
                            timestamp.isoformat(),
                            hour,
                            consumption,
                            datetime.now().isoformat()
                        ))
                        imported_count += 1

                except Exception as e:
                    logger.error(f"Error importing day {day_entry.get('date', 'unknown')}: {e}")
                    skipped_count += 1
                    continue

            conn.commit()

        logger.info(f"Import complete: {imported_count} hourly records imported, {skipped_count} days skipped")

        # Clean up old data
        self._cleanup_old_data()

        return {
            'imported_hours': imported_count,
            'skipped_days': skipped_count,
            'success': skipped_count == 0
        }

    def import_from_csv(self, csv_content: str) -> Dict:
        """
        Import consumption data from CSV string

        CSV Format:
            datum,wochentag,h0,h1,h2,h3,...,h23
            2024-10-07,Montag,0.2,0.2,0.15,0.15,...,0.3
            2024-10-08,Dienstag,0.18,0.19,0.14,0.13,...,0.35

        Args:
            csv_content: CSV content as string

        Returns:
            Dict with import results
        """
        try:
            logger.info("Parsing CSV data...")

            # Parse CSV
            csv_file = io.StringIO(csv_content)
            reader = csv.DictReader(csv_file)

            daily_data = []

            for row_num, row in enumerate(reader, start=2):  # Start at 2 (header is 1)
                try:
                    # Extract date and weekday
                    date_str = row.get('datum', '').strip()
                    weekday = row.get('wochentag', '').strip()

                    if not date_str:
                        logger.warning(f"Row {row_num}: Missing date, skipping")
                        continue

                    # Parse date
                    try:
                        date = datetime.strptime(date_str, '%Y-%m-%d')
                    except ValueError:
                        try:
                            # Try alternative format
                            date = datetime.strptime(date_str, '%d.%m.%Y')
                        except ValueError:
                            logger.error(f"Row {row_num}: Invalid date format '{date_str}', expected YYYY-MM-DD or DD.MM.YYYY")
                            continue

                    # Extract hourly values (h0 to h23)
                    hours = []
                    for h in range(24):
                        col_name = f'h{h}'
                        if col_name not in row:
                            logger.error(f"Row {row_num}: Missing column '{col_name}'")
                            break

                        try:
                            value = row[col_name].strip()
                            # Replace comma with dot for German number format
                            value = value.replace(',', '.')
                            hours.append(float(value))
                        except ValueError:
                            logger.error(f"Row {row_num}: Invalid number in column '{col_name}': '{row[col_name]}'")
                            break

                    # Check if we have all 24 hours
                    if len(hours) != 24:
                        logger.error(f"Row {row_num}: Incomplete hourly data (got {len(hours)} values)")
                        continue

                    daily_data.append({
                        'date': date,
                        'weekday': weekday,
                        'hours': hours
                    })

                except Exception as e:
                    logger.error(f"Row {row_num}: Error parsing row: {e}")
                    continue

            if not daily_data:
                return {
                    'success': False,
                    'error': 'No valid data found in CSV',
                    'imported_hours': 0,
                    'imported_days': 0,
                    'skipped_days': 0
                }

            logger.info(f"Successfully parsed {len(daily_data)} days from CSV")

            # Import the parsed data
            result = self.import_detailed_history(daily_data)
            result['imported_days'] = len(daily_data)
            return result

        except Exception as e:
            logger.error(f"Error parsing CSV: {e}")
            return {
                'success': False,
                'error': str(e),
                'imported_hours': 0,
                'imported_days': 0,
                'skipped_days': 0
            }

    def import_calculated_consumption_energy_sensors(self, ha_client,
                                                       grid_from_energy_sensor: str,
                                                       grid_to_energy_sensor: str,
                                                       battery_charge_from_grid_sensor: str,
                                                       battery_charge_from_pv_sensor: str,
                                                       battery_discharge_sensor: str,
                                                       pv_dc_sensors: List[str] = None,
                                                       pv_energy_sensor: str = None,
                                                       days: int = 28) -> Dict:
        """
        Import calculated home consumption from Home Assistant using energy sensors (v1.2.0-beta.43)

        Uses cumulative energy sensors (kWh) instead of power sensors (W) for accurate calculation.
        This matches Home Assistant Energy Dashboard methodology.

        Formula: Home = (GridFrom - GridTo) + PV + (BatteryDischarge - BatteryChargeFromGrid - BatteryChargeFromPV)

        Args:
            ha_client: HomeAssistantClient instance
            grid_from_energy_sensor: Grid import energy sensor (kWh cumulative, e.g., 'sensor.ksem_total_active_energy_from_grid')
            grid_to_energy_sensor: Grid export energy sensor (kWh cumulative, e.g., 'sensor.ksem_total_active_energy_to_grid')
            battery_charge_from_grid_sensor: Battery charge from grid (kWh cumulative)
            battery_charge_from_pv_sensor: Battery charge from PV (kWh cumulative)
            battery_discharge_sensor: Battery discharge (kWh cumulative)
            pv_dc_sensors: List of PV DC power sensors in W (optional, fallback if pv_energy_sensor not available)
            pv_energy_sensor: PV energy sensor (kWh cumulative, e.g., 'sensor.solarproduktion') - PREFERRED over DC sensors
            days: Number of days to import (default 28)

        Returns:
            Dict with import results
        """
        try:
            logger.info(f"Starting consumption import using ENERGY sensors (v1.2.0-beta.43), last {days} days...")
            logger.info(f"Grid From Energy: {grid_from_energy_sensor}")
            logger.info(f"Grid To Energy: {grid_to_energy_sensor}")
            logger.info(f"Battery Charge From Grid: {battery_charge_from_grid_sensor}")
            logger.info(f"Battery Charge From PV: {battery_charge_from_pv_sensor}")
            logger.info(f"Battery Discharge: {battery_discharge_sensor}")

            # PV source selection (v1.2.0-beta.43)
            if pv_energy_sensor:
                logger.info(f"PV Energy Sensor: {pv_energy_sensor} (PREFERRED - using cumulative kWh sensor)")
            elif pv_dc_sensors:
                logger.info(f"PV DC Sensors: {pv_dc_sensors} (FALLBACK - integration from power sensors)")
            else:
                logger.warning("No PV sensors configured!")

            # Calculate time range
            end_time = datetime.now()
            start_time = end_time - timedelta(days=days)
            logger.info(f"Time range: {start_time.isoformat()} to {end_time.isoformat()}")

            # Get history data for all energy sensors
            logger.info("Fetching grid FROM energy sensor history...")
            grid_from_history = ha_client.get_history(grid_from_energy_sensor, start_time, end_time)

            logger.info("Fetching grid TO energy sensor history...")
            grid_to_history = ha_client.get_history(grid_to_energy_sensor, start_time, end_time)

            logger.info("Fetching battery charge from grid sensor history...")
            batt_charge_grid_history = ha_client.get_history(battery_charge_from_grid_sensor, start_time, end_time)

            logger.info("Fetching battery charge from PV sensor history...")
            batt_charge_pv_history = ha_client.get_history(battery_charge_from_pv_sensor, start_time, end_time)

            logger.info("Fetching battery discharge sensor history...")
            batt_discharge_history = ha_client.get_history(battery_discharge_sensor, start_time, end_time)

            # Get PV history - prefer energy sensor over DC sensors
            pv_energy_history = None
            pv_dc_histories = []

            if pv_energy_sensor:
                logger.info(f"Fetching PV energy sensor history: {pv_energy_sensor}...")
                pv_energy_history = ha_client.get_history(pv_energy_sensor, start_time, end_time)
            elif pv_dc_sensors:
                # Fallback: Get PV DC power sensor histories
                for sensor in pv_dc_sensors:
                    if sensor:
                        logger.info(f"Fetching PV DC sensor history: {sensor}...")
                        history = ha_client.get_history(sensor, start_time, end_time)
                        if history:
                            pv_dc_histories.append((sensor, history))

            # Validate required sensors
            if not grid_from_history or not grid_to_history:
                error_msg = []
                if not grid_from_history:
                    error_msg.append(f"No history for grid FROM energy sensor {grid_from_energy_sensor}")
                if not grid_to_history:
                    error_msg.append(f"No history for grid TO energy sensor {grid_to_energy_sensor}")
                error_str = ", ".join(error_msg)
                logger.error(error_str)
                return {
                    'success': False,
                    'error': error_str,
                    'imported_hours': 0,
                    'imported_days': 0,
                    'skipped_days': 0,
                    'history_entries': 0
                }

            logger.info(f"Received: GridFrom={len(grid_from_history)}, GridTo={len(grid_to_history)}, "
                       f"BattChgGrid={len(batt_charge_grid_history) if batt_charge_grid_history else 0}, "
                       f"BattChgPV={len(batt_charge_pv_history) if batt_charge_pv_history else 0}, "
                       f"BattDisch={len(batt_discharge_history) if batt_discharge_history else 0}, "
                       f"PV_Energy={len(pv_energy_history) if pv_energy_history else 0}, "
                       f"PV_DC_sensors={len(pv_dc_histories)}")

            # Process energy sensors to calculate hourly deltas
            # Energy sensors are cumulative (kWh), so we need to calculate the difference between consecutive hours

            # Helper function to process cumulative energy sensor
            def process_cumulative_energy_sensor(history, sensor_name):
                """Process cumulative kWh sensor and return hourly deltas"""
                hourly_deltas = {}  # Key: (date, hour), Value: energy delta in kWh

                if not history:
                    return hourly_deltas

                # Sort by timestamp
                sorted_history = sorted(history, key=lambda x: x.get('last_changed') or x.get('last_updated', ''))

                # Group readings by hour and take first/last value
                hourly_values = {}  # Key: (date, hour), Value: {'first': value, 'last': value}

                for entry in sorted_history:
                    try:
                        timestamp_str = entry.get('last_changed') or entry.get('last_updated')
                        if not timestamp_str:
                            continue

                        timestamp = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
                        local_timestamp = timestamp.astimezone()

                        state = entry.get('state')
                        if state in ['unknown', 'unavailable', None]:
                            continue

                        try:
                            value = float(state)
                        except (ValueError, TypeError):
                            continue

                        date_key = local_timestamp.date()
                        hour_key = local_timestamp.hour
                        key = (date_key, hour_key)

                        if key not in hourly_values:
                            hourly_values[key] = {'first': value, 'last': value, 'first_ts': local_timestamp, 'last_ts': local_timestamp}
                        else:
                            # Update last value (sorted, so this is chronologically later)
                            hourly_values[key]['last'] = value
                            hourly_values[key]['last_ts'] = local_timestamp

                    except Exception as e:
                        logger.debug(f"Skipping {sensor_name} entry: {e}")
                        continue

                # Calculate deltas from first/last values per hour
                for key, values in hourly_values.items():
                    first_val = values['first']
                    last_val = values['last']
                    delta = last_val - first_val

                    # Handle sensor resets (negative delta)
                    if delta < 0:
                        logger.warning(f"{sensor_name}: Sensor reset detected in hour {key}, skipping")
                        continue

                    # Skip zero deltas (no change in this hour)
                    if delta > 0.001:  # Small threshold to avoid floating point noise
                        hourly_deltas[key] = delta

                logger.info(f"{sensor_name}: Processed {len(hourly_deltas)} hourly deltas from {len(hourly_values)} hour buckets")
                return hourly_deltas

            # Process all energy sensors using the helper function
            logger.info("Processing energy sensor deltas...")
            grid_from_deltas = process_cumulative_energy_sensor(grid_from_history, "GridFromEnergy")
            grid_to_deltas = process_cumulative_energy_sensor(grid_to_history, "GridToEnergy")
            batt_charge_grid_deltas = process_cumulative_energy_sensor(batt_charge_grid_history, "BattChgGrid")
            batt_charge_pv_deltas = process_cumulative_energy_sensor(batt_charge_pv_history, "BattChgPV")
            batt_discharge_deltas = process_cumulative_energy_sensor(batt_discharge_history, "BattDischarge")

            # Process PV: Prefer energy sensor, fallback to DC power sensors
            pv_hourly_energy = {}  # Key: (date, hour), Value: PV energy in kWh

            if pv_energy_history:
                # Use PV energy sensor (PREFERRED - more accurate)
                logger.info("Processing PV energy sensor (cumulative kWh)...")
                pv_hourly_energy = process_cumulative_energy_sensor(pv_energy_history, "PVEnergy")
            elif pv_dc_histories:
                # Fallback: Process PV DC power sensors to calculate hourly energy
                # These are instantaneous power sensors (W), so we need to integrate them
                logger.info("Processing PV DC power sensors (fallback - less accurate)...")

                # Process each PV DC sensor
                for sensor_name, pv_history in pv_dc_histories:
                # Group readings by hour and calculate average power, then integrate to energy
                hourly_power_readings = {}  # Key: (date, hour), Value: list of power values in W

                for entry in pv_history:
                    try:
                        timestamp_str = entry.get('last_changed') or entry.get('last_updated')
                        if not timestamp_str:
                            continue

                        timestamp = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
                        local_timestamp = timestamp.astimezone()

                        state = entry.get('state')
                        if state in ['unknown', 'unavailable', None]:
                            continue

                        try:
                            value = float(state)  # Power in W
                        except (ValueError, TypeError):
                            continue

                        # PV should not be negative
                        if value < 0:
                            value = 0

                        # Skip unrealistically high values
                        if value > 15000:  # > 15 kW per string
                            continue

                        date_key = local_timestamp.date()
                        hour_key = local_timestamp.hour
                        key = (date_key, hour_key)

                        if key not in hourly_power_readings:
                            hourly_power_readings[key] = []
                        hourly_power_readings[key].append(value)

                    except Exception as e:
                        logger.debug(f"Skipping {sensor_name} entry: {e}")
                        continue

                # Calculate average power per hour and convert to energy (kWh)
                # Average power (kW) * 1 hour = energy (kWh)
                for key, power_values in hourly_power_readings.items():
                    avg_power_w = sum(power_values) / len(power_values)
                    avg_power_kw = avg_power_w / 1000.0
                    energy_kwh = avg_power_kw  # For 1 hour: kW * 1h = kWh

                    if key not in pv_hourly_energy:
                        pv_hourly_energy[key] = 0
                    pv_hourly_energy[key] += energy_kwh

                    logger.info(f"{sensor_name}: Calculated {len(hourly_power_readings)} hourly energy values")

                logger.info(f"Total PV hourly energy calculated for {len(pv_hourly_energy)} hours from DC sensors")
            else:
                logger.warning("No PV sensors configured - PV will be 0")

            # Calculate home consumption using energy sensors
            # Formula: Home = (GridFrom - GridTo) + PV + (BatteryDischarge - BatteryChargeFromGrid - BatteryChargeFromPV)
            consumption_hourly_data = {}  # Key: (date, hour), Value: consumption in kWh

            # Get all unique date/hour combinations where we have grid data
            all_keys = set(grid_from_deltas.keys()) | set(grid_to_deltas.keys()) | set(pv_hourly_energy.keys()) | \
                       set(batt_charge_grid_deltas.keys()) | set(batt_charge_pv_deltas.keys()) | set(batt_discharge_deltas.keys())

            logger.info(f"Found {len(all_keys)} hours with sensor data")

            for key in all_keys:
                # Get energy values for this hour (all in kWh)
                grid_from_kwh = grid_from_deltas.get(key, 0.0)
                grid_to_kwh = grid_to_deltas.get(key, 0.0)
                pv_kwh = pv_hourly_energy.get(key, 0.0)
                batt_charge_grid_kwh = batt_charge_grid_deltas.get(key, 0.0)
                batt_charge_pv_kwh = batt_charge_pv_deltas.get(key, 0.0)
                batt_discharge_kwh = batt_discharge_deltas.get(key, 0.0)

                # Calculate net grid energy
                grid_net_kwh = grid_from_kwh - grid_to_kwh

                # Calculate net battery energy (positive = net discharge, negative = net charge)
                batt_net_kwh = batt_discharge_kwh - batt_charge_grid_kwh - batt_charge_pv_kwh

                # Calculate home consumption
                home_kwh = grid_net_kwh + pv_kwh + batt_net_kwh

                # Validate result
                if home_kwh < 0:
                    logger.warning(f"Negative home consumption {home_kwh:.3f} kWh at {key} "
                                  f"(GridNet={grid_net_kwh:.3f}, PV={pv_kwh:.3f}, BattNet={batt_net_kwh:.3f}) - skipping")
                    continue

                if home_kwh > 50:  # Unrealistically high hourly consumption
                    logger.warning(f"Unrealistically high consumption {home_kwh:.3f} kWh at {key} - skipping")
                    continue

                consumption_hourly_data[key] = home_kwh

                # Debug log for validation
                if home_kwh > 5.0:
                    date_key, hour_key = key
                    logger.info(f"🔍 High consumption at {key}: Home={home_kwh:.2f}kWh "
                              f"(GridFrom={grid_from_kwh:.2f}, GridTo={grid_to_kwh:.2f}, PV={pv_kwh:.2f}, "
                              f"BattDisch={batt_discharge_kwh:.2f}, BattChgGrid={batt_charge_grid_kwh:.2f}, BattChgPV={batt_charge_pv_kwh:.2f})")

            logger.info(f"Calculated {len(consumption_hourly_data)} valid consumption values")

            # DEBUG: Log detailed calculation for today 0-18h (v1.2.0-beta.41)
            today = datetime.now().date()
            logger.info("=" * 80)
            logger.info(f"🔍 DETAILED CALCULATION DEBUG FOR TODAY {today} (0-18h)")
            logger.info("=" * 80)
            for hour in range(19):  # 0-18 Uhr
                key = (today, hour)
                if key in consumption_hourly_data:
                    # Get all components
                    grid_from = grid_from_deltas.get(key, 0.0)
                    grid_to = grid_to_deltas.get(key, 0.0)
                    pv = pv_hourly_energy.get(key, 0.0)
                    batt_chg_grid = batt_charge_grid_deltas.get(key, 0.0)
                    batt_chg_pv = batt_charge_pv_deltas.get(key, 0.0)
                    batt_disch = batt_discharge_deltas.get(key, 0.0)

                    grid_net = grid_from - grid_to
                    batt_net = batt_disch - batt_chg_grid - batt_chg_pv
                    home = consumption_hourly_data[key]

                    logger.info(f"Hour {hour:02d}:00")
                    logger.info(f"  Grid:    FROM={grid_from:7.3f} kWh  TO={grid_to:7.3f} kWh  → NET={grid_net:+7.3f} kWh")
                    logger.info(f"  Battery: DISCH={batt_disch:7.3f} kWh  CHG_GRID={batt_chg_grid:7.3f} kWh  CHG_PV={batt_chg_pv:7.3f} kWh  → NET={batt_net:+7.3f} kWh")
                    logger.info(f"  PV:      {pv:7.3f} kWh")
                    logger.info(f"  ➜ HOME = GridNet({grid_net:+.3f}) + PV({pv:.3f}) + BattNet({batt_net:+.3f}) = {home:.3f} kWh")
                    logger.info("")
                else:
                    # Check if we have any data for this hour
                    has_any_data = (key in grid_from_deltas or key in grid_to_deltas or
                                   key in pv_hourly_energy or key in batt_charge_grid_deltas or
                                   key in batt_charge_pv_deltas or key in batt_discharge_deltas)
                    if has_any_data:
                        logger.info(f"Hour {hour:02d}:00 - SKIPPED (incomplete or invalid data)")
                        logger.info(f"  Grid FROM: {'✓' if key in grid_from_deltas else '✗'} {grid_from_deltas.get(key, 0.0):.3f}")
                        logger.info(f"  Grid TO:   {'✓' if key in grid_to_deltas else '✗'} {grid_to_deltas.get(key, 0.0):.3f}")
                        logger.info(f"  PV:        {'✓' if key in pv_hourly_energy else '✗'} {pv_hourly_energy.get(key, 0.0):.3f}")
                        logger.info(f"  Batt Disch:{'✓' if key in batt_discharge_deltas else '✗'} {batt_discharge_deltas.get(key, 0.0):.3f}")
                        logger.info("")
                    else:
                        logger.info(f"Hour {hour:02d}:00 - NO DATA")
            logger.info("=" * 80)

            # Group by day (same as original method)
            daily_data_dict = {}  # Key: date, Value: dict with hours

            for (date_key, hour_key), consumption_kwh in consumption_hourly_data.items():
                if date_key not in daily_data_dict:
                    daily_data_dict[date_key] = {}
                daily_data_dict[date_key][hour_key] = consumption_kwh

            logger.info(f"Found data for {len(daily_data_dict)} unique days")

            # Convert to format for import_detailed_history
            daily_data = []
            skipped_days = 0
            weekdays_de = ['Montag', 'Dienstag', 'Mittwoch', 'Donnerstag', 'Freitag', 'Samstag', 'Sonntag']

            for date_key in sorted(daily_data_dict.keys()):
                hours_dict = daily_data_dict[date_key]

                # Require at least 3 hours of data per day
                if len(hours_dict) < 3:
                    logger.warning(f"Skipping {date_key}: only {len(hours_dict)} hours of data (need >= 3)")
                    skipped_days += 1
                    continue

                # Build 24-hour array (fill missing hours with average)
                hours = []
                for h in range(24):
                    if h in hours_dict:
                        hours.append(hours_dict[h])
                    else:
                        # Use average of available data for missing hours
                        if hours_dict:
                            hours.append(sum(hours_dict.values()) / len(hours_dict))
                        else:
                            hours.append(0)

                # Get weekday
                weekday_idx = date_key.weekday()
                weekday = weekdays_de[weekday_idx]

                daily_data.append({
                    'date': date_key.isoformat(),
                    'weekday': weekday,
                    'hours': hours
                })

            if not daily_data:
                logger.error(f"No complete days found. Checked {len(daily_data_dict)} days")
                return {
                    'success': False,
                    'error': f'No complete days found in history data. Check if sensors are logging correctly.',
                    'imported_hours': 0,
                    'imported_days': 0,
                    'skipped_days': len(daily_data_dict),
                    'history_entries': len(grid_from_history) + len(grid_to_history) + len(pv_history)
                }

            logger.info(f"Prepared {len(daily_data)} days for import (skipped {skipped_days} incomplete days)")

            # Import the data
            result = self.import_detailed_history(daily_data)
            result['history_entries'] = (len(grid_from_history) + len(grid_to_history) +
                                        len(batt_charge_grid_history or []) + len(batt_charge_pv_history or []) +
                                        len(batt_discharge_history or []) +
                                        sum(len(h) for _, h in pv_dc_histories))
            result['imported_days'] = len(daily_data)
            return result

        except Exception as e:
            logger.error(f"Error importing calculated consumption from HA (energy sensors): {e}", exc_info=True)
            return {
                'success': False,
                'error': str(e),
                'imported_hours': 0,
                'imported_days': 0,
                'skipped_days': 0
            }

    def import_calculated_consumption_dual_grid(self, ha_client,
                                                 grid_from_sensor: str,
                                                 grid_to_sensor: str,
                                                 pv_sensor: str,
                                                 battery_sensor: str = None,
                                                 days: int = 28) -> Dict:
        """
        DEPRECATED METHOD - REMOVED (v1.2.0-beta.40)

        This method has been removed. Use import_calculated_consumption_energy_sensors() instead.
        The new method uses cumulative energy sensors (kWh) for accurate calculation.

        Required sensors:
        - grid_from_energy_sensor: Grid import energy (kWh cumulative)
        - grid_to_energy_sensor: Grid export energy (kWh cumulative)
        - battery_charge_from_grid_sensor: Battery charge from grid (kWh cumulative)
        - battery_charge_from_pv_sensor: Battery charge from PV (kWh cumulative)
        - battery_discharge_sensor: Battery discharge (kWh cumulative)
        - pv_dc_sensors: List of PV DC power sensors in W
        """
        logger.error("import_calculated_consumption_dual_grid() is DEPRECATED and has been removed")
        return {
            'success': False,
            'error': 'This method is deprecated. Use import_calculated_consumption_energy_sensors() instead.',
            'imported_hours': 0,
            'imported_days': 0,
            'skipped_days': 0
        }

    def import_calculated_consumption_from_ha(self, ha_client, grid_sensor: str, pv_sensor: str, battery_sensor: str = None, days: int = 28) -> Dict:
        """
        DEPRECATED METHOD - REMOVED

        This method has been removed. Only import_calculated_consumption_dual_grid() is supported now.
        Use separate grid_from_sensor and grid_to_sensor instead of a single signed grid sensor.

        Required sensors:
        - grid_from_sensor: Netzbezug (always positive)
        - grid_to_sensor: Netzeinspeisung (always positive)
        - pv_total_sensor: PV Leistung (always positive)
        - battery_power_sensor: Batterieleistung (positive=discharge, negative=charge)

        Formula: Hausverbrauch = Netzbezug - Netzeinspeisung + PV + Batterie
        """
        logger.error("import_calculated_consumption_from_ha() is DEPRECATED and has been removed")
        return {
            'success': False,
            'error': 'This method is deprecated. Use import_calculated_consumption_dual_grid() instead.',
            'imported_hours': 0,
            'imported_days': 0,
            'skipped_days': 0
        }

    def import_from_home_assistant(self, ha_client, entity_id: str, days: int = 28) -> Dict:
        """
        DEPRECATED METHOD - REMOVED

        This method has been removed. Only import_calculated_consumption_dual_grid() is supported now.
        Single-sensor import does not provide accurate home consumption calculation.
        """
        logger.error("import_from_home_assistant() is DEPRECATED and has been removed")
        return {
            'success': False,
            'error': 'This method is deprecated. Use import_calculated_consumption_dual_grid() instead.',
            'imported_hours': 0,
            'imported_days': 0,
            'skipped_days': 0
        }


    def record_consumption(self, timestamp: datetime, consumption_kwh: float):
        """
        Record actual consumption for learning

        Args:
            timestamp: Timestamp of consumption
            consumption_kwh: Consumption in kWh for that hour
        """
        # Validate: negative values indicate sensor/metering errors
        if consumption_kwh < 0:
            logger.warning(f"Negative consumption value detected: {consumption_kwh} kWh at {timestamp.strftime('%Y-%m-%d %H:%M')} - "
                          f"Skipping (likely Kostal Smart Meter bug)")
            return

        # Validate: unrealistic high values (> 100 kWh/h suggests wrong sensor type)
        if consumption_kwh > 100:
            logger.error(f"⚠️ CONFIGURATION ERROR: Sensor value {consumption_kwh} kWh is too high! "
                        f"You likely configured a cumulative TOTAL energy sensor (Gesamtverbrauch) "
                        f"instead of a POWER or hourly ENERGY sensor. "
                        f"Please use a sensor that measures instantaneous power (W) or energy per time period (kWh/h), "
                        f"NOT a cumulative total counter.")
            return

        # Validate: high but possible values (50-100 kWh/h)
        if consumption_kwh > 50:
            logger.warning(f"Very high consumption value: {consumption_kwh} kWh at {timestamp.strftime('%Y-%m-%d %H:%M')} - "
                          f"Recording but please verify your sensor configuration is correct")
            # Continue recording despite warning

        hour = timestamp.hour

        # Round timestamp to full hour to match imported data format
        # This ensures automatic learning overwrites averaged values from imports
        rounded_timestamp = timestamp.replace(minute=0, second=0, microsecond=0)

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO hourly_consumption
                (timestamp, hour, consumption_kwh, is_manual, created_at)
                VALUES (?, ?, ?, 0, ?)
            """, (
                rounded_timestamp.isoformat(),
                hour,
                consumption_kwh,
                datetime.now().isoformat()
            ))
            conn.commit()

        logger.debug(f"Recorded consumption: {consumption_kwh:.2f} kWh at hour {hour}")

        # Clean up old data
        self._cleanup_old_data()

    def _cleanup_old_data(self):
        """Remove data older than learning period"""
        cutoff = datetime.now() - timedelta(days=self.learning_days)

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                DELETE FROM hourly_consumption
                WHERE timestamp < ?
            """, (cutoff.isoformat(),))
            conn.commit()

    def cleanup_duplicates(self):
        """
        Remove duplicate entries for the same date+hour combination.
        Keeps the best entry: prefer learned (is_manual=0) over imported (is_manual=1),
        and latest created_at as tiebreaker.
        """
        with sqlite3.connect(self.db_path) as conn:
            # Find and delete duplicates, keeping only the best entry per date+hour
            cursor = conn.execute("""
                DELETE FROM hourly_consumption
                WHERE rowid NOT IN (
                    SELECT rowid
                    FROM (
                        SELECT rowid,
                               ROW_NUMBER() OVER (
                                   PARTITION BY DATE(timestamp), hour
                                   ORDER BY is_manual ASC, created_at DESC, timestamp DESC
                               ) as rn
                        FROM hourly_consumption
                    )
                    WHERE rn = 1
                )
            """)
            deleted = cursor.rowcount
            conn.commit()

            if deleted > 0:
                logger.info(f"Cleaned up {deleted} duplicate entries")

            return deleted

    def clear_all_manual_data(self):
        """Clear all manually imported data (keeps automatically learned data)"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("DELETE FROM hourly_consumption WHERE is_manual = 1")
            deleted = cursor.rowcount
            conn.commit()
            logger.info(f"Cleared {deleted} manually imported records")
            return deleted

    def clear_all_data(self):
        """Clear ALL consumption data (manual AND learned)"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("DELETE FROM hourly_consumption")
            deleted = cursor.rowcount
            conn.commit()
            logger.info(f"Cleared ALL {deleted} consumption records")
            return deleted

    def get_average_consumption(self, hour: int, target_date=None) -> float:
        """
        Get average consumption for a specific hour

        Args:
            hour: Hour of day (0-23)
            target_date: Optional date/datetime to get consumption for.
                        If provided, only uses data from same weekday
                        If None, uses all available data

        Returns:
            Average consumption in kWh for that hour
        """
        from datetime import datetime, date as date_type

        # Determine weekday filter
        weekday_filter = None
        if target_date is not None:
            if isinstance(target_date, datetime):
                target_date = target_date.date()
            weekday_filter = target_date.strftime('%w')

        with sqlite3.connect(self.db_path) as conn:
            # Handle duplicates: only use best entry per date+hour
            if weekday_filter is not None:
                cursor = conn.execute("""
                    SELECT AVG(consumption_kwh) as avg_consumption
                    FROM (
                        SELECT DATE(timestamp) as date, hour, consumption_kwh, is_manual, created_at,
                               ROW_NUMBER() OVER (
                                   PARTITION BY DATE(timestamp), hour
                                   ORDER BY is_manual ASC, created_at DESC
                               ) as rn
                        FROM hourly_consumption
                        WHERE hour = ? AND strftime('%w', timestamp) = ?
                    )
                    WHERE rn = 1
                """, (hour, weekday_filter))
            else:
                cursor = conn.execute("""
                    SELECT AVG(consumption_kwh) as avg_consumption
                    FROM (
                        SELECT DATE(timestamp) as date, hour, consumption_kwh, is_manual, created_at,
                               ROW_NUMBER() OVER (
                                   PARTITION BY DATE(timestamp), hour
                                   ORDER BY is_manual ASC, created_at DESC
                               ) as rn
                        FROM hourly_consumption
                        WHERE hour = ?
                    )
                    WHERE rn = 1
                """, (hour,))

            result = cursor.fetchone()
            if result and result[0]:
                avg_consumption = float(result[0])
                # DEBUG: Log consumption values to diagnose high forecasts
                if avg_consumption > 5.0:
                    logger.warning(f"⚠️ High consumption forecast for hour {hour}: {avg_consumption:.2f} kWh "
                                  f"(weekday_filter={weekday_filter})")
                    # Show which rows contributed to this average
                    if weekday_filter is not None:
                        debug_cursor = conn.execute("""
                            SELECT timestamp, consumption_kwh, is_manual
                            FROM hourly_consumption
                            WHERE hour = ? AND strftime('%w', timestamp) = ?
                            ORDER BY timestamp DESC
                            LIMIT 5
                        """, (hour, weekday_filter))
                    else:
                        debug_cursor = conn.execute("""
                            SELECT timestamp, consumption_kwh, is_manual
                            FROM hourly_consumption
                            WHERE hour = ?
                            ORDER BY timestamp DESC
                            LIMIT 5
                        """, (hour,))
                    rows = debug_cursor.fetchall()
                    logger.warning(f"   Sample data (latest 5): {rows}")
                return avg_consumption

            logger.warning(f"No data for hour {hour}, using default {self.default_fallback} kWh")
            return self.default_fallback

    def get_hourly_profile(self, target_date=None) -> Dict[int, float]:
        """
        Get complete 24-hour average consumption profile

        Args:
            target_date: Optional date/datetime to get profile for.
                        If provided, only uses data from same weekday (e.g., all Mondays)
                        If None, uses all available data (old behavior)

        Returns:
            Dict with hour (0-23) as key and average consumption in kWh as value
        """
        from datetime import datetime, date as date_type

        profile = {}

        # Determine if we should filter by weekday
        weekday_filter = None
        if target_date is not None:
            if isinstance(target_date, datetime):
                target_date = target_date.date()
            # SQLite's strftime('%w', date) returns: 0=Sunday, 1=Monday, ..., 6=Saturday
            # Python's strftime('%w') matches this
            weekday_filter = target_date.strftime('%w')
            logger.debug(f"Filtering consumption profile for weekday {weekday_filter} ({target_date.strftime('%A')})")

        with sqlite3.connect(self.db_path) as conn:
            # For each hour, calculate average consumption
            # Handle duplicates by selecting best entry per date+hour:
            # - Prefer learned (is_manual=0) over imported (is_manual=1)
            # - Use latest created_at as tiebreaker

            if weekday_filter is not None:
                # Filter by weekday
                cursor = conn.execute("""
                    SELECT hour, AVG(consumption_kwh) as avg_consumption, COUNT(*) as sample_count
                    FROM (
                        SELECT DATE(timestamp) as date, hour, consumption_kwh, is_manual, created_at,
                               ROW_NUMBER() OVER (
                                   PARTITION BY DATE(timestamp), hour
                                   ORDER BY is_manual ASC, created_at DESC
                               ) as rn
                        FROM hourly_consumption
                        WHERE strftime('%w', timestamp) = ?
                    )
                    WHERE rn = 1
                    GROUP BY hour
                    ORDER BY hour
                """, (weekday_filter,))
            else:
                # Use all data
                cursor = conn.execute("""
                    SELECT hour, AVG(consumption_kwh) as avg_consumption, COUNT(*) as sample_count
                    FROM (
                        SELECT DATE(timestamp) as date, hour, consumption_kwh, is_manual, created_at,
                               ROW_NUMBER() OVER (
                                   PARTITION BY DATE(timestamp), hour
                                   ORDER BY is_manual ASC, created_at DESC
                               ) as rn
                        FROM hourly_consumption
                    )
                    WHERE rn = 1
                    GROUP BY hour
                    ORDER BY hour
                """)

            for row in cursor:
                hour = row[0]
                avg = row[1]
                count = row[2]
                profile[hour] = float(avg)
                if weekday_filter is not None:
                    logger.debug(f"Hour {hour:02d}:00 - avg: {avg:.2f} kWh (from {count} samples)")

        # Fill missing hours with fallback or average of available data
        if profile:
            avg_consumption = sum(profile.values()) / len(profile)
            for hour in range(24):
                if hour not in profile:
                    profile[hour] = avg_consumption
                    if weekday_filter is not None:
                        logger.debug(f"Hour {hour:02d}:00 - using fallback: {avg_consumption:.2f} kWh")
        else:
            # No data at all - use default fallback
            for hour in range(24):
                profile[hour] = self.default_fallback

        return profile

    def predict_consumption_until(self, target_hour: int, start_datetime=None) -> float:
        """
        Predict total consumption from now (or start_datetime) until target hour

        Args:
            target_hour: Target hour (0-23)
            start_datetime: Optional start datetime. If None, uses now()

        Returns:
            Predicted total consumption in kWh
        """
        from datetime import datetime, timedelta

        if start_datetime is None:
            start_datetime = datetime.now().astimezone()

        current_hour = start_datetime.hour
        current_minute = start_datetime.minute
        current_date = start_datetime.date()

        total = 0.0

        # Partial current hour (remaining minutes)
        remaining_fraction = (60 - current_minute) / 60
        total += self.get_average_consumption(current_hour, target_date=current_date) * remaining_fraction

        # Full hours until target
        # Track current position with datetime to handle day transitions
        position = start_datetime + timedelta(hours=1)
        position = position.replace(minute=0, second=0, microsecond=0)

        while position.hour != target_hour:
            hour = position.hour
            date = position.date()
            total += self.get_average_consumption(hour, target_date=date)
            position += timedelta(hours=1)

        return total

    def get_statistics(self) -> Dict:
        """Get statistics about learned data"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                SELECT
                    COUNT(*) as total_records,
                    SUM(CASE WHEN is_manual = 1 THEN 1 ELSE 0 END) as manual_records,
                    SUM(CASE WHEN is_manual = 0 THEN 1 ELSE 0 END) as learned_records,
                    MIN(timestamp) as oldest_record,
                    MAX(timestamp) as newest_record
                FROM hourly_consumption
            """)

            row = cursor.fetchone()

            if row:
                return {
                    'total_records': row[0],
                    'manual_records': row[1],
                    'learned_records': row[2],
                    'oldest_record': row[3],
                    'newest_record': row[4],
                    'learning_progress': round((row[2] / row[0] * 100) if row[0] > 0 else 0, 1)
                }

        return {
            'total_records': 0,
            'manual_records': 0,
            'learned_records': 0,
            'oldest_record': None,
            'newest_record': None,
            'learning_progress': 0.0
        }

    def get_today_consumption(self, date=None) -> Dict[int, float]:
        """
        Get actual recorded consumption values for a specific date (default: today)

        Args:
            date: Date to get consumption for (datetime.date object), defaults to today

        Returns:
            Dict with hour (0-23) as key and actual consumption in kWh as value
            Only includes hours that have been recorded
        """
        from datetime import date as date_type, datetime

        if date is None:
            date = datetime.now().date()
        elif isinstance(date, datetime):
            date = date.date()

        date_str = date.isoformat()

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                SELECT hour, consumption_kwh
                FROM hourly_consumption
                WHERE DATE(timestamp) = ?
                ORDER BY created_at DESC
            """, (date_str,))

            # Build dict with most recent value per hour
            hourly_consumption = {}
            for row in cursor.fetchall():
                hour = row[0]
                consumption = row[1]
                # Only store if we haven't seen this hour yet (ORDER BY DESC means first is newest)
                if hour not in hourly_consumption:
                    hourly_consumption[hour] = consumption

            return hourly_consumption

