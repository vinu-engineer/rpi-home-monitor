SUMMARY = "Software versions file for SWUpdate"
DESCRIPTION = "Provides /etc/sw-versions for SWUpdate version tracking"
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

SRC_URI = "file://sw-versions"

inherit allarch

do_install() {
    install -d ${D}${sysconfdir}
    install -m 0644 ${WORKDIR}/sw-versions ${D}${sysconfdir}/sw-versions
}

FILES:${PN} = "${sysconfdir}/sw-versions"
