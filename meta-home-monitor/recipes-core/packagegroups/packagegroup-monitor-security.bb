SUMMARY = "Security packages for Home Monitor devices"
DESCRIPTION = "TLS, firewall, disk encryption, and certificate management."
LICENSE = "MIT"

inherit packagegroup

RDEPENDS:${PN} = " \
    openssl \
    nftables \
    cryptsetup \
    "
