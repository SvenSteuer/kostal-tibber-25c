#!/usr/bin/env python3
"""
Kostal REST API Client - v0.1.2 FIXED SESSION HANDLING
Portiert von batctl.py - Credits: Kilian Knoll

Verbesserungen:
- Keine Session-Validierung die zu 403 führt
- Session wird vertraut wenn sie existiert
- Nur bei 401 Unauthorized neu authentifizieren
"""

import requests
import hashlib
import hmac
import base64
import json
import random
import string
import os
import logging
from pathlib import Path
from Crypto.Cipher import AES

logger = logging.getLogger(__name__)


class KostalAPI:
    """
    Kostal Plenticore Plus REST API Client
    
    Handhabt die komplexe PBKDF2 + AES Authentifizierung
    und ermöglicht das Setzen von Wechselrichter-Parametern
    """
    
    def __init__(self, inverter_ip, installer_password, master_password):
        """
        Initialize Kostal API Client
        
        Args:
            inverter_ip: IP-Adresse des Wechselrichters
            installer_password: Installer-Passwort (Master Key im Kostal)
            master_password: Master-Passwort (Service Code - MIT : am Anfang!)
        """
        self.base_url = f"http://{inverter_ip}/api/v1"
        self.installer_password = installer_password
        self.master_password = master_password
        self.session_id = None
        self.headers = None
        self.authenticated = False
        
        # Session file path
        self.session_file = Path("/data/kostal_session.id")
        
        logger.info(f"Kostal API initialized for {inverter_ip}")
    
    def _random_string(self, length=12):
        """Generate random string for nonce"""
        letters = string.ascii_letters
        return ''.join(random.choice(letters) for i in range(length))
    
    def _pbkdf2_hash(self, password, salt, rounds):
        """Generate PBKDF2 hash"""
        return hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, rounds)
    
    def login(self):
        """
        Authenticate with Kostal inverter using complex PBKDF2 + AES flow
        
        Returns:
            bool: True if successful, False otherwise
        """
        try:
            logger.info("Starting Kostal API authentication...")
            
            # Step 1: Start authentication
            u = self._random_string(12)
            u = base64.b64encode(u.encode('utf-8')).decode('utf-8')
            
            step1 = {
                "username": "master",  # Use "master" for full access
                "nonce": u
            }
            
            url = f"{self.base_url}/auth/start"
            headers = {'Content-type': 'application/json', 'Accept': 'application/json'}
            
            logger.debug(f"Auth start request to {url}")
            response = requests.post(url, json=step1, headers=headers, timeout=10)
            
            if response.status_code != 200:
                logger.error(f"Auth start failed: {response.status_code} - {response.text}")
                return False
            
            response_data = response.json()
            logger.debug(f"Auth start response: {response_data}")
            
            # Extract authentication parameters
            i = response_data['nonce']
            e = response_data['transactionId']
            o = response_data['rounds']
            a = response_data['salt']
            bit_salt = base64.b64decode(a)
            
            # Step 2: Generate proof
            r = self._pbkdf2_hash(self.installer_password, bit_salt, o)
            s = hmac.new(r, "Client Key".encode('utf-8'), hashlib.sha256).digest()
            c = hmac.new(r, "Server Key".encode('utf-8'), hashlib.sha256).digest()
            _ = hashlib.sha256(s).digest()
            
            d = f"n=master,r={u},r={i},s={a},i={str(o)},c=biws,r={i}"
            g = hmac.new(_, d.encode('utf-8'), hashlib.sha256).digest()
            p = hmac.new(c, d.encode('utf-8'), hashlib.sha256).digest()
            f = bytes(a ^ b for (a, b) in zip(s, g))
            
            proof = base64.b64encode(f).decode('utf-8')
            
            step2 = {
                "transactionId": e,
                "proof": proof
            }
            
            # Step 3: Finish authentication
            url = f"{self.base_url}/auth/finish"
            logger.debug("Sending auth finish request")
            response = requests.post(url, json=step2, headers=headers, timeout=10)
            
            if response.status_code != 200:
                logger.error(f"Auth finish failed: {response.status_code} - {response.text}")
                return False
            
            response_data = response.json()
            token = response_data['token']
            
            # Step 4: Create session with master password
            y = hmac.new(_, "Session Key".encode('utf-8'), hashlib.sha256)
            y.update(d.encode('utf-8'))
            y.update(s)
            protocol_key = y.digest()
            
            t = os.urandom(16)
            cipher = AES.new(protocol_key, AES.MODE_GCM, t)
            
            # Encrypt token with master password
            encrypted, authtag = cipher.encrypt_and_digest(
                (token + self.master_password).encode('utf-8')
            )
            
            step3 = {
                "transactionId": e,
                "iv": base64.b64encode(t).decode('utf-8'),
                "tag": base64.b64encode(authtag).decode("utf-8"),
                "payload": base64.b64encode(encrypted).decode('utf-8')
            }
            
            # Step 5: Create session
            url = f"{self.base_url}/auth/create_session"
            logger.debug("Creating session")
            response = requests.post(url, json=step3, headers=headers, timeout=10)
            
            if response.status_code != 200:
                logger.error(f"Session creation failed: {response.status_code} - {response.text}")
                return False
            
            response_data = response.json()
            self.session_id = response_data['sessionId']
            
            # Save session ID to file
            try:
                with open(self.session_file, 'w') as f:
                    f.write(self.session_id)
                logger.debug(f"Session saved to {self.session_file}")
            except Exception as e:
                logger.warning(f"Could not save session to file: {e}")
            
            # Create headers for subsequent requests
            self.headers = {
                'Content-type': 'application/json',
                'Accept': 'application/json',
                'authorization': f"Session {self.session_id}"
            }
            
            self.authenticated = True
            logger.info("✅ Kostal API authentication successful")
            return True
                
        except requests.exceptions.Timeout:
            logger.error("❌ Kostal API authentication timeout - check network connection")
            return False
        except requests.exceptions.ConnectionError as e:
            logger.error(f"❌ Kostal API connection error: {e}")
            return False
        except KeyError as e:
            logger.error(f"❌ Kostal API response missing key: {e}")
            return False
        except Exception as e:
            logger.error(f"❌ Kostal API authentication error: {e}")
            logger.exception(e)
            return False
    
    def logout(self):
        """Logout and invalidate session"""
        try:
            if self.headers:
                url = f"{self.base_url}/auth/logout"
                requests.post(url, headers=self.headers, timeout=5)
                logger.info("Logged out from Kostal API")
            
            # Remove session file
            if self.session_file.exists():
                self.session_file.unlink()
                
            self.session_id = None
            self.headers = None
            self.authenticated = False
            
        except Exception as e:
            logger.warning(f"Error during logout: {e}")
    
    def _load_session(self):
        """Load existing session from file"""
        if self.session_file.exists():
            try:
                with open(self.session_file, 'r') as f:
                    self.session_id = f.read().strip()
                    if self.session_id:
                        self.headers = {
                            'Content-type': 'application/json',
                            'Accept': 'application/json',
                            'authorization': f"Session {self.session_id}"
                        }
                        logger.info("✅ Loaded existing Kostal session")
                        return True
            except Exception as e:
                logger.warning(f"Could not load session: {e}")
        return False
    
    def _ensure_authenticated(self):
        """
        Ensure we have a valid session
        
        IMPROVED: Trust existing session, only re-authenticate if no session exists
        This avoids 403 errors from session validation attempts
        """
        # If we already have headers, trust them
        if self.headers:
            logger.debug("Using existing session headers")
            return True
        
        # Try to load session from file
        if self._load_session():
            logger.info("Using loaded session (no validation to avoid 403)")
            return True
        
        # No session exists, need to authenticate
        logger.info("No existing session found, authenticating...")
        return self.login()
    
    def _api_call_with_retry(self, method, url, **kwargs):
        """
        Make API call with automatic re-authentication on 401
        
        Args:
            method: HTTP method ('get', 'post', 'put', etc.)
            url: Full URL
            **kwargs: Additional arguments for requests
        
        Returns:
            Response object or None
        """
        if not self._ensure_authenticated():
            logger.error("Cannot make API call - authentication failed")
            return None
        
        try:
            # Add headers
            if 'headers' not in kwargs:
                kwargs['headers'] = self.headers
            
            # Make request
            response = getattr(requests, method)(url, **kwargs)
            
            # If 401 Unauthorized, try to re-authenticate once
            if response.status_code == 401:
                logger.warning("Got 401 Unauthorized, re-authenticating...")
                self.headers = None  # Clear old headers
                if self.login():
                    # Retry with new session
                    kwargs['headers'] = self.headers
                    response = getattr(requests, method)(url, **kwargs)
                else:
                    logger.error("Re-authentication failed")
                    return None
            
            return response
            
        except Exception as e:
            logger.error(f"API call error: {e}")
            return None
    
    def set_external_control(self, enabled):
        """
        Enable or disable external battery control
        
        Args:
            enabled: True = External control (mode 2), False = Internal control (mode 0)
        
        Returns:
            bool: True if successful
        """
        try:
            mode = "2" if enabled else "0"
            
            # Read current setting first
            url = f"{self.base_url}/settings/devices%3Alocal/Battery%3AExternControl"
            response = self._api_call_with_retry('get', url, timeout=10)
            
            if not response:
                logger.error("Failed to read current ExternControl setting")
                return False
            
            if response.status_code == 200:
                logger.debug(f"Current ExternControl: {response.json()}")
            
            # Prepare payload
            payload = [{
                "moduleid": "devices:local",
                "settings": [{
                    "id": "Battery:ExternControl",
                    "value": mode
                }]
            }]
            
            # Write new setting
            url = f"{self.base_url}/settings"
            response = self._api_call_with_retry('put', url, json=payload, timeout=10)
            
            if not response:
                logger.error("Failed to set ExternControl")
                return False
            
            if response.status_code == 200:
                control_mode = "external (Modbus)" if enabled else "internal"
                logger.info(f"✅ Battery control set to: {control_mode}")
                return True
            else:
                logger.error(f"❌ Failed to set battery control: {response.status_code} - {response.text}")
                return False
                
        except Exception as e:
            logger.error(f"❌ Error setting external control: {e}")
            logger.exception(e)
            return False
    
    def get_setting(self, setting_id):
        """
        Get a setting value from the inverter

        Args:
            setting_id: Setting ID (e.g., "Battery:ExternControl")

        Returns:
            dict: Setting data or None if failed
        """
        try:
            # URL encode the setting ID
            setting_id_encoded = setting_id.replace(":", "%3A")
            url = f"{self.base_url}/settings/devices%3Alocal/{setting_id_encoded}"

            response = self._api_call_with_retry('get', url, timeout=10)

            if response and response.status_code == 200:
                return response.json()
            else:
                logger.error(f"Failed to get setting {setting_id}")
                return None

        except Exception as e:
            logger.error(f"Error getting setting: {e}")
            return None

    def test_connection(self):
        """
        Test connection to inverter - FIXED VERSION
        
        Returns:
            bool: True if successful
        """
        try:
            logger.info("Testing connection to Kostal inverter...")
            
            # Test with a simple auth/start request (POST!)
            u = self._random_string(12)
            u = base64.b64encode(u.encode('utf-8')).decode('utf-8')
            
            url = f"{self.base_url}/auth/start"
            headers = {'Content-Type': 'application/json', 'Accept': 'application/json'}
            
            # Correct: POST with JSON body
            response = requests.post(
                url,
                json={"username": "master", "nonce": u},
                headers=headers,
                timeout=5
            )
            
            if response.status_code == 200:
                logger.info("✅ Connection test successful - Kostal responds correctly")
                return True
            elif response.status_code == 403:
                logger.warning("⚠️ Connection works but user is locked (too many failed attempts)")
                logger.warning("   Wait 30 minutes or restart the inverter to unlock")
                return False
            else:
                logger.error(f"❌ Connection test failed: HTTP {response.status_code}")
                return False
                
        except requests.exceptions.Timeout:
            logger.error("❌ Connection test timeout")
            return False
        except requests.exceptions.ConnectionError as e:
            logger.error(f"❌ Connection test failed: {e}")
            return False
        except Exception as e:
            logger.error(f"❌ Connection test error: {e}")
            return False
    
    def __del__(self):
        """Cleanup on destruction"""
        # Don't logout automatically - keep session alive
        pass