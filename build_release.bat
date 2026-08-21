@echo off
chcp 65001 > nul
echo ==================================================
echo 🛠️ Building Standalone Single Player Release Build
echo ==================================================
uv run python build_release.py
pause
