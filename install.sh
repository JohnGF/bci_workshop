#!/bin/bash
set -e

echo "================================================="
echo "🧠 BCI Workshop: Native macOS / Linux Setup"
echo "================================================="
echo ""

# Install uv (Python package manager)
if ! command -v uv &> /dev/null
then
    echo "[1/3] 🚀 Installing 'uv' Python package manager..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
else
    echo "[1/3] ✅ 'uv' is already installed."
fi

# Sync dependencies
echo ""
echo "[2/3] 🐍 Syncing Python dependencies..."
uv sync

echo ""
echo "================================================="
echo "✅ Setup Complete!"
echo "================================================="
echo ""
echo "To launch the BCI Master Dashboard, run:"
echo ""
echo "  uv run main.py"
echo ""
