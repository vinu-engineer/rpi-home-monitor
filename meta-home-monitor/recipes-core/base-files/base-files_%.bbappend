# =============================================================
# OS branding — customize /etc/os-release
# =============================================================

do_install:append() {
    cat > ${D}${sysconfdir}/os-release << EOF
NAME="Home Monitor OS"
VERSION="${DISTRO_VERSION}"
ID=home-monitor
VERSION_ID=${DISTRO_VERSION}
VERSION_CODENAME=${DISTRO_CODENAME}
PRETTY_NAME="Home Monitor OS ${DISTRO_VERSION} (${DISTRO_CODENAME})"
HOME_URL="https://github.com/vinu-engineer/rpi-home-monitor"
EOF
}
