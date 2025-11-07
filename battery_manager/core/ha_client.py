#!/usr/bin/env python3
"""
Home Assistant API Client

Liest Sensordaten aus Home Assistant (Tibber, Forecast.Solar, Battery SOC, etc.)
"""

import os
import requests
import logging

logger = logging.getLogger(__name__)


class HomeAssistantClient:
    """Client for Home Assistant Supervisor API"""
    
    def __init__(self):
        """Initialize Home Assistant API client"""
        self.token = os.getenv('SUPERVISOR_TOKEN')
        self.api_url = os.getenv('HASSIO_API', 'http://supervisor/core')
        
        self.headers = {
            'Authorization': f'Bearer {self.token}',
            'Content-Type': 'application/json'
        }
        
        if not self.token:
            logger.warning("No SUPERVISOR_TOKEN found - running in development mode")
        else:
            logger.info("Home Assistant API client initialized")
    
    def get_state(self, entity_id):
        """
        Get state of an entity
        
        Args:
            entity_id: Entity ID (e.g., 'sensor.battery_soc')
        
        Returns:
            str: State value or None if failed
        """
        if not self.token:
            logger.debug(f"Cannot get state for {entity_id} - no token")
            return None
        
        try:
            url = f"{self.api_url}/api/states/{entity_id}"
            response = requests.get(url, headers=self.headers, timeout=5)
            
            if response.status_code == 200:
                data = response.json()
                return data.get('state')
            else:
                logger.warning(f"Failed to get state for {entity_id}: {response.status_code}")
                return None
                
        except Exception as e:
            logger.error(f"Error getting state for {entity_id}: {e}")
            return None
    
    def get_attributes(self, entity_id):
        """
        Get all attributes of an entity
        
        Args:
            entity_id: Entity ID
        
        Returns:
            dict: Attributes or None if failed
        """
        if not self.token:
            return None
        
        try:
            url = f"{self.api_url}/api/states/{entity_id}"
            response = requests.get(url, headers=self.headers, timeout=5)
            
            if response.status_code == 200:
                data = response.json()
                return data.get('attributes', {})
            else:
                return None
                
        except Exception as e:
            logger.error(f"Error getting attributes for {entity_id}: {e}")
            return None
    
    def call_service(self, domain, service, entity_id=None, data=None):
        """
        Call a Home Assistant service

        Args:
            domain: Service domain (e.g., 'light', 'switch')
            service: Service name (e.g., 'turn_on', 'turn_off')
            entity_id: Entity ID (optional)
            data: Additional service data (optional)

        Returns:
            bool: True if successful
        """
        if not self.token:
            logger.debug("Cannot call service - no token")
            return False

        try:
            url = f"{self.api_url}/api/services/{domain}/{service}"

            payload = data or {}
            if entity_id:
                payload['entity_id'] = entity_id

            response = requests.post(
                url,
                json=payload,
                headers=self.headers,
                timeout=10
            )

            return response.status_code == 200

        except Exception as e:
            logger.error(f"Error calling service {domain}.{service}: {e}")
            return False

    def get_state_with_attributes(self, entity_id):
        """
        Get entity state with all attributes (v0.2.1)

        Args:
            entity_id: Entity ID

        Returns:
            dict: Full entity data including state and attributes, or None if failed
        """
        if not self.token:
            logger.debug(f"Cannot get state with attributes for {entity_id} - no token")
            return None

        try:
            url = f"{self.api_url}/api/states/{entity_id}"
            response = requests.get(url, headers=self.headers, timeout=10)

            if response.status_code == 200:
                return response.json()
            else:
                logger.warning(f"Failed to get state with attributes for {entity_id}: {response.status_code}")
                return None

        except Exception as e:
            logger.error(f"Error getting state with attributes for {entity_id}: {e}")
            return None

    def set_datetime(self, entity_id, dt):
        """
        Set input_datetime value (v0.3.0)

        Args:
            entity_id: Entity ID of input_datetime
            dt: datetime object to set

        Returns:
            bool: True if successful
        """
        if not self.token:
            logger.debug("Cannot set datetime - no token")
            return False

        try:
            url = f"{self.api_url}/api/services/input_datetime/set_datetime"
            data = {
                "entity_id": entity_id,
                "datetime": dt.isoformat()
            }
            response = requests.post(url, json=data, headers=self.headers, timeout=10)
            return response.status_code == 200
        except Exception as e:
            logger.error(f"Error setting datetime: {e}")
            return False

    def get_history(self, entity_id, start_time, end_time=None):
        """
        Get historical data for an entity (v0.6.0)

        Args:
            entity_id: Entity ID (e.g., 'sensor.ksem_home_consumption')
            start_time: Start datetime (ISO format or datetime object)
            end_time: End datetime (ISO format or datetime object), optional (defaults to now)

        Returns:
            list: List of state changes, each with 'state', 'last_changed', etc.
                  Returns empty list if failed
        """
        if not self.token:
            logger.debug(f"Cannot get history for {entity_id} - no token")
            return []

        try:
            # Convert datetime objects to ISO strings if needed
            if hasattr(start_time, 'isoformat'):
                start_time = start_time.isoformat()

            # Build URL
            url = f"{self.api_url}/api/history/period/{start_time}"
            params = {'filter_entity_id': entity_id}

            if end_time:
                if hasattr(end_time, 'isoformat'):
                    end_time = end_time.isoformat()
                params['end_time'] = end_time

            logger.info(f"Fetching history for {entity_id} from {start_time} to {end_time or 'now'}")

            response = requests.get(url, params=params, headers=self.headers, timeout=30)

            if response.status_code == 200:
                data = response.json()
                # History API returns list of lists, one per entity
                if data and len(data) > 0:
                    history = data[0]  # First element is our entity
                    logger.info(f"Retrieved {len(history)} history entries for {entity_id}")
                    return history
                else:
                    logger.warning(f"No history data found for {entity_id}")
                    return []
            else:
                logger.error(f"Failed to get history for {entity_id}: HTTP {response.status_code}")
                return []

        except Exception as e:
            logger.error(f"Error getting history for {entity_id}: {e}")
            return []
