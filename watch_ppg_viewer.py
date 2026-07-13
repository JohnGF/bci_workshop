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
from scipy.signal import butter, sosfilt
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication, QMainWindow, QVBoxLayout, QWidget
from pylsl import StreamInlet, resolve_byprop


class SmartwatchPPGViewer(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("⌚ Smartwatch PPG & Heart Rate Monitor (FFT)")
        self.resize(1000, 800)

        print("🔍 Searching for LSL smartwatch stream (Smartwatch_PPG)...")
        streams = resolve_byprop("name", "Smartwatch_PPG", timeout=6)
        if not streams:
            print("❌ Smartwatch_PPG stream not found. Make sure watch_ppg_streamer.py is running.")
            sys.exit(1)

        self.inlet = StreamInlet(streams[0])
        self.sfreq = int(self.inlet.info().nominal_srate())
        print(f"✅ Connected to stream! Nominal sample rate: {self.sfreq} Hz")

        # Establish 10-second rolling buffer for high-resolution spectral peaks
        self.win_sec = 10
        self.win_samples = self.sfreq * self.win_sec
        self.buffer_green = np.zeros(self.win_samples)

        # 4th-order Butterworth bandpass (0.5Hz - 3.5Hz -> 30 to 210 BPM range) to remove breathing drift/high-freq noise
        self.sos = butter(4, [0.5, 3.5], btype="bandpass", fs=self.sfreq, output="sos")

        # --- Telemetry Smoothing Buffer ---
        self.bpm_history = []
        # Update every 50ms (20Hz loop rate)
        self.max_history_len = int(60 / 0.05) # 1 minute window

        self.init_ui()

        self.timer = QTimer()
        self.timer.timeout.connect(self.update_plot)
        self.timer.start(50)

    def init_ui(self):
        pg.setConfigOptions(antialias=True)
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)

        # Top Plot: Time Domain Waveform
        self.time_plot = pg.PlotWidget(title="Time Domain: Smartwatch Green LED (Filtered)")
        self.time_plot.showGrid(x=True, y=True, alpha=0.3)
        self.time_plot.setLabel("bottom", "Samples (Rolling 10-Sec Window)")
        layout.addWidget(self.time_plot)
        self.curve_green = self.time_plot.plot(pen=pg.mkPen("#10b981", width=2.5), name="Green LED PPG")

        # Bottom Plot: Spectral Spectrum
        self.fft_plot = pg.PlotWidget(title="Frequency Domain: Heart Rate Spectrum Peak")
        self.fft_plot.showGrid(x=True, y=True, alpha=0.3)
        self.fft_plot.setXRange(40, 180)
        self.fft_plot.setLabel("bottom", "Estimated Heart Rate (BPM)")
        self.fft_plot.setLabel("left", "Spectral Density Power")
        layout.addWidget(self.fft_plot)
        self.fft_curve = self.fft_plot.plot(
            pen=pg.mkPen("#38bdf8", width=2.5), name="PPG Power Spectrum"
        )

        # Large real-time overlay text
        self.bpm_text = pg.TextItem(
            text="Calculating Heart Rate...", color=(248, 250, 252), anchor=(0, 0)
        )
        self.bpm_text.setFont(pg.QtGui.QFont("Inter", 24, pg.QtGui.QFont.Weight.Bold))
        self.fft_plot.addItem(self.bpm_text)

    def update_plot(self):
        samples, _ = self.inlet.pull_chunk(max_samples=150)
        if not samples:
            return

        data = np.array(samples)
        num_new = len(data)

        # Shift rolling buffer and append new samples
        self.buffer_green[:-num_new] = self.buffer_green[num_new:]
        self.buffer_green[-num_new:] = data[:, 0]  # Channel 0: Green LED

        # 1. Update Time Domain Curve
        filtered_green = sosfilt(self.sos, self.buffer_green)
        
        # Display the latter half of the buffer for crisp real-time movement
        self.curve_green.setData(filtered_green[int(self.sfreq * 2):])

        # 2. Extract Spectral FFT
        window = np.hanning(self.win_samples)
        windowed_data = self.buffer_green * window

        # Calculate FFT padded to 2048 points for smooth interpolation
        n_fft = 2048
        fft_vals = np.fft.rfft(windowed_data, n=n_fft)
        psd = np.abs(fft_vals) ** 2
        freqs = np.fft.rfftfreq(n_fft, 1.0 / self.sfreq)

        # Translate Hz to BPM (cycles per minute)
        bpm_array = freqs * 60.0

        # Bound check (only evaluate humanly possible heart rates)
        valid_idx = (bpm_array >= 40.0) & (bpm_array <= 180.0)
        valid_bpm = bpm_array[valid_idx]
        valid_psd = psd[valid_idx]

        self.fft_curve.setData(valid_bpm, valid_psd)

        # 3. Peak Detection & Telemetry
        if len(valid_psd) > 0:
            max_idx = np.argmax(valid_psd)
            detected_bpm = valid_bpm[max_idx]

            self.bpm_history.append(detected_bpm)
            if len(self.bpm_history) > self.max_history_len:
                self.bpm_history.pop(0)

            # Robust 1-minute median smoothing
            median_bpm = np.median(self.bpm_history)

            # Position text relative to viewport
            view_range = self.fft_plot.viewRange()
            x_pos = view_range[0][0] + (view_range[0][1] - view_range[0][0]) * 0.05
            y_pos = view_range[1][1] * 0.82
            self.bpm_text.setPos(x_pos, y_pos)
            
            self.bpm_text.setText(
                f"Live Pulse: {int(detected_bpm)} BPM  |  1-Min Trend: {int(median_bpm)} BPM"
            )


def main():
    app = QApplication(sys.argv)
    viewer = SmartwatchPPGViewer()
    viewer.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
