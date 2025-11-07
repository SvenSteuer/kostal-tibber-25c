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
        self.threshold_1h = config.get('tibber_price_threshold_1h', 8) / 100
        self.threshold_3h = config.get('tibber_price_threshold_3h', 8) / 100
        self.charge_duration_per_10 = config.get('charge_duration_per_10_percent', 18)
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
        if (self.forecast_solar_api and
            config.get('enable_forecast_solar_api', False)):

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
        Plans battery schedule using simple rolling window (v1.2.0 - REWRITE)

        Simple 5-step approach:
        1. Forecast consumption for next 24h (from NOW)
        2. Forecast PV production for next 24h (from NOW)
        3. Find deficit hours (where battery + PV don't cover consumption)
        4. Find optimal charging times (cheapest hours BEFORE deficits)
        5. Calculate final SOC trajectory

        NO backward estimation, NO complex past/present/future logic.
        Rolling window: Always X hours from NOW, not calendar days.

        Args:
            ha_client: Home Assistant client
            config: Configuration dict
            current_soc: Current battery SOC (%)
            prices: List of Tibber prices (today + tomorrow)
            lookahead_hours: How many hours to look ahead (default: 24)

        Returns:
            dict: {
                'hourly_soc': List[float],  # SOC at start of each hour (0=now, 1=now+1h, ...)
                'hourly_charging': List[float],  # Planned charging kWh per hour
                'hourly_pv': List[float],  # PV forecast per hour
                'hourly_consumption': List[float],  # Consumption forecast per hour
                'hourly_prices': List[float],  # Prices per hour
                'charging_windows': List[dict],  # Charging schedule details
                'start_time': str,  # When this plan starts (ISO format)
                'last_planned': str  # When this plan was created (ISO format)
            }
        """
        if not self.consumption_learner:
            logger.warning("No consumption learner available")
            return None

        try:
            now = datetime.now().astimezone()
            current_hour_in_day = now.hour

            # Battery parameters
            battery_capacity = config.get('battery_capacity', 10.6)  # kWh
            min_soc = int(config.get('auto_safety_soc', 20))  # %
            max_soc = int(config.get('auto_charge_below_soc', 95))  # %
            max_charge_power = config.get('max_charge_power', 3900) / 1000  # kW → kWh/h

            logger.info(f"Planning {lookahead_hours}h rolling schedule starting from {now.strftime('%H:%M')}, SOC={current_soc:.1f}%")

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

            logger.info(f"Forecasts ready: Avg consumption={sum(hourly_consumption)/len(hourly_consumption):.2f}kWh, "
                       f"Avg PV={sum(hourly_pv)/len(hourly_pv):.2f}kWh, "
                       f"Avg price={sum(hourly_prices)/len(hourly_prices)*100:.1f}Ct")

            # DEBUG: Show first 12 hours in detail
            logger.info("📊 First 12 hours forecast:")
            for i in range(min(12, lookahead_hours)):
                logger.info(f"  Hour {i:2d}: PV={hourly_pv[i]:6.2f}kWh, Cons={hourly_consumption[i]:6.2f}kWh, "
                          f"Net={hourly_pv[i]-hourly_consumption[i]:+6.2f}kWh, Price={hourly_prices[i]*100:5.1f}Ct")

            # =================================================================
            # STEP 3: Simulate SOC WITHOUT charging to find deficits
            # =================================================================
            baseline_soc = [0.0] * lookahead_hours
            baseline_soc[0] = current_soc

            min_kwh = (min_soc / 100) * battery_capacity
            max_kwh = (max_soc / 100) * battery_capacity
            soc_kwh = (current_soc / 100) * battery_capacity

            logger.info(f"🔋 Starting baseline SOC simulation from {current_soc:.1f}% ({soc_kwh:.2f} kWh)")
            logger.info(f"   Battery limits: min={min_soc}% ({min_kwh:.2f} kWh), max={max_soc}% ({max_kwh:.2f} kWh)")

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
                    logger.info(f"   Hour {hour}: SOC {soc_before_pct:.1f}% → {baseline_soc[hour]:.1f}% "
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
            # STEP 4: Find optimal charging times (v1.2.0-beta.30: Economic charging)
            # =================================================================
            charging_windows = []
            hourly_charging = [0.0] * lookahead_hours

            # NEW STRATEGY v1.2.0-beta.31: Multi-Peak Economic Charging
            # Goal: Identify MULTIPLE price peaks and charge optimally for EACH peak
            # - Lower threshold to catch more peaks (top 40% instead of 30%)
            # - Group peaks into clusters (gaps > 3h = separate peaks)
            # - For each peak: charge ONLY what's needed, not always to max_soc

            # Step 1: Identify expensive hours with LOWER threshold
            avg_price = sum(hourly_prices) / len(hourly_prices)
            sorted_prices = sorted(hourly_prices)
            top_40_index = int(len(sorted_prices) * 0.6)  # Top 40% (was 30%)
            price_threshold = max(avg_price * 1.05, sorted_prices[top_40_index])  # 5% over avg (was 10%)

            expensive_hours = []
            for hour in range(lookahead_hours):
                if hourly_prices[hour] >= price_threshold:
                    expensive_hours.append({
                        'hour': hour,
                        'price': hourly_prices[hour]
                    })

            logger.info(f"💰 Price Analysis (v1.2.0-beta.31 Multi-Peak):")
            logger.info(f"  Average price: {avg_price*100:.1f} Ct/kWh")
            logger.info(f"  Expensive threshold (top 40%): {price_threshold*100:.1f} Ct/kWh")
            logger.info(f"  Expensive hours: {[e['hour'] for e in expensive_hours]}")

            # Step 2: Group expensive hours into PEAKS (clusters)
            # If gap between expensive hours > 3h, it's a separate peak
            peaks = []
            if expensive_hours:
                current_peak = [expensive_hours[0]]
                for i in range(1, len(expensive_hours)):
                    if expensive_hours[i]['hour'] - expensive_hours[i-1]['hour'] > 3:
                        # Gap > 3h → new peak
                        peaks.append(current_peak)
                        current_peak = [expensive_hours[i]]
                    else:
                        current_peak.append(expensive_hours[i])
                peaks.append(current_peak)  # Add last peak

                logger.info(f"📊 Found {len(peaks)} price peak(s):")
                for idx, peak in enumerate(peaks):
                    peak_hours = [p['hour'] for p in peak]
                    peak_prices = [f"{p['price']*100:.1f}" for p in peak]
                    logger.info(f"  Peak {idx+1}: Hours {peak_hours[0]}-{peak_hours[-1]}, "
                              f"Prices {peak_prices} Ct/kWh")

            if peaks and deficit_hours:
                # Step 3: Plan charging for EACH peak separately
                import math

                for peak_idx, peak in enumerate(peaks):
                    peak_start = peak[0]['hour']
                    peak_end = peak[-1]['hour']
                    peak_prices_list = [f"{p['price']*100:.1f}" for p in peak]

                    logger.info(f"")
                    logger.info(f"🎯 Planning for Peak {peak_idx+1}: Hours {peak_start}-{peak_end}")
                    logger.info(f"   Peak prices: {peak_prices_list} Ct/kWh")

                    # v1.2.0-beta.34: ITERATIVE simulation with DYNAMIC JIT window
                    # Problem in beta.34: JIT window too late - SOC already at min at JIT-start!
                    # Solution: Find where SOC drops below target, start JIT there!

                    # Step 1: Find where SOC drops below target in baseline simulation
                    search_start = 0
                    if peak_idx > 0:
                        search_start = peaks[peak_idx - 1][-1]['hour'] + 1

                    target_lowest_kwh = ((min_soc + 15) / 100) * battery_capacity

                    # Find first hour where baseline SOC < target
                    jit_start = None
                    for h in range(search_start, peak_start):
                        if h >= lookahead_hours:
                            break
                        baseline_soc_kwh = baseline_soc[h] / 100 * battery_capacity
                        if baseline_soc_kwh < target_lowest_kwh:
                            jit_start = h
                            break

                    # If no low point found, use default 4h window
                    if jit_start is None:
                        jit_start = max(search_start, peak_start - 4)

                    # Ensure JIT window is large enough (at least 3h before peak)
                    jit_start = min(jit_start, peak_start - 3)
                    jit_start = max(search_start, jit_start)
                    jit_end = peak_start - 1

                    # Step 2: Calculate SOC at JIT-START
                    cumulative_energy = 0
                    for h in range(0, jit_start):
                        net = hourly_pv[h] + hourly_charging[h] - hourly_consumption[h]
                        cumulative_energy += net

                    soc_at_jit_start_kwh = (current_soc / 100) * battery_capacity + cumulative_energy
                    soc_at_jit_start_kwh = max(min_kwh, min(max_kwh, soc_at_jit_start_kwh))

                    logger.info(f"   ⏰ Just-in-Time window: Hours {jit_start}-{jit_end} (SOC drops below target at hour {jit_start})")

                    # Step 3: Find available hours in JIT window
                    available_hours = []
                    for h in range(jit_start, peak_start):
                        if h < 0 or h >= lookahead_hours:
                            continue
                        if hourly_charging[h] == 0 and baseline_soc[h] < max_soc - 2:
                            available_hours.append({
                                'hour': h,
                                'price': hourly_prices[h]
                            })

                    # Sort by price (cheapest first)
                    available_hours.sort(key=lambda x: x['price'])

                    # Step 4: Calculate energy needed during peak (initial estimate)
                    energy_during_peak = 0
                    for h in range(peak_start, peak_end + 1):
                        if h < lookahead_hours:
                            net_deficit = hourly_consumption[h] - hourly_pv[h]
                            if net_deficit > 0:
                                energy_during_peak += net_deficit

                    logger.info(f"   Energy during peak: {energy_during_peak:.2f} kWh")
                    logger.info(f"   SOC at JIT-start (hour {jit_start}): {(soc_at_jit_start_kwh/battery_capacity)*100:.1f}%")

                    # Start with reasonable estimate: peak energy + 50% buffer
                    required_charge_kwh = energy_during_peak * 1.5

                    # Step 5: ITERATIVE SIMULATION to find optimal charge
                    max_iterations = 5
                    window_expanded = False

                    for iteration in range(max_iterations):
                        # Allocate charge in cheapest hours (temporary)
                        temp_charge_allocation = {}
                        remaining_kwh = required_charge_kwh

                        for slot in available_hours:
                            if remaining_kwh <= 0:
                                break
                            hour = slot['hour']
                            charge_this_hour = min(remaining_kwh, max_charge_power)
                            temp_charge_allocation[hour] = charge_this_hour
                            remaining_kwh -= charge_this_hour

                        # Simulate from JIT-start to peak-end WITH this charging plan
                        sim_soc_kwh = soc_at_jit_start_kwh
                        lowest_soc_kwh = sim_soc_kwh
                        lowest_soc_hour = jit_start

                        for h in range(jit_start, min(peak_end + 1, lookahead_hours)):
                            charge_this_hour = temp_charge_allocation.get(h, 0)
                            # Include: already planned charging, hypothetical new charging, PV, consumption
                            net = hourly_pv[h] + hourly_charging[h] + charge_this_hour - hourly_consumption[h]
                            sim_soc_kwh += net
                            sim_soc_kwh = max(min_kwh, min(max_kwh, sim_soc_kwh))

                            if sim_soc_kwh < lowest_soc_kwh:
                                lowest_soc_kwh = sim_soc_kwh
                                lowest_soc_hour = h

                        logger.info(f"   🔄 Iteration {iteration+1}: Charge {required_charge_kwh:.2f} kWh → Lowest SOC {(lowest_soc_kwh/battery_capacity)*100:.1f}% at hour {lowest_soc_hour}")

                        # Check if lowest point is AT JIT-start - window too late!
                        if lowest_soc_hour == jit_start and lowest_soc_kwh <= min_kwh + 0.5 and not window_expanded:
                            logger.info(f"   ⚠️ Lowest at JIT-start! Expanding window earlier...")
                            # Expand window to include earlier hours
                            expansion_found = False
                            for h in range(jit_start - 1, search_start - 1, -1):
                                if h < 0:
                                    break
                                if hourly_charging[h] == 0 and baseline_soc[h] < max_soc - 2:
                                    available_hours.append({'hour': h, 'price': hourly_prices[h]})
                                    expansion_found = True
                                if len(available_hours) >= 10:  # Enough hours
                                    break

                            if expansion_found:
                                available_hours.sort(key=lambda x: x['price'])
                                window_expanded = True
                                # Recalculate JIT-start
                                if available_hours:
                                    jit_start = min(h['hour'] for h in available_hours)
                                    # Recalculate SOC at new JIT-start
                                    cumulative_energy = 0
                                    for h in range(0, jit_start):
                                        net = hourly_pv[h] + hourly_charging[h] - hourly_consumption[h]
                                        cumulative_energy += net
                                    soc_at_jit_start_kwh = (current_soc / 100) * battery_capacity + cumulative_energy
                                    soc_at_jit_start_kwh = max(min_kwh, min(max_kwh, soc_at_jit_start_kwh))
                                    logger.info(f"   ✅ Window expanded to hour {jit_start}, restarting iteration...")
                                continue  # Restart iteration with expanded window
                            else:
                                # Cannot expand further - accept lower target
                                logger.info(f"   ⚠️ Cannot expand window further, using cheaper hours only")
                                window_expanded = True  # Prevent infinite loop
                                break  # Exit iteration, use what we have

                        # Check if we reached target
                        if lowest_soc_kwh >= target_lowest_kwh - 0.1:  # 0.1 kWh tolerance
                            logger.info(f"   ✅ Target reached! Lowest {(lowest_soc_kwh/battery_capacity)*100:.1f}% >= target {(target_lowest_kwh/battery_capacity)*100:.1f}%")
                            break

                        # If not enough hours available, expand window
                        if remaining_kwh > 0.1 and iteration == 0:
                            for h in range(jit_start - 1, search_start - 1, -1):
                                if h < 0 or remaining_kwh <= 0:
                                    break
                                if hourly_charging[h] == 0 and baseline_soc[h] < max_soc - 2:
                                    available_hours.append({'hour': h, 'price': hourly_prices[h]})
                                    available_hours.sort(key=lambda x: x['price'])

                        # Need more charge - add deficit + 10% buffer
                        deficit_kwh = target_lowest_kwh - lowest_soc_kwh
                        required_charge_kwh += deficit_kwh * 1.1

                        if iteration == max_iterations - 1:
                            logger.warning(f"   ⚠️ Max iterations, using best effort: {required_charge_kwh:.2f} kWh")

                    # Step 6: Apply the final charging plan
                    if required_charge_kwh > 0.5 and available_hours:
                        remaining_kwh = required_charge_kwh
                        initial_kwh = required_charge_kwh

                        cheapest_3 = [(h['hour'], f"{h['price']*100:.1f}Ct") for h in available_hours[:3]]
                        logger.info(f"   ⚡ Cheapest hours: {cheapest_3}")
                        logger.info(f"   📊 Final plan: {required_charge_kwh:.2f} kWh in {math.ceil(required_charge_kwh / max_charge_power)}h @ {max_charge_power:.2f} kW")

                        for slot in available_hours:
                            if remaining_kwh <= 0.1:  # Small tolerance
                                break

                            hour = slot['hour']
                            price_ct = slot['price'] * 100

                            # Check if this hour has significant PV that makes charging unnecessary
                            # Skip if PV > 2.5 kW (saves expensive grid charging when sun provides power)
                            # Extended to 4h before peak to catch midday PV hours
                            if hourly_pv[hour] > 2.5 and abs(hour - peak_start) <= 4:
                                logger.info(f"   ⏭️ Skip hour {hour}: High PV ({hourly_pv[hour]:.1f} kWh) - prefer morning hours")
                                continue

                            # Skip expensive hours if we already charged > 60% of target
                            charged_so_far = initial_kwh - remaining_kwh
                            if price_ct > 28.5 and charged_so_far > initial_kwh * 0.6:
                                logger.info(f"   ⏭️ Skip hour {hour}: Too expensive ({price_ct:.1f} Ct), have {charged_so_far:.1f}/{initial_kwh:.1f} kWh")
                                break  # Stop here, accept partial charge

                            charge_kwh = min(remaining_kwh, max_charge_power)

                            hourly_charging[hour] = charge_kwh
                            remaining_kwh -= charge_kwh

                            charging_windows.append({
                                'hour': hour,
                                'charge_kwh': charge_kwh,
                                'price': slot['price'],
                                'reason': f'Peak {peak_idx+1} (h{peak_start}-{peak_end} @ {peak[0]["price"]*100:.0f}+ Ct)'
                            })

                            logger.info(f"   ✓ Charge hour {hour}: {charge_kwh:.2f} kWh @ {slot['price']*100:.1f} Ct")

                        if remaining_kwh > 0.5:
                            logger.info(f"   ℹ️ Partial charge: {initial_kwh - remaining_kwh:.1f}/{initial_kwh:.1f} kWh (skipped expensive hours)")
                    else:
                        logger.info(f"   ✓ Sufficient SOC, no charging needed")

                logger.info(f"")
                logger.info(f"💡 Multi-Peak Strategy Complete: {len(charging_windows)} windows, total {sum(hourly_charging):.2f} kWh")

            elif deficit_hours:
                # Fallback: Only deficits, no expensive hours → use minimal charging
                first_deficit_hour = deficit_hours[0]['hour']
                logger.info(f"📊 Fallback: Deficit-only charging (no expensive hours above {price_threshold*100:.1f} Ct)")

                # Calculate minimal energy to stay above min_soc
                max_cumulative_deficit = 0
                cumulative_energy = 0
                for hour in range(first_deficit_hour, lookahead_hours):
                    net_energy = hourly_pv[hour] - hourly_consumption[hour]
                    cumulative_energy += net_energy
                    soc_at_hour_kwh = (current_soc / 100) * battery_capacity + cumulative_energy
                    target_kwh = ((min_soc + 10) / 100) * battery_capacity
                    deficit_at_hour = max(0, target_kwh - soc_at_hour_kwh)
                    max_cumulative_deficit = max(max_cumulative_deficit, deficit_at_hour)

                required_charge_kwh = max_cumulative_deficit
                logger.info(f"  Energy needed: {required_charge_kwh:.2f} kWh to stay above {min_soc+10}%")

                # Find cheapest hours before deficit
                available_hours = []
                for h in range(0, first_deficit_hour):
                    available_hours.append({'hour': h, 'price': hourly_prices[h]})
                available_hours.sort(key=lambda x: x['price'])

                remaining_kwh = required_charge_kwh
                for slot in available_hours:
                    if remaining_kwh <= 0:
                        break
                    hour = slot['hour']
                    charge_kwh = min(remaining_kwh, max_charge_power)
                    hourly_charging[hour] = charge_kwh
                    remaining_kwh -= charge_kwh
                    charging_windows.append({
                        'hour': hour,
                        'charge_kwh': charge_kwh,
                        'price': slot['price'],
                        'reason': f'Prevent deficit at hour {first_deficit_hour}'
                    })

            logger.info(f"Planned {len(charging_windows)} charging windows, total {sum(hourly_charging):.2f} kWh")

            # DEBUG: Log charging plan
            if charging_windows:
                logger.info(f"📊 Charging Plan:")
                for window in charging_windows[:10]:  # First 10 windows
                    logger.info(f"  Hour {window['hour']:2d}: {window['charge_kwh']:.2f} kWh @ {window['price']*100:.1f} Ct")

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

            # Get battery parameters
            battery_capacity = config.get('battery_capacity', 10.6)  # kWh
            min_soc = int(config.get('auto_safety_soc', 20))  # %
            max_soc = int(config.get('auto_charge_below_soc', 95))  # %
            max_charge_power = config.get('max_charge_power', 3900) / 1000  # kW

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