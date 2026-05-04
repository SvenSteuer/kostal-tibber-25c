#!/usr/bin/env python3
"""
Tibber-basierte Lade-Optimierung
Portiert von Home Assistant Automationen
"""

import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple

logger = logging.getLogger(__name__)


class TibberOptimizer:
    """Smart charging optimization based on Tibber prices"""

    def __init__(self, config: Dict):
        # v1.2.1 - Explicit type conversion (config values are strings!)
        self.threshold_1h = float(config.get('tibber_price_threshold_1h', 8)) / 100
        self.threshold_3h = float(config.get('tibber_price_threshold_3h', 8)) / 100
        self.charge_duration_per_10 = int(config.get('charge_duration_per_10_percent', 18))
        self.consumption_learner = None  # v0.4.0
        self.forecast_solar_api = None  # v0.9.2

    def set_consumption_learner(self, learner):
        """Set consumption learner for advanced optimization (v0.4.0)"""
        self.consumption_learner = learner
        logger.info("Consumption learner integrated into optimizer")

    def set_forecast_solar_api(self, api):
        """Set Forecast.Solar API client for PV forecasts (v0.9.2)"""
        self.forecast_solar_api = api
        logger.info("Forecast.Solar API integrated into optimizer")

    def get_hourly_pv_forecast(self, ha_client, config, include_tomorrow=False) -> Dict[int, float]:
        """
        Get hourly PV forecast (v0.9.2: now supports Forecast.Solar API)

        Priority:
        1. Forecast.Solar Professional API (if enabled and configured)
        2. Home Assistant sensors with wh_hours attribute (fallback)

        Args:
            ha_client: Home Assistant client instance
            config: Configuration dict with sensor names
            include_tomorrow: If True, returns 48h forecast (today 0-23 + tomorrow 24-47)

        Returns:
            dict: {hour: kwh_forecast} for each hour
                  If include_tomorrow=False: hour 0-23 (today only)
                  If include_tomorrow=True: hour 0-47 (today=0-23, tomorrow=24-47)
        """
        # v0.9.2 - Try Forecast.Solar API first if enabled
        # v1.2.1 - Explicit type conversion (config values are strings!)
        api_enabled_raw = config.get('enable_forecast_solar_api', False)
        api_enabled = bool(api_enabled_raw) if isinstance(api_enabled_raw, bool) else str(api_enabled_raw).lower() in ('true', '1', 'yes')
        if self.forecast_solar_api and api_enabled:

            logger.debug(f"Using Forecast.Solar Professional API for PV forecast (include_tomorrow={include_tomorrow})")

            planes = config.get('forecast_solar_planes', [])
            if planes:
                try:
                    hourly_forecast = self.forecast_solar_api.get_hourly_forecast(planes, include_tomorrow=include_tomorrow)
                    if hourly_forecast:
                        return hourly_forecast
                    else:
                        logger.warning("Forecast.Solar API returned no data, falling back to sensors")
                except Exception as e:
                    logger.error(f"Error using Forecast.Solar API: {e}, falling back to sensors")
            else:
                logger.warning("Forecast.Solar API enabled but no planes configured")

        # Fallback: Use Home Assistant sensors (original v0.8.1 method)
        logger.debug(f"Using Home Assistant sensors for PV forecast (include_tomorrow={include_tomorrow})")
        hourly_forecast = {}

        # Get sensor names from config
        roof1_today_sensor = config.get('pv_production_today_roof1')
        roof2_today_sensor = config.get('pv_production_today_roof2')
        roof1_tomorrow_sensor = config.get('pv_production_tomorrow_roof1') if include_tomorrow else None
        roof2_tomorrow_sensor = config.get('pv_production_tomorrow_roof2') if include_tomorrow else None

        logger.debug(f"PV forecast sensors: roof1_today='{roof1_today_sensor}', roof2_today='{roof2_today_sensor}'")
        if include_tomorrow:
            logger.debug(f"Tomorrow sensors: roof1_tomorrow='{roof1_tomorrow_sensor}', roof2_tomorrow='{roof2_tomorrow_sensor}'")

        if not roof1_today_sensor and not roof2_today_sensor:
            logger.warning("No PV forecast sensors configured")
            return {}

        try:
            # Get today's and tomorrow's date for filtering
            now = datetime.now().astimezone()
            today = now.date()
            tomorrow = today + timedelta(days=1)

            # Process TODAY's sensors
            for roof_sensor in [roof1_today_sensor, roof2_today_sensor]:
                if not roof_sensor:
                    continue

                logger.debug(f"Fetching today attributes from {roof_sensor}")
                attrs = ha_client.get_attributes(roof_sensor)
                if attrs:
                    logger.debug(f"Today {roof_sensor} attributes keys: {list(attrs.keys())}")
                    if 'wh_hours' in attrs:
                        wh_hours = attrs['wh_hours']
                        logger.debug(f"Today {roof_sensor} wh_hours has {len(wh_hours)} entries")

                        for timestamp_str, wh_value in wh_hours.items():
                            try:
                                # Parse timestamp
                                dt = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))

                                # Only process today's data
                                if dt.date() != today:
                                    continue

                                hour = dt.hour  # 0-23 for today
                                kwh = float(wh_value) / 1000.0  # Wh to kWh

                                # Add to hourly forecast
                                hourly_forecast[hour] = hourly_forecast.get(hour, 0.0) + kwh

                            except (ValueError, TypeError) as e:
                                logger.warning(f"Error parsing wh_hours entry {timestamp_str}: {e}")
                                continue
                    else:
                        logger.warning(f"Sensor {roof_sensor} has no 'wh_hours' attribute")
                else:
                    logger.warning(f"Could not get attributes for sensor {roof_sensor}")

            # Process TOMORROW's sensors (if requested)
            if include_tomorrow:
                for roof_sensor in [roof1_tomorrow_sensor, roof2_tomorrow_sensor]:
                    if not roof_sensor:
                        continue

                    logger.debug(f"Fetching tomorrow attributes from {roof_sensor}")
                    attrs = ha_client.get_attributes(roof_sensor)
                    if attrs:
                        logger.debug(f"Tomorrow {roof_sensor} attributes keys: {list(attrs.keys())}")
                        if 'wh_hours' in attrs:
                            wh_hours = attrs['wh_hours']
                            logger.debug(f"Tomorrow {roof_sensor} wh_hours has {len(wh_hours)} entries")

                            for timestamp_str, wh_value in wh_hours.items():
                                try:
                                    # Parse timestamp
                                    dt = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))

                                    # Only process tomorrow's data
                                    if dt.date() != tomorrow:
                                        continue

                                    hour = dt.hour + 24  # Offset by 24 hours: 24-47 for tomorrow
                                    kwh = float(wh_value) / 1000.0  # Wh to kWh

                                    # Add to hourly forecast
                                    hourly_forecast[hour] = hourly_forecast.get(hour, 0.0) + kwh

                                except (ValueError, TypeError) as e:
                                    logger.warning(f"Error parsing wh_hours entry {timestamp_str}: {e}")
                                    continue
                        else:
                            logger.warning(f"Tomorrow sensor {roof_sensor} has no 'wh_hours' attribute")
                    else:
                        logger.warning(f"Could not get attributes for tomorrow sensor {roof_sensor}")

            if hourly_forecast:
                logger.info(f"Retrieved hourly PV forecast for {len(hourly_forecast)} hours")
                logger.debug(f"PV forecast: {hourly_forecast}")
            else:
                logger.warning("No hourly PV forecast data available")

            return hourly_forecast

        except Exception as e:
            logger.error(f"Error getting hourly PV forecast: {e}")
            return {}

    def find_optimal_charge_end_time(self, prices: List[Dict]) -> Optional[datetime]:
        """
        Findet den optimalen Zeitpunkt zum Beenden der Ladung.
        Das ist der Moment, an dem der Preis nach einer günstigen Phase wieder steigt.

        Args:
            prices: Liste von Preis-Dicts mit 'total', 'startsAt', 'level'

        Returns:
            datetime des optimalen Ladeendes oder None
        """
        # v0.3.3 - Use timezone-aware datetime for comparison
        now = datetime.now().astimezone()

        # Brauchen mindestens 6 Datenpunkte (3 zurück, aktuell, 2 voraus)
        if len(prices) < 6:
            logger.warning("Not enough price data for optimization")
            return None

        # Durchlaufe Preise ab Index 3 (brauchen 2h Historie)
        for i in range(3, len(prices) - 2):
            try:
                # Parse startsAt Zeit
                starts_at_str = prices[i]['startsAt']
                starts_at = datetime.fromisoformat(starts_at_str.replace('Z', '+00:00'))

                # Überspringe vergangene Zeiten
                if starts_at <= now:
                    continue

                # Hole Preise
                current_price = float(prices[i]['total'])
                price_1h_ago = float(prices[i-1]['total'])
                price_2h_ago = float(prices[i-2]['total'])
                price_1h_future = float(prices[i+1]['total'])
                price_2h_future = float(prices[i+2]['total'])

                # Berechne 3h Summen
                sum_3h_past = current_price + price_1h_ago + price_2h_ago
                sum_3h_future = current_price + price_1h_future + price_2h_future

                # Bedingung 1: Preis steigt um mehr als Schwelle zur vorherigen Stunde
                condition_1 = current_price > price_1h_ago * (1 + self.threshold_1h)

                # Bedingung 2: Nächste 3h Block teurer als vergangener 3h Block
                condition_2 = sum_3h_past < sum_3h_future * (1 + self.threshold_3h)

                if condition_1 and condition_2:
                    logger.info(f"Found optimal charge end time: {starts_at}")
                    logger.info(f"  Current price: {current_price:.4f}, 1h ago: {price_1h_ago:.4f}")
                    logger.info(f"  3h past sum: {sum_3h_past:.4f}, 3h future sum: {sum_3h_future:.4f}")
                    return starts_at

            except (KeyError, ValueError, TypeError) as e:
                logger.warning(f"Error processing price data at index {i}: {e}")
                continue

        logger.info("No optimal charge end time found (prices stay low)")
        return None

    def calculate_charge_start_time(self,
                                     charge_end: datetime,
                                     current_soc: float,
                                     target_soc: float = 95) -> datetime:
        """
        Berechnet den Ladebeginn basierend auf SOC-Differenz.

        Args:
            charge_end: Gewünschter Ladezeitpunkt Ende
            current_soc: Aktueller SOC in %
            target_soc: Ziel-SOC in %

        Returns:
            datetime des Ladebeginns
        """
        # Berechne benötigte Ladung
        soc_diff = target_soc - current_soc

        if soc_diff <= 0:
            # Bereits voll genug
            return charge_end

        # Berechne Ladedauer in Minuten
        charge_duration_minutes = (soc_diff / 10) * self.charge_duration_per_10

        # Berechne Startzeit
        charge_start = charge_end - timedelta(minutes=charge_duration_minutes)

        logger.info(f"Calculated charge start: {charge_start}")
        logger.info(f"  SOC: {current_soc}% → {target_soc}% ({soc_diff}%)")
        logger.info(f"  Duration: {charge_duration_minutes:.0f} minutes")

        return charge_start

    def plan_battery_schedule_rolling(self,
                                       ha_client,
                                       config,
                                       current_soc: float,
                                       prices: List[Dict],
                                       lookahead_hours: int = 24) -> Dict:
        """
        Plans battery schedule with PV-aware grid charging (v1.3 - smart grid charge).

        Strategy:
        1. Forecast consumption + PV (with bias correction) for next N hours
        2. Optionally refine current hour's PV via power_production_now sensors
        3. Plan grid charging:
           - smart mode (default): greedy bridge-the-night with arbitrage check
           - legacy mode: multi-peak economic charging (fallback)
        4. Compute final SOC trajectory

        The smart mode skips grid charging when forecasted PV alone is enough to
        cover consumption AND refill the battery, and only schedules grid charges
        in the cheapest hours where there is genuine deficit AND a meaningful
        price spread vs the deficit hours.
        """
        if not self.consumption_learner:
            logger.warning("No consumption learner available")
            return None

        try:
            now = datetime.now().astimezone()
            current_hour_in_day = now.hour

            # Battery parameters (v1.2.1 - Explicit type conversion!)
            battery_capacity = float(config.get('battery_capacity', 10.6))  # kWh
            min_soc = int(config.get('auto_safety_soc', 20))  # %
            max_soc = int(config.get('auto_charge_below_soc', 95))  # %
            max_charge_power = float(config.get('max_charge_power', 3900)) / 1000  # kW → kWh/h

            # v1.3: PV-aware grid charging
            smart_enabled = self._cfg_bool(config, 'enable_smart_grid_charge', True)
            bias_correction = float(config.get('pv_forecast_bias_correction', 1.3))
            min_spread = float(config.get('grid_arbitrage_min_spread_pct', 5)) / 100.0

            logger.info(f"Planning {lookahead_hours}h rolling schedule starting from {now.strftime('%H:%M')}, "
                       f"SOC={current_soc:.1f}%, smart_charge={smart_enabled}, bias={bias_correction:.2f}x")

            # =================================================================
            # STEP 1 & 2: Forecast consumption and PV for next N hours
            # =================================================================
            hourly_consumption = []
            hourly_pv = []
            hourly_prices = []

            # Get PV forecast (0-23 = today, 24-47 = tomorrow)
            pv_forecast_48h = self.get_hourly_pv_forecast(ha_client, config, include_tomorrow=True)

            for i in range(lookahead_hours):
                # Calculate which calendar hour this represents
                target_hour_in_day = (current_hour_in_day + i) % 24
                target_day_offset = (current_hour_in_day + i) // 24  # 0=today, 1=tomorrow
                target_date = now.date() + timedelta(days=target_day_offset)

                # Hour index in the 48h PV array (0-47)
                pv_hour_index = current_hour_in_day + i

                # Consumption forecast
                consumption = self.consumption_learner.get_average_consumption(
                    target_hour_in_day,
                    target_date=target_date
                )
                hourly_consumption.append(consumption)

                # PV forecast
                pv = pv_forecast_48h.get(pv_hour_index, 0.0) if pv_hour_index < 48 else 0.0
                hourly_pv.append(pv)

                # Price
                price = 0.30  # Fallback
                for p in prices:
                    start_time = p.get('startsAt', '')
                    if start_time:
                        try:
                            price_dt = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
                            target_dt = now + timedelta(hours=i)
                            if price_dt.hour == target_dt.hour and price_dt.date() == target_dt.date():
                                price = p.get('total', 0.30)
                                break
                        except:
                            pass
                hourly_prices.append(price)

            # v1.2.0-beta.5: PV conversion now handled at source in forecast_solar_api.py
            # ForecastSolarAPI.get_hourly_forecast() converts cumulative to hourly deltas
            # Consumption learner returns hourly values directly

            # v1.3: Apply forecast bias correction (forecast.solar systematically under-estimates)
            hourly_pv_raw = list(hourly_pv)
            if smart_enabled and abs(bias_correction - 1.0) > 0.001:
                hourly_pv = [pv * bias_correction for pv in hourly_pv]
                logger.info(f"📈 PV bias correction {bias_correction:.2f}x: "
                           f"{sum(hourly_pv_raw):.1f} → {sum(hourly_pv):.1f} kWh next {lookahead_hours}h")

            # v1.3: Refine current hour with power_production_now (highest correlation)
            if smart_enabled:
                refined = self._refine_current_hour_pv(ha_client, config, now, hourly_pv[0])
                if abs(refined - hourly_pv[0]) > 0.05:
                    logger.info(f"📡 Hour 0 PV refined via power_production_now: "
                               f"{hourly_pv[0]:.2f} → {refined:.2f} kWh")
                    hourly_pv[0] = refined

            logger.debug(f"Forecasts ready: Avg consumption={sum(hourly_consumption)/len(hourly_consumption):.2f}kWh, "
                       f"Avg PV={sum(hourly_pv)/len(hourly_pv):.2f}kWh, "
                       f"Avg price={sum(hourly_prices)/len(hourly_prices)*100:.1f}Ct")

            # DEBUG: Show first 12 hours in detail
            logger.debug("📊 First 12 hours forecast:")
            for i in range(min(12, lookahead_hours)):
                logger.debug(f"  Hour {i:2d}: PV={hourly_pv[i]:6.2f}kWh, Cons={hourly_consumption[i]:6.2f}kWh, "
                          f"Net={hourly_pv[i]-hourly_consumption[i]:+6.2f}kWh, Price={hourly_prices[i]*100:5.1f}Ct")

            # =================================================================
            # STEP 3: Simulate SOC WITHOUT charging to find deficits
            # =================================================================
            baseline_soc = [0.0] * lookahead_hours
            baseline_soc[0] = current_soc

            min_kwh = (min_soc / 100) * battery_capacity
            max_kwh = (max_soc / 100) * battery_capacity
            soc_kwh = (current_soc / 100) * battery_capacity

            logger.debug(f"🔋 Starting baseline SOC simulation from {current_soc:.1f}% ({soc_kwh:.2f} kWh)")
            logger.debug(f"   Battery limits: min={min_soc}% ({min_kwh:.2f} kWh), max={max_soc}% ({max_kwh:.2f} kWh)")

            for hour in range(1, lookahead_hours):
                soc_before = soc_kwh
                soc_before_pct = (soc_before / battery_capacity) * 100

                # Energy from PREVIOUS hour
                net_energy = hourly_pv[hour - 1] - hourly_consumption[hour - 1]
                soc_kwh += net_energy

                # Smart clamping: Allow discharge from above max_soc, but prevent charging above it
                if net_energy > 0:
                    # Adding energy (PV surplus): cap at max_soc
                    soc_kwh = min(max_kwh, soc_kwh)

                # Always respect minimum SOC
                soc_kwh = max(min_kwh, soc_kwh)

                baseline_soc[hour] = (soc_kwh / battery_capacity) * 100

                # Debug: Log all hours in detail
                if hour < lookahead_hours:
                    logger.debug(f"   Hour {hour}: SOC {soc_before_pct:.1f}% → {baseline_soc[hour]:.1f}% "
                              f"(PV={hourly_pv[hour-1]:.2f}, Cons={hourly_consumption[hour-1]:.2f}, Net={net_energy:+.2f} kWh)")

            # Find deficit hours (SOC below threshold)
            deficit_hours = []
            for hour in range(lookahead_hours):
                if baseline_soc[hour] <= min_soc + 5:  # 5% buffer
                    current_kwh = (baseline_soc[hour] / 100) * battery_capacity
                    target_kwh = ((min_soc + 10) / 100) * battery_capacity
                    deficit_kwh = max(0, target_kwh - current_kwh)

                    deficit_hours.append({
                        'hour': hour,
                        'soc': baseline_soc[hour],
                        'deficit_kwh': deficit_kwh,
                        'price': hourly_prices[hour]  # Add price for smart charging
                    })

            logger.info(f"Found {len(deficit_hours)} deficit hours: {[d['hour'] for d in deficit_hours]}")
            if deficit_hours:
                logger.info(f"  First deficit at hour {deficit_hours[0]['hour']}: SOC={deficit_hours[0]['soc']:.1f}%")

            # =================================================================
            # STEP 4: Plan grid charging (smart bridge-the-night or legacy fallback)
            # =================================================================
            if smart_enabled:
                charging_windows, hourly_charging = self._plan_grid_charge_smart(
                    current_soc, hourly_pv, hourly_consumption, hourly_prices,
                    battery_capacity, min_soc, max_soc, max_charge_power,
                    lookahead_hours, min_spread)
            else:
                charging_windows, hourly_charging = self._plan_grid_charge_multipeak_fallback(
                    current_soc, hourly_pv, hourly_consumption, hourly_prices,
                    battery_capacity, min_soc, max_soc, max_charge_power,
                    lookahead_hours, baseline_soc, deficit_hours, min_kwh, max_kwh)

            logger.info(f"Planned {len(charging_windows)} charging windows, total {sum(hourly_charging):.2f} kWh")

            # DEBUG: Log charging plan
            if charging_windows:
                logger.info(f"📊 Charging Plan:")
                for window in charging_windows[:10]:  # First 10 windows
                    logger.info(f"  Hour {window['hour']:2d}: {window['charge_kwh']:.2f} kWh @ "
                               f"{window['price']*100:.1f} Ct ({window['reason']})")

            # =================================================================
            # STEP 5: Calculate final SOC WITH charging
            # =================================================================
            final_soc = [0.0] * lookahead_hours
            final_soc[0] = current_soc
            soc_kwh = (current_soc / 100) * battery_capacity

            logger.info(f"🔋 Starting final SOC calculation with charging from {current_soc:.1f}% ({soc_kwh:.2f} kWh)")

            for hour in range(1, lookahead_hours):
                soc_before = soc_kwh
                soc_before_pct = (soc_before / battery_capacity) * 100

                # Energy from PREVIOUS hour (PV + charging - consumption)
                net_energy = hourly_pv[hour - 1] + hourly_charging[hour - 1] - hourly_consumption[hour - 1]
                soc_kwh += net_energy

                # Smart clamping: Allow discharge from above max_soc, but prevent charging above it
                if net_energy > 0:
                    # Adding energy (PV surplus or grid charging): cap at max_soc
                    soc_kwh = min(max_kwh, soc_kwh)

                # Always respect minimum SOC
                soc_kwh = max(min_kwh, soc_kwh)

                final_soc[hour] = (soc_kwh / battery_capacity) * 100

                # Debug: Log all hours in detail
                if hour < lookahead_hours:
                    charge_str = f", Charge={hourly_charging[hour-1]:.2f}" if hourly_charging[hour-1] > 0 else ""
                    logger.info(f"   Hour {hour}: SOC {soc_before_pct:.1f}% → {final_soc[hour]:.1f}% "
                              f"(PV={hourly_pv[hour-1]:.2f}, Cons={hourly_consumption[hour-1]:.2f}{charge_str}, Net={net_energy:+.2f} kWh)")

            min_soc_reached = min(final_soc)
            logger.info(f"✅ Rolling plan complete: Min SOC {min_soc_reached:.1f}%, "
                       f"{len(charging_windows)} charge windows")

            return {
                'hourly_soc': final_soc,
                'hourly_charging': hourly_charging,
                'hourly_pv': hourly_pv,
                'hourly_consumption': hourly_consumption,
                'hourly_prices': hourly_prices,
                'charging_windows': charging_windows,
                'start_time': now.isoformat(),
                'last_planned': now.isoformat(),
                'min_soc_reached': min_soc_reached,
                'total_charging_kwh': sum(hourly_charging)
            }

        except Exception as e:
            logger.error(f"Error in rolling battery schedule: {e}", exc_info=True)
            return None

    # =====================================================================
    # v1.3 helpers: PV-aware smart grid charging
    # =====================================================================

    @staticmethod
    def _cfg_bool(config: Dict, key: str, default: bool) -> bool:
        """Robustly parse a config value as bool (handles strings 'true'/'1'/'yes')."""
        v = config.get(key, default)
        if isinstance(v, bool):
            return v
        return str(v).strip().lower() in ('true', '1', 'yes', 'on')

    def _refine_current_hour_pv(self, ha_client, config, now: datetime,
                                  fallback_kwh: float) -> float:
        """v1.3: Use forecast.solar power_production_now sensors to refine the
        CURRENT hour's PV estimate.

        These sensors had the highest correlation with reality (0.89) of all
        forecast variants in the analysis. Configured via two optional config
        keys:
          - power_production_now_sensor_1
          - power_production_now_sensor_2
        Both report instantaneous Watts. We translate to kWh for the rest of
        the current hour and add a linear ramp-up estimate for the elapsed part.

        Returns the larger of (fallback, refined estimate) — forecast.solar
        systematically under-estimates, so taking the max is conservative.
        """
        sensor1 = config.get('power_production_now_sensor_1')
        sensor2 = config.get('power_production_now_sensor_2')
        if not sensor1 and not sensor2:
            return fallback_kwh
        try:
            p1 = float(ha_client.get_state(sensor1) or 0) if sensor1 else 0.0
            p2 = float(ha_client.get_state(sensor2) or 0) if sensor2 else 0.0
            total_w = p1 + p2
            if total_w <= 0:
                return fallback_kwh
            minutes_remaining = max(1, 60 - now.minute)
            kwh_rest = total_w / 1000.0 * (minutes_remaining / 60.0)
            elapsed_h = now.minute / 60.0
            kwh_elapsed = total_w / 1000.0 * 0.5 * elapsed_h
            estimate = kwh_rest + kwh_elapsed
            return max(fallback_kwh, estimate)
        except Exception as e:
            logger.warning(f"Could not refine current hour PV: {e}")
            return fallback_kwh

    @staticmethod
    def _simulate_forward_planning(soc_start_kwh: float,
                                    hourly_pv: List[float],
                                    hourly_consumption: List[float],
                                    hourly_charging: List[float],
                                    min_kwh: float, max_kwh: float,
                                    max_charge_power: float,
                                    lookahead_hours: int) -> List[Dict]:
        """Simulate hourly battery flows with natural physics + planned grid charges.

        Returns list of per-hour dicts with: hour, room_at_start, soc_after,
        grid_to_house (= deficit), grid_to_battery, export.
        """
        soc_kwh = soc_start_kwh
        out = []
        for h in range(lookahead_hours):
            pv = hourly_pv[h]
            cons = hourly_consumption[h]
            forced = hourly_charging[h]
            room_at_start = max(0.0, max_kwh - soc_kwh)

            pv_to_house = min(pv, cons)
            leftover_pv = pv - pv_to_house
            leftover_cons = cons - pv_to_house

            if leftover_pv > 0:
                pv_to_battery = min(leftover_pv, max_charge_power, max(0.0, max_kwh - soc_kwh))
                export = leftover_pv - pv_to_battery
                bat_to_house = 0.0
                grid_to_house = 0.0
            else:
                pv_to_battery = 0.0
                export = 0.0
                avail = max(0.0, soc_kwh - min_kwh)
                bat_to_house = min(leftover_cons, max_charge_power, avail)
                grid_to_house = leftover_cons - bat_to_house

            room_after_pv = max(0.0, max_kwh - (soc_kwh + pv_to_battery))
            rate_left = max(0.0, max_charge_power - pv_to_battery)
            grid_to_battery = min(forced, room_after_pv, rate_left)

            new_soc = soc_kwh + pv_to_battery + grid_to_battery - bat_to_house
            new_soc = max(min_kwh, min(max_kwh, new_soc))

            out.append({
                'hour': h, 'room_at_start': room_at_start, 'soc_after': new_soc,
                'grid_to_house': grid_to_house, 'grid_to_battery': grid_to_battery,
                'export': export,
            })
            soc_kwh = new_soc
        return out

    def _plan_grid_charge_smart(self, current_soc: float,
                                 hourly_pv: List[float],
                                 hourly_consumption: List[float],
                                 hourly_prices: List[float],
                                 battery_capacity: float,
                                 min_soc: int, max_soc: int,
                                 max_charge_power: float,
                                 lookahead_hours: int,
                                 min_arbitrage_spread: float) -> Tuple[List[Dict], List[float]]:
        """v1.3: PV-aware bridge-the-night greedy scheduling.

        Algorithm:
          1. Quick PV-skip check: if total PV >> total consumption + battery
             refill needs, schedule no grid charging at all.
          2. Otherwise, iteratively:
             a. Forward-simulate with current charging plan.
             b. Find the most expensive hour where house pulled from grid
                because battery was empty (= "deficit" hour).
             c. Find the cheapest hour BEFORE it that still has room in battery.
             d. If the cheapest charge price is at least min_arbitrage_spread
                cheaper than the deficit price, add a small charge step there.
             e. Repeat until no economical opportunities remain.

        This trades grid-charge cost at cheap hours for avoided grid-import at
        expensive hours, but ONLY when the spread is meaningful.
        """
        min_kwh = (min_soc / 100) * battery_capacity
        max_kwh = (max_soc / 100) * battery_capacity
        soc_start_kwh = (current_soc / 100) * battery_capacity

        hourly_charging = [0.0] * lookahead_hours

        # Always compute baseline once — needed for both the skip-check and the
        # PV-reset-horizon used by the greedy below.
        baseline_sim = self._simulate_forward_planning(
            soc_start_kwh, hourly_pv, hourly_consumption, hourly_charging,
            min_kwh, max_kwh, max_charge_power, lookahead_hours)

        # v1.3.3: PV-reset horizon — the first hour where SOC reaches max_soc through
        # PV alone. Defizits AFTER this point are NOT our problem to plan now: the next
        # PV peak refills the battery, and any post-reset deficit will be handled by a
        # future plan update when its actual SOC is known. Planning grid-charge sessions
        # across a PV day causes "morning charge to cover tomorrow night", which then
        # blocks the PV peak from filling the battery (see v1.3.2 logs 04.05.).
        pv_reset_hour = lookahead_hours
        for r in baseline_sim:
            if r['soc_at_end'] >= max_kwh - 0.5:
                pv_reset_hour = r['hour'] + 1
                break

        # Quick PV-skip: only valid when BOTH:
        #   1. 24h energy balance: PV >= consumption + half the empty battery space
        #   2. distribution is benign: battery doesn't sit pinned at min_soc for many
        #      hours BEFORE the PV reset (after the reset, future plan updates handle it)
        total_pv = sum(hourly_pv)
        total_cons = sum(hourly_consumption)
        room_now = max_kwh - soc_start_kwh
        balance_ok = total_pv >= total_cons + room_now * 0.5
        if balance_ok:
            grid_hours = sum(1 for r in baseline_sim
                             if r['grid_to_house'] > 0.05 and r['hour'] < pv_reset_hour)
            if grid_hours <= 2:
                logger.info(f"☀️ PV-Skip: {total_pv:.1f} kWh PV ≥ {total_cons:.1f} kWh cons, "
                           f"only {grid_hours}h grid deficit before PV-reset (h{pv_reset_hour}) "
                           f"— no grid charging needed.")
                return [], hourly_charging
            logger.info(f"⚠️ PV bilanziert ({total_pv:.1f}≥{total_cons:.1f} kWh), "
                       f"aber {grid_hours}h Akku am min_soc vor PV-Reset (h{pv_reset_hour}) "
                       f"— Greedy für Arbitrage aktiv.")

        # Greedy iterative scheduling
        max_iterations = 100
        for iteration in range(max_iterations):
            sim = self._simulate_forward_planning(
                soc_start_kwh, hourly_pv, hourly_consumption, hourly_charging,
                min_kwh, max_kwh, max_charge_power, lookahead_hours)

            # v1.3.3: only consider deficits BEFORE the PV-reset horizon
            deficits = [r for r in sim
                        if r['grid_to_house'] > 0.05 and r['hour'] < pv_reset_hour]
            if not deficits:
                logger.debug(f"  Iter {iteration+1}: no deficit before PV-reset (h{pv_reset_hour}), plan converged")
                break

            # Most expensive deficit hour first
            deficits.sort(key=lambda r: -hourly_prices[r['hour']])
            worst = deficits[0]
            worst_h = worst['hour']
            worst_price = hourly_prices[worst_h]

            # Candidates: hours BEFORE worst with battery room, not already maxed,
            # and NO PV surplus (v1.3.3 — hours where pv >= consumption fill the
            # battery on their own; planning a grid charge there forces the inverter
            # to pull from the grid instead of using free PV).
            candidates = []
            for r in sim:
                if r['hour'] >= worst_h:
                    break
                h = r['hour']
                if hourly_pv[h] >= hourly_consumption[h]:
                    continue
                existing = hourly_charging[h]
                if existing >= max_charge_power - 0.05:
                    continue
                if r['room_at_start'] < 0.2:
                    continue
                candidates.append((h, hourly_prices[h], r['room_at_start']))

            if not candidates:
                logger.debug(f"  Iter {iteration+1}: no charging room before deficit @ h{worst_h}")
                break

            candidates.sort(key=lambda x: x[1])
            cheap_h, cheap_p, room = candidates[0]

            # Arbitrage check: only proceed if meaningfully cheaper
            if cheap_p >= worst_price * (1 - min_arbitrage_spread):
                logger.debug(f"  Iter {iteration+1}: spread too small "
                            f"(cheapest {cheap_p*100:.1f} Ct vs deficit "
                            f"{worst_price*100:.1f} Ct), stopping")
                break

            existing = hourly_charging[cheap_h]
            rate_left = max_charge_power - existing
            step = min(worst['grid_to_house'], rate_left, room, 1.5)
            if step < 0.1:
                break
            hourly_charging[cheap_h] += step
            logger.debug(f"  Iter {iteration+1}: +{step:.2f}kWh @ h{cheap_h} "
                        f"({cheap_p*100:.1f}Ct) → save deficit @ h{worst_h} "
                        f"({worst_price*100:.1f}Ct)")

        charging_windows = []
        for h in range(lookahead_hours):
            if hourly_charging[h] > 0.05:
                charging_windows.append({
                    'hour': h,
                    'charge_kwh': hourly_charging[h],
                    'price': hourly_prices[h],
                    'reason': 'Smart bridge-the-night',
                })

        if charging_windows:
            logger.info(f"🌉 Smart grid charge: {len(charging_windows)} windows, "
                       f"total {sum(hourly_charging):.2f} kWh")
        else:
            logger.info(f"☀️ Smart grid charge: no grid charging scheduled (PV sufficient)")

        return charging_windows, hourly_charging

    def _plan_grid_charge_multipeak_fallback(self, current_soc, hourly_pv,
                                                hourly_consumption, hourly_prices,
                                                battery_capacity, min_soc, max_soc,
                                                max_charge_power, lookahead_hours,
                                                baseline_soc, deficit_hours,
                                                min_kwh, max_kwh):
        """Legacy v1.2 multi-peak economic charging — kept as fallback when
        enable_smart_grid_charge=False. Has the well-known PV-blindness bug:
        plans full nightly grid charging even when forecasted PV would refill
        the battery. Use only if you intentionally want the old behavior.
        """
        import math

        charging_windows = []
        hourly_charging = [0.0] * lookahead_hours

        # Step 1: Identify expensive hours (top 40%)
        avg_price = sum(hourly_prices) / len(hourly_prices)
        sorted_prices = sorted(hourly_prices)
        top_40_index = int(len(sorted_prices) * 0.6)
        price_threshold = max(avg_price * 1.05, sorted_prices[top_40_index])

        expensive_hours = []
        for hour in range(lookahead_hours):
            if hourly_prices[hour] >= price_threshold:
                expensive_hours.append({'hour': hour, 'price': hourly_prices[hour]})

        # Step 2: Group into peaks
        peaks = []
        if expensive_hours:
            current_peak = [expensive_hours[0]]
            for i in range(1, len(expensive_hours)):
                if expensive_hours[i]['hour'] - expensive_hours[i-1]['hour'] > 3:
                    peaks.append(current_peak)
                    current_peak = [expensive_hours[i]]
                else:
                    current_peak.append(expensive_hours[i])
            peaks.append(current_peak)

        if peaks and deficit_hours:
            for peak_idx, peak in enumerate(peaks):
                peak_start = peak[0]['hour']
                peak_end = peak[-1]['hour']

                search_start = 0
                if peak_idx > 0:
                    search_start = peaks[peak_idx - 1][-1]['hour'] + 1

                target_lowest_kwh = ((min_soc + 15) / 100) * battery_capacity

                jit_start = None
                for h in range(search_start, peak_start):
                    if h >= lookahead_hours:
                        break
                    if baseline_soc[h] / 100 * battery_capacity < target_lowest_kwh:
                        jit_start = h
                        break
                if jit_start is None:
                    jit_start = max(search_start, peak_start - 4)
                jit_start = min(jit_start, peak_start - 3)
                jit_start = max(search_start, jit_start)

                cumulative_energy = 0
                for h in range(0, jit_start):
                    net = hourly_pv[h] + hourly_charging[h] - hourly_consumption[h]
                    cumulative_energy += net
                soc_at_jit_start_kwh = (current_soc / 100) * battery_capacity + cumulative_energy
                soc_at_jit_start_kwh = max(min_kwh, min(max_kwh, soc_at_jit_start_kwh))

                available_hours = []
                for h in range(jit_start, peak_start):
                    if 0 <= h < lookahead_hours and hourly_charging[h] == 0 \
                            and baseline_soc[h] < max_soc - 2:
                        available_hours.append({'hour': h, 'price': hourly_prices[h]})
                available_hours.sort(key=lambda x: x['price'])

                energy_during_peak = 0
                for h in range(peak_start, peak_end + 1):
                    if h < lookahead_hours:
                        d = hourly_consumption[h] - hourly_pv[h]
                        if d > 0:
                            energy_during_peak += d

                required_charge_kwh = energy_during_peak * 1.5
                window_expanded = False
                for iteration in range(5):
                    temp_alloc = {}
                    rem = required_charge_kwh
                    for slot in available_hours:
                        if rem <= 0:
                            break
                        c = min(rem, max_charge_power)
                        temp_alloc[slot['hour']] = c
                        rem -= c

                    sim_soc_kwh = soc_at_jit_start_kwh
                    lowest_soc_kwh = sim_soc_kwh
                    lowest_soc_hour = jit_start
                    for h in range(jit_start, min(peak_end + 1, lookahead_hours)):
                        c = temp_alloc.get(h, 0)
                        net = hourly_pv[h] + hourly_charging[h] + c - hourly_consumption[h]
                        sim_soc_kwh = max(min_kwh, min(max_kwh, sim_soc_kwh + net))
                        if sim_soc_kwh < lowest_soc_kwh:
                            lowest_soc_kwh = sim_soc_kwh
                            lowest_soc_hour = h

                    if lowest_soc_hour == jit_start and lowest_soc_kwh <= min_kwh + 0.5 \
                            and not window_expanded:
                        expansion_found = False
                        for h in range(jit_start - 1, search_start - 1, -1):
                            if h < 0:
                                break
                            if hourly_charging[h] == 0 and baseline_soc[h] < max_soc - 2:
                                available_hours.append({'hour': h, 'price': hourly_prices[h]})
                                expansion_found = True
                            if len(available_hours) >= 10:
                                break
                        if expansion_found:
                            available_hours.sort(key=lambda x: x['price'])
                            window_expanded = True
                            if available_hours:
                                jit_start = min(h['hour'] for h in available_hours)
                                cumulative_energy = 0
                                for h in range(0, jit_start):
                                    net = hourly_pv[h] + hourly_charging[h] - hourly_consumption[h]
                                    cumulative_energy += net
                                soc_at_jit_start_kwh = (current_soc / 100) * battery_capacity + cumulative_energy
                                soc_at_jit_start_kwh = max(min_kwh, min(max_kwh, soc_at_jit_start_kwh))
                            continue
                        else:
                            window_expanded = True
                            break

                    if lowest_soc_kwh >= target_lowest_kwh - 0.1:
                        break
                    if rem > 0.1 and iteration == 0:
                        for h in range(jit_start - 1, search_start - 1, -1):
                            if h < 0 or rem <= 0:
                                break
                            if hourly_charging[h] == 0 and baseline_soc[h] < max_soc - 2:
                                available_hours.append({'hour': h, 'price': hourly_prices[h]})
                                available_hours.sort(key=lambda x: x['price'])
                    deficit_kwh = target_lowest_kwh - lowest_soc_kwh
                    required_charge_kwh += deficit_kwh * 1.1

                if required_charge_kwh > 0.5 and available_hours:
                    rem = required_charge_kwh
                    initial = required_charge_kwh
                    for slot in available_hours:
                        if rem <= 0.1:
                            break
                        h = slot['hour']
                        if hourly_pv[h] > 2.5 and abs(h - peak_start) <= 4:
                            continue
                        charged_so_far = initial - rem
                        if slot['price'] * 100 > 28.5 and charged_so_far > initial * 0.6:
                            break
                        c = min(rem, max_charge_power)
                        hourly_charging[h] = c
                        rem -= c
                        charging_windows.append({
                            'hour': h, 'charge_kwh': c, 'price': slot['price'],
                            'reason': f'Multi-peak {peak_idx+1} (legacy)'
                        })

        elif deficit_hours:
            first_deficit = deficit_hours[0]['hour']
            max_cum_def = 0
            cum = 0
            for hour in range(first_deficit, lookahead_hours):
                cum += hourly_pv[hour] - hourly_consumption[hour]
                soc_h_kwh = (current_soc / 100) * battery_capacity + cum
                target_kwh = ((min_soc + 10) / 100) * battery_capacity
                max_cum_def = max(max_cum_def, max(0, target_kwh - soc_h_kwh))

            available = sorted([{'hour': h, 'price': hourly_prices[h]}
                                for h in range(0, first_deficit)],
                               key=lambda x: x['price'])
            rem = max_cum_def
            for slot in available:
                if rem <= 0:
                    break
                c = min(rem, max_charge_power)
                hourly_charging[slot['hour']] = c
                rem -= c
                charging_windows.append({
                    'hour': slot['hour'], 'charge_kwh': c, 'price': slot['price'],
                    'reason': f'Prevent deficit at h{first_deficit} (legacy)'
                })

        return charging_windows, hourly_charging

    def plan_daily_battery_schedule(self,
                                    ha_client,
                                    config,
                                    current_soc: float,
                                    prices: List[Dict]) -> Dict:
        """
        Plans 48-hour battery schedule using predictive optimization (v1.1.0 - extended to 2 days)

        Simulates 48 hours (today + tomorrow) hour-by-hour with consumption, PV, and prices.
        Identifies deficits and schedules charging at cheapest times BEFORE deficits.

        Args:
            ha_client: Home Assistant client for sensor data
            config: Configuration dict
            current_soc: Current battery SOC (%)
            prices: List of Tibber price data with datetime and total price (today + tomorrow)

        Returns:
            dict: {
                'hourly_soc': [float],  # Projected SOC for each hour (0-47: today=0-23, tomorrow=24-47)
                'hourly_charging': [float],  # Planned grid charging kWh per hour
                'hourly_pv': [float],  # PV production per hour
                'hourly_consumption': [float],  # Consumption per hour
                'charging_windows': [dict],  # Detailed charging plan
                'last_planned': str  # ISO timestamp of planning
            }
        """
        if not self.consumption_learner:
            logger.warning("No consumption learner available for daily planning")
            return None

        try:
            now = datetime.now().astimezone()
            today = now.date()
            current_hour = now.hour

            # Get battery parameters (v1.2.1 - Explicit type conversion!)
            battery_capacity = float(config.get('battery_capacity', 10.6))  # kWh
            min_soc = int(config.get('auto_safety_soc', 20))  # %
            max_soc = int(config.get('auto_charge_below_soc', 95))  # %
            max_charge_power = float(config.get('max_charge_power', 3900)) / 1000  # kW

            # 1. Collect hourly data for 48 hours (today + tomorrow)
            tomorrow = today + timedelta(days=1)
            hourly_consumption = []
            hourly_pv = []
            hourly_prices = []

            # Get PV forecast for 48 hours (today + tomorrow)
            pv_forecast = self.get_hourly_pv_forecast(ha_client, config, include_tomorrow=True)

            # Build hourly data arrays for 48 hours
            for hour in range(48):
                # Determine actual date and hour for this iteration
                if hour < 24:
                    # Today (hours 0-23)
                    actual_date = today
                    actual_hour = hour
                else:
                    # Tomorrow (hours 24-47)
                    actual_date = tomorrow
                    actual_hour = hour - 24

                # Consumption forecast (weekday-aware)
                consumption = self.consumption_learner.get_average_consumption(actual_hour, target_date=actual_date)
                hourly_consumption.append(consumption)

                # PV forecast (already has correct indexing: 0-23=today, 24-47=tomorrow)
                pv = pv_forecast.get(hour, 0.0)
                hourly_pv.append(pv)

                # Price (find matching hour from Tibber data)
                price = 0.30  # Default fallback
                for p in prices:
                    # Tibber uses 'startsAt' key, not 'datetime'
                    start_time = p.get('startsAt', '')
                    if start_time:
                        try:
                            price_dt = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
                            price_dt = price_dt.astimezone()  # Convert to local timezone
                            if price_dt.hour == actual_hour and price_dt.date() == actual_date:
                                price = p.get('total', 0.30)
                                break
                        except Exception:
                            continue
                hourly_prices.append(price)

            logger.info(f"Planning 48h battery schedule for {today} and {tomorrow}")
            logger.info(f"Current: hour {current_hour}, SOC {current_soc:.1f}%")
            logger.debug(f"Hourly consumption (48h): {[f'{c:.2f}' for c in hourly_consumption]}")
            logger.debug(f"Hourly PV (48h): {[f'{p:.2f}' for p in hourly_pv]}")

            # 2. Simulate SOC without any grid charging (baseline) - 48 hours
            # For past hours: estimate backwards from current SOC
            # For current + future hours: simulate forward from current SOC
            baseline_soc = [0.0] * 48

            # v1.1.1: Estimate SOC for past hours (0 to current_hour-1)
            # Simple approach: Assume SOC was similar to current level, adjust slightly for time of day
            # This is just for visualization - past hours don't affect charging decisions
            soc_at_midnight_kwh = (current_soc / 100) * battery_capacity

            # Try to back-calculate, but clamp aggressively to avoid unrealistic values
            for h in range(0, current_hour):
                soc_at_midnight_kwh -= (hourly_pv[h] - hourly_consumption[h])

            # Clamp to battery limits
            max_kwh = (max_soc / 100) * battery_capacity
            min_kwh = (min_soc / 100) * battery_capacity
            soc_at_midnight_kwh = max(min_kwh, min(max_kwh, soc_at_midnight_kwh))

            # If estimated midnight SOC is way off current SOC, use simpler estimate
            # (Happens when there's been massive PV generation or grid charging)
            estimated_midnight_pct = (soc_at_midnight_kwh / battery_capacity) * 100
            if abs(estimated_midnight_pct - current_soc) > 50:
                # Fallback: Assume gradual change from 70% at midnight to current SOC
                logger.debug(f"Large SOC deviation detected (midnight est: {estimated_midnight_pct:.1f}%, current: {current_soc:.1f}%), using simpler estimate")
                soc_at_midnight_kwh = (70 / 100) * battery_capacity  # Typical overnight value

            # Simulate PAST hours (0 to current_hour-1) from midnight estimate
            soc_kwh = soc_at_midnight_kwh
            for hour in range(current_hour):
                baseline_soc[hour] = (soc_kwh / battery_capacity) * 100
                net_energy = hourly_pv[hour] - hourly_consumption[hour]
                soc_kwh += net_energy
                soc_kwh = max(min_kwh, min(max_kwh, soc_kwh))

            # CURRENT hour: Use actual current SOC!
            baseline_soc[current_hour] = current_soc
            soc_kwh = (current_soc / 100) * battery_capacity
            logger.info(f"🔵 DEBUG baseline_soc: current_hour={current_hour}, setting baseline_soc[{current_hour}]={current_soc:.1f}%")

            # Simulate FUTURE hours (current_hour+1 to 47) from current SOC
            # Same logic as past hours: store SOC at START of hour, then add energy from that hour
            for hour in range(current_hour + 1, 48):
                # First add energy from PREVIOUS hour to get to START of this hour
                net_energy = hourly_pv[hour - 1] - hourly_consumption[hour - 1]
                soc_kwh += net_energy
                soc_kwh = max(min_kwh, min(max_kwh, soc_kwh))
                # Store SOC at START of this hour
                baseline_soc[hour] = (soc_kwh / battery_capacity) * 100

                # Debug: Show critical hours
                if hour in [9, 10, 11, 12, 15, 18, 21, 23]:
                    logger.info(f"  📊 Hour {hour}: SOC={baseline_soc[hour]:.1f}%, PV={hourly_pv[hour-1]:.2f}kWh, Cons={hourly_consumption[hour-1]:.2f}kWh, Net={net_energy:.2f}kWh")

            # 3. Identify deficit hours (where SOC falls below minimum) - 48 hours
            deficit_hours = []
            for hour in range(current_hour, 48):
                if baseline_soc[hour] <= min_soc + 5:  # 5% buffer
                    # Calculate how much energy is missing
                    current_kwh = (baseline_soc[hour] / 100) * battery_capacity
                    target_kwh = ((min_soc + 10) / 100) * battery_capacity  # Charge to min + 10%
                    deficit_kwh = target_kwh - current_kwh

                    deficit_hours.append({
                        'hour': hour,
                        'soc': baseline_soc[hour],
                        'deficit_kwh': max(0, deficit_kwh)
                    })

            logger.info(f"Found {len(deficit_hours)} deficit hours: {[d['hour'] for d in deficit_hours]}")

            # Debug: Show why we have deficits
            if deficit_hours:
                logger.info(f"📉 First deficit at hour {deficit_hours[0]['hour']}: SOC={deficit_hours[0]['soc']:.1f}%, needs {deficit_hours[0]['deficit_kwh']:.2f} kWh")
                if len(deficit_hours) > 1:
                    logger.info(f"📉 Deficit hours today: {[d['hour'] for d in deficit_hours if d['hour'] < 24]}")
                    logger.info(f"📉 Deficit hours tomorrow: {[d['hour'] for d in deficit_hours if d['hour'] >= 24]}")

            # 4. Plan charging windows (cheapest hours BEFORE deficits) - 48 hours
            charging_windows = []
            hourly_charging = [0.0] * 48

            for deficit in deficit_hours:
                deficit_hour = deficit['hour']
                needed_kwh = deficit['deficit_kwh']

                if needed_kwh < 0.5:
                    continue  # Skip small deficits

                # Find available hours before deficit
                available_hours = []
                for h in range(current_hour, deficit_hour):
                    if hourly_charging[h] == 0:  # Not already planned
                        available_hours.append({
                            'hour': h,
                            'price': hourly_prices[h]
                        })

                # Sort by price (cheapest first)
                available_hours.sort(key=lambda x: x['price'])

                # Allocate charging to cheapest hours
                remaining_kwh = needed_kwh
                for slot in available_hours:
                    if remaining_kwh <= 0:
                        break

                    hour = slot['hour']
                    # Maximum charge per hour (1 hour at max power)
                    max_charge_per_hour = max_charge_power  # kWh (kW * 1h)
                    charge_kwh = min(remaining_kwh, max_charge_per_hour)

                    hourly_charging[hour] = charge_kwh
                    remaining_kwh -= charge_kwh

                    charging_windows.append({
                        'hour': hour,
                        'charge_kwh': charge_kwh,
                        'price': slot['price'],
                        'reason': f'Prepare for deficit at {deficit_hour}:00'
                    })

            logger.info(f"Planned {len(charging_windows)} deficit-based charging windows")

            # 4b. ECONOMIC OPTIMIZATION: Opportunistic charging at cheap prices (v1.0.9) - 48 hours
            # Only charge if economically beneficial AND battery won't be filled by PV anyway
            # v1.1.1: Start from NEXT hour, not current hour (current hour is already partially over)
            economic_threshold = 1.10  # Minimum 10% cost saving required
            negative_price_threshold = 0.0  # Charge if price <= 0 (we get paid!)

            for hour in range(current_hour + 1, 48):
                # Skip if already charging in this hour
                if hourly_charging[hour] > 0:
                    continue

                # Simulate SOC at this hour with PV but WITHOUT new grid charging
                temp_soc_kwh = (current_soc / 100) * battery_capacity
                for h in range(current_hour, hour + 1):
                    if h < current_hour:
                        continue
                    # Include only: PV + existing planned charging - consumption
                    net = hourly_pv[h] + hourly_charging[h] - hourly_consumption[h]
                    temp_soc_kwh += net
                    temp_soc_kwh = max(0, min((max_soc / 100) * battery_capacity, temp_soc_kwh))

                soc_at_hour = (temp_soc_kwh / battery_capacity) * 100

                # Check available battery space (considering PV will fill it)
                available_space_kwh = ((max_soc - soc_at_hour) / 100) * battery_capacity

                # Skip if battery will be nearly full anyway
                if available_space_kwh < 0.5:
                    continue

                # Special case: NEGATIVE PRICES - always charge (we get paid!)
                if hourly_prices[hour] <= negative_price_threshold:
                    charge_kwh = min(available_space_kwh, max_charge_power)
                    hourly_charging[hour] = charge_kwh
                    charging_windows.append({
                        'hour': hour,
                        'charge_kwh': charge_kwh,
                        'price': hourly_prices[hour],
                        'reason': f'NEGATIVE PRICE: {hourly_prices[hour]*100:.2f} Ct/kWh (we get paid to charge!)'
                    })
                    logger.info(f"💰 Negative price at {hour}:00: Charging {charge_kwh:.2f} kWh @ {hourly_prices[hour]*100:.2f} Ct/kWh")
                    continue

                # Find future expensive hours where this stored energy would be used
                future_expensive_hours = []
                for future_h in range(hour + 1, 48):
                    if hourly_prices[future_h] > hourly_prices[hour] * economic_threshold:
                        future_expensive_hours.append(future_h)

                # Skip if no future expensive hours
                if not future_expensive_hours:
                    continue

                # Calculate average expensive price
                avg_expensive_price = sum(hourly_prices[h] for h in future_expensive_hours) / len(future_expensive_hours)

                # Economic decision: Is it worth charging now?
                cost_per_kwh = hourly_prices[hour]
                benefit_per_kwh = avg_expensive_price

                if benefit_per_kwh > cost_per_kwh * economic_threshold:
                    # Economically beneficial! But calculate optimal amount, not maximum

                    # Step 1: Find target hour (when to stop charging for)
                    # Either: PV becomes sufficient, cheaper hour arrives, or expensive hours begin
                    target_hour = 48

                    # Check if a cheaper hour is coming
                    for future_h in range(hour + 1, 48):
                        if hourly_prices[future_h] < cost_per_kwh * 0.98:  # 2% cheaper
                            target_hour = future_h
                            logger.debug(f"Found cheaper hour at {future_h}:00, stopping calculation there")
                            break

                    # Check when PV becomes sufficient (covers 80%+ of consumption)
                    for future_h in range(hour + 1, min(target_hour, 48)):
                        if hourly_pv[future_h] >= hourly_consumption[future_h] * 0.8:
                            target_hour = future_h
                            logger.debug(f"PV becomes sufficient at {future_h}:00, stopping calculation there")
                            break

                    # Limit to first expensive hour as maximum target
                    if future_expensive_hours:
                        target_hour = min(target_hour, max(future_expensive_hours))

                    # Step 2: Calculate energy deficit from charge hour to target hour
                    # Simulate forward from 'hour' to 'target_hour' WITHOUT this charge
                    deficit_kwh = 0
                    sim_soc_kwh = temp_soc_kwh  # Start with SOC at charge hour (already calculated above)

                    for h in range(hour, target_hour):
                        net = hourly_pv[h] + hourly_charging[h] - hourly_consumption[h]
                        sim_soc_kwh += net

                        # If SOC drops below minimum, we need to cover that deficit
                        min_kwh = (min_soc / 100) * battery_capacity
                        if sim_soc_kwh < min_kwh:
                            deficit_kwh += (min_kwh - sim_soc_kwh)
                            sim_soc_kwh = min_kwh

                        # Cap at max SOC
                        sim_soc_kwh = min((max_soc / 100) * battery_capacity, sim_soc_kwh)

                    # Step 3: Charge optimal amount (deficit + small buffer, minimum 1 kWh)
                    # If no deficit, still charge a reasonable amount for the economic opportunity
                    if deficit_kwh < 0.5:
                        # No immediate deficit, but economically beneficial
                        # Charge enough for the expensive hours (estimate: consumption during expensive hours)
                        estimated_usage = sum(hourly_consumption[h] - hourly_pv[h]
                                            for h in future_expensive_hours
                                            if hourly_consumption[h] > hourly_pv[h])
                        optimal_charge = max(1.0, min(estimated_usage * 0.5, max_charge_power))
                    else:
                        # Charge deficit + 15% buffer
                        optimal_charge = deficit_kwh * 1.15

                    charge_kwh = min(optimal_charge, available_space_kwh, max_charge_power)

                    hourly_charging[hour] = charge_kwh
                    charging_windows.append({
                        'hour': hour,
                        'charge_kwh': charge_kwh,
                        'price': cost_per_kwh,
                        'reason': f'Economic: Buy @ {cost_per_kwh*100:.2f} Ct/kWh, avoid @ {benefit_per_kwh*100:.2f} Ct/kWh later (until h{target_hour})'
                    })

                    logger.info(f"💡 Economic charging at {hour}:00: {charge_kwh:.2f} kWh @ {cost_per_kwh*100:.2f} Ct/kWh "
                              f"(saves {(benefit_per_kwh - cost_per_kwh)*100:.2f} Ct/kWh, bridges until {target_hour}:00)")

            logger.info(f"Total charging windows planned: {len(charging_windows)} "
                       f"({len([w for w in charging_windows if 'deficit' in w['reason'].lower()])} deficit, "
                       f"{len([w for w in charging_windows if 'Economic' in w['reason']])} economic)")

            # 5. Re-simulate SOC with planned charging - 48 hours
            # Use actual current SOC as anchor point
            final_soc = [0.0] * 48

            # Estimate SOC at midnight (for past hours visualization)
            soc_at_midnight_kwh = (current_soc / 100) * battery_capacity
            for h in range(0, current_hour):
                soc_at_midnight_kwh -= (hourly_pv[h] + hourly_charging[h] - hourly_consumption[h])
            soc_at_midnight_kwh = max(min_kwh, min(max_kwh, soc_at_midnight_kwh))

            # Sanity check: If estimated midnight SOC is way off, use simpler estimate
            estimated_midnight_pct = (soc_at_midnight_kwh / battery_capacity) * 100
            if abs(estimated_midnight_pct - current_soc) > 50:
                logger.debug(f"Large SOC deviation in final_soc (midnight est: {estimated_midnight_pct:.1f}%, current: {current_soc:.1f}%), using simpler estimate")
                soc_at_midnight_kwh = (70 / 100) * battery_capacity

            # Simulate PAST hours (0 to current_hour-1)
            soc_kwh = soc_at_midnight_kwh
            for hour in range(current_hour):
                final_soc[hour] = (soc_kwh / battery_capacity) * 100
                net_energy = hourly_pv[hour] + hourly_charging[hour] - hourly_consumption[hour]
                soc_kwh += net_energy
                soc_kwh = max(min_kwh, min(max_kwh, soc_kwh))

            # CURRENT hour: Use actual current SOC!
            final_soc[current_hour] = current_soc
            soc_kwh = (current_soc / 100) * battery_capacity
            logger.info(f"🟢 DEBUG final_soc: current_hour={current_hour}, setting final_soc[{current_hour}]={current_soc:.1f}%")

            # Simulate FUTURE hours (current_hour+1 to 47) with planned charging
            for hour in range(current_hour + 1, 48):
                # Apply energy changes from PREVIOUS hour
                net_energy = hourly_pv[hour - 1] + hourly_charging[hour - 1] - hourly_consumption[hour - 1]
                soc_kwh += net_energy
                soc_kwh = max(min_kwh, min(max_kwh, soc_kwh))
                final_soc[hour] = (soc_kwh / battery_capacity) * 100

            # 6. Return comprehensive 48-hour plan
            plan = {
                'hourly_soc': final_soc,
                'hourly_charging': hourly_charging,
                'hourly_pv': hourly_pv,
                'hourly_consumption': hourly_consumption,
                'hourly_prices': hourly_prices,
                'charging_windows': charging_windows,
                'last_planned': now.isoformat(),
                'total_charging_kwh': sum(hourly_charging),
                'min_soc_reached': min(final_soc[current_hour:]) if current_hour < 48 else current_soc
            }

            logger.info(f"48h plan complete: {len(charging_windows)} charge windows, "
                       f"total {plan['total_charging_kwh']:.2f} kWh, "
                       f"min SOC {plan['min_soc_reached']:.1f}%")

            # Debug: Show SOC values around current hour
            if current_hour > 0 and current_hour < 47:
                logger.info(f"🔍 SOC values: hour {current_hour-1}={final_soc[current_hour-1]:.1f}%, "
                          f"hour {current_hour}={final_soc[current_hour]:.1f}%, "
                          f"hour {current_hour+1}={final_soc[current_hour+1]:.1f}%")
            elif current_hour == 0:
                logger.info(f"🔍 SOC values: hour {current_hour}={final_soc[current_hour]:.1f}%, "
                          f"hour {current_hour+1}={final_soc[current_hour+1]:.1f}%")
            else:  # current_hour == 47
                logger.info(f"🔍 SOC values: hour {current_hour-1}={final_soc[current_hour-1]:.1f}%, "
                          f"hour {current_hour}={final_soc[current_hour]:.1f}%")

            return plan

        except Exception as e:
            logger.error(f"Error planning daily battery schedule: {e}", exc_info=True)
            return None

    def predict_short_term_deficit(self,
                                   ha_client,
                                   config,
                                   lookahead_hours: int = 3) -> Tuple[bool, float, str]:
        """
        Predicts short-term energy deficit using hourly PV forecast (v0.8.1)

        Uses granular hourly forecast from forecast.solar instead of broad daily check.
        More intelligent than the old 6:00-18:00 approach.

        Args:
            ha_client: Home Assistant client for fetching sensor data
            config: Configuration dict with sensor names
            lookahead_hours: How many hours to look ahead (default: 3)

        Returns:
            (has_deficit: bool, deficit_kwh: float, reason: str)
        """
        if not self.consumption_learner:
            logger.warning("No consumption learner available, using fallback")
            return False, 0.0, "No consumption learning data"

        try:
            now = datetime.now().astimezone()
            current_hour = now.hour

            # Get hourly PV forecast
            pv_forecast = self.get_hourly_pv_forecast(ha_client, config)

            if not pv_forecast:
                logger.warning("No PV forecast available, cannot predict deficit")
                return False, 0.0, "No PV forecast data"

            # Calculate consumption and PV production for next N hours
            total_consumption = 0.0
            total_pv = 0.0

            for i in range(lookahead_hours):
                future_hour = (current_hour + i) % 24
                future_date = (now + timedelta(hours=i)).date()

                # Get predicted consumption for this hour
                hour_consumption = self.consumption_learner.get_average_consumption(
                    future_hour,
                    target_date=future_date
                )
                total_consumption += hour_consumption

                # Get PV forecast for this hour
                hour_pv = pv_forecast.get(future_hour, 0.0)
                total_pv += hour_pv

                logger.debug(f"Hour {future_hour}: Consumption={hour_consumption:.2f} kWh, "
                           f"PV={hour_pv:.2f} kWh")

            # Calculate deficit
            deficit = total_consumption - total_pv
            has_deficit = deficit > 0.5  # At least 0.5 kWh gap

            reason = (f"Next {lookahead_hours}h: Consumption={total_consumption:.1f} kWh, "
                     f"PV={total_pv:.1f} kWh, Deficit={deficit:.1f} kWh")

            logger.info(f"Short-term deficit check: {reason}")

            return has_deficit, max(0, deficit), reason

        except Exception as e:
            logger.error(f"Error predicting short-term deficit: {e}")
            return False, 0.0, f"Error: {e}"

    def predict_energy_deficit(self,
                              pv_remaining: float,
                              current_hour: int = None) -> tuple[bool, float]:
        """
        Predicts if there will be an energy deficit based on consumption learning.

        DEPRECATED: Use predict_short_term_deficit() for more granular forecasting (v0.8.1)

        Args:
            pv_remaining: Expected PV production remaining today (kWh)
            current_hour: Current hour (0-23), defaults to now

        Returns:
            (has_deficit: bool, deficit_kwh: float)
        """
        if not self.consumption_learner:
            # Fallback to simple threshold
            return pv_remaining < 5, max(0, 5 - pv_remaining)

        try:
            if current_hour is None:
                current_hour = datetime.now().astimezone().hour

            # Predict consumption until evening (18:00)
            # This covers the critical morning period before PV ramps up
            target_hour = 18
            if current_hour >= target_hour:
                target_hour = 23  # Rest of day

            predicted_consumption = self.consumption_learner.predict_consumption_until(target_hour)

            # Simple deficit: consumption > PV remaining
            deficit = predicted_consumption - pv_remaining
            has_deficit = deficit > 0.5  # At least 0.5 kWh gap

            logger.debug(f"Energy balance: PV={pv_remaining:.1f} kWh, "
                        f"Consumption={predicted_consumption:.1f} kWh, "
                        f"Deficit={deficit:.1f} kWh")

            return has_deficit, max(0, deficit)

        except Exception as e:
            logger.error(f"Error predicting energy deficit: {e}")
            # Fallback
            return pv_remaining < 5, max(0, 5 - pv_remaining)

    # v1.1.0 - REMOVED: Old should_charge_now() method
    # Now using plan_daily_battery_schedule() with 48h windows exclusively