# =============================================================
# home-camera-image-prod.bb — Production image for RPi Zero 2W camera
#
# Build: bitbake home-camera-image-prod
# =============================================================

require home-camera-image.inc

SUMMARY .= " (Production)"

# --- Production features: SSH but no debug ---
EXTRA_IMAGE_FEATURES += "ssh-server-openssh"

# No debug-tweaks: root locked, managed by server
