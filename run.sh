#!/usr/bin/with-contenv bashio

# ==============================================================================
# Kostal Battery Manager - Start Script
# ==============================================================================

bashio::log.info "Starting Kostal Battery Manager..."

# Get configuration from Home Assistant
export CONFIG_PATH=/data/options.json
export INVERTER_IP=$(bashio::config 'inverter_ip')
export INVERTER_PORT=$(bashio::config 'inverter_port')

# Log level is now managed via Web GUI, use INFO as default
export LOG_LEVEL="INFO"
export SUPERVISOR_TOKEN="${SUPERVISOR_TOKEN}"
export HASSIO_API="http://supervisor/core"

bashio::log.info "Configuration loaded:"
bashio::log.info "  Inverter: ${INVERTER_IP}:${INVERTER_PORT}"
bashio::log.info "  Log Level: ${LOG_LEVEL} (managed via Web GUI)"

# Ensure data directory exists
mkdir -p /data

# Start Flask application with Gunicorn
bashio::log.info "Starting web server on port 8099..."

cd /app
exec gunicorn \
    --bind 0.0.0.0:8099 \
    --workers 1 \
    --threads 4 \
    --timeout 120 \
    --access-logfile - \
    --error-logfile - \
    --log-level "${LOG_LEVEL}" \
    battery_manager.app:app
