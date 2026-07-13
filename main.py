# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#     "pylsl",
#     "pyqtgraph",
#     "PyQt6",
#     "numpy",
#     "scipy",
#     "numba",
#     "pygame",
#     "PyOpenGL"
# ]
# ///

import sys
import os
import time
import numpy as np
import pyqtgraph as pg
from scipy.signal import butter, sosfilt, iirnotch, lfilter
from numba import njit

from PyQt6.QtCore import QTimer, QProcess, QThread, pyqtSignal, Qt, QSize
from PyQt6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QTabWidget,
    QVBoxLayout,
    QHBoxLayout,
    QGridLayout,
    QFrame,
    QLabel,
    QPushButton,
    QTextEdit,
    QComboBox,
    QCheckBox,
    QScrollArea,
    QMessageBox,
    QProgressBar,
    QGroupBox,
    QSlider
)
from PyQt6.QtGui import QFont, QColor, QTextCursor, QPainter
from pylsl import StreamInlet, resolve_byprop

# Check if OpenGL is available for the 3D IMU Trajectory Tracker
try:
    import pyqtgraph.opengl as gl
    OPENGL_AVAILABLE = True
except Exception as e:
    OPENGL_AVAILABLE = False
    opengl_import_error = str(e)


# ==========================================
# C-OPTIMIZED NUMBA DSP ENGINE FOR EEG
# ==========================================
@njit(fastmath=True, nogil=True, cache=True)
def integrate_band_powers(freqs, psd, band_edges):
    """Offloads the slow Python for-loops to Numba C-level execution."""
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
    """Executes FFT using NumPy native C-backend, then passes to Numba for integration."""
    N = len(data_window)

    # 1. 50Hz Notch Filter (Mains Hum)
    notch_freq = 50.0
    quality_factor = 30.0
    b_notch, a_notch = iirnotch(notch_freq, quality_factor, sfreq)
    clean_data = lfilter(b_notch, a_notch, data_window)

    # 2. Hanning window
    window = 0.5 - 0.5 * np.cos(2.0 * np.pi * np.arange(N) / (N - 1))
    windowed = clean_data * window

    # 3. Real FFT
    fft_vals = np.fft.rfft(windowed)

    # 4. Power Spectral Density (PSD)
    psd = (np.abs(fft_vals) ** 2) / (sfreq * N)
    if len(psd) > 2:
        psd[1:-1] *= 2.0

    freqs = np.linspace(0.0, sfreq / 2.0, len(psd))

    # 5. Numba JIT compiled band power integration
    band_powers = integrate_band_powers(freqs, psd, band_edges)

    return freqs, psd, band_powers


# ==========================================
# ASYNCHRONOUS LSL RESOLVER THREAD
# ==========================================
class LSLResolverThread(QThread):
    resolved = pyqtSignal(object)
    failed = pyqtSignal()

    def __init__(self, stream_name, timeout=3.0):
        super().__init__()
        self.stream_name = stream_name
        self.timeout = timeout

    def run(self):
        try:
            print(f"[Resolver] Searching for stream: {self.stream_name}...")
            streams = resolve_byprop("name", self.stream_name, timeout=self.timeout)
            if streams:
                print(f"[Resolver] Stream {self.stream_name} found!")
                self.resolved.emit(streams[0])
            else:
                print(f"[Resolver] Stream {self.stream_name} not found (timeout).")
                self.failed.emit()
        except Exception as e:
            print(f"[Resolver] Error searching for {self.stream_name}: {e}")
            self.failed.emit()


# ==========================================
# BASE CLASS FOR LSL TELEMETRY TABS
# ==========================================
class LSLTabBase(QWidget):
    def __init__(self, stream_name, tab_title, parent=None):
        super().__init__(parent)
        self.stream_name = stream_name
        self.tab_title = tab_title
        self.inlet = None
        self.resolver_thread = None
        
        self.is_paused = False
        self.is_recording = False
        self.recording_file = None
        self.samples_recorded = 0
        
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_data)
        
        self.init_base_ui()
        
        # Auto-start connection in the background on widget load
        QTimer.singleShot(500, self.start_connection)

    def init_base_ui(self):
        self.base_layout = QVBoxLayout(self)
        self.base_layout.setContentsMargins(12, 12, 12, 12)

        # Connection Card
        self.conn_card = QFrame()
        self.conn_card.setObjectName("ConnCard")
        self.conn_card.setStyleSheet("""
            QFrame#ConnCard {
                background-color: #1e293b;
                border: 1px solid #334155;
                border-radius: 12px;
            }
        """)
        conn_layout = QVBoxLayout(self.conn_card)
        conn_layout.setContentsMargins(40, 40, 40, 40)
        conn_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.title_lbl = QLabel(f"LSL Stream: {self.stream_name}")
        self.title_lbl.setStyleSheet("font-size: 20px; font-weight: bold; color: #38bdf8; margin-bottom: 8px;")
        conn_layout.addWidget(self.title_lbl, alignment=Qt.AlignmentFlag.AlignCenter)

        self.desc_lbl = QLabel("This visualization widget requires an active network stream.\nEnsure the corresponding streamer or simulator is running.")
        self.desc_lbl.setStyleSheet("color: #94a3b8; font-size: 14px; line-height: 1.4; margin-bottom: 24px;")
        self.desc_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        conn_layout.addWidget(self.desc_lbl, alignment=Qt.AlignmentFlag.AlignCenter)

        self.conn_btn = QPushButton("Connect to Stream")
        self.conn_btn.setMinimumSize(QSize(200, 42))
        self.conn_btn.setStyleSheet("""
            QPushButton {
                background-color: #0284c7;
                color: #ffffff;
                font-weight: bold;
                border-radius: 8px;
                font-size: 14px;
            }
            QPushButton:hover {
                background-color: #0369a1;
            }
            QPushButton:disabled {
                background-color: #334155;
                color: #64748b;
            }
        """)
        self.conn_btn.clicked.connect(self.start_connection)
        conn_layout.addWidget(self.conn_btn, alignment=Qt.AlignmentFlag.AlignCenter)

        self.base_layout.addWidget(self.conn_card, alignment=Qt.AlignmentFlag.AlignCenter)

        # Actual visualization layout container (hidden by default)
        self.viz_container = QWidget()
        self.viz_layout = QVBoxLayout(self.viz_container)
        self.viz_layout.setContentsMargins(0, 0, 0, 0)
        self.viz_layout.setSpacing(8)

        # Data Flow Control Toolbar
        self.flow_toolbar = QFrame()
        self.flow_toolbar.setStyleSheet("background-color: #1e293b; border-radius: 8px; border: 1px solid #334155;")
        self.flow_toolbar.setFixedHeight(48)
        toolbar_layout = QHBoxLayout(self.flow_toolbar)
        toolbar_layout.setContentsMargins(12, 0, 12, 0)
        toolbar_layout.setSpacing(12)
        
        # 1. Pause/Resume button
        self.btn_pause = QPushButton("⏸️ Pause Plot")
        self.btn_pause.setCheckable(True)
        self.btn_pause.setStyleSheet("""
            QPushButton {
                background-color: #334155;
                color: #f1f5f9;
                border: 1px solid #475569;
                border-radius: 6px;
                font-weight: bold;
                padding: 6px 12px;
            }
            QPushButton:checked {
                background-color: #fbbf24;
                color: #0f172a;
            }
        """)
        self.btn_pause.toggled.connect(self.toggle_pause)
        toolbar_layout.addWidget(self.btn_pause)
        
        # 2. Record button
        self.btn_record = QPushButton("🔴 Start Recording")
        self.btn_record.setCheckable(True)
        self.btn_record.setStyleSheet("""
            QPushButton {
                background-color: #334155;
                color: #f1f5f9;
                border: 1px solid #475569;
                border-radius: 6px;
                font-weight: bold;
                padding: 6px 12px;
            }
            QPushButton:checked {
                background-color: #ef4444;
                color: white;
                border-color: #ef4444;
            }
        """)
        self.btn_record.toggled.connect(self.toggle_recording)
        toolbar_layout.addWidget(self.btn_record)
        
        # 3. Recording status label
        self.lbl_record_status = QLabel("")
        self.lbl_record_status.setStyleSheet("color: #94a3b8; font-size: 12px; font-weight: bold;")
        toolbar_layout.addWidget(self.lbl_record_status)
        
        toolbar_layout.addStretch(1)
        
        # 4. Disconnect button
        self.btn_disconnect = QPushButton("🔌 Disconnect")
        self.btn_disconnect.setStyleSheet("""
            QPushButton {
                background-color: #ef4444;
                color: white;
                border-radius: 6px;
                font-weight: bold;
                padding: 6px 12px;
                border: none;
            }
            QPushButton:hover {
                background-color: #dc2626;
            }
        """)
        self.btn_disconnect.clicked.connect(self.stop_feed)
        toolbar_layout.addWidget(self.btn_disconnect)
        
        self.viz_layout.addWidget(self.flow_toolbar)
        self.viz_container.hide()
        self.base_layout.addWidget(self.viz_container)

    def start_connection(self):
        self.conn_btn.setEnabled(False)
        self.desc_lbl.setText("Searching LSL network nodes for target stream...")
        self.desc_lbl.setStyleSheet("color: #fbbf24; font-size: 14px;") # Warning gold

        self.resolver_thread = LSLResolverThread(self.stream_name, timeout=3.0)
        self.resolver_thread.resolved.connect(self.on_resolved)
        self.resolver_thread.failed.connect(self.on_failed)
        self.resolver_thread.start()

    def on_resolved(self, stream_info):
        try:
            self.inlet = StreamInlet(stream_info)
            self.conn_card.hide()
            self.viz_container.show()
            self.init_visualizer()
            self.timer.start(self.get_timer_interval())
        except Exception as e:
            QMessageBox.critical(self, "Connection Error", f"Failed to open LSL inlet: {e}")
            self.on_failed()

    def on_failed(self):
        self.conn_btn.setEnabled(True)
        self.desc_lbl.setText("Failed to resolve stream. Please make sure the streamer is started\nin the Control Room and try again.")
        self.desc_lbl.setStyleSheet("color: #f87171; font-size: 14px;") # Error red

    def get_timer_interval(self):
        return 16  # Default is ~60 FPS

    def init_visualizer(self):
        pass

    def update_data(self):
        pass

    def toggle_pause(self, checked):
        self.is_paused = checked
        if checked:
            self.btn_pause.setText("▶️ Resume Plot")
        else:
            self.btn_pause.setText("⏸️ Pause Plot")

    def toggle_recording(self, checked):
        from datetime import datetime
        if checked:
            os.makedirs("recordings", exist_ok=True)
            info = self.inlet.info()
            ch_count = info.channel_count()
            labels = []
            ch = info.desc().child("channels").child("channel")
            for i in range(ch_count):
                if not ch.empty():
                    labels.append(ch.child_value("label"))
                    ch = ch.next_sibling()
                else:
                    labels.append(f"Ch{i+1}")
            
            filename = f"recordings/{self.stream_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            self.recording_file = open(filename, "w", encoding="utf-8")
            self.recording_file.write("Timestamp," + ",".join(labels) + "\n")
            self.samples_recorded = 0
            self.is_recording = True
            self.btn_record.setText("⏹️ Stop Recording")
            self.lbl_record_status.setText(f"Recording to {os.path.basename(filename)}... 0 samples")
        else:
            self.is_recording = False
            if self.recording_file:
                self.recording_file.close()
                self.recording_file = None
            self.btn_record.setText("🔴 Start Recording")
            self.lbl_record_status.setText("Recording saved!")
            QTimer.singleShot(3000, lambda: self.lbl_record_status.setText("") if not self.is_recording else None)

    def pull_and_record_samples(self, max_samples=50):
        if not self.inlet:
            return []
        samples, timestamps = self.inlet.pull_chunk(max_samples=max_samples)
        if not samples:
            return []
        if self.is_recording and self.recording_file:
            for sample, ts in zip(samples, timestamps):
                row = f"{ts}," + ",".join(map(str, sample)) + "\n"
                self.recording_file.write(row)
            self.samples_recorded += len(samples)
            self.lbl_record_status.setText(f"🔴 Recording: {self.samples_recorded} samples saved")
        return samples

    def stop_feed(self):
        self.timer.stop()
        if self.is_recording:
            self.btn_record.setChecked(False)
        self.btn_pause.setChecked(False)
        self.inlet = None
        self.viz_container.hide()
        self.conn_card.show()
        self.desc_lbl.setText("Disconnected. Start the stream and click Connect.")
        self.desc_lbl.setStyleSheet("color: #94a3b8; font-size: 14px;")
        self.conn_btn.setEnabled(True)


# ==========================================
# 1. EEG BRAINWAVES & PREDICTIONS TELEMETRY
# ==========================================
class EEGVisualizerWidget(LSLTabBase):
    def __init__(self, parent=None):
        super().__init__("Muse_Athena", "EEG Brainwaves", parent)

    def init_visualizer(self):
        self.sfreq = int(self.inlet.info().nominal_srate())
        self.ch_count = self.inlet.info().channel_count()

        # 4-second sliding buffer
        self.win_sec = 4
        self.win_samples = self.sfreq * self.win_sec
        self.data_buffer = np.zeros((self.win_samples, self.ch_count), dtype=np.float32)

        # DSP variables
        self.band_labels = ["Delta", "Theta", "Alpha", "Beta", "Gamma"]
        self.band_edges = np.array([1.0, 4.0, 8.0, 12.0, 30.0, 50.0], dtype=np.float32)

        # Focus / Calm history (prediction dashboard)
        self.smoothed_focus = 50.0
        self.smoothed_calm = 50.0
        self.ema_alpha = 0.04
        self.history_len = 100
        self.focus_history = np.ones(self.history_len) * 50.0
        self.calm_history = np.ones(self.history_len) * 50.0

        # Build UI layout
        main_widget = QWidget()
        layout = QHBoxLayout(main_widget)
        layout.setContentsMargins(0, 0, 0, 0)
        self.viz_layout.addWidget(main_widget)

        # Left Column: Raw Channel Traces
        left_layout_widget = pg.GraphicsLayoutWidget()
        left_layout_widget.setStyleSheet("border-radius: 8px; background-color: #0f172a;")
        layout.addWidget(left_layout_widget, stretch=1)

        self.raw_curves = []
        ch_names = ["TP9 (L-Ear)", "AF7 (L-Front)", "AF8 (R-Front)", "TP10 (R-Ear)"]
        for i in range(self.ch_count):
            p = left_layout_widget.addPlot(row=i, col=0, title=ch_names[i] if i < len(ch_names) else f"Ch {i+1}")
            p.setYRange(-100, 100)
            p.showGrid(x=True, y=True, alpha=0.15)
            p.getAxis("bottom").setStyle(showValues=False)
            curve = p.plot(pen=pg.mkPen(color=QColor("#38bdf8"), width=1), autoDownsample=True)
            self.raw_curves.append(curve)

        # Right Column: DSP & Predictions
        right_panel = QVBoxLayout()
        layout.addLayout(right_panel, stretch=1)

        # Real-time State Banner Card
        self.state_card = QFrame()
        self.state_card.setStyleSheet("background-color: #1e293b; border-radius: 8px; border: 1px solid #334155; padding: 10px;")
        state_layout = QVBoxLayout(self.state_card)
        self.state_title_lbl = QLabel("🧠 NEUTRAL COGNITION")
        self.state_title_lbl.setStyleSheet("font-size: 16px; font-weight: bold; color: #ffffff;")
        self.state_desc_lbl = QLabel("Baseline tracking initialized smoothly.")
        self.state_desc_lbl.setStyleSheet("font-size: 12px; color: #94a3b8;")
        state_layout.addWidget(self.state_title_lbl)
        state_layout.addWidget(self.state_desc_lbl)
        right_panel.addWidget(self.state_card)

        # Power Spectral Density Plot (FFT)
        self.fft_plot_widget = pg.PlotWidget(title="Power Spectral Density (Numba JIT)")
        self.fft_plot_widget.setLogMode(x=False, y=True)
        self.fft_plot_widget.setXRange(1, 50)
        self.fft_plot_widget.setYRange(-4, 4)
        self.fft_plot_widget.showGrid(x=True, y=True, alpha=0.2)
        self.fft_curve = self.fft_plot_widget.plot(pen=pg.mkPen(color=QColor("#f43f5e"), width=1.5), autoDownsample=True)
        right_panel.addWidget(self.fft_plot_widget, stretch=1)

        # Band Powers Bar Plot
        self.bar_plot_widget = pg.PlotWidget(title="Absolute Band Powers")
        self.bar_plot_widget.showGrid(x=False, y=True, alpha=0.2)
        self.bar_items = pg.BarGraphItem(
            x=range(len(self.band_labels)),
            height=np.zeros(len(self.band_labels)),
            width=0.6,
            brush=pg.mkBrush("#10b981"),
            pen=pg.mkPen("#059669")
        )
        self.bar_plot_widget.addItem(self.bar_items)
        self.bar_plot_widget.getAxis("bottom").setTicks([list(enumerate(self.band_labels))])
        self.bar_plot_widget.setYRange(0, 5)
        right_panel.addWidget(self.bar_plot_widget, stretch=1)

        # Focus / Calm trends Plot
        self.trend_plot_widget = pg.PlotWidget(title="Real-Time State Predictions (AF8 Insulated)")
        self.trend_plot_widget.addLegend()
        self.trend_plot_widget.setYRange(0, 100)
        self.trend_plot_widget.showGrid(x=True, y=True, alpha=0.2)
        self.curve_focus = self.trend_plot_widget.plot(pen=pg.mkPen(color=QColor("#06b6d4"), width=2.5), name="Focus %")
        self.curve_calm = self.trend_plot_widget.plot(pen=pg.mkPen(color=QColor("#d946ef"), width=2.5), name="Calm %")
        right_panel.addWidget(self.trend_plot_widget, stretch=1)

    def update_data(self):
        samples = self.pull_and_record_samples(max_samples=200)
        if not samples:
            return

        if self.is_paused:
            return

        # Get thresholds from mapping tab dynamically
        main_win = self.window()
        blink_thresh = 45.0
        clench_thresh = 35.0
        focus_thresh = 60.0
        calm_thresh = 60.0
        
        if hasattr(main_win, "mapping_tab") and main_win.mapping_tab:
            blink_thresh = float(main_win.mapping_tab.blink_thresh_slider.value())
            clench_thresh = float(main_win.mapping_tab.clench_thresh_slider.value())
            focus_thresh = float(main_win.mapping_tab.focus_thresh_slider.value())
            calm_thresh = float(main_win.mapping_tab.calm_thresh_slider.value())

        num_new = len(samples)
        if num_new >= self.win_samples:
            # Overwrite entirely if chunk is bigger than buffer
            self.data_buffer[:, :] = np.array(samples[-self.win_samples:], dtype=np.float32)
        else:
            self.data_buffer[:-num_new] = self.data_buffer[num_new:]
            self.data_buffer[-num_new:] = np.array(samples, dtype=np.float32)

        # Update Time-Domain Curves
        x_axis = np.arange(self.win_samples) / self.sfreq
        for i in range(self.ch_count):
            centered_data = self.data_buffer[:, i] - np.mean(self.data_buffer[:, i])
            # Cap extreme spikes for visual clean rendering
            centered_data = np.clip(centered_data, -150, 150)
            self.raw_curves[i].setData(x_axis, centered_data)

        # Process DSP metrics on temporal cortex (Average of TP9 [0] and TP10 [3])
        # Or AF8 (channel 2) for focus prediction
        temporal_sig = (self.data_buffer[:, 0] + self.data_buffer[:, 3]) / 2.0
        freqs, psd, band_powers = hybrid_dsp_pipeline(temporal_sig, float(self.sfreq), self.band_edges)

        valid_idx = (freqs >= 1.0) & (freqs <= 50.0)
        self.fft_curve.setData(freqs[valid_idx], psd[valid_idx] + 1e-12)

        # Scale and update bar chart
        self.bar_items.setOpts(height=band_powers * 1e9)

        # Update Focus & Calm predictions using insulated AF8 channel (Index 2)
        af8_data = self.data_buffer[:, 2]
        af8_freqs, af8_psd = self.compute_single_channel_psd(af8_data, self.sfreq)
        
        theta = self.get_band_power(af8_freqs, af8_psd, 4.0, 7.0)
        alpha = self.get_band_power(af8_freqs, af8_psd, 8.0, 12.0)
        beta = self.get_band_power(af8_freqs, af8_psd, 13.0, 30.0)

        # Ratio computations
        raw_focus = beta / (alpha + 1e-12)
        inst_focus = np.clip((raw_focus / 2.0) * 100, 0, 100)

        raw_calm = alpha / (theta + 1e-12)
        inst_calm = np.clip((raw_calm / 1.5) * 100, 0, 100)

        # Smoothing
        self.smoothed_focus = (self.ema_alpha * inst_focus) + ((1.0 - self.ema_alpha) * self.smoothed_focus)
        self.smoothed_calm = (self.ema_alpha * inst_calm) + ((1.0 - self.ema_alpha) * self.smoothed_calm)

        # Append to rolling histories
        self.focus_history[:-1] = self.focus_history[1:]
        self.focus_history[-1] = self.smoothed_focus

        self.calm_history[:-1] = self.calm_history[1:]
        self.calm_history[-1] = self.smoothed_calm

        self.curve_focus.setData(self.focus_history)
        self.curve_calm.setData(self.calm_history)

        # Update BCI State Banner using dynamic focus/calm thresholds
        if self.smoothed_focus > focus_thresh and self.smoothed_calm < calm_thresh - 10.0:
            self.state_title_lbl.setText("🧠 ACTIVE FOCUS STATE")
            self.state_title_lbl.setStyleSheet("font-size: 16px; font-weight: bold; color: #06b6d4;")
            self.state_desc_lbl.setText("Right prefrontal beta activation active. High cognitive engagement.")
        elif self.smoothed_calm > calm_thresh and self.smoothed_focus < focus_thresh - 10.0:
            self.state_title_lbl.setText("🧘 DEEP CALM / RELAXED")
            self.state_title_lbl.setStyleSheet("font-size: 16px; font-weight: bold; color: #d946ef;")
            self.state_desc_lbl.setText("Right prefrontal alpha synchronization active. Restful baseline state.")
        else:
            self.state_title_lbl.setText("🧠 NEUTRAL COGNITION")
            self.state_title_lbl.setStyleSheet("font-size: 16px; font-weight: bold; color: #ffffff;")
            self.state_desc_lbl.setText("Stable single-electrode configuration tracking baseline smoothly.")

        # Blink & Clench checks for Snake Game redirect
        c3_clean = self.data_buffer[:, 1] - np.mean(self.data_buffer[:, 1])
        tp9_clean = self.data_buffer[:, 0] - np.mean(self.data_buffer[:, 0])
        
        window_blink = int(self.sfreq * 0.3)
        b_val = float(np.max(c3_clean[-window_blink:]) - np.min(c3_clean[-window_blink:]))
        c_val = float(np.std(tp9_clean))
        
        now = time.time()
        if not hasattr(self, "last_trigger_time"):
            self.last_trigger_time = 0.0
            
        if now - self.last_trigger_time > 0.8:
            main_win = self.window()
            if hasattr(main_win, "get_trigger_action") and hasattr(main_win, "send_command_to_snake_game"):
                if c_val > clench_thresh:  # Clench trigger
                    cmd = main_win.get_trigger_action("Muse Jaw Clench")
                    if cmd:
                        self.last_trigger_time = now
                        main_win.send_command_to_snake_game(cmd)
                elif b_val > blink_thresh:  # Blink trigger
                    cmd = main_win.get_trigger_action("Muse Eye Blink")
                    if cmd:
                        self.last_trigger_time = now
                        main_win.send_command_to_snake_game(cmd)

        # Focus & Calm trigger redirects using dynamic thresholds
        if not hasattr(self, "triggered_focus"): self.triggered_focus = False
        if not hasattr(self, "triggered_calm"): self.triggered_calm = False
        
        main_win = self.window()
        if hasattr(main_win, "get_trigger_action") and hasattr(main_win, "send_command_to_snake_game"):
            # High Focus
            if self.smoothed_focus > focus_thresh:
                if not self.triggered_focus:
                    cmd = main_win.get_trigger_action("Muse High Focus")
                    if cmd:
                        main_win.send_command_to_snake_game(cmd)
                    self.triggered_focus = True
            elif self.smoothed_focus < focus_thresh - 10.0:
                self.triggered_focus = False
                
            # Deep Calm
            if self.smoothed_calm > calm_thresh:
                if not self.triggered_calm:
                    cmd = main_win.get_trigger_action("Muse Deep Calm")
                    if cmd:
                        main_win.send_command_to_snake_game(cmd)
                    self.triggered_calm = True
            elif self.smoothed_calm < calm_thresh - 10.0:
                self.triggered_calm = False

    def compute_single_channel_psd(self, data, sfreq):
        N = len(data)
        # Notch filter
        b, a = iirnotch(50.0, 30.0, sfreq)
        clean_data = lfilter(b, a, data)
        # Window & FFT
        window = np.hanning(N)
        windowed = clean_data * window
        fft_vals = np.fft.rfft(windowed)
        psd = (np.abs(fft_vals) ** 2) / (sfreq * N)
        if len(psd) > 2:
            psd[1:-1] *= 2.0
        freqs = np.fft.rfftfreq(N, 1.0 / sfreq)
        return freqs, psd

    def get_band_power(self, freqs, psd, fmin, fmax):
        idx = (freqs >= fmin) & (freqs <= fmax)
        if not np.any(idx):
            return 1e-12
        if hasattr(np, "trapezoid"):
            return np.trapezoid(psd[idx], freqs[idx])
        return np.trapz(psd[idx], freqs[idx])


# ==========================================
# 2. SMARTWATCH PPG TELEMETRY
# ==========================================
class SmartwatchPPGWidget(LSLTabBase):
    def __init__(self, parent=None):
        super().__init__("Smartwatch_PPG", "PPG Watch Monitor", parent)

    def init_visualizer(self):
        self.sfreq = int(self.inlet.info().nominal_srate())
        self.win_sec = 10
        self.win_samples = self.sfreq * self.win_sec
        self.buffer_green = np.zeros(self.win_samples)

        # 4th-order Butterworth bandpass (0.5Hz - 3.5Hz -> 30 to 210 BPM range)
        self.sos = butter(4, [0.5, 3.5], btype="bandpass", fs=self.sfreq, output="sos")

        self.bpm_history = []
        self.max_history_len = int(60 / 0.05)  # 1 minute of telemetry at 20Hz update rate

        # Main Layout
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        self.viz_layout.addLayout(layout)

        # Time-domain green wave
        self.time_plot = pg.PlotWidget(title="PPG Time-Series: Green LED Signal (Butterworth Bandpass)")
        self.time_plot.showGrid(x=True, y=True, alpha=0.15)
        self.curve_green = self.time_plot.plot(pen=pg.mkPen("#10b981", width=2.5))
        layout.addWidget(self.time_plot, stretch=1)

        # Frequency-domain spectral peak
        self.fft_plot = pg.PlotWidget(title="PPG Frequency-Series: Cardiac Power Spectrum")
        self.fft_plot.showGrid(x=True, y=True, alpha=0.15)
        self.fft_plot.setXRange(40, 180)
        self.fft_curve = self.fft_plot.plot(pen=pg.mkPen("#38bdf8", width=2))
        layout.addWidget(self.fft_plot, stretch=1)

        # Large status and Pulse Overlay Card
        self.pulse_card = QFrame()
        self.pulse_card.setStyleSheet("background-color: #1e293b; border-radius: 8px; border: 1px solid #334155; padding: 15px;")
        pulse_layout = QHBoxLayout(self.pulse_card)
        
        self.pulse_icon = QLabel("💓")
        self.pulse_icon.setStyleSheet("font-size: 32px;")
        self.pulse_val_lbl = QLabel("Live Pulse: -- BPM")
        self.pulse_val_lbl.setStyleSheet("font-size: 24px; font-weight: bold; color: #f43f5e;")
        self.pulse_trend_lbl = QLabel("1-Min Trend: -- BPM")
        self.pulse_trend_lbl.setStyleSheet("font-size: 16px; color: #94a3b8;")

        pulse_layout.addWidget(self.pulse_icon)
        pulse_layout.addWidget(self.pulse_val_lbl, stretch=1)
        pulse_layout.addWidget(self.pulse_trend_lbl)
        layout.addWidget(self.pulse_card)

    def get_timer_interval(self):
        return 50  # 20 Hz update rate (50ms interval) is plenty for PPG

    def update_data(self):
        samples = self.pull_and_record_samples(max_samples=150)
        if not samples:
            return

        if self.is_paused:
            return

        data = np.array(samples)
        num_new = len(data)

        # Append to rolling buffer
        self.buffer_green[:-num_new] = self.buffer_green[num_new:]
        self.buffer_green[-num_new:] = data[:, 0]  # Channel 0: Green LED

        # Filter time-series and plot the latter half for scrolling rendering
        filtered_green = sosfilt(self.sos, self.buffer_green)
        self.curve_green.setData(filtered_green[int(self.sfreq * 2):])

        # Compute power spectrum via FFT
        window = np.hanning(self.win_samples)
        windowed_data = self.buffer_green * window
        n_fft = 2048
        fft_vals = np.fft.rfft(windowed_data, n=n_fft)
        psd = np.abs(fft_vals) ** 2
        freqs = np.fft.rfftfreq(n_fft, 1.0 / self.sfreq)

        bpm_array = freqs * 60.0
        valid_idx = (bpm_array >= 40.0) & (bpm_array <= 180.0)
        valid_bpm = bpm_array[valid_idx]
        valid_psd = psd[valid_idx]

        self.fft_curve.setData(valid_bpm, valid_psd)

        # Peak detection & smooth digital readout
        if len(valid_psd) > 0:
            max_idx = np.argmax(valid_psd)
            detected_bpm = valid_bpm[max_idx]

            self.bpm_history.append(detected_bpm)
            if len(self.bpm_history) > self.max_history_len:
                self.bpm_history.pop(0)

            median_bpm = np.median(self.bpm_history)
            self.pulse_val_lbl.setText(f"Live Pulse: {int(detected_bpm)} BPM")
            self.pulse_trend_lbl.setText(f"1-Min Trend: {int(median_bpm)} BPM")


# ==========================================
# 3. 3D IMU TRAJECTORY MONITOR
class IMUTrajectory3DWidget(LSLTabBase):
    def __init__(self, parent=None):
        self.opengl_missing = not OPENGL_AVAILABLE
        
        # Initialize orientation/shake variables
        self.pitch = 0.0
        self.roll = 0.0
        self.yaw = 0.0
        self.current_shake_force = 0.0
        self.shake_detected_counter = 0
        
        # Calibration baseline offsets
        self.roll_offset = 0.0
        self.pitch_offset = 0.0
        self.raw_roll = 0.0
        self.raw_pitch = 0.0
        
        super().__init__("Smartwatch_IMU", "3D IMU Trajectory", parent)

    def init_base_ui(self):
        # Override connection/initialization behavior if OpenGL packages are missing
        if self.opengl_missing:
            self.base_layout = QVBoxLayout(self)
            self.base_layout.setContentsMargins(12, 12, 12, 12)
            
            error_card = QFrame()
            error_card.setStyleSheet("background-color: #1e293b; border-radius: 12px; border: 1px solid #ef4444; padding: 30px;")
            layout = QVBoxLayout(error_card)
            layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            
            icon = QLabel("⚠️")
            icon.setStyleSheet("font-size: 40px;")
            layout.addWidget(icon, alignment=Qt.AlignmentFlag.AlignCenter)
            
            title = QLabel("PyOpenGL Dependency Missing")
            title.setStyleSheet("font-size: 20px; font-weight: bold; color: #f87171; margin-top: 10px; margin-bottom: 8px;")
            layout.addWidget(title, alignment=Qt.AlignmentFlag.AlignCenter)
            
            desc = QLabel("The 3D Trajectory Viewer requires PyOpenGL to render hardware graphics.\n"
                          "Press the button below to automatically install it via uv in the project environment.")
            desc.setStyleSheet("color: #94a3b8; font-size: 14px; line-height: 1.4; margin-bottom: 24px;")
            desc.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(desc, alignment=Qt.AlignmentFlag.AlignCenter)
            
            self.install_btn = QPushButton("Install PyOpenGL via uv")
            self.install_btn.setMinimumSize(QSize(220, 42))
            self.install_btn.setStyleSheet("""
                QPushButton {
                    background-color: #ef4444;
                    color: white;
                    font-weight: bold;
                    border-radius: 8px;
                    font-size: 14px;
                }
                QPushButton:hover {
                    background-color: #dc2626;
                }
            """)
            self.install_btn.clicked.connect(self.install_pyopengl)
            layout.addWidget(self.install_btn, alignment=Qt.AlignmentFlag.AlignCenter)

            self.progress_bar = QProgressBar()
            self.progress_bar.setVisible(False)
            self.progress_bar.setStyleSheet("""
                QProgressBar {
                    background-color: #334155;
                    border: 1px solid #475569;
                    border-radius: 6px;
                    text-align: center;
                    color: white;
                }
                QProgressBar::chunk {
                    background-color: #ef4444;
                    border-radius: 5px;
                }
            """)
            layout.addWidget(self.progress_bar)

            self.base_layout.addWidget(error_card, alignment=Qt.AlignmentFlag.AlignCenter)
        else:
            super().init_base_ui()

    def install_pyopengl(self):
        self.install_btn.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0) # Infinite spinner
        
        self.install_proc = QProcess()
        self.install_proc.finished.connect(self.on_install_finished)
        self.install_proc.start("uv", ["pip", "install", "pyopengl"])

    def on_install_finished(self, exit_code, exit_status):
        self.progress_bar.setVisible(False)
        if exit_code == 0:
            QMessageBox.information(self, "Success", "PyOpenGL installed successfully! Please restart the application to enable 3D rendering.")
            self.install_btn.setText("Restart App to Load")
            self.install_btn.setStyleSheet("background-color: #10b981; color: white;")
        else:
            self.install_btn.setEnabled(True)
            self.install_btn.setText("Installation Failed. Retry?")
            QMessageBox.critical(self, "Installation Failed", "Could not install PyOpenGL. Check logs or run 'uv pip install pyopengl' manually.")

    def init_visualizer(self):
        if self.opengl_missing:
            return

        self.sfreq = int(self.inlet.info().nominal_srate())
        self.dt = 1.0 / self.sfreq
        
        # Leaky double integration variables
        self.pos = np.zeros(3)
        self.vel = np.zeros(3)
        self.g_vec = np.array([0.0, 0.0, 9.81])  # gravity estimator baseline
        
        self.max_trail_len = 350
        self.trail = [np.zeros(3) for _ in range(self.max_trail_len)]

        # Set up horizontal split: Left panel for controls/metrics, Right panel for 3D trajectory GL view
        main_h_layout = QHBoxLayout()
        main_h_layout.setContentsMargins(0, 0, 0, 0)
        main_h_layout.setSpacing(12)
        self.viz_layout.addLayout(main_h_layout)

        # 1. LEFT CONTROL PANEL (Width: ~250px)
        ctrl_panel = QFrame()
        ctrl_panel.setFixedWidth(250)
        ctrl_panel.setStyleSheet("background-color: #1e293b; border-radius: 8px; border: 1px solid #334155;")
        ctrl_layout = QVBoxLayout(ctrl_panel)
        ctrl_layout.setContentsMargins(12, 12, 12, 12)
        ctrl_layout.setSpacing(10)
        
        # Title
        lbl_title = QLabel("📐 IMU Controller Center")
        lbl_title.setStyleSheet("font-size: 14px; font-weight: bold; color: #38bdf8; border: none;")
        ctrl_layout.addWidget(lbl_title)

        # 1.1 Live Orientation metrics group
        metrics_group = QFrame()
        metrics_group.setStyleSheet("background-color: #0b0f19; border-radius: 6px; border: none;")
        m_layout = QVBoxLayout(metrics_group)
        m_layout.setContentsMargins(8, 8, 8, 8)
        m_layout.setSpacing(6)
        
        lbl_o_title = QLabel("LIVE ORIENTATION")
        lbl_o_title.setStyleSheet("font-size: 10px; font-weight: bold; color: #64748b;")
        m_layout.addWidget(lbl_o_title)
        
        self.roll_lbl = QLabel("Roll (Tilt): 0.0°")
        self.roll_lbl.setStyleSheet("font-family: monospace; font-size: 12px; color: #38bdf8;")
        m_layout.addWidget(self.roll_lbl)
        
        self.pitch_lbl = QLabel("Pitch: 0.0°")
        self.pitch_lbl.setStyleSheet("font-family: monospace; font-size: 12px; color: #818cf8;")
        m_layout.addWidget(self.pitch_lbl)
        
        self.yaw_lbl = QLabel("Yaw: 0.0°")
        self.yaw_lbl.setStyleSheet("font-family: monospace; font-size: 12px; color: #f43f5e;")
        m_layout.addWidget(self.yaw_lbl)
        
        ctrl_layout.addWidget(metrics_group)

        # Calibration / Zero button
        self.calib_btn = QPushButton("🎯 Calibrate (Zero IMU)")
        self.calib_btn.setStyleSheet("""
            QPushButton {
                background-color: #3b82f6;
                color: white;
                font-weight: bold;
                border-radius: 6px;
                padding: 8px;
                border: none;
            }
            QPushButton:hover {
                background-color: #2563eb;
            }
        """)
        self.calib_btn.clicked.connect(self.calibrate_imu_sensors)
        ctrl_layout.addWidget(self.calib_btn)

        # 1.2 Shake detection group
        shake_group = QFrame()
        shake_group.setStyleSheet("background-color: #0b0f19; border-radius: 6px; border: none;")
        s_layout = QVBoxLayout(shake_group)
        s_layout.setContentsMargins(8, 8, 8, 8)
        s_layout.setSpacing(6)
        
        lbl_s_title = QLabel("SHAKE DETECTOR")
        lbl_s_title.setStyleSheet("font-size: 10px; font-weight: bold; color: #64748b;")
        s_layout.addWidget(lbl_s_title)
        
        # Shake progress bar
        self.shake_bar = QProgressBar()
        self.shake_bar.setRange(0, 100)
        self.shake_bar.setValue(0)
        self.shake_bar.setTextVisible(False)
        self.shake_bar.setFixedHeight(8)
        self.shake_bar.setStyleSheet("""
            QProgressBar { background-color: #1e293b; border-radius: 4px; }
            QProgressBar::chunk { background-color: #f43f5e; border-radius: 4px; }
        """)
        s_layout.addWidget(self.shake_bar)
        
        # Shake indicator box
        self.shake_indicator = QLabel("STABLE")
        self.shake_indicator.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.shake_indicator.setStyleSheet("background-color: #1e293b; color: #64748b; font-weight: bold; border-radius: 4px; padding: 8px;")
        s_layout.addWidget(self.shake_indicator)
        
        # Shake sensitivity slider
        lbl_sens = QLabel("Shake Threshold:")
        lbl_sens.setStyleSheet("font-size: 11px; color: #94a3b8;")
        s_layout.addWidget(lbl_sens)
        
        from PyQt6.QtWidgets import QSlider
        self.shake_threshold_slider = QSlider(Qt.Orientation.Horizontal)
        self.shake_threshold_slider.setRange(30, 150) # translates to 3.0 to 15.0 m/s^2
        self.shake_threshold_slider.setValue(80) # default 8.0 m/s^2
        self.shake_threshold_slider.valueChanged.connect(self.sync_shake_threshold)
        s_layout.addWidget(self.shake_threshold_slider)
        
        ctrl_layout.addWidget(shake_group)

        # 1.3 Axis Mapping Controls
        map_group = QFrame()
        map_group.setStyleSheet("background-color: #0b0f19; border-radius: 6px; border: none;")
        map_layout = QVBoxLayout(map_group)
        map_layout.setContentsMargins(8, 8, 8, 8)
        map_layout.setSpacing(6)
        
        lbl_m_title = QLabel("AXIS MAPPING & INVERT")
        lbl_m_title.setStyleSheet("font-size: 10px; font-weight: bold; color: #64748b;")
        map_layout.addWidget(lbl_m_title)
        
        self.swap_xy_checkbox = QCheckBox("Swap X / Y axes")
        self.swap_xy_checkbox.setStyleSheet("font-size: 11px; color: #cbd5e1;")
        map_layout.addWidget(self.swap_xy_checkbox)
        
        self.invert_x_checkbox = QCheckBox("Invert X Axis")
        self.invert_x_checkbox.setStyleSheet("font-size: 11px; color: #cbd5e1;")
        map_layout.addWidget(self.invert_x_checkbox)
        
        self.invert_y_checkbox = QCheckBox("Invert Y Axis")
        self.invert_y_checkbox.setStyleSheet("font-size: 11px; color: #cbd5e1;")
        map_layout.addWidget(self.invert_y_checkbox)
        
        self.invert_z_checkbox = QCheckBox("Invert Z Axis")
        self.invert_z_checkbox.setStyleSheet("font-size: 11px; color: #cbd5e1;")
        map_layout.addWidget(self.invert_z_checkbox)
        
        ctrl_layout.addWidget(map_group)
        ctrl_layout.addStretch(1)
        
        main_h_layout.addWidget(ctrl_panel)

        # 2. RIGHT VIEW PANEL (3D Viewer)
        viz_panel = QFrame()
        viz_panel.setStyleSheet("background-color: #1e293b; border-radius: 8px; border: 1px solid #334155;")
        viz_layout = QVBoxLayout(viz_panel)
        viz_layout.setContentsMargins(12, 12, 12, 12)
        viz_layout.setSpacing(10)
        
        # 3D View Widget
        self.gl_view = gl.GLViewWidget()
        self.gl_view.opts['distance'] = 12
        self.gl_view.setStyleSheet("border-radius: 6px;")
        viz_layout.addWidget(self.gl_view, stretch=1)

        # Grid
        grid = gl.GLGridItem()
        grid.setSize(20, 20, 1)
        grid.setSpacing(1, 1, 1)
        self.gl_view.addItem(grid)

        # Trajectory 3D path line
        self.path_line = gl.GLLinePlotItem(
            pos=np.array(self.trail),
            color=pg.glColor('#38bdf8'),
            width=3,
            antialias=True
        )
        self.gl_view.addItem(self.path_line)

        # Axes guide (R=X, G=Y, B=Z)
        axis = gl.GLAxisItem()
        axis.setSize(3, 3, 3)
        self.gl_view.addItem(axis)

        # Instructions / reset HUD overlay
        reset_btn = QPushButton("Reset Trajectory Path")
        reset_btn.setStyleSheet("""
            QPushButton {
                background-color: #334155;
                color: #ffffff;
                font-weight: bold;
                padding: 8px 16px;
                border: 1px solid #475569;
                border-radius: 6px;
            }
            QPushButton:hover {
                background-color: #475569;
            }
        """)
        reset_btn.clicked.connect(self.reset_trajectory)
        viz_layout.addWidget(reset_btn, alignment=Qt.AlignmentFlag.AlignCenter)
        
        main_h_layout.addWidget(viz_panel, stretch=1)

    def reset_trajectory(self):
        self.pos = np.zeros(3)
        self.vel = np.zeros(3)
        self.trail = [np.zeros(3) for _ in range(self.max_trail_len)]
        if not self.opengl_missing:
            self.path_line.setData(pos=np.array(self.trail))

    def calibrate_imu_sensors(self):
        self.roll_offset = self.raw_roll
        self.pitch_offset = self.raw_pitch
        main_win = self.window()
        if hasattr(main_win, "append_system_log"):
            main_win.append_system_log(f"IMU Calibrated! Offsets: Roll: {self.roll_offset:.1f}°, Pitch: {self.pitch_offset:.1f}°")

    def sync_shake_threshold(self, val):
        main_win = self.window()
        if hasattr(main_win, "mapping_tab") and main_win.mapping_tab:
            main_win.mapping_tab.shake_thresh_slider.blockSignals(True)
            main_win.mapping_tab.shake_thresh_slider.setValue(val)
            main_win.mapping_tab.shake_thresh_slider.blockSignals(False)

    def update_data(self):
        # Skip early return so LSL stream is pulled even if PyOpenGL is missing
        pass

        samples = self.pull_and_record_samples(max_samples=50)
        if not samples:
            return

        if self.is_paused:
            return

        # Get thresholds from mapping tab dynamically
        main_win = self.window()
        tilt_thresh = 15.0
        flick_thresh = 0.35
        shake_thresh = 8.0
        
        if hasattr(main_win, "mapping_tab") and main_win.mapping_tab:
            tilt_thresh = float(main_win.mapping_tab.tilt_thresh_slider.value())
            flick_thresh = float(main_win.mapping_tab.flick_thresh_slider.value() / 100.0)
            shake_thresh = float(main_win.mapping_tab.shake_thresh_slider.value() / 10.0)

        for sample in samples:
            # Acceleration readings are the first 3 channels (XYZ)
            acc = np.array(sample[0:3])
            gyro = np.array(sample[3:6]) if len(sample) >= 6 else np.zeros(3)

            # Apply mapping checkboxes configurations
            if self.swap_xy_checkbox.isChecked():
                acc[0], acc[1] = acc[1], acc[0]
                gyro[0], gyro[1] = gyro[1], gyro[0]
            if self.invert_x_checkbox.isChecked():
                acc[0] = -acc[0]
                gyro[0] = -gyro[0]
            if self.invert_y_checkbox.isChecked():
                acc[1] = -acc[1]
                gyro[1] = -gyro[1]
            if self.invert_z_checkbox.isChecked():
                acc[2] = -acc[2]
                gyro[2] = -gyro[2]

            # 1. Update running gravity estimation vector (Low-pass filter)
            self.g_vec = 0.995 * self.g_vec + 0.005 * acc

            # 2. Subtract gravity component to isolate linear acceleration
            linear_acc = acc - self.g_vec

            # 3. Leaky velocity integration: v = (v + a*dt) * leak
            # 0.97 prevents integration baseline walk drift
            self.vel = (self.vel + linear_acc * self.dt) * 0.97

            # 4. Leaky position integration: p = (p + v*dt) * leak
            # 0.99 pulls position slowly back to origin over time
            self.pos = (self.pos + self.vel * self.dt) * 0.99

            scaled_pos = self.pos * 18.0  # Scale up for grid visuality

            self.trail.pop(0)
            self.trail.append(scaled_pos)

            # --- CALCULATE ORIENTATION ---
            ax, ay, az = acc[0], acc[1], acc[2]
            
            # Pitch (rotation around X, using Y and Z)
            denom_pitch = np.sqrt(ay**2 + az**2)
            pitch_rad = np.arctan2(-ax, denom_pitch) if denom_pitch > 1e-4 else 0.0
            self.raw_pitch = pitch_rad * 180.0 / np.pi
            self.pitch = self.raw_pitch - self.pitch_offset
            
            # Roll (rotation around Y, using X and Z)
            roll_rad = np.arctan2(ay, az) if np.abs(az) > 1e-4 else 0.0
            self.raw_roll = roll_rad * 180.0 / np.pi
            self.roll = self.raw_roll - self.roll_offset
            
            # Yaw (Integrate gyro Z)
            self.yaw += gyro[2] * self.dt * 180.0 / np.pi
            self.yaw = (self.yaw + 180) % 360 - 180

            # --- SHAKE DETECTION ---
            acc_mag = np.sqrt(ax**2 + ay**2 + az**2)
            shake_force = np.abs(acc_mag - 9.81)
            
            # Smooth shake level
            self.current_shake_force = 0.9 * self.current_shake_force + 0.1 * shake_force
            
            # Shake trigger using dynamic shake threshold
            if shake_force > shake_thresh:
                self.shake_detected_counter = 15

        # Update OpenGL coordinates
        self.path_line.setData(pos=np.array(self.trail))

        # Update UI labels
        self.roll_lbl.setText(f"Roll (Tilt): {self.roll:.1f}°")
        self.pitch_lbl.setText(f"Pitch: {self.pitch:.1f}°")
        self.yaw_lbl.setText(f"Yaw: {self.yaw:.1f}°")
        
        # Update shake level bar (max bar limit is 15.0 m/s^2)
        bar_percent = min(100, int((self.current_shake_force / 15.0) * 100))
        self.shake_bar.setValue(bar_percent)
        
        # Update shake indicator box
        if self.shake_detected_counter > 0:
            self.shake_detected_counter -= 1
            self.shake_indicator.setText("🔥 SHAKE DETECTED! 🔥")
            self.shake_indicator.setStyleSheet("background-color: #ef4444; color: white; font-weight: bold; border-radius: 4px; padding: 8px; text-align: center; border: none;")
        else:
            self.shake_indicator.setText("STABLE")
            self.shake_indicator.setStyleSheet("background-color: #1e293b; color: #64748b; font-weight: bold; border-radius: 4px; padding: 8px; text-align: center; border: none;")

        # Check Orientation, Flicks, Taps, Voice and Shake triggers
        if len(samples) > 0:
            last_sample = samples[-1]
            last_gyro = np.array(last_sample[3:6]) if len(last_sample) >= 6 else np.zeros(3)
            main_win = self.window()
            
            if hasattr(main_win, "get_trigger_action") and hasattr(main_win, "send_command_to_snake_game"):
                # 1. Screen Tap (Scan all samples in batch to avoid missing transient spikes)
                tap_detected = False
                for sample in samples:
                    if len(sample) >= 7 and sample[6] > 0.5:
                        tap_detected = True
                        break
                if tap_detected:
                    cmd = main_win.get_trigger_action("Phone Screen Tap")
                    if cmd:
                        main_win.send_command_to_snake_game(cmd)

                # 2. Voice Command (Scan all samples in batch)
                voice_cmd = None
                for sample in samples:
                    if len(sample) >= 8:
                        voice_val = sample[7]
                        if voice_val > 90.0:
                            voice_cmd = "left"
                            break
                        elif voice_val < -90.0:
                            voice_cmd = "right"
                            break
                
                if voice_cmd == "left":
                    cmd = main_win.get_trigger_action("Voice Command 'Left'")
                    if cmd:
                        main_win.send_command_to_snake_game(cmd)
                elif voice_cmd == "right":
                    cmd = main_win.get_trigger_action("Voice Command 'Right'")
                    if cmd:
                        main_win.send_command_to_snake_game(cmd)
                else:
                    # Fallback on gyro Z spike (third-party logger apps)
                    for sample in samples:
                        g = np.array(sample[3:6]) if len(sample) >= 6 else np.zeros(3)
                        if g[2] > 90.0:
                            voice_cmd = "left"
                            break
                        elif g[2] < -90.0:
                            voice_cmd = "right"
                            break
                    if voice_cmd == "left":
                        cmd = main_win.get_trigger_action("Voice Command 'Left'")
                        if cmd:
                            main_win.send_command_to_snake_game(cmd)
                    elif voice_cmd == "right":
                        cmd = main_win.get_trigger_action("Voice Command 'Right'")
                        if cmd:
                            main_win.send_command_to_snake_game(cmd)

                # 3. Phone Tilts (Roll and Pitch) - Unlatched for smooth continuous steering!
                # Roll (Left / Right) using dynamic tilt threshold
                if self.roll < -tilt_thresh:
                    cmd = main_win.get_trigger_action("Phone Tilt Left")
                    if cmd:
                        main_win.send_command_to_snake_game(cmd)
                elif self.roll > tilt_thresh:
                    cmd = main_win.get_trigger_action("Phone Tilt Right")
                    if cmd:
                        main_win.send_command_to_snake_game(cmd)

                # Pitch (Forward / Backward)
                if self.pitch > tilt_thresh:
                    cmd = main_win.get_trigger_action("Phone Tilt Forward")
                    if cmd:
                        main_win.send_command_to_snake_game(cmd)
                elif self.pitch < -tilt_thresh:
                    cmd = main_win.get_trigger_action("Phone Tilt Backward")
                    if cmd:
                        main_win.send_command_to_snake_game(cmd)

                # 4. Flicks / XYZ Position Displacement using dynamic flick threshold
                if not hasattr(self, "flicked_left"): self.flicked_left = False
                if not hasattr(self, "flicked_right"): self.flicked_right = False
                if not hasattr(self, "flicked_up"): self.flicked_up = False

                # X Axis (Left / Right Flicks)
                if self.pos[0] < -flick_thresh:
                    if not self.flicked_left:
                        cmd = main_win.get_trigger_action("Phone Flick Left")
                        if cmd:
                            main_win.send_command_to_snake_game(cmd)
                        self.flicked_left = True
                elif self.pos[0] > flick_thresh:
                    if not self.flicked_right:
                        cmd = main_win.get_trigger_action("Phone Flick Right")
                        if cmd:
                            main_win.send_command_to_snake_game(cmd)
                        self.flicked_right = True
                elif -flick_thresh * 0.4 <= self.pos[0] <= flick_thresh * 0.4:
                    self.flicked_left = False
                    self.flicked_right = False

                # Y Axis (Forward / Up / Down Flicks)
                if not hasattr(self, "flicked_down"): self.flicked_down = False
                if self.pos[1] > flick_thresh:
                    if not self.flicked_up:
                        cmd = main_win.get_trigger_action("Phone Flick Up")
                        if cmd:
                            main_win.send_command_to_snake_game(cmd)
                        self.flicked_up = True
                elif self.pos[1] < -flick_thresh:
                    if not self.flicked_down:
                        cmd = main_win.get_trigger_action("Phone Flick Down")
                        if cmd:
                            main_win.send_command_to_snake_game(cmd)
                        self.flicked_down = True
                elif -flick_thresh * 0.4 <= self.pos[1] <= flick_thresh * 0.4:
                    self.flicked_up = False
                    self.flicked_down = False

                # 5. Shake detection (Scan all samples in batch for max force) using dynamic shake threshold
                if not hasattr(self, "shook_device"):
                    self.shook_device = False
                    
                max_shake_force = 0.0
                for sample in samples:
                    acc = np.array(sample[0:3])
                    acc_mag = np.sqrt(np.sum(acc**2))
                    shake_f = np.abs(acc_mag - 9.81)
                    if shake_f > max_shake_force:
                        max_shake_force = shake_f

                if max_shake_force > shake_thresh:
                    if not self.shook_device:
                        cmd = main_win.get_trigger_action("Phone Shake")
                        if cmd:
                            main_win.send_command_to_snake_game(cmd)
                        self.shook_device = True
                else:
                    self.shook_device = False


# ==========================================
# 4. MUSE S HARDWARE PPG MONITOR
# ==========================================
class MusePPGWidget(LSLTabBase):
    def __init__(self, parent=None):
        super().__init__("Muse_Optics", "Muse PPG Monitor", parent)

    def init_visualizer(self):
        self.sfreq = int(self.inlet.info().nominal_srate())
        self.win_sec = 10
        self.win_samples = self.sfreq * self.win_sec
        self.buffer_ir = np.zeros(self.win_samples)

        # 4th-order Butterworth bandpass filter
        self.sos = butter(4, [0.5, 4.0], btype="bandpass", fs=self.sfreq, output="sos")

        self.bpm_history = []
        self.max_history_len = int(60 / 0.05)

        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        self.viz_layout.addLayout(layout)

        self.time_plot = pg.PlotWidget(title="Muse Optical Infrared Waveform (Bandpass Filtered)")
        self.time_plot.showGrid(x=True, y=True, alpha=0.15)
        self.curve_ir = self.time_plot.plot(pen=pg.mkPen("#c084fc", width=2.5))  # Purple
        layout.addWidget(self.time_plot, stretch=1)

        self.fft_plot = pg.PlotWidget(title="Muse Power Spectral Pulse Peak")
        self.fft_plot.showGrid(x=True, y=True, alpha=0.15)
        self.fft_plot.setXRange(40, 180)
        self.fft_curve = self.fft_plot.plot(pen=pg.mkPen("#e879f9", width=2))
        layout.addWidget(self.fft_plot, stretch=1)

        self.pulse_card = QFrame()
        self.pulse_card.setStyleSheet("background-color: #1e293b; border-radius: 8px; border: 1px solid #334155; padding: 15px;")
        pulse_layout = QHBoxLayout(self.pulse_card)

        self.pulse_icon = QLabel("🫀")
        self.pulse_icon.setStyleSheet("font-size: 32px;")
        self.pulse_val_lbl = QLabel("Live Pulse: -- BPM")
        self.pulse_val_lbl.setStyleSheet("font-size: 24px; font-weight: bold; color: #a855f7;")
        self.pulse_trend_lbl = QLabel("1-Min Trend: -- BPM")
        self.pulse_trend_lbl.setStyleSheet("font-size: 16px; color: #94a3b8;")

        pulse_layout.addWidget(self.pulse_icon)
        pulse_layout.addWidget(self.pulse_val_lbl, stretch=1)
        pulse_layout.addWidget(self.pulse_trend_lbl)
        layout.addWidget(self.pulse_card)

    def get_timer_interval(self):
        return 50

    def update_data(self):
        samples = self.pull_and_record_samples(max_samples=100)
        if not samples:
            return

        if self.is_paused:
            return

        data = np.array(samples)
        num_new = len(data)

        # Shift buffers - Muse Optics channel 1 is typically Infrared
        self.buffer_ir[:-num_new] = self.buffer_ir[num_new:]
        self.buffer_ir[-num_new:] = data[:, 1]

        # Update Time-Domain Visualization
        filt_ir = sosfilt(self.sos, self.buffer_ir)
        self.curve_ir.setData(filt_ir[int(self.sfreq):])

        # Power spectrum peak matching
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

        if len(valid_psd) > 0:
            max_idx = np.argmax(valid_psd)
            calculated_bpm = valid_bpm[max_idx]

            self.bpm_history.append(calculated_bpm)
            if len(self.bpm_history) > self.max_history_len:
                self.bpm_history.pop(0)

            running_median = np.median(self.bpm_history)
            self.pulse_val_lbl.setText(f"Live Pulse: {int(calculated_bpm)} BPM")
            self.pulse_trend_lbl.setText(f"1-Min Trend: {int(running_median)} BPM")


def get_all_local_ips():
    ips = [("All Interfaces (0.0.0.0)", "0.0.0.0")]
    try:
        import socket
        hostname = socket.gethostname()
        infos = socket.getaddrinfo(hostname, None)
        seen = set()
        for info in infos:
            ip = info[4][0]
            if "." in ip and not ip.startswith("127.") and ip not in seen:
                ips.append((f"Interface IP: {ip}", ip))
                seen.add(ip)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(('8.8.8.8', 1))
            primary_ip = s.getsockname()[0]
            if primary_ip not in seen and not primary_ip.startswith("127."):
                ips.append((f"Primary IP: {primary_ip}", primary_ip))
        except Exception:
            pass
        finally:
            s.close()
    except Exception:
        pass
    return ips


class BCIControllerMappingWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.init_ui()

    def init_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(16)

        # Left Column: Config Panel
        left_panel = QFrame()
        left_panel.setStyleSheet("background-color: #1e293b; border-radius: 12px; border: 1px solid #334155;")
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(20, 20, 20, 20)
        left_layout.setSpacing(12)

        lbl_title = QLabel("🎮 BCI Controller Mapping Configuration")
        lbl_title.setStyleSheet("font-size: 18px; font-weight: bold; color: #38bdf8; border: none;")
        left_layout.addWidget(lbl_title)

        lbl_desc = QLabel("Map physiological inputs (Muse EEG) and phone sensor actions to game controller actions.")
        lbl_desc.setStyleSheet("font-size: 12px; color: #94a3b8; border: none;")
        lbl_desc.setWordWrap(True)
        left_layout.addWidget(lbl_desc)

        left_layout.addSpacing(10)

        # Grid for mappings
        grid = QGridLayout()
        grid.setSpacing(12)

        # Options list
        self.options = [
            "Muse Eye Blink",
            "Muse Jaw Clench",
            "Muse High Focus",
            "Muse Deep Calm",
            "Phone Tilt Left",
            "Phone Tilt Right",
            "Phone Tilt Forward",
            "Phone Tilt Backward",
            "Phone Flick Left",
            "Phone Flick Right",
            "Phone Flick Up",
            "Phone Flick Down",
            "Phone Screen Tap",
            "Phone Shake",
            "Voice Command 'Left'",
            "Voice Command 'Right'"
        ]

        lbl_left = QLabel("LEFT Command:")
        lbl_left.setStyleSheet("color: #cbd5e1; font-size: 13px; border: none; font-weight: bold;")
        grid.addWidget(lbl_left, 0, 0)

        self.map_left_cb = QComboBox()
        self.map_left_cb.addItems(self.options)
        self.map_left_cb.setCurrentIndex(4)  # Phone Tilt Left
        self.map_left_cb.setStyleSheet("background-color: #0f172a; color: #cbd5e1; border: 1px solid #475569; padding: 4px; border-radius: 4px; font-size: 12px;")
        grid.addWidget(self.map_left_cb, 0, 1)

        lbl_right = QLabel("RIGHT Command:")
        lbl_right.setStyleSheet("color: #cbd5e1; font-size: 13px; border: none; font-weight: bold;")
        grid.addWidget(lbl_right, 1, 0)

        self.map_right_cb = QComboBox()
        self.map_right_cb.addItems(self.options)
        self.map_right_cb.setCurrentIndex(5)  # Phone Tilt Right
        self.map_right_cb.setStyleSheet("background-color: #0f172a; color: #cbd5e1; border: 1px solid #475569; padding: 4px; border-radius: 4px; font-size: 12px;")
        grid.addWidget(self.map_right_cb, 1, 1)

        lbl_up = QLabel("UP Command:")
        lbl_up.setStyleSheet("color: #cbd5e1; font-size: 13px; border: none; font-weight: bold;")
        grid.addWidget(lbl_up, 2, 0)

        self.map_up_cb = QComboBox()
        self.map_up_cb.addItems(self.options)
        self.map_up_cb.setCurrentIndex(6)  # Phone Tilt Forward
        self.map_up_cb.setStyleSheet("background-color: #0f172a; color: #cbd5e1; border: 1px solid #475569; padding: 4px; border-radius: 4px; font-size: 12px;")
        grid.addWidget(self.map_up_cb, 2, 1)

        lbl_down = QLabel("DOWN Command:")
        lbl_down.setStyleSheet("color: #cbd5e1; font-size: 13px; border: none; font-weight: bold;")
        grid.addWidget(lbl_down, 3, 0)

        self.map_down_cb = QComboBox()
        self.map_down_cb.addItems(self.options)
        self.map_down_cb.setCurrentIndex(7)  # Phone Tilt Backward
        self.map_down_cb.setStyleSheet("background-color: #0f172a; color: #cbd5e1; border: 1px solid #475569; padding: 4px; border-radius: 4px; font-size: 12px;")
        grid.addWidget(self.map_down_cb, 3, 1)

        lbl_action = QLabel("Primary Action (Pause/Fire):")
        lbl_action.setStyleSheet("color: #cbd5e1; font-size: 13px; border: none; font-weight: bold;")
        grid.addWidget(lbl_action, 4, 0)

        self.map_action_cb = QComboBox()
        self.map_action_cb.addItems(self.options)
        self.map_action_cb.setCurrentIndex(12)  # Phone Screen Tap
        self.map_action_cb.setStyleSheet("background-color: #0f172a; color: #cbd5e1; border: 1px solid #475569; padding: 4px; border-radius: 4px; font-size: 12px;")
        grid.addWidget(self.map_action_cb, 4, 1)

        lbl_sec = QLabel("Secondary Action (Extra/Spread):")
        lbl_sec.setStyleSheet("color: #cbd5e1; font-size: 13px; border: none; font-weight: bold;")
        grid.addWidget(lbl_sec, 5, 0)

        self.map_sec_cb = QComboBox()
        self.map_sec_cb.addItems(self.options)
        self.map_sec_cb.setCurrentIndex(13)  # Phone Shake
        self.map_sec_cb.setStyleSheet("background-color: #0f172a; color: #cbd5e1; border: 1px solid #475569; padding: 4px; border-radius: 4px; font-size: 12px;")
        grid.addWidget(self.map_sec_cb, 5, 1)

        lbl_tert = QLabel("Tertiary Action (Grid/Bomb):")
        lbl_tert.setStyleSheet("color: #cbd5e1; font-size: 13px; border: none; font-weight: bold;")
        grid.addWidget(lbl_tert, 6, 0)

        self.map_tert_cb = QComboBox()
        self.map_tert_cb.addItems(self.options)
        self.map_tert_cb.setCurrentIndex(0)  # Muse Eye Blink
        self.map_tert_cb.setStyleSheet("background-color: #0f172a; color: #cbd5e1; border: 1px solid #475569; padding: 4px; border-radius: 4px; font-size: 12px;")
        grid.addWidget(self.map_tert_cb, 6, 1)

        left_layout.addLayout(grid)
        left_layout.addSpacing(10)

        # Thresholds tuning group box
        thresh_group = QGroupBox("🎛️ Sensitivity Thresholds Tuning")
        thresh_group.setStyleSheet("""
            QGroupBox {
                color: #38bdf8;
                font-weight: bold;
                border: 1px solid #334155;
                border-radius: 8px;
                margin-top: 8px;
                padding-top: 12px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px;
            }
            QLabel {
                color: #cbd5e1;
                font-size: 11px;
                border: none;
            }
        """)
        thresh_layout = QVBoxLayout(thresh_group)
        thresh_layout.setSpacing(6)

        # Helper to create a slider with live label
        def create_slider(label_text, min_val, max_val, default_val, unit=""):
            row_layout = QHBoxLayout()
            lbl = QLabel(label_text)
            val_lbl = QLabel(f"{default_val}{unit}")
            val_lbl.setFixedWidth(50)
            val_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setRange(min_val, max_val)
            slider.setValue(default_val)
            slider.setStyleSheet("""
                QSlider::groove:horizontal { height: 4px; background: #334155; border-radius: 2px; }
                QSlider::handle:horizontal { background: #38bdf8; width: 12px; height: 12px; margin: -4px 0; border-radius: 6px; }
            """)
            def update_label(v):
                if "Flick" in label_text:
                    val_lbl.setText(f"{v/100.0:.2f}")
                elif "Shake" in label_text:
                    val_lbl.setText(f"{v/10.0:.1f}")
                else:
                    val_lbl.setText(f"{v}{unit}")
            slider.valueChanged.connect(update_label)
            # Set initial label
            update_label(default_val)
            
            row_layout.addWidget(lbl)
            row_layout.addWidget(slider)
            row_layout.addWidget(val_lbl)
            thresh_layout.addLayout(row_layout)
            return slider

        self.blink_thresh_slider = create_slider("Muse Eye Blink:", 15, 120, 45, " uV")
        self.clench_thresh_slider = create_slider("Muse Jaw Clench:", 10, 80, 35, " uV")
        self.focus_thresh_slider = create_slider("Muse High Focus:", 25, 90, 60, "%")
        self.calm_thresh_slider = create_slider("Muse Deep Calm:", 25, 90, 60, "%")
        self.tilt_thresh_slider = create_slider("Phone Tilt Angle:", 5, 45, 15, "°")
        self.flick_thresh_slider = create_slider("Phone Flick:", 10, 80, 35) 
        self.shake_thresh_slider = create_slider("Phone Shake:", 20, 150, 80)
        self.shake_thresh_slider.valueChanged.connect(self.sync_imu_shake_threshold)

        left_layout.addWidget(thresh_group)
        left_layout.addSpacing(10)

        # Calibration / Zero button
        self.calib_btn = QPushButton("🎯 Zero / Calibrate Phone Sensors")
        self.calib_btn.setMinimumHeight(38)
        self.calib_btn.setStyleSheet("""
            QPushButton {
                background-color: #0284c7;
                color: white;
                font-weight: bold;
                border-radius: 8px;
                font-size: 13px;
                border: none;
            }
            QPushButton:hover {
                background-color: #0369a1;
            }
        """)
        self.calib_btn.clicked.connect(self.trigger_calibration)
        left_layout.addWidget(self.calib_btn)

        left_layout.addStretch(1)
        layout.addWidget(left_panel, stretch=1)

        # Right Column: Visual Test Console
        right_panel = QFrame()
        right_panel.setStyleSheet("background-color: #0f172a; border-radius: 12px; border: 1px solid #1e293b;")
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(20, 20, 20, 20)
        right_layout.setSpacing(12)

        lbl_right_title = QLabel("🔬 Real-time Input Signal Monitor")
        lbl_right_title.setStyleSheet("font-size: 18px; font-weight: bold; color: #10b981; border: none;")
        right_layout.addWidget(lbl_right_title)

        lbl_right_desc = QLabel("Perform actions (blink, clench, tilt or flick phone) to test triggers before starting the game.")
        lbl_right_desc.setStyleSheet("font-size: 12px; color: #64748b; border: none;")
        lbl_right_desc.setWordWrap(True)
        right_layout.addWidget(lbl_right_desc)

        right_layout.addSpacing(10)

        # Grid of LED indicators
        self.indicators = {}
        ind_grid = QGridLayout()
        ind_grid.setSpacing(10)

        # Build 16 indicators
        for i, opt in enumerate(self.options):
            card = QFrame()
            card.setObjectName("IndCard")
            card.setStyleSheet("""
                QFrame#IndCard {
                    background-color: #1e293b;
                    border: 1px solid #334155;
                    border-radius: 8px;
                }
            """)
            card_lay = QHBoxLayout(card)
            card_lay.setContentsMargins(10, 8, 10, 8)
            card_lay.setSpacing(8)

            led = QLabel()
            led.setFixedSize(14, 14)
            led.setStyleSheet("background-color: #475569; border-radius: 7px; border: none;")
            
            lbl_name = QLabel(opt)
            lbl_name.setStyleSheet("color: #cbd5e1; font-size: 12px; border: none; font-weight: bold;")
            
            card_lay.addWidget(led)
            card_lay.addWidget(lbl_name, stretch=1)

            # Keep references to animate later
            self.indicators[opt] = {
                "frame": card,
                "led": led,
                "label": lbl_name
            }

            row = i // 2
            col = i % 2
            ind_grid.addWidget(card, row, col)

        right_layout.addLayout(ind_grid)
        right_layout.addSpacing(15)

        # Real-time Tuning Oscilloscope Plot!
        self.scope_widget = pg.PlotWidget(title="📈 Real-Time Calibration Scope")
        self.scope_widget.setMinimumHeight(240)
        self.scope_widget.setBackground('#0b0f19')
        self.scope_widget.showGrid(x=True, y=True, alpha=0.15)
        self.scope_widget.setYRange(0, 150) # Range up to 150 for blink spikes and scaled shakes
        
        # Track 6 curves: Focus, Calm, Blink, Clench, Tilt, Shake
        self.history_len = 100
        self.focus_hist = np.zeros(self.history_len)
        self.calm_hist = np.zeros(self.history_len)
        self.blink_hist = np.zeros(self.history_len)
        self.clench_hist = np.zeros(self.history_len)
        self.tilt_hist = np.zeros(self.history_len)
        self.shake_hist = np.zeros(self.history_len)
        
        # Color curves
        self.curve_focus = self.scope_widget.plot(pen=pg.mkPen('#06b6d4', width=2), name="Focus %")
        self.curve_calm = self.scope_widget.plot(pen=pg.mkPen('#d946ef', width=2), name="Calm %")
        self.curve_blink = self.scope_widget.plot(pen=pg.mkPen('#fbbf24', width=2), name="Blink uV")
        self.curve_clench = self.scope_widget.plot(pen=pg.mkPen('#ef4444', width=2), name="Clench uV")
        self.curve_tilt = self.scope_widget.plot(pen=pg.mkPen('#10b981', width=2), name="Tilt Angle°")
        self.curve_shake = self.scope_widget.plot(pen=pg.mkPen('#f97316', width=2), name="Shake Force")
        
        # Horizontal threshold indicator lines (dashed)
        self.line_focus = pg.InfiniteLine(angle=0, pen=pg.mkPen('#06b6d4', style=Qt.PenStyle.DashLine))
        self.line_calm = pg.InfiniteLine(angle=0, pen=pg.mkPen('#d946ef', style=Qt.PenStyle.DashLine))
        self.line_blink = pg.InfiniteLine(angle=0, pen=pg.mkPen('#fbbf24', style=Qt.PenStyle.DashLine))
        self.line_clench = pg.InfiniteLine(angle=0, pen=pg.mkPen('#ef4444', style=Qt.PenStyle.DashLine))
        self.line_tilt = pg.InfiniteLine(angle=0, pen=pg.mkPen('#10b981', style=Qt.PenStyle.DashLine))
        self.line_shake = pg.InfiniteLine(angle=0, pen=pg.mkPen('#f97316', style=Qt.PenStyle.DashLine))
        
        self.scope_widget.addItem(self.line_focus)
        self.scope_widget.addItem(self.line_calm)
        self.scope_widget.addItem(self.line_blink)
        self.scope_widget.addItem(self.line_clench)
        self.scope_widget.addItem(self.line_tilt)
        self.scope_widget.addItem(self.line_shake)
        
        right_layout.addWidget(self.scope_widget, stretch=1)
        layout.addWidget(right_panel, stretch=1)

        # Update Timer for Scope (Runs at ~30 FPS / 33ms)
        self.update_timer = QTimer(self)
        self.update_timer.timeout.connect(self.update_scope)
        self.update_timer.start(33)

    def flash_indicator(self, source_input):
        """Lights up the indicator corresponding to the action for 0.5 seconds."""
        if source_input in self.indicators:
            ind = self.indicators[source_input]
            
            # Animate LED color and label glow
            ind["led"].setStyleSheet("background-color: #10b981; border-radius: 7px; border: none;")
            ind["frame"].setStyleSheet("""
                QFrame#IndCard {
                    background-color: #064e3b;
                    border: 1px solid #10b981;
                    border-radius: 8px;
                }
            """)
            ind["label"].setStyleSheet("color: #ffffff; font-size: 12px; border: none; font-weight: bold;")

            # Reset after 500ms
            QTimer.singleShot(500, lambda: self.reset_indicator(source_input))

    def reset_indicator(self, source_input):
        if source_input in self.indicators:
            ind = self.indicators[source_input]
            ind["led"].setStyleSheet("background-color: #475569; border-radius: 7px; border: none;")
            ind["frame"].setStyleSheet("""
                QFrame#IndCard {
                    background-color: #1e293b;
                    border: 1px solid #334155;
                    border-radius: 8px;
                }
            """)
            ind["label"].setStyleSheet("color: #cbd5e1; font-size: 12px; border: none; font-weight: bold;")

    def get_trigger_action(self, source_input):
        # Flash the test monitor LED dynamically in real-time!
        self.flash_indicator(source_input)
        
        if self.map_left_cb.currentText() == source_input:
            return "left"
        if self.map_right_cb.currentText() == source_input:
            return "right"
        if self.map_up_cb.currentText() == source_input:
            return "up"
        if self.map_down_cb.currentText() == source_input:
            return "down"
        if self.map_action_cb.currentText() == source_input:
            return "action"
        if self.map_sec_cb.currentText() == source_input:
            return "sec_action"
        if self.map_tert_cb.currentText() == source_input:
            return "tert_action"
        return None

    def trigger_calibration(self):
        main_win = self.window()
        if hasattr(main_win, "imu_tab") and main_win.imu_tab:
            main_win.imu_tab.calibrate_imu_sensors()

    def sync_imu_shake_threshold(self, val):
        main_win = self.window()
        if hasattr(main_win, "imu_tab") and main_win.imu_tab:
            main_win.imu_tab.shake_threshold_slider.blockSignals(True)
            main_win.imu_tab.shake_threshold_slider.setValue(val)
            main_win.imu_tab.shake_threshold_slider.blockSignals(False)

    def update_scope(self):
        main_win = self.window()
        if not main_win:
            return
            
        # 1. Pull live values from EEG and IMU tabs
        eeg_tab = getattr(main_win, "eeg_tab", None)
        imu_tab = getattr(main_win, "imu_tab", None)
        
        live_focus = eeg_tab.smoothed_focus if (eeg_tab and hasattr(eeg_tab, "smoothed_focus")) else 0.0
        live_calm = eeg_tab.smoothed_calm if (eeg_tab and hasattr(eeg_tab, "smoothed_calm")) else 0.0
        live_blink = eeg_tab.blink_val if (eeg_tab and hasattr(eeg_tab, "blink_val")) else 0.0
        live_clench = eeg_tab.clench_val if (eeg_tab and hasattr(eeg_tab, "clench_val")) else 0.0
        
        live_tilt = 0.0
        live_shake = 0.0
        if imu_tab and hasattr(imu_tab, "roll") and hasattr(imu_tab, "pitch"):
            # Max deviation tilt
            live_tilt = max(abs(imu_tab.roll), abs(imu_tab.pitch))
            live_shake = imu_tab.current_shake_force
            
        # 2. Append to histories
        self.focus_hist[:-1] = self.focus_hist[1:]
        self.focus_hist[-1] = live_focus
        
        self.calm_hist[:-1] = self.calm_hist[1:]
        self.calm_hist[-1] = live_calm
        
        self.blink_hist[:-1] = self.blink_hist[1:]
        self.blink_hist[-1] = live_blink
        
        self.clench_hist[:-1] = self.clench_hist[1:]
        self.clench_hist[-1] = live_clench
        
        self.tilt_hist[:-1] = self.tilt_hist[1:]
        self.tilt_hist[-1] = live_tilt
        
        self.shake_hist[:-1] = self.shake_hist[1:]
        self.shake_hist[-1] = live_shake * 10.0 # scale shake by 10 for plot visibility
        
        # 3. Redraw curves
        self.curve_focus.setData(self.focus_hist)
        self.curve_calm.setData(self.calm_hist)
        self.curve_blink.setData(self.blink_hist)
        self.curve_clench.setData(self.clench_hist)
        self.curve_tilt.setData(self.tilt_hist)
        self.curve_shake.setData(self.shake_hist)
        
        # 4. Synchronize threshold dashed lines with slider values
        self.line_focus.setValue(self.focus_thresh_slider.value())
        self.line_calm.setValue(self.calm_thresh_slider.value())
        self.line_blink.setValue(self.blink_thresh_slider.value())
        self.line_clench.setValue(self.clench_thresh_slider.value())
        self.line_tilt.setValue(self.tilt_thresh_slider.value())
        self.line_shake.setValue(self.shake_thresh_slider.value())


# ==========================================
# CENTRAL NERVOUS SYSTEM - MASTER DASHBOARD
# ==========================================
class MasterControlDashboard(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("⚙️ Neurotech & Wearables Workshop Orchestrator")
        self.resize(1200, 850)

        # Registry of process structures
        self.services = {
            "athena": {
                "name": "Athena Muse Streamer",
                "script": "athena_streamer.py",
                "sim_capable": True,
                "desc": "Connects to Muse S via BLE. Streams EEG (channels 1-4) and PPG optical signals over LSL.",
                "process": None,
                "status": "Stopped",
                "color": "#ec4899"  # magenta
            },
            "ppg_sim": {
                "name": "PPG Smartwatch Simulator",
                "script": "watch_ppg_streamer.py",
                "sim_capable": False,
                "desc": "Simulates Watch green/red optical channels over LSL. Keyboards edit heart rate on the fly.",
                "process": None,
                "status": "Stopped",
                "color": "#10b981"  # emerald green
            },
            "imu_sim": {
                "name": "IMU Smartwatch Simulator",
                "script": "watch_imu_streamer.py",
                "sim_capable": False,
                "desc": "Simulates Watch Accel/Gyro coordinates. Allows switching circular, stationary and figure-8 paths.",
                "process": None,
                "status": "Stopped",
                "color": "#0ea5e9"  # sky blue
            },
            "phone_bridge": {
                "name": "Phone IMU LSL Bridge",
                "script": "phone_imu_bridge.py",
                "sim_capable": False,
                "desc": "Listens for real-time IMU streams from the free 'Sensor Logger' phone app over WebSockets (port 8000) and maps to the Smartwatch LSL format.",
                "process": None,
                "status": "Stopped",
                "color": "#a855f7"  # purple
            }
        }

        # Track external detached processes
        self.external_processes = []

        self.apply_theme()
        self.init_ui()

    def apply_theme(self):
        self.setStyleSheet("""
            QMainWindow {
                background-color: #0b0f19;
            }
            QWidget {
                font-family: 'Inter', 'Segoe UI', Arial, sans-serif;
                color: #e2e8f0;
            }
            QTabWidget::pane {
                border: 1px solid #1e293b;
                background-color: #0f172a;
                border-radius: 8px;
            }
            QTabBar::tab {
                background-color: #1e293b;
                border: 1px solid #1e293b;
                border-bottom-color: transparent;
                padding: 10px 20px;
                border-top-left-radius: 6px;
                border-top-right-radius: 6px;
                margin-right: 4px;
                font-size: 13px;
                font-weight: 500;
                color: #94a3b8;
            }
            QTabBar::tab:selected {
                background-color: #0f172a;
                border-color: #334155;
                border-bottom: 2px solid #38bdf8;
                color: #38bdf8;
                font-weight: bold;
            }
            QTabBar::tab:hover {
                background-color: #334155;
                color: #f1f5f9;
            }
            QPushButton {
                background-color: #1e293b;
                border: 1px solid #334155;
                border-radius: 6px;
                padding: 8px 14px;
                font-size: 12px;
                font-weight: bold;
                color: #f1f5f9;
            }
            QPushButton:hover {
                background-color: #334155;
                border-color: #475569;
            }
            QPushButton:pressed {
                background-color: #0f172a;
            }
            QTextEdit {
                background-color: #020617;
                border: 1px solid #1e293b;
                border-radius: 8px;
                color: #38bdf8;
            }
            QComboBox {
                background-color: #1e293b;
                border: 1px solid #334155;
                border-radius: 6px;
                padding: 6px;
                color: #f1f5f9;
            }
            QComboBox::drop-down {
                border: none;
            }
            QCheckBox {
                spacing: 8px;
            }
            QCheckBox::indicator {
                width: 16px;
                height: 16px;
                border: 1px solid #334155;
                border-radius: 4px;
                background-color: #1e293b;
            }
            QCheckBox::indicator:checked {
                background-color: #38bdf8;
                border-color: #0ea5e9;
            }
            QFrame.ServiceCard {
                background-color: #111827;
                border: 1px solid #1f2937;
                border-radius: 10px;
            }
        """)

    def init_ui(self):
        # Central Tab Manager
        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)

        # Add Tab 1: Control Center Dashboard
        self.control_tab = QWidget()
        self.init_control_tab()
        self.tabs.addTab(self.control_tab, "🎮 Control Room")

        # Add Tab 2: EEG Visualizer Widget
        self.eeg_tab = EEGVisualizerWidget()
        self.tabs.addTab(self.eeg_tab, "🧠 EEG Telemetry")

        # Add Tab 3: PPG Watch Widget
        self.ppg_tab = SmartwatchPPGWidget()
        self.tabs.addTab(self.ppg_tab, "⌚ PPG Watch Monitor")

        # Add Tab 4: 3D IMU Widget
        self.imu_tab = IMUTrajectory3DWidget()
        self.tabs.addTab(self.imu_tab, "📐 3D IMU Trajectory")

        # Add Tab 5: Muse Hardware PPG
        self.muse_ppg_tab = MusePPGWidget()
        self.tabs.addTab(self.muse_ppg_tab, "🫀 Muse PPG Monitor")

        # Add Tab 6: BCI Game Controller Mapping
        self.mapping_tab = BCIControllerMappingWidget(self)
        self.tabs.addTab(self.mapping_tab, "🕹️ Controller Mapping")

        # Dynamic disconnect logic when changing tabs to release LSL locks
        self.tabs.currentChanged.connect(self.on_tab_changed)

    def init_control_tab(self):
        main_layout = QHBoxLayout(self.control_tab)
        main_layout.setContentsMargins(16, 16, 16, 16)
        main_layout.setSpacing(16)

        # Left Column: Service Grid (2/3 size)
        left_layout = QVBoxLayout()
        left_layout.setSpacing(12)
        main_layout.addLayout(left_layout, stretch=2)

        # Header Title Banner
        header = QLabel("Physiological Computing Workshop Orchestrator")
        header.setStyleSheet("font-size: 22px; font-weight: bold; color: #38bdf8; padding-bottom: 2px;")
        desc = QLabel("Central control panel to configure BLE hardware daemons, start smartwatch simulators, and launch slide servers.")
        desc.setStyleSheet("font-size: 13px; color: #94a3b8; padding-bottom: 8px;")
        left_layout.addWidget(header)
        left_layout.addWidget(desc)

        # Service Card Area (Scrollable grid)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("background-color: transparent; border: none;")
        scroll_content = QWidget()
        scroll_content.setStyleSheet("background-color: transparent;")
        self.card_layout = QVBoxLayout(scroll_content)
        self.card_layout.setSpacing(12)
        self.card_layout.setContentsMargins(0, 0, 0, 0)
        
        # Build cards
        self.service_widgets = {}
        for key, s in self.services.items():
            card = self.create_service_card(key, s)
            self.card_layout.addWidget(card)
            
        scroll.setWidget(scroll_content)
        left_layout.addWidget(scroll)

        # Bottom section: External App launcher
        external_card = QFrame()
        external_card.setObjectName("ExtCard")
        external_card.setStyleSheet("""
            QFrame#ExtCard {
                background-color: #1e293b;
                border: 1px solid #334155;
                border-radius: 8px;
            }
        """)
        ext_layout = QHBoxLayout(external_card)
        ext_layout.setContentsMargins(12, 12, 12, 12)
        
        ext_lbl_layout = QVBoxLayout()
        ext_title = QLabel("Workshop Interactive BCI Applications")
        ext_title.setStyleSheet("font-weight: bold; font-size: 13px; color: #f1f5f9;")
        ext_desc = QLabel("Launch external standalone applications that use active LSL streams.")
        ext_desc.setStyleSheet("font-size: 11px; color: #94a3b8;")
        ext_lbl_layout.addWidget(ext_title)
        ext_lbl_layout.addWidget(ext_desc)
        ext_layout.addLayout(ext_lbl_layout, stretch=1)

        snake_btn = QPushButton("🎮 Start BCI Snake Game")
        snake_btn.setMinimumHeight(36)
        snake_btn.setStyleSheet("background-color: #1e1b4b; border: 1px solid #4338ca; color: #a5b4fc;")
        snake_btn.clicked.connect(lambda: self.launch_external("lsl_snake_game.py"))
        ext_layout.addWidget(snake_btn)

        plane_btn = QPushButton("🚀 Start BCI Retro Fighter")
        plane_btn.setMinimumHeight(36)
        plane_btn.setStyleSheet("background-color: #064e3b; border: 1px solid #059669; color: #a7f3d0;")
        plane_btn.clicked.connect(lambda: self.launch_external("lsl_retro_airplane.py"))
        ext_layout.addWidget(plane_btn)

        drum_btn = QPushButton("🥁 Start Muscle Drum Kit")
        drum_btn.setMinimumHeight(36)
        drum_btn.setStyleSheet("background-color: #1c1917; border: 1px solid #78350f; color: #fcd34d;")
        drum_btn.clicked.connect(lambda: self.launch_external("drum_trigger.py"))
        ext_layout.addWidget(drum_btn)

        left_layout.addWidget(external_card)

        # Right Column: Live Logs Console & Filters
        right_layout = QVBoxLayout()
        right_layout.setSpacing(10)
        main_layout.addLayout(right_layout, stretch=1)

        console_header_layout = QHBoxLayout()
        console_lbl = QLabel("📡 System Telemetry Output")
        console_lbl.setStyleSheet("font-weight: bold; font-size: 14px; color: #38bdf8;")
        console_header_layout.addWidget(console_lbl)
        
        self.log_filter_cb = QComboBox()
        self.log_filter_cb.addItem("All Log Streams", "all")
        for k, s in self.services.items():
            self.log_filter_cb.addItem(s["name"], k)
        self.log_filter_cb.currentIndexChanged.connect(self.refresh_console)
        console_header_layout.addWidget(self.log_filter_cb)
        
        right_layout.addLayout(console_header_layout)

        # Log storage
        self.logs_data = {k: [] for k in self.services.keys()}
        self.logs_data["all"] = []

        self.console = QTextEdit()
        self.console.setReadOnly(True)
        self.console.setFont(QFont("Courier New", 10))
        self.console.setStyleSheet("background-color: #020617; border: 1px solid #1e293b; color: #34d399;")
        right_layout.addWidget(self.console, stretch=1)

        # Console controls
        btn_layout = QHBoxLayout()
        
        clear_btn = QPushButton("Clear Output")
        clear_btn.setStyleSheet("background-color: #334155;")
        clear_btn.clicked.connect(self.clear_console)
        btn_layout.addWidget(clear_btn)

        self.autoscroll_check = QCheckBox("Auto-scroll")
        self.autoscroll_check.setChecked(True)
        btn_layout.addWidget(self.autoscroll_check)

        right_layout.addLayout(btn_layout)

    def create_service_card(self, key, s):
        card = QFrame()
        card.setObjectName("ServiceCard")
        card.setFrameShape(QFrame.Shape.StyledPanel)
        card.setStyleSheet("""
            QFrame#ServiceCard {
                background-color: #0f172a;
                border: 1px solid #1e293b;
                border-radius: 10px;
            }
        """)
        
        layout = QVBoxLayout(card)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        # Top section: status + title
        top_row = QHBoxLayout()
        
        # Status Light Indicator
        status_indicator = QWidget()
        status_indicator.setFixedSize(12, 12)
        # Custom properties to update color style
        status_indicator.setStyleSheet("background-color: #64748b; border-radius: 6px;")
        top_row.addWidget(status_indicator)

        title = QLabel(s["name"])
        title.setStyleSheet("font-size: 15px; font-weight: bold; color: #f8fafc;")
        top_row.addWidget(title, stretch=1)

        status_lbl = QLabel("Stopped")
        status_lbl.setStyleSheet("font-size: 12px; font-weight: bold; color: #64748b;")
        top_row.addWidget(status_lbl)
        
        layout.addLayout(top_row)

        # Mid section: description
        desc = QLabel(s["desc"])
        desc.setWordWrap(True)
        desc.setStyleSheet("font-size: 11px; color: #94a3b8; line-height: 1.4;")
        layout.addWidget(desc)

        # Interactive Sub-controls (e.g. key inputs for simulator adjustments)
        sub_panel = QFrame()
        sub_panel.setVisible(False)
        sub_panel_layout = QHBoxLayout(sub_panel)
        sub_panel_layout.setContentsMargins(0, 0, 0, 0)
        sub_panel_layout.setSpacing(6)

        if key == "ppg_sim":
            sub_panel.setVisible(True)
            lbl = QLabel("Manual HR Tuning:")
            lbl.setStyleSheet("font-size: 11px; font-weight: bold; color: #94a3b8;")
            sub_panel_layout.addWidget(lbl)
            
            btn_up = QPushButton("BPM +5")
            btn_up.clicked.connect(lambda: self.send_to_process("ppg_sim", "w"))
            sub_panel_layout.addWidget(btn_up)
            
            btn_down = QPushButton("BPM -5")
            btn_down.clicked.connect(lambda: self.send_to_process("ppg_sim", "s"))
            sub_panel_layout.addWidget(btn_down)
            
            btn_rand = QPushButton("Randomize")
            btn_rand.clicked.connect(lambda: self.send_to_process("ppg_sim", " "))
            sub_panel_layout.addWidget(btn_rand)
            sub_panel_layout.addStretch(1)

        elif key == "imu_sim":
            sub_panel.setVisible(True)
            lbl = QLabel("Motion Pattern:")
            lbl.setStyleSheet("font-size: 11px; font-weight: bold; color: #94a3b8;")
            sub_panel_layout.addWidget(lbl)
            
            patterns = [("Stationary", "1"), ("Circular", "2"), ("Figure-8", "3"), ("Shaking", "4")]
            for pat_name, cmd in patterns:
                btn = QPushButton(pat_name)
                btn.clicked.connect(lambda checked, c=cmd: self.send_to_process("imu_sim", c))
                sub_panel_layout.addWidget(btn)
            sub_panel_layout.addStretch(1)

        elif key == "athena" and s["sim_capable"]:
            sub_panel.setVisible(True)
            self.sim_checkbox = QCheckBox("Synthesize simulated EEG data (Mock Mode)")
            self.sim_checkbox.setStyleSheet("font-size: 11px; color: #94a3b8;")
            sub_panel_layout.addWidget(self.sim_checkbox)
            sub_panel_layout.addStretch(1)

        elif key == "phone_bridge":
            sub_panel.setVisible(True)
            lbl = QLabel("IP Interface:")
            lbl.setStyleSheet("font-size: 11px; font-weight: bold; color: #94a3b8;")
            sub_panel_layout.addWidget(lbl)
            
            self.ip_combo = QComboBox()
            for label, ip in get_all_local_ips():
                self.ip_combo.addItem(label, ip)
            sub_panel_layout.addWidget(self.ip_combo)
            sub_panel_layout.addStretch(1)

        layout.addWidget(sub_panel)

        # Bottom section: Control Buttons
        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        
        start_btn = QPushButton("Start Service")
        start_btn.setMinimumHeight(32)
        start_btn.setStyleSheet("""
            QPushButton {
                background-color: #064e3b;
                border: 1px solid #065f46;
                color: #34d399;
            }
            QPushButton:hover {
                background-color: #065f46;
            }
        """)
        
        stop_btn = QPushButton("Stop Service")
        stop_btn.setMinimumHeight(32)
        stop_btn.setEnabled(False)
        stop_btn.setStyleSheet("""
            QPushButton {
                background-color: #451a03;
                border: 1px solid #7c2d12;
                color: #f87171;
            }
            QPushButton:hover {
                background-color: #7c2d12;
            }
            QPushButton:disabled {
                background-color: #1e1e2d;
                border: 1px solid #2d2d3d;
                color: #4b4b5b;
            }
        """)

        start_btn.clicked.connect(lambda: self.start_service(key))
        stop_btn.clicked.connect(lambda: self.stop_service(key))
        
        btn_row.addWidget(start_btn)
        btn_row.addWidget(stop_btn)
        
        layout.addLayout(btn_row)

        # Store widget references for status updates
        self.service_widgets[key] = {
            "light": status_indicator,
            "status_lbl": status_lbl,
            "start_btn": start_btn,
            "stop_btn": stop_btn,
            "sub_panel": sub_panel
        }

        return card

    # ==========================================
    # SUBPROCESS MANAGEMENT LOGIC
    # ==========================================
    def start_service(self, key):
        s = self.services[key]
        if s["process"] is not None:
            return

        w = self.service_widgets[key]
        w["start_btn"].setEnabled(False)
        w["status_lbl"].setText("Starting...")
        w["status_lbl"].setStyleSheet("color: #f59e0b;") # Orange
        w["light"].setStyleSheet("background-color: #f59e0b; border-radius: 6px;")

        # Resolve launch arguments
        cmd_args = [s["script"]]
        if key == "athena" and self.sim_checkbox.isChecked():
            cmd_args.append("--sim")
        elif key == "phone_bridge":
            selected_ip = self.ip_combo.currentData()
            cmd_args.extend(["--ip", selected_ip])
            
        from PyQt6.QtCore import QProcessEnvironment
        process = QProcess()
        process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONUNBUFFERED", "1")
        process.setProcessEnvironment(env)
        process.readyReadStandardOutput.connect(lambda: self.read_process_output(key))
        process.stateChanged.connect(lambda state: self.process_state_changed(key, state))
        
        # Start using uv run to resolve metadata-defined dependencies automatically
        process.start("uv", ["run"] + cmd_args)
        
        s["process"] = process

    def stop_service(self, key):
        s = self.services[key]
        if s["process"] is None:
            return

        # Attempt clean shutdown
        w = self.service_widgets[key]
        w["stop_btn"].setEnabled(False)
        w["status_lbl"].setText("Stopping...")
        w["status_lbl"].setStyleSheet("color: #f59e0b;")
        w["light"].setStyleSheet("background-color: #f59e0b; border-radius: 6px;")

        # Send 'q' key to simulator if supported, otherwise terminate
        if key in ["ppg_sim", "imu_sim"]:
            self.send_to_process(key, "q")
            # Wait briefly for standard cleanup, otherwise force terminate
            QTimer.singleShot(800, lambda: self.force_kill_process(key))
        else:
            s["process"].terminate()
            QTimer.singleShot(1500, lambda: self.force_kill_process(key))

    def force_kill_process(self, key):
        s = self.services[key]
        if s["process"] and s["process"].state() != QProcess.ProcessState.NotRunning:
            s["process"].kill()

    def process_state_changed(self, key, state):
        s = self.services[key]
        w = self.service_widgets[key]

        if state == QProcess.ProcessState.Running:
            s["status"] = "Running"
            w["status_lbl"].setText("Running")
            w["status_lbl"].setStyleSheet("color: #10b981;") # Emerald Green
            w["light"].setStyleSheet("background-color: #10b981; border-radius: 6px;")
            w["start_btn"].setEnabled(False)
            w["stop_btn"].setEnabled(True)
            if key == "athena":
                self.sim_checkbox.setEnabled(False)
            elif key == "phone_bridge":
                self.ip_combo.setEnabled(False)
                
            self.append_system_log(f"Service {s['name']} successfully initialized.")

        elif state == QProcess.ProcessState.NotRunning:
            s["status"] = "Stopped"
            w["status_lbl"].setText("Stopped")
            w["status_lbl"].setStyleSheet("color: #64748b;")
            w["light"].setStyleSheet("background-color: #64748b; border-radius: 6px;")
            w["start_btn"].setEnabled(True)
            w["stop_btn"].setEnabled(False)
            if key == "athena":
                self.sim_checkbox.setEnabled(True)
            elif key == "phone_bridge":
                self.ip_combo.setEnabled(True)
                
            # Log cleanup
            exit_code = s["process"].exitCode()
            self.append_system_log(f"Service {s['name']} shut down (Exit code: {exit_code}).")
            s["process"] = None
            
            # Proactively stop telemetry plots if the source script dies
            self.disconnect_telemetry_widgets(key)

    def read_process_output(self, key):
        s = self.services[key]
        if s["process"] is None:
            return
        
        data = s["process"].readAllStandardOutput().data().decode("utf-8", errors="replace")
        self.append_service_log(key, data)

    def send_to_process(self, key, data):
        """Sends data (e.g. keys) down the stdin pipe of the target QProcess."""
        s = self.services[key]
        if s["process"] and s["process"].state() == QProcess.ProcessState.Running:
            # Append newline to ensure flushed read on pipes
            s["process"].write(f"{data}\n".encode())
            print(f"[Dashboard] Sent command '{data}' to stdin of {s['name']}")

    def launch_external(self, script_name):
        """Launches pygame or audio triggers as isolated detached processes."""
        self.append_system_log(f"Launching external BCI application: {script_name}...")
        proc = QProcess()
        if script_name in ["lsl_snake_game.py", "lsl_retro_airplane.py"]:
            # Pipe standard input, output and error separately so stdin writing works
            proc.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
            
            # Read and print child outputs to terminal live
            proc.readyReadStandardOutput.connect(lambda p=proc: sys.stdout.write(p.readAllStandardOutput().data().decode(errors='ignore')))
            proc.readyReadStandardError.connect(lambda p=proc: sys.stderr.write(p.readAllStandardError().data().decode(errors='ignore')))
            
            # Use '-u' flag to force unbuffered stdin/stdout in the child python process
            proc.start(sys.executable, ["-u", script_name])
            self.snake_process = proc
        else:
            proc.startDetached(sys.executable, [script_name])
        self.external_processes.append(proc)

    def send_command_to_snake_game(self, cmd):
        if hasattr(self, "snake_process") and self.snake_process and self.snake_process.state() == QProcess.ProcessState.Running:
            self.snake_process.write(f"{cmd}\n".encode())
            self.append_system_log(f"Forwarded BCI command '{cmd}' to Snake Game stdin")

    def get_trigger_action(self, source_input):
        """Returns the game command mapped to the given input source."""
        if hasattr(self, "mapping_tab") and self.mapping_tab:
            return self.mapping_tab.get_trigger_action(source_input)
        return None

    # ==========================================
    # LOGGING AND FILTERING LOGIC
    # ==========================================
    def append_system_log(self, text):
        timestamp = time.strftime("%H:%M:%S")
        formatted = f'<span style="color: #64748b;">[{timestamp}] [SYSTEM] {text}</span><br>'
        
        self.logs_data["all"].append(formatted)
        if len(self.logs_data["all"]) > 500:
            self.logs_data["all"].pop(0)

        # Show if current filter is 'all'
        if self.log_filter_cb.currentData() == "all":
            self.console.append(formatted)

    def append_service_log(self, key, text):
        s = self.services[key]
        timestamp = time.strftime("%H:%M:%S")
        
        # Normalize and split lines
        lines = text.strip().split("\n")
        formatted_lines = []
        for line in lines:
            if not line.strip():
                continue
            # Color code log based on service
            col = s["color"]
            formatted = f'<span style="color: {col};">[{timestamp}] [{key.upper()}]</span> <span style="color: #cbd5e1;">{line}</span>'
            formatted_lines.append(formatted)

        for f in formatted_lines:
            self.logs_data[key].append(f)
            if len(self.logs_data[key]) > 200:
                self.logs_data[key].pop(0)
                
            self.logs_data["all"].append(f)
            if len(self.logs_data["all"]) > 500:
                self.logs_data["all"].pop(0)

        # Append to visible console if filter matches
        active_filter = self.log_filter_cb.currentData()
        if active_filter == "all" or active_filter == key:
            for f in formatted_lines:
                self.console.append(f)
                
        if self.autoscroll_check.isChecked():
            self.console.moveCursor(QTextCursor.MoveOperation.End)

    def refresh_console(self):
        self.console.clear()
        active_filter = self.log_filter_cb.currentData()
        logs_to_show = self.logs_data[active_filter]
        
        # Batch inject text to prevent layout stalls
        self.console.setHtml("<br>".join(logs_to_show))
        if self.autoscroll_check.isChecked():
            self.console.moveCursor(QTextCursor.MoveOperation.End)

    def clear_console(self):
        active_filter = self.log_filter_cb.currentData()
        self.logs_data[active_filter].clear()
        if active_filter == "all":
            # Clear individual service caches as well
            for k in self.logs_data.keys():
                self.logs_data[k].clear()
        self.console.clear()

    # ==========================================
    # TELEMETRY FLOW CONNECTIONS
    # ==========================================
    def disconnect_telemetry_widgets(self, key):
        """Force close plots if the backend LSL streamer script is terminated."""
        if key == "athena":
            if self.eeg_tab.inlet:
                self.eeg_tab.stop_feed()
            if self.muse_ppg_tab.inlet:
                self.muse_ppg_tab.stop_feed()
        elif key == "ppg_sim":
            if self.ppg_tab.inlet:
                self.ppg_tab.stop_feed()
        elif key in ["imu_sim", "phone_bridge"]:
            if self.imu_tab.inlet:
                self.imu_tab.stop_feed()

    def on_tab_changed(self, index):
        # We can trigger periodic telemetry checks, but letting connections
        # be explicit via user clicks inside tabs is cleaner and safer.
        pass

    def closeEvent(self, event):
        self.append_system_log("Shutting down master processes. Cleaning up LSL slots...")
        
        # 1. Cleanly stop running QProcesses
        for key, s in self.services.items():
            proc = s["process"]
            if proc and proc.state() != QProcess.ProcessState.NotRunning:
                proc.disconnect()  # prevent state change callbacks from clearing references
                proc.terminate()
                proc.waitForFinished(800)
                proc.kill()

        # 2. Release LSL tab timers
        self.eeg_tab.timer.stop()
        self.ppg_tab.timer.stop()
        self.imu_tab.timer.stop()
        self.muse_ppg_tab.timer.stop()
        
        event.accept()


# ==========================================
# MAIN EXECUTION ENTRYPOINT
# ==========================================
def main():
    app = QApplication(sys.argv)
    dashboard = MasterControlDashboard()
    dashboard.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
