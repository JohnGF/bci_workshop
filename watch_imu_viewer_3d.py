# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pylsl",
#     "pyqtgraph",
#     "PyQt6",
#     "numpy",
#     "scipy",
#     "PyOpenGL"
# ]
# ///

import sys
import numpy as np
from pylsl import StreamInlet, resolve_byprop
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication, QMainWindow, QVBoxLayout, QWidget, QLabel

try:
    import pyqtgraph as pg
    import pyqtgraph.opengl as gl
    OPENGL_AVAILABLE = True
except ImportError as e:
    OPENGL_AVAILABLE = False
    import_error_msg = str(e)


class IMUTrajectoryViewer3D(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("⌚ Smartwatch 3D IMU Trajectory Tracker")
        self.resize(1000, 800)

        if not OPENGL_AVAILABLE:
            self.show_error_ui(f"❌ OpenGL Modules Missing.\nDetail: {import_error_msg}\n\nPlease install PyOpenGL:\n`pip install PyOpenGL` or run with `uv run watch_imu_viewer_3d.py`")
            return

        print("🔍 Searching for LSL smartwatch IMU stream (Smartwatch_IMU)...")
        streams = resolve_byprop("name", "Smartwatch_IMU", timeout=6)
        if not streams:
            print("❌ Smartwatch_IMU stream not found. Make sure watch_imu_streamer.py is running.")
            sys.exit(1)

        self.inlet = StreamInlet(streams[0])
        self.sfreq = int(self.inlet.info().nominal_srate())
        print(f"✅ Connected to stream! nominal sample rate: {self.sfreq} Hz")

        # --- DSP / Double Integration State variables ---
        self.dt = 1.0 / self.sfreq
        self.pos = np.zeros(3) # 3D position [x, y, z]
        self.vel = np.zeros(3) # 3D velocity [vx, vy, vz]
        
        # Gravity tracker (low pass filter of raw accelerometer signals)
        self.g_vec = np.array([0.0, 0.0, 9.81])
        
        # Trail memory (rolling buffer of 3D positions)
        self.max_trail_len = 350
        self.trail = []
        for _ in range(self.max_trail_len):
            self.trail.append(np.zeros(3))
            
        self.init_ui()

        # Update loop (run at ~60Hz)
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_trajectory)
        self.timer.start(16)

    def show_error_ui(self, msg):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)
        label = QLabel(msg)
        label.setStyleSheet("color: red; font-size: 16px; font-weight: bold; font-family: monospace;")
        layout.addWidget(label)

    def init_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)

        # 3D OpenGL View Widget
        self.view = gl.GLViewWidget()
        self.view.opts['distance'] = 12 # Camera zoom distance
        self.view.setWindowTitle('3D Trajectory')
        layout.addWidget(self.view)

        # Add 3D grid grids
        grid = gl.GLGridItem()
        grid.setSize(20, 20, 1)
        grid.setSpacing(1, 1, 1)
        self.view.addItem(grid)

        # Trajectory line plot item
        # Colors: light blue to dark blue gradient/tail
        self.line = gl.GLLinePlotItem(
            pos=np.array(self.trail),
            color=pg.glColor('#38bdf8'),
            width=3,
            antialias=True
        )
        self.view.addItem(self.line)

        # Add a coordinate axis guide (RGB = XYZ)
        axis = gl.GLAxisItem()
        axis.setSize(3, 3, 3)
        self.view.addItem(axis)

    def update_trajectory(self):
        # Pull any available chunks
        samples, _ = self.inlet.pull_chunk(max_samples=50)
        if not samples:
            return

        for sample in samples:
            # Parse Accelerometer (channels 0, 1, 2)
            acc = np.array(sample[0:3])
            
            # 1. Update running gravity estimation vector (Low-pass filter)
            # This adaptively tracks gravity direction relative to the watch sensor
            self.g_vec = 0.995 * self.g_vec + 0.005 * acc
            
            # Subtract gravity to get clean linear acceleration
            linear_acc = acc - self.g_vec
            
            # 2. Leaky integration for Velocity: v = (v + a*dt) * leak_factor
            # Leaky factor (0.97) acts as a high-pass filter to bleed off integration drifts
            self.vel = (self.vel + linear_acc * self.dt) * 0.97
            
            # 3. Leaky integration for Position: p = (p + v*dt) * leak_factor
            # Leaky factor (0.99) pulls the trajectory line back to the origin
            self.pos = (self.pos + self.vel * self.dt) * 0.99
            
            # Scale coordinates up for visualization space
            scaled_pos = self.pos * 18.0
            
            # Append to trail
            self.trail.pop(0)
            self.trail.append(scaled_pos)

        # Update 3D OpenGL line coordinates
        trail_array = np.array(self.trail)
        self.line.setData(pos=trail_array)


def main():
    app = QApplication(sys.argv)
    viewer = IMUTrajectoryViewer3D()
    viewer.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
