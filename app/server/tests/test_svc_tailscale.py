"""Tests for TailscaleService — Tailscale VPN management."""

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from monitor.services.tailscale_service import TailscaleService


@pytest.fixture
def svc():
    """Create TailscaleService with mock audit."""
    return TailscaleService(audit=MagicMock())


# Sample tailscale status --json output (connected state)
SAMPLE_STATUS_CONNECTED = {
    "BackendState": "Running",
    "Self": {
        "HostName": "rpi-divinu",
        "TailscaleIPs": ["100.64.0.1", "fd7a:115c:a1e0::1"],
        "ExitNode": False,
    },
    "Peer": {
        "node1": {
            "HostName": "my-laptop",
            "TailscaleIPs": ["100.64.0.2"],
            "Online": True,
        },
        "node2": {
            "HostName": "my-phone",
            "TailscaleIPs": ["100.64.0.3"],
            "Online": False,
        },
    },
}

SAMPLE_STATUS_NEEDS_LOGIN = {
    "BackendState": "NeedsLogin",
    "Self": {"HostName": "rpi-divinu", "TailscaleIPs": [], "ExitNode": False},
    "Peer": {},
}

SAMPLE_STATUS_STOPPED = {
    "BackendState": "Stopped",
    "Self": {"HostName": "rpi-divinu", "TailscaleIPs": [], "ExitNode": False},
    "Peer": {},
}


class TestGetStatus:
    """Test Tailscale status retrieval."""

    @patch("monitor.services.tailscale_service.subprocess.run")
    def test_connected_status(self, mock_run, svc):
        """Should return full status when connected."""
        # First call: version check (binary_exists)
        # Second call: status --json
        mock_run.side_effect = [
            MagicMock(returncode=0),  # version check
            MagicMock(
                returncode=0,
                stdout=json.dumps(SAMPLE_STATUS_CONNECTED),
                stderr="",
            ),
        ]
        status = svc.get_status()
        assert status["installed"] is True
        assert status["running"] is True
        assert status["state"] == "connected"
        assert status["hostname"] == "rpi-divinu"
        assert status["tailscale_ip"] == "100.64.0.1"
        assert len(status["peers"]) == 2
        assert status["peers"][0]["name"] == "my-laptop"
        assert status["peers"][0]["online"] is True

    @patch("monitor.services.tailscale_service.subprocess.run")
    def test_needs_login_status(self, mock_run, svc):
        """Should report needs-login state."""
        mock_run.side_effect = [
            MagicMock(returncode=0),
            MagicMock(
                returncode=0,
                stdout=json.dumps(SAMPLE_STATUS_NEEDS_LOGIN),
                stderr="",
            ),
        ]
        status = svc.get_status()
        assert status["state"] == "needs-login"
        assert status["tailscale_ip"] == ""

    @patch("monitor.services.tailscale_service.subprocess.run")
    def test_stopped_status(self, mock_run, svc):
        """Should report stopped state."""
        mock_run.side_effect = [
            MagicMock(returncode=0),
            MagicMock(
                returncode=0,
                stdout=json.dumps(SAMPLE_STATUS_STOPPED),
                stderr="",
            ),
        ]
        status = svc.get_status()
        assert status["state"] == "stopped"

    @patch("monitor.services.tailscale_service.subprocess.run")
    def test_not_installed(self, mock_run, svc):
        """Should return unavailable when binary not found."""
        mock_run.side_effect = FileNotFoundError
        status = svc.get_status()
        assert status["installed"] is False
        assert status["state"] == "unavailable"

    @patch("monitor.services.tailscale_service.subprocess.run")
    def test_daemon_not_running(self, mock_run, svc):
        """Should return stopped when daemon isn't running."""
        mock_run.side_effect = [
            MagicMock(returncode=0),  # binary exists (version check)
            MagicMock(
                returncode=1,
                stdout="",
                stderr="failed to connect to local tailscaled; it doesn't appear to be running",
            ),  # status --json fails
        ]
        status = svc.get_status()
        assert status["installed"] is True
        assert status["running"] is False
        assert status["state"] == "stopped"

    @patch("monitor.services.tailscale_service.subprocess.run")
    def test_no_peers(self, mock_run, svc):
        """Should return empty peers list when none connected."""
        status_data = {**SAMPLE_STATUS_CONNECTED, "Peer": {}}
        mock_run.side_effect = [
            MagicMock(returncode=0),
            MagicMock(returncode=0, stdout=json.dumps(status_data), stderr=""),
        ]
        status = svc.get_status()
        assert status["peers"] == []

    @patch("monitor.services.tailscale_service.subprocess.run")
    def test_timeout_on_version_check(self, mock_run, svc):
        """Should handle timeout gracefully."""
        mock_run.side_effect = subprocess.TimeoutExpired("tailscale", 5)
        status = svc.get_status()
        assert status["installed"] is False
        assert status["state"] == "unavailable"


class TestConnect:
    """Test Tailscale connect."""

    @patch("monitor.services.tailscale_service.subprocess.run")
    def test_connect_already_authenticated(self, mock_run, svc):
        """Should return no auth URL when already connected."""
        mock_run.side_effect = [
            MagicMock(returncode=0),  # binary exists
            MagicMock(returncode=0, stdout="", stderr=""),  # tailscale up
        ]
        auth_url, err = svc.connect()
        assert auth_url is None
        assert err == ""

    @patch("monitor.services.tailscale_service.subprocess.run")
    def test_connect_returns_auth_url(self, mock_run, svc):
        """Should extract auth URL from tailscale up output."""
        mock_run.side_effect = [
            MagicMock(returncode=0),  # binary exists
            MagicMock(
                returncode=0,
                stdout="To authenticate, visit:\n\n\thttps://login.tailscale.com/a/abc123def\n",
                stderr="",
            ),
        ]
        auth_url, err = svc.connect()
        assert auth_url == "https://login.tailscale.com/a/abc123def"
        assert err == ""

    @patch("monitor.services.tailscale_service.subprocess.run")
    def test_connect_auth_url_in_stderr(self, mock_run, svc):
        """Should also check stderr for auth URL."""
        mock_run.side_effect = [
            MagicMock(returncode=0),
            MagicMock(
                returncode=0,
                stdout="",
                stderr="To authenticate, visit:\n\n\thttps://login.tailscale.com/a/xyz789\n",
            ),
        ]
        auth_url, err = svc.connect()
        assert auth_url == "https://login.tailscale.com/a/xyz789"

    @patch("monitor.services.tailscale_service.subprocess.run")
    def test_connect_not_installed(self, mock_run, svc):
        """Should return error when binary not found."""
        mock_run.side_effect = FileNotFoundError
        auth_url, err = svc.connect()
        assert auth_url is None
        assert "not installed" in err

    @patch("monitor.services.tailscale_service.subprocess.run")
    def test_connect_timeout(self, mock_run, svc):
        """Should handle timeout."""
        mock_run.side_effect = [
            MagicMock(returncode=0),
            subprocess.TimeoutExpired("tailscale", 30),
        ]
        auth_url, err = svc.connect()
        assert auth_url is None
        assert "timed out" in err.lower()

    @patch("monitor.services.tailscale_service.subprocess.run")
    def test_connect_logs_audit_on_auth_needed(self, mock_run, svc):
        """Should log audit event when auth URL is generated."""
        mock_run.side_effect = [
            MagicMock(returncode=0),
            MagicMock(
                returncode=0,
                stdout="https://login.tailscale.com/a/test123\n",
                stderr="",
            ),
        ]
        svc.connect()
        calls = [str(c) for c in svc._audit.log_event.call_args_list]
        assert any("TAILSCALE_AUTH_NEEDED" in c for c in calls)

    @patch("monitor.services.tailscale_service.subprocess.run")
    def test_connect_logs_audit_on_success(self, mock_run, svc):
        """Should log audit when connected without auth."""
        mock_run.side_effect = [
            MagicMock(returncode=0),
            MagicMock(returncode=0, stdout="", stderr=""),
        ]
        svc.connect()
        calls = [str(c) for c in svc._audit.log_event.call_args_list]
        assert any("TAILSCALE_CONNECTED" in c for c in calls)


class TestDisconnect:
    """Test Tailscale disconnect."""

    @patch("monitor.services.tailscale_service.subprocess.run")
    def test_disconnect_success(self, mock_run, svc):
        """Should disconnect successfully."""
        mock_run.side_effect = [
            MagicMock(returncode=0),  # binary exists
            MagicMock(returncode=0, stdout="", stderr=""),  # tailscale down
        ]
        ok, err = svc.disconnect()
        assert ok is True
        assert err == ""

    @patch("monitor.services.tailscale_service.subprocess.run")
    def test_disconnect_failure(self, mock_run, svc):
        """Should return error on failure."""
        mock_run.side_effect = [
            MagicMock(returncode=0),
            MagicMock(returncode=1, stdout="", stderr="not connected"),
        ]
        ok, err = svc.disconnect()
        assert ok is False
        assert "not connected" in err

    @patch("monitor.services.tailscale_service.subprocess.run")
    def test_disconnect_not_installed(self, mock_run, svc):
        """Should return error when binary not found."""
        mock_run.side_effect = FileNotFoundError
        ok, err = svc.disconnect()
        assert ok is False
        assert "not installed" in err

    @patch("monitor.services.tailscale_service.subprocess.run")
    def test_disconnect_logs_audit(self, mock_run, svc):
        """Should log audit on disconnect."""
        mock_run.side_effect = [
            MagicMock(returncode=0),
            MagicMock(returncode=0, stdout="", stderr=""),
        ]
        svc.disconnect()
        calls = [str(c) for c in svc._audit.log_event.call_args_list]
        assert any("TAILSCALE_DISCONNECTED" in c for c in calls)


class TestExtractAuthUrl:
    """Test auth URL extraction from CLI output."""

    def test_standard_format(self):
        text = "To authenticate, visit:\n\n\thttps://login.tailscale.com/a/abc123\n"
        assert (
            TailscaleService._extract_auth_url(text)
            == "https://login.tailscale.com/a/abc123"
        )

    def test_no_url(self):
        assert TailscaleService._extract_auth_url("some other output") is None

    def test_empty(self):
        assert TailscaleService._extract_auth_url("") is None

    def test_url_with_long_code(self):
        text = "https://login.tailscale.com/a/16cc78fe01ed46abcdef"
        assert TailscaleService._extract_auth_url(text) == text


class TestAuditResilience:
    """Test that audit failures don't crash the service."""

    @patch("monitor.services.tailscale_service.subprocess.run")
    def test_audit_error_ignored(self, mock_run):
        audit = MagicMock()
        audit.log_event.side_effect = RuntimeError("audit broken")
        svc = TailscaleService(audit=audit)

        mock_run.side_effect = [
            MagicMock(returncode=0),
            MagicMock(returncode=0, stdout="", stderr=""),
        ]
        # Should not raise despite audit failure
        ok, err = svc.disconnect()
        assert ok is True

    def test_no_audit_logger(self):
        """Should work fine without audit logger."""
        svc = TailscaleService(audit=None)
        svc._log_audit("TEST", "test")  # Should not raise
