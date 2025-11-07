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

    def import_calculated_consumption_dual_grid(self, ha_client,
                                                 grid_from_sensor: str,
                                                 grid_to_sensor: str,
                                                 pv_sensor: str,
                                                 battery_sensor: str = None,
                                                 days: int = 28) -> Dict:
        """
        Import calculated home consumption from Home Assistant using dual grid sensors (v1.2.0-beta.11)

        Calculates actual home consumption using formula: Home = PV + (GridFrom - GridTo) + Battery
        - GridFrom: Import from grid (always positive or 0)
        - GridTo: Export to grid (always positive or 0)
        - PV: Total PV production (always positive or 0)
        - Battery: Battery power (positive = discharge, negative = charge)

        Args:
            ha_client: HomeAssistantClient instance
            grid_from_sensor: Grid import sensor (e.g., 'sensor.ksem_active_power_from_grid')
            grid_to_sensor: Grid export sensor (e.g., 'sensor.ksem_active_power_to_grid')
            pv_sensor: PV total power sensor (e.g., 'sensor.ksem_sum_pv_power_inverter_dc')
            battery_sensor: Battery power sensor (e.g., 'sensor.ksem_battery_power'), optional
            days: Number of days to import (default 28)

        Returns:
            Dict with import results
        """
        try:
            logger.info(f"Starting calculated consumption import from HA (dual grid sensors), last {days} days...")
            logger.info(f"GridFrom: {grid_from_sensor}, GridTo: {grid_to_sensor}, PV: {pv_sensor}")
            if battery_sensor:
                logger.info(f"Battery: {battery_sensor}")

            # Calculate time range
            end_time = datetime.now()
            start_time = end_time - timedelta(days=days)
            logger.info(f"Time range: {start_time.isoformat()} to {end_time.isoformat()}")

            # Get history data for all sensors
            logger.info("Fetching grid FROM sensor history...")
            grid_from_history = ha_client.get_history(grid_from_sensor, start_time, end_time)

            logger.info("Fetching grid TO sensor history...")
            grid_to_history = ha_client.get_history(grid_to_sensor, start_time, end_time)

            logger.info("Fetching PV sensor history...")
            pv_history = ha_client.get_history(pv_sensor, start_time, end_time)

            # Get battery history if sensor is provided
            battery_history = []
            if battery_sensor:
                logger.info("Fetching battery sensor history...")
                battery_history = ha_client.get_history(battery_sensor, start_time, end_time)

            if not grid_from_history or not grid_to_history or not pv_history:
                error_msg = []
                if not grid_from_history:
                    error_msg.append(f"No history for grid FROM sensor {grid_from_sensor}")
                if not grid_to_history:
                    error_msg.append(f"No history for grid TO sensor {grid_to_sensor}")
                if not pv_history:
                    error_msg.append(f"No history for PV sensor {pv_sensor}")
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

            logger.info(f"Received {len(grid_from_history)} FROM entries, {len(grid_to_history)} TO entries, {len(pv_history)} PV entries" + (f", {len(battery_history)} battery entries" if battery_history else ""))

            # Get sensor units to determine correct conversion (v1.2.0-beta.13)
            logger.info("Detecting sensor units...")
            grid_from_unit = None
            grid_to_unit = None
            pv_unit = None
            battery_unit = None

            grid_from_info = ha_client.get_state_with_attributes(grid_from_sensor)
            if grid_from_info:
                grid_from_unit = grid_from_info.get('attributes', {}).get('unit_of_measurement', '').lower()
                logger.info(f"GridFrom sensor unit: {grid_from_unit}")

            grid_to_info = ha_client.get_state_with_attributes(grid_to_sensor)
            if grid_to_info:
                grid_to_unit = grid_to_info.get('attributes', {}).get('unit_of_measurement', '').lower()
                logger.info(f"GridTo sensor unit: {grid_to_unit}")

            pv_info = ha_client.get_state_with_attributes(pv_sensor)
            if pv_info:
                pv_unit = pv_info.get('attributes', {}).get('unit_of_measurement', '').lower()
                logger.info(f"PV sensor unit: {pv_unit}")

            if battery_sensor:
                battery_info = ha_client.get_state_with_attributes(battery_sensor)
                if battery_info:
                    battery_unit = battery_info.get('attributes', {}).get('unit_of_measurement', '').lower()
                    logger.info(f"Battery sensor unit: {battery_unit}")

            # Process grid FROM sensor data
            grid_from_hourly_data = {}  # Key: (date, hour), Value: list of values (kW)

            for entry in grid_from_history:
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

                    # Should be non-negative (import)
                    if value < 0:
                        logger.warning(f"Negative grid FROM value {value} at {local_timestamp} - skipping")
                        continue

                    # Skip unrealistically high values
                    if value > 50000:  # > 50 kW or 50000 W
                        continue

                    # Convert based on sensor unit (v1.2.0-beta.13)
                    if grid_from_unit and ('kw' in grid_from_unit or 'kilowatt' in grid_from_unit):
                        # Already in kW, no conversion needed
                        pass
                    else:
                        # Assume Watts, convert to kW
                        value = value / 1000

                    date_key = local_timestamp.date()
                    hour_key = local_timestamp.hour
                    key = (date_key, hour_key)

                    if key not in grid_from_hourly_data:
                        grid_from_hourly_data[key] = []
                    grid_from_hourly_data[key].append(value)

                except Exception as e:
                    logger.debug(f"Skipping grid FROM entry: {e}")
                    continue

            # Process grid TO sensor data
            grid_to_hourly_data = {}  # Key: (date, hour), Value: list of values (kW)

            for entry in grid_to_history:
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

                    # Should be non-negative (export)
                    if value < 0:
                        logger.warning(f"Negative grid TO value {value} at {local_timestamp} - skipping")
                        continue

                    # Skip unrealistically high values
                    if value > 50000:  # > 50 kW or 50000 W
                        continue

                    # Convert based on sensor unit (v1.2.0-beta.13)
                    if grid_to_unit and ('kw' in grid_to_unit or 'kilowatt' in grid_to_unit):
                        # Already in kW, no conversion needed
                        pass
                    else:
                        # Assume Watts, convert to kW
                        value = value / 1000

                    date_key = local_timestamp.date()
                    hour_key = local_timestamp.hour
                    key = (date_key, hour_key)

                    if key not in grid_to_hourly_data:
                        grid_to_hourly_data[key] = []
                    grid_to_hourly_data[key].append(value)

                except Exception as e:
                    logger.debug(f"Skipping grid TO entry: {e}")
                    continue

            # Process PV sensor data (same as before)
            pv_hourly_data = {}  # Key: (date, hour), Value: list of values (kW)

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
                        value = float(state)
                    except (ValueError, TypeError):
                        continue

                    # PV should not be negative
                    if value < 0:
                        continue

                    # Skip unrealistically high values
                    if value > 50000:  # > 50 kW or 50000 W
                        continue

                    # Convert based on sensor unit (v1.2.0-beta.13)
                    if pv_unit and ('kw' in pv_unit or 'kilowatt' in pv_unit):
                        # Already in kW, no conversion needed
                        pass
                    else:
                        # Assume Watts, convert to kW
                        value = value / 1000

                    date_key = local_timestamp.date()
                    hour_key = local_timestamp.hour
                    key = (date_key, hour_key)

                    if key not in pv_hourly_data:
                        pv_hourly_data[key] = []
                    pv_hourly_data[key].append(value)

                except Exception as e:
                    logger.debug(f"Skipping PV entry: {e}")
                    continue

            # Process battery sensor data (if available)
            battery_hourly_data = {}  # Key: (date, hour), Value: list of values (kW)

            if battery_history:
                for entry in battery_history:
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

                        # Battery can be positive (discharge) or negative (charge)
                        # Skip unrealistically high values
                        if abs(value) > 50000:  # > 50 kW or 50000 W
                            continue

                        # Convert based on sensor unit (v1.2.0-beta.13)
                        if battery_unit and ('kw' in battery_unit or 'kilowatt' in battery_unit):
                            # Already in kW, no conversion needed
                            pass
                        else:
                            # Assume Watts, convert to kW
                            value = value / 1000

                        date_key = local_timestamp.date()
                        hour_key = local_timestamp.hour
                        key = (date_key, hour_key)

                        if key not in battery_hourly_data:
                            battery_hourly_data[key] = []
                        battery_hourly_data[key].append(value)

                    except Exception as e:
                        logger.debug(f"Skipping battery entry: {e}")
                        continue

            logger.info(f"Processed {len(grid_from_hourly_data)} FROM buckets, {len(grid_to_hourly_data)} TO buckets, {len(pv_hourly_data)} PV buckets" + (f", {len(battery_hourly_data)} battery buckets" if battery_hourly_data else ""))

            # Calculate home consumption: Home = PV + (GridFrom - GridTo) + Battery
            # Formula: Hausverbrauch = Netzbezug - Netzeinspeisung + PV + Batterie
            # Only grid sensors (FROM and TO) are required. PV and Battery default to 0 if missing.
            consumption_hourly_data = {}  # Key: (date, hour), Value: avg consumption in kWh

            # Get all unique date/hour combinations where we have BOTH grid sensors (FROM and TO)
            # PV and Battery are optional and will be 0 if missing (e.g., PV at night)
            all_keys = set(grid_from_hourly_data.keys()) & set(grid_to_hourly_data.keys())

            logger.info(f"Found {len(all_keys)} hours with grid sensors (FROM and TO)")

            for key in all_keys:
                date_key, hour_key = key
                grid_from_values = grid_from_hourly_data[key]
                grid_to_values = grid_to_hourly_data[key]

                # Calculate grid averages (in kW)
                grid_from_avg_kw = sum(grid_from_values) / len(grid_from_values)
                grid_to_avg_kw = sum(grid_to_values) / len(grid_to_values)

                # PV: Use 0 if no data (e.g., at night when PV sensor doesn't log)
                pv_avg_kw = 0
                pv_count = 0
                if key in pv_hourly_data:
                    pv_values = pv_hourly_data[key]
                    pv_avg_kw = sum(pv_values) / len(pv_values)
                    pv_count = len(pv_values)

                # Battery: Use 0 if no data (positive = discharge, negative = charge)
                battery_avg_kw = 0
                battery_count = 0
                if key in battery_hourly_data:
                    battery_values = battery_hourly_data[key]
                    battery_avg_kw = sum(battery_values) / len(battery_values)
                    battery_count = len(battery_values)

                # Calculate net grid power (positive = import, negative = export)
                grid_net_kw = grid_from_avg_kw - grid_to_avg_kw

                # Calculate home consumption: Home = PV + GridNet + Battery
                # This matches: Hausverbrauch = Netzbezug - Netzeinspeisung + PV + Batterie
                home_avg_kw = pv_avg_kw + grid_net_kw + battery_avg_kw

                # DEBUG: Log detailed calculation for specific date/hour (5.11. 11:00)
                if date_key.month == 11 and date_key.day == 5 and hour_key == 11:
                    logger.info(f"🔍 DEBUG {date_key} {hour_key}:00 - DETAILED CALCULATION:")
                    logger.info(f"  GridFrom: {len(grid_from_values)} values, avg={grid_from_avg_kw:.3f} kW, raw values: {[f'{v:.3f}' for v in grid_from_values[:10]]}")
                    logger.info(f"  GridTo: {len(grid_to_values)} values, avg={grid_to_avg_kw:.3f} kW, raw values: {[f'{v:.3f}' for v in grid_to_values[:10]]}")
                    if pv_count > 0:
                        logger.info(f"  PV: {pv_count} values, avg={pv_avg_kw:.3f} kW, raw values: {[f'{v:.3f}' for v in pv_values[:10]]}")
                    else:
                        logger.info(f"  PV: No data - using 0 kW")
                    if battery_count > 0:
                        logger.info(f"  Battery: {battery_count} values, avg={battery_avg_kw:.3f} kW, raw values: {[f'{v:.3f}' for v in battery_values[:10]]}")
                    else:
                        logger.info(f"  Battery: No data - using 0 kW")
                    logger.info(f"  GridNet = GridFrom - GridTo = {grid_from_avg_kw:.3f} - {grid_to_avg_kw:.3f} = {grid_net_kw:.3f} kW")
                    logger.info(f"  HOME = PV + GridNet + Battery = {pv_avg_kw:.3f} + {grid_net_kw:.3f} + {battery_avg_kw:.3f} = {home_avg_kw:.3f} kWh")


                # Validate result
                if home_avg_kw < 0:
                    logger.warning(f"Negative home consumption {home_avg_kw:.3f} kW at {key} "
                                  f"(PV={pv_avg_kw:.3f}, FROM={grid_from_avg_kw:.3f}, TO={grid_to_avg_kw:.3f}, Battery={battery_avg_kw:.3f}) - skipping")
                    continue

                # For 1 hour average in kW, energy is kW * 1h = kWh
                consumption_hourly_data[key] = home_avg_kw

                # Debug log for validation
                if home_avg_kw > 5.0:
                    logger.info(f"🔍 High consumption at {key}: Home={home_avg_kw:.2f}kW "
                              f"(PV={pv_avg_kw:.2f}, FROM={grid_from_avg_kw:.2f}, TO={grid_to_avg_kw:.2f}, Battery={battery_avg_kw:.2f})")

            logger.info(f"Calculated {len(consumption_hourly_data)} valid consumption values")

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
            result['history_entries'] = len(grid_from_history) + len(grid_to_history) + len(pv_history)
            result['imported_days'] = len(daily_data)
            return result

        except Exception as e:
            logger.error(f"Error importing calculated consumption from HA (dual grid): {e}", exc_info=True)
            return {
                'success': False,
                'error': str(e),
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

