# Aqara G5 Pro → SHARP Gaussian Splat Pipeline

Automatically generate 3D Gaussian Splat renders from your Aqara G5 Pro security
camera when it detects people, animals, or motion. View the results on your iPhone,
Meta Quest 2, or Mac.

All traffic stays within your **Tailscale** mesh network. Nothing is exposed to
the public internet.

## How It Works

```
┌──────────────────── Your LAN ────────────────────┐
│                                                   │
│  Aqara G5 Pro ◄──RTSP──┐                         │
│  192.168.1.100          │                         │
│                         │                         │
│  Raspberry Pi ──────────┘                         │
│  (Tailscale subnet router + motion monitor)       │
│                                                   │
└───────────┬───────────────────────────────────────┘
            │ Tailscale (WireGuard)
            │
            │  ① Pi detects motion → POST /trigger
            │  ② VPS pulls frame via RTSP (routed through Pi)
            │  ③ SHARP generates .ply
            ▼
┌───────────────────────────────────────────────────┐
│  Cloud VPS (100.x.x.20)                          │
│  • server.py (Flask)                              │
│  • ffmpeg snapshot from RTSP                      │
│  • SHARP predict → .ply                           │
│  • Web gallery + file server                      │
└───────────┬───────────────────────────────────────┘
            │ Tailscale
            ▼
    ┌───────────────┐
    │  Your Devices  │
    │  • iPhone 17   │  ← browse gallery / download .ply
    │  • Quest 2     │  ← open in SuperSplat WebXR
    │  • MacBook Pro │  ← open in Spatial Fields
    └───────────────┘
```

## Two Trigger Options

| Method | Detection Quality | Latency | Cloud Dependency | Default |
|--------|-------------------|---------|------------------|---------|
| **Aqara AI → IFTTT → Pi relay** | Person, animal, face, vehicle, sound | ~5-8s | Aqara Cloud + IFTTT | **Primary** |
| **Pi frame differencing** | Basic motion only | ~3s | None (fully local) | Fallback |

The IFTTT path is the primary trigger — it uses the camera's onboard AI which is
far smarter than pixel comparison. The local motion monitor (`monitor.py`) exists
only as a fallback for when you want zero cloud dependency.

## Project Structure

```
aqara-sharp-pipeline/
├── server.py                    # VPS: pipeline server (Flask)
├── gallery.html                 # VPS: web gallery UI
├── config.yaml                  # VPS: configuration
├── requirements.txt             # VPS: Python dependencies
├── setup-vps.sh                 # VPS: one-command setup
│
├── pi/                          # Raspberry Pi files
│   ├── monitor.py               # Motion detection via RTSP
│   ├── ifttt_relay.py           # IFTTT webhook → VPS forwarder
│   ├── pi.yaml                  # Pi configuration
│   └── setup-pi.sh              # One-command Pi setup
│
└── homeassistant/               # (Optional) Home Assistant configs
    ├── automations.yaml
    └── configuration.yaml
```

## Quick Start

### 1. Set up the Raspberry Pi

**Recommended:** Pi Zero 2 W (~$15), Pi 3B+, Pi 4, or Pi 5.
**Not recommended:** Pi Zero W (original) — too slow for Tailscale.

```bash
# Flash Raspberry Pi OS Lite (64-bit) to an SD card
# Connect to Wi-Fi, enable SSH, then:

ssh pi@raspberrypi.local
git clone <this-repo> /tmp/sharp-pipeline
cd /tmp/sharp-pipeline/pi
sudo ./setup-pi.sh
```

Join Tailscale and advertise your LAN subnet:

```bash
# Find your subnet first:
ip route | grep 'src' | head -1
# e.g., "192.168.1.0/24 dev wlan0 ..."

sudo tailscale up --advertise-routes=192.168.1.0/24
```

Then approve the subnet route in the [Tailscale admin console](https://login.tailscale.com/admin/machines) → find the Pi → Edit route settings → Approve.

### 2. Provision a VPS

Any Linux VPS works. For fastest SHARP processing, pick one with a GPU:

| Provider | GPU | Price |
|----------|-----|-------|
| Vast.ai | RTX 3060+ | ~$0.10-0.30/hr |
| TensorDock | Various | ~$0.12/hr+ |
| RunPod | Various | ~$0.20/hr+ |
| Hetzner (CPU only) | None | ~$4/mo |

SHARP `predict` works on CPU — just slower (~10-30s vs <1s on GPU).

```bash
# On your VPS:
git clone <this-repo> /tmp/sharp-pipeline
cd /tmp/sharp-pipeline
sudo ./setup-vps.sh
```

### 3. Configure both machines

**On the Pi** — edit `/opt/sharp-monitor/pi.yaml`:
```yaml
camera:
  rtsp_url: "rtsp://USER:PASS@192.168.1.100:8554/360p"
vps:
  trigger_url: "http://100.x.x.20:8080/trigger"   # VPS Tailscale IP
```

**On the VPS** — edit `/opt/sharp-pipeline/config.yaml`:
```yaml
camera:
  # Use the camera's LAN IP — routed through Pi's Tailscale subnet
  rtsp_url: "rtsp://USER:PASS@192.168.1.100:8554/1520p"
```

### 4. Set up IFTTT

1. In the **Aqara Home** app, ensure your camera's AI detection types
   (person, animal, etc.) are enabled under camera settings.

2. In **IFTTT**, create an applet:
   - **If This:** Aqara Home → (choose your detection trigger, e.g. "Person detected")
   - **Then That:** Webhooks → Make a web request
     - URL: `http://YOUR_HOME_PUBLIC_IP:9090/ifttt`
     - Method: POST
     - Content-Type: application/json
     - Body: `{"event_type": "person", "source": "ifttt"}`

3. Port-forward port 9090 on your router to the Pi's LAN IP.
   (Or use a Cloudflare Tunnel / ngrok to avoid exposing your home IP.)

### 5. Start everything

```bash
# On the Pi:
sudo systemctl start sharp-ifttt-relay
journalctl -u sharp-ifttt-relay -f

# On the VPS:
sudo systemctl start sharp-pipeline
journalctl -u sharp-pipeline -f
```

### 6. Test

```bash
# From any machine on your tailnet:
curl -X POST http://<VPS_TAILSCALE_IP>:8080/trigger \
  -H "Content-Type: application/json" \
  -d '{"source": "test", "event_type": "manual"}'
```

Then open `http://<VPS_TAILSCALE_IP>:8080/` in your browser to see the gallery.

## Optional: Local Motion Monitor (No Cloud)

If you want a fallback that works even when IFTTT or Aqara's cloud is
down, enable the local frame-differencing monitor on the Pi:

```bash
sudo systemctl enable --now sharp-monitor
```

This polls the camera's RTSP stream every 3 seconds and detects pixel-level
changes. It can't distinguish a person from a swaying tree — it only knows
"something changed." Tune sensitivity in `pi.yaml` (see below).

## Viewing .ply Files

### iPhone 17 Pro Max
- **Spatial Fields** — best native Gaussian Splat viewer for iOS
- **MetalSplatter** — open-source Metal renderer
- **SHARP & Stereo** — specifically designed for SHARP .ply output

### Meta Quest 2
- **SuperSplat (WebXR)** — open the gallery URL in Quest Browser
- **BDViewer** — native Quest app for Gaussian Splats
- **MR Gaussian Splat Viewer** — web-based, works in Quest Browser

### MacBook Pro (2018 Intel)
- **Spatial Fields** (macOS version)
- **SuperSplat** — web-based editor/viewer at playcanvas.com/supersplat/editor
- Open the gallery at `http://<VPS_TAILSCALE_IP>:8080/`

## API Endpoints (VPS)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Web gallery |
| `/trigger` | POST | Trigger capture + SHARP processing |
| `/capture` | POST | Upload image directly (multipart form) |
| `/latest` | GET | Download the most recent .ply file |
| `/events` | GET | List all events as JSON |
| `/status` | GET | Server health check |
| `/splats/<id>/<file>` | GET | Download a specific .ply file |
| `/captures/<id>/snapshot.jpg` | GET | View the original snapshot |

## Push Notifications

Get alerts on your iPhone when a new splat is ready:

1. Install the [ntfy app](https://ntfy.sh) on your iPhone
2. Subscribe to a topic (e.g., `my-sharp-splats`)
3. Set in the VPS config.yaml:
   ```yaml
   notifications:
     enabled: true
     ntfy_topic: "my-sharp-splats"
   ```

## Tuning Local Motion Detection (optional monitor only)

Only relevant if you enabled `sharp-monitor.service`. Edit `pi.yaml` on the Pi:

| Parameter | Default | Effect |
|-----------|---------|--------|
| `threshold` | 20 | Per-pixel brightness change needed (lower = more sensitive) |
| `min_changed_pct` | 5.0 | % of frame that must change (lower = more sensitive) |
| `cooldown` | 30 | Seconds between triggers |
| `confirm_frames` | 2 | Consecutive changed frames needed (higher = fewer false positives) |
| `poll_interval` | 3 | Seconds between frame checks |

Enable `save_debug_frames: true` to see what triggered detection.
