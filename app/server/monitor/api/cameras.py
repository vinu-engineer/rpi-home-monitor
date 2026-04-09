"""
Camera management API.

Endpoints:
  GET    /cameras              - list all cameras (confirmed + pending)
  POST   /cameras/<id>/confirm - confirm a discovered camera (admin)
  PUT    /cameras/<id>         - update name, location, recording mode (admin)
  DELETE /cameras/<id>         - remove camera and revoke cert (admin)
  GET    /cameras/<id>/status  - live status (online, fps, uptime)
"""
from datetime import datetime, timezone

from flask import Blueprint, current_app, jsonify, request, session

from monitor.auth import admin_required, login_required

cameras_bp = Blueprint("cameras", __name__)

VALID_RECORDING_MODES = {"continuous", "off"}
VALID_RESOLUTIONS = {"720p", "1080p"}


@cameras_bp.route("", methods=["GET"])
@login_required
def list_cameras():
    """List all cameras (confirmed + pending)."""
    cameras = current_app.store.get_cameras()
    return jsonify([
        {
            "id": c.id,
            "name": c.name,
            "location": c.location,
            "status": c.status,
            "ip": c.ip,
            "recording_mode": c.recording_mode,
            "resolution": c.resolution,
            "fps": c.fps,
            "paired_at": c.paired_at,
            "last_seen": c.last_seen,
            "firmware_version": c.firmware_version,
        }
        for c in cameras
    ]), 200


@cameras_bp.route("/<camera_id>/confirm", methods=["POST"])
@admin_required
def confirm_camera(camera_id):
    """Confirm a discovered (pending) camera. Admin only."""
    camera = current_app.store.get_camera(camera_id)
    if camera is None:
        return jsonify({"error": "Camera not found"}), 404

    if camera.status != "pending":
        return jsonify({"error": "Camera is already confirmed"}), 400

    data = request.get_json(silent=True) or {}
    camera.name = data.get("name", camera.name or camera_id)
    camera.location = data.get("location", camera.location)
    camera.status = "online"
    camera.paired_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    camera.rtsp_url = f"rtsps://{camera.ip}:8554/stream"

    current_app.store.save_camera(camera)

    audit = getattr(current_app, "audit", None)
    if audit:
        audit.log_event(
            "CAMERA_CONFIRMED",
            user=session.get("username", ""),
            ip=request.remote_addr or "",
            detail=f"confirmed camera {camera_id} as '{camera.name}'",
        )

    return jsonify({
        "id": camera.id,
        "name": camera.name,
        "status": camera.status,
        "paired_at": camera.paired_at,
    }), 200


@cameras_bp.route("/<camera_id>", methods=["PUT"])
@admin_required
def update_camera(camera_id):
    """Update camera settings. Admin only."""
    camera = current_app.store.get_camera(camera_id)
    if camera is None:
        return jsonify({"error": "Camera not found"}), 404

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    allowed = {"name", "location", "recording_mode", "resolution", "fps"}
    unknown = set(data.keys()) - allowed
    if unknown:
        return jsonify({"error": f"Unknown fields: {', '.join(sorted(unknown))}"}), 400

    if "recording_mode" in data and data["recording_mode"] not in VALID_RECORDING_MODES:
        return jsonify({"error": f"recording_mode must be one of: {', '.join(sorted(VALID_RECORDING_MODES))}"}), 400

    if "resolution" in data and data["resolution"] not in VALID_RESOLUTIONS:
        return jsonify({"error": f"resolution must be one of: {', '.join(sorted(VALID_RESOLUTIONS))}"}), 400

    if "fps" in data:
        fps = data["fps"]
        if not isinstance(fps, int) or fps < 1 or fps > 30:
            return jsonify({"error": "fps must be an integer between 1 and 30"}), 400

    if "name" in data:
        name = data["name"]
        if not isinstance(name, str) or len(name) < 1 or len(name) > 64:
            return jsonify({"error": "name must be 1-64 characters"}), 400

    for key, value in data.items():
        setattr(camera, key, value)

    current_app.store.save_camera(camera)

    audit = getattr(current_app, "audit", None)
    if audit:
        audit.log_event(
            "CAMERA_UPDATED",
            user=session.get("username", ""),
            ip=request.remote_addr or "",
            detail=f"updated camera {camera_id}: {', '.join(sorted(data.keys()))}",
        )

    return jsonify({"message": "Camera updated"}), 200


@cameras_bp.route("/<camera_id>", methods=["DELETE"])
@admin_required
def delete_camera(camera_id):
    """Remove a camera. Admin only."""
    deleted = current_app.store.delete_camera(camera_id)
    if not deleted:
        return jsonify({"error": "Camera not found"}), 404

    audit = getattr(current_app, "audit", None)
    if audit:
        audit.log_event(
            "CAMERA_DELETED",
            user=session.get("username", ""),
            ip=request.remote_addr or "",
            detail=f"removed camera {camera_id}",
        )

    return jsonify({"message": "Camera removed"}), 200


@cameras_bp.route("/<camera_id>/status", methods=["GET"])
@login_required
def camera_status(camera_id):
    """Get live status for a camera."""
    camera = current_app.store.get_camera(camera_id)
    if camera is None:
        return jsonify({"error": "Camera not found"}), 404

    return jsonify({
        "id": camera.id,
        "name": camera.name,
        "status": camera.status,
        "ip": camera.ip,
        "last_seen": camera.last_seen,
        "firmware_version": camera.firmware_version,
        "resolution": camera.resolution,
        "fps": camera.fps,
        "recording_mode": camera.recording_mode,
    }), 200
