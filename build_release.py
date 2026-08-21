"""
Release Build Packager for Retro Fighter Jet BCI Game (Single-Player Standalone Edition)
Packages Python source into an optimized Windows standalone binary using PyInstaller.
"""

import os
import sys
import subprocess
import shutil
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

def main():
    root_dir = Path(__file__).resolve().parent
    dist_dir = root_dir / "dist" / "RetroFighterJet_BCI"
    
    print("==================================================")
    print("🚀 Building Retro Fighter Jet BCI: Single Player Release")
    print("==================================================")

    # Clean previous build artifacts
    if dist_dir.exists():
        print(f"🧹 Cleaning previous release directory: {dist_dir}")
        shutil.rmtree(dist_dir, ignore_errors=True)

    # PyInstaller packaging command
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--onedir",
        "--name=RetroFighterJet_BCI",
        "--collect-all=pylsl",
        "--collect-all=OpenGL",
        "--collect-all=pygame",
        "--hidden-import=game_input",
        "--hidden-import=pylsl",
        "--hidden-import=OpenGL.GL",
        "--hidden-import=OpenGL.GLU",
        "--hidden-import=scipy.signal",
        str(root_dir / "lsl_retro_airplane.py"),
    ]

    print(f"📦 Executing packaging: {' '.join(cmd)}")
    res = subprocess.run(cmd, cwd=str(root_dir))
    if res.returncode != 0:
        print("❌ PyInstaller build failed.")
        sys.exit(res.returncode)

    # Copy companion files (profiles, batch launcher) into output folder
    out_dir = root_dir / "dist" / "RetroFighterJet_BCI"
    if out_dir.exists():
        if (root_dir / "bci_profile.json").exists():
            shutil.copy(root_dir / "bci_profile.json", out_dir / "bci_profile.json")
        
        # Create a ready-to-run batch launcher inside dist
        launcher_bat = out_dir / "Play_SinglePlayer.bat"
        with open(launcher_bat, "w", encoding="utf-8") as f:
            f.write("@echo off\n")
            f.write("echo 🚀 Starting Retro Fighter Jet BCI (Single Player)...\n")
            f.write("start RetroFighterJet_BCI.exe --players 1\n")
        
        fullscreen_bat = out_dir / "Play_Fullscreen.bat"
        with open(fullscreen_bat, "w", encoding="utf-8") as f:
            f.write("@echo off\n")
            f.write("echo 🖥️ Starting Retro Fighter Jet BCI (Fullscreen)...\n")
            f.write("start RetroFighterJet_BCI.exe --players 1 --fullscreen\n")

        print("==================================================")
        print("✅ Release Build Successfully Created!")
        print(f"📂 Location: {out_dir}")
        print("🎮 Executable: RetroFighterJet_BCI.exe")
        print("==================================================")

if __name__ == "__main__":
    main()
