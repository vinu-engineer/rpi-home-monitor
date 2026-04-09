# =============================================================
# home-monitor-image-prod.bb — Production image for RPi 4B server
#
# Hardened: no root password, no debug-tweaks, key-only SSH.
# This is what gets flashed to production devices.
#
# Build: bitbake home-monitor-image-prod
# =============================================================

require home-monitor-image.inc

SUMMARY .= " (Production)"

# --- Production features: SSH but no debug ---
EXTRA_IMAGE_FEATURES += "ssh-server-openssh"

# No debug-tweaks: root account is locked, must use first-boot wizard
