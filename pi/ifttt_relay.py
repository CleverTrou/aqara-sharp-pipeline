#!/usr/bin/env python3
"""
IFTTT Webhook Relay — runs on the Pi alongside the motion monitor.

This provides an alternative (and smarter) trigger path:

    Aqara G5 Pro AI detection
        → Aqara Cloud
        → IFTTT applet (trigger: Aqara detection event)
        → IFTTT webhook action (POST to this relay)
        → This relay forwards to VPS over Tailscale

WHY RELAY THROUGH THE PI instead of IFTTT → VPS directly?
  - Your VPS has no public IP (it's Tailscale-only, by design).
  - IFTTT can't reach Tailscale IPs.
  - The Pi IS on the public internet (your home network), so IFTTT
    can POST to your-home-ip:9090 (port-forwarded), and the Pi
    forwards it over Tailscale to the VPS.

ALTERNATIVE: If you don't want to port-forward, you can use a free
Cloudflare Tunnel or ngrok on the Pi to receive IFTTT webhooks without
exposing your home IP.

IFTTT SETUP:
  1. Open IFTTT → Create → If This: "Aqara Home" → choose trigger
     (e.g., "Person detected by camera")
  2. Then That: "Webhooks" → "Make a web request"
     - URL: http://YOUR_HOME_IP:9090/ifttt
       (or your Cloudflare Tunnel / ngrok URL)
     - Method: POST
     - Content-Type: application/json
     - Body: {"event_type": "{{EventType}}", "source": "ifttt"}
  3. Save the applet

Usage:
    python3 ifttt_relay.py
    python3 ifttt_relay.py --config pi.yaml

Runs on port 9090 by default (separate from the VPS server on 8080).
"""

import argparse
import logging
import os
from pathlib import Path

import yaml

try:
    from flask import Flask, request, jsonify
    import requests as http_requests
except ImportError:
    print("Install dependencies: pip3 install flask requests")
    exit(1)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "relay": {
        "host": "0.0.0.0",
        "port": 9090,
    },
    "vps": {
        "trigger_url": "http://100.x.x.20:8080/trigger",
        "timeout": 10,
    },
}


def load_config(path):
    config = DEFAULT_CONFIG.copy()
    if path and Path(path).exists():
        with open(path) as f:
            user = yaml.safe_load(f) or {}
        for k, v in user.items():
            if k in config and isinstance(config[k], dict) and isinstance(v, dict):
                config[k].update(v)
            else:
                config[k] = v
    if os.environ.get("VPS_TRIGGER_URL"):
        config["vps"]["trigger_url"] = os.environ["VPS_TRIGGER_URL"]
    return config


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ifttt-relay")

app = Flask(__name__)
_config = {}


@app.route("/ifttt", methods=["POST"])
def ifttt_webhook():
    """
    Receive a webhook from IFTTT and forward it to the VPS.
    IFTTT sends JSON with whatever body you configured in the applet.
    """
    data = request.get_json(silent=True) or {}
    event_type = data.get("event_type", "ifttt_detection")
    source = data.get("source", "ifttt")

    log.info(f"IFTTT webhook received: type={event_type}")

    try:
        resp = http_requests.post(
            _config["vps"]["trigger_url"],
            json={"source": source, "event_type": event_type},
            timeout=_config["vps"]["timeout"],
        )
        log.info(f"Forwarded to VPS → {resp.status_code}")
        return jsonify({"status": "forwarded", "vps_response": resp.status_code})
    except http_requests.exceptions.ConnectionError:
        log.error("Cannot reach VPS — is Tailscale up?")
        return jsonify({"error": "VPS unreachable"}), 502
    except Exception as e:
        log.error(f"Forward failed: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/health")
def health():
    return jsonify({"status": "ok", "role": "ifttt_relay"})


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main():
    global _config
    parser = argparse.ArgumentParser(description="IFTTT webhook relay")
    parser.add_argument("--config", "-c", help="Path to YAML config file")
    args = parser.parse_args()

    _config = load_config(args.config)

    log.info("=" * 50)
    log.info("IFTTT Webhook Relay")
    log.info(f"  Listening:  :{_config['relay']['port']}/ifttt")
    log.info(f"  Forwarding: {_config['vps']['trigger_url']}")
    log.info("=" * 50)

    app.run(
        host=_config["relay"]["host"],
        port=_config["relay"]["port"],
        debug=False,
    )


if __name__ == "__main__":
    main()
