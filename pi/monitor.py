#!/usr/bin/env python3
"""
Raspberry Pi Motion Monitor — Aqara G5 Pro RTSP Stream

Monitors the camera's RTSP stream for visual changes using ffmpeg frame
differencing. When motion is detected, fires a webhook to the SHARP
pipeline server on the VPS (reachable over Tailscale).

This replaces Home Assistant entirely. The Pi serves two roles:
  1. Tailscale subnet router (so the VPS can reach the camera)
  2. Motion detection trigger (this script)

Runs as a systemd service. See setup-pi.sh for installation.

Detection method:
  - Captures a reference frame, then periodically captures a new frame
  - Compares frames using pixel-level mean absolute difference (MAD)
  - If the difference exceeds a threshold, it's considered motion
  - After triggering, enters a cooldown period to avoid flooding

This is intentionally simple. For smarter detection (person vs wind),
use IFTTT with the Aqara app's built-in AI instead (see ifttt_relay.py).

Usage:
    python3 monitor.py                    # run with defaults
    python3 monitor.py --config pi.yaml   # run with custom config
"""

import argparse
import io
import logging
import os
import signal
import struct
import subprocess
import sys
import time
from pathlib import Path

import yaml

try:
    import requests
except ImportError:
    print("Install requests: pip3 install requests")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "camera": {
        "rtsp_url": "rtsp://USER:PASS@192.168.1.100:8554/1520p",
        "rtsp_transport": "tcp",
    },
    "detection": {
        # How often to capture a comparison frame (seconds).
        # Lower = more responsive but more CPU. 2-3s is good for a Pi Zero 2 W.
        "poll_interval": 3,

        # Resolution for motion comparison (downscaled for speed).
        # The actual snapshot sent to VPS is full resolution.
        "compare_width": 320,
        "compare_height": 240,

        # Pixel difference threshold (0-255). Higher = less sensitive.
        # 15-25 works well outdoors. 8-12 for indoor/static scenes.
        "threshold": 20,

        # Minimum percentage of changed pixels to count as motion.
        # Prevents triggering on sensor noise or minor lighting shifts.
        "min_changed_pct": 5.0,

        # Cooldown after a trigger before re-arming (seconds).
        "cooldown": 30,

        # Number of consecutive "changed" frames before triggering.
        # Helps filter single-frame glitches. 1 = trigger immediately.
        "confirm_frames": 2,
    },
    "vps": {
        # The SHARP pipeline server's Tailscale address.
        "trigger_url": "http://100.x.x.20:8080/trigger",
        "timeout": 10,
    },
    "logging": {
        "level": "INFO",
        # Optional: save detection snapshots to disk for debugging
        "save_debug_frames": False,
        "debug_dir": "/tmp/sharp-monitor-debug",
    },
}


def load_config(path: str | None) -> dict:
    config = DEFAULT_CONFIG.copy()
    if path and Path(path).exists():
        with open(path) as f:
            user = yaml.safe_load(f) or {}
        _deep_merge(config, user)

    # Env overrides
    if os.environ.get("CAMERA_RTSP_URL"):
        config["camera"]["rtsp_url"] = os.environ["CAMERA_RTSP_URL"]
    if os.environ.get("VPS_TRIGGER_URL"):
        config["vps"]["trigger_url"] = os.environ["VPS_TRIGGER_URL"]

    return config


def _deep_merge(base, override):
    for k, v in override.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("monitor")

# ---------------------------------------------------------------------------
# Frame capture and comparison
# ---------------------------------------------------------------------------


def capture_raw_frame(rtsp_url: str, transport: str, width: int, height: int) -> bytes | None:
    """
    Capture a single frame from RTSP, returned as raw RGB bytes.

    Uses ffmpeg to grab one frame, scale it down, and output raw RGB24.
    This avoids needing PIL/OpenCV on the Pi — pure bytes comparison.
    """
    cmd = [
        "ffmpeg", "-y",
        "-rtsp_transport", transport,
        "-i", rtsp_url,
        "-vframes", "1",
        "-vf", f"scale={width}:{height}",
        "-f", "rawvideo",
        "-pix_fmt", "rgb24",
        "pipe:1",
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=15,
        )
        if result.returncode != 0:
            log.warning("ffmpeg frame capture failed")
            return None

        expected_size = width * height * 3  # RGB24
        data = result.stdout
        if len(data) < expected_size:
            log.warning(f"Frame too small: {len(data)} < {expected_size}")
            return None

        return data[:expected_size]

    except subprocess.TimeoutExpired:
        log.warning("ffmpeg timed out capturing frame")
        return None
    except Exception as e:
        log.error(f"Frame capture error: {e}")
        return None


def compute_frame_diff(frame_a: bytes, frame_b: bytes, threshold: int) -> tuple[float, float]:
    """
    Compare two raw RGB frames, return (mean_abs_diff, pct_changed_pixels).

    Operates directly on bytes without numpy/PIL to keep dependencies minimal
    on the Pi. For a 320x240 RGB frame that's 230,400 bytes — fast enough.
    """
    if len(frame_a) != len(frame_b):
        return 0.0, 0.0

    total_pixels = len(frame_a) // 3
    total_diff = 0
    changed_pixels = 0

    # Process in chunks for speed (compare per-pixel RGB average)
    for i in range(0, len(frame_a), 3):
        # Average of RGB channels for each pixel
        avg_a = (frame_a[i] + frame_a[i + 1] + frame_a[i + 2]) // 3
        avg_b = (frame_b[i] + frame_b[i + 1] + frame_b[i + 2]) // 3
        diff = abs(avg_a - avg_b)
        total_diff += diff
        if diff > threshold:
            changed_pixels += 1

    mean_diff = total_diff / total_pixels
    pct_changed = (changed_pixels / total_pixels) * 100

    return mean_diff, pct_changed


def fire_trigger(trigger_url: str, timeout: int, event_type: str = "motion"):
    """Send a trigger to the VPS pipeline server."""
    try:
        resp = requests.post(
            trigger_url,
            json={"source": "pi_monitor", "event_type": event_type},
            timeout=timeout,
        )
        if resp.status_code == 200:
            data = resp.json()
            log.info(f"Trigger sent → event_id={data.get('event_id', '?')}")
        else:
            log.warning(f"Trigger returned {resp.status_code}: {resp.text[:200]}")
    except requests.exceptions.ConnectionError:
        log.error(f"Cannot reach VPS at {trigger_url} — is Tailscale up?")
    except Exception as e:
        log.error(f"Trigger failed: {e}")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

running = True


def handle_signal(signum, frame):
    global running
    log.info("Shutting down...")
    running = False


def main():
    parser = argparse.ArgumentParser(description="Aqara RTSP motion monitor")
    parser.add_argument("--config", "-c", help="Path to YAML config file")
    args = parser.parse_args()

    config = load_config(args.config)
    log.setLevel(config["logging"]["level"])

    cam = config["camera"]
    det = config["detection"]
    vps = config["vps"]

    rtsp_url = cam["rtsp_url"]
    transport = cam["rtsp_transport"]
    width = det["compare_width"]
    height = det["compare_height"]
    threshold = det["threshold"]
    min_pct = det["min_changed_pct"]
    cooldown = det["cooldown"]
    poll = det["poll_interval"]
    confirm_needed = det["confirm_frames"]

    log.info("=" * 50)
    log.info("SHARP Pipeline — Pi Motion Monitor")
    log.info(f"  Camera:    {rtsp_url[:40]}...")
    log.info(f"  VPS:       {vps['trigger_url']}")
    log.info(f"  Poll:      every {poll}s")
    log.info(f"  Threshold: {threshold} (min {min_pct}% changed)")
    log.info(f"  Cooldown:  {cooldown}s after trigger")
    log.info(f"  Confirm:   {confirm_needed} consecutive frames")
    log.info("=" * 50)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # Debug frame saving
    debug_dir = None
    if config["logging"]["save_debug_frames"]:
        debug_dir = Path(config["logging"]["debug_dir"])
        debug_dir.mkdir(parents=True, exist_ok=True)
        log.info(f"  Debug frames: {debug_dir}")

    # Capture initial reference frame
    log.info("Capturing reference frame...")
    reference = None
    while reference is None and running:
        reference = capture_raw_frame(rtsp_url, transport, width, height)
        if reference is None:
            log.warning("Failed to capture reference — retrying in 5s...")
            time.sleep(5)

    if not running:
        return

    log.info(f"Reference frame captured ({len(reference):,} bytes). Monitoring...")

    last_trigger_time = 0
    confirm_count = 0

    while running:
        time.sleep(poll)

        current = capture_raw_frame(rtsp_url, transport, width, height)
        if current is None:
            confirm_count = 0
            continue

        mean_diff, pct_changed = compute_frame_diff(reference, current, threshold)

        in_cooldown = (time.time() - last_trigger_time) < cooldown

        if pct_changed >= min_pct:
            confirm_count += 1
            level = "INFO" if not in_cooldown else "DEBUG"
            log.log(
                logging.getLevelName(level),
                f"Change detected: diff={mean_diff:.1f} changed={pct_changed:.1f}% "
                f"[{confirm_count}/{confirm_needed}]"
                + (" (cooldown)" if in_cooldown else ""),
            )

            if confirm_count >= confirm_needed and not in_cooldown:
                log.info("MOTION CONFIRMED — firing trigger")
                fire_trigger(vps["trigger_url"], vps["timeout"])
                last_trigger_time = time.time()
                confirm_count = 0

                # Save debug frame if enabled
                if debug_dir:
                    ts = time.strftime("%Y%m%d_%H%M%S")
                    # Write raw RGB as a simple PPM file (no dependencies needed)
                    ppm_path = debug_dir / f"trigger_{ts}.ppm"
                    with open(ppm_path, "wb") as f:
                        header = f"P6\n{width} {height}\n255\n".encode()
                        f.write(header)
                        f.write(current)
                    log.debug(f"Debug frame saved: {ppm_path}")
        else:
            confirm_count = 0

        # Update reference frame (gradual adaptation to lighting changes).
        # Only update when NOT in a motion event to avoid capturing the
        # moving subject as the new "normal."
        if pct_changed < min_pct:
            reference = current

    log.info("Monitor stopped.")


if __name__ == "__main__":
    main()
