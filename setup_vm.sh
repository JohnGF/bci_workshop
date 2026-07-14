#!/bin/bash
set -e

echo "================================================="
echo "🧠 BCI & Kinematic Workshop: VM Setup Script"
echo "================================================="
echo "This script will install all necessary system"
echo "dependencies to run the BCI Workshop inside a"
echo "fresh Ubuntu Virtual Machine."
echo ""

# Update and install system packages
echo "📦 Installing system dependencies (GUI, Audio, OpenGL, Bluetooth)..."
sudo apt-get update
sudo apt-get install -y \
    python3 \
    python3-pip \
    python3-venv \
    curl \
    git \
    build-essential \
    libgl1 \
    libgl1-mesa-dri \
    libglib2.0-0 \
    mesa-utils \
    libxcb-cursor0 \
    alsa-utils \
    libasound2-dev \
    libxkbcommon-x11-0 \
    libxcb-icccm4 \
    libxcb-image0 \
    libxcb-keysyms1 \
    libxcb-randr0 \
    libxcb-render-util0 \
    libxcb-shape0 \
    libxcb-xfixes0 \
    libxcb-xinerama0 \
    bluez \
    bluetooth \
    rfkill \
    libbluetooth-dev \
    fonts-noto-color-emoji

# Install uv (Python package manager)
echo "🚀 Installing 'uv' Python package manager..."
if ! command -v uv &> /dev/null
then
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
else
    echo "✅ 'uv' is already installed."
fi

# Prevent VM from overwriting the host's Windows .venv
export UV_PROJECT_ENVIRONMENT="/home/vagrant/.venv"
echo 'export UV_PROJECT_ENVIRONMENT="/home/vagrant/.venv"' >> ~/.bashrc

# Install python dependencies via uv
echo "🐍 Syncing Python dependencies..."
uv sync

echo "================================================="
echo "✅ Setup Complete!"
echo "If this is a VM (VirtualBox/VMware), ensure you have:"
echo " 1. Enabled 3D Acceleration in your VM Display settings."
echo " 2. Passed through your Host's Bluetooth adapter via USB settings."
echo ""
echo "You can now launch the dashboard with:"
echo "  uv run main.py"
echo "================================================="
