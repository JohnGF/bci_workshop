# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pylsl",
#     "pyqtgraph",
#     "PyQt6",
#     "numpy",
#     "numba",
#     "scipy"
# ]
# ///

import sys
import numpy as np
import pyqtgraph as pg
from numba import njit
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication, QMainWindow, QVBoxLayout, QWidget, QHBoxLayout
from pylsl import StreamInlet, resolve_byprop
from scipy.signal import iirnotch, lfilter

# ==========================================
# C-OPTIMIZED NUMBA DSP ENGINE
# Compiles directly to machine code on launch
# ==========================================


@njit(fastmath=True, nogil=True, cache=True)
def integrate_band_powers(freqs, psd, band_edges):
    """
    Offloads the slow Python for-loops to Numba C-level execution.
    """
    band_powers = np.zeros(len(band_edges) - 1, dtype=np.float32)
    df = freqs[1] - freqs[0]

    for i in range(len(band_edges) - 1):
        fmin = band_edges[i]
        fmax = band_edges[i + 1]
        bp_sum = 0.0

        for j in range(len(freqs)):
            if fmin <= freqs[j] <= fmax:
                bp_sum += psd[j] * df
        band_powers[i] = bp_sum

    return band_powers


def hybrid_dsp_pipeline(data_window, sfreq, band_edges):
    """
    Executes the FFT using NumPy 2.x native C-backend,
    then passes to Numba for loop integration.
    """
    N = len(data_window)

    # 1. Surgical 50Hz Notch Filter (European Mains Hum)
    notch_freq = 50.0
    quality_factor = 30.0
    b_notch, a_notch = iirnotch(notch_freq, quality_factor, sfreq)
    clean_data = lfilter(b_notch, a_notch, data_window)

    # 2. Generate Hanning window & apply it to the CLEAN data
    window = 0.5 - 0.5 * np.cos(2.0 * np.pi * np.arange(N) / (N - 1))
    windowed = clean_data * window

    # 3. Compute Real FFT (NumPy 2.x native, highly optimized)
    fft_vals = np.fft.rfft(windowed)

    # 4. Compute Power Spectral Density (PSD)
    psd = (np.abs(fft_vals) ** 2) / (sfreq * N)
    if len(psd) > 2:
        psd[1:-1] *= 2.0

    freqs = np.linspace(0.0, sfreq / 2.0, len(psd))

    # 5. Offload heavy integration loops to Numba
    band_powers = integrate_band_powers(freqs, psd, band_edges)

    return freqs, psd, band_powers


# ==========================================
# DASHBOARD APPLICATION
# ==========================================
class AdvancedEEGDashboard(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("⚡ C-Optimized Spectral Telemetry")
        self.resize(1200, 900)

        # 1. Connect to LSL Stream
        print("🔍 Hooking LSL network stream (Muse_Athena)...")
        streams = resolve_byprop("name", "Muse_Athena", timeout=5)
        if not streams:
            print("❌ Stream not found. Ensure athena_streamer.py is running.")
            sys.exit(1)

        self.inlet = StreamInlet(streams[0])
        self.sfreq = int(self.inlet.info().nominal_srate())
        self.ch_count = self.inlet.info().channel_count()

        # 2. Pre-allocate flat 32-bit buffers to prevent memory allocation in the loop
        self.win_sec = 4
        self.win_samples = self.sfreq * self.win_sec
        self.data_buffer = np.zeros((self.win_samples, self.ch_count), dtype=np.float32)

        # Flattened band edges for Numba compatibility: [1, 4, 8, 12, 30, 50]
        self.band_labels = ["Delta", "Theta", "Alpha", "Beta", "Gamma"]
        self.band_edges = np.array([1.0, 4.0, 8.0, 12.0, 30.0, 50.0], dtype=np.float32)

        self.init_ui()

        # 3. High-frequency UI loop (Update at 60 FPS instead of 30)
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_pipeline)
        self.timer.start(16)  # ~60 FPS
        print("🚀 Numba JIT compiled. Dashboard live at 60 FPS.")

    def init_ui(self):
        pg.setConfigOptions(antialias=False)  # Disable antialiasing for raw speed
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QHBoxLayout(central_widget)

        # Left Column: Raw Time-Series
        self.layout_left = pg.GraphicsLayoutWidget()
        main_layout.addWidget(self.layout_left, stretch=1)

        self.raw_curves = []
        ch_names = [
            "TP9 (Left Ear)",
            "AF7 (Left Front)",
            "AF8 (Right Front)",
            "TP10 (Right Ear)",
        ]

        for i in range(self.ch_count):
            p = self.layout_left.addPlot(row=i, col=0, title=ch_names[i])
            p.setYRange(-100, 100)
            # Use AutoDownsampling to prevent drawing pixels that overlap on the monitor
            curve = p.plot(
                pen=pg.mkPen(color=(100, 200, 255), width=1), autoDownsample=True
            )
            self.raw_curves.append(curve)

        # Right Column: Frequency Domain
        self.layout_right = pg.GraphicsLayoutWidget()
        main_layout.addWidget(self.layout_right, stretch=1)

        # FFT Plot
        self.fft_plot = self.layout_right.addPlot(
            row=0, col=0, title="Power Spectral Density (Numba JIT)"
        )
        self.fft_plot.setLogMode(x=False, y=True)
        self.fft_plot.setXRange(1, 50)
        self.fft_curve = self.fft_plot.plot(
            pen=pg.mkPen(color=(255, 100, 100), width=1.5), autoDownsample=True
        )

        # Bar Chart
        self.bar_plot = self.layout_right.addPlot(
            row=1, col=0, title="Absolute Band Powers"
        )
        self.bar_items = pg.BarGraphItem(
            x=range(len(self.band_labels)),
            height=np.zeros(len(self.band_labels)),
            width=0.6,
            brush="g",
        )
        self.bar_plot.addItem(self.bar_items)
        self.bar_plot.getAxis("bottom").setTicks([list(enumerate(self.band_labels))])

    def update_pipeline(self):
        # 1. Ingest Network Data
        samples, timestamps = self.inlet.pull_chunk(max_samples=200)
        if not samples:
            return

        num_new = len(samples)

        # 2. C-Level in-place memory shift (memmove) -> O(1) allocation
        self.data_buffer[:-num_new] = self.data_buffer[num_new:]
        self.data_buffer[-num_new:] = np.array(samples, dtype=np.float32)

        # 3. Update Time-Domain Plots (Decoupled drawing)
        x_axis = np.arange(self.win_samples) / self.sfreq
        for i in range(self.ch_count):
            centered_data = self.data_buffer[:, i] - np.mean(self.data_buffer[:, i])
            self.raw_curves[i].setData(x_axis, centered_data)

        # 4. Offload math to Numba compiled C-engine
        # Average TP9 (0) and TP10 (3) for auditory cortex tracking
        temporal_sig = (self.data_buffer[:, 0] + self.data_buffer[:, 3]) / 2.0

        freqs, psd, band_powers = hybrid_dsp_pipeline(
            temporal_sig, float(self.sfreq), self.band_edges
        )  # Filter display to 1-50Hz bounds natively via indexing for speed
        valid_idx = (freqs >= 1.0) & (freqs <= 50.0)

        # 5. Update UI
        self.fft_curve.setData(freqs[valid_idx], psd[valid_idx] + 1e-12)

        # Scale for bar chart visualization
        self.bar_items.setOpts(height=band_powers * 1e10)


def main():
    app = QApplication(sys.argv)
    dashboard = AdvancedEEGDashboard()
    dashboard.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
