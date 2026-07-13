# 🫀 Brain & Pulse: Muse & Smartwatch LSL Workshop

This repository contains real-time signal processing and visualization tools for Brain-Computer Interfaces (BCI) and Physiological Computing, designed for interactive academic workshops.

All scripts use the modern Python script metadata format and can be run instantly with `uv` (which automatically manages dependency environments).

---

## 🚀 Setup & Prerequisites

We recommend using **[uv](https://github.com/astral-sh/uv)**, a fast Python package installer and runner.

If you don't have `uv` installed, run:
```bash
# macOS/Linux
curl -LsSf https://astral-sh/uv/install.sh | sh
```

Make sure your Bluetooth is enabled if streaming from actual Muse hardware.

---

## 📺 Live Presentation Synchronization Server

For local workshops and lectures, you can run a local WebSocket server that syncs the presentation slides across all attendee devices in real time and lets you remote-control the slide deck from your phone.

### 1. Start the Sync Server
This establishes a central WebSocket server on port `8080` that manages the slide state, broadcasts lock states, and forwards remote commands.

Run:
```bash
uv run presentation_sync_server.py
```

### 2. Connect as Master
Load the presentation URL on your main projector computer with the `?role=master` parameter:
```
http://localhost:4321/about_me/presentation/bci_advanced/bci_advanced.html?role=master
```

### 3. Connect as Audience (Listeners)
When attendees load the standard URL (without parameters, e.g. by scanning the QR code on Slide 1), they join as **Listeners**.
*   **Locked Mode (Default):** Manual navigation is disabled on the listener's screen. Their view updates automatically as the Master changes slides.
*   **Independent Mode:** If the Master disables the lock, listeners can browse the slides at their own pace. If they read ahead, they can click the **"Jump to Master"** button to snap back to the presenter's active slide.

### 4. Connect as Phone Remote Controller
To control the presentation while moving around the room:
1. Open the **Script Window** (`🎤` button or `S` key) on your laptop.
2. Scan the **Remote Controller QR code** shown at the bottom of the notes.
3. Your phone will open the page with `?role=controller`, displaying a touch remote with **Prev/Next** buttons, a **Sync Lock** toggle, and the **Speaker Script** matching the active slide.

---

## ⌚ Smartwatch PPG Simulation (No Hardware Required)

Since raw smartwatch PPG (optical heart rate) streams are restricted by Wear OS/watch OS for third-party developers, we provide an interactive simulator that streams high-fidelity PPG data over the Lab Streaming Layer (LSL), alongside a real-time spectral heart rate extractor.

### 1. Start the Smartwatch PPG LSL Streamer
This simulator models a dual-wavelength sensor (Green LED for HR, Red LED for SpO2) complete with respiratory sinus arrhythmia (RSA), heart rate variability (HRV), baseline drifts, and sensor noise. Adjust the simulated heart rate in real time using your keyboard.

Run:
```bash
uv run watch_ppg_streamer.py
```
*   **Controls in Terminal:**
    *   `w` or `[Arrow Up]`: Increase Heart Rate (+5 BPM)
    *   `s` or `[Arrow Down]`: Decrease Heart Rate (-5 BPM)
    *   `Space`: Randomize Heart Rate
    *   `q` or `[Ctrl+C]`: Stop simulator

### 2. Launch the Real-Time PPG Visualizer
This client connects to the LSL stream, applies a 4th-order Butterworth bandpass filter to remove breathing drift, performs an FFT on a rolling 10-second window, and overlays the calculated heart rate.

Run:
```bash
uv run watch_ppg_viewer.py
```

---

## 📐 Smartwatch 3D IMU Trajectory Tracker (No Hardware Required)

In addition to physiological pulse waves, smartwatches collect kinematic data using Inertial Measurement Units (IMU). This demo simulates 3D movements and uses real-time leaky double integration of accelerometer readings to trace the watch's movement path in 3D space.

### 1. Start the Smartwatch IMU LSL Streamer
Pipes simulated 6-axis IMU (3-axis Acc, 3-axis Gyro) values to the network. You can select different motion patterns interactively in the console.

Run:
```bash
uv run watch_imu_streamer.py
```
*   **Controls in Terminal:**
    *   `1`: Stationary (No movement + minor noise)
    *   `2`: Circular displacement (Translation in XY plane)
    *   `3`: Figure-8 (Lissajous trajectory)
    *   `4`: Shaking (Violent high-freq tremor)
    *   `q` or `[Ctrl+C]`: Stop simulator

### 2. Launch the 3D Trajectory Viewer
Connects to the IMU stream, subtracts gravity using a low-pass baseline filter, performs real-time double integration to compute position, and plots the trailing path in 3D.

Run:
```bash
uv run watch_imu_viewer_3d.py
```

---

## 🧠 Muse EEG & Optical PPG (Requires Hardware)

These scripts interface with the **Muse S (Athena)** headband using the BrainFlow library.

### 1. Start the Muse LSL Streamer
Launches the persistent BLE hardware session and pipes raw EEG (Ch. 1-4) and PPG Optics (Ch. 1-9) onto LSL nodes `Muse_Athena` and `Muse_Optics`.

Run:
```bash
uv run athena_streamer.py
```

### 2. View the Muse PPG Waveform
Extracts heart rate metrics from the Muse S optical PPG channels.

Run:
```bash
uv run ppg_viewer.py
```

### 3. Play the Python BCI Snake Game
A self-contained Pygame-based Snake game. It can be played using standard keys, but runs a background thread that binds to the live `Muse_Athena` EEG stream. It processes AF7/AF8 peaks to decode blinks (turn Left) and TP9/TP10 variance to decode jaw clenches (turn Right), displaying the live waveforms and threshold bars directly in the game UI.

Run:
```bash
uv run lsl_snake_game.py
```
*   **Controls & Simulators:**
    *   `W`/`A`/`S`/`D` or `Arrow Keys`: Manual overwrite steering.
    *   `B`: Simulate an eye blink (Left turn).
    *   `C`: Simulate a jaw clench (Right turn).
    *   `Space`: Start / Reset game.

---

## 🛠️ Codebase Structure

*   `presentation_sync_server.py`: Central WebSocket server to sync slides across Master, Phone Remotes, and Listener screens.
*   `lsl_snake_game.py`: Python BCI Snake game driven by LSL EEG (blinks/clenches) or simulator keyboard overlays.
*   `watch_ppg_streamer.py`: Simulates smartwatch dual-wavelength PPG over LSL with interactive keyboard tuning.
*   `watch_ppg_viewer.py`: Reads the simulated smartwatch PPG, filters out drift, and processes the signal via FFT.
*   `watch_imu_streamer.py`: Simulates smartwatch 6-axis IMU over LSL with interactive keyboard pattern switching.
*   `watch_imu_viewer_3d.py`: Connects to `Smartwatch_IMU` stream and plots the de-drifted integrated 3D trajectory path.
*   `athena_streamer.py`: Hardware driver that connects to the Muse S and creates LSL EEG and Optical streams.
*   `ppg_viewer.py`: Signal processor and viewer for the Muse S optical sensors.
*   `prediction_dashboard.py` / `advanced_dashboard.py`: Real-time state prediction dashboards (e.g. attention, blink detections).
*   `lsl_viewer.py`: Generic multi-channel LSL time-series data plotter.
