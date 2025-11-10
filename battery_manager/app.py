#!/usr/bin/env python3
"""
Kostal Battery Manager - Main Flask Application - FIXED VERSION
"""

import os
import json
import logging
import threading
from datetime import datetime
from typing import List
from flask import Flask, render_template, jsonify, request, redirect, url_for, make_response
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix

# Setup logging
log_level = os.getenv('LOG_LEVEL', 'INFO').upper()
logging.basicConfig(
    level=getattr(logging, log_level),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Initialize Flask app with correct paths
app = Flask(__name__,
            static_folder='static',
            template_folder='templates')

# Configure for Home Assistant Ingress support
# This ensures url_for() generates correct URLs with the Ingress prefix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# Enable CORS for Ingress
CORS(app)

# Context processor to inject base_path into all templates
# IMPORTANT: This runs BEFORE template rendering, so we detect Ingress here
@app.context_processor
def inject_base_path():
    """Detect Ingress prefix and inject base_path into all templates"""
    # Home Assistant Ingress sends the prefix in X-Ingress-Path header
    # Example: X-Ingress-Path: /api/hassio_ingress/1ytBWj2lv6Xc0Uy7veOWxrVwNgRR09z7NsoXmLVe9tM
    base_path = request.environ.get('SCRIPT_NAME', '')

    if not base_path or base_path == '':
        # Check for Home Assistant Ingress header
        ingress_path = request.headers.get('X-Ingress-Path', '')
        if ingress_path:
            # Use the Ingress path as base_path
            base_path = ingress_path
            # Set SCRIPT_NAME so url_for() generates correct URLs
            request.environ['SCRIPT_NAME'] = base_path
            logger.debug(f"Ingress detected: {base_path}")

    return dict(base_path=base_path)

app.config['SECRET_KEY'] = os.urandom(24)
# Disable template caching to ensure changes are reflected immediately
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.jinja_env.auto_reload = True
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0

# Configuration
CONFIG_PATH = os.getenv('CONFIG_PATH', '/data/options.json')
RUNTIME_CONFIG_PATH = '/data/runtime_config.json'  # v1.2.0-beta.53 - Persistent config from GUI

def normalize_planes_config(config):
    """
    v1.0.5 - Backward-compatible plane configuration normalization

    Supports BOTH formats for maximum compatibility:
    1. Array format (new/standard): forecast_solar_planes: [{...}, {...}]
    2. Individual fields (legacy): plane1_declination, plane1_azimuth, etc.
    3. Auto-creates defaults if Forecast.Solar API is enabled but no planes exist

    This ensures existing installations continue working while allowing
    the array format to persist without being deleted by HA.
    """
    # Check if array format already exists
    if 'forecast_solar_planes' in config and isinstance(config.get('forecast_solar_planes'), list):
        planes = config['forecast_solar_planes']
        if planes:
            logger.info(f"✓ Using existing forecast_solar_planes array: {len(planes)} plane(s)")
            return config

    # Fallback: Try to build from individual fields (for backward compatibility)
    planes = []
    for i in range(1, 3):  # Support up to 2 planes
        declination = config.get(f'plane{i}_declination')
        azimuth = config.get(f'plane{i}_azimuth')
        kwp = config.get(f'plane{i}_kwp')

        if declination is not None and azimuth is not None and kwp is not None:
            planes.append({
                'declination': int(declination),
                'azimuth': int(azimuth),
                'kwp': float(kwp)
            })

    if planes:
        config['forecast_solar_planes'] = planes
        logger.info(f"✓ Built forecast_solar_planes array from individual fields: {len(planes)} plane(s)")
    elif config.get('enable_forecast_solar_api', False):
        # No planes configured, but API is enabled → use defaults from config.yaml
        default_planes = [
            {'declination': 22, 'azimuth': 45, 'kwp': 8.96},
            {'declination': 22, 'azimuth': -135, 'kwp': 10.665}
        ]
        config['forecast_solar_planes'] = default_planes
        logger.info(f"✓ Using default forecast_solar_planes (no config found): {len(default_planes)} plane(s)")

    return config

def load_config():
    """
    Load configuration with priority:
    1. Runtime config (GUI changes) - /data/runtime_config.json
    2. Addon config (HA options) - /data/options.json
    3. Defaults

    This ensures GUI changes persist across addon restarts (v1.2.0-beta.53)
    """
    try:
        base_config = None

        # First, try to load Home Assistant addon options
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, 'r') as f:
                base_config = json.load(f)
                logger.info(f"Addon configuration loaded from {CONFIG_PATH}")

        # Then, check for runtime config (GUI changes) and merge/override
        if os.path.exists(RUNTIME_CONFIG_PATH):
            with open(RUNTIME_CONFIG_PATH, 'r') as f:
                runtime_config = json.load(f)
                logger.info(f"Runtime configuration loaded from {RUNTIME_CONFIG_PATH}")

                if base_config:
                    # Merge: runtime config overrides addon config
                    base_config.update(runtime_config)
                    logger.info("✓ Runtime config merged with addon config (runtime takes priority)")
                else:
                    base_config = runtime_config
                    logger.info("✓ Using runtime config only")

        if base_config:
            # v1.0.5 - Normalize planes configuration (backward-compatible)
            config = normalize_planes_config(base_config)
            return config
        else:
            logger.warning(f"No config files found, using defaults")

    except Exception as e:
        logger.error(f"Error loading config: {e}")

    # Default configuration
    return {
        'inverter_ip': '192.168.80.76',
        'inverter_port': 1502,
        'installer_password': '',
        'master_password': '',
        'max_charge_power': 3900,
        'battery_capacity': 10.6,
        'log_level': 'info',
        'control_interval': 30,
        # v1.2.0-beta.51 - Mode switching hysteresis to prevent frequent changes
        'mode_switch_hysteresis_seconds': 900,  # 15 minutes minimum between mode changes
        'enable_tibber_optimization': True,
        'price_threshold': 0.85,
        'battery_soc_sensor': 'sensor.zwh8_8500_battery_soc',
        # v0.2.0 - Battery sensor options
        'battery_power_sensor': 'sensor.zwh8_8500_battery_power',
        'battery_voltage_sensor': '',
        'tibber_price_sensor': 'sensor.tibber_prices',
        'tibber_price_level_sensor': 'sensor.tibber_price_level_deutsch',
        'auto_optimization_enabled': True,
        'auto_charge_below_soc': 95,
        'auto_safety_soc': 20,
        # v0.2.1 - PV Production Sensors (Dual Roof)
        'pv_power_now_roof1': 'sensor.power_production_now_roof1',
        'pv_power_now_roof2': 'sensor.power_production_now_roof2',
        'pv_remaining_today_roof1': 'sensor.energy_production_today_remaining_roof1',
        'pv_remaining_today_roof2': 'sensor.energy_production_today_remaining_roof2',
        'pv_production_today_roof1': 'sensor.energy_production_today_roof1',
        'pv_production_today_roof2': 'sensor.energy_production_today_roof2',
        'pv_production_tomorrow_roof1': 'sensor.energy_production_tomorrow_roof1',
        'pv_production_tomorrow_roof2': 'sensor.energy_production_tomorrow_roof2',
        'pv_next_hour_roof1': 'sensor.energy_next_hour_roof1',
        'pv_next_hour_roof2': 'sensor.energy_next_hour_roof2',
        # v1.2.0-beta.40 - Energy sensors for accurate consumption calculation
        'grid_from_energy_sensor': 'sensor.ksem_total_active_energy_from_grid',
        'grid_to_energy_sensor': 'sensor.ksem_total_active_energy_to_grid',
        'battery_charge_from_grid_sensor': 'sensor.zwh8_8500_battery_charge_from_grid_total',
        'battery_charge_from_pv_sensor': 'sensor.zwh8_8500_battery_charge_from_pv_total',
        'battery_discharge_sensor': 'sensor.zwh8_8500_battery_discharge_total',
        'pv_dc1_inverter1_sensor': 'sensor.zwh8_8500_dc1_power',
        'pv_dc2_inverter1_sensor': 'sensor.zwh8_8500_dc2_power',
        'pv_dc1_inverter2_sensor': 'sensor.zhw8_7000_dc1_power',
        'pv_dc2_inverter2_sensor': 'sensor.zhw8_7000_dc2_power',
        # v0.3.0 - Tibber Smart Charging
        'tibber_price_threshold_1h': 8,
        'tibber_price_threshold_3h': 8,
        'charge_duration_per_10_percent': 18,
        'input_datetime_planned_charge_end': 'input_datetime.tibber_geplantes_ladeende',
        'input_datetime_planned_charge_start': 'input_datetime.tibber_geplanter_ladebeginn',
        # v1.0.5 - Forecast.Solar planes (array format)
        'forecast_solar_planes': [
            {'declination': 22, 'azimuth': 45, 'kwp': 8.96},
            {'declination': 22, 'azimuth': -135, 'kwp': 10.665}
        ]
    }

# Load configuration
config = load_config()

# Global state
app_state = {
    'controller_running': True,  # v0.2.5 - Automation ON by default
    'last_update': None,
    'battery': {
        'soc': 0,
        'power': 0,
        'voltage': 0
    },
    'inverter': {
        'connected': False,
        'mode': 'automatic',
        'control_mode': 'internal'
    },
    'price': {
        'current': 0.0,
        'average': 0.0,
        'level': 'unknown'
    },
    'forecast': {
        'today': 0.0,
        'tomorrow': 0.0
    },
    'charging_plan': {
        'planned_start': None,
        'planned_end': None,
        'last_calculated': None
    },
    'daily_battery_schedule': None,  # v0.9.0 - Full-day predictive plan
    'logs': []
}

def add_log(level, message):
    """Add log entry to state"""
    timestamp = datetime.now().isoformat()
    app_state['logs'].append({
        'timestamp': timestamp,
        'level': level,
        'message': message
    })
    # Keep only last 100 logs
    if len(app_state['logs']) > 100:
        app_state['logs'] = app_state['logs'][-100:]
    
    # Also log to logger
    if level == 'ERROR':
        logger.error(message)
    elif level == 'WARNING':
        logger.warning(message)
    else:
        logger.info(message)

# Import components
try:
    # Try relative import first
    try:
        from .core.kostal_api import KostalAPI
        from .core.modbus_client import ModbusClient
        from .core.ha_client import HomeAssistantClient
        from .core.tibber_optimizer import TibberOptimizer
        from .core.consumption_learner import ConsumptionLearner
        from .core.forecast_solar_api import ForecastSolarAPI  # v0.9.2
    except ImportError:
        # Fall back to absolute import
        import sys
        sys.path.insert(0, os.path.dirname(__file__))
        from core.kostal_api import KostalAPI
        from core.modbus_client import ModbusClient
        from core.ha_client import HomeAssistantClient
        from core.tibber_optimizer import TibberOptimizer
        from core.consumption_learner import ConsumptionLearner
        from core.forecast_solar_api import ForecastSolarAPI  # v0.9.2
    
    # Initialize components
    kostal_api = KostalAPI(
        config['inverter_ip'],
        config.get('installer_password', ''),
        config.get('master_password', '')
    )
    modbus_client = ModbusClient(
        config['inverter_ip'],
        config['inverter_port']
    )
    ha_client = HomeAssistantClient()
    tibber_optimizer = TibberOptimizer(config)

    # v0.4.0 - Initialize consumption learner
    consumption_learner = None
    if config.get('enable_consumption_learning', True):
        db_path = '/data/consumption_learning.db'
        learning_days = config.get('learning_period_days', 28)

        # Calculate fallback value
        # Priority: 1) default_hourly_consumption_fallback, 2) average_daily_consumption / 24, 3) 1.0
        default_fallback = config.get('default_hourly_consumption_fallback')
        if not default_fallback:
            avg_daily = config.get('average_daily_consumption')
            if avg_daily:
                default_fallback = float(avg_daily) / 24.0
                logger.info(f"Using average_daily_consumption {avg_daily} kWh/day → {default_fallback:.2f} kWh/h fallback")
            else:
                default_fallback = 1.0
                logger.info("No consumption baseline configured, using default 1.0 kWh/h fallback")

        consumption_learner = ConsumptionLearner(db_path, learning_days, default_fallback)

        # DISABLED: Cleanup duplicates - has critical bug that deletes all data
        # The duplicate handling is now done in queries instead (see get_hourly_profile, etc.)
        # TODO: Fix cleanup function and re-enable after thorough testing
        # try:
        #     deleted = consumption_learner.cleanup_duplicates()
        #     if deleted > 0:
        #         logger.info(f"Cleaned up {deleted} duplicate entries on startup")
        # except Exception as e:
        #     logger.error(f"Error cleaning up duplicates: {e}")

        # Load manual profile if provided
        manual_profile = config.get('manual_load_profile')
        if manual_profile:
            try:
                consumption_learner.add_manual_profile(manual_profile)
                add_log('INFO', f'Consumption learner initialized with manual profile ({learning_days} days)')
            except Exception as e:
                logger.error(f"Error loading manual profile: {e}")
                add_log('ERROR', f'Failed to load manual profile: {str(e)}')
        else:
            add_log('INFO', f'Consumption learner initialized (learning period: {learning_days} days, fallback: {default_fallback:.2f} kWh/h)')

        # Connect consumption learner to optimizer
        if tibber_optimizer:
            tibber_optimizer.set_consumption_learner(consumption_learner)

        # v1.2.0-beta.52: Log last 24h consumption calculation on startup
        try:
            # Get energy sensor configuration
            grid_from_energy_sensor = config.get('grid_from_energy_sensor')
            grid_to_energy_sensor = config.get('grid_to_energy_sensor')
            battery_charge_from_grid_sensor = config.get('battery_charge_from_grid_sensor')
            battery_charge_from_pv_sensor = config.get('battery_charge_from_pv_sensor')
            battery_discharge_sensor = config.get('battery_discharge_sensor')

            # Get PV energy sensors
            pv_energy_sensors = [
                config.get('pv_energy_pv1_inverter1_sensor'),
                config.get('pv_energy_pv2_inverter1_sensor'),
                config.get('pv_energy_pv1_inverter2_sensor'),
                config.get('pv_energy_pv2_inverter2_sensor')
            ]
            pv_energy_sensors = [s for s in pv_energy_sensors if s]

            # Only log if all required sensors are configured
            if (grid_from_energy_sensor and grid_to_energy_sensor and
                battery_charge_from_grid_sensor and battery_charge_from_pv_sensor and
                battery_discharge_sensor and ha_client):

                logger.info("Logging last 24h consumption calculation on startup...")
                consumption_learner.log_last_24h_calculation(
                    ha_client,
                    grid_from_energy_sensor,
                    grid_to_energy_sensor,
                    battery_charge_from_grid_sensor,
                    battery_charge_from_pv_sensor,
                    battery_discharge_sensor,
                    pv_energy_sensors
                )
            else:
                logger.info("Skipping 24h calculation log: Not all required sensors configured")
        except Exception as e:
            logger.error(f"Error logging 24h calculation on startup: {e}")

    # v0.9.2 - Initialize Forecast.Solar Professional API if enabled
    forecast_solar_api = None
    if config.get('enable_forecast_solar_api', False):
        try:
            api_key = config.get('forecast_solar_api_key')
            latitude = config.get('forecast_solar_latitude')
            longitude = config.get('forecast_solar_longitude')
            planes = config.get('forecast_solar_planes', [])

            if api_key and latitude is not None and longitude is not None:
                forecast_solar_api = ForecastSolarAPI(api_key, latitude, longitude)

                # Connect to optimizer
                if tibber_optimizer:
                    tibber_optimizer.set_forecast_solar_api(forecast_solar_api)

                # v1.0.4 - Check if planes are configured
                if planes:
                    add_log('INFO', f'Forecast.Solar Professional API enabled (lat={latitude}, lon={longitude}, {len(planes)} planes)')
                else:
                    logger.warning("Forecast.Solar API enabled but no planes configured")
                    add_log('WARNING', 'Forecast.Solar API enabled but no planes configured')
            else:
                logger.warning("Forecast.Solar API enabled but missing configuration (api_key, latitude, longitude)")
                add_log('WARNING', 'Forecast.Solar API: Missing configuration parameters')

        except Exception as e:
            logger.error(f"Error initializing Forecast.Solar API: {e}")
            add_log('ERROR', f'Failed to initialize Forecast.Solar API: {str(e)}')

    add_log('INFO', 'Components initialized successfully')
    add_log('INFO', 'Tibber Optimizer initialized')

    # Automatic connection test on startup
    if kostal_api:
        logger.info("Testing Kostal connection on startup...")
        result = kostal_api.test_connection()
        if result:
            app_state['inverter']['connected'] = True
            add_log('INFO', 'Connection test successful - Inverter connected')
        else:
            app_state['inverter']['connected'] = False
            add_log('WARNING', 'Connection test failed - Check inverter IP and network')
except ImportError as e:
    logger.warning(f"Could not import components: {e}")
    kostal_api = None
    modbus_client = None
    ha_client = None
    tibber_optimizer = None
    consumption_learner = None
    add_log('WARNING', 'Running in development mode - components not available')
except Exception as e:
    logger.error(f"Error initializing components: {e}")
    kostal_api = None
    modbus_client = None
    ha_client = None
    tibber_optimizer = None
    consumption_learner = None
    add_log('ERROR', f'Failed to initialize components: {str(e)}')

# ==============================================================================
# Web Routes
# ==============================================================================

@app.route('/')
def index():
    """Main dashboard"""
    # base_path is injected by context processor
    return render_template('dashboard.html', config=config, state=app_state)

@app.route('/config')
def config_page():
    """Configuration page"""
    # base_path is injected by context processor
    return render_template('config.html', config=config)

@app.route('/logs')
def logs_page():
    """Logs page"""
    # base_path is injected by context processor
    return render_template('logs.html', logs=app_state['logs'])

@app.route('/consumption_import')
def consumption_import_page():
    """Consumption data import page (v0.5.0)"""
    # base_path is injected by context processor
    return render_template('consumption_import.html')

@app.route('/debug_ingress')
def debug_ingress():
    """Debug route to show what Flask sees from Ingress"""
    from flask import url_for
    debug_info = {
        'request.url': request.url,
        'request.base_url': request.base_url,
        'request.url_root': request.url_root,
        'request.path': request.path,
        'request.script_root': request.script_root,
        'request.environ.SCRIPT_NAME': request.environ.get('SCRIPT_NAME', 'NOT SET'),
        'request.environ.PATH_INFO': request.environ.get('PATH_INFO', 'NOT SET'),
        'url_for("static", filename="css/style.css")': url_for('static', filename='css/style.css'),
        'url_for("index")': url_for('index'),
        'headers': dict(request.headers)
    }
    html = '<html><head><title>Debug Ingress</title></head><body>'
    html += '<h1>Flask Ingress Debug Info</h1>'
    html += '<table border="1" cellpadding="5">'
    for key, value in debug_info.items():
        html += f'<tr><td><b>{key}</b></td><td>{value}</td></tr>'
    html += '</table>'
    html += '</body></html>'
    return html

@app.route('/debug_consumption')
def debug_consumption_html():
    """Debug: Show all consumption data as HTML table"""
    try:
        import sqlite3
        with sqlite3.connect(consumption_learner.db_path) as conn:
            cursor = conn.execute("""
                SELECT DATE(timestamp) as date, COUNT(*) as hour_count,
                       MIN(hour) as first_hour, MAX(hour) as last_hour,
                       SUM(CASE WHEN is_manual = 1 THEN 1 ELSE 0 END) as manual_count
                FROM hourly_consumption
                GROUP BY DATE(timestamp)
                ORDER BY date DESC
            """)

            rows = cursor.fetchall()

            # Total count
            cursor = conn.execute("SELECT COUNT(*), SUM(CASE WHEN is_manual = 1 THEN 1 ELSE 0 END) FROM hourly_consumption")
            total, manual_total = cursor.fetchone()

            html = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <title>Consumption Data Debug</title>
                <style>
                    body {{ font-family: Arial; background: #1a1a2e; color: #eee; padding: 2rem; }}
                    h1 {{ color: #4CAF50; }}
                    table {{ border-collapse: collapse; width: 100%; margin-top: 1rem; }}
                    th, td {{ border: 1px solid #444; padding: 0.5rem; text-align: left; }}
                    th {{ background: #333; }}
                    .total {{ background: #2a2a3e; font-weight: bold; }}
                </style>
            </head>
            <body>
                <h1>🔍 Consumption Data Debug</h1>
                <p><strong>Total:</strong> {total} Stunden ({manual_total} manuell, {total - manual_total} gelernt)</p>
                <table>
                    <tr>
                        <th>Datum</th>
                        <th>Stunden</th>
                        <th>Erste Stunde</th>
                        <th>Letzte Stunde</th>
                        <th>Manuell</th>
                    </tr>
            """

            for row in rows:
                html += f"""
                    <tr>
                        <td>{row[0]}</td>
                        <td>{row[1]}/24</td>
                        <td>{row[2]}</td>
                        <td>{row[3]}</td>
                        <td>{row[4]}</td>
                    </tr>
                """

            html += """
                </table>
                <p style="margin-top: 2rem;"><a href="./" style="color: #4CAF50;">← Zurück zum Dashboard</a></p>
            </body>
            </html>
            """

            return html
    except Exception as e:
        return f"<h1>Error</h1><p>{str(e)}</p>", 500

@app.route('/api/debug_consumption_all')
def debug_consumption_all():
    """Debug: Show all consumption data from DB (JSON)"""
    try:
        import sqlite3
        with sqlite3.connect(consumption_learner.db_path) as conn:
            cursor = conn.execute("""
                SELECT DATE(timestamp) as date, COUNT(*) as hour_count,
                       MIN(timestamp) as first_hour, MAX(timestamp) as last_hour
                FROM hourly_consumption
                GROUP BY DATE(timestamp)
                ORDER BY date DESC
            """)

            dates = []
            for row in cursor.fetchall():
                dates.append({
                    'date': row[0],
                    'hour_count': row[1],
                    'first_hour': row[2],
                    'last_hour': row[3]
                })

            # Total count
            cursor = conn.execute("SELECT COUNT(*) FROM hourly_consumption")
            total = cursor.fetchone()[0]

            return jsonify({
                'total_hours': total,
                'dates': dates
            })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/debug_consumption/<date>')
def debug_consumption(date):
    """Debug: Show raw DB data for a specific date"""
    try:
        import sqlite3
        with sqlite3.connect(consumption_learner.db_path) as conn:
            cursor = conn.execute("""
                SELECT timestamp, hour, consumption_kwh, is_manual, created_at
                FROM hourly_consumption
                WHERE DATE(timestamp) = ?
                ORDER BY hour
            """, (date,))

            rows = cursor.fetchall()
            result = {
                'date': date,
                'count': len(rows),
                'hours': []
            }

            for row in rows:
                result['hours'].append({
                    'timestamp': row[0],
                    'hour': row[1],
                    'consumption_kwh': row[2],
                    'is_manual': row[3],
                    'created_at': row[4]
                })

            return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/test')
def test_page():
    """Test route to verify new routes work"""
    return "<h1>Test Route funktioniert!</h1><p>Wenn du das siehst, funktionieren neue Routen.</p><p><a href='/'>Zurück zum Dashboard</a></p>"

# ==============================================================================
# API Endpoints
# ==============================================================================

@app.route('/api/status')
def api_status():
    """Get current status"""
    app_state['last_update'] = datetime.now().isoformat()

    # Try to read battery SOC from Home Assistant
    if ha_client:
        try:
            soc = ha_client.get_state(config.get('battery_soc_sensor', 'sensor.zwh8_8500_battery_soc'))
            if soc and soc not in ['unknown', 'unavailable']:
                app_state['battery']['soc'] = float(soc)
        except Exception as e:
            logger.debug(f"Could not read battery SOC: {e}")

        # Read battery power (v0.2.0)
        try:
            battery_power_sensor = config.get('battery_power_sensor')
            if battery_power_sensor:
                power = ha_client.get_state(battery_power_sensor)
                if power and power not in ['unknown', 'unavailable']:
                    app_state['battery']['power'] = float(power)
        except Exception as e:
            logger.debug(f"Could not read battery power: {e}")

        # Read battery voltage (v0.2.0)
        try:
            battery_voltage_sensor = config.get('battery_voltage_sensor')
            if battery_voltage_sensor:
                voltage = ha_client.get_state(battery_voltage_sensor)
                if voltage and voltage not in ['unknown', 'unavailable']:
                    app_state['battery']['voltage'] = float(voltage)
        except Exception as e:
            logger.debug(f"Could not read battery voltage: {e}")

        # Read current Tibber price (v0.2.1 - simplified)
        try:
            # Current price from main Tibber sensor
            tibber_sensor = config.get('tibber_price_sensor', 'sensor.tibber_prices')
            current_price = ha_client.get_state(tibber_sensor)
            if current_price and current_price not in ['unknown', 'unavailable']:
                app_state['price']['current'] = float(current_price)

            # Price level from separate German sensor
            tibber_level_sensor = config.get('tibber_price_level_sensor', 'sensor.tibber_price_level_deutsch')
            if tibber_level_sensor:
                price_level = ha_client.get_state(tibber_level_sensor)
                if price_level and price_level not in ['unknown', 'unavailable']:
                    app_state['price']['level'] = price_level

            # Calculate average price from attributes
            prices_data = ha_client.get_state_with_attributes(tibber_sensor)
            if prices_data and 'attributes' in prices_data:
                today_prices = prices_data['attributes'].get('today', [])
                if today_prices and isinstance(today_prices, list):
                    avg = sum(p.get('total', 0) for p in today_prices) / len(today_prices)
                    app_state['price']['average'] = float(avg)
        except Exception as e:
            logger.debug(f"Could not read Tibber price: {e}")

        # Read PV forecast data (v0.2.1)
        # v1.2.0-beta.54: Use Forecast.Solar API data from daily_battery_schedule
        try:
            # Get PV forecast from daily_battery_schedule if available
            schedule = app_state.get('daily_battery_schedule')
            if schedule and 'hourly_pv' in schedule:
                # Sum today's PV forecast (next 24 hours from NOW)
                hourly_pv = schedule.get('hourly_pv', [])

                # Calculate remaining today (from now until midnight)
                now = datetime.now()
                hours_until_midnight = 24 - now.hour
                pv_remaining_today = sum(hourly_pv[:hours_until_midnight]) if len(hourly_pv) >= hours_until_midnight else 0

                # Calculate tomorrow's total (24 hours after midnight)
                pv_tomorrow = sum(hourly_pv[hours_until_midnight:hours_until_midnight + 24]) if len(hourly_pv) >= hours_until_midnight + 24 else 0

                # Update app state
                app_state['forecast']['today'] = pv_remaining_today
                app_state['forecast']['tomorrow'] = pv_tomorrow

                # Current PV power (if available from sensor)
                pv_total_sensor = config.get('pv_total_sensor')
                if pv_total_sensor:
                    power = ha_client.get_state(pv_total_sensor)
                    if power and power not in ['unknown', 'unavailable']:
                        pv_power_now = float(power) / 1000  # Convert W to kW
                        app_state['pv'] = {
                            'power_now': pv_power_now,
                            'remaining_today': pv_remaining_today
                        }
            else:
                # Fallback: No forecast data available yet
                app_state['forecast']['today'] = 0
                app_state['forecast']['tomorrow'] = 0
                app_state['pv'] = {'power_now': 0, 'remaining_today': 0}
        except Exception as e:
            logger.debug(f"Could not read PV data: {e}")

    return jsonify({
        'status': 'ok',
        'timestamp': app_state['last_update'],
        'controller_running': app_state['controller_running'],
        'inverter': app_state['inverter'],
        'battery': app_state['battery'],
        'price': app_state['price'],
        'forecast': app_state['forecast'],
        'pv': app_state.get('pv', {'power_now': 0, 'remaining_today': 0}),
        'charging_plan': app_state.get('charging_plan', {})
    })

@app.route('/api/config', methods=['GET', 'POST'])
def api_config():
    """Get or update configuration"""
    global config

    if request.method == 'POST':
        try:
            new_config = request.json

            # v1.2.0-beta.53 - Save to runtime config to persist across addon restarts
            # Runtime config takes priority over addon config
            # This prevents HA from overwriting GUI changes on restart

            # Update in-memory configuration
            config.update(new_config)

            # Save to runtime config file (persists across restarts)
            with open(RUNTIME_CONFIG_PATH, 'w') as f:
                json.dump(config, f, indent=2)

            logger.info(f"✓ Configuration saved to {RUNTIME_CONFIG_PATH}")

            # Reload config to ensure consistency
            config = load_config()

            add_log('INFO', 'Configuration updated and saved (persists across restarts)')
            return jsonify({
                'status': 'ok',
                'message': 'Configuration saved successfully. Changes will persist across addon restarts.'
            })
        except Exception as e:
            add_log('ERROR', f'Failed to save configuration: {str(e)}')
            return jsonify({
                'status': 'error',
                'message': str(e)
            }), 500

    return jsonify(config)

@app.route('/api/control', methods=['POST'])
def api_control():
    """Manual control endpoint"""
    data = request.json
    action = data.get('action')
    
    add_log('INFO', f'Control action received: {action}')
    
    try:
        if action == 'start_charging':
            # Start manual charging
            if kostal_api and modbus_client:
                # Set external control mode
                kostal_api.set_external_control(True)
                # Get charge power from request or use max_charge_power as fallback
                requested_power = data.get('power', config['max_charge_power'])
                charge_power = -abs(int(requested_power))
                modbus_client.write_battery_power(charge_power)

                app_state['inverter']['mode'] = 'manual_charging'
                app_state['inverter']['control_mode'] = 'external'
                add_log('INFO', f'Manual charging started: {charge_power}W')
            else:
                add_log('WARNING', 'Components not available - cannot start charging')
                
        elif action == 'stop_charging':
            # Stop charging, back to internal control
            if kostal_api and modbus_client:
                modbus_client.write_battery_power(0)
                kostal_api.set_external_control(False)
                
                app_state['inverter']['mode'] = 'automatic'
                app_state['inverter']['control_mode'] = 'internal'
                add_log('INFO', 'Charging stopped, back to internal control')
            else:
                add_log('WARNING', 'Components not available - cannot stop charging')
                
        elif action == 'auto_mode':
            # Enable automatic optimization
            app_state['controller_running'] = True
            app_state['inverter']['mode'] = 'automatic'
            add_log('INFO', 'Automatic optimization mode enabled')

        elif action == 'toggle_automation':
            # v0.2.5 - Toggle automation on/off
            enabled = data.get('enabled', True)
            app_state['controller_running'] = enabled
            if enabled:
                add_log('INFO', 'Automatik aktiviert')
            else:
                add_log('INFO', 'Automatik deaktiviert')

        elif action == 'test_connection':
            # Test connection to inverter
            if kostal_api:
                logger.info("Testing Kostal connection...")
                result = kostal_api.test_connection()
                if result:
                    app_state['inverter']['connected'] = True
                    add_log('INFO', '✅ Connection test successful')
                else:
                    app_state['inverter']['connected'] = False
                    add_log('ERROR', '❌ Connection test failed')
            else:
                add_log('WARNING', 'Components not available - cannot test connection')
        
        else:
            add_log('WARNING', f'Unknown action: {action}')
            return jsonify({
                'status': 'error',
                'message': f'Unknown action: {action}'
            }), 400
        
        return jsonify({
            'status': 'ok',
            'action': action,
            'message': 'Action executed successfully'
        })
        
    except Exception as e:
        add_log('ERROR', f'Error executing action {action}: {str(e)}')
        logger.exception(e)
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500

@app.route('/api/logs')
def api_logs():
    """Get logs"""
    return jsonify({
        'logs': app_state['logs']
    })

@app.route('/api/charging_plan')
def api_charging_plan():
    """Get current charging plan (v0.3.0)"""
    plan = app_state.get('charging_plan', {})

    # Format für Frontend
    response = {
        'has_plan': plan.get('planned_start') is not None,
        'planned_start': plan.get('planned_start'),
        'planned_end': plan.get('planned_end'),
        'last_calculated': plan.get('last_calculated')
    }

    return jsonify(response)

@app.route('/api/recalculate_plan', methods=['POST'])
def api_recalculate_plan():
    """Manually trigger charging plan recalculation (v0.3.2)"""
    try:
        add_log('INFO', 'Manual charging plan recalculation triggered')
        update_charging_plan()
        return jsonify({
            'status': 'ok',
            'message': 'Charging plan recalculated'
        })
    except Exception as e:
        logger.error(f"Error in manual recalculation: {e}")
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500

@app.route('/api/charging_status')
def api_charging_status():
    """Get detailed charging status explanation (v0.3.6)"""
    try:
        status = get_charging_status_explanation()
        return jsonify(status)
    except Exception as e:
        logger.error(f"Error getting charging status: {e}")
        return jsonify({
            'explanation': 'Fehler beim Abrufen des Status',
            'will_charge': False,
            'conditions': {},
            'current_soc': 0,
            'target_soc': 0,
            'pv_remaining': 0,
            'planned_start': None,
            'planned_end': None
        }), 500

def get_historical_soc_interpolated(ha_client, soc_sensor: str, hours: int = 24) -> List[float]:
    """
    Get historical SOC values for the last N hours with linear interpolation (v1.2.0-beta.14)

    The SOC sensor typically has sparse data points (only when value changes).
    This function interpolates between data points to get one value per hour.

    Args:
        ha_client: Home Assistant client
        soc_sensor: SOC sensor entity ID (e.g., 'sensor.zwh8_8500_battery_soc')
        hours: Number of hours to retrieve (default: 24)

    Returns:
        List[float]: SOC values for each hour (24 values from oldest to newest)
                     Returns None if error or no data
    """
    try:
        from datetime import datetime, timedelta

        now = datetime.now().astimezone()
        start_time = now - timedelta(hours=hours)

        # Fetch SOC history
        logger.info(f"Fetching historical SOC from {soc_sensor} for last {hours} hours...")
        history = ha_client.get_history(soc_sensor, start_time, now)

        if not history or len(history) == 0:
            logger.warning(f"No historical SOC data available from {soc_sensor}")
            return None

        # Extract valid data points with timestamps
        data_points = []  # List of (timestamp, soc_value)
        for entry in history:
            try:
                state = entry.get('state')
                if state not in ['unknown', 'unavailable', None, '']:
                    soc_value = float(state)

                    # Validate SOC range
                    if not (0 <= soc_value <= 100):
                        continue

                    # Parse timestamp
                    timestamp_str = entry.get('last_changed') or entry.get('last_updated')
                    if timestamp_str:
                        ts = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
                        data_points.append((ts, soc_value))
            except (ValueError, TypeError):
                continue

        if not data_points:
            logger.warning(f"No valid SOC data points found in history")
            return None

        # Sort by timestamp
        data_points.sort(key=lambda x: x[0])

        logger.info(f"Found {len(data_points)} SOC data points, interpolating to {hours} hourly values...")
        logger.debug(f"SOC range: {data_points[0][1]:.1f}% to {data_points[-1][1]:.1f}%")

        # Create target timestamps for each hour (start of hour)
        target_timestamps = []
        for i in range(hours):
            target_time = start_time + timedelta(hours=i)
            # Round down to start of hour
            target_time = target_time.replace(minute=0, second=0, microsecond=0)
            target_timestamps.append(target_time)

        # Interpolate SOC for each target hour
        interpolated_soc = []

        for target_ts in target_timestamps:
            # Find the two nearest data points (before and after target)
            before_point = None
            after_point = None

            for ts, soc in data_points:
                if ts <= target_ts:
                    before_point = (ts, soc)
                if ts >= target_ts and after_point is None:
                    after_point = (ts, soc)
                    break

            # Interpolate
            if before_point and after_point and before_point[0] != after_point[0]:
                # Linear interpolation between two points
                t1, soc1 = before_point
                t2, soc2 = after_point
                time_diff = (t2 - t1).total_seconds()
                time_to_target = (target_ts - t1).total_seconds()
                ratio = time_to_target / time_diff
                interpolated_value = soc1 + (soc2 - soc1) * ratio
                interpolated_soc.append(round(interpolated_value, 2))
            elif before_point:
                # Use the before point (extrapolate)
                interpolated_soc.append(before_point[1])
            elif after_point:
                # Use the after point (extrapolate)
                interpolated_soc.append(after_point[1])
            else:
                # No data at all, use fallback
                logger.warning(f"No SOC data for hour {target_ts}, using 50% as fallback")
                interpolated_soc.append(50.0)

        logger.info(f"✓ Historical SOC interpolated: {len(interpolated_soc)} hourly values from {interpolated_soc[0]:.1f}% to {interpolated_soc[-1]:.1f}%")

        return interpolated_soc

    except Exception as e:
        logger.error(f"Error getting historical SOC: {e}", exc_info=True)
        return None


def get_historical_pv_hourly(ha_client, pv_sensor: str, hours: int = 24) -> List[float]:
    """
    Get historical PV production values for the last N hours (v1.2.0-beta.37)

    Calculates hourly energy production in kWh by integrating power data from sensor.
    For past hours: uses real historical data from Home Assistant
    For future hours: will be filled from Forecast.Solar API

    Args:
        ha_client: Home Assistant client
        pv_sensor: PV power sensor entity ID (e.g., 'sensor.ksem_sum_pv_power_inverter_dc')
        hours: Number of hours to retrieve (default: 24)

    Returns:
        List[float]: PV energy production in kWh for each hour (24 values)
                     Returns None if error or no data
    """
    try:
        from datetime import datetime, timedelta

        now = datetime.now().astimezone()
        start_time = now - timedelta(hours=hours)

        # Fetch PV history
        logger.info(f"Fetching historical PV from {pv_sensor} for last {hours} hours...")
        history = ha_client.get_history(pv_sensor, start_time, now)

        if not history or len(history) == 0:
            logger.warning(f"No historical PV data available from {pv_sensor}")
            return None

        # Extract valid data points with timestamps
        # Power sensor typically reports in W, we need to convert to kWh per hour
        data_points = []  # List of (timestamp, power_w)
        for entry in history:
            try:
                state = entry.get('state')
                if state not in ['unknown', 'unavailable', None, '']:
                    power_value = float(state)

                    # Validate power range (0 to reasonable max, e.g., 20 kW)
                    if not (0 <= power_value <= 20000):
                        continue

                    # Parse timestamp
                    timestamp_str = entry.get('last_changed') or entry.get('last_updated')
                    if timestamp_str:
                        ts = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
                        data_points.append((ts, power_value))
            except (ValueError, TypeError):
                continue

        if not data_points:
            logger.warning(f"No valid PV data points found in history")
            return None

        # Sort by timestamp
        data_points.sort(key=lambda x: x[0])

        logger.info(f"Found {len(data_points)} PV data points, calculating hourly energy...")

        # Calculate energy for each hour by integrating power over time
        hourly_energy = []

        for i in range(hours):
            hour_start = start_time + timedelta(hours=i)
            hour_end = hour_start + timedelta(hours=1)

            # Round to hour boundaries
            hour_start = hour_start.replace(minute=0, second=0, microsecond=0)
            hour_end = hour_end.replace(minute=0, second=0, microsecond=0)

            # Find all data points within this hour
            hour_points = [(ts, power) for ts, power in data_points if hour_start <= ts < hour_end]

            if not hour_points:
                # No data for this hour - check if before or after sunrise
                # Use 0 for now (nighttime), will improve with solar position later
                hourly_energy.append(0.0)
                continue

            # Calculate energy by trapezoidal integration
            # Energy (kWh) = integral of Power (W) over time, divided by 1000
            total_energy_wh = 0.0

            # Add boundary points for accurate integration
            if hour_points[0][0] > hour_start:
                # Extrapolate first point back to hour start (assume same power)
                hour_points.insert(0, (hour_start, hour_points[0][1]))

            if hour_points[-1][0] < hour_end:
                # Extrapolate last point to hour end
                hour_points.append((hour_end, hour_points[-1][1]))

            # Trapezoidal rule: E = sum((P1 + P2)/2 * dt)
            for j in range(len(hour_points) - 1):
                t1, p1 = hour_points[j]
                t2, p2 = hour_points[j + 1]
                dt_seconds = (t2 - t1).total_seconds()
                avg_power_w = (p1 + p2) / 2
                energy_wh = avg_power_w * (dt_seconds / 3600)  # Wh = W * hours
                total_energy_wh += energy_wh

            # Convert Wh to kWh
            energy_kwh = total_energy_wh / 1000
            hourly_energy.append(round(energy_kwh, 2))

        total_pv = sum(hourly_energy)
        logger.info(f"✓ Historical PV calculated: {len(hourly_energy)} hourly values, total {total_pv:.2f} kWh")

        return hourly_energy

    except Exception as e:
        logger.error(f"Error getting historical PV: {e}", exc_info=True)
        return None


def get_historical_charging_hourly(ha_client, charge_sensor: str, hours: int = 24) -> List[float]:
    """
    Get historical battery charging values for the last N hours

    Calculates hourly energy charged in kWh from cumulative energy sensor.
    Uses delta calculation: end_value - start_value for each hour.

    Args:
        ha_client: Home Assistant client
        charge_sensor: Battery charge energy sensor entity ID (cumulative kWh)
        hours: Number of hours to retrieve (default: 24)

    Returns:
        List[float]: Battery charging energy in kWh for each hour (24 values)
                     Returns None if error or no data
    """
    try:
        from datetime import datetime, timedelta

        now = datetime.now().astimezone()
        start_time = now - timedelta(hours=hours)

        # Fetch charging history
        logger.info(f"Fetching historical charging from {charge_sensor} for last {hours} hours...")
        history = ha_client.get_history(charge_sensor, start_time, now)

        if not history or len(history) == 0:
            logger.warning(f"No historical charging data available from {charge_sensor}")
            return None

        # Extract valid data points with timestamps
        data_points = []  # List of (timestamp, energy_kwh)
        for entry in history:
            try:
                state = entry.get('state')
                if state not in ['unknown', 'unavailable', None, '']:
                    energy_value = float(state)

                    # Validate energy range (0 to reasonable max, e.g., 10000 kWh cumulative)
                    if not (0 <= energy_value <= 10000):
                        continue

                    # Parse timestamp
                    timestamp_str = entry.get('last_changed') or entry.get('last_updated')
                    if timestamp_str:
                        ts = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
                        data_points.append((ts, energy_value))
            except (ValueError, TypeError):
                continue

        if not data_points:
            logger.warning(f"No valid charging data points found in history")
            return None

        # Sort by timestamp
        data_points.sort(key=lambda x: x[0])

        logger.info(f"Found {len(data_points)} charging data points, calculating hourly deltas...")

        # Calculate energy delta for each hour
        hourly_charging = []

        for i in range(hours):
            hour_start = start_time + timedelta(hours=i)
            hour_end = hour_start + timedelta(hours=1)

            # Round to hour boundaries
            hour_start = hour_start.replace(minute=0, second=0, microsecond=0)
            hour_end = hour_end.replace(minute=0, second=0, microsecond=0)

            # Find the last reading at or before hour_start
            start_value = None
            for ts, val in data_points:
                if ts <= hour_start:
                    start_value = val
                else:
                    break

            # Find the last reading at or before hour_end
            end_value = None
            for ts, val in data_points:
                if ts <= hour_end:
                    end_value = val
                else:
                    break

            # Calculate delta
            if start_value is not None and end_value is not None:
                delta = end_value - start_value
                # Validate delta (should be >= 0 and reasonable, e.g., < 10 kWh per hour)
                if 0 <= delta <= 10:
                    hourly_charging.append(round(delta, 2))
                else:
                    # Invalid delta, use 0
                    hourly_charging.append(0.0)
            else:
                # No data for this hour
                hourly_charging.append(0.0)

        total_charging = sum(hourly_charging)
        logger.info(f"✓ Historical charging calculated: {len(hourly_charging)} hourly values, total {total_charging:.2f} kWh")

        return hourly_charging

    except Exception as e:
        logger.error(f"Error getting historical charging: {e}", exc_info=True)
        return None


@app.route('/api/battery_schedule')
def api_battery_schedule():
    """Get battery data for rolling 49h window centered on current hour (v1.2.0-beta.51)"""
    try:
        from datetime import datetime, timedelta

        now = datetime.now().astimezone()
        current_hour = now.hour

        # Get forecast (next 24h rolling from NOW)
        forecast_plan = app_state.get('daily_battery_schedule')

        # Get historical SOC data (last 24h) with interpolation
        soc_sensor = config.get('battery_soc_sensor', 'sensor.zwh8_8500_battery_soc')
        historical_soc = get_historical_soc_interpolated(ha_client, soc_sensor, hours=24)

        # Get historical PV data (last 24h) with hourly integration (v1.2.0-beta.37)
        pv_sensor = config.get('pv_total_sensor', 'sensor.ksem_sum_pv_power_inverter_dc')
        historical_pv = get_historical_pv_hourly(ha_client, pv_sensor, hours=24)

        # Get historical charging data (last 24h) from battery charge sensor
        charge_sensor = config.get('battery_charge_from_grid_sensor', 'sensor.zwh8_8500_battery_charge_from_grid_total')
        historical_charging = get_historical_charging_hourly(ha_client, charge_sensor, hours=24)

        # Build 49-hour rolling window: 24h back + current + 24h forward (v1.2.0-beta.51)
        if forecast_plan and historical_soc:
            # Get forecast data (24h rolling from NOW)
            forecast_soc = forecast_plan.get('hourly_soc', [])
            forecast_charging = forecast_plan.get('hourly_charging', [])
            forecast_pv = forecast_plan.get('hourly_pv', [])
            forecast_consumption = forecast_plan.get('hourly_consumption', [])
            forecast_prices = forecast_plan.get('hourly_prices', [])

            # Generate labels for rolling window (v1.2.0-beta.51)
            labels = []
            start_offset = current_hour - 24

            for offset in range(start_offset, start_offset + 49):
                actual_offset = offset

                if actual_offset < 0:
                    # Yesterday
                    hour = (24 + actual_offset) % 24
                    labels.append(f"Gestern {hour:02d}:00")
                elif actual_offset < 24:
                    # Today
                    hour = actual_offset
                    if offset == current_hour:
                        labels.append(f"► JETZT {hour:02d}:00")
                    else:
                        labels.append(f"Heute {hour:02d}:00")
                elif actual_offset < 48:
                    # Tomorrow
                    hour = actual_offset - 24
                    labels.append(f"Morgen {hour:02d}:00")
                else:
                    # Day after tomorrow
                    hour = actual_offset - 48
                    labels.append(f"Übermorgen {hour:02d}:00")

            # Initialize 49-hour arrays
            rolling_soc = []
            rolling_charging = []
            rolling_pv = []
            rolling_consumption = []
            rolling_prices = []

            # Build rolling window data
            # historical_soc contains last 24 hours: [yesterday current_hour, ..., yesterday 23:00, today 00:00, ..., today (current_hour-1)]
            # We need: [yesterday current_hour, ..., today current_hour (NOW), ..., tomorrow current_hour]

            for i in range(49):
                if i < 24:
                    # Historical data (yesterday and earlier today)
                    # i=0: yesterday current_hour
                    # i=23: today current_hour-1
                    if historical_soc and i < len(historical_soc):
                        rolling_soc.append(historical_soc[i])

                        # Use historical charging data if available
                        if historical_charging and i < len(historical_charging):
                            rolling_charging.append(historical_charging[i])
                        else:
                            rolling_charging.append(0.0)

                        if historical_pv and i < len(historical_pv):
                            rolling_pv.append(historical_pv[i])
                        else:
                            rolling_pv.append(0.0)

                        rolling_consumption.append(0.0)  # Could add historical consumption if available
                        rolling_prices.append(0.30)  # Default price for historical
                    else:
                        # Fallback if no historical data
                        current_soc = app_state['battery'].get('soc', 50)
                        rolling_soc.append(current_soc)
                        rolling_charging.append(0.0)
                        rolling_pv.append(0.0)
                        rolling_consumption.append(0.0)
                        rolling_prices.append(0.30)

                else:
                    # Current hour and future (forecast data)
                    # i=24: current hour (NOW) = forecast[0]
                    # i=25: current hour + 1 = forecast[1]
                    # i=48: current hour + 24 = forecast[24] (but forecast only has 24 values, so this would be out of range)
                    forecast_index = i - 24

                    if forecast_index < len(forecast_soc):
                        rolling_soc.append(forecast_soc[forecast_index])
                        rolling_charging.append(forecast_charging[forecast_index] if forecast_index < len(forecast_charging) else 0.0)
                        rolling_pv.append(forecast_pv[forecast_index] if forecast_index < len(forecast_pv) else 0.0)
                        rolling_consumption.append(forecast_consumption[forecast_index] if forecast_index < len(forecast_consumption) else 0.0)
                        rolling_prices.append(forecast_prices[forecast_index] if forecast_index < len(forecast_prices) else 0.30)
                    else:
                        # Beyond forecast range (should only be i=48)
                        last_soc = rolling_soc[-1] if rolling_soc else app_state['battery'].get('soc', 50)
                        rolling_soc.append(last_soc)
                        rolling_charging.append(0.0)
                        rolling_pv.append(0.0)
                        rolling_consumption.append(0.0)
                        rolling_prices.append(0.30)

            # Adjust charging windows to rolling window indices
            # Forecast charging windows are indexed 0-23 (0 = NOW)
            # In rolling window, NOW = index 24
            adjusted_windows = []
            for window in forecast_plan.get('charging_windows', []):
                forecast_hour = window['hour']
                rolling_index = 24 + forecast_hour  # Map forecast hour to rolling window index

                if rolling_index < 49:
                    adjusted_window = window.copy()
                    adjusted_window['hour'] = rolling_index
                    adjusted_windows.append(adjusted_window)

            return jsonify({
                'labels': labels,  # v1.2.0-beta.51: Rolling window labels
                'hourly_soc': rolling_soc,
                'hourly_charging': rolling_charging,
                'hourly_pv': rolling_pv,
                'hourly_consumption': rolling_consumption,
                'hourly_prices': rolling_prices,
                'charging_windows': adjusted_windows,
                'current_hour_index': 24,  # Current hour always at index 24
                'last_planned': forecast_plan.get('last_planned'),
                'start_time': forecast_plan.get('start_time'),
                'min_soc_reached': forecast_plan.get('min_soc_reached', 0),
                'total_charging_kwh': forecast_plan.get('total_charging_kwh', 0)
            })

        # Fallback: only forecast available, no historical data (v1.2.0-beta.51)
        elif forecast_plan:
            # Get forecast data (24h rolling from NOW)
            forecast_soc = forecast_plan.get('hourly_soc', [])
            forecast_charging = forecast_plan.get('hourly_charging', [])
            forecast_pv = forecast_plan.get('hourly_pv', [])
            forecast_consumption = forecast_plan.get('hourly_consumption', [])
            forecast_prices = forecast_plan.get('hourly_prices', [])

            # Generate labels for rolling window (v1.2.0-beta.51)
            labels = []
            start_offset = current_hour - 24

            for offset in range(start_offset, start_offset + 49):
                actual_offset = offset

                if actual_offset < 0:
                    hour = (24 + actual_offset) % 24
                    labels.append(f"Gestern {hour:02d}:00")
                elif actual_offset < 24:
                    hour = actual_offset
                    if offset == current_hour:
                        labels.append(f"► JETZT {hour:02d}:00")
                    else:
                        labels.append(f"Heute {hour:02d}:00")
                elif actual_offset < 48:
                    hour = actual_offset - 24
                    labels.append(f"Morgen {hour:02d}:00")
                else:
                    hour = actual_offset - 48
                    labels.append(f"Übermorgen {hour:02d}:00")

            current_soc = app_state['battery']['soc']

            # Build 49-hour rolling window with current SOC for historical data
            rolling_soc = [current_soc] * 24  # Fill past 24 hours with current SOC
            rolling_charging = [0.0] * 24
            rolling_pv = [0.0] * 24
            rolling_consumption = [0.0] * 24
            rolling_prices = [0.30] * 24

            # Add forecast data for current hour onwards (24 hours)
            for i in range(24):
                if i < len(forecast_soc):
                    rolling_soc.append(forecast_soc[i])
                    rolling_charging.append(forecast_charging[i] if i < len(forecast_charging) else 0.0)
                    rolling_pv.append(forecast_pv[i] if i < len(forecast_pv) else 0.0)
                    rolling_consumption.append(forecast_consumption[i] if i < len(forecast_consumption) else 0.0)
                    rolling_prices.append(forecast_prices[i] if i < len(forecast_prices) else 0.30)
                else:
                    rolling_soc.append(current_soc)
                    rolling_charging.append(0.0)
                    rolling_pv.append(0.0)
                    rolling_consumption.append(0.0)
                    rolling_prices.append(0.30)

            # Add one more hour to reach 49 total
            rolling_soc.append(rolling_soc[-1])
            rolling_charging.append(0.0)
            rolling_pv.append(0.0)
            rolling_consumption.append(0.0)
            rolling_prices.append(0.30)

            # Adjust charging windows to rolling window indices
            adjusted_windows = []
            for window in forecast_plan.get('charging_windows', []):
                forecast_hour = window['hour']
                rolling_index = 24 + forecast_hour

                if rolling_index < 49:
                    adjusted_window = window.copy()
                    adjusted_window['hour'] = rolling_index
                    adjusted_windows.append(adjusted_window)

            return jsonify({
                'labels': labels,  # v1.2.0-beta.51: Rolling window labels
                'hourly_soc': rolling_soc,
                'hourly_charging': rolling_charging,
                'hourly_pv': rolling_pv,
                'hourly_consumption': rolling_consumption,
                'hourly_prices': rolling_prices,
                'charging_windows': adjusted_windows,
                'current_hour_index': 24,
                'last_planned': forecast_plan.get('last_planned'),
                'start_time': forecast_plan.get('start_time'),
                'min_soc_reached': forecast_plan.get('min_soc_reached', 0),
                'total_charging_kwh': forecast_plan.get('total_charging_kwh', 0)
            }), 200

        # No data at all (v1.2.0-beta.51)
        else:
            # Generate labels for rolling window
            labels = []
            start_offset = current_hour - 24

            for offset in range(start_offset, start_offset + 49):
                actual_offset = offset

                if actual_offset < 0:
                    hour = (24 + actual_offset) % 24
                    labels.append(f"Gestern {hour:02d}:00")
                elif actual_offset < 24:
                    hour = actual_offset
                    if offset == current_hour:
                        labels.append(f"► JETZT {hour:02d}:00")
                    else:
                        labels.append(f"Heute {hour:02d}:00")
                elif actual_offset < 48:
                    hour = actual_offset - 24
                    labels.append(f"Morgen {hour:02d}:00")
                else:
                    hour = actual_offset - 48
                    labels.append(f"Übermorgen {hour:02d}:00")

            current_soc = app_state['battery']['soc']
            return jsonify({
                'error': 'No schedule available yet',
                'labels': labels,
                'hourly_soc': [current_soc] * 49,
                'hourly_charging': [0] * 49,
                'hourly_pv': [0] * 49,
                'hourly_consumption': [0] * 49,
                'hourly_prices': [0.30] * 49,
                'charging_windows': [],
                'current_hour_index': 24,
                'last_planned': None,
                'start_time': None
            }), 200

    except Exception as e:
        logger.error(f"Error getting battery schedule: {e}", exc_info=True)

        # Generate labels for rolling window (v1.2.0-beta.51)
        labels = []
        start_offset = now.hour - 24

        for offset in range(start_offset, start_offset + 49):
            actual_offset = offset

            if actual_offset < 0:
                hour = (24 + actual_offset) % 24
                labels.append(f"Gestern {hour:02d}:00")
            elif actual_offset < 24:
                hour = actual_offset
                if offset == now.hour:
                    labels.append(f"► JETZT {hour:02d}:00")
                else:
                    labels.append(f"Heute {hour:02d}:00")
            elif actual_offset < 48:
                hour = actual_offset - 24
                labels.append(f"Morgen {hour:02d}:00")
            else:
                hour = actual_offset - 48
                labels.append(f"Übermorgen {hour:02d}:00")

        current_soc = app_state['battery'].get('soc', 50)
        return jsonify({
            'error': str(e),
            'labels': labels,
            'hourly_soc': [current_soc] * 49,
            'hourly_charging': [0] * 49,
            'hourly_pv': [0] * 49,
            'hourly_consumption': [0] * 49,
            'hourly_prices': [0.30] * 49,
            'charging_windows': [],
            'current_hour_index': 24,
            'last_planned': None,
            'start_time': None
        }), 500

@app.route('/api/adjust_power', methods=['POST'])
def api_adjust_power():
    """Adjust charging power during active charging (v0.2.0)"""
    try:
        data = request.json
        power = data.get('power', config.get('max_charge_power', 3900))

        # Only execute if currently charging
        if app_state['inverter']['mode'] in ['manual_charging', 'auto_charging']:
            if not modbus_client:
                add_log('ERROR', 'Modbus client not available')
                return jsonify({
                    'status': 'error',
                    'message': 'Modbus client not available'
                }), 500

            charge_power = -abs(int(power))
            success = modbus_client.write_battery_power(charge_power)

            if success:
                add_log('INFO', f'Charging power adjusted to {power}W')
                return jsonify({
                    'status': 'ok',
                    'power': power
                })
            else:
                add_log('ERROR', 'Failed to adjust charging power')
                return jsonify({
                    'status': 'error',
                    'message': 'Failed to adjust charging power'
                }), 500
        else:
            return jsonify({
                'status': 'error',
                'message': 'Not currently charging'
            }), 400

    except Exception as e:
        add_log('ERROR', f'Error adjusting power: {str(e)}')
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500

@app.route('/api/tibber_price_chart')
def api_tibber_price_chart():
    """Get Tibber price data for rolling 48h window (v1.2.0-beta.50 - centered on current hour)"""
    try:
        if not ha_client:
            return jsonify({
                'success': False,
                'error': 'HA client not available'
            }), 500

        tibber_sensor = config.get('tibber_price_sensor', 'sensor.tibber_prices')
        prices_data = ha_client.get_state_with_attributes(tibber_sensor)

        if not prices_data or 'attributes' not in prices_data:
            return jsonify({
                'success': False,
                'error': 'No Tibber price data available'
            }), 500

        today_prices = prices_data['attributes'].get('today', [])
        tomorrow_prices = prices_data['attributes'].get('tomorrow', [])

        if not today_prices:
            return jsonify({
                'success': False,
                'error': 'No price data for today'
            }), 500

        from datetime import datetime, timedelta
        now = datetime.now().astimezone()
        current_hour = now.hour

        # Create a map of all available prices by hour offset from midnight today
        # Offset 0 = today 00:00, offset 23 = today 23:00, offset 24 = tomorrow 00:00, etc.
        price_map = {}

        for i, entry in enumerate(today_prices):
            price_map[i] = round(entry.get('total', 0) * 100, 2)

        for i, entry in enumerate(tomorrow_prices):
            price_map[24 + i] = round(entry.get('total', 0) * 100, 2)

        # Build rolling window: 24 hours back, current hour, 24 hours forward
        hours = []
        prices = []

        # Start from 24 hours before current hour
        start_offset = current_hour - 24

        for offset in range(start_offset, start_offset + 49):  # 49 hours total (24 + 1 + 24)
            # Calculate actual hour offset from today midnight
            actual_offset = offset

            # Determine label
            if actual_offset < 0:
                # Yesterday
                hour = (24 + actual_offset) % 24
                hours.append(f"Gestern {hour:02d}:00")
                prices.append(None)  # No data for yesterday
            elif actual_offset < 24:
                # Today
                hour = actual_offset
                if offset == current_hour:
                    hours.append(f"► JETZT {hour:02d}:00")
                else:
                    hours.append(f"Heute {hour:02d}:00")
                prices.append(price_map.get(actual_offset))
            elif actual_offset < 48:
                # Tomorrow
                hour = actual_offset - 24
                hours.append(f"Morgen {hour:02d}:00")
                prices.append(price_map.get(actual_offset))
            else:
                # Day after tomorrow
                hour = actual_offset - 48
                hours.append(f"Übermorgen {hour:02d}:00")
                prices.append(None)  # No data yet

        return jsonify({
            'success': True,
            'labels': hours,
            'prices': prices,
            'current_hour_index': 24,  # Current hour is always at index 24 (middle)
            'tomorrow_available': len(tomorrow_prices) > 0
        })

    except Exception as e:
        logger.error(f"Error getting Tibber price chart data: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/consumption_forecast_chart')
def api_consumption_forecast_chart():
    """Get consumption forecast for rolling 48h window (v1.2.0-beta.50 - centered on current hour)"""
    try:
        if not consumption_learner:
            return jsonify({
                'success': False,
                'error': 'Consumption learner not available'
            }), 500

        from datetime import datetime, timedelta
        now = datetime.now()
        current_hour = now.hour
        current_minute = now.minute
        today = now.date()
        yesterday = today - timedelta(days=1)
        tomorrow = today + timedelta(days=1)

        # Get hourly profiles for yesterday, today, tomorrow
        profile_yesterday = consumption_learner.get_hourly_profile(target_date=yesterday)
        profile_today = consumption_learner.get_hourly_profile(target_date=today)
        profile_tomorrow = consumption_learner.get_hourly_profile(target_date=tomorrow)

        if not profile_today:
            return jsonify({
                'success': False,
                'error': 'No consumption data available'
            }), 500

        # Get recorded values from database for yesterday and today (v1.2.0-beta.51)
        yesterday_db_consumption = consumption_learner.get_today_consumption(date=yesterday)
        today_db_consumption = consumption_learner.get_today_consumption()

        # For current hour: calculate live value with blending
        current_hour_live_value = None
        if ha_client and current_minute < 59:
            try:
                avg = get_home_consumption_kwh(ha_client, config, now)
                if avg is not None and avg >= 0:
                    elapsed_fraction = current_minute / 60.0
                    remaining_fraction = (60 - current_minute) / 60.0
                    forecast_value = profile_today.get(current_hour, avg)
                    current_hour_live_value = (avg * elapsed_fraction) + (forecast_value * remaining_fraction)
            except Exception as e:
                logger.error(f"Error calculating current hour consumption: {e}")

        # Build rolling window: 24 hours back, current hour, 24 hours forward
        hours = []
        forecast_consumption = []
        actual_consumption = []

        # Start from 24 hours before current hour
        start_offset = current_hour - 24

        for offset in range(start_offset, start_offset + 49):  # 49 hours total
            # Calculate which day and hour this offset represents
            if offset < 0:
                # Yesterday
                day_profile = profile_yesterday
                hour = (24 + offset) % 24
                label = f"Gestern {hour:02d}:00"
                # Use DB data for yesterday (v1.2.0-beta.51)
                actual = round(yesterday_db_consumption[hour], 2) if hour in yesterday_db_consumption else None
            elif offset < 24:
                # Today
                day_profile = profile_today
                hour = offset
                if offset == current_hour:
                    label = f"► JETZT {hour:02d}:00"
                    # Actual: use live blended value or DB value
                    if current_hour_live_value is not None:
                        actual = round(current_hour_live_value, 2)
                    elif hour in today_db_consumption:
                        actual = round(today_db_consumption[hour], 2)
                    else:
                        actual = None
                elif offset < current_hour:
                    label = f"Heute {hour:02d}:00"
                    # Past hours: use DB value
                    actual = round(today_db_consumption[hour], 2) if hour in today_db_consumption else None
                else:
                    label = f"Heute {hour:02d}:00"
                    actual = None  # Future
            elif offset < 48:
                # Tomorrow
                day_profile = profile_tomorrow
                hour = offset - 24
                label = f"Morgen {hour:02d}:00"
                actual = None  # No data yet
            else:
                # Day after tomorrow
                day_profile = profile_tomorrow  # Use tomorrow's profile as best guess
                hour = offset - 48
                label = f"Übermorgen {hour:02d}:00"
                actual = None

            hours.append(label)
            forecast_consumption.append(round(day_profile.get(hour, 0), 2))
            actual_consumption.append(actual)

        # Calculate forecast accuracy for completed hours only
        accuracy = None
        accuracy_hours = 0

        errors = []
        for i in range(len(actual_consumption)):
            actual = actual_consumption[i]
            forecast = forecast_consumption[i]

            if actual is not None and forecast is not None and forecast > 0.01:
                percentage_error = abs(actual - forecast) / forecast * 100
                errors.append(percentage_error)

        if errors:
            mape = sum(errors) / len(errors)
            accuracy = max(0, 100 - mape)
            accuracy_hours = len(errors)
            logger.debug(f"Forecast accuracy: {accuracy:.1f}% based on {accuracy_hours} hours (MAPE: {mape:.1f}%)")

        return jsonify({
            'success': True,
            'labels': hours,
            'forecast': forecast_consumption,
            'actual': actual_consumption,
            'current_hour_index': 24,  # Current hour is always at index 24 (middle)
            'accuracy': round(accuracy, 1) if accuracy is not None else None,
            'accuracy_hours': accuracy_hours
        })

    except Exception as e:
        logger.error(f"Error getting consumption forecast chart data: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/consumption_learning')
def api_consumption_learning():
    """Get consumption learning statistics and hourly profile (v0.4.0)"""
    try:
        if not consumption_learner:
            return jsonify({
                'enabled': False,
                'message': 'Consumption learning not enabled'
            })

        # Get statistics
        stats = consumption_learner.get_statistics()

        # Get hourly profile for today's weekday
        from datetime import datetime
        today = datetime.now().date()
        profile = consumption_learner.get_hourly_profile(target_date=today)

        return jsonify({
            'enabled': True,
            'statistics': stats,
            'hourly_profile': profile
        })

    except Exception as e:
        logger.error(f"Error getting consumption learning data: {e}")
        return jsonify({
            'enabled': False,
            'error': str(e)
        }), 500

@app.route('/api/consumption_import_csv', methods=['POST'])
def api_consumption_import_csv():
    """Import consumption data from CSV file (v0.4.0)"""
    try:
        if not consumption_learner:
            return jsonify({
                'success': False,
                'error': 'Consumption learning not enabled'
            }), 400

        # Check if file was uploaded
        if 'file' not in request.files:
            return jsonify({
                'success': False,
                'error': 'No file uploaded'
            }), 400

        file = request.files['file']

        if file.filename == '':
            return jsonify({
                'success': False,
                'error': 'No file selected'
            }), 400

        if not file.filename.endswith('.csv'):
            return jsonify({
                'success': False,
                'error': 'File must be CSV format'
            }), 400

        # Read CSV content
        csv_content = file.read().decode('utf-8')

        # Clear all manually imported data before importing new data
        # This prevents old manual data from lingering
        deleted = consumption_learner.clear_all_manual_data()
        add_log('INFO', f'🗑️ Gelöscht: {deleted} alte manuelle Datensätze vor Import')

        # Import data
        result = consumption_learner.import_from_csv(csv_content)

        if result['success']:
            add_log('INFO', f'✅ CSV Import: {result["imported_hours"]} Stundenwerte importiert')
            return jsonify(result)
        else:
            add_log('ERROR', f'❌ CSV Import fehlgeschlagen: {result.get("error", "Unknown error")}')
            return jsonify(result), 400

    except Exception as e:
        logger.error(f"Error importing CSV: {e}", exc_info=True)
        add_log('ERROR', f'CSV Import Fehler: {str(e)}')
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/consumption_import_ha', methods=['POST'])
def api_consumption_import_ha():
    """Import consumption data from Home Assistant history (v1.2.0-beta.10 - calculated from Grid + PV)"""
    try:
        if not consumption_learner:
            return jsonify({
                'success': False,
                'error': 'Consumption learning not enabled'
            }), 400

        if not ha_client:
            return jsonify({
                'success': False,
                'error': 'Home Assistant client not available'
            }), 400

        # v1.2.0-beta.40: Use energy sensors (kWh cumulative) for accurate calculation
        grid_from_energy_sensor = config.get('grid_from_energy_sensor')
        grid_to_energy_sensor = config.get('grid_to_energy_sensor')
        battery_charge_from_grid_sensor = config.get('battery_charge_from_grid_sensor')
        battery_charge_from_pv_sensor = config.get('battery_charge_from_pv_sensor')
        battery_discharge_sensor = config.get('battery_discharge_sensor')

        # v1.2.0-beta.45: PV energy sensors (kWh cumulative, native inverter energy meters)
        pv_energy_sensors = [
            config.get('pv_energy_pv1_inverter1_sensor'),
            config.get('pv_energy_pv2_inverter1_sensor'),
            config.get('pv_energy_pv1_inverter2_sensor'),
            config.get('pv_energy_pv2_inverter2_sensor')
        ]
        # Filter out None values
        pv_energy_sensors = [s for s in pv_energy_sensors if s]

        # Validate configuration
        if not grid_from_energy_sensor or not grid_to_energy_sensor:
            return jsonify({
                'success': False,
                'error': 'Configuration required: grid_from_energy_sensor and grid_to_energy_sensor must be configured'
            }), 400

        if not battery_charge_from_grid_sensor or not battery_charge_from_pv_sensor or not battery_discharge_sensor:
            return jsonify({
                'success': False,
                'error': 'Configuration required: All battery energy sensors must be configured (charge_from_grid, charge_from_pv, discharge)'
            }), 400

        # v1.2.0-beta.45: PV energy sensors must be configured
        if not pv_energy_sensors:
            return jsonify({
                'success': False,
                'error': 'Configuration required: At least one PV energy sensor must be configured'
            }), 400

        days = request.json.get('days', 28) if request.json else 28

        # v1.2.0-beta.45: Energy sensor mode (cumulative kWh sensors)
        # Formula: Home = (GridFrom - GridTo) + PV + (BatteryDischarge - BatteryChargeFromGrid - BatteryChargeFromPV)
        add_log('INFO', f'Starting HA import with ENERGY sensors for last {days} days...')
        add_log('INFO', f'GridFromEnergy: {grid_from_energy_sensor}, GridToEnergy: {grid_to_energy_sensor}')
        add_log('INFO', f'BattChgGrid: {battery_charge_from_grid_sensor}, BattChgPV: {battery_charge_from_pv_sensor}, BattDisch: {battery_discharge_sensor}')
        add_log('INFO', f'PV Energy Sensors: {pv_energy_sensors} (native inverter energy meters - cumulative kWh)')

        # Clear all manually imported data before importing new data
        deleted = consumption_learner.clear_all_manual_data()
        add_log('INFO', f'🗑️ Gelöscht: {deleted} alte manuelle Datensätze vor Import')

        # Use energy sensor import method (v1.2.0-beta.45: PV energy sensors)
        result = consumption_learner.import_calculated_consumption_energy_sensors(
            ha_client,
            grid_from_energy_sensor,
            grid_to_energy_sensor,
            battery_charge_from_grid_sensor,
            battery_charge_from_pv_sensor,
            battery_discharge_sensor,
            pv_energy_sensors,
            days
        )

        if result['success']:
            add_log('INFO', f'✅ HA Import: {result["imported_hours"]} Stundenwerte aus Home Assistant importiert')
            return jsonify(result)
        else:
            add_log('ERROR', f'❌ HA Import fehlgeschlagen: {result.get("error", "Unknown error")}')
            return jsonify(result), 400

    except Exception as e:
        logger.error(f"Error importing from Home Assistant: {e}", exc_info=True)
        add_log('ERROR', f'HA Import Fehler: {str(e)}')
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/consumption_import/clear_manual', methods=['POST'])
def api_clear_manual_data():
    """Clear all manually imported consumption data (v1.2.0-beta.7)"""
    try:
        if not consumption_learner:
            return jsonify({
                'success': False,
                'error': 'Consumption learning not enabled'
            }), 400

        deleted = consumption_learner.clear_all_manual_data()
        add_log('INFO', f'🗑️ Gelöscht: {deleted} manuelle Datensätze')

        return jsonify({
            'success': True,
            'deleted': deleted
        })

    except Exception as e:
        logger.error(f"Error clearing manual data: {e}", exc_info=True)
        add_log('ERROR', f'Fehler beim Löschen: {str(e)}')
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/consumption_data', methods=['GET'])
def api_consumption_data_get():
    """Get all consumption data for editing (v0.4.0)"""
    try:
        if not consumption_learner:
            return jsonify({
                'success': False,
                'error': 'Consumption learning not enabled'
            }), 400

        # Get all data from database
        import sqlite3
        daily_data = []

        with sqlite3.connect(consumption_learner.db_path) as conn:
            # Get unique dates
            cursor = conn.execute("""
                SELECT DISTINCT DATE(timestamp) as date
                FROM hourly_consumption
                ORDER BY date DESC
                LIMIT 28
            """)

            dates = [row[0] for row in cursor.fetchall()]

            # For each date, get all 24 hours
            for date_str in dates:
                # Get all entries for this date (may have duplicates due to non-rounded timestamps)
                # v1.2.0-beta.9: Match chart behavior - show NEWEST data (ORDER BY created_at DESC)
                # This ensures table and chart are consistent after automatic imports
                cursor = conn.execute("""
                    SELECT hour, consumption_kwh, created_at
                    FROM hourly_consumption
                    WHERE DATE(timestamp) = ?
                    ORDER BY hour, created_at DESC
                """, (date_str,))

                # Take newest entry per hour (first in DESC order)
                hours_data = {}
                for hour, consumption, created_at in cursor.fetchall():
                    if hour not in hours_data:
                        hours_data[hour] = consumption
                        # First entry is newest due to ORDER BY created_at DESC

                # Build 24-hour array
                hours = [hours_data.get(h, 0) for h in range(24)]

                # Get weekday (0=Monday, 6=Sunday)
                from datetime import datetime
                date_obj = datetime.fromisoformat(date_str)
                weekdays = ['Montag', 'Dienstag', 'Mittwoch', 'Donnerstag', 'Freitag', 'Samstag', 'Sonntag']
                weekday = weekdays[date_obj.weekday()]

                daily_data.append({
                    'date': date_str,
                    'weekday': weekday,
                    'hours': hours
                })

        return jsonify({
            'success': True,
            'data': daily_data
        })

    except Exception as e:
        logger.error(f"Error getting consumption data: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/consumption_data', methods=['POST'])
def api_consumption_data_post():
    """Save consumption data from web editor (v0.4.0)"""
    try:
        if not consumption_learner:
            return jsonify({
                'success': False,
                'error': 'Consumption learning not enabled'
            }), 400

        data = request.json.get('data', [])

        if not data:
            return jsonify({
                'success': False,
                'error': 'No data provided'
            }), 400

        # Import the data
        result = consumption_learner.import_detailed_history(data)

        if result['success']:
            add_log('INFO', f'✅ Daten gespeichert: {result["imported_hours"]} Stundenwerte')
            return jsonify(result)
        else:
            add_log('ERROR', f'❌ Fehler beim Speichern der Daten')
            return jsonify(result), 400

    except Exception as e:
        logger.error(f"Error saving consumption data: {e}")
        add_log('ERROR', f'Fehler beim Speichern: {str(e)}')
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

# ==============================================================================
# Error Handlers
# ==============================================================================

@app.errorhandler(404)
def not_found(error):
    return jsonify({'error': 'Not found'}), 404

@app.errorhandler(500)
def internal_error(error):
    logger.error(f"Internal error: {error}")
    return jsonify({'error': 'Internal server error'}), 500

# ==============================================================================
# Background Controller Thread
# ==============================================================================

def update_charging_plan():
    """Calculate optimal charging schedule based on Tibber prices (v0.3.0)"""
    try:
        logger.info("Starting charging plan calculation...")
        add_log('INFO', 'Calculating charging plan...')

        # Check prerequisites
        if not ha_client:
            logger.warning("HA client not available - cannot calculate charging plan")
            add_log('WARNING', 'Charging plan calculation skipped: HA client not available')
            return

        if not tibber_optimizer:
            logger.warning("Tibber optimizer not available - cannot calculate charging plan")
            add_log('WARNING', 'Charging plan calculation skipped: Tibber optimizer not available')
            return

        # Hole Tibber-Preise
        tibber_sensor = config.get('tibber_price_sensor', 'sensor.tibber_prices')
        logger.info(f"Fetching price data from sensor: {tibber_sensor}")
        prices_data = ha_client.get_state_with_attributes(tibber_sensor)

        if not prices_data:
            logger.warning(f"Could not get data from {tibber_sensor}")
            add_log('WARNING', f'No data from Tibber sensor: {tibber_sensor}')
            return

        if 'attributes' not in prices_data:
            logger.warning(f"Sensor {tibber_sensor} has no attributes")
            add_log('WARNING', f'Tibber sensor {tibber_sensor} missing attributes')
            return

        # Kombiniere heute + morgen Preise
        today = prices_data['attributes'].get('today', [])
        tomorrow = prices_data['attributes'].get('tomorrow', [])
        all_prices = today + tomorrow

        logger.info(f"Price data: {len(today)} today, {len(tomorrow)} tomorrow = {len(all_prices)} total")

        if not all_prices:
            logger.warning("No price data in today/tomorrow attributes")
            add_log('WARNING', 'No Tibber price data available (today/tomorrow empty)')
            return

        # Finde optimales Ladeende
        logger.info("Analyzing prices to find optimal charge end time...")
        charge_end = tibber_optimizer.find_optimal_charge_end_time(all_prices)

        if charge_end:
            # Hole aktuellen SOC
            current_soc = app_state['battery']['soc']
            max_soc = int(config.get('auto_charge_below_soc', 95))

            logger.info(f"Found optimal charge end time: {charge_end}, current SOC: {current_soc}%, target: {max_soc}%")

            # Berechne Ladebeginn
            charge_start = tibber_optimizer.calculate_charge_start_time(
                charge_end, current_soc, max_soc
            )

            # Speichere im State
            app_state['charging_plan']['planned_start'] = charge_start.isoformat()
            app_state['charging_plan']['planned_end'] = charge_end.isoformat()
            app_state['charging_plan']['last_calculated'] = datetime.now().isoformat()
            app_state['charging_plan']['target_soc'] = max_soc

            add_log('INFO', f'✓ Ladeplan berechnet: Start {charge_start.strftime("%d.%m. %H:%M")} - Ende {charge_end.strftime("%d.%m. %H:%M")}')

            # Optional: Setze auch Home Assistant Input Datetime
            try:
                input_end = config.get('input_datetime_planned_charge_end')
                input_start = config.get('input_datetime_planned_charge_start')

                if input_end:
                    ha_client.set_datetime(input_end, charge_end)
                    logger.debug(f"Updated {input_end}")
                if input_start:
                    ha_client.set_datetime(input_start, charge_start)
                    logger.debug(f"Updated {input_start}")
            except Exception as e:
                logger.warning(f"Could not set input_datetime: {e}")
        else:
            logger.info("No optimal charge end time found - prices remain low or insufficient data")
            add_log('INFO', 'Kein optimaler Ladezeitpunkt gefunden (Preise bleiben günstig)')
            # Mark calculation as done even if no plan was created
            app_state['charging_plan']['last_calculated'] = datetime.now().isoformat()

    except Exception as e:
        logger.error(f"Error updating charging plan: {e}", exc_info=True)
        add_log('ERROR', f'Fehler bei Ladeplan-Berechnung: {str(e)}')


def get_charging_status_explanation():
    """Generate human-readable explanation of charging status (v1.2.0-beta.38 - Rolling Schedule)"""
    try:
        from datetime import datetime, timedelta

        # Get current values
        current_soc = app_state['battery']['soc']
        min_soc = 10  # Battery minimum
        max_soc = int(config.get('auto_charge_below_soc', 98))

        # Get rolling schedule (new multi-peak logic)
        schedule = app_state.get('daily_battery_schedule')

        now = datetime.now().astimezone()

        # Default response
        explanation = "⏸️ Keine 24h-Simulation verfügbar. System startet..."
        will_charge = False
        schedule_info = {}

        if schedule:
            # Extract schedule information
            charging_windows = schedule.get('charging_windows', [])
            min_soc_reached = schedule.get('min_soc_reached', 0)
            total_charging_kwh = schedule.get('total_charging_kwh', 0)
            start_time_str = schedule.get('start_time')

            # Parse start time to map rolling hours to real time
            if start_time_str:
                start_time = datetime.fromisoformat(start_time_str)
                current_hour_offset = start_time.hour
            else:
                current_hour_offset = now.hour

            # Find next charging window
            next_window = None
            current_window = None

            for window in charging_windows:
                rolling_hour = window['hour']
                # Convert rolling hour to real time
                target_hour = (current_hour_offset + rolling_hour) % 24
                target_day_offset = (current_hour_offset + rolling_hour) // 24

                window_time = now.replace(hour=target_hour, minute=0, second=0, microsecond=0)
                if target_day_offset > 0:
                    window_time += timedelta(days=1)

                # Check if this is the current or next window
                window_end = window_time + timedelta(hours=1)
                if now >= window_time and now < window_end:
                    current_window = {**window, 'time': window_time}
                    break
                elif window_time > now and next_window is None:
                    next_window = {**window, 'time': window_time}

            # Determine explanation based on current state
            if current_soc < 15:
                # Critical SOC
                if current_window:
                    explanation = f"🔋 Laden AKTIV! Aktuell {current_window['charge_kwh']:.1f} kWh @ {current_window['price']*100:.1f} Ct/kWh (SOC kritisch: {current_soc:.0f}%)"
                    will_charge = True
                elif next_window:
                    time_until = (next_window['time'] - now).total_seconds() / 3600
                    explanation = f"⚡ SOC kritisch ({current_soc:.0f}%)! Nächster Ladeblock in {time_until:.1f}h um {next_window['time'].strftime('%H:%M')} Uhr"
                    will_charge = False
                else:
                    explanation = f"⚠️ SOC niedrig ({current_soc:.0f}%)! Kein Ladeblock geplant - Preise bleiben günstig"
                    will_charge = False

            elif current_window:
                # Currently charging
                explanation = f"🔋 Laden AKTIV! {current_window['charge_kwh']:.1f} kWh @ {current_window['price']*100:.1f} Ct/kWh für {current_window.get('reason', 'optimale Preise')}"
                will_charge = True

            elif next_window:
                # Waiting for next window
                time_until = (next_window['time'] - now).total_seconds() / 3600
                if time_until < 1:
                    explanation = f"⏳ Nächster Ladeblock in {int(time_until * 60)} Minuten um {next_window['time'].strftime('%H:%M')} Uhr ({next_window['charge_kwh']:.1f} kWh @ {next_window['price']*100:.1f} Ct)"
                else:
                    explanation = f"⏳ Nächster Ladeblock in {time_until:.1f}h um {next_window['time'].strftime('%H:%M')} Uhr ({next_window['charge_kwh']:.1f} kWh @ {next_window['price']*100:.1f} Ct)"
                will_charge = False

            elif current_soc >= max_soc - 2:
                # Already full
                explanation = f"✅ Batterie fast voll ({current_soc:.0f}%). Keine weitere Netz-Ladung nötig"
                will_charge = False

            elif len(charging_windows) == 0:
                # No charging needed
                explanation = f"☀️ Keine Netz-Ladung geplant. Preise bleiben günstig oder PV-Ertrag ausreichend (SOC: {current_soc:.0f}%)"
                will_charge = False

            else:
                # Charging completed
                explanation = f"✅ Alle geplanten Ladeblöcke abgeschlossen (SOC: {current_soc:.0f}%)"
                will_charge = False

            # Build schedule info (replaces old conditions)
            schedule_info = {
                'charging_blocks': {
                    'fulfilled': len(charging_windows) > 0,
                    'label': f'{len(charging_windows)} Ladeblöcke geplant ({total_charging_kwh:.1f} kWh)',
                    'priority': 1,
                    'value': f'{len(charging_windows)} Blöcke'
                },
                'min_soc_forecast': {
                    'fulfilled': min_soc_reached >= 15,
                    'label': f'Niedrigster SOC in 24h: {min_soc_reached:.0f}%',
                    'priority': 2,
                    'value': f'{min_soc_reached:.0f}%'
                },
                'total_cost': {
                    'fulfilled': True,
                    'label': f'Ø Ladepreis: {(sum([w["price"] for w in charging_windows]) / len(charging_windows) * 100) if charging_windows else 0:.1f} Ct/kWh',
                    'priority': 3,
                    'value': f'{(sum([w["price"] for w in charging_windows]) / len(charging_windows) * 100) if charging_windows else 0:.1f} Ct'
                },
                'next_window': {
                    'fulfilled': next_window is not None or current_window is not None,
                    'label': f'Nächster Block: {next_window["time"].strftime("%H:%M")} Uhr' if next_window else ('Laden läuft' if current_window else 'Kein Block'),
                    'priority': 4,
                    'value': next_window['time'].strftime('%H:%M') if next_window else ('Jetzt' if current_window else '-')
                }
            }

        else:
            # No schedule available yet
            schedule_info = {
                'status': {
                    'fulfilled': False,
                    'label': 'System berechnet initiale 24h-Simulation...',
                    'priority': 1,
                    'value': 'Initialisierung'
                }
            }

        return {
            'explanation': explanation,
            'will_charge': will_charge,
            'conditions': schedule_info,
            'current_soc': current_soc,
            'target_soc': max_soc,
            'pv_remaining': 0,  # Deprecated but kept for compatibility
            'planned_start': None,
            'planned_end': None
        }

    except Exception as e:
        logger.error(f"Error generating charging status explanation: {e}", exc_info=True)
        return {
            'explanation': '❌ Fehler beim Ermitteln des Ladestatus',
            'will_charge': False,
            'conditions': {
                'error': {
                    'fulfilled': False,
                    'label': 'Fehler beim Laden der Daten',
                    'priority': 1,
                    'value': 'Fehler'
                }
            },
            'current_soc': 0,
            'target_soc': 0,
            'pv_remaining': 0,
            'planned_start': None,
            'planned_end': None
        }


def calculate_hourly_average(ha_client, sensor_id, timestamp, allow_negative=False):
    """
    Calculate hourly energy consumption from sensor history.

    Handles both power sensors (W/kW) and energy sensors (Wh/kWh):
    - Power sensors: Integrates using trapezoidal rule (average × time)
    - Energy sensors: Calculates difference (end - start)

    Args:
        ha_client: Home Assistant API client
        sensor_id: Sensor entity ID
        timestamp: Current timestamp
        allow_negative: If True, allows negative values (e.g., for grid feed-in)

    Returns:
        float: Energy in kWh for the past hour, or None if error/unavailable
    """
    try:
        from datetime import timedelta, datetime

        # Calculate time range: from 1 hour ago to now
        end_time = timestamp
        start_time = timestamp - timedelta(hours=1)

        # Fetch history
        history = ha_client.get_history(sensor_id, start_time, end_time)
        if not history or len(history) == 0:
            logger.warning(f"No history data available for {sensor_id}")
            return None

        # Get sensor unit to determine if it's power or energy
        sensor_info = ha_client.get_state_with_attributes(sensor_id)
        unit = None
        if sensor_info:
            unit = sensor_info.get('attributes', {}).get('unit_of_measurement', '').lower()

        # Collect valid values with timestamps
        valid_entries = []
        for entry in history:
            try:
                value_state = entry.get('state')
                if value_state not in ['unknown', 'unavailable', None, '']:
                    value = float(value_state)

                    # Skip negative values only if not allowed
                    if not allow_negative and value < 0:
                        continue

                    # Skip unrealistically high values
                    if abs(value) > 1000000:  # > 1 MW seems like an error
                        continue

                    # Parse timestamp
                    last_changed = entry.get('last_changed')
                    if last_changed:
                        ts = datetime.fromisoformat(last_changed.replace('Z', '+00:00'))
                        valid_entries.append((ts, value))
            except (ValueError, TypeError) as e:
                continue

        if not valid_entries:
            logger.warning(f"No valid values in history for {sensor_id}")
            return None

        # Sort by timestamp
        valid_entries.sort(key=lambda x: x[0])

        # Determine sensor type and calculate energy
        is_energy_sensor = unit and ('wh' in unit or 'kwh' in unit)

        if is_energy_sensor:
            # Energy sensor (Wh/kWh): Calculate difference (end - start)
            # This handles cumulative energy counters
            first_value = valid_entries[0][1]
            last_value = valid_entries[-1][1]
            energy = last_value - first_value

            # Convert Wh to kWh if needed
            if unit and 'wh' in unit and 'kwh' not in unit:
                energy = energy / 1000

            logger.info(f"✓ Energy sensor ({unit}): {sensor_id}: {first_value:.3f} → {last_value:.3f} = {energy:.3f} kWh")
        else:
            # Power sensor (W/kW): Integrate using trapezoidal rule
            # Energy = average power × time
            total_energy_wh = 0.0

            for i in range(len(valid_entries) - 1):
                ts1, value1 = valid_entries[i]
                ts2, value2 = valid_entries[i + 1]

                # Calculate time difference in hours
                time_diff_hours = (ts2 - ts1).total_seconds() / 3600.0

                # Trapezoidal rule: average of two consecutive readings × time
                avg_power = (value1 + value2) / 2.0
                energy_increment = avg_power * time_diff_hours

                total_energy_wh += energy_increment

            # Convert W to kW if needed (values > 50 are likely in Watts)
            if abs(sum(v for _, v in valid_entries) / len(valid_entries)) > 50:
                energy = total_energy_wh / 1000  # W·h → kW·h
            else:
                energy = total_energy_wh  # Already in kW·h

            logger.info(f"✓ Power sensor ({unit or 'unknown'}): {sensor_id}: Integrated {len(valid_entries)} samples = {energy:.3f} kWh")

        return energy

    except Exception as e:
        logger.error(f"Error calculating hourly average for {sensor_id}: {e}", exc_info=True)
        return None


def calculate_synchronized_energy(ha_client, sensors, start_time, end_time):
    """
    Calculate energy from multiple sensors using simple average method.

    For each sensor, this function:
    1. Fetches all data points in the time range
    2. Calculates the average of all values (sum / count)
    3. Treats each sensor independently (no timestamp synchronization)
    4. Returns 0 if no data exists and zero_when_missing is True

    This matches the Energy Dashboard calculation method:
    - For each hour (e.g., 11:00:00 to 11:59:59)
    - Take all data points in that range
    - Calculate average: sum of all values / number of values
    - Use formula: home consumption = gridnet + battery + pv

    Args:
        ha_client: Home Assistant API client
        sensors: Dict with sensor config, e.g.:
                 {'grid': {'id': 'sensor.x', 'allow_negative': True, 'zero_when_missing': False},
                  'pv': {'id': 'sensor.y', 'allow_negative': False, 'zero_when_missing': True},
                  'battery': {'id': 'sensor.z', 'allow_negative': True, 'zero_when_missing': False}}
        start_time: Start datetime
        end_time: End datetime

    Returns:
        dict: Average power values converted to kWh for each sensor, e.g.:
              {'grid': 1.234, 'pv': 2.345, 'battery': 0.123}
              Returns None if critical data is missing
    """
    try:
        results = {}

        for sensor_name, sensor_config in sensors.items():
            sensor_id = sensor_config['id']
            zero_when_missing = sensor_config.get('zero_when_missing', False)

            # Get sensor unit
            sensor_info = ha_client.get_state_with_attributes(sensor_id)
            unit = None
            if sensor_info:
                unit = sensor_info.get('attributes', {}).get('unit_of_measurement', '').lower()

            # Fetch history
            history = ha_client.get_history(sensor_id, start_time, end_time)

            if not history or len(history) == 0:
                logger.warning(f"No history data available for {sensor_name} ({sensor_id})")
                if zero_when_missing:
                    results[sensor_name] = 0
                    logger.info(f"✓ {sensor_name}: No data, using 0 (zero_when_missing=True)")
                    continue
                else:
                    # For critical sensors without data
                    results[sensor_name] = None
                    continue

            # Parse and validate data points
            valid_values = []
            for entry in history:
                try:
                    value_state = entry.get('state')
                    if value_state not in ['unknown', 'unavailable', None, '']:
                        value = float(value_state)

                        # Skip negative values if not allowed
                        if not sensor_config.get('allow_negative', True) and value < 0:
                            continue

                        # Skip unrealistically high values
                        if abs(value) > 1000000:  # > 1 MW seems like an error
                            continue

                        valid_values.append(value)
                except (ValueError, TypeError):
                    continue

            if not valid_values:
                logger.warning(f"No valid values for {sensor_name}")
                if zero_when_missing:
                    results[sensor_name] = 0
                    logger.info(f"✓ {sensor_name}: No valid data, using 0 (zero_when_missing=True)")
                else:
                    results[sensor_name] = None
                continue

            # Calculate average (sum / count)
            average_value = sum(valid_values) / len(valid_values)
            logger.info(f"{sensor_name}: {len(valid_values)} data points, average = {average_value:.3f} {unit or '?'}")

            # Check if this is an energy sensor or power sensor
            is_energy_sensor = unit and ('wh' in unit or 'kwh' in unit)

            if is_energy_sensor:
                # Energy sensor: Use difference between first and last value
                first_value = valid_values[0]
                last_value = valid_values[-1]
                energy = last_value - first_value

                # Convert Wh to kWh if needed
                if unit and 'wh' in unit and 'kwh' not in unit:
                    energy = energy / 1000

                results[sensor_name] = energy
                logger.info(f"✓ {sensor_name} (energy sensor, {unit}): {first_value:.3f} → {last_value:.3f} = {energy:.3f} kWh")
            else:
                # Power sensor: Average power value
                # For hourly calculation: average_power (W) * 1h = energy (Wh)
                # Convert based on actual unit from sensor

                if unit and ('kw' in unit or 'kilowatt' in unit):
                    # Already in kW - multiply by 1 hour = kWh
                    energy = average_value
                    logger.info(f"✓ {sensor_name} (power sensor, {unit}): avg={average_value:.3f} kW → {energy:.3f} kWh")
                else:
                    # Assume Watts (default for power sensors)
                    # Convert W to kWh: (W * 1h) / 1000
                    energy = average_value / 1000
                    logger.info(f"✓ {sensor_name} (power sensor, {unit or 'W assumed'}): avg={average_value:.3f} W → {energy:.3f} kWh")

                results[sensor_name] = energy

        return results

    except Exception as e:
        logger.error(f"Error calculating energy averages: {e}", exc_info=True)
        return None


def get_home_consumption_kwh(ha_client, config, timestamp):
    """
    Calculate actual home consumption in kWh from cumulative energy sensors (v1.2.0-beta.53).

    Uses energy sensors (cumulative kWh counters) to calculate home consumption.
    This matches the Home Assistant Energy Dashboard methodology exactly.

    Required sensors:
    - grid_from_energy_sensor: Grid import energy (kWh cumulative)
    - grid_to_energy_sensor: Grid export energy (kWh cumulative)
    - battery_discharge_sensor: Battery discharge (kWh cumulative)
    - battery_charge_from_grid_sensor: Battery charge from grid (kWh cumulative)
    - battery_charge_from_pv_sensor: Battery charge from PV (kWh cumulative)
    - pv_energy_pv*_inverter*_sensor: PV energy sensors (kWh cumulative, optional)

    Formula: Home Consumption = GridFrom - GridTo + PV + BattDischarge - BattCharge
    - Grid Net = Grid Import (FROM) - Grid Export (TO)
    - PV Production = Sum of all PV strings
    - Battery Net = Discharge - (ChargeFromGrid + ChargeFromPV)

    Example (30.10. 10:00-11:00):
    - Grid FROM: 0.5 kWh (import during hour)
    - Grid TO: 2.0 kWh (export during hour)
    - Grid Net: 0.5 - 2.0 = -1.5 kWh
    - PV: 2.1 kWh (production during hour)
    - Battery Discharge: 0.8 kWh
    - Battery Charge: 0.3 kWh
    - Battery Net: 0.8 - 0.3 = 0.5 kWh
    - Home: -1.5 + 2.1 + 0.5 = 1.1 kWh

    Args:
        ha_client: Home Assistant API client
        config: Configuration dict with energy sensor IDs
        timestamp: Current timestamp (end of hour)

    Returns:
        float: Home consumption in kWh for the hour ending at timestamp, or None if error/unavailable
    """
    try:
        from datetime import timedelta

        # Calculate time range: from 1 hour ago to now
        end_time = timestamp
        start_time = timestamp - timedelta(hours=1)

        # PRIORITY 1: Check for Energy sensors (cumulative kWh) - v1.2.0-beta.53
        grid_from_energy_sensor = config.get('grid_from_energy_sensor')
        grid_to_energy_sensor = config.get('grid_to_energy_sensor')
        battery_charge_from_grid_sensor = config.get('battery_charge_from_grid_sensor')
        battery_charge_from_pv_sensor = config.get('battery_charge_from_pv_sensor')
        battery_discharge_sensor = config.get('battery_discharge_sensor')
        pv_energy_sensors = [
            config.get('pv_energy_pv1_inverter1_sensor'),
            config.get('pv_energy_pv2_inverter1_sensor'),
            config.get('pv_energy_pv1_inverter2_sensor'),
            config.get('pv_energy_pv2_inverter2_sensor')
        ]
        pv_energy_sensors = [s for s in pv_energy_sensors if s]

        if (grid_from_energy_sensor and grid_to_energy_sensor and
            battery_discharge_sensor and battery_charge_from_grid_sensor and battery_charge_from_pv_sensor):
            # Use energy sensor mode (same as log calculation)
            logger.info("Using ENERGY sensor mode (cumulative kWh)")

            # Helper function to get energy delta (reuse same logic as log)
            def get_energy_delta(sensor_id):
                """Get energy delta for the hour by reading cumulative sensor at start and end"""
                try:
                    # Get value at start of hour
                    start_history = ha_client.get_history(sensor_id, start_time, start_time + timedelta(seconds=1))
                    if not start_history or len(start_history) == 0:
                        logger.warning(f"No history data for {sensor_id} at start time {start_time}")
                        return 0.0

                    start_entry = start_history[0]
                    start_state = start_entry.get('state')
                    if start_state in ['unknown', 'unavailable', None]:
                        logger.warning(f"Sensor {sensor_id} unavailable at start time")
                        return 0.0

                    start_value = float(start_state)

                    # Get value at end of hour
                    end_history = ha_client.get_history(sensor_id, end_time - timedelta(seconds=1), end_time)
                    if not end_history or len(end_history) == 0:
                        logger.warning(f"No history data for {sensor_id} at end time {end_time}")
                        return 0.0

                    end_entry = end_history[-1]  # Get last entry
                    end_state = end_entry.get('state')
                    if end_state in ['unknown', 'unavailable', None]:
                        logger.warning(f"Sensor {sensor_id} unavailable at end time")
                        return 0.0

                    end_value = float(end_state)

                    # Calculate delta
                    delta = end_value - start_value

                    # Handle sensor resets (negative delta)
                    if delta < 0:
                        logger.warning(f"Sensor {sensor_id} reset detected (delta={delta:.3f}), using 0")
                        return 0.0

                    return delta

                except Exception as e:
                    logger.error(f"Error getting energy delta for {sensor_id}: {e}")
                    return 0.0

            # Get deltas for all sensors
            grid_from_kwh = get_energy_delta(grid_from_energy_sensor)
            grid_to_kwh = get_energy_delta(grid_to_energy_sensor)
            batt_discharge_kwh = get_energy_delta(battery_discharge_sensor)
            batt_charge_grid_kwh = get_energy_delta(battery_charge_from_grid_sensor)
            batt_charge_pv_kwh = get_energy_delta(battery_charge_from_pv_sensor)

            # Get PV energy (sum of all PV strings)
            pv_kwh = 0.0
            if pv_energy_sensors:
                for pv_sensor in pv_energy_sensors:
                    pv_kwh += get_energy_delta(pv_sensor)

            # Calculate net values
            grid_net_kwh = grid_from_kwh - grid_to_kwh
            batt_net_kwh = batt_discharge_kwh - batt_charge_grid_kwh - batt_charge_pv_kwh

            # Calculate home consumption (same formula as log)
            home_consumption_kwh = grid_net_kwh + pv_kwh + batt_net_kwh

            logger.info(f"Energy sensor mode:")
            logger.info(f"  Grid: FROM={grid_from_kwh:.3f} - TO={grid_to_kwh:.3f} = NET={grid_net_kwh:.3f} kWh")
            logger.info(f"  Battery: DISCH={batt_discharge_kwh:.3f} - CHG_GRID={batt_charge_grid_kwh:.3f} - CHG_PV={batt_charge_pv_kwh:.3f} = NET={batt_net_kwh:.3f} kWh")
            logger.info(f"  PV: {pv_kwh:.3f} kWh")
            logger.info(f"  ➜ HOME = GridNet({grid_net_kwh:+.3f}) + PV({pv_kwh:.3f}) + BattNet({batt_net_kwh:+.3f}) = {home_consumption_kwh:.3f} kWh")

            # Validate result
            if home_consumption_kwh < 0:
                logger.warning(f"Negative home consumption {home_consumption_kwh:.3f} kWh - likely sensor error")
                return None

            return home_consumption_kwh

        # If energy sensors are not configured, we cannot calculate consumption
        logger.error("Energy sensors not configured! Required: grid_from_energy_sensor, grid_to_energy_sensor, "
                    "battery_discharge_sensor, battery_charge_from_grid_sensor, battery_charge_from_pv_sensor")
        return None

    except Exception as e:
        logger.error(f"Error calculating home consumption: {e}", exc_info=True)
        return None


def get_consumption_kwh(ha_client, consumption_sensor, timestamp):
    """
    DEPRECATED: Use get_home_consumption_kwh() instead.

    Get consumption in kWh for recording, handling both power (W/kW) and energy (kWh) sensors.

    For power sensors (W/kW): Fetches last hour's history, calculates average, converts to kWh
    For energy sensors (kWh): Returns current value directly

    Args:
        ha_client: Home Assistant API client
        consumption_sensor: Sensor entity ID
        timestamp: Current timestamp

    Returns:
        float: Consumption in kWh, or None if error/unavailable
    """
    try:
        # Get sensor info with attributes to determine unit
        sensor_data = ha_client.get_state_with_attributes(consumption_sensor)
        if not sensor_data:
            logger.warning(f"Could not get sensor data for {consumption_sensor}")
            return None

        state = sensor_data.get('state')
        if state in ['unknown', 'unavailable', None]:
            logger.debug(f"Sensor {consumption_sensor} unavailable")
            return None

        # Get unit of measurement
        attributes = sensor_data.get('attributes', {})
        unit = attributes.get('unit_of_measurement', '').upper()

        logger.debug(f"Sensor {consumption_sensor}: state={state}, unit={unit}")

        # Handle different units
        if unit in ['KWH', 'KILOWATTHOUR']:
            # Energy sensor - use value directly
            try:
                consumption_kwh = float(state)
                logger.debug(f"Energy sensor (kWh): {consumption_kwh} kWh")
                return consumption_kwh
            except (ValueError, TypeError):
                logger.warning(f"Could not convert {state} to float")
                return None

        elif unit in ['W', 'WATT', 'KW', 'KILOWATT']:
            # Power sensor - need to calculate average over last hour
            logger.info(f"Power sensor detected ({unit}) - fetching hourly history for accurate consumption")

            # Calculate time range: from 1 hour ago to now
            from datetime import timedelta
            end_time = timestamp
            start_time = timestamp - timedelta(hours=1)

            # Fetch history
            history = ha_client.get_history(consumption_sensor, start_time, end_time)
            if not history or len(history) == 0:
                logger.warning(f"No history data available for {consumption_sensor}")
                # Fallback: use current value as snapshot
                try:
                    current_value = float(state)
                    if unit in ['W', 'WATT']:
                        consumption_kwh = current_value / 1000  # W to kWh (assuming 1 hour)
                    else:  # kW
                        consumption_kwh = current_value  # kW * 1h = kWh
                    logger.warning(f"Using snapshot value: {consumption_kwh:.3f} kWh")
                    return consumption_kwh
                except (ValueError, TypeError):
                    return None

            # Calculate average power from all readings
            valid_values = []
            for entry in history:
                try:
                    value_state = entry.get('state')
                    if value_state not in ['unknown', 'unavailable', None, '']:
                        value = float(value_state)

                        # Skip negative or unrealistically high values
                        if value < 0:
                            continue
                        if value > 1000000:  # > 1 MW seems like an error
                            continue

                        valid_values.append(value)
                except (ValueError, TypeError):
                    continue

            if not valid_values:
                logger.warning(f"No valid values in history for {consumption_sensor}")
                return None

            # Calculate average
            avg_power = sum(valid_values) / len(valid_values)

            # Convert to kWh
            if unit in ['W', 'WATT']:
                consumption_kwh = avg_power / 1000  # W to kW, then * 1h = kWh
            else:  # kW, KILOWATT
                consumption_kwh = avg_power  # kW * 1h = kWh

            logger.info(f"Calculated from {len(valid_values)} samples: avg={avg_power:.1f} {unit} → {consumption_kwh:.3f} kWh")
            return consumption_kwh

        else:
            logger.error(f"⚠️ Unknown sensor unit '{unit}' for {consumption_sensor}. "
                        f"Expected: W, kW, or kWh. Please check sensor configuration.")
            return None

    except Exception as e:
        logger.error(f"Error getting consumption from {consumption_sensor}: {e}", exc_info=True)
        return None


def controller_loop():
    """Background thread for battery control"""
    import time
    logger.info("Controller loop started")

    # Ladeplan-Update Intervall (alle 5 Minuten)
    last_plan_update = None
    plan_update_interval = 300  # 5 Minuten

    # v0.4.0 - Consumption recording (every hour)
    last_consumption_recording = None
    consumption_recording_interval = 3600  # 1 Stunde

    # v1.2.0-beta.51 - Mode switching hysteresis to prevent frequent changes
    last_mode_switch_time = None
    last_mode_state = None  # Track last mode to detect changes

    # v1.1.0 - Initialize SOC before first plan calculation
    if ha_client:
        try:
            soc_sensor = config.get('battery_soc_sensor', 'sensor.zwh8_8500_battery_soc')
            soc_value = ha_client.get_state(soc_sensor)
            if soc_value and soc_value != 'unavailable':
                app_state['battery']['soc'] = float(soc_value)
                logger.info(f"✓ Initial SOC loaded: {app_state['battery']['soc']:.1f}%")
            else:
                logger.warning(f"SOC sensor {soc_sensor} not available at startup, will retry in loop")
        except Exception as e:
            logger.warning(f"Could not load initial SOC: {e}")

    # v0.3.1 - Calculate charging plan immediately on startup
    update_charging_plan()

    # v1.2.0 - Calculate initial rolling battery schedule on startup
    if ha_client and tibber_optimizer and consumption_learner:
        try:
            logger.info("Calculating initial 24h rolling battery schedule...")
            soc_sensor = config.get('battery_soc_sensor', 'sensor.zwh8_8500_battery_soc')
            soc_value = ha_client.get_state(soc_sensor)
            current_soc = float(soc_value) if soc_value and soc_value != 'unavailable' else app_state['battery'].get('soc', 50)

            # Get Tibber prices
            tibber_sensor = config.get('tibber_price_sensor', 'sensor.tibber_prices')
            attrs = ha_client.get_attributes(tibber_sensor)
            prices = []
            if attrs:
                today_prices = attrs.get('today', [])
                tomorrow_prices = attrs.get('tomorrow', [])
                prices = today_prices + tomorrow_prices

            # Generate initial rolling schedule (24h from now)
            schedule = tibber_optimizer.plan_battery_schedule_rolling(
                ha_client=ha_client,
                config=config,
                current_soc=current_soc,
                prices=prices,
                lookahead_hours=24
            )

            if schedule:
                app_state['daily_battery_schedule'] = schedule
                logger.info(f"✓ Initial rolling schedule calculated: "
                          f"{len(schedule.get('charging_windows', []))} charging windows, "
                          f"min SOC {schedule.get('min_soc_reached', 0):.1f}%")

                # v1.2.0-beta.22: Initialize inverter mode based on schedule
                # Check if we should be charging NOW (hour 0 in rolling window)
                if kostal_api and modbus_client and config.get('auto_optimization_enabled', True):
                    try:
                        charging_windows = schedule.get('charging_windows', [])
                        should_charge_now = False
                        charge_reason = ""

                        for window in charging_windows:
                            if window['hour'] == 0:  # Hour 0 = NOW
                                should_charge_now = True
                                charge_reason = (f"{window['charge_kwh']:.2f} kWh @ {window['price']*100:.2f} Ct/kWh "
                                               f"({window['reason']})")
                                break

                        if should_charge_now:
                            # Start charging immediately
                            logger.info(f"🔌 STARTUP: Charging required NOW - activating external control")
                            kostal_api.set_external_control(True)
                            charge_power = -config['max_charge_power']
                            modbus_client.write_battery_power(charge_power)
                            app_state['inverter']['mode'] = 'auto_charging'
                            app_state['inverter']['control_mode'] = 'external'
                            add_log('INFO', f'Startup: Started charging - {charge_reason}')
                            last_mode_switch_time = datetime.now()  # v1.2.0-beta.51: Initialize hysteresis timer
                        else:
                            # No charging required - ensure internal mode
                            logger.info(f"✅ STARTUP: No charging required - setting internal control mode")
                            modbus_client.write_battery_power(0)
                            kostal_api.set_external_control(False)
                            app_state['inverter']['mode'] = 'automatic'
                            app_state['inverter']['control_mode'] = 'internal'
                            add_log('INFO', 'Startup: Set to internal mode (no charging planned)')
                            last_mode_switch_time = datetime.now()  # v1.2.0-beta.51: Initialize hysteresis timer

                    except Exception as e:
                        logger.error(f"Error initializing inverter mode at startup: {e}", exc_info=True)
            else:
                logger.warning("Failed to generate initial rolling battery schedule")
        except Exception as e:
            logger.error(f"Error calculating initial rolling schedule: {e}", exc_info=True)

    last_plan_update = datetime.now()

    while True:
        try:
            # Update charging plan periodically (v0.3.0, enhanced v0.9.0)
            now = datetime.now()
            if (last_plan_update is None or
                (now - last_plan_update).total_seconds() > plan_update_interval):
                update_charging_plan()

                # v0.9.0 - Calculate daily battery schedule with predictive optimization
                if ha_client and tibber_optimizer and consumption_learner:
                    try:
                        # Get current SOC with proper error handling
                        soc_sensor = config.get('battery_soc_sensor', 'sensor.zwh8_8500_battery_soc')
                        soc_value = ha_client.get_state(soc_sensor)

                        # Convert to float with proper fallback
                        if soc_value is None or soc_value == '' or soc_value == 'unavailable':
                            logger.warning(f"SOC sensor {soc_sensor} unavailable, using app_state value")
                            current_soc = app_state['battery'].get('soc', 50)
                        else:
                            try:
                                current_soc = float(soc_value)
                                if current_soc < 0 or current_soc > 100:
                                    logger.warning(f"SOC value {current_soc}% out of range, using app_state value")
                                    current_soc = app_state['battery'].get('soc', 50)
                            except (ValueError, TypeError):
                                logger.warning(f"Invalid SOC value '{soc_value}', using app_state value")
                                current_soc = app_state['battery'].get('soc', 50)

                        # Get Tibber prices for today + tomorrow
                        prices = []
                        tibber_sensor = config.get('tibber_price_sensor', 'sensor.tibber_prices')
                        attrs = ha_client.get_attributes(tibber_sensor)
                        if attrs:
                            today_prices = attrs.get('today', [])
                            tomorrow_prices = attrs.get('tomorrow', [])
                            prices = today_prices + tomorrow_prices

                        # v1.2.0 - Generate rolling 24h schedule
                        schedule = tibber_optimizer.plan_battery_schedule_rolling(
                            ha_client=ha_client,
                            config=config,
                            current_soc=current_soc,
                            prices=prices,
                            lookahead_hours=24
                        )

                        if schedule:
                            app_state['daily_battery_schedule'] = schedule
                            logger.info(f"✓ Rolling battery schedule updated: "
                                      f"{len(schedule.get('charging_windows', []))} charging windows, "
                                      f"min SOC {schedule.get('min_soc_reached', 0):.1f}%")
                        else:
                            logger.warning("Failed to generate rolling battery schedule")

                    except Exception as e:
                        logger.error(f"Error updating daily battery schedule: {e}", exc_info=True)

                last_plan_update = now

            # Record consumption periodically (v0.4.0, improved in v1.2.0-beta.8)
            if (consumption_learner and ha_client and
                config.get('enable_consumption_learning', True)):
                if (last_consumption_recording is None or
                    (now - last_consumption_recording).total_seconds() > consumption_recording_interval):
                    try:
                        # v1.2.0-beta.8: Calculate actual home consumption from grid + PV sensors
                        consumption_kwh = get_home_consumption_kwh(ha_client, config, now)

                        if consumption_kwh is not None:
                            # Validate: negative values should not occur with correct calculation
                            if consumption_kwh < 0:
                                add_log('WARNING', f'⚠️ Negativer Hausverbrauch: {consumption_kwh:.3f} kWh (Sensorfehler - Wert ignoriert)')
                            else:
                                consumption_learner.record_consumption(now, consumption_kwh)
                                logger.info(f"✓ Recorded consumption: {consumption_kwh:.3f} kWh at {now.strftime('%H:%M')}")

                            last_consumption_recording = now
                    except Exception as e:
                        logger.error(f"Error recording consumption: {e}", exc_info=True)

            if app_state['controller_running'] and config.get('auto_optimization_enabled', True):
                # v0.3.0 - Intelligent Tibber-based charging
                if ha_client and kostal_api and modbus_client and tibber_optimizer:
                    try:
                        # Hole aktuelle Werte
                        current_soc = float(ha_client.get_state(
                            config.get('battery_soc_sensor', 'sensor.zwh8_8500_battery_soc')
                        ) or 0)
                        app_state['battery']['soc'] = current_soc

                        # v0.3.4 - Use existing parameters consistently
                        min_soc = int(config.get('auto_safety_soc', 20))
                        max_soc = int(config.get('auto_charge_below_soc', 95))

                        # v0.9.0 - Use daily battery schedule for charging decisions
                        should_charge = False
                        reason = "No action"

                        # Safety check: SOC too low
                        if current_soc < min_soc:
                            should_charge = True
                            reason = f"SAFETY: SOC below minimum ({current_soc:.1f}% < {min_soc}%)"

                        # Safety check: Battery full
                        elif current_soc >= max_soc:
                            should_charge = False
                            reason = f"Battery full ({current_soc:.1f}% >= {max_soc}%)"

                        # v1.2.0: Use rolling schedule if available
                        elif app_state['daily_battery_schedule']:
                            schedule = app_state['daily_battery_schedule']

                            # Rolling window: hour 0 = now, hour 1 = now+1h, etc.
                            # Check if hour 0 (current hour) is a charging window
                            charging_windows = schedule.get('charging_windows', [])
                            current_window = None
                            for window in charging_windows:
                                if window['hour'] == 0:  # Hour 0 = charge NOW
                                    current_window = window
                                    break

                            if current_window:
                                should_charge = True
                                reason = (f"Rolling schedule: {current_window['charge_kwh']:.2f} kWh "
                                        f"@ {current_window['price']*100:.2f} Cent/kWh "
                                        f"({current_window['reason']})")
                                logger.info(f"🔌 CHARGING NOW (rolling hour 0): {reason}")
                            else:
                                should_charge = False
                                min_soc_forecast = schedule.get('min_soc_reached', 100)
                                reason = f"No charging needed - Rolling schedule OK (min SOC: {min_soc_forecast:.1f}%)"

                                # Debug: Show upcoming charging windows
                                upcoming_windows = [w['hour'] for w in charging_windows if w['hour'] < 6]
                                if upcoming_windows:
                                    logger.debug(f"⏸️ NOT charging now. Upcoming windows in next 6h: {upcoming_windows}")
                                else:
                                    logger.debug(f"⏸️ NOT charging - no charging windows in next 6h")

                        else:
                            # No schedule available - don't charge
                            should_charge = False
                            reason = "No charging schedule available - waiting for next calculation"
                            logger.warning("⚠️ No daily battery schedule available for charging decision")

                        # v1.2.0-beta.51 - Hysteresis logic to prevent frequent mode switching
                        hysteresis_seconds = config.get('mode_switch_hysteresis_seconds', 900)
                        current_mode_is_charging = app_state['inverter']['mode'] in ['manual_charging', 'auto_charging']
                        time_since_last_switch = None

                        if last_mode_switch_time:
                            time_since_last_switch = (now - last_mode_switch_time).total_seconds()

                        # Determine if we want to change mode
                        wants_mode_change = (should_charge and not current_mode_is_charging) or \
                                           (not should_charge and current_mode_is_charging)

                        # Check if enough time has passed since last mode switch
                        can_switch = True
                        if wants_mode_change and time_since_last_switch is not None:
                            if time_since_last_switch < hysteresis_seconds:
                                # Safety override: If SOC is critically low, allow immediate switch to charging
                                is_safety_charge = should_charge and current_soc < min_soc
                                if not is_safety_charge:
                                    can_switch = False
                                    remaining = hysteresis_seconds - time_since_last_switch
                                    logger.debug(f"⏸️ Mode switch blocked by hysteresis: {remaining:.0f}s remaining "
                                               f"(want {'charging' if should_charge else 'internal'}, "
                                               f"currently {'charging' if current_mode_is_charging else 'internal'})")

                        # Aktion ausführen (v1.2.0-beta.51 - with hysteresis)
                        if should_charge and not current_mode_is_charging:
                            if can_switch:
                                # Starte automatisches Laden
                                kostal_api.set_external_control(True)
                                charge_power = -config['max_charge_power']
                                modbus_client.write_battery_power(charge_power)
                                app_state['inverter']['mode'] = 'auto_charging'
                                app_state['inverter']['control_mode'] = 'external'
                                add_log('INFO', f'Auto-Optimization started charging: {reason}')
                                last_mode_switch_time = now
                                logger.info(f"🔄 Mode switched: internal → charging (hysteresis reset)")

                        elif not should_charge and current_mode_is_charging:
                            if can_switch:
                                # Stoppe automatisches Laden
                                modbus_client.write_battery_power(0)
                                kostal_api.set_external_control(False)
                                app_state['inverter']['mode'] = 'automatic'
                                app_state['inverter']['control_mode'] = 'internal'
                                add_log('INFO', f'Auto-Optimization stopped charging: {reason}')
                                last_mode_switch_time = now
                                logger.info(f"🔄 Mode switched: charging → internal (hysteresis reset)")

                    except Exception as e:
                        logger.error(f"Error in auto-optimization: {e}")

            # Sleep for control interval
            time.sleep(config.get('control_interval', 30))

        except Exception as e:
            logger.error(f"Error in controller loop: {e}")
            add_log('ERROR', f'Controller error: {str(e)}')

# Start controller thread
controller_thread = threading.Thread(target=controller_loop, daemon=True)
controller_thread.start()

# ==============================================================================
# Main Entry Point
# ==============================================================================

if __name__ == '__main__':
    port = int(os.getenv('PORT', 8099))
    logger.info(f"Starting Flask app on port {port}")
    logger.info(f"Inverter: {config['inverter_ip']}:{config['inverter_port']}")
    add_log('INFO', f'Application started on port {port}')
    
    app.run(host='0.0.0.0', port=port, debug=(log_level == 'DEBUG'))
