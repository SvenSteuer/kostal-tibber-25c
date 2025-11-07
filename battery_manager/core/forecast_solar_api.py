#!/usr/bin/env python3
"""
Forecast.Solar Professional API Client

Fetches hourly solar production forecasts from forecast.solar API
Supports multiple planes (roof orientations) and caching
"""

import logging
import requests
from datetime import datetime, timedelta
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class ForecastSolarAPI:
    """Client for forecast.solar Professional API"""

    def __init__(self, api_key: str, latitude: float, longitude: float):
        """
        Initialize forecast.solar API client

        Args:
            api_key: forecast.solar Professional API key
            latitude: Location latitude
            longitude: Location longitude
        """
        self.api_key = api_key
        self.latitude = latitude
        self.longitude = longitude
        self.base_url = "https://api.forecast.solar"

        # Cache for API responses (15 min cache)
        self._cache = {}
        self._cache_timestamp = None
        self._cache_duration = timedelta(minutes=15)

        logger.info(f"Forecast.Solar API initialized (lat={latitude}, lon={longitude})")

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

                url = self._build_url(
                    endpoint='estimate/watthours',
                    declination=plane['declination'],
                    azimuth=plane['azimuth'],
                    kwp=plane['kwp']
                )

                logger.info(f"Fetching Plane {i+1} from Forecast.Solar: {url}")

                response = requests.get(url, timeout=10)

                if response.status_code != 200:
                    logger.error(f"Forecast.Solar API error: HTTP {response.status_code}")
                    logger.error(f"Response: {response.text}")
                    continue

                data = response.json()

                # Extract watt_hours data
                # The /estimate/watthours endpoint returns timestamps directly in 'result'
                if 'result' in data:
                    watt_hours = data['result']

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
                    logger.error(f"Full response: {data}")

            if hourly_forecast:
                logger.info(f"✓ Forecast.Solar: Retrieved {len(hourly_forecast)} hours from API (include_tomorrow={include_tomorrow})")
                logger.debug(f"Hourly forecast (kWh): {hourly_forecast}")

                # Update cache
                self._cache[cache_key] = hourly_forecast
                self._cache_timestamp = datetime.now()
            else:
                logger.warning("No hourly forecast data retrieved from Forecast.Solar API")

            return hourly_forecast

        except requests.RequestException as e:
            logger.error(f"Network error calling Forecast.Solar API: {e}")
            return {}
        except Exception as e:
            logger.error(f"Error getting hourly forecast from Forecast.Solar: {e}", exc_info=True)
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
