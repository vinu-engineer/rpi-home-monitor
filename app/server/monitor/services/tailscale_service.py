"""
Tailscale VPN management service.

Wraps the tailscale CLI to provide status, connect/disconnect, and
auth URL retrieval for the web dashboard settings page.

Design patterns:
- Constructor Injection (audit)
- Single Responsibility (Tailscale lifecycle only)
- Fail-Silent (CLI failures return error tuples, never crash)
"""

import json
import logging
import subprocess

log = logging.getLogger("monitor.tailscale")

# Timeout for tailscale CLI commands (seconds)
CLI_TIMEOUT = 15

# Longer timeout for 'tailscale up' which may need network negotiation
CONNECT_TIMEOUT = 30


class TailscaleService:
    """Manages Tailscale VPN status and connection.

    Args:
        audit: AuditLogger instance (optional).
    """

    def __init__(self, audit=None):
        self._audit = audit

    def get_status(self):
        """Get current Tailscale status.

        Returns:
            dict with keys:
                installed (bool): Whether tailscale binary exists.
                running (bool): Whether tailscaled daemon is running.
                state (str): "connected", "needs-login", "stopped", or "unavailable".
                hostname (str): Tailscale hostname (if connected).
                tailscale_ip (str): Tailscale IP address (if connected).
                exit_node (bool): Whether acting as exit node.
                peers (list[dict]): Connected peers [{name, ip, online}].
        """
        result = {
            "installed": False,
            "running": False,
            "state": "unavailable",
            "hostname": "",
            "tailscale_ip": "",
            "exit_node": False,
            "peers": [],
        }

        # Check if binary exists
        if not self._binary_exists():
            return result
        result["installed"] = True

        # Get JSON status from tailscale
        status_data, err = self._run_json(["tailscale", "status", "--json"])
        if err:
            err_lower = err.lower()
            if (
                "not running" in err_lower
                or "not connect" in err_lower
                or "doesn't appear to be running" in err_lower
            ):
                result["state"] = "stopped"
            return result

        result["running"] = True

        # Parse backend state
        backend_state = status_data.get("BackendState", "")
        if backend_state == "Running":
            result["state"] = "connected"
        elif backend_state == "NeedsLogin":
            result["state"] = "needs-login"
        elif backend_state == "Stopped":
            result["state"] = "stopped"
        else:
            result["state"] = backend_state.lower() if backend_state else "unknown"

        # Parse self node info
        self_node = status_data.get("Self", {})
        if self_node:
            result["hostname"] = self_node.get("HostName", "")
            ips = self_node.get("TailscaleIPs", [])
            if ips:
                # First IP is always the IPv4 Tailscale address
                result["tailscale_ip"] = ips[0]
            result["exit_node"] = self_node.get("ExitNode", False)

        # Parse peers
        peer_map = status_data.get("Peer", {})
        for _key, peer in peer_map.items():
            result["peers"].append(
                {
                    "name": peer.get("HostName", ""),
                    "ip": (peer.get("TailscaleIPs") or [""])[0],
                    "online": peer.get("Online", False),
                }
            )

        return result

    def connect(self):
        """Start Tailscale and return auth URL if login is needed.

        Returns:
            (auth_url_or_none, error) tuple.
            auth_url is a string URL if login is needed, None if already connected.
        """
        if not self._binary_exists():
            return None, "Tailscale is not installed"

        try:
            proc = subprocess.run(
                ["tailscale", "up"],
                capture_output=True,
                text=True,
                timeout=CONNECT_TIMEOUT,
            )
        except FileNotFoundError:
            return None, "Tailscale binary not found"
        except subprocess.TimeoutExpired:
            return None, "Connection timed out"
        except OSError as e:
            return None, str(e)

        combined = proc.stdout + proc.stderr

        # Look for auth URL in output
        auth_url = self._extract_auth_url(combined)
        if auth_url:
            self._log_audit("TAILSCALE_AUTH_NEEDED", "Auth URL generated")
            return auth_url, ""

        if proc.returncode == 0:
            self._log_audit("TAILSCALE_CONNECTED", "Tailscale connected")
            return None, ""

        return None, combined.strip() or "Unknown error"

    def disconnect(self):
        """Stop Tailscale (keeps authentication).

        Returns:
            (success, error) tuple.
        """
        if not self._binary_exists():
            return False, "Tailscale is not installed"

        try:
            result = subprocess.run(
                ["tailscale", "down"],
                capture_output=True,
                text=True,
                timeout=CLI_TIMEOUT,
            )
            if result.returncode == 0:
                self._log_audit("TAILSCALE_DISCONNECTED", "Tailscale disconnected")
                return True, ""
            return False, result.stderr.strip() or "Disconnect failed"
        except FileNotFoundError:
            return False, "Tailscale binary not found"
        except subprocess.TimeoutExpired:
            return False, "Disconnect timed out"
        except OSError as e:
            return False, str(e)

    def _binary_exists(self):
        """Check if tailscale binary is available."""
        try:
            subprocess.run(
                ["tailscale", "version"],
                capture_output=True,
                timeout=5,
            )
            return True
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return False

    def _run_json(self, cmd):
        """Run a tailscale command and parse JSON output.

        Returns:
            (parsed_dict, error_string) tuple.
        """
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=CLI_TIMEOUT,
            )
            if result.returncode != 0:
                return {}, result.stderr.strip() or "Command failed"
            try:
                return json.loads(result.stdout), ""
            except (json.JSONDecodeError, ValueError):
                return {}, "Invalid JSON response"
        except FileNotFoundError:
            return {}, "Tailscale binary not found"
        except subprocess.TimeoutExpired:
            return {}, "Command timed out"
        except OSError as e:
            return {}, str(e)

    @staticmethod
    def _extract_auth_url(text):
        """Extract Tailscale auth URL from CLI output."""
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("https://login.tailscale.com/"):
                return line
        return None

    def _log_audit(self, event, detail):
        """Log audit event (fail-silent)."""
        if self._audit:
            try:
                self._audit.log_event(event, detail=detail)
            except Exception:
                pass
