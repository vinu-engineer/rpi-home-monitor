SUMMARY = "Hardware revision file for SWUpdate"
DESCRIPTION = "Provides /etc/hwrevision for SWUpdate hardware compatibility checks"
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

inherit allarch

do_install() {
    install -d ${D}${sysconfdir}
    echo "${MACHINE} 1.0" > ${D}${sysconfdir}/hwrevision
}

# MACHINE changes per build, so package must be machine-specific
PACKAGE_ARCH = "${MACHINE_ARCH}"

FILES:${PN} = "${sysconfdir}/hwrevision"
