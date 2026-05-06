"""
Device Scheduler for Kostal Battery Manager
Manages scheduled devices to run during cheap price periods

Features:
- Schedule up to 3 devices (e.g., pool pumps, washing machines)
- Flexible runtime and power configuration (direct values or HA entities)
- Splittable or continuous runtime scheduling
- Optimal time calculation based on Tibber prices
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
import threading
import time

logger = logging.getLogger(__name__)


class ScheduledDevice:
    """Represents a single scheduled device"""

    def __init__(self, device_id: str, entity_id: str, runtime_config: str,
                 power_config: str, splittable: bool = False):
        """
        Initialize a scheduled device

        Args:
            device_id: Device identifier (1, 2, or 3)
            entity_id: Home Assistant switch entity ID
            runtime_config: Runtime in hours (direct value or entity ID)
            power_config: Power in watts (direct value or entity ID)
            splittable: Whether runtime can be split across multiple periods
        """
        self.device_id = device_id
        self.entity_id = entity_id
        self.runtime_config = runtime_config
        self.power_config = power_config
        self.splittable = splittable

        # Runtime tracking
        self.scheduled_slots: List[Tuple[datetime, datetime]] = []
        self.current_state = False
        self.today_runtime = 0.0  # Hours already run today
        self.last_reset = datetime.now().date()

    def get_runtime_hours(self, ha_client) -> Optional[float]:
        """
        Get runtime in hours, either from direct config or HA entity

        Returns:
            Runtime in hours or None if invalid
        """
        try:
            # Try direct numeric value first
            return float(self.runtime_config)
        except (ValueError, TypeError):
            # Try to get from HA entity
            if ha_client and self.runtime_config:
                try:
                    state = ha_client.get_state(self.runtime_config)
                    if state and 'state' in state:
                        return float(state['state'])
                except Exception as e:
                    logger.warning(f"Could not get runtime from entity {self.runtime_config}: {e}")
        return None

    def get_power_watts(self, ha_client) -> Optional[float]:
        """
        Get power in watts, either from direct config or HA entity

        Returns:
            Power in watts or None if invalid
        """
        try:
            # Try direct numeric value first
            return float(self.power_config)
        except (ValueError, TypeError):
            # Try to get from HA entity
            if ha_client and self.power_config:
                try:
                    state = ha_client.get_state(self.power_config)
                    if state and 'state' in state:
                        return float(state['state'])
                except Exception as e:
                    logger.warning(f"Could not get power from entity {self.power_config}: {e}")
        return None

    def reset_daily_tracking(self):
        """Reset daily runtime tracking"""
        today = datetime.now().date()
        if today != self.last_reset:
            self.today_runtime = 0.0
            self.scheduled_slots = []
            self.last_reset = today
            logger.info(f"Device {self.device_id}: Daily runtime tracking reset")


class DeviceScheduler:
    """Manages scheduling for all configured devices"""

    def __init__(self, config: dict, ha_client, forecast_solar_api=None):
        """
        Initialize device scheduler

        Args:
            config: Configuration dictionary
            ha_client: Home Assistant client for state management
            forecast_solar_api: Optional ForecastSolarAPI for PV-aware scheduling (v1.3)
        """
        self.config = config
        self.ha_client = ha_client
        self.forecast_solar_api = forecast_solar_api
        self.devices: Dict[str, ScheduledDevice] = {}
        self.running = False
        self.scheduler_thread = None

        self._load_devices()

    def set_forecast_solar_api(self, api):
        """v1.3: Wire up forecast.solar API for PV-aware scheduling."""
        self.forecast_solar_api = api
        logger.info("Forecast.Solar API integrated into device scheduler")

    def _get_hourly_pv_forecast(self):
        """v1.3: Fetch hourly PV forecast (W per hour) keyed by datetime.

        Returns dict: {datetime: pv_kwh_for_that_hour}
        Uses forecast.solar Pro 'time'-style logic (sliding window over hourly forecast).
        """
        if not self.forecast_solar_api:
            return {}
        if not self.config.get('enable_forecast_solar_api', False):
            return {}

        planes = self.config.get('forecast_solar_planes', [])
        if not planes:
            return {}

        try:
            hourly = self.forecast_solar_api.get_hourly_forecast(planes, include_tomorrow=True)
            if not hourly:
                return {}

            now = datetime.now().astimezone()
            today = now.date()
            tomorrow = today + timedelta(days=1)
            result = {}
            for hour_idx, kwh in hourly.items():
                if hour_idx < 24:
                    dt = datetime.combine(today, datetime.min.time()).replace(
                        hour=hour_idx, tzinfo=now.tzinfo)
                else:
                    dt = datetime.combine(tomorrow, datetime.min.time()).replace(
                        hour=hour_idx - 24, tzinfo=now.tzinfo)
                result[dt] = kwh
            return result
        except Exception as e:
            logger.warning(f"Could not fetch PV forecast for device scheduler: {e}")
            return {}

    def _load_devices(self):
        """Load scheduled devices from configuration"""
        self.devices = {}

        # Debug: Log all config keys related to scheduled devices
        scheduled_keys = [k for k in self.config.keys() if 'scheduled_device' in k]
        logger.info(f"📝 Device scheduler checking config keys: {scheduled_keys}")
        for key in scheduled_keys:
            logger.debug(f"  {key} = {self.config[key]}")

        for i in range(1, 4):  # Devices 1-3
            device_key = f'scheduled_device_{i}'
            runtime_key = f'scheduled_device_{i}_runtime'
            power_key = f'scheduled_device_{i}_power'
            splittable_key = f'scheduled_device_{i}_splittable'

            entity_id = self.config.get(device_key)
            runtime_config = self.config.get(runtime_key)
            power_config = self.config.get(power_key)

            logger.debug(f"Device {i}: entity_id={entity_id}, runtime={runtime_config}, power={power_config}")

            # Only add if device entity is configured
            if entity_id and runtime_config and power_config:
                # v1.2.1 - Explicit type conversion (config values are strings!)
                # v1.3.4 - Accept 'on' too (HTML form-checkbox default)
                splittable_raw = self.config.get(splittable_key, False)
                splittable = bool(splittable_raw) if isinstance(splittable_raw, bool) else \
                    str(splittable_raw).strip().lower() in ('true', '1', 'yes', 'on')

                device = ScheduledDevice(
                    device_id=str(i),
                    entity_id=entity_id,
                    runtime_config=runtime_config,
                    power_config=power_config,
                    splittable=splittable
                )

                self.devices[str(i)] = device
                logger.info(f"✓ Loaded scheduled device {i}: {entity_id} "
                          f"(runtime: {runtime_config}h, power: {power_config}W, "
                          f"splittable: {splittable})")

        if not self.devices:
            logger.info("No scheduled devices configured")

    def _effective_price_for_hour(self, hour_dt: datetime, tibber_price: float,
                                   device_power_w: float, pv_forecast: Dict) -> float:
        """v1.3: Compute effective electricity price for the device in this hour
        accounting for PV self-consumption.

        Logic per hour:
          device_kwh = device_power_w / 1000  (assumes runs for 1 hour)
          pv_kwh    = pv_forecast.get(hour_dt, 0)
          pv_to_device = min(pv_kwh, device_kwh)
          grid_to_device = device_kwh - pv_to_device

          # Cost components:
          #  - grid_to_device kWh @ tibber_price (we pay)
          #  - pv_to_device kWh — we lose feed-in revenue (8.2 Ct/kWh)
          # Effective rate per kWh consumed by device:
          effective_per_kwh = (grid_to_device * tibber_price + pv_to_device * 0.082) / device_kwh

        For tibber_price = 25 Ct, feed-in = 8.2 Ct, full PV coverage:
          effective = 8.2 Ct (only feed-in opportunity cost)
        For zero PV coverage:
          effective = 25 Ct (full grid price)
        """
        if device_power_w <= 0 or not pv_forecast:
            return tibber_price

        feed_in_value = float(self.config.get('einspeise_verguetung_eur_per_kwh', 0.082))
        device_kwh = device_power_w / 1000.0
        # Round to start-of-hour
        slot_hour = hour_dt.replace(minute=0, second=0, microsecond=0)
        pv_kwh = pv_forecast.get(slot_hour, 0.0)
        pv_to_device = min(pv_kwh, device_kwh)
        grid_to_device = max(0.0, device_kwh - pv_to_device)

        if device_kwh < 1e-6:
            return tibber_price
        effective = (grid_to_device * tibber_price + pv_to_device * feed_in_value) / device_kwh
        return effective

    def calculate_optimal_schedule(self, device: ScheduledDevice,
                                   price_data: List[Dict]) -> List[Tuple[datetime, datetime]]:
        """
        Calculate optimal time slots for device operation with guaranteed runtime

        Strategy:
        1. Always try to use the cheapest available hours (v1.3: PV-aware effective prices)
        2. Emergency mode: If running out of time today, start immediately
        3. Guarantee: Ensure device gets its required runtime, even if prices aren't optimal

        Args:
            device: ScheduledDevice instance
            price_data: List of price data with timestamps and prices

        Returns:
            List of (start_time, end_time) tuples for device operation
        """
        runtime_hours = device.get_runtime_hours(self.ha_client)
        power_watts = device.get_power_watts(self.ha_client)

        if not runtime_hours or not power_watts:
            logger.warning(f"Device {device.device_id}: Invalid runtime or power configuration")
            return []

        # v1.3: Fetch PV forecast and compute effective price per hour for THIS device.
        # Build a per-device copy so mutations do not bleed into other devices.
        pv_forecast = self._get_hourly_pv_forecast()
        pv_aware_raw = self.config.get('enable_pv_aware_device_scheduling', True)
        pv_aware = bool(pv_aware_raw) if isinstance(pv_aware_raw, bool) else \
            str(pv_aware_raw).strip().lower() in ('true', '1', 'yes', 'on')
        if pv_forecast and pv_aware:
            new_price_data = []
            for entry in price_data:
                t = entry.get('start_time')
                tibber_p = entry.get('price', 0)
                copy_entry = dict(entry)
                if t is not None:
                    copy_entry['_tibber_price'] = tibber_p
                    copy_entry['price'] = self._effective_price_for_hour(
                        t, tibber_p, power_watts, pv_forecast)
                new_price_data.append(copy_entry)
            price_data = new_price_data
            logger.info(f"Device {device.device_id}: PV-aware pricing applied "
                       f"({len(pv_forecast)} PV forecast hours, {power_watts:.0f}W device)")

        # Calculate remaining runtime needed today
        remaining_hours = runtime_hours - device.today_runtime
        if remaining_hours <= 0:
            logger.info(f"Device {device.device_id}: Daily runtime already completed")
            return []

        # Check if device is currently running in an existing slot
        now = datetime.now()
        now_aware = now.astimezone() if now.tzinfo is None else now
        currently_running_slot = None

        for start_time, end_time in device.scheduled_slots:
            if start_time <= now_aware < end_time:
                currently_running_slot = (start_time, end_time)
                logger.debug(f"Device {device.device_id}: Currently running in slot "
                           f"{start_time.strftime('%H:%M')} - {end_time.strftime('%H:%M')}")
                break

        # Filter for future times: either current hour or later
        # Include current hour to preserve running slots
        future_prices = [p for p in price_data
                        if p.get('start_time', now_aware).replace(minute=0, second=0, microsecond=0)
                        >= now_aware.replace(minute=0, second=0, microsecond=0)]

        if not future_prices:
            logger.warning(f"Device {device.device_id}: No future price data available")
            # If currently running, keep that slot
            return [currently_running_slot] if currently_running_slot else []

        # If device is currently running, preserve the running slot
        if currently_running_slot:
            start_time, end_time = currently_running_slot
            logger.info(f"Device {device.device_id}: Preserving currently running slot "
                      f"{start_time.strftime('%H:%M')} - {end_time.strftime('%H:%M')}")
            return [currently_running_slot]

        # Calculate hours until end of day (midnight)
        # Find the last available hour in price_data (within today)
        midnight = now_aware.replace(hour=23, minute=59, second=59, microsecond=999999)

        # Separate today and tomorrow prices
        today_future_prices = [p for p in future_prices
                              if p.get('start_time').date() == now_aware.date()]
        tomorrow_prices = [p for p in future_prices
                          if p.get('start_time').date() > now_aware.date()]

        hours_until_eod = len(today_future_prices)
        hours_needed = int(remaining_hours)

        # v1.2.0-beta.70: TODAY-FIRST STRATEGY (Fixed)
        # Problem: If tomorrow is cheaper, old logic would skip today completely!
        # Solution: ALWAYS prioritize today for daily devices (they MUST run today!)

        # GUARANTEE: Daily devices MUST run today if time available
        # Only exception: If it's very late (23:00+) and not enough time left

        current_hour = now_aware.hour

        # STRICT POLICY: Use today-first unless it's impossible
        use_today_first = True  # Default: ALWAYS prioritize today

        if hours_until_eod == 0:
            # No time left today - must use tomorrow
            use_today_first = False
            logger.info(f"Device {device.device_id}: No hours left today (EOD reached), using tomorrow")
        elif hours_needed > hours_until_eod:
            # EMERGENCY MODE: Not enough time left today
            logger.warning(f"⚠️ Device {device.device_id}: EMERGENCY - Need {hours_needed}h but only {hours_until_eod}h left today!")
            logger.warning(f"   Will use ALL remaining {hours_until_eod}h today + {hours_needed - hours_until_eod}h tomorrow")
            use_today_first = True  # Force today usage
        else:
            # NORMAL MODE: Enough time today - always use today first!
            logger.info(f"📅 Device {device.device_id}: Daily device with {hours_until_eod}h available today → TODAY-FIRST (need {hours_needed}h)")
            use_today_first = True

        # Sort prices by value (cheapest first)
        # v1.2.0-beta.69: If use_today_first, prefer today's hours
        if use_today_first and today_future_prices:
            # Use today's hours first, then tomorrow if needed
            sorted_today = sorted(today_future_prices, key=lambda x: x.get('price', float('inf')))
            sorted_tomorrow = sorted(tomorrow_prices, key=lambda x: x.get('price', float('inf')))
            sorted_prices = sorted_today + sorted_tomorrow  # Today first, then tomorrow
            logger.debug(f"Device {device.device_id}: Using TODAY-FIRST strategy ({len(sorted_today)} today + {len(sorted_tomorrow)} tomorrow)")
        else:
            # Normal: cheapest overall (can be mixed today/tomorrow)
            sorted_prices = sorted(future_prices, key=lambda x: x.get('price', float('inf')))

        # GUARANTEE: Ensure we have enough hours
        # If we don't have enough hours in future_prices, we need all available hours
        available_hours = len(future_prices)
        if hours_needed > available_hours:
            logger.warning(f"⚠️ Device {device.device_id}: Need {hours_needed}h but only {available_hours}h available in price data")
            hours_needed = available_hours

        if device.splittable:
            # Splittable: Select cheapest individual hours
            slots = []

            # Take the cheapest N hours
            selected_prices = sorted_prices[:hours_needed]

            for price_entry in selected_prices:
                start_time = price_entry.get('start_time')
                if start_time:
                    end_time = start_time + timedelta(hours=1)
                    slots.append((start_time, end_time))

            # Sort slots by time for logical display
            slots.sort(key=lambda x: x[0])

            if slots:
                avg_price = sum(p.get('price', 0) for p in selected_prices) / len(selected_prices)
                logger.info(f"✓ Device {device.device_id}: Splittable schedule - {len(slots)} slots, avg price {avg_price:.2f} Ct/kWh")
            else:
                logger.warning(f"⚠️ Device {device.device_id}: No slots created despite having {hours_needed}h needed")

        else:
            # Continuous: Find cheapest continuous block
            slots = []

            # Need to find continuous block in time order (not sorted by price)
            # Try to find the cheapest continuous block
            best_start_idx = None
            best_avg_price = float('inf')

            # Search through future_prices (time-ordered) for best continuous block
            if hours_needed <= len(future_prices):
                for i in range(len(future_prices) - hours_needed + 1):
                    block = future_prices[i:i + hours_needed]
                    avg_price = sum(p.get('price', 0) for p in block) / len(block)

                    if avg_price < best_avg_price:
                        best_avg_price = avg_price
                        best_start_idx = i

                # Create continuous slot
                if best_start_idx is not None:
                    start_time = future_prices[best_start_idx].get('start_time')
                    end_time = future_prices[best_start_idx + hours_needed - 1].get('start_time') + timedelta(hours=1)

                    if start_time and end_time:
                        slots.append((start_time, end_time))
                        logger.info(f"✓ Device {device.device_id}: Continuous schedule - "
                                  f"{start_time.strftime('%H:%M')} to {end_time.strftime('%H:%M')}, "
                                  f"avg price {best_avg_price:.2f} Ct/kWh")
            else:
                logger.error(f"❌ Device {device.device_id}: Cannot create continuous block - "
                           f"need {hours_needed}h but only {len(future_prices)}h available")

        return slots

    def update_schedules(self, price_data: List[Dict]):
        """
        Update schedules for all devices based on current price data

        Args:
            price_data: List of price data with timestamps and prices
        """
        if not self.devices:
            return

        logger.info(f"Updating device schedules based on {len(price_data)} price points")

        for device_id, device in self.devices.items():
            # Reset daily tracking if needed
            device.reset_daily_tracking()

            # Calculate optimal schedule
            slots = self.calculate_optimal_schedule(device, price_data)
            device.scheduled_slots = slots

            if slots:
                logger.info(f"Device {device.device_id} ({device.entity_id}): "
                          f"Scheduled for {len(slots)} time slot(s)")
                for start, end in slots:
                    logger.info(f"  → {start.strftime('%Y-%m-%d %H:%M')} - {end.strftime('%Y-%m-%d %H:%M')}")
            else:
                logger.warning(f"Device {device.device_id} ({device.entity_id}): No slots scheduled")

    def control_devices(self):
        """Control devices based on current time and schedule"""
        if not self.devices:
            return

        # Make timezone-aware to compare with scheduled slots (v1.2.0-beta.68)
        now = datetime.now()
        now_aware = now.astimezone() if now.tzinfo is None else now

        for device_id, device in self.devices.items():
            should_be_on = False

            # Check if current time is within any scheduled slot
            for start_time, end_time in device.scheduled_slots:
                if start_time <= now_aware < end_time:
                    should_be_on = True
                    break

            # Control device if state needs to change
            if should_be_on != device.current_state:
                try:
                    if should_be_on:
                        self.ha_client.turn_on(device.entity_id)
                        logger.info(f"✓ Device {device.device_id} ({device.entity_id}): Turned ON")
                    else:
                        self.ha_client.turn_off(device.entity_id)
                        logger.info(f"✓ Device {device.device_id} ({device.entity_id}): Turned OFF")

                    device.current_state = should_be_on

                except Exception as e:
                    logger.error(f"Error controlling device {device.device_id}: {e}")

            # Update runtime tracking
            if should_be_on:
                # Add time to today's runtime (check interval in hours)
                # v1.2.1 - Explicit type conversion (config values are strings!)
                control_interval = float(self.config.get('control_interval', 30))
                device.today_runtime += control_interval / 3600.0

    def start(self):
        """Start the device scheduler thread"""
        if self.running:
            logger.warning("Device scheduler already running")
            return

        if not self.devices:
            logger.info("No devices to schedule - scheduler not started")
            return

        self.running = True
        self.scheduler_thread = threading.Thread(target=self._scheduler_loop, daemon=True)
        self.scheduler_thread.start()
        logger.info("✓ Device scheduler started")

    def stop(self):
        """Stop the device scheduler thread"""
        if not self.running:
            return

        self.running = False
        if self.scheduler_thread:
            self.scheduler_thread.join(timeout=5)
        logger.info("Device scheduler stopped")

    def _scheduler_loop(self):
        """Main scheduler loop (runs in separate thread)"""
        while self.running:
            try:
                self.control_devices()
            except Exception as e:
                logger.error(f"Error in device scheduler loop: {e}")

            # Sleep for control interval
            # v1.2.1 - Explicit type conversion (config values are strings!)
            control_interval = float(self.config.get('control_interval', 30))
            time.sleep(control_interval)

    def get_status(self) -> Dict:
        """
        Get current status of all scheduled devices

        Returns:
            Dictionary with device status information
        """
        status = {
            'enabled': bool(self.devices),
            'devices': {}
        }

        for device_id, device in self.devices.items():
            runtime_hours = device.get_runtime_hours(self.ha_client)
            power_watts = device.get_power_watts(self.ha_client)

            status['devices'][device_id] = {
                'entity_id': device.entity_id,
                'runtime_hours': runtime_hours,
                'power_watts': power_watts,
                'splittable': device.splittable,
                'current_state': device.current_state,
                'today_runtime': round(device.today_runtime, 2),
                'scheduled_slots': [
                    {
                        'start': slot[0].strftime('%Y-%m-%d %H:%M'),
                        'end': slot[1].strftime('%Y-%m-%d %H:%M')
                    }
                    for slot in device.scheduled_slots
                ]
            }

        return status
