SUMMARY = "Security packages for Home Monitor devices"
DESCRIPTION = "TLS, firewall, disk encryption, and OTA update support."
LICENSE = "MIT"

inherit packagegroup

RDEPENDS:${PN} = " \
    openssl \
    nftables \
    cryptsetup \
    hwrevision \
    sw-versions \
    "
