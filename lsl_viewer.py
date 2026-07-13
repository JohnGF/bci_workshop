# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pylsl",
#     "pyqtgraph",
#     "PyQt6",
#     "numpy"
# ]
# ///

import sys
import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication, QMainWindow, QVBoxLayout, QWidget
from pylsl import StreamInlet, resolve_byprop


class EEGViewer(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("🧠 Live Laboratory EEG Monitor")
        self.resize(800, 600)

        # 1. Resolve and hook into our custom LSL stream
        print("🔍 Searching for Muse_Athena LSL stream on network...")
        streams = resolve_byprop("name", "Muse_Athena", timeout=5)
        if not streams:
            print("❌ Error: No active Muse_Athena stream found. Run the daemon first!")
            sys.exit(1)

        self.inlet = StreamInlet(streams[0])
        self.ch_count = self.inlet.info().channel_count()
        self.sfreq = int(self.inlet.info().nominal_srate())

        # 2. Setup a rolling data buffer (hold last 5 seconds of data)
        self.buffer_seconds = 5
        self.buffer_samples = self.sfreq * self.buffer_seconds
        self.data_buffer = np.zeros((self.buffer_samples, self.ch_count))

        # 3. Setup PyQtGraph Layout
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)

        self.win = pg.GraphicsLayoutWidget()
        layout.addWidget(self.win)

        # Create subplots for each channel
        self.plots = []
        self.curves = []
        for i in range(self.ch_count):
            p = self.win.addPlot(row=i, col=0)
            p.showAxis("bottom", show=False if i < self.ch_count - 1 else True)
            p.setLabel("left", f"Ch {i+1}")
            curve = p.plot(pen=pg.mkPen(color=pg.intColor(i), width=2))
            self.plots.append(p)
            self.curves.append(curve)

        # 4. Spin up a high-frequency UI thread timer loop (Refresh at ~60 FPS)
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_plot)
        self.timer.start(16)
        print("🚀 Visualizer initialized. Plotting real-time feeds.")

    def update_plot(self):
        # Pull all available chunks from the LSL network socket
        samples, timestamps = self.inlet.pull_chunk(max_samples=100)

        if samples:
            # Shift buffer left and append new incoming frames
            num_new_samples = len(samples)
            self.data_buffer = np.roll(self.data_buffer, -num_new_samples, axis=0)
            self.data_buffer[-num_new_samples:, :] = np.array(samples)

            # Update trace lines
            x_axis = np.arange(self.buffer_samples)
            for i in range(self.ch_count):
                # Apply channel-specific offset scaling for trace display isolation
                self.curves[i].setData(x_axis, self.data_buffer[:, i])


def main():
    app = QApplication(sys.argv)
    viewer = EEGViewer()
    viewer.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
