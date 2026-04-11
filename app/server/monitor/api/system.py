"""
System health and info API.

Endpoints:
  GET /system/health  - CPU temp, CPU%, RAM%, disk usage, warnings
  GET /system/info    - firmware version, uptime, hostname, OS version
"""

from flask import Blueprint, current_app, jsonify

from monitor.auth import login_required
from monitor.services.health import get_health_summary, get_uptime

system_bp = Blueprint("system", __name__)


def _read_os_release():
    """Read /etc/os-release into a dict. Returns empty dict on failure."""
    try:
        with open("/etc/os-release") as f:
            result = {}
            for line in f:
                line = line.strip()
                if "=" in line:
                    key, _, value = line.partition("=")
                    result[key] = value.strip('"')
            return result
    except OSError:
        return {}


@system_bp.route("/health", methods=["GET"])
@login_required
def health():
    """Return system health metrics."""
    data_dir = current_app.config.get("DATA_DIR", "/data")
    summary = get_health_summary(data_dir)
    return jsonify(summary), 200


@system_bp.route("/info", methods=["GET"])
@login_required
def info():
    """Return system info."""
    settings = current_app.store.get_settings()
    uptime = get_uptime()
    os_info = _read_os_release()
    return jsonify(
        {
            "hostname": settings.hostname,
            "firmware_version": settings.firmware_version,
            "uptime": uptime,
            "os_name": os_info.get("PRETTY_NAME", "Unknown"),
            "os_version": os_info.get("VERSION_ID", ""),
            "os_build": os_info.get("BUILD_ID", ""),
            "os_variant": os_info.get("VARIANT_ID", ""),
        }
    ), 200
