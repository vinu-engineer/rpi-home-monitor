SUMMARY = "Video/camera packages for Zero 2W camera node"
DESCRIPTION = "FFmpeg, libcamera, and v4l for video capture and RTSP streaming."
LICENSE = "MIT"

inherit packagegroup

RDEPENDS:${PN} = " \
    ffmpeg \
    v4l-utils \
    libcamera \
    libcamera-apps \
    "
