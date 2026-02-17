#!/usr/bin/env bash
# =============================================================================
# Raspberry Pi Setup — SHARP Pipeline Monitor + Tailscale Subnet Router
# =============================================================================
#
# Supported Pi models:
#   ✅ Pi Zero 2 W (recommended — smallest form factor that works)
#   ✅ Pi 3B / 3B+
#   ✅ Pi 4 / Pi 5
#   ❌ Pi Zero W (original) — too slow for Tailscale + ffmpeg
#
# This script sets up the Pi to serve two roles:
#   1. Tailscale subnet router — lets your VPS reach the camera's RTSP
#   2. Motion monitor — detects changes and triggers the VPS pipeline
#
# Prerequisites:
#   - Raspberry Pi OS Lite (64-bit recommended for Zero 2 W)
#   - Wi-Fi configured and connected to the same network as the camera
#   - SSH access
#
# Usage:
#   chmod +x setup-pi.sh
#   sudo ./setup-pi.sh
#
# After running:
#   1. sudo tailscale up --advertise-routes=192.168.1.0/24
#      (replace with YOUR local subnet)
#   2. Approve the subnet route in Tailscale admin console:
#      https://login.tailscale.com/admin/machines
#   3. Edit /opt/sharp-monitor/pi.yaml with your camera + VPS details
#   4. sudo systemctl start sharp-monitor
# =============================================================================

set -euo pipefail

echo "=========================================="
echo "  SHARP Pipeline — Pi Setup"
echo "=========================================="

# ---------------------------------------------------------------------------
# Preflight checks
# ---------------------------------------------------------------------------
ARCH=$(uname -m)
echo ""
echo "  Architecture: $ARCH"
echo "  Memory: $(free -h | awk '/^Mem:/{print $2}')"
echo ""

if [[ "$ARCH" == "armv6l" ]]; then
    echo "  ⚠  WARNING: ARMv6 detected (Pi Zero W / Pi 1)."
    echo "  This hardware is too slow for Tailscale + ffmpeg."
    echo "  Please use a Pi Zero 2 W or newer."
    echo ""
    read -p "  Continue anyway? (y/N) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
fi

# ---------------------------------------------------------------------------
# 1. System packages
# ---------------------------------------------------------------------------
echo "[1/5] Installing system packages..."
apt-get update -qq
apt-get install -y -qq \
    ffmpeg \
    python3 \
    python3-pip \
    python3-venv \
    curl \
    > /dev/null 2>&1

echo "  ✓ System packages installed"

# ---------------------------------------------------------------------------
# 2. Tailscale
# ---------------------------------------------------------------------------
echo ""
echo "[2/5] Installing Tailscale..."
if command -v tailscale &> /dev/null; then
    echo "  ✓ Tailscale already installed ($(tailscale version | head -1))"
else
    curl -fsSL https://tailscale.com/install.sh | sh
    echo "  ✓ Tailscale installed"
fi

# Enable IP forwarding (required for subnet routing)
echo ""
echo "[3/5] Configuring IP forwarding for subnet routing..."

SYSCTL_CONF="/etc/sysctl.d/99-tailscale-forward.conf"
cat > "$SYSCTL_CONF" << 'EOF'
# Enable IP forwarding for Tailscale subnet routing
net.ipv4.ip_forward = 1
net.ipv6.conf.all.forwarding = 1
EOF

sysctl -p "$SYSCTL_CONF" > /dev/null 2>&1
echo "  ✓ IP forwarding enabled"

# ---------------------------------------------------------------------------
# 4. Monitor application
# ---------------------------------------------------------------------------
echo ""
echo "[4/5] Installing SHARP monitor..."
MONITOR_DIR="/opt/sharp-monitor"
mkdir -p "$MONITOR_DIR"

# Copy files
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cp "$SCRIPT_DIR/monitor.py" "$MONITOR_DIR/"
cp "$SCRIPT_DIR/ifttt_relay.py" "$MONITOR_DIR/"

# Only copy config if not already present
if [ ! -f "$MONITOR_DIR/pi.yaml" ]; then
    if [ -f "$SCRIPT_DIR/pi.yaml" ]; then
        cp "$SCRIPT_DIR/pi.yaml" "$MONITOR_DIR/"
    fi
    echo "  ⚠  Edit /opt/sharp-monitor/pi.yaml with your settings!"
fi

# Install Python dependencies (minimal — just requests, flask, pyyaml)
pip3 install --break-system-packages -q requests flask pyyaml 2>/dev/null \
    || pip3 install -q requests flask pyyaml

echo "  ✓ Monitor installed at $MONITOR_DIR"

# ---------------------------------------------------------------------------
# 5. Systemd services
# ---------------------------------------------------------------------------
echo ""
echo "[5/5] Installing systemd services..."

# --- Motion monitor service ---
cat > /etc/systemd/system/sharp-monitor.service << 'UNIT'
[Unit]
Description=SHARP Pipeline — RTSP Motion Monitor
After=network-online.target tailscaled.service
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/sharp-monitor
ExecStart=/usr/bin/python3 monitor.py --config pi.yaml
Restart=on-failure
RestartSec=15
StandardOutput=journal
StandardError=journal

# Limit memory on resource-constrained Pis
MemoryMax=200M

[Install]
WantedBy=multi-user.target
UNIT

# --- IFTTT relay service (optional) ---
cat > /etc/systemd/system/sharp-ifttt-relay.service << 'UNIT'
[Unit]
Description=SHARP Pipeline — IFTTT Webhook Relay
After=network-online.target tailscaled.service
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/sharp-monitor
ExecStart=/usr/bin/python3 ifttt_relay.py --config pi.yaml
Restart=on-failure
RestartSec=15
StandardOutput=journal
StandardError=journal

MemoryMax=100M

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable sharp-ifttt-relay

# Don't auto-enable the local motion monitor — it's a fallback
# for when you don't want to depend on IFTTT/Aqara cloud.
echo "  ✓ sharp-ifttt-relay.service installed and enabled (primary trigger)"
echo "  ✓ sharp-monitor.service installed (not enabled — local fallback)"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "=========================================="
echo "  Setup Complete!"
echo "=========================================="
echo ""
echo "  STEP 1 — Join Tailscale and advertise your LAN:"
echo ""
echo "    sudo tailscale up --advertise-routes=192.168.1.0/24"
echo ""
echo "    Replace 192.168.1.0/24 with YOUR local subnet."
echo "    Find it with: ip route | grep 'src' | head -1"
echo ""
echo "  STEP 2 — Approve the subnet route in the Tailscale admin console:"
echo "    https://login.tailscale.com/admin/machines"
echo "    Find the Pi → Edit route settings → Approve the subnet"
echo ""
echo "  STEP 3 — Edit the config:"
echo "    nano /opt/sharp-monitor/pi.yaml"
echo ""
echo "  STEP 4 — Start the IFTTT relay:"
echo "    sudo systemctl start sharp-ifttt-relay"
echo "    journalctl -u sharp-ifttt-relay -f"
echo ""
echo "  STEP 5 — Configure IFTTT:"
echo "    Create an applet: Aqara detection → Webhook POST"
echo "    URL: http://YOUR_HOME_PUBLIC_IP:9090/ifttt"
echo "    (or use Cloudflare Tunnel / ngrok instead of port-forwarding)"
echo ""
echo "  OPTIONAL — Enable local motion monitor (no cloud dependency):"
echo "    sudo systemctl enable --now sharp-monitor"
echo "    This is a fallback if IFTTT/Aqara cloud is down."
echo ""
echo "  VERIFY — Test the pipeline end-to-end:"
echo "    curl -X POST http://\$(tailscale ip -4 | head -1):8080/trigger"
echo "    (run from the Pi, targeting VPS)"
echo ""
