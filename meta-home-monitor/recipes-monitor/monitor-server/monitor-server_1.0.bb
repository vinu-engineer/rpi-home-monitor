# =============================================================
# monitor-server — Home monitoring web application
# =============================================================
SUMMARY = "Home monitoring server with web UI and video recording"
DESCRIPTION = "Flask-based web server that manages RTSP camera streams, \
records video using ffmpeg, and provides a mobile-friendly web interface."
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

SRC_URI = " \
    file://app.py \
    file://monitor.service \
    file://record.sh \
    file://nginx-monitor.conf \
    file://templates/index.html \
    "

S = "${WORKDIR}"

RDEPENDS:${PN} = " \
    python3 \
    python3-flask \
    python3-jinja2 \
    ffmpeg \
    nginx \
    "

inherit systemd

SYSTEMD_SERVICE:${PN} = "monitor.service"
SYSTEMD_AUTO_ENABLE = "enable"

do_install() {
    # Install the Flask app
    install -d ${D}/opt/monitor
    install -m 0755 ${WORKDIR}/app.py ${D}/opt/monitor/app.py
    install -m 0755 ${WORKDIR}/record.sh ${D}/opt/monitor/record.sh

    # Templates
    install -d ${D}/opt/monitor/templates
    install -m 0644 ${WORKDIR}/templates/index.html ${D}/opt/monitor/templates/index.html

    # Create directories for recordings and snapshots
    install -d ${D}/opt/monitor/recordings
    install -d ${D}/opt/monitor/snapshots

    # Systemd service
    install -d ${D}${systemd_system_unitdir}
    install -m 0644 ${WORKDIR}/monitor.service ${D}${systemd_system_unitdir}/monitor.service

    # Nginx config
    install -d ${D}${sysconfdir}/nginx/sites-enabled
    install -m 0644 ${WORKDIR}/nginx-monitor.conf ${D}${sysconfdir}/nginx/sites-enabled/monitor.conf
}

FILES:${PN} = " \
    /opt/monitor \
    ${systemd_system_unitdir}/monitor.service \
    ${sysconfdir}/nginx/sites-enabled/monitor.conf \
    "
