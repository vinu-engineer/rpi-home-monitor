#!/usr/bin/env python3
"""
Home Monitor Server
- Manages RTSP camera streams from RPi Zero 2W units
- Records video segments using ffmpeg
- Provides a mobile-friendly web UI for live view and playback
"""

import os
import json
import subprocess
import glob
import signal
from datetime import datetime
from pathlib import Path
from flask import Flask, render_template, jsonify, request, send_file, Response

app = Flask(__name__)

BASE_DIR = "/opt/monitor"
RECORDINGS_DIR = os.path.join(BASE_DIR, "recordings")
SNAPSHOTS_DIR = os.path.join(BASE_DIR, "snapshots")
CONFIG_FILE = os.path.join(BASE_DIR, "cameras.json")

# Active recording processes
active_recordings = {}


def load_cameras():
    """Load camera configuration from JSON file."""
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    # Default config — update IPs after deploying Zero 2W cameras
    default = {
        "cameras": [
            {
                "id": "cam1",
                "name": "Front Door",
                "rtsp_url": "rtsp://192.168.1.101:8554/stream",
                "enabled": True,
            },
            {
                "id": "cam2",
                "name": "Back Yard",
                "rtsp_url": "rtsp://192.168.1.102:8554/stream",
                "enabled": True,
            },
        ]
    }
    save_cameras(default)
    return default


def save_cameras(config):
    """Save camera configuration."""
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


def start_recording(camera):
    """Start ffmpeg recording for a camera."""
    cam_id = camera["id"]
    if cam_id in active_recordings:
        return  # Already recording

    cam_dir = os.path.join(RECORDINGS_DIR, cam_id)
    os.makedirs(cam_dir, exist_ok=True)

    # Record in 15-minute segments, H.264 copy (no re-encode)
    output_pattern = os.path.join(
        cam_dir, f"{cam_id}_%Y-%m-%d_%H-%M-%S.mp4"
    )

    cmd = [
        "ffmpeg",
        "-rtsp_transport", "tcp",
        "-i", camera["rtsp_url"],
        "-c", "copy",
        "-f", "segment",
        "-segment_time", "900",
        "-segment_format", "mp4",
        "-strftime", "1",
        "-reset_timestamps", "1",
        output_pattern,
    ]

    proc = subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    active_recordings[cam_id] = proc


def stop_recording(cam_id):
    """Stop recording for a camera."""
    if cam_id in active_recordings:
        active_recordings[cam_id].send_signal(signal.SIGINT)
        active_recordings[cam_id].wait(timeout=10)
        del active_recordings[cam_id]


@app.route("/")
def index():
    """Main dashboard — mobile friendly."""
    config = load_cameras()
    return render_template("index.html", cameras=config["cameras"])


@app.route("/api/cameras", methods=["GET"])
def get_cameras():
    """List all cameras and their status."""
    config = load_cameras()
    for cam in config["cameras"]:
        cam["recording"] = cam["id"] in active_recordings
    return jsonify(config["cameras"])


@app.route("/api/cameras", methods=["POST"])
def add_camera():
    """Add a new camera."""
    data = request.json
    config = load_cameras()
    new_cam = {
        "id": f"cam{len(config['cameras']) + 1}",
        "name": data.get("name", "New Camera"),
        "rtsp_url": data.get("rtsp_url", ""),
        "enabled": True,
    }
    config["cameras"].append(new_cam)
    save_cameras(config)
    return jsonify(new_cam), 201


@app.route("/api/record/<cam_id>/start", methods=["POST"])
def api_start_recording(cam_id):
    """Start recording a specific camera."""
    config = load_cameras()
    camera = next((c for c in config["cameras"] if c["id"] == cam_id), None)
    if not camera:
        return jsonify({"error": "Camera not found"}), 404
    start_recording(camera)
    return jsonify({"status": "recording", "camera": cam_id})


@app.route("/api/record/<cam_id>/stop", methods=["POST"])
def api_stop_recording(cam_id):
    """Stop recording a specific camera."""
    stop_recording(cam_id)
    return jsonify({"status": "stopped", "camera": cam_id})


@app.route("/api/recordings/<cam_id>")
def list_recordings(cam_id):
    """List recorded video files for a camera."""
    cam_dir = os.path.join(RECORDINGS_DIR, cam_id)
    if not os.path.exists(cam_dir):
        return jsonify([])
    files = sorted(glob.glob(os.path.join(cam_dir, "*.mp4")), reverse=True)
    result = []
    for f in files[:50]:  # Last 50 recordings
        stat = os.stat(f)
        result.append({
            "filename": os.path.basename(f),
            "size_mb": round(stat.st_size / (1024 * 1024), 1),
            "created": datetime.fromtimestamp(stat.st_ctime).isoformat(),
        })
    return jsonify(result)


@app.route("/api/recordings/<cam_id>/<filename>")
def get_recording(cam_id, filename):
    """Stream a recorded video file."""
    # Sanitize filename to prevent path traversal
    safe_name = os.path.basename(filename)
    filepath = os.path.join(RECORDINGS_DIR, cam_id, safe_name)
    if not os.path.exists(filepath):
        return jsonify({"error": "File not found"}), 404
    return send_file(filepath, mimetype="video/mp4")


@app.route("/api/snapshot/<cam_id>")
def take_snapshot(cam_id):
    """Take a snapshot from a camera's RTSP stream."""
    config = load_cameras()
    camera = next((c for c in config["cameras"] if c["id"] == cam_id), None)
    if not camera:
        return jsonify({"error": "Camera not found"}), 404

    os.makedirs(SNAPSHOTS_DIR, exist_ok=True)
    snap_file = os.path.join(
        SNAPSHOTS_DIR,
        f"{cam_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg",
    )

    cmd = [
        "ffmpeg", "-y",
        "-rtsp_transport", "tcp",
        "-i", camera["rtsp_url"],
        "-frames:v", "1",
        "-q:v", "2",
        snap_file,
    ]

    try:
        subprocess.run(cmd, timeout=10, capture_output=True, check=True)
        return send_file(snap_file, mimetype="image/jpeg")
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
        return jsonify({"error": "Failed to capture snapshot"}), 500


@app.route("/api/storage")
def storage_info():
    """Get storage usage information."""
    stat = os.statvfs(RECORDINGS_DIR)
    total_gb = (stat.f_blocks * stat.f_frsize) / (1024 ** 3)
    free_gb = (stat.f_bavail * stat.f_frsize) / (1024 ** 3)
    used_gb = total_gb - free_gb
    return jsonify({
        "total_gb": round(total_gb, 1),
        "used_gb": round(used_gb, 1),
        "free_gb": round(free_gb, 1),
        "percent_used": round((used_gb / total_gb) * 100, 1),
    })


def auto_start_recordings():
    """Start recording all enabled cameras on boot."""
    config = load_cameras()
    for camera in config["cameras"]:
        if camera.get("enabled"):
            start_recording(camera)


if __name__ == "__main__":
    os.makedirs(RECORDINGS_DIR, exist_ok=True)
    os.makedirs(SNAPSHOTS_DIR, exist_ok=True)
    auto_start_recordings()
    app.run(host="0.0.0.0", port=5000)
