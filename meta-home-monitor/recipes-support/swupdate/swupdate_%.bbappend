# Home Monitor SWUpdate configuration (ADR-0008)
# Our defconfig overrides the upstream one via FILESEXTRAPATHS priority
FILESEXTRAPATHS:prepend := "${THISDIR}/files:"
