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


class PPGViewer(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("🫀 Spectral Heart Rate Monitor (FFT)")
        self.resize(1000, 800)

        print("🔍 Hooking LSL network stream (Muse_Optics)...")
        streams = resolve_byprop("name", "Muse_Optics", timeout=5)
        if not streams:
            print("❌ Stream not found. Ensure athena_streamer.py is running.")
            sys.exit(1)

        self.inlet = StreamInlet(streams[0])
        self.sfreq = int(self.inlet.info().nominal_srate())

        # Expand to a 10-second rolling window for high-resolution FFT
        self.win_sec = 10
        self.win_samples = self.sfreq * self.win_sec
        self.buffer_ir = np.zeros(self.win_samples)

        # 4th-order Butterworth bandpass (0.5Hz - 4.0Hz) for the visual time-series
        self.sos = butter(4, [0.5, 4.0], btype="bandpass", fs=self.sfreq, output="sos")

        # --- Running Statistics Buffer ---
        self.bpm_history = []
        # Timer updates every 50ms -> ~20 updates per second.
        # 60 seconds * 20 updates/sec = 1200 max elements for a 1-minute window
        self.max_history_len = int(60 / 0.05)

        self.init_ui()

        self.timer = QTimer()
        self.timer.timeout.connect(self.update_plot)
        self.timer.start(50)

    def init_ui(self):
        pg.setConfigOptions(antialias=True)
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)

        # Top Plot: Time Domain
        self.time_plot = pg.PlotWidget(title="Time Domain: Noisy Optical Pulse")
        self.time_plot.showGrid(x=True, y=True, alpha=0.3)
        layout.addWidget(self.time_plot)
        self.curve_ir = self.time_plot.plot(pen=pg.mkPen("c", width=2), name="Infrared")

        # Bottom Plot: Frequency Domain
        self.fft_plot = pg.PlotWidget(title="Frequency Domain: Heart Rate Spectrum")
        self.fft_plot.showGrid(x=True, y=True, alpha=0.3)
        self.fft_plot.setXRange(40, 180)
        self.fft_plot.setLabel("bottom", "Heart Rate (Beats Per Minute)")
        layout.addWidget(self.fft_plot)
        self.fft_curve = self.fft_plot.plot(
            pen=pg.mkPen("m", width=2.5), name="Power Density"
        )

        # Real-time Telemetry Text UI
        self.bpm_text = pg.TextItem(
            text="Live BPM: -- | 1-Min Trend: --", color=(0, 255, 0), anchor=(0, 0)
        )
        self.bpm_text.setFont(pg.QtGui.QFont("Arial", 22, pg.QtGui.QFont.Weight.Bold))
        self.fft_plot.addItem(self.bpm_text)

    def update_plot(self):
        samples, timestamps = self.inlet.pull_chunk(max_samples=100)
        if not samples:
            return

        data = np.array(samples)
        num_new = len(data)

        # Shift buffers
        self.buffer_ir[:-num_new] = self.buffer_ir[num_new:]
        self.buffer_ir[-num_new:] = data[:, 1]  # Infrared channel

        # 1. Update Time-Domain Visualization
        filt_ir = sosfilt(self.sos, self.buffer_ir)
        self.curve_ir.setData(filt_ir[int(self.sfreq) :])

        # --- DSP: FFT-Based Heart Rate Extraction ---
        window = np.hanning(self.win_samples)
        windowed_ir = self.buffer_ir * window

        fft_vals = np.fft.rfft(windowed_ir, n=2048)
        psd = np.abs(fft_vals) ** 2
        freqs = np.fft.rfftfreq(2048, 1.0 / self.sfreq)

        bpm_array = freqs * 60.0

        valid_idx = (bpm_array >= 40.0) & (bpm_array <= 180.0)
        valid_bpm = bpm_array[valid_idx]
        valid_psd = psd[valid_idx]

        self.fft_curve.setData(valid_bpm, valid_psd)

        # 2. Extract and smooth the dominant frequency peak
        if len(valid_psd) > 0:
            max_idx = np.argmax(valid_psd)
            calculated_bpm = valid_bpm[max_idx]

            # In-place memory shift for running metric array
            self.bpm_history.append(calculated_bpm)
            if len(self.bpm_history) > self.max_history_len:
                self.bpm_history.pop(0)

            # Extract the robust 1-minute median
            running_median = np.median(self.bpm_history)

            # Dynamic Text Placement
            view_range = self.fft_plot.viewRange()
            self.bpm_text.setPos(view_range[0][0] + 5, view_range[1][1] * 0.85)
            self.bpm_text.setText(
                f"Live BPM: {int(calculated_bpm)} | 1-Min Trend: {int(running_median)}"
            )


def main():
    app = QApplication(sys.argv)
    viewer = PPGViewer()
    viewer.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
