"""
WiFi setup for camera first boot + authenticated status page.

On first boot (when /data/.setup-done doesn't exist):
1. Starts a WiFi hotspot "HomeCam-Setup"
2. Runs a tiny HTTP server on port 80 with a setup page
3. User enters WiFi SSID + password + server IP + camera password
4. Camera saves settings, tears down hotspot, connects to home WiFi
5. If WiFi fails, hotspot comes back up for retry

After setup, port 80 serves a login-protected status page where the
user can view camera info and change WiFi with the password they set
during provisioning.

Single-radio constraint: wlan0 can either be an AP or a client, not both.
So we can't scan while hotspot is active. User types SSID manually or we
do a quick scan BEFORE starting the hotspot.
"""
import http.server
import json
import os
import secrets
import subprocess
import threading
import time
import logging
from pathlib import Path

from camera_streamer import led

log = logging.getLogger("camera-streamer.wifi-setup")

HOTSPOT_SSID = "HomeCam-Setup"
HOTSPOT_PASS = "homecamera"
CONN_NAME = "HomeCam-Setup"
IFACE = "wlan0"
SETUP_STAMP = ".setup-done"
LISTEN_PORT = 80
# Seconds to wait after sending response before tearing down AP
CONNECT_DELAY = 3
# Session timeout in seconds (2 hours)
SESSION_TIMEOUT = 7200


# Template directory (adjacent to this module)
_TEMPLATE_DIR = Path(__file__).parent / "templates"

# Cache loaded templates to avoid re-reading on every request
_template_cache = {}


def _load_template(name):
    """Load an HTML template from the templates/ directory.

    Templates are cached in memory after first load.
    Placeholders like {{CAMERA_ID}} are left for runtime replacement.
    """
    if name in _template_cache:
        return _template_cache[name]
    path = _TEMPLATE_DIR / name
    try:
        html = path.read_text(encoding="utf-8")
    except OSError:
        log.error("Template not found: %s", path)
        html = f"<h1>Template Error</h1><p>Missing: {name}</p>"
    _template_cache[name] = html
    return html


def is_setup_complete(data_dir):
    """Check if camera has been configured."""
    return os.path.isfile(os.path.join(data_dir, SETUP_STAMP))


def mark_setup_complete(data_dir):
    """Write setup-done stamp file."""
    os.makedirs(data_dir, exist_ok=True)
    with open(os.path.join(data_dir, SETUP_STAMP), "w") as f:
        f.write("1\n")


# ============================================================
# Session store (in-memory, survives within one process lifetime)
# ============================================================
_sessions = {}  # token -> expiry timestamp
_session_lock = threading.Lock()


def _create_session():
    """Create a new session token."""
    token = secrets.token_hex(32)
    with _session_lock:
        _sessions[token] = time.time() + SESSION_TIMEOUT
    return token


def _check_session(token):
    """Return True if session token is valid and not expired."""
    if not token:
        return False
    with _session_lock:
        expiry = _sessions.get(token)
        if expiry is None:
            return False
        if time.time() > expiry:
            del _sessions[token]
            return False
        # Refresh expiry on activity
        _sessions[token] = time.time() + SESSION_TIMEOUT
        return True


def _destroy_session(token):
    """Remove a session."""
    if token:
        with _session_lock:
            _sessions.pop(token, None)


def _get_session_cookie(headers):
    """Extract session token from Cookie header."""
    cookie_header = headers.get("Cookie", "")
    for part in cookie_header.split(";"):
        part = part.strip()
        if part.startswith("cam_session="):
            return part.split("=", 1)[1]
    return ""


# ============================================================
# System info helpers (for status page)
# ============================================================
def _get_cpu_temp():
    """Read CPU temperature in Celsius."""
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return round(int(f.read().strip()) / 1000.0, 1)
    except (OSError, ValueError):
        return 0.0


def _get_uptime():
    """Get human-readable uptime."""
    try:
        with open("/proc/uptime") as f:
            seconds = int(float(f.read().split()[0]))
    except (OSError, ValueError, IndexError):
        seconds = 0
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    minutes = (seconds % 3600) // 60
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


def _get_memory_mb():
    """Get total and used memory in MB."""
    try:
        with open("/proc/meminfo") as f:
            lines = f.readlines()
        info = {}
        for line in lines:
            parts = line.split()
            if len(parts) >= 2:
                info[parts[0].rstrip(":")] = int(parts[1])
        total = info.get("MemTotal", 0) // 1024
        available = info.get("MemAvailable", 0) // 1024
        return total, total - available
    except (OSError, ValueError):
        return 0, 0


# ============================================================
# WifiSetupServer — first boot provisioning
# ============================================================
class WifiSetupServer:
    """HTTP server for camera WiFi and server configuration."""

    def __init__(self, config):
        self._config = config
        self._server = None
        self._thread = None
        self._hotspot_active = False
        self._cached_networks = []
        self._connect_result = None  # None=not tried, True=ok, str=error

    def needs_setup(self):
        """Return True if setup hasn't been completed."""
        return not is_setup_complete(self._config.data_dir)

    def start(self):
        """Scan networks, start hotspot, then start HTTP server."""
        if not self.needs_setup():
            log.info("Setup already complete, skipping")
            return False

        # Scan BEFORE starting hotspot (wlan0 is in client mode now)
        self._cached_networks = self._scan_wifi()
        log.info("Pre-scan found %d networks", len(self._cached_networks))

        if not self._start_hotspot():
            log.warning("Could not start hotspot — setup via ethernet")

        # LED: slow blink = waiting for setup
        led.setup_mode()

        # Start HTTP server
        handler = _make_handler(self._config, self)
        self._server = http.server.HTTPServer(("0.0.0.0", LISTEN_PORT), handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True, name="setup-http"
        )
        self._thread.start()
        log.info("Setup server listening on port %d", LISTEN_PORT)
        return True

    def stop(self):
        """Stop HTTP server and hotspot."""
        if self._server:
            self._server.shutdown()
            self._server = None
        self._stop_hotspot()
        led.off()
        log.info("Setup server stopped")

    def save_and_connect(self, ssid, password, server_ip, server_port="8554",
                         admin_username="admin", admin_password=""):
        """Save settings and attempt WiFi connection in background.

        Returns immediately so the HTTP response reaches the phone
        before the hotspot is torn down.
        """
        self._connect_result = None

        # Save admin credentials immediately (before WiFi connect attempt)
        if admin_password:
            if admin_username:
                self._config.update(ADMIN_USERNAME=admin_username)
            self._config.set_password(admin_password)
            self._config.save()
            log.info("Camera admin credentials set during provisioning "
                     "(user=%s)", admin_username)

        t = threading.Thread(
            target=self._do_connect,
            args=(ssid, password, server_ip, server_port),
            daemon=True,
            name="wifi-connect",
        )
        t.start()

    def _do_connect(self, ssid, password, server_ip, server_port):
        """Background: wait for phone to get response, then connect."""
        # Give the phone time to receive the HTTP response
        time.sleep(CONNECT_DELAY)

        log.info("Tearing down hotspot, connecting to WiFi: %s", ssid)
        led.connecting()

        # Tear down hotspot
        self._stop_hotspot()

        # Short pause for interface to settle
        time.sleep(2)

        # Try to connect
        ok, err = self._connect_wifi(ssid, password)

        if ok:
            log.info("WiFi connected! Saving config and completing setup.")
            self._config.update(SERVER_IP=server_ip, SERVER_PORT=server_port)
            mark_setup_complete(self._config.data_dir)
            self._set_unique_hostname()
            self._connect_result = True
            led.connected()
        else:
            log.error("WiFi connection failed: %s — restarting hotspot", err)
            self._connect_result = err or "Connection failed"
            led.error()
            # Restart hotspot so user can retry
            time.sleep(2)
            self._start_hotspot()
            led.setup_mode()

    def _set_unique_hostname(self):
        """Set unique hostname using CPU serial suffix.

        Format: rpi-divinu-cam-XXXX where XXXX = last 4 hex of serial.
        This avoids collisions when multiple cameras are on the network.
        Also sends the hostname in DHCP so routers display it.
        """
        try:
            serial = ""
            with open("/proc/cpuinfo", "r") as f:
                for line in f:
                    if line.startswith("Serial"):
                        serial = line.split(":")[-1].strip()
            suffix = serial[-4:] if serial else "0000"
            hostname = f"rpi-divinu-cam-{suffix}"

            # Set OS hostname
            subprocess.run(["hostname", hostname], capture_output=True, timeout=5)
            with open("/etc/hostname", "w") as f:
                f.write(hostname + "\n")

            # Tell NetworkManager so it sends hostname in DHCP requests
            subprocess.run(
                ["nmcli", "general", "hostname", hostname],
                capture_output=True, timeout=5,
            )

            # Restart avahi so mDNS advertises the new name
            subprocess.run(
                ["systemctl", "restart", "avahi-daemon"],
                capture_output=True, timeout=10,
            )

            log.info("Hostname set to %s", hostname)
        except Exception as e:
            log.warning("Failed to set hostname: %s", e)

    def get_status(self):
        """Return current connection attempt status."""
        return self._connect_result

    def get_cached_networks(self):
        """Return networks from pre-hotspot scan."""
        return list(self._cached_networks)

    def rescan(self):
        """Rescan by briefly dropping AP, scanning, then restarting AP.

        This is disruptive — phone will disconnect briefly.
        """
        log.info("Rescan requested — dropping AP briefly")
        self._stop_hotspot()
        time.sleep(2)
        self._cached_networks = self._scan_wifi()
        log.info("Rescan found %d networks", len(self._cached_networks))
        time.sleep(1)
        self._start_hotspot()
        return self._cached_networks

    def _scan_wifi(self):
        """Scan for WiFi networks (only works when NOT in AP mode)."""
        try:
            # Force a fresh scan
            subprocess.run(
                ["nmcli", "device", "wifi", "rescan", "ifname", IFACE],
                capture_output=True, timeout=10,
            )
            time.sleep(3)  # Give scan time to complete

            result = subprocess.run(
                ["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY",
                 "device", "wifi", "list", "ifname", IFACE],
                capture_output=True, text=True, timeout=15,
            )
            networks = []
            seen = set()
            for line in result.stdout.strip().splitlines():
                parts = line.split(":")
                if len(parts) >= 3 and parts[0] and parts[0] not in seen:
                    seen.add(parts[0])
                    networks.append({
                        "ssid": parts[0],
                        "signal": int(parts[1]) if parts[1].isdigit() else 0,
                        "security": parts[2],
                    })
            networks.sort(key=lambda n: n["signal"], reverse=True)
            return networks
        except Exception as e:
            log.error("WiFi scan failed: %s", e)
            return []

    def _connect_wifi(self, ssid, password):
        """Connect to a WiFi network (must NOT be in AP mode)."""
        try:
            result = subprocess.run(
                [
                    "nmcli", "device", "wifi", "connect", ssid,
                    "password", password,
                    "ifname", IFACE,
                ],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0:
                return True, ""
            err = result.stderr.strip() or result.stdout.strip()
            return False, err
        except subprocess.TimeoutExpired:
            return False, "Connection timed out"
        except Exception as e:
            return False, str(e)

    def _wait_for_wifi(self, max_wait=30):
        """Wait until wlan0 is recognized by NetworkManager as a wifi device."""
        log.info("Waiting for WiFi interface %s to be ready...", IFACE)
        for waited in range(max_wait):
            try:
                result = subprocess.run(
                    ["nmcli", "-t", "-f", "DEVICE,TYPE", "device", "status"],
                    capture_output=True, text=True, timeout=10,
                )
                for line in result.stdout.strip().splitlines():
                    parts = line.split(":")
                    if len(parts) >= 2 and parts[0] == IFACE and parts[1] == "wifi":
                        log.info("WiFi interface %s ready after %ds", IFACE, waited)
                        return True
            except Exception:
                pass
            time.sleep(1)
        log.warning("WiFi interface %s not ready after %ds", IFACE, max_wait)
        return False

    def _start_hotspot(self):
        """Start WiFi AP via NetworkManager."""
        try:
            # Wait for WiFi hardware to be ready (firmware may still load at boot)
            if not self._wait_for_wifi():
                log.warning("WiFi interface %s not found", IFACE)
                return False

            # Remove old connection
            subprocess.run(
                ["nmcli", "connection", "delete", CONN_NAME],
                capture_output=True, timeout=10,
            )

            # Create AP with shared mode (auto dnsmasq DHCP)
            subprocess.run(
                [
                    "nmcli", "connection", "add",
                    "type", "wifi",
                    "ifname", IFACE,
                    "con-name", CONN_NAME,
                    "autoconnect", "no",
                    "ssid", HOTSPOT_SSID,
                    "wifi.mode", "ap",
                    "wifi.band", "bg",
                    "wifi-sec.key-mgmt", "wpa-psk",
                    "wifi-sec.psk", HOTSPOT_PASS,
                    "ipv4.method", "shared",
                ],
                capture_output=True, text=True, timeout=15, check=True,
            )

            # Activate with explicit interface binding + retry.
            max_retries = 5
            for attempt in range(1, max_retries + 1):
                try:
                    subprocess.run(
                        ["nmcli", "connection", "up", CONN_NAME,
                         "ifname", IFACE],
                        capture_output=True, text=True, timeout=15, check=True,
                    )
                    break  # success
                except subprocess.CalledProcessError as e:
                    log.warning(
                        "Hotspot activation attempt %d/%d failed: %s",
                        attempt, max_retries,
                        e.stderr.strip() if e.stderr else str(e),
                    )
                    if attempt >= max_retries:
                        raise
                    time.sleep(2)

            self._hotspot_active = True
            log.info("Hotspot started: SSID=%s", HOTSPOT_SSID)
            return True

        except (subprocess.CalledProcessError, FileNotFoundError,
                subprocess.TimeoutExpired) as e:
            log.error("Failed to start hotspot: %s", e)
            return False

    def _stop_hotspot(self):
        """Stop and remove the hotspot connection."""
        if not self._hotspot_active:
            return
        try:
            subprocess.run(
                ["nmcli", "connection", "down", CONN_NAME],
                capture_output=True, timeout=10,
            )
            subprocess.run(
                ["nmcli", "connection", "delete", CONN_NAME],
                capture_output=True, timeout=10,
            )
            self._hotspot_active = False
            log.info("Hotspot stopped")
        except (subprocess.CalledProcessError, FileNotFoundError,
                subprocess.TimeoutExpired):
            pass


# ============================================================
# CameraStatusServer — post-setup authenticated status page
# ============================================================
class CameraStatusServer:
    """Lightweight HTTP server showing camera status after setup.

    Runs on port 80. Requires login with the password set during
    provisioning. Shows camera ID, WiFi, server connection, stream
    status, system health, and a form to change WiFi.
    """

    def __init__(self, config, stream_manager=None):
        self._config = config
        self._stream = stream_manager
        self._server = None
        self._thread = None

    def start(self):
        """Start the status HTTP server on port 80."""
        handler = _make_status_handler(self._config, self._stream, self)
        try:
            self._server = http.server.HTTPServer(("0.0.0.0", LISTEN_PORT), handler)
            self._thread = threading.Thread(
                target=self._server.serve_forever, daemon=True, name="status-http"
            )
            self._thread.start()
            log.info("Status server listening on port %d", LISTEN_PORT)
            return True
        except Exception as e:
            log.error("Failed to start status server: %s", e)
            return False

    def stop(self):
        """Stop the status HTTP server."""
        if self._server:
            self._server.shutdown()
            self._server = None
            log.info("Status server stopped")

    def connect_wifi(self, ssid, password):
        """Connect to a new WiFi network. Returns (ok, error)."""
        try:
            result = subprocess.run(
                ["nmcli", "device", "wifi", "connect", ssid,
                 "password", password, "ifname", IFACE],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0:
                return True, ""
            err = result.stderr.strip() or result.stdout.strip()
            return False, err
        except subprocess.TimeoutExpired:
            return False, "Connection timed out"
        except Exception as e:
            return False, str(e)


# ============================================================
# Status page HTTP handler (login-gated)
# ============================================================
def _make_status_handler(config, stream_manager, status_server):
    """Create HTTP handler for the camera status page."""

    class StatusHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            log.debug("Status HTTP: " + format % args)

        def _is_authenticated(self):
            """Check if request has a valid session cookie."""
            if not config.has_password:
                return True  # No password set = open access (legacy)
            token = _get_session_cookie(self.headers)
            return _check_session(token)

        def _require_auth(self):
            """Return True if authenticated, or send login redirect and return False."""
            if self._is_authenticated():
                return True
            # API calls get 401, browser gets login page
            if self.path.startswith("/api/"):
                self._json_response({"error": "Authentication required"}, 401)
            else:
                self.send_response(302)
                self.send_header("Location", "/login")
                self.end_headers()
            return False

        def do_GET(self):
            if self.path == "/login":
                self._serve_login_page()
            elif self.path == "/logout":
                token = _get_session_cookie(self.headers)
                _destroy_session(token)
                self.send_response(302)
                self.send_header("Set-Cookie",
                                 "cam_session=; Path=/; Max-Age=0; HttpOnly")
                self.send_header("Location", "/login")
                self.end_headers()
            elif self.path == "/" or self.path == "/status":
                if not self._require_auth():
                    return
                self._serve_status_page()
            elif self.path == "/api/status":
                if not self._require_auth():
                    return
                self._json_response(self._get_status())
            elif self.path == "/api/networks":
                if not self._require_auth():
                    return
                nets = self._scan_networks()
                self._json_response({"networks": nets})
            else:
                self.send_response(302)
                self.send_header("Location", "/")
                self.end_headers()

        def do_POST(self):
            content_len = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_len) if content_len > 0 else b""

            if self.path == "/login":
                self._handle_login(body)
            elif self.path == "/api/wifi":
                if not self._require_auth():
                    return
                try:
                    data = json.loads(body)
                    ssid = data.get("ssid", "").strip()
                    password = data.get("password", "")
                    if not ssid:
                        self._json_response({"error": "SSID required"}, 400)
                        return
                    if not password:
                        self._json_response({"error": "Password required"}, 400)
                        return

                    ok, err = status_server.connect_wifi(ssid, password)
                    if ok:
                        self._json_response({"message": f"Connected to {ssid}"})
                    else:
                        self._json_response(
                            {"error": err or "Connection failed"}, 500)
                except json.JSONDecodeError:
                    self._json_response({"error": "Invalid JSON"}, 400)
            elif self.path == "/api/password":
                if not self._require_auth():
                    return
                try:
                    data = json.loads(body)
                    current = data.get("current_password", "")
                    new_pw = data.get("new_password", "")
                    if not current or not new_pw:
                        self._json_response(
                            {"error": "Both current and new password required"}, 400)
                        return
                    if len(new_pw) < 4:
                        self._json_response(
                            {"error": "Password must be at least 4 characters"}, 400)
                        return
                    if not config.check_password(current):
                        self._json_response(
                            {"error": "Current password is incorrect"}, 403)
                        return
                    config.set_password(new_pw)
                    config.save()
                    self._json_response({"message": "Password changed"})
                except json.JSONDecodeError:
                    self._json_response({"error": "Invalid JSON"}, 400)
            else:
                self.send_error(404)

        def _handle_login(self, body):
            """Process login form or JSON login."""
            username = ""
            password = ""
            content_type = self.headers.get("Content-Type", "")

            if "application/json" in content_type:
                try:
                    data = json.loads(body)
                    username = data.get("username", "").strip()
                    password = data.get("password", "")
                except json.JSONDecodeError:
                    self._json_response({"error": "Invalid JSON"}, 400)
                    return
            else:
                # URL-encoded form
                from urllib.parse import parse_qs
                params = parse_qs(body.decode("utf-8", errors="replace"))
                username = params.get("username", [""])[0].strip()
                password = params.get("password", [""])[0]

            if not username or not password:
                self._serve_login_page(error="Username and password required")
                return

            if (username == config.admin_username
                    and config.check_password(password)):
                token = _create_session()
                log.info("Successful login from %s (user=%s)",
                         self.client_address[0], username)
                if "application/json" in content_type:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header(
                        "Set-Cookie",
                        f"cam_session={token}; Path=/; HttpOnly; SameSite=Strict"
                    )
                    resp = json.dumps({"message": "Login successful"}).encode()
                    self.send_header("Content-Length", str(len(resp)))
                    self.end_headers()
                    self.wfile.write(resp)
                else:
                    self.send_response(302)
                    self.send_header(
                        "Set-Cookie",
                        f"cam_session={token}; Path=/; HttpOnly; SameSite=Strict"
                    )
                    self.send_header("Location", "/")
                    self.end_headers()
            else:
                log.warning("Failed login from %s (user=%s)",
                            self.client_address[0], username)
                if "application/json" in content_type:
                    self._json_response(
                        {"error": "Invalid username or password"}, 401)
                else:
                    self._serve_login_page(
                        error="Invalid username or password")

        def _get_status(self):
            """Collect camera status info."""
            # Current WiFi SSID
            current_ssid = ""
            try:
                r = subprocess.run(
                    ["nmcli", "-t", "-f", "active,ssid", "device", "wifi"],
                    capture_output=True, text=True, timeout=10,
                )
                for line in r.stdout.strip().splitlines():
                    parts = line.split(":", 1)
                    if len(parts) == 2 and parts[0].lower() == "yes":
                        current_ssid = parts[1]
                        break
            except Exception:
                pass

            # IP address
            ip_addr = ""
            try:
                r = subprocess.run(
                    ["nmcli", "-t", "-f", "IP4.ADDRESS", "device", "show", "wlan0"],
                    capture_output=True, text=True, timeout=10,
                )
                for line in r.stdout.strip().splitlines():
                    if line.startswith("IP4.ADDRESS") and "/" in line:
                        ip_addr = line.split(":", 1)[1].split("/")[0]
                        break
            except Exception:
                pass

            # Hostname
            hostname = ""
            try:
                r = subprocess.run(
                    ["hostname"], capture_output=True, text=True, timeout=5,
                )
                hostname = r.stdout.strip()
            except Exception:
                pass

            # Server connection
            server_connected = False
            server_addr = config.server_ip or "unknown"
            if config.server_ip:
                import socket
                try:
                    socket.gethostbyname(config.server_ip)
                    server_connected = True
                except socket.gaierror:
                    pass

            # Stream status
            streaming = False
            if stream_manager:
                streaming = stream_manager.is_streaming

            # System health
            cpu_temp = _get_cpu_temp()
            uptime = _get_uptime()
            mem_total, mem_used = _get_memory_mb()

            return {
                "camera_id": config.camera_id,
                "hostname": hostname,
                "ip_address": ip_addr,
                "wifi_ssid": current_ssid,
                "server_address": server_addr,
                "server_connected": server_connected,
                "streaming": streaming,
                "cpu_temp": cpu_temp,
                "uptime": uptime,
                "memory_total_mb": mem_total,
                "memory_used_mb": mem_used,
            }

        def _scan_networks(self):
            """Quick WiFi scan."""
            try:
                subprocess.run(
                    ["nmcli", "device", "wifi", "rescan"],
                    capture_output=True, timeout=10,
                )
                time.sleep(2)
                r = subprocess.run(
                    ["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY",
                     "device", "wifi", "list"],
                    capture_output=True, text=True, timeout=15,
                )
                networks = []
                seen = set()
                for line in r.stdout.strip().splitlines():
                    parts = line.split(":", 2)
                    if len(parts) >= 3 and parts[0] and parts[0] not in seen:
                        seen.add(parts[0])
                        networks.append({
                            "ssid": parts[0],
                            "signal": int(parts[1]) if parts[1].isdigit() else 0,
                            "security": parts[2],
                        })
                networks.sort(key=lambda n: n["signal"], reverse=True)
                return networks
            except Exception:
                return []

        def _json_response(self, data, code=200):
            body = json.dumps(data).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _serve_login_page(self, error=""):
            html = _load_template("login.html").replace(
                "{{CAMERA_ID}}", config.camera_id
            ).replace(
                "{{ERROR}}", _html_escape(error)
            ).replace(
                "{{ERROR_DISPLAY}}", "block" if error else "none"
            )
            body = html.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _serve_status_page(self):
            html = _load_template("status.html").replace(
                "{{CAMERA_ID}}", config.camera_id)
            body = html.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return StatusHandler


def _html_escape(s):
    """Escape HTML special characters."""
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


# Setup page handler (first boot — no auth required)
# ============================================================
def _make_handler(config, setup_server):
    """Create an HTTP request handler with access to config/setup."""

    class SetupHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            log.info("HTTP: " + format % args)

        def do_GET(self):
            if self.path == "/" or self.path == "/setup":
                self._serve_setup_page()
            elif self.path == "/api/networks":
                networks = setup_server.get_cached_networks()
                self._json_response({"networks": networks})
            elif self.path == "/api/status":
                result = setup_server.get_status()
                if result is None:
                    status = "idle"
                    error = ""
                elif result is True:
                    status = "connected"
                    error = ""
                else:
                    status = "failed"
                    error = str(result)
                # Include hostname so setup success page can show .local URL
                hostname = ""
                try:
                    r = subprocess.run(
                        ["hostname"], capture_output=True, text=True, timeout=5,
                    )
                    hostname = r.stdout.strip()
                except Exception:
                    pass
                self._json_response({
                    "status": status,
                    "error": error,
                    "setup_complete": is_setup_complete(config.data_dir),
                    "camera_id": config.camera_id,
                    "hostname": hostname,
                })
            else:
                # Captive portal: redirect ANY unknown path to setup page.
                log.debug("Captive portal redirect: %s -> /setup", self.path)
                self.send_response(302)
                self.send_header("Location", "http://10.42.0.1/setup")
                self.end_headers()

        def do_POST(self):
            content_len = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_len) if content_len > 0 else b""

            if self.path == "/api/connect":
                try:
                    data = json.loads(body)
                    ssid = data.get("ssid", "").strip()
                    password = data.get("password", "")
                    server_ip = data.get("server_ip", "").strip()
                    server_port = data.get("server_port", "8554").strip()
                    admin_username = data.get("admin_username", "admin").strip()
                    admin_password = data.get("admin_password", "")

                    if not ssid:
                        self._json_response({"error": "WiFi name required"}, 400)
                        return
                    if not password:
                        self._json_response({"error": "WiFi password required"}, 400)
                        return
                    if not server_ip:
                        self._json_response({"error": "Server IP required"}, 400)
                        return
                    if not admin_username or len(admin_username) < 3:
                        self._json_response(
                            {"error": "Username required (min 3 characters)"},
                            400)
                        return
                    if not admin_password or len(admin_password) < 4:
                        self._json_response(
                            {"error": "Password required (min 4 characters)"},
                            400)
                        return

                    # Start connection in background
                    setup_server.save_and_connect(
                        ssid, password, server_ip, server_port,
                        admin_username=admin_username,
                        admin_password=admin_password,
                    )

                    self._json_response({
                        "status": "connecting",
                        "message": "Settings saved. Connecting to WiFi...",
                    })
                except json.JSONDecodeError:
                    self._json_response({"error": "Invalid request"}, 400)

            elif self.path == "/api/rescan":
                # Disruptive — will briefly drop AP
                networks = setup_server.rescan()
                self._json_response({"networks": networks})
            else:
                self.send_error(404)

        def _json_response(self, data, code=200):
            body = json.dumps(data).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _serve_setup_page(self):
            html = _load_template("setup.html").replace(
                "{{CAMERA_ID}}", config.camera_id
            )
            body = html.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return SetupHandler


