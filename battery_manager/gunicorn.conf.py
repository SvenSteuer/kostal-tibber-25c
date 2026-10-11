"""
Kostal Battery Manager - gunicorn configuration (v1.3.5, H5)

Releases the inverter (register 1034 = 0, then Battery:ExternControl = 0) when the
worker is stopped, e.g. when the add-on is stopped, restarted or updated. The
inverter has no watchdog and would otherwise keep an external charge setpoint.

- SIGTERM: chained handler, releases immediately - before gunicorn waits for
  running requests (the container may SIGKILL a few seconds after SIGTERM).
- worker_exit: fallback for all other exit paths (SIGINT/SIGQUIT, SIGABRT on a
  worker timeout). Runs in the worker process after its main loop has ended.

SIGKILL cannot be handled; the next start sets internal mode anyway (H4).
All other gunicorn settings stay on the command line in run.sh.
"""

import signal
import sys


def _release_inverter(reason):
    app_module = sys.modules.get('battery_manager.app')
    if app_module is None:
        return
    try:
        app_module.release_inverter_on_shutdown(reason)
    except Exception as e:
        sys.stderr.write(f"Kostal Battery Manager: inverter release on {reason} failed: {e}\n")


def post_worker_init(worker):
    """Chain a SIGTERM handler in front of gunicorn's own (worker.handle_exit)."""
    previous_handler = signal.getsignal(signal.SIGTERM)

    def handle_sigterm(signum, frame):
        _release_inverter('SIGTERM')
        if callable(previous_handler):
            previous_handler(signum, frame)

    signal.signal(signal.SIGTERM, handle_sigterm)
    signal.siginterrupt(signal.SIGTERM, False)


def worker_exit(server, worker):
    """Fallback: release on every other worker exit (no-op if already released)."""
    _release_inverter('worker exit')
