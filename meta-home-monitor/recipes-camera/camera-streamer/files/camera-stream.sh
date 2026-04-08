#!/bin/bash
# =============================================================
# camera-stream.sh — Stream camera feed via RTSP using ffmpeg
# =============================================================
source /opt/camera/camera.conf

echo "Starting camera stream..."
echo "  Resolution: ${WIDTH}x${HEIGHT}"
echo "  FPS: ${FPS}"
echo "  Server: ${SERVER_IP}:${SERVER_PORT}"
echo "  Stream: ${STREAM_NAME}"

# Use v4l2 to capture from the ZeroCam and stream via RTSP
# The RPi 4B server receives this stream
exec ffmpeg \
    -f v4l2 \
    -input_format h264 \
    -video_size ${WIDTH}x${HEIGHT} \
    -framerate ${FPS} \
    -i /dev/video0 \
    -c:v copy \
    -f rtsp \
    -rtsp_transport tcp \
    "rtsp://${SERVER_IP}:${SERVER_PORT}/${STREAM_NAME}"
