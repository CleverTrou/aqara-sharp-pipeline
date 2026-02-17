#!/usr/bin/env bash
# =============================================================================
# VPS Setup Script — SHARP Gaussian Splat Pipeline
# =============================================================================
# Run this on a fresh Ubuntu 22.04/24.04 VPS (with or without GPU).
#
# Usage:
#   chmod +x setup-vps.sh
#   sudo ./setup-vps.sh
#
# After running this script:
#   1. Configure Tailscale:  sudo tailscale up
#   2. Edit config.yaml with your camera's RTSP URL and Tailscale IP
#   3. Start the service:    sudo systemctl start sharp-pipeline
# =============================================================================

set -euo pipefail

echo "=========================================="
echo "  SHARP Pipeline — VPS Setup"
echo "=========================================="

# ---------------------------------------------------------------------------
# 1. System packages
# ---------------------------------------------------------------------------
echo ""
echo "[1/6] Installing system packages..."
apt-get update -qq
apt-get install -y -qq \
    curl \
    git \
    ffmpeg \
    python3 \
    python3-pip \
    python3-venv \
    > /dev/null

echo "  ✓ System packages installed"

# ---------------------------------------------------------------------------
# 2. Tailscale
# ---------------------------------------------------------------------------
echo ""
echo "[2/6] Installing Tailscale..."
if command -v tailscale &> /dev/null; then
    echo "  ✓ Tailscale already installed"
else
    curl -fsSL https://tailscale.com/install.sh | sh
    echo "  ✓ Tailscale installed"
    echo ""
    echo "  ⚠  Run 'sudo tailscale up' after this script to join your tailnet."
    echo "     Then verify your camera is reachable:"
    echo "       tailscale ping <camera-tailscale-ip>"
    echo ""
fi

# ---------------------------------------------------------------------------
# 3. Conda (for SHARP's Python 3.13 requirement)
# ---------------------------------------------------------------------------
echo "[3/6] Setting up Conda environment..."
CONDA_DIR="/opt/miniforge"

if [ ! -d "$CONDA_DIR" ]; then
    echo "  Installing Miniforge..."
    ARCH=$(uname -m)
    MINIFORGE_URL="https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-${ARCH}.sh"
    curl -fsSL "$MINIFORGE_URL" -o /tmp/miniforge.sh
    bash /tmp/miniforge.sh -b -p "$CONDA_DIR"
    rm /tmp/miniforge.sh
fi

# Initialize conda for this script
eval "$($CONDA_DIR/bin/conda shell.bash hook)"

if conda env list | grep -q "sharp"; then
    echo "  ✓ Conda env 'sharp' already exists"
else
    echo "  Creating conda env 'sharp' with Python 3.13..."
    conda create -y -n sharp python=3.13 -q
fi

conda activate sharp
echo "  ✓ Conda environment ready (Python $(python --version 2>&1 | cut -d' ' -f2))"

# ---------------------------------------------------------------------------
# 4. SHARP
# ---------------------------------------------------------------------------
echo ""
echo "[4/6] Installing Apple SHARP..."
SHARP_DIR="/opt/ml-sharp"

if [ ! -d "$SHARP_DIR" ]; then
    git clone https://github.com/apple/ml-sharp.git "$SHARP_DIR"
fi

cd "$SHARP_DIR"
pip install -q -r requirements.txt

# Verify SHARP is available
if sharp --help > /dev/null 2>&1; then
    echo "  ✓ SHARP installed and working"
else
    echo "  ⚠  SHARP installed but 'sharp' command not found in PATH."
    echo "     You may need to run: pip install -e ."
fi

# ---------------------------------------------------------------------------
# 5. Pipeline server
# ---------------------------------------------------------------------------
echo ""
echo "[5/6] Setting up pipeline server..."
PIPELINE_DIR="/opt/sharp-pipeline"

if [ ! -d "$PIPELINE_DIR" ]; then
    mkdir -p "$PIPELINE_DIR"
fi

# Copy files (assumes this script is run from the project directory)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cp "$SCRIPT_DIR/server.py" "$PIPELINE_DIR/"
cp "$SCRIPT_DIR/gallery.html" "$PIPELINE_DIR/"
cp "$SCRIPT_DIR/requirements.txt" "$PIPELINE_DIR/"

# Only copy config if it doesn't already exist (don't overwrite user edits)
if [ ! -f "$PIPELINE_DIR/config.yaml" ]; then
    cp "$SCRIPT_DIR/config.yaml" "$PIPELINE_DIR/"
    echo "  ⚠  Edit /opt/sharp-pipeline/config.yaml with your camera's RTSP URL!"
fi

pip install -q -r "$PIPELINE_DIR/requirements.txt"

# Create data directory
mkdir -p /data/sharp-pipeline/{captures,splats}

echo "  ✓ Pipeline server installed at $PIPELINE_DIR"

# ---------------------------------------------------------------------------
# 6. Systemd service
# ---------------------------------------------------------------------------
echo ""
echo "[6/6] Installing systemd service..."

cat > /etc/systemd/system/sharp-pipeline.service << 'UNIT'
[Unit]
Description=SHARP Gaussian Splat Pipeline Server
After=network-online.target tailscaled.service
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/sharp-pipeline
ExecStart=/opt/miniforge/envs/sharp/bin/python server.py
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

# Environment
Environment="PATH=/opt/miniforge/envs/sharp/bin:/usr/local/bin:/usr/bin:/bin"
Environment="HOME=/root"

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable sharp-pipeline

echo "  ✓ Systemd service installed and enabled"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "=========================================="
echo "  Setup Complete!"
echo "=========================================="
echo ""
echo "  Next steps:"
echo ""
echo "  1. Join your Tailscale network:"
echo "     sudo tailscale up"
echo ""
echo "  2. Edit the config with your camera details:"
echo "     nano /opt/sharp-pipeline/config.yaml"
echo ""
echo "  3. Start the service:"
echo "     sudo systemctl start sharp-pipeline"
echo ""
echo "  4. Check logs:"
echo "     journalctl -u sharp-pipeline -f"
echo ""
echo "  5. Test from any device on your tailnet:"
echo "     curl -X POST http://\$(tailscale ip -4):8080/trigger"
echo ""
echo "  6. Browse splats at:"
echo "     http://\$(tailscale ip -4):8080/"
echo ""
