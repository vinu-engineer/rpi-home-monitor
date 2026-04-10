"""
WiFi setup for camera first boot.

On first boot (when /data/.setup-done doesn't exist):
1. Starts a WiFi hotspot "HomeCam-Setup"
2. Runs a tiny HTTP server on port 80 with a setup page
3. User connects, enters WiFi SSID + password + server IP on ONE page
4. Camera saves settings, tears down hotspot, connects to home WiFi
5. If WiFi fails, hotspot comes back up for retry

Single-radio constraint: wlan0 can either be an AP or a client, not both.
So we can't scan while hotspot is active. User types SSID manually or we
do a quick scan BEFORE starting the hotspot.
"""
import http.server
import json
import os
import subprocess
import threading
import time
import logging

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


def is_setup_complete(data_dir):
    """Check if camera has been configured."""
    return os.path.isfile(os.path.join(data_dir, SETUP_STAMP))


def mark_setup_complete(data_dir):
    """Write setup-done stamp file."""
    os.makedirs(data_dir, exist_ok=True)
    with open(os.path.join(data_dir, SETUP_STAMP), "w") as f:
        f.write("1\n")


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

    def save_and_connect(self, ssid, password, server_ip, server_port="8554"):
        """Save settings and attempt WiFi connection in background.

        Returns immediately so the HTTP response reaches the phone
        before the hotspot is torn down.
        """
        self._connect_result = None
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
            # Even after NM sees wlan0, AP mode can fail briefly while
            # the driver finishes initialization.
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


class CameraStatusServer:
    """Lightweight HTTP server showing camera status after setup.

    Runs on port 80 and shows camera ID, WiFi, server connection,
    stream status, and a form to change WiFi.
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


def _make_status_handler(config, stream_manager, status_server):
    """Create HTTP handler for the camera status page."""

    class StatusHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            log.debug("Status HTTP: " + format % args)

        def do_GET(self):
            if self.path == "/" or self.path == "/status":
                self._serve_status_page()
            elif self.path == "/api/status":
                self._json_response(self._get_status())
            elif self.path == "/api/networks":
                nets = self._scan_networks()
                self._json_response({"networks": nets})
            else:
                self.send_response(302)
                self.send_header("Location", "/")
                self.end_headers()

        def do_POST(self):
            content_len = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_len) if content_len > 0 else b""

            if self.path == "/api/wifi":
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
                        self._json_response({"error": err or "Connection failed"}, 500)
                except json.JSONDecodeError:
                    self._json_response({"error": "Invalid JSON"}, 400)
            else:
                self.send_error(404)

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

            # Server connection — try resolving server address
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

            return {
                "camera_id": config.camera_id,
                "hostname": hostname,
                "ip_address": ip_addr,
                "wifi_ssid": current_ssid,
                "server_address": server_addr,
                "server_connected": server_connected,
                "streaming": streaming,
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
                    ["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list"],
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

        def _serve_status_page(self):
            html = _STATUS_HTML.replace("{{CAMERA_ID}}", config.camera_id)
            body = html.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return StatusHandler


_STATUS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Camera Status</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, sans-serif;
       background: #1a1a2e; color: #eee; min-height: 100vh;
       display: flex; flex-direction: column; align-items: center; }
.container { max-width: 400px; width: 100%; padding: 20px; }
h1 { text-align: center; color: #e94560; margin: 30px 0 6px; font-size: 1.5em; }
.cam-id { text-align: center; color: #666; font-size: 0.8em; margin-bottom: 24px; }
.card { background: #16213e; border-radius: 12px; padding: 20px; margin: 12px 0; }
.card h3 { margin-bottom: 12px; font-size: 1.1em; }
.info-row { display: flex; justify-content: space-between; padding: 10px 0;
  border-bottom: 1px solid #0f3460; }
.info-row:last-child { border-bottom: none; }
.info-label { color: #8090b0; font-size: 0.9em; }
.info-value { font-weight: 500; }
.status-ok { color: #4ade80; }
.status-err { color: #f87171; }
label { display: block; margin: 12px 0 4px; color: #8090b0; font-size: 0.85em; }
input[type=text], input[type=password] {
  width: 100%; padding: 12px; border: 1px solid #2a3a5e; border-radius: 8px;
  background: #0f3460; color: #eee; font-size: 1em; outline: none; }
input:focus { border-color: #e94560; }
.btn { display: block; width: 100%; padding: 14px; border: none; border-radius: 10px;
  font-size: 1em; font-weight: 600; cursor: pointer; margin-top: 14px; }
.btn-primary { background: #e94560; color: #fff; }
.btn-primary:hover { background: #d63851; }
.btn-primary:disabled { opacity: 0.5; cursor: not-allowed; }
.btn-secondary { background: #0f3460; color: #b0b0c0; margin-top: 8px; }
.btn-secondary:hover { background: #133a6a; }
.network-list { max-height: 200px; overflow-y: auto; margin: 8px 0; }
.net { padding: 10px 12px; border-bottom: 1px solid #0f3460; cursor: pointer;
  display: flex; justify-content: space-between; align-items: center; }
.net:hover { background: #0f3460; border-radius: 6px; }
.msg { text-align: center; padding: 12px; border-radius: 8px; margin: 8px 0; font-size: 0.9em; }
.msg-ok { background: #065f46; color: #d1fae5; }
.msg-err { background: #7f1d1d; color: #fecaca; }
.msg-warn { background: #78350f; color: #fde68a; }
#wifi-form { display: none; }
</style>
</head>
<body>
<div class="container">
  <h1>Camera Status</h1>
  <p class="cam-id">{{CAMERA_ID}}</p>

  <div class="card">
    <h3>Device Info</h3>
    <div class="info-row">
      <span class="info-label">Hostname</span>
      <span class="info-value" id="s-hostname">--</span>
    </div>
    <div class="info-row">
      <span class="info-label">IP Address</span>
      <span class="info-value" id="s-ip">--</span>
    </div>
    <div class="info-row">
      <span class="info-label">WiFi Network</span>
      <span class="info-value" id="s-wifi">--</span>
    </div>
  </div>

  <div class="card">
    <h3>Connection Status</h3>
    <div class="info-row">
      <span class="info-label">Server</span>
      <span class="info-value" id="s-server">--</span>
    </div>
    <div class="info-row">
      <span class="info-label">Stream</span>
      <span class="info-value" id="s-stream">--</span>
    </div>
  </div>

  <button class="btn btn-secondary" id="btn-wifi" onclick="toggleWifi()">Change WiFi</button>

  <div id="wifi-form">
    <div class="card">
      <h3>Change WiFi Network</h3>
      <div class="msg msg-warn">Warning: Changing WiFi will briefly disconnect the camera.</div>
      <div id="net-list" class="network-list"></div>
      <button class="btn btn-secondary" onclick="scanNetworks()" id="btn-scan" style="margin-bottom:8px;">Scan for Networks</button>
      <label>WiFi Name (SSID)</label>
      <input type="text" id="in-ssid" placeholder="Network name">
      <label>WiFi Password</label>
      <input type="password" id="in-pass" placeholder="Password">
      <button class="btn btn-primary" id="btn-connect" onclick="doConnect()">Connect</button>
      <div id="wifi-result"></div>
    </div>
  </div>
</div>

<script>
function $(id) { return document.getElementById(id); }

function loadStatus() {
  fetch('/api/status')
    .then(function(r) { return r.json(); })
    .then(function(d) {
      $('s-hostname').textContent = d.hostname || '--';
      $('s-ip').textContent = d.ip_address || '--';
      $('s-wifi').textContent = d.wifi_ssid || 'Not connected';

      var serverEl = $('s-server');
      if (d.server_connected) {
        serverEl.textContent = d.server_address + ' (connected)';
        serverEl.className = 'info-value status-ok';
      } else {
        serverEl.textContent = d.server_address + ' (disconnected)';
        serverEl.className = 'info-value status-err';
      }

      var streamEl = $('s-stream');
      if (d.streaming) {
        streamEl.textContent = 'Streaming';
        streamEl.className = 'info-value status-ok';
      } else {
        streamEl.textContent = 'Not streaming';
        streamEl.className = 'info-value status-err';
      }
    })
    .catch(function() {});
}

function toggleWifi() {
  var form = $('wifi-form');
  form.style.display = form.style.display === 'none' ? 'block' : 'none';
}

function scanNetworks() {
  var btn = $('btn-scan');
  btn.disabled = true;
  btn.textContent = 'Scanning...';
  $('net-list').innerHTML = '';

  fetch('/api/networks')
    .then(function(r) { return r.json(); })
    .then(function(d) {
      var nets = d.networks || [];
      var html = '';
      nets.forEach(function(n) {
        html += '<div class="net" onclick="pickNet(\\''+esc(n.ssid)+'\\')"><span>'+esc(n.ssid)+'</span><span style="color:#4ade80;font-size:0.85em">'+n.signal+'%</span></div>';
      });
      $('net-list').innerHTML = html || '<div class="msg" style="color:#8090b0">No networks found</div>';
      btn.disabled = false;
      btn.textContent = 'Scan for Networks';
    })
    .catch(function() {
      btn.disabled = false;
      btn.textContent = 'Scan for Networks';
      $('net-list').innerHTML = '<div class="msg msg-err">Scan failed</div>';
    });
}

function pickNet(ssid) {
  $('in-ssid').value = ssid;
  $('in-pass').focus();
}

function doConnect() {
  var ssid = $('in-ssid').value.trim();
  var pass = $('in-pass').value;
  if (!ssid) { alert('Enter WiFi name'); return; }
  if (!pass) { alert('Enter WiFi password'); return; }

  $('btn-connect').disabled = true;
  $('btn-connect').textContent = 'Connecting...';
  $('wifi-result').innerHTML = '';

  fetch('/api/wifi', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ssid: ssid, password: pass})
  })
  .then(function(r) { return r.json(); })
  .then(function(d) {
    $('btn-connect').disabled = false;
    $('btn-connect').textContent = 'Connect';
    if (d.error) {
      $('wifi-result').innerHTML = '<div class="msg msg-err">' + esc(d.error) + '</div>';
    } else {
      $('wifi-result').innerHTML = '<div class="msg msg-ok">' + esc(d.message || 'Connected!') + '</div>';
      setTimeout(loadStatus, 2000);
    }
  })
  .catch(function() {
    $('btn-connect').disabled = false;
    $('btn-connect').textContent = 'Connect';
    $('wifi-result').innerHTML = '<div class="msg msg-err">Request failed — camera may have changed networks. Try reconnecting.</div>';
  });
}

function esc(s) {
  var d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML.replace(/'/g, "\\\\'");
}

// Load on start, then refresh every 10s
loadStatus();
setInterval(loadStatus, 10000);
</script>
</body>
</html>
"""


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
                self._json_response({
                    "status": status,
                    "error": error,
                    "setup_complete": is_setup_complete(config.data_dir),
                    "camera_id": config.camera_id,
                })
            else:
                # Captive portal: redirect ANY unknown path to setup page.
                # When phone connects to hotspot and checks connectivity
                # (Apple, Android, Windows all probe different URLs),
                # this redirect triggers the "Sign in to network" popup.
                log.debug("Captive portal redirect: %s → /setup", self.path)
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

                    if not ssid:
                        self._json_response({"error": "WiFi name required"}, 400)
                        return
                    if not password:
                        self._json_response({"error": "WiFi password required"}, 400)
                        return
                    if not server_ip:
                        self._json_response({"error": "Server IP required"}, 400)
                        return

                    # Start connection in background
                    setup_server.save_and_connect(
                        ssid, password, server_ip, server_port
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
            html = _SETUP_HTML.replace(
                "{{CAMERA_ID}}", config.camera_id
            )
            body = html.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return SetupHandler


_SETUP_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Camera Setup</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, sans-serif;
       background: #1a1a2e; color: #eee; min-height: 100vh;
       display: flex; flex-direction: column; align-items: center; }
.container { max-width: 400px; width: 100%; padding: 20px; }
h1 { text-align: center; color: #e94560; margin: 30px 0 6px; font-size: 1.5em; }
.cam-id { text-align: center; color: #666; font-size: 0.8em; margin-bottom: 24px; }
.card { background: #16213e; border-radius: 12px; padding: 20px; margin: 12px 0; }
.card h3 { margin-bottom: 12px; font-size: 1.1em; }
label { display: block; margin: 12px 0 4px; color: #8090b0; font-size: 0.85em; }
input[type=text], input[type=password] {
  width: 100%; padding: 12px; border: 1px solid #2a3a5e; border-radius: 8px;
  background: #0f3460; color: #eee; font-size: 1em; outline: none; }
input:focus { border-color: #e94560; }
.btn { display: block; width: 100%; padding: 14px; border: none; border-radius: 10px;
  font-size: 1em; font-weight: 600; cursor: pointer; margin-top: 14px; }
.btn-primary { background: #e94560; color: #fff; }
.btn-primary:hover { background: #d63851; }
.btn-primary:disabled { opacity: 0.5; cursor: not-allowed; }
.btn-secondary { background: #0f3460; color: #b0b0c0; margin-top: 8px; }
.btn-secondary:hover { background: #133a6a; }
.hint { color: #506080; font-size: 0.8em; margin-top: 4px; }
.network-list { max-height: 200px; overflow-y: auto; margin: 8px 0; }
.net { padding: 10px 12px; border-bottom: 1px solid #0f3460; cursor: pointer;
  display: flex; justify-content: space-between; align-items: center; }
.net:hover { background: #0f3460; border-radius: 6px; }
.net-ssid { font-weight: 500; }
.net-signal { color: #4ade80; font-size: 0.85em; }
.msg { text-align: center; padding: 16px; border-radius: 8px; margin: 12px 0; }
.msg-info { background: #0f3460; color: #8090b0; }
.msg-ok { background: #065f46; color: #d1fae5; }
.msg-err { background: #7f1d1d; color: #fecaca; }
.msg-warn { background: #78350f; color: #fde68a; }
.spinner { display: inline-block; width: 18px; height: 18px;
  border: 2px solid rgba(255,255,255,0.2); border-top-color: #fff;
  border-radius: 50%; animation: spin 0.7s linear infinite;
  vertical-align: middle; margin-right: 8px; }
@keyframes spin { to { transform: rotate(360deg); } }
#view-form, #view-connecting, #view-result { display: none; }
#view-form.active, #view-connecting.active, #view-result.active { display: block; }
</style>
</head>
<body>
<div class="container">
  <h1>Camera Setup</h1>
  <p class="cam-id">{{CAMERA_ID}}</p>

  <!-- FORM VIEW -->
  <div id="view-form" class="active">
    <div class="card">
      <h3>WiFi Network</h3>
      <div id="net-section">
        <div id="net-list-wrap" style="display:none">
          <p style="color:#8090b0;font-size:0.85em;margin-bottom:6px">
            Networks found before hotspot started. Tap to select:</p>
          <div class="network-list" id="net-list"></div>
        </div>
        <label>WiFi Name (SSID)</label>
        <input type="text" id="in-ssid" placeholder="Your WiFi network name">
        <label>WiFi Password</label>
        <input type="password" id="in-pass" placeholder="WiFi password">
      </div>
    </div>

    <div class="card">
      <h3>Server Connection</h3>
      <label>Server Address</label>
      <input type="text" id="in-server" value="rpi-divinu.local" placeholder="rpi-divinu.local">
      <div class="hint">Default works out of the box. Change only if you renamed your server.</div>
      <label>RTSP Port</label>
      <input type="text" id="in-port" value="8554">
    </div>

    <button class="btn btn-primary" id="btn-save" onclick="doConnect()">
      Save &amp; Connect
    </button>
    <button class="btn btn-secondary" onclick="loadNetworks()">
      Scan for Networks
    </button>
    <p class="hint" style="text-align:center;margin-top:8px">
      Scanning will briefly drop the hotspot connection.</p>
  </div>

  <!-- CONNECTING VIEW -->
  <div id="view-connecting">
    <div class="card">
      <div class="msg msg-info">
        <div class="spinner"></div> Connecting to WiFi...
      </div>
      <p style="color:#8090b0;font-size:0.85em;text-align:center;margin-top:12px">
        The hotspot will disappear now. This is normal.<br><br>
        <strong>If it worked:</strong> the camera LED will go solid.
        You can close this page.<br><br>
        <strong>If it failed:</strong> the <em>HomeCam-Setup</em> hotspot
        will reappear in about 30 seconds. Reconnect and try again.
      </p>
    </div>
  </div>

  <!-- RESULT VIEW (shown if user reconnects after failure) -->
  <div id="view-result">
    <div id="result-msg" class="msg msg-err"></div>
    <button class="btn btn-primary" onclick="showForm()">Try Again</button>
  </div>
</div>

<script>
function $(id) { return document.getElementById(id); }

function showView(name) {
  ['view-form','view-connecting','view-result'].forEach(function(v) {
    $(v).className = v === name ? 'active' : '';
  });
}

function showForm() { showView('view-form'); }

function loadNetworks() {
  var wrap = $('net-list-wrap');
  var list = $('net-list');
  list.innerHTML = '<div class="msg msg-warn"><div class="spinner"></div> Scanning... hotspot will drop briefly, please wait.</div>';
  wrap.style.display = 'block';

  fetch('/api/rescan', {method: 'POST'})
    .then(function(r) { return r.json(); })
    .then(function(d) { renderNetworks(d.networks || []); })
    .catch(function() {
      list.innerHTML = '<div class="msg msg-err">Scan failed — you may need to reconnect to HomeCam-Setup and try again.</div>';
    });
}

function renderNetworks(nets) {
  var list = $('net-list');
  if (nets.length === 0) {
    list.innerHTML = '<div class="msg msg-info">No networks found. Type SSID manually.</div>';
    return;
  }
  var html = '';
  nets.forEach(function(n) {
    html += '<div class="net" onclick="pickNet(\\''+esc(n.ssid)+'\\')"><span class="net-ssid">'
      +esc(n.ssid)+'</span><span class="net-signal">'+n.signal+'%</span></div>';
  });
  list.innerHTML = html;
}

function pickNet(ssid) {
  $('in-ssid').value = ssid;
  $('in-pass').focus();
}

function doConnect() {
  var ssid = $('in-ssid').value.trim();
  var pass = $('in-pass').value;
  var server = $('in-server').value.trim();
  var port = $('in-port').value.trim() || '8554';

  if (!ssid) { alert('Enter WiFi network name'); return; }
  if (!pass) { alert('Enter WiFi password'); return; }
  if (!server) { alert('Enter server IP address'); return; }

  $('btn-save').disabled = true;

  fetch('/api/connect', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ssid:ssid, password:pass, server_ip:server, server_port:port})
  })
  .then(function(r) { return r.json(); })
  .then(function(d) {
    if (d.error) {
      alert(d.error);
      $('btn-save').disabled = false;
    } else {
      showView('view-connecting');
    }
  })
  .catch(function() {
    showView('view-connecting');
  });
}

function esc(s) {
  var d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML.replace(/'/g, "\\\\'");
}

// On page load: check if we're returning after a failed attempt
fetch('/api/status')
  .then(function(r) { return r.json(); })
  .then(function(d) {
    if (d.setup_complete) {
      showView('view-result');
      $('result-msg').className = 'msg msg-ok';
      $('result-msg').textContent = 'Setup complete! Camera is connected.';
    } else if (d.status === 'failed') {
      showView('view-result');
      $('result-msg').textContent = 'WiFi connection failed: ' + d.error + '. Try again.';
    } else {
      // Load cached networks from pre-hotspot scan
      fetch('/api/networks')
        .then(function(r) { return r.json(); })
        .then(function(nd) {
          var nets = nd.networks || [];
          if (nets.length > 0) {
            $('net-list-wrap').style.display = 'block';
            renderNetworks(nets);
          }
        });
    }
  })
  .catch(function() {});
</script>
</body>
</html>
"""
