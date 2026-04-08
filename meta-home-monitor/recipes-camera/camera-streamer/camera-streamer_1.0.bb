# =============================================================
# camera-streamer — RTSP streaming service for PiHut ZeroCam
# =============================================================
SUMMARY = "Camera RTSP streamer using ffmpeg"
DESCRIPTION = "Captures video from the PiHut ZeroCam via libcamera/v4l2 \
and streams it over RTSP to the home monitoring server."
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

SRC_URI = " \
    file://camera-stream.sh \
    file://camera-streamer.service \
    file://camera.conf \
    "

S = "${WORKDIR}"

RDEPENDS:${PN} = "ffmpeg v4l-utils bash"

inherit systemd

SYSTEMD_SERVICE:${PN} = "camera-streamer.service"
SYSTEMD_AUTO_ENABLE = "enable"

do_install() {
    install -d ${D}/opt/camera
    install -m 0755 ${WORKDIR}/camera-stream.sh ${D}/opt/camera/camera-stream.sh
    install -m 0644 ${WORKDIR}/camera.conf ${D}/opt/camera/camera.conf

    install -d ${D}${systemd_system_unitdir}
    install -m 0644 ${WORKDIR}/camera-streamer.service ${D}${systemd_system_unitdir}/camera-streamer.service
}

FILES:${PN} = " \
    /opt/camera \
    ${systemd_system_unitdir}/camera-streamer.service \
    "
