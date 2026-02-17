"""
Aqara G5 Pro → SHARP Gaussian Splat Pipeline Server

Runs on a cloud VPS connected to your Tailscale network.
Receives event triggers, captures a frame from the camera's RTSP stream
via ffmpeg over Tailscale, runs Apple's SHARP to generate a .ply Gaussian
Splat, and serves the results to your devices on the tailnet.

Usage:
    python server.py

Environment variables (or set in config.yaml):
    CAMERA_RTSP_URL   - e.g. rtsp://user:pass@100.x.x.10:8554/1520p
    NTFY_TOPIC        - ntfy.sh topic for push notifications (optional)
    SHARP_DEVICE      - "cpu", "cuda", or "mps" (default: auto-detect)
"""

import os
import sys
import time
import uuid
import shutil
import logging
import subprocess
import threading
from datetime import datetime
from pathlib import Path

import yaml
from flask import Flask, request, jsonify, send_from_directory, send_file
import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CONFIG_PATH = Path(__file__).parent / "config.yaml"

DEFAULT_CONFIG = {
    "camera": {
        "rtsp_url": "rtsp://USER:PASS@100.x.x.10:8554/1520p",
        "snapshot_quality": 2,          # ffmpeg -q:v (1=best, 31=worst)
        "rtsp_transport": "tcp",        # tcp is more reliable than udp
    },
    "sharp": {
        "device": "auto",               # auto, cpu, cuda, mps
        "timeout_seconds": 120,
    },
    "server": {
        "host": "0.0.0.0",
        "port": 8080,
        "data_dir": "/data/sharp-pipeline",
        "max_splats": 100,              # keep last N splats to save disk
    },
    "notifications": {
        "enabled": False,
        "ntfy_topic": "",               # e.g. "my-sharp-alerts"
        "ntfy_server": "https://ntfy.sh",
    },
    "tailscale": {
        "use_tailscale_ip": True,       # include tailscale IP in notifications
    },
}


def load_config() -> dict:
    """Load config from YAML file, falling back to defaults."""
    config = DEFAULT_CONFIG.copy()
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            user_config = yaml.safe_load(f) or {}
        _deep_merge(config, user_config)

    # Environment variable overrides
    if os.environ.get("CAMERA_RTSP_URL"):
        config["camera"]["rtsp_url"] = os.environ["CAMERA_RTSP_URL"]
    if os.environ.get("NTFY_TOPIC"):
        config["notifications"]["ntfy_topic"] = os.environ["NTFY_TOPIC"]
        config["notifications"]["enabled"] = True
    if os.environ.get("SHARP_DEVICE"):
        config["sharp"]["device"] = os.environ["SHARP_DEVICE"]

    return config


def _deep_merge(base: dict, override: dict):
    """Recursively merge override into base dict."""
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("sharp-pipeline")

# ---------------------------------------------------------------------------
# Core pipeline
# ---------------------------------------------------------------------------

config = load_config()
DATA_DIR = Path(config["server"]["data_dir"])
CAPTURES_DIR = DATA_DIR / "captures"
SPLATS_DIR = DATA_DIR / "splats"

for d in [CAPTURES_DIR, SPLATS_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# Track processing state
processing_lock = threading.Lock()
is_processing = False


def capture_frame(event_id: str) -> Path:
    """Grab a single frame from the camera's RTSP stream via ffmpeg."""
    input_dir = CAPTURES_DIR / event_id
    input_dir.mkdir(parents=True, exist_ok=True)
    output_path = input_dir / "snapshot.jpg"

    rtsp_url = config["camera"]["rtsp_url"]
    quality = config["camera"]["snapshot_quality"]
    transport = config["camera"]["rtsp_transport"]

    cmd = [
        "ffmpeg", "-y",
        "-rtsp_transport", transport,
        "-i", rtsp_url,
        "-vframes", "1",
        "-q:v", str(quality),
        str(output_path),
    ]

    log.info(f"[{event_id}] Capturing frame from RTSP stream...")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

    if result.returncode != 0 or not output_path.exists():
        log.error(f"[{event_id}] ffmpeg failed: {result.stderr[-500:]}")
        raise RuntimeError(f"Frame capture failed: {result.stderr[-200:]}")

    file_size = output_path.stat().st_size
    log.info(f"[{event_id}] Captured {file_size:,} byte snapshot")
    return input_dir


def run_sharp(event_id: str, input_dir: Path) -> Path:
    """Run SHARP predict to generate a .ply Gaussian Splat."""
    output_dir = SPLATS_DIR / event_id
    output_dir.mkdir(parents=True, exist_ok=True)

    device = config["sharp"]["device"]
    timeout = config["sharp"]["timeout_seconds"]

    cmd = ["sharp", "predict", "-i", str(input_dir), "-o", str(output_dir)]

    # SHARP auto-detects device, but we can hint via environment
    env = os.environ.copy()
    if device == "cuda":
        env["CUDA_VISIBLE_DEVICES"] = env.get("CUDA_VISIBLE_DEVICES", "0")
    elif device == "cpu":
        env["CUDA_VISIBLE_DEVICES"] = ""

    log.info(f"[{event_id}] Running SHARP predict (device={device})...")
    start = time.time()
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, env=env
    )
    elapsed = time.time() - start

    if result.returncode != 0:
        log.error(f"[{event_id}] SHARP failed ({elapsed:.1f}s): {result.stderr[-500:]}")
        raise RuntimeError(f"SHARP prediction failed: {result.stderr[-200:]}")

    # Find the generated .ply file
    ply_files = list(output_dir.glob("*.ply"))
    if not ply_files:
        raise RuntimeError("SHARP completed but no .ply file was generated")

    ply_path = ply_files[0]
    ply_size = ply_path.stat().st_size
    log.info(
        f"[{event_id}] SHARP complete in {elapsed:.1f}s → "
        f"{ply_path.name} ({ply_size:,} bytes)"
    )
    return output_dir


def cleanup_old_splats():
    """Remove oldest splats beyond the configured maximum."""
    max_splats = config["server"]["max_splats"]
    all_events = sorted(SPLATS_DIR.iterdir(), key=lambda p: p.name, reverse=True)

    for old_dir in all_events[max_splats:]:
        if old_dir.is_dir():
            shutil.rmtree(old_dir, ignore_errors=True)
            # Also clean up corresponding capture
            capture_dir = CAPTURES_DIR / old_dir.name
            if capture_dir.exists():
                shutil.rmtree(capture_dir, ignore_errors=True)
            log.info(f"Cleaned up old event: {old_dir.name}")


def send_notification(event_id: str, ply_filename: str):
    """Send a push notification via ntfy.sh when a new splat is ready."""
    if not config["notifications"]["enabled"]:
        return

    topic = config["notifications"]["ntfy_topic"]
    server = config["notifications"]["ntfy_server"]

    if not topic:
        return

    port = config["server"]["port"]
    download_url = f"http://localhost:{port}/splats/{event_id}/{ply_filename}"

    try:
        tailscale_ip = get_tailscale_ip()
        if tailscale_ip:
            download_url = (
                f"http://{tailscale_ip}:{port}/splats/{event_id}/{ply_filename}"
            )
    except Exception:
        pass

    try:
        requests.post(
            f"{server}/{topic}",
            data=f"New Gaussian Splat ready!\n{download_url}",
            headers={
                "Title": f"SHARP: {event_id}",
                "Tags": "camera,3d",
                "Click": download_url,
            },
            timeout=10,
        )
        log.info(f"[{event_id}] Notification sent to ntfy/{topic}")
    except Exception as e:
        log.warning(f"[{event_id}] Notification failed: {e}")


def get_tailscale_ip() -> str | None:
    """Get this machine's Tailscale IP address."""
    try:
        result = subprocess.run(
            ["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def process_event(event_id: str, source: str, image_data: bytes | None = None):
    """Full pipeline: capture → SHARP → notify. Runs in a background thread."""
    global is_processing

    with processing_lock:
        if is_processing:
            log.warning(f"[{event_id}] Skipping — already processing another event")
            return
        is_processing = True

    try:
        log.info(f"[{event_id}] Processing event from {source}")

        # Step 1: Get the image
        if image_data:
            # Image was uploaded directly (Approach B)
            input_dir = CAPTURES_DIR / event_id
            input_dir.mkdir(parents=True, exist_ok=True)
            image_path = input_dir / "snapshot.jpg"
            image_path.write_bytes(image_data)
            log.info(f"[{event_id}] Using uploaded image ({len(image_data):,} bytes)")
        else:
            # Pull frame from RTSP stream (Approach A)
            input_dir = capture_frame(event_id)

        # Step 2: Run SHARP
        output_dir = run_sharp(event_id, input_dir)

        # Step 3: Notify
        ply_files = list(output_dir.glob("*.ply"))
        if ply_files:
            send_notification(event_id, ply_files[0].name)

        # Step 4: Housekeeping
        cleanup_old_splats()

        log.info(f"[{event_id}] Pipeline complete")

    except Exception as e:
        log.error(f"[{event_id}] Pipeline failed: {e}")
    finally:
        with processing_lock:
            is_processing = False


# ---------------------------------------------------------------------------
# Flask application
# ---------------------------------------------------------------------------

app = Flask(__name__)


def make_event_id() -> str:
    """Generate a timestamped event ID."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    short_uuid = uuid.uuid4().hex[:6]
    return f"{ts}_{short_uuid}"


@app.route("/trigger", methods=["POST"])
def trigger_event():
    """
    Lightweight event trigger — the VPS pulls the frame itself.
    Call this from Home Assistant, IFTTT, or any webhook source.

    Optional JSON body:
        {"source": "home_assistant", "event_type": "person"}
    """
    data = request.get_json(silent=True) or {}
    source = data.get("source", "webhook")
    event_type = data.get("event_type", "unknown")
    event_id = make_event_id()

    log.info(f"[{event_id}] Trigger received: type={event_type}, source={source}")

    thread = threading.Thread(
        target=process_event,
        args=(event_id, source),
        daemon=True,
    )
    thread.start()

    return jsonify({
        "status": "processing",
        "event_id": event_id,
        "message": "Capturing frame and generating Gaussian Splat...",
    })


@app.route("/capture", methods=["POST"])
def upload_and_process():
    """
    Upload an image directly (Approach B).
    Use when the trigger source also provides the image.

    Send as multipart form: curl -F "image=@snapshot.jpg" http://vps:8080/capture
    """
    if "image" not in request.files:
        return jsonify({"error": "No image file provided"}), 400

    image = request.files["image"]
    image_data = image.read()

    if len(image_data) < 1000:
        return jsonify({"error": "Image too small — likely corrupt"}), 400

    event_id = make_event_id()
    source = request.form.get("source", "upload")

    thread = threading.Thread(
        target=process_event,
        args=(event_id, source, image_data),
        daemon=True,
    )
    thread.start()

    return jsonify({
        "status": "processing",
        "event_id": event_id,
        "message": "Processing uploaded image...",
    })


@app.route("/splats/<event_id>/<filename>")
def serve_splat(event_id, filename):
    """Serve a specific .ply file for download."""
    directory = SPLATS_DIR / event_id
    if not directory.exists():
        return jsonify({"error": "Event not found"}), 404
    return send_from_directory(str(directory), filename)


@app.route("/captures/<event_id>/<filename>")
def serve_capture(event_id, filename):
    """Serve the original snapshot image."""
    directory = CAPTURES_DIR / event_id
    if not directory.exists():
        return jsonify({"error": "Event not found"}), 404
    return send_from_directory(str(directory), filename)


@app.route("/latest")
def latest_splat():
    """Redirect to the most recent .ply file."""
    events = sorted(SPLATS_DIR.iterdir(), key=lambda p: p.name, reverse=True)
    for event_dir in events:
        if event_dir.is_dir():
            ply_files = list(event_dir.glob("*.ply"))
            if ply_files:
                return send_from_directory(str(event_dir), ply_files[0].name)
    return jsonify({"error": "No splats available yet"}), 404


@app.route("/events")
def list_events():
    """List all processed events as JSON."""
    events = []
    for event_dir in sorted(SPLATS_DIR.iterdir(), key=lambda p: p.name, reverse=True):
        if not event_dir.is_dir():
            continue
        ply_files = list(event_dir.glob("*.ply"))
        capture_dir = CAPTURES_DIR / event_dir.name
        has_snapshot = (capture_dir / "snapshot.jpg").exists()

        events.append({
            "event_id": event_dir.name,
            "has_ply": len(ply_files) > 0,
            "ply_filename": ply_files[0].name if ply_files else None,
            "ply_size": ply_files[0].stat().st_size if ply_files else 0,
            "has_snapshot": has_snapshot,
        })
    return jsonify(events)


@app.route("/status")
def status():
    """Server health check."""
    splat_count = sum(1 for d in SPLATS_DIR.iterdir() if d.is_dir())
    return jsonify({
        "status": "ok",
        "processing": is_processing,
        "splat_count": splat_count,
        "tailscale_ip": get_tailscale_ip(),
    })


@app.route("/")
def gallery():
    """Serve the static gallery HTML page."""
    gallery_path = Path(__file__).parent / "gallery.html"
    if gallery_path.exists():
        return send_file(str(gallery_path))
    return "<h1>SHARP Pipeline</h1><p>Gallery page not found. See /events for JSON.</p>"


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    log.info("=" * 60)
    log.info("SHARP Gaussian Splat Pipeline Server")
    log.info(f"  Data dir:    {DATA_DIR}")
    log.info(f"  Camera RTSP: {config['camera']['rtsp_url'][:40]}...")
    log.info(f"  SHARP device: {config['sharp']['device']}")
    ts_ip = get_tailscale_ip()
    if ts_ip:
        log.info(f"  Tailscale IP: {ts_ip}")
        log.info(f"  Gallery:     http://{ts_ip}:{config['server']['port']}/")
    log.info("=" * 60)

    app.run(
        host=config["server"]["host"],
        port=config["server"]["port"],
        debug=False,
    )
