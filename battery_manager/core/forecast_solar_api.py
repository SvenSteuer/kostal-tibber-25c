#!/usr/bin/env python3
"""
Forecast.Solar Professional API Client

Fetches hourly solar production forecasts from forecast.solar API
Supports multiple planes (roof orientations) and caching
"""

import logging
import time
import requests
from datetime import datetime, timedelta
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class ForecastSolarAPI:
    """Client for forecast.solar Professional API"""

    def __init__(self, api_key: str, latitude: float, longitude: float,
                 use_weather_endpoint: bool = True):
        """
        Initialize forecast.solar API client

        Args:
            api_key: forecast.solar Professional API key
            latitude: Location latitude
            longitude: Location longitude
            use_weather_endpoint: If True (default), use estimateweather (Pro);
                                  set False to fall back to plain estimate.
        """
        self.api_key = api_key
        self.latitude = latitude
        self.longitude = longitude
        self.base_url = "https://api.forecast.solar"
        self._use_weather_endpoint = use_weather_endpoint

        # Cache for API responses (15 min cache)
        self._cache = {}
        self._cache_timestamp = None
        self._cache_duration = timedelta(minutes=15)

        # v1.3.5: back-off after a failed/incomplete fetch. Incomplete results are no longer
        # cached and /api/status polls every 2 s - without this the API would be hammered
        # (and a rate limit would never recover).
        self._failure_backoff_seconds = 300
        self._last_failure = None  # time.monotonic()

        # v1.3: Cache for historic data (1 day TTL — historic doesn't change)
        self._historic_cache = {}
        self._historic_cache_date = None

        # v1.3.6 (Phase 1b): optional PVForecastArchive - stores every raw API response
        self.archive = None

        logger.info(f"Forecast.Solar API initialized (lat={latitude}, lon={longitude}, "
                    f"weather_endpoint={use_weather_endpoint})")

    def _build_url(self, endpoint: str, declination: int, azimuth: int, kwp: float) -> str:
        """
        Build API URL for a single plane

        Args:
            endpoint: API endpoint (e.g., 'estimate')
            declination: Roof tilt angle (0-90°)
            azimuth: Roof orientation (-180 to 180°, 0=South, 90=West, -90=East)
            kwp: Peak power in kWp

        Returns:
            Complete API URL
        """
        # Convert float to URL-safe format
        lat = str(self.latitude).replace('.', ',')
        lon = str(self.longitude).replace('.', ',')
        kwp_str = str(kwp).replace('.', ',')

        url = (f"{self.base_url}/{self.api_key}/{endpoint}/"
               f"{lat}/{lon}/{declination}/{azimuth}/{kwp_str}")

        return url

    def set_archive(self, archive):
        """v1.3.6: Archive for raw forecasts (core.data_collection.PVForecastArchive)."""
        self.archive = archive

    def _archive_plane(self, plane_index: int, plane: dict, data: dict):
        """v1.3.6: Archive one plane's raw watthours - an archive error never blocks the forecast."""
        if self.archive is None:
            return
        try:
            info = (data.get('message') or {}).get('info') or {}
            self.archive.store(plane_index, plane, data['result'], tz_name=info.get('timezone'))
        except Exception as e:
            logger.warning(f"Forecast.Solar archive failed (forecast not affected): {self._mask(e)}")

    def _mask(self, text) -> str:
        """v1.3.5: Hide the API key in log output (URLs, request exceptions)."""
        text = str(text)
        if self.api_key:
            text = text.replace(str(self.api_key), '***')
        return text

    def get_hourly_forecast(self,
                           planes: list,
                           include_tomorrow: bool = False) -> Dict[int, float]:
        """
        Get hourly solar production forecast for today (and optionally tomorrow)

        Args:
            planes: List of dicts with 'declination', 'azimuth', 'kwp'
                   e.g., [{'declination': 22, 'azimuth': 45, 'kwp': 8.96}]
            include_tomorrow: If True, returns 48h forecast (today=0-23, tomorrow=24-47)

        Returns:
            dict: {hour: kwh_forecast} for each hour
                  If include_tomorrow=False: hour 0-23 (today only)
                  If include_tomorrow=True: hour 0-47 (today=0-23, tomorrow=24-47)
        """
        # Check cache first
        cache_key = f'hourly_forecast_tomorrow_{include_tomorrow}'
        if self._is_cache_valid():
            cached = self._cache.get(cache_key)
            if cached is not None:
                logger.debug(f"Using cached forecast.solar data (include_tomorrow={include_tomorrow})")
                return cached

        if (self._last_failure is not None and
                time.monotonic() - self._last_failure < self._failure_backoff_seconds):
            logger.debug("Forecast.Solar: last fetch failed - backing off, no forecast")
            return {}

        try:
            today = datetime.now().astimezone().date()
            tomorrow = today + timedelta(days=1)
            hourly_forecast = {}

            # Fetch forecast for each plane and combine
            for i, plane in enumerate(planes):
                logger.debug(f"Fetching forecast for plane {i+1}: "
                           f"azimuth={plane['azimuth']}°, "
                           f"tilt={plane['declination']}°, "
                           f"kWp={plane['kwp']}")

                # v1.3.1 Fix: forecast.solar has no separate "estimateweather" endpoint —
                # the standard /estimate endpoint is automatically weather-aware for Pro accounts
                # and uses average climate data for Personal/Public keys. The previous
                # 'estimateweather/watthours' returns HTTP 404 ("Requested function not found"),
                # which caused total_pv=0 in the optimizer and triggered aggressive nightly
                # grid charging. The use_weather_endpoint flag is now a no-op kept for
                # backwards-compat with existing runtime_config.json files.
                endpoint = 'estimate/watthours'
                url = self._build_url(
                    endpoint=endpoint,
                    declination=plane['declination'],
                    azimuth=plane['azimuth'],
                    kwp=plane['kwp']
                )

                logger.info(f"Fetching Plane {i+1} from Forecast.Solar: {self._mask(url)}")

                response = requests.get(url, timeout=10)

                # v1.3.5 (H8): one missing plane -> no forecast at all (planner makes no plan)
                # instead of a partial sum that under-estimates PV and triggers grid charging
                if response.status_code != 200:
                    logger.error(f"Forecast.Solar API error: HTTP {response.status_code}")
                    logger.error(f"Response: {self._mask(response.text)}")
                    logger.error(f"Plane {i+1} missing - discarding the whole PV forecast")
                    return self._fetch_failed()

                data = response.json()

                # Extract watt_hours data
                # The /estimate/watthours endpoint returns timestamps directly in 'result'
                if 'result' in data:
                    watt_hours = data['result']
                    self._archive_plane(i, plane, data)  # v1.3.6 (1b): raw data, never raises

                    # Filter out non-timestamp keys (API might include metadata)
                    valid_entries = {k: v for k, v in watt_hours.items()
                                   if isinstance(k, str) and len(k) >= 10}  # Timestamp format check

                    logger.info(f"Plane {i+1}: received {len(valid_entries)} time intervals")

                    # CRITICAL: Forecast.Solar /watthours endpoint returns CUMULATIVE values
                    # We need to convert each plane's cumulative values to hourly deltas first,
                    # then combine the deltas from all planes

                    # Step 1: Collect cumulative values for THIS plane, by date and hour
                    plane_cumulative_today = {}
                    plane_cumulative_tomorrow = {}

                    for timestamp_str, wh_value in valid_entries.items():
                        try:
                            # Parse timestamp (format: "2025-11-05 14:00:00")
                            dt = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
                            kwh = float(wh_value) / 1000.0  # Wh to kWh

                            # Process today's data
                            if dt.date() == today:
                                hour = dt.hour
                                plane_cumulative_today[hour] = kwh
                            # Process tomorrow's data (if requested)
                            elif include_tomorrow and dt.date() == tomorrow:
                                hour = dt.hour
                                plane_cumulative_tomorrow[hour] = kwh

                        except (ValueError, TypeError) as e:
                            logger.debug(f"Skipping entry {timestamp_str}: {e}")
                            continue

                    # v1.3.5 (H8): the plane must cover today (and tomorrow if requested)
                    if not plane_cumulative_today or (include_tomorrow and not plane_cumulative_tomorrow):
                        logger.error(f"Plane {i+1}: forecast incomplete (today={len(plane_cumulative_today)}, "
                                     f"tomorrow={len(plane_cumulative_tomorrow)} entries) - "
                                     f"discarding the whole PV forecast")
                        return self._fetch_failed()

                    # Step 2: Convert THIS plane's cumulative values to hourly deltas for TODAY
                    if plane_cumulative_today:
                        sorted_hours = sorted(plane_cumulative_today.keys())
                        logger.debug(f"Plane {i+1}: Converting today's cumulative to hourly deltas for hours {sorted_hours}")

                        for idx, hour in enumerate(sorted_hours):
                            if idx == 0:
                                # First hour: use cumulative value as-is (production from midnight to this hour)
                                hourly_delta = plane_cumulative_today[hour]
                            else:
                                # Subsequent hours: subtract previous cumulative from current
                                prev_hour = sorted_hours[idx - 1]
                                hourly_delta = plane_cumulative_today[hour] - plane_cumulative_today[prev_hour]
                                hourly_delta = max(0.0, hourly_delta)  # Can't be negative

                            # Step 3: Add this plane's hourly delta to the combined forecast (hour 0-23)
                            hourly_forecast[hour] = hourly_forecast.get(hour, 0.0) + hourly_delta

                        logger.debug(f"Plane {i+1}: Converted {len(plane_cumulative_today)} today's cumulative values to hourly deltas")

                    # Step 2b: Convert THIS plane's cumulative values to hourly deltas for TOMORROW
                    if include_tomorrow and plane_cumulative_tomorrow:
                        sorted_hours = sorted(plane_cumulative_tomorrow.keys())
                        logger.debug(f"Plane {i+1}: Converting tomorrow's cumulative to hourly deltas for hours {sorted_hours}")

                        for idx, hour in enumerate(sorted_hours):
                            if idx == 0:
                                # First hour: use cumulative value as-is
                                hourly_delta = plane_cumulative_tomorrow[hour]
                            else:
                                # Subsequent hours: subtract previous cumulative from current
                                prev_hour = sorted_hours[idx - 1]
                                hourly_delta = plane_cumulative_tomorrow[hour] - plane_cumulative_tomorrow[prev_hour]
                                hourly_delta = max(0.0, hourly_delta)  # Can't be negative

                            # Step 3: Add this plane's hourly delta to the combined forecast (hour 24-47)
                            tomorrow_hour = hour + 24  # Offset by 24 hours
                            hourly_forecast[tomorrow_hour] = hourly_forecast.get(tomorrow_hour, 0.0) + hourly_delta

                        logger.debug(f"Plane {i+1}: Converted {len(plane_cumulative_tomorrow)} tomorrow's cumulative values to hourly deltas")

                else:
                    logger.error(f"Plane {i+1}: No 'result' key in API response")
                    logger.error(f"Full response: {self._mask(data)}")
                    return self._fetch_failed()  # v1.3.5 (H8): no partial forecast

            if hourly_forecast:
                logger.info(f"✓ Forecast.Solar: Retrieved {len(hourly_forecast)} hours from API (include_tomorrow={include_tomorrow})")
                logger.debug(f"Hourly forecast (kWh): {hourly_forecast}")

                # Update cache
                self._cache[cache_key] = hourly_forecast
                self._cache_timestamp = datetime.now()
                self._last_failure = None
            else:
                logger.warning("No hourly forecast data retrieved from Forecast.Solar API")
                return self._fetch_failed()

            return hourly_forecast

        except requests.RequestException as e:
            logger.error(f"Network error calling Forecast.Solar API: {self._mask(e)}")
            return self._fetch_failed()
        except Exception as e:
            logger.error(f"Error getting hourly forecast from Forecast.Solar: {self._mask(e)}", exc_info=True)
            return self._fetch_failed()

    def _fetch_failed(self) -> Dict[int, float]:
        """v1.3.5: Remember the failure for the back-off and return 'no forecast'."""
        self._last_failure = time.monotonic()
        return {}

    def _is_cache_valid(self) -> bool:
        """Check if cached data is still valid"""
        if not self._cache or not self._cache_timestamp:
            return False

        age = datetime.now() - self._cache_timestamp
        return age < self._cache_duration

    def clear_cache(self):
        """Clear cached forecast data"""
        self._cache = {}
        self._cache_timestamp = None
        logger.debug("Forecast.Solar cache cleared")

    # =====================================================================
    # v1.3: Pro endpoints — historic + time
    # =====================================================================

    def get_historic_daily_kwh(self, planes: list, days_back: int = 14) -> Dict[str, float]:
        """v1.3: Fetch ACTUAL historic production from forecast.solar Pro
        ('historic' endpoint, computed from actual past weather data).

        Args:
            planes: list of dicts with 'declination', 'azimuth', 'kwp'
            days_back: how many past days to fetch (max ~30 on Pro)

        Returns:
            dict: {YYYY-MM-DD: kWh_total} for each past day, summed across planes
        """
        # Cache by date (re-fetch only once per calendar day)
        today = datetime.now().date()
        if self._historic_cache_date == today and self._historic_cache:
            logger.debug("Using cached historic data")
            return dict(self._historic_cache)

        try:
            daily_totals: Dict[str, float] = {}

            for i, plane in enumerate(planes):
                # v1.3.1 Fix: forecast.solar endpoint is /history, not /historic
                # ('historic' returns HTTP 404, blocking PV-bias auto-calibration entirely).
                url = self._build_url(
                    endpoint='history',
                    declination=plane['declination'],
                    azimuth=plane['azimuth'],
                    kwp=plane['kwp']
                )
                # Pro history endpoint accepts ?time=YYYY-MM-DD or returns last N days by default.
                # We just take what comes back and aggregate per day.
                logger.info(f"Fetching history for plane {i+1}: {self._mask(url)}")
                response = requests.get(url, timeout=15)
                if response.status_code != 200:
                    logger.error(f"historic API error {response.status_code}: {self._mask(response.text[:200])}")
                    continue
                data = response.json()
                result = data.get('result', {})
                if not result:
                    logger.warning(f"historic plane {i+1}: empty result")
                    continue

                # The 'historic' endpoint returns per-hour wh values. Aggregate per day.
                # Keys are timestamp strings; values are watt-hours.
                # forecast.solar /historic returns CUMULATIVE values per day similar to /estimate.
                # Simpler: fetch /historic/watthours which returns hourly deltas, OR just take
                # the day-end value as the daily total.
                plane_daily: Dict[str, float] = {}
                # Group keys by date and keep the maximum (cumulative end-of-day value)
                for ts_str, wh in result.items():
                    if not isinstance(ts_str, str) or len(ts_str) < 10:
                        continue
                    try:
                        date_str = ts_str[:10]
                        plane_daily[date_str] = max(plane_daily.get(date_str, 0.0), float(wh))
                    except (ValueError, TypeError):
                        continue

                # Add to daily_totals (sum across planes), convert Wh -> kWh
                for date_str, wh_max in plane_daily.items():
                    daily_totals[date_str] = daily_totals.get(date_str, 0.0) + wh_max / 1000.0

            # Limit to most recent N days
            if daily_totals:
                cutoff = (today - timedelta(days=days_back)).isoformat()
                daily_totals = {d: kwh for d, kwh in daily_totals.items() if d >= cutoff}

            self._historic_cache = dict(daily_totals)
            self._historic_cache_date = today
            logger.info(f"✓ Forecast.Solar historic: {len(daily_totals)} days fetched")
            return daily_totals

        except requests.RequestException as e:
            logger.error(f"Network error calling historic endpoint: {self._mask(e)}")
            return {}
        except Exception as e:
            logger.error(f"Error fetching historic: {self._mask(e)}", exc_info=True)
            return {}

    def get_time_windows(self, planes: list, duration_minutes: int,
                         min_power_w: int = 0) -> list:
        """v1.3: Fetch optimal solar time windows from forecast.solar Pro 'time' endpoint.

        Returns the best calendar windows (today/tomorrow) where the predicted PV
        production for the requested duration is highest. Useful for scheduling
        controllable loads (pool pump, washing machine, etc.) to run on solar power.

        Args:
            planes: list of dicts with 'declination', 'azimuth', 'kwp'
            duration_minutes: required runtime in minutes
            min_power_w: optional — only consider windows where predicted power >= this (W)

        Returns:
            list of dicts: [{start: datetime, end: datetime, expected_kwh: float, ...}]
            sorted best-first (highest expected energy)
        """
        # The /time/{key}/{lat}/{lon}/{dec}/{az}/{kwp} endpoint returns optimal
        # production-time windows. With multiple planes, we sum their hourly
        # forecasts (already available via get_hourly_forecast) and search locally.
        # This is more flexible than calling /time per plane and combining heuristically.
        try:
            hourly = self.get_hourly_forecast(planes, include_tomorrow=True)
            if not hourly:
                return []

            # Convert to ordered list of (datetime, kwh)
            now = datetime.now().astimezone()
            today = now.date()
            tomorrow = today + timedelta(days=1)
            slots = []
            for hour_idx, kwh in hourly.items():
                if hour_idx < 24:
                    dt = datetime.combine(today, datetime.min.time()).replace(
                        hour=hour_idx, tzinfo=now.tzinfo)
                else:
                    dt = datetime.combine(tomorrow, datetime.min.time()).replace(
                        hour=hour_idx - 24, tzinfo=now.tzinfo)
                slots.append((dt, kwh))
            slots.sort(key=lambda x: x[0])

            # Filter to future
            slots = [s for s in slots if s[0] >= now - timedelta(hours=1)]

            # Sliding window of size = ceil(duration / 60)
            import math
            window_size = max(1, math.ceil(duration_minutes / 60))

            results = []
            for i in range(0, len(slots) - window_size + 1):
                window = slots[i:i + window_size]
                expected_kwh = sum(s[1] for s in window)
                avg_power_w = expected_kwh * 1000 / window_size  # Wh per hour ~ W
                if min_power_w > 0 and avg_power_w < min_power_w:
                    continue
                results.append({
                    'start': window[0][0],
                    'end': window[-1][0] + timedelta(hours=1),
                    'expected_kwh': expected_kwh,
                    'avg_power_w': avg_power_w,
                })

            results.sort(key=lambda x: -x['expected_kwh'])
            return results

        except Exception as e:
            logger.error(f"Error in get_time_windows: {e}", exc_info=True)
            return []
