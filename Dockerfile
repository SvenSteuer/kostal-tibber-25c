ARG BUILD_FROM
FROM $BUILD_FROM

# Install Python and dependencies
RUN apk add --no-cache \
    python3 \
    py3-pip \
    py3-cryptography \
    py3-cffi \
    gcc \
    musl-dev \
    python3-dev \
    libffi-dev \
    openssl-dev \
    bash \
    && pip3 install --upgrade pip

# Set working directory
WORKDIR /app

# Copy requirements first (for better caching)
COPY requirements.txt .

# Install Python packages
RUN pip3 install --no-cache-dir -r requirements.txt

# Copy application
COPY battery_manager ./battery_manager
COPY run.sh .
RUN chmod a+x run.sh

# Expose port for Ingress
EXPOSE 8099

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python3 -c "import requests; requests.get('http://localhost:8099/api/status', timeout=5)" || exit 1

# v1.3.5: s6-overlay (PID 1) sends SIGTERM to gunicorn only in its final kill phase and
# SIGKILL after S6_KILL_GRACETIME (default 3000 ms). The inverter release on stop
# (gunicorn.conf.py) needs Modbus + REST GET/PUT, possibly a re-login - give it 10 s.
# Note: s6 waits this time unconditionally, so every add-on stop takes ~10 s.
ENV S6_KILL_GRACETIME=10000

# Start the application
CMD [ "./run.sh" ]
