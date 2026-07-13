# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pylsl",
#     "pyqtgraph",
#     "PyQt6",
#     "numpy",
#     "scipy"
# ]
# ///

import sys
import numpy as np
import pyqtgraph as pg
from scipy.signal import iirnotch, lfilter
from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtWidgets import (
    QApplication,
    QMainWindow,
    QVBoxLayout,
    QWidget,
    QHBoxLayout,
    QLabel,
)
from pylsl import StreamInlet, resolve_byprop


def compute_psd(data, sfreq):
    N = len(data)
    # Surgical 50Hz Notch Filter (Mains Hum)
    b, a = iirnotch(50.0, 30.0, sfreq)
    clean_data = lfilter(b, a, data)

    window = np.hanning(N)
    windowed = clean_data * window
    fft_vals = np.fft.rfft(windowed)
    psd = (np.abs(fft_vals) ** 2) / (sfreq * N)
    if len(psd) > 2:
        psd[1:-1] *= 2.0
    freqs = np.fft.rfftfreq(N, 1.0 / sfreq)
    return freqs, psd


def get_band_power(freqs, psd, fmin, fmax):
    idx = (freqs >= fmin) & (freqs <= fmax)
    if not np.any(idx):
        return 1e-12

    # NumPy 2.0+ compatibility: handle legacy trapz removal seamlessly
    if hasattr(np, "trapezoid"):
        return np.trapezoid(psd[idx], freqs[idx])
    return np.trapezoid(psd[idx], freqs[idx])


class PredictionDashboard(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("🔮 Single-Channel Hardened Prediction Engine")
        self.resize(1000, 600)

        print("🔍 Hooking LSL network stream (Muse_Athena)...")
        streams = resolve_byprop("name", "Muse_Athena", timeout=5)
        if not streams:
            print("❌ Stream not found. Run athena_streamer.py first.")
            sys.exit(1)

        self.inlet = StreamInlet(streams[0])
        self.sfreq = int(self.inlet.info().nominal_srate())

        # 2-second sliding matrix window
        self.win_samples = self.sfreq * 2
        self.data_buffer = np.zeros((self.win_samples, 4), dtype=np.float32)

        # Exponential Moving Average (EMA) smoothing variables
        self.smoothed_focus = 50.0
        self.smoothed_calm = 50.0
        self.ema_alpha = 0.04  # High-inertia smoothing to insulate from remaining noise

        self.init_ui()

        self.timer = QTimer()
        self.timer.timeout.connect(self.update_inference)
        self.timer.start(100)

    def init_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QHBoxLayout(central_widget)

        self.plot_widget = pg.PlotWidget(title="Single-Channel Robust Trends")
        self.plot_widget.addLegend()
        self.plot_widget.setYRange(0, 100)
        main_layout.addWidget(self.plot_widget, stretch=2)

        self.history_len = 100
        self.focus_history = np.ones(self.history_len) * 50.0
        self.calm_history = np.ones(self.history_len) * 50.0

        self.curve_focus = self.plot_widget.plot(
            pen=pg.mkPen("c", width=3), name="Smoothed Focus %"
        )
        self.curve_calm = self.plot_widget.plot(
            pen=pg.mkPen("m", width=3), name="Smoothed Calm %"
        )

        self.panel = QVBoxLayout()
        main_layout.addLayout(self.panel, stretch=1)

        self.state_label = QLabel("State: Running...")
        self.state_label.setStyleSheet(
            "font-size: 20px; font-weight: bold; color: #FFFFFF;"
        )
        self.panel.addWidget(self.state_label)

        self.suggest_label = QLabel("Initializing robust mode...")
        self.suggest_label.setStyleSheet("font-size: 14px; color: #A0A0A0;")
        self.suggest_label.setWordWrap(True)
        self.panel.addWidget(self.suggest_label)

    def update_inference(self):
        samples, _ = self.inlet.pull_chunk(max_samples=100)
        if not samples:
            return

        num_new = len(samples)
        self.data_buffer[:-num_new] = self.data_buffer[num_new:]
        self.data_buffer[-num_new:] = np.array(samples, dtype=np.float32)

        # --- Hardened Tactical Bypass ---
        # We slice ONLY index 2, which corresponds to AF8 (Right Forehead).
        # We completely ignore the railed channels (0, 1, 3) to prevent NaN math.
        af8_data = self.data_buffer[:, 2]

        # Compute single-channel Power Spectral Density
        freqs, psd = compute_psd(af8_data, self.sfreq)

        # Extract standard bands from AF8
        theta = get_band_power(freqs, psd, 4.0, 7.0)
        alpha = get_band_power(freqs, psd, 8.0, 12.0)
        beta = get_band_power(freqs, psd, 13.0, 30.0)

        # --- Robust Single-Electrode Metrics ---
        # 1. Focus: Beta/Alpha ratio on the right prefrontal cortex
        raw_focus = beta / (alpha + 1e-12)
        inst_focus = np.clip((raw_focus / 2.0) * 100, 0, 100)

        # 2. Calm/Relaxation: Alpha/Theta ratio (Standard single-channel relaxation marker)
        raw_calm = alpha / (theta + 1e-12)
        inst_calm = np.clip((raw_calm / 1.5) * 100, 0, 100)

        # Apply Exponential Moving Average (EMA) to smooth out local noise spikes
        self.smoothed_focus = (self.ema_alpha * inst_focus) + (
            (1.0 - self.ema_alpha) * self.smoothed_focus
        )
        self.smoothed_calm = (self.ema_alpha * inst_calm) + (
            (1.0 - self.ema_alpha) * self.smoothed_calm
        )

        # Update plotting data arrays
        self.focus_history[:-1] = self.focus_history[1:]
        self.focus_history[-1] = self.smoothed_focus

        self.calm_history[:-1] = self.calm_history[1:]
        self.calm_history[-1] = self.smoothed_calm

        self.curve_focus.setData(self.focus_history)
        self.curve_calm.setData(self.calm_history)

        # Final Classifier Translation
        mode_prefix = "🧠 [AF8 INSULATED] "

        if self.smoothed_focus > 55 and self.smoothed_calm < 45:
            self.state_label.setText(f"{mode_prefix}FOCUS STATE")
            self.state_label.setStyleSheet(
                "font-size: 20px; font-weight: bold; color: #00FFCC;"
            )
            self.suggest_label.setText(
                "Right prefrontal beta activation detected. High target cognitive engagement."
            )
        elif self.smoothed_calm > 55 and self.smoothed_focus < 45:
            self.state_label.setText(f"{mode_prefix}RELAXED STATE")
            self.state_label.setStyleSheet(
                "font-size: 20px; font-weight: bold; color: #99CCFF;"
            )
            self.suggest_label.setText(
                "Right prefrontal alpha synchronization active. Brain is in a resting baseline state."
            )
        else:
            self.state_label.setText(f"{mode_prefix}NEUTRAL COGNITION")
            self.state_label.setStyleSheet(
                "font-size: 20px; font-weight: bold; color: #FFFFFF;"
            )
            self.suggest_label.setText(
                "Stable single-electrode configuration tracking baseline activity smoothly."
            )


def main():
    app = QApplication(sys.argv)
    window = PredictionDashboard()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
