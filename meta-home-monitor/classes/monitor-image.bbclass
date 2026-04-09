# =============================================================
# monitor-image.bbclass — Shared image configuration
#
# Common image settings for all Home Monitor images.
# Inherit this in image recipes for consistent behavior.
# =============================================================

# Ensure data directories exist in the rootfs
create_data_dirs() {
    install -d ${IMAGE_ROOTFS}/data
    install -d ${IMAGE_ROOTFS}/opt/monitor
    install -d ${IMAGE_ROOTFS}/opt/camera
}

ROOTFS_POSTPROCESS_COMMAND += "create_data_dirs;"
