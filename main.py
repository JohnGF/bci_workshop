# -*- coding: utf-8 -*-
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
#     "PyOpenGL",
#     "cryptography",
#     "sounddevice"
# ]
# ///

import sys
import os
import time
import json
import logging
import logging.handlers
import numpy as np
import pyqtgraph as pg
from scipy.signal import butter, sosfilt, iirnotch, lfilter
from numba import njit

from PyQt6.QtCore import QTimer, QProcess, QProcessEnvironment, QThread, pyqtSignal, Qt, QSize

# ==========================================
# CONNECTION LOGGER  (connections.log)
# ==========================================
_log_dir = os.path.dirname(os.path.abspath(__file__))
_log_path = os.path.join(_log_dir, "connections.log")

conn_logger = logging.getLogger("connections")
if not conn_logger.handlers:
    conn_logger.setLevel(logging.DEBUG)
    _handler = logging.handlers.RotatingFileHandler(
        _log_path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    _handler.setFormatter(
        logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    )
    conn_logger.addHandler(_handler)
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
    QSlider,
    QSpinBox,
    QFileDialog
)
from PyQt6.QtGui import QFont, QColor, QTextCursor, QPainter
from pylsl import StreamInlet, resolve_byprop

# ==========================================
# SSL CERT AUTO-PROVISIONING
# ==========================================
def ensure_ssl_cert(base_dir: str) -> str | None:
    """
    Checks cert.pem / key.pem in base_dir.
    Generates / regenerates them if:
      - Either file is missing, OR
      - The cert expires within the next 7 days.
    Returns a status string for the log, or None on failure.
    """
    cert_path = os.path.join(base_dir, "cert.pem")
    key_path  = os.path.join(base_dir, "key.pem")

    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
        import datetime, ipaddress, socket as _socket

        need_regen = False
        reason = ""

        if not os.path.exists(cert_path) or not os.path.exists(key_path):
            need_regen = True
            reason = "cert files missing"
        else:
            try:
                with open(cert_path, "rb") as f:
                    existing = x509.load_pem_x509_certificate(f.read())
                days_left = (existing.not_valid_after_utc - datetime.datetime.now(datetime.timezone.utc)).days
                if days_left < 7:
                    need_regen = True
                    reason = f"cert expires in {days_left} day(s)"
            except Exception:
                need_regen = True
                reason = "cert unreadable"

        if not need_regen:
            return None  # All good, nothing to do

        # --- Detect local IP ---
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 1))
            local_ip = s.getsockname()[0]
        except Exception:
            local_ip = "127.0.0.1"
        finally:
            s.close()

        # --- Generate new key + cert ---
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, local_ip),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "BCI Workshop Local"),
        ])
        san = x509.SubjectAlternativeName([
            x509.DNSName("localhost"),
            x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
            x509.IPAddress(ipaddress.IPv4Address(local_ip)),
        ])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(san, critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256())
        )
        with open(cert_path, "wb") as f:
            f.write(cert.public_bytes(serialization.Encoding.PEM))
        with open(key_path, "wb") as f:
            f.write(key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            ))
        return f"SSL cert auto-generated for {local_ip} ({reason}). Valid 10 years."

    except ImportError:
        return "WARNING: 'cryptography' not installed — HTTPS cert not generated."
    except Exception as e:
        return f"WARNING: cert generation failed: {e}"


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
def integrate_custom_bands(freqs, psd, band_ranges):
    """Integrates arbitrary frequency band ranges without boundary or continuity limits."""
    n_bands = band_ranges.shape[0]
    band_powers = np.zeros(n_bands, dtype=np.float32)
    df = freqs[1] - freqs[0]

    for i in range(n_bands):
        fmin = band_ranges[i, 0]
        fmax = band_ranges[i, 1]
        bp_sum = 0.0

        for j in range(len(freqs)):
            if fmin <= freqs[j] <= fmax:
                bp_sum += psd[j] * df
        band_powers[i] = bp_sum

    return band_powers


def hybrid_dsp_pipeline(data_window, sfreq, band_ranges, notch_freq=50.0, notch_q=30.0, highpass=1.0, lowpass=50.0):
    """Executes FFT using NumPy native C-backend, then passes arbitrary bands to Numba."""
    N = len(data_window)
    clean_data = data_window

    # 1. Notch Filter (Any custom frequency 10Hz-250Hz or Off)
    if notch_freq is not None and notch_freq > 0 and notch_freq < (sfreq / 2.0):
        try:
            b_notch, a_notch = iirnotch(notch_freq, notch_q, sfreq)
            clean_data = lfilter(b_notch, a_notch, clean_data)
        except Exception:
            pass

    # 2. Optional Arbitrary Highpass / Lowpass Bandpass
    if highpass is not None and highpass > 0 and lowpass is not None and lowpass > highpass and lowpass < (sfreq / 2.0):
        try:
            b_band, a_band = butter(2, [highpass, lowpass], btype="bandpass", fs=sfreq)
            clean_data = lfilter(b_band, a_band, clean_data)
        except Exception:
            pass
    elif highpass is not None and highpass > 0 and highpass < (sfreq / 2.0):
        try:
            b_hp, a_hp = butter(2, highpass, btype="highpass", fs=sfreq)
            clean_data = lfilter(b_hp, a_hp, clean_data)
        except Exception:
            pass
    elif lowpass is not None and lowpass > 0 and lowpass < (sfreq / 2.0):
        try:
            b_lp, a_lp = butter(2, lowpass, btype="lowpass", fs=sfreq)
            clean_data = lfilter(b_lp, a_lp, clean_data)
        except Exception:
            pass

    # 3. Hanning window
    window = 0.5 - 0.5 * np.cos(2.0 * np.pi * np.arange(N) / (N - 1))
    windowed = clean_data * window

    # 4. Real FFT
    fft_vals = np.fft.rfft(windowed)

    # 5. Power Spectral Density (PSD)
    psd = (np.abs(fft_vals) ** 2) / (sfreq * N)
    if len(psd) > 2:
        psd[1:-1] *= 2.0

    freqs = np.linspace(0.0, sfreq / 2.0, len(psd))

    # 6. Numba JIT compiled band power integration on arbitrary band ranges
    if band_ranges is not None and len(band_ranges) > 0:
        band_ranges_arr = np.array(band_ranges, dtype=np.float32)
        band_powers = integrate_custom_bands(freqs, psd, band_ranges_arr)
    else:
        band_powers = np.zeros(0, dtype=np.float32)

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
        conn_logger.info(f"RESOLVING  stream={self.stream_name}")

        self.resolver_thread = LSLResolverThread(self.stream_name, timeout=8.0)
        self.resolver_thread.resolved.connect(self.on_resolved)
        self.resolver_thread.failed.connect(self.on_failed)
        self.resolver_thread.start()

    def on_resolved(self, stream_info):
        try:
            self.inlet = StreamInlet(stream_info)
            self._lsl_connect_time = time.monotonic()
            conn_logger.info(f"CONNECT    stream={self.stream_name}  source_id={stream_info.source_id()}")
            self.conn_card.hide()
            self.viz_container.show()
            self.init_visualizer()
            self.timer.start(self.get_timer_interval())
        except Exception as e:
            conn_logger.error(f"ERROR      stream={self.stream_name}  error={e}")
            QMessageBox.critical(self, "Connection Error", f"Failed to open LSL inlet: {e}")
            self.on_failed()

    def on_failed(self):
        conn_logger.warning(f"FAILED     stream={self.stream_name}  reason=resolve_timeout")
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
        duration = time.monotonic() - getattr(self, '_lsl_connect_time', time.monotonic())
        conn_logger.info(f"DISCONNECT stream={self.stream_name}  session_duration={duration:.1f}s")
        self.timer.stop()
        if self.is_recording:
            self.btn_record.setChecked(False)
        self.btn_pause.setChecked(False)
        self.inlet = None
        self.clear_visualizer()
        self.viz_container.hide()
        self.conn_card.show()
        self.desc_lbl.setText("Disconnected. Start the stream and click Connect.")
        self.desc_lbl.setStyleSheet("color: #94a3b8; font-size: 14px;")
        self.conn_btn.setEnabled(True)

    def clear_visualizer(self):
        # Remove and destroy any widgets added after the flow_toolbar
        while self.viz_layout.count() > 1:
            item = self.viz_layout.takeAt(1)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
            else:
                self.clear_layout(item.layout())

    def clear_layout(self, layout):
        if layout is not None:
            while layout.count():
                item = layout.takeAt(0)
                widget = item.widget()
                if widget is not None:
                    widget.deleteLater()
                else:
                    self.clear_layout(item.layout())


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
        # Auto-range the Y axis since EEG power magnitudes vary significantly based on hardware/simulators
        self.bar_plot_widget.enableAutoRange(axis='y')
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

        # Get thresholds & DSP settings from mapping tab dynamically
        main_win = self.window()
        blink_thresh = 45.0
        clench_thresh = 35.0
        focus_thresh = 60.0
        calm_thresh = 60.0
        
        notch_freq = 50.0
        notch_q = 30.0
        hp_cutoff = 1.0
        lp_cutoff = 45.0
        focus_ch_mode = "af8"
        ema_alpha = self.ema_alpha
        blink_ch_mode = 1
        clench_ch_mode = 0
        cooldown_period = 0.8
        
        # Default frequency band ranges: [Delta, Theta, Alpha, Beta, Gamma]
        custom_bands = [
            [0.5, 4.0],   # Delta
            [4.0, 8.0],   # Theta
            [8.0, 13.0],  # Alpha
            [13.0, 30.0], # Beta
            [30.0, 50.0], # Gamma
        ]
        focus_num_range = (13.0, 30.0) # Beta by default
        focus_den_range = (8.0, 13.0)  # Alpha by default
        calm_num_range  = (8.0, 13.0)  # Alpha by default
        calm_den_range  = (4.0, 8.0)   # Theta by default

        if hasattr(main_win, "mapping_tab") and main_win.mapping_tab:
            mt = main_win.mapping_tab
            blink_thresh = float(mt.blink_thresh_slider.value())
            clench_thresh = float(mt.clench_thresh_slider.value())
            focus_thresh = float(mt.focus_thresh_slider.value())
            calm_thresh = float(mt.calm_thresh_slider.value())
            
            if hasattr(mt, "eeg_notch_cb"):
                notch_mode = mt.eeg_notch_cb.currentData()
                if notch_mode == "custom" and hasattr(mt, "eeg_custom_notch_spin"):
                    notch_freq = float(mt.eeg_custom_notch_spin.value()) / 10.0
                else:
                    notch_freq = notch_mode
            if hasattr(mt, "eeg_notch_q"):
                notch_q = float(mt.eeg_notch_q.value())
            if hasattr(mt, "eeg_bandpass_cb"):
                bp = mt.eeg_bandpass_cb.currentData()
                if bp == "custom" and hasattr(mt, "eeg_hp_spin") and hasattr(mt, "eeg_lp_spin"):
                    hp_cutoff = float(mt.eeg_hp_spin.value()) / 10.0
                    lp_cutoff = float(mt.eeg_lp_spin.value()) / 10.0
                elif bp:
                    hp_cutoff, lp_cutoff = bp
                else:
                    hp_cutoff, lp_cutoff = None, None
            if hasattr(mt, "eeg_focus_ch_cb"):
                focus_ch_mode = mt.eeg_focus_ch_cb.currentData()
            if hasattr(mt, "eeg_ema_slider"):
                ema_alpha = float(mt.eeg_ema_slider.value()) / 100.0
            if hasattr(mt, "eeg_blink_ch_cb"):
                blink_ch_mode = mt.eeg_blink_ch_cb.currentData()
            if hasattr(mt, "eeg_clench_ch_cb"):
                clench_ch_mode = mt.eeg_clench_ch_cb.currentData()
            if hasattr(mt, "eeg_cooldown_slider"):
                cooldown_period = float(mt.eeg_cooldown_slider.value()) / 10.0

            # Pull custom arbitrary band limits if configured
            if hasattr(mt, "delta_min_spin") and hasattr(mt, "delta_max_spin"):
                custom_bands[0] = [float(mt.delta_min_spin.value()) / 10.0, float(mt.delta_max_spin.value()) / 10.0]
                custom_bands[1] = [float(mt.theta_min_spin.value()) / 10.0, float(mt.theta_max_spin.value()) / 10.0]
                custom_bands[2] = [float(mt.alpha_min_spin.value()) / 10.0, float(mt.alpha_max_spin.value()) / 10.0]
                custom_bands[3] = [float(mt.beta_min_spin.value()) / 10.0, float(mt.beta_max_spin.value()) / 10.0]
                custom_bands[4] = [float(mt.gamma_min_spin.value()) / 10.0, float(mt.gamma_max_spin.value()) / 10.0]

            if hasattr(mt, "focus_num_min") and hasattr(mt, "focus_num_max"):
                focus_num_range = (float(mt.focus_num_min.value()) / 10.0, float(mt.focus_num_max.value()) / 10.0)
                focus_den_range = (float(mt.focus_den_min.value()) / 10.0, float(mt.focus_den_max.value()) / 10.0)
                calm_num_range  = (float(mt.calm_num_min.value()) / 10.0, float(mt.calm_num_max.value()) / 10.0)
                calm_den_range  = (float(mt.calm_den_min.value()) / 10.0, float(mt.calm_den_max.value()) / 10.0)

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
        temporal_sig = (self.data_buffer[:, 0] + self.data_buffer[:, 3]) / 2.0
        freqs, psd, band_powers = hybrid_dsp_pipeline(
            temporal_sig, float(self.sfreq), custom_bands,
            notch_freq=notch_freq, notch_q=notch_q, highpass=hp_cutoff, lowpass=lp_cutoff
        )

        valid_idx = (freqs >= 0.5) & (freqs <= max(55.0, custom_bands[-1][1] + 5.0))
        self.fft_curve.setData(freqs[valid_idx], psd[valid_idx] + 1e-12)

        # Update bar chart with dynamic arbitrary custom band powers
        self.bar_items.setOpts(height=band_powers)

        # Extract target electrode signal for Focus & Calm
        if focus_ch_mode == "af8" and self.ch_count > 2:
            target_data = self.data_buffer[:, 2]
        elif focus_ch_mode == "af7" and self.ch_count > 1:
            target_data = self.data_buffer[:, 1]
        elif focus_ch_mode == "frontal_avg" and self.ch_count > 2:
            target_data = (self.data_buffer[:, 1] + self.data_buffer[:, 2]) / 2.0
        elif focus_ch_mode == "tp9" and self.ch_count > 0:
            target_data = self.data_buffer[:, 0]
        elif focus_ch_mode == "tp10" and self.ch_count > 3:
            target_data = self.data_buffer[:, 3]
        else:
            target_data = self.data_buffer[:, min(2, self.ch_count - 1)]

        f_freqs, f_psd = self.compute_single_channel_psd(
            target_data, self.sfreq, notch_freq=notch_freq, notch_q=notch_q, highpass=hp_cutoff, lowpass=lp_cutoff
        )
        
        # Calculate cognitive state ratios with completely arbitrary, customized frequency ranges
        focus_num_val = self.get_band_power(f_freqs, f_psd, focus_num_range[0], focus_num_range[1])
        focus_den_val = self.get_band_power(f_freqs, f_psd, focus_den_range[0], focus_den_range[1])
        raw_focus = focus_num_val / (focus_den_val + 1e-12)
        inst_focus = np.clip((raw_focus / 2.0) * 100, 0, 100)

        calm_num_val = self.get_band_power(f_freqs, f_psd, calm_num_range[0], calm_num_range[1])
        calm_den_val = self.get_band_power(f_freqs, f_psd, calm_den_range[0], calm_den_range[1])
        raw_calm = calm_num_val / (calm_den_val + 1e-12)
        inst_calm = np.clip((raw_calm / 1.5) * 100, 0, 100)

        # Smoothing with dynamic alpha
        self.smoothed_focus = (ema_alpha * inst_focus) + ((1.0 - ema_alpha) * self.smoothed_focus)
        self.smoothed_calm = (ema_alpha * inst_calm) + ((1.0 - ema_alpha) * self.smoothed_calm)

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
            self.state_desc_lbl.setText(f"Target electrode ({focus_ch_mode.upper()}) beta activation active. High cognitive engagement.")
        elif self.smoothed_calm > calm_thresh and self.smoothed_focus < focus_thresh - 10.0:
            self.state_title_lbl.setText("🧘 DEEP CALM / RELAXED")
            self.state_title_lbl.setStyleSheet("font-size: 16px; font-weight: bold; color: #d946ef;")
            self.state_desc_lbl.setText(f"Target electrode ({focus_ch_mode.upper()}) alpha synchronization active. Restful baseline state.")
        else:
            self.state_title_lbl.setText("🧠 NEUTRAL COGNITION")
            self.state_title_lbl.setStyleSheet("font-size: 16px; font-weight: bold; color: #ffffff;")
            self.state_desc_lbl.setText(f"Electrode {focus_ch_mode.upper()} tracking baseline smoothly with adaptive DSP.")

        # Blink & Clench checks
        if blink_ch_mode == "avg" and self.ch_count > 2:
            blink_sig = (self.data_buffer[:, 1] + self.data_buffer[:, 2]) / 2.0
        elif isinstance(blink_ch_mode, int) and blink_ch_mode < self.ch_count:
            blink_sig = self.data_buffer[:, blink_ch_mode]
        else:
            blink_sig = self.data_buffer[:, min(1, self.ch_count - 1)]

        if clench_ch_mode == "avg" and self.ch_count > 3:
            clench_sig = (self.data_buffer[:, 0] + self.data_buffer[:, 3]) / 2.0
        elif isinstance(clench_ch_mode, int) and clench_ch_mode < self.ch_count:
            clench_sig = self.data_buffer[:, clench_ch_mode]
        else:
            clench_sig = self.data_buffer[:, 0]

        blink_clean = blink_sig - np.mean(blink_sig)
        clench_clean = clench_sig - np.mean(clench_sig)
        
        window_blink = int(self.sfreq * 0.3)
        b_val = float(np.max(blink_clean[-window_blink:]) - np.min(blink_clean[-window_blink:]))
        c_val = float(np.std(clench_clean))
        self.blink_val = b_val
        self.clench_val = c_val
        
        now = time.time()
        if not hasattr(self, "last_trigger_time"):
            self.last_trigger_time = 0.0
            
        if now - self.last_trigger_time > cooldown_period:
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

        # ── Persistent Cognitive Buff States ──────────────────────────────────
        # Focus/Calm are sustained physiological states, not discrete events.
        # Instead of one-shot triggers, we drive persistent gameplay buffs:
        #   High Focus  → Shield buff: halves incoming damage while active
        #   Deep Calm   → Missile buff: fires a Smart Bomb every 5 seconds
        # ──────────────────────────────────────────────────────────────────────
        if not hasattr(self, "_focus_buff_active"): self._focus_buff_active = False
        if not hasattr(self, "_calm_buff_active"):  self._calm_buff_active  = False
        if not hasattr(self, "_calm_last_missile"):  self._calm_last_missile = 0.0

        main_win = self.window()
        if hasattr(main_win, "send_command_to_snake_game"):
            # ── High Focus → Shield Buff ──
            if self.smoothed_focus > focus_thresh:
                if not self._focus_buff_active:
                    self._focus_buff_active = True
                    main_win.send_command_to_snake_game("focus_shield_on")
                    main_win.append_system_log("🧠 FOCUS BUFF ACTIVATED — Shield halves incoming damage")
            elif self.smoothed_focus < focus_thresh - 10.0:
                if self._focus_buff_active:
                    self._focus_buff_active = False
                    main_win.send_command_to_snake_game("focus_shield_off")
                    main_win.append_system_log("🧠 Focus buff faded — Shield deactivated")

            # ── Deep Calm → Periodic Missile Buff ──
            if self.smoothed_calm > calm_thresh:
                if not self._calm_buff_active:
                    self._calm_buff_active = True
                    main_win.append_system_log("🧘 CALM BUFF ACTIVATED — Smart Bomb every 5s while relaxed")
                # Fire a Smart Bomb every 5 seconds while in calm state
                if now - self._calm_last_missile >= 5.0:
                    main_win.send_command_to_snake_game("tert_action")
                    self._calm_last_missile = now
                    main_win.append_system_log("🧘 Calm Missile launched!")
            elif self.smoothed_calm < calm_thresh - 10.0:
                if self._calm_buff_active:
                    self._calm_buff_active = False
                    main_win.append_system_log("🧘 Calm buff faded — no longer relaxed enough")
        # ──────────────────────────────────────────────────────────────────────

    def compute_single_channel_psd(self, data, sfreq, notch_freq=50.0, notch_q=30.0, highpass=1.0, lowpass=50.0):
        N = len(data)
        clean_data = data
        # Notch filter
        if notch_freq is not None and notch_freq > 0 and notch_freq < (sfreq / 2.0):
            try:
                b, a = iirnotch(notch_freq, notch_q, sfreq)
                clean_data = lfilter(b, a, clean_data)
            except Exception:
                pass
        # Bandpass filter
        if highpass is not None and highpass > 0 and lowpass is not None and lowpass > highpass and lowpass < (sfreq / 2.0):
            try:
                b_band, a_band = butter(2, [highpass, lowpass], btype="bandpass", fs=sfreq)
                clean_data = lfilter(b_band, a_band, clean_data)
            except Exception:
                pass
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
        self.shake_threshold_slider.setRange(1, 100000) # translates to 0.1 to 10000.0 m/s^2
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
                        elif 50.0 < voice_val < 60.0:
                            voice_cmd = "fire"
                            break
                
                if voice_cmd == "left":
                    cmd = main_win.get_trigger_action("Voice Command 'Left'")
                    if cmd:
                        main_win.send_command_to_snake_game(cmd)
                elif voice_cmd == "right":
                    cmd = main_win.get_trigger_action("Voice Command 'Right'")
                    if cmd:
                        main_win.send_command_to_snake_game(cmd)
                elif voice_cmd == "fire":
                    cmd = main_win.get_trigger_action("Voice Command 'Fire'")
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
        ch_idx = 1 if data.shape[1] > 1 else 0
        if num_new >= self.win_samples:
            self.buffer_ir[:] = data[-self.win_samples:, ch_idx]
        else:
            self.buffer_ir[:-num_new] = self.buffer_ir[num_new:]
            self.buffer_ir[-num_new:] = data[:, ch_idx]

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
    import sys
    ips = [("All Interfaces (0.0.0.0)", "0.0.0.0")]
    seen = {"0.0.0.0", "127.0.0.1"}
    try:
        import socket
        # 1. Primary socket connection
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(('8.8.8.8', 1))
            primary_ip = s.getsockname()[0]
            if primary_ip not in seen and not primary_ip.startswith("127."):
                ips.append((f"Primary LAN/Wi-Fi: {primary_ip}", primary_ip))
                seen.add(primary_ip)
        except Exception:
            pass
        finally:
            s.close()

        # 2. Hostname lookup
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None):
            ip = info[4][0]
            if "." in ip and not ip.startswith("127.") and ip not in seen:
                ips.append((f"Interface IP: {ip}", ip))
                seen.add(ip)

        # 3. Windows ipconfig scan (Discovers Mobile Hotspot 192.168.137.1, etc.)
        if sys.platform == "win32":
            try:
                import subprocess, re
                out = subprocess.check_output(["ipconfig"], text=True, errors="replace")
                for match in re.finditer(r"IPv4 Address[.\s]+:\s*([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)", out):
                    ip = match.group(1).strip()
                    if ip not in seen and not ip.startswith("127."):
                        label = f"📱 Mobile Hotspot ({ip})" if ip.startswith("192.168.137.") else f"Adapter IP: {ip}"
                        ips.append((label, ip))
                        seen.add(ip)
            except Exception:
                pass
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

        # Left Column: Config Panel wrapped in Scroll Area
        left_scroll = QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setStyleSheet("background-color: transparent; border: none;")

        left_panel = QFrame()
        left_panel.setStyleSheet("background-color: #1e293b; border-radius: 12px; border: 1px solid #334155;")
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(18, 18, 18, 18)
        left_layout.setSpacing(12)

        lbl_title = QLabel("🎮 BCI Controller & DSP Tuning")
        lbl_title.setStyleSheet("font-size: 18px; font-weight: bold; color: #38bdf8; border: none;")
        left_layout.addWidget(lbl_title)

        lbl_desc = QLabel("Fine-tune Muse EEG filters, signal DSP parameters, and map sensor inputs to game controls.")
        lbl_desc.setStyleSheet("font-size: 12px; color: #94a3b8; border: none;")
        lbl_desc.setWordWrap(True)
        left_layout.addWidget(lbl_desc)

        # ── PROFILE PRESETS & IMPORT / EXPORT TOOLBAR ──
        profile_bar = QFrame()
        profile_bar.setStyleSheet("background-color: #0f172a; border-radius: 8px; border: 1px solid #334155;")
        profile_layout = QHBoxLayout(profile_bar)
        profile_layout.setContentsMargins(10, 8, 10, 8)
        profile_layout.setSpacing(8)

        lbl_prof = QLabel("Profile:")
        lbl_prof.setStyleSheet("color: #38bdf8; font-size: 11px; font-weight: bold; border: none;")
        profile_layout.addWidget(lbl_prof)

        self.preset_combo = QComboBox()
        self.preset_combo.addItem("Select Preset Profile...", None)
        self.preset_combo.addItem("🎮 Action Gaming & Reflex (Fast, High-Sens)", "gaming")
        self.preset_combo.addItem("🧘 Deep Meditation & Relaxation (Alpha-Theta)", "meditation")
        self.preset_combo.addItem("🧠 Peak Focus & SMR Cognitive Work", "focus")
        self.preset_combo.addItem("⚖️ Default Balanced Workshop Setup", "default")
        self.preset_combo.setStyleSheet("background-color: #1e293b; color: #f1f5f9; border: 1px solid #475569; padding: 4px; border-radius: 4px; font-size: 11px;")
        self.preset_combo.currentIndexChanged.connect(self.apply_preset)
        profile_layout.addWidget(self.preset_combo, stretch=1)

        self.btn_export = QPushButton("💾 Export JSON")
        self.btn_export.setStyleSheet("""
            QPushButton {
                background-color: #065f46;
                color: #6ee7b7;
                font-weight: bold;
                border-radius: 4px;
                padding: 4px 10px;
                font-size: 11px;
                border: 1px solid #059669;
            }
            QPushButton:hover {
                background-color: #047857;
            }
        """)
        self.btn_export.clicked.connect(self.export_settings)
        profile_layout.addWidget(self.btn_export)

        self.btn_import = QPushButton("📂 Import JSON")
        self.btn_import.setStyleSheet("""
            QPushButton {
                background-color: #1e3a8a;
                color: #93c5fd;
                font-weight: bold;
                border-radius: 4px;
                padding: 4px 10px;
                font-size: 11px;
                border: 1px solid #2563eb;
            }
            QPushButton:hover {
                background-color: #1d4ed8;
            }
        """)
        self.btn_import.clicked.connect(self.import_settings)
        profile_layout.addWidget(self.btn_import)

        left_layout.addWidget(profile_bar)
        left_layout.addSpacing(6)

        # ── 1. ACTION MAPPING GRID ──────────────────
        map_group = QGroupBox("🕹️ Controller Action Mapping")
        map_group.setStyleSheet("""
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
        """)
        map_layout = QVBoxLayout(map_group)
        grid = QGridLayout()
        grid.setSpacing(6)

        # Game action options for each input trigger
        self.action_options = [
            "Disabled (None)",
            "Move Left (left)",
            "Move Right (right)",
            "Move Up (up)",
            "Move Down (down)",
            "Primary Action (Fire / Pause)",
            "Secondary Action (Spread / Food)",
            "Tertiary Action (Smart Bomb / Color)"
        ]

        self.action_value_map = {
            "Disabled (None)": None,
            "Move Left (left)": "left",
            "Move Right (right)": "right",
            "Move Up (up)": "up",
            "Move Down (down)": "down",
            "Primary Action (Fire / Pause)": "action",
            "Secondary Action (Spread / Food)": "sec_action",
            "Tertiary Action (Smart Bomb / Color)": "tert_action"
        }

        # Value to combo item text lookup
        self.action_key_map = {
            None: "Disabled (None)",
            "none": "Disabled (None)",
            "disabled": "Disabled (None)",
            "left": "Move Left (left)",
            "right": "Move Right (right)",
            "up": "Move Up (up)",
            "down": "Move Down (down)",
            "action": "Primary Action (Fire / Pause)",
            "sec_action": "Secondary Action (Spread / Food)",
            "sec": "Secondary Action (Spread / Food)",
            "tert_action": "Tertiary Action (Smart Bomb / Color)",
            "tert": "Tertiary Action (Smart Bomb / Color)"
        }

        # All available trigger inputs
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
            "Voice Command 'Right'",
            "Voice Command 'Fire'"
        ]

        # Default mapping per input trigger
        default_trigger_actions = {
            "Muse Eye Blink": "Tertiary Action (Smart Bomb / Color)",
            "Muse Jaw Clench": "Disabled (None)",
            "Muse High Focus": "Disabled (None)",
            "Muse Deep Calm": "Disabled (None)",
            "Phone Tilt Left": "Move Left (left)",
            "Phone Tilt Right": "Move Right (right)",
            "Phone Tilt Forward": "Move Up (up)",
            "Phone Tilt Backward": "Move Down (down)",
            "Phone Flick Left": "Disabled (None)",
            "Phone Flick Right": "Disabled (None)",
            "Phone Flick Up": "Disabled (None)",
            "Phone Flick Down": "Disabled (None)",
            "Phone Screen Tap": "Primary Action (Fire / Pause)",
            "Phone Shake": "Secondary Action (Spread / Food)",
            "Voice Command 'Left'": "Disabled (None)",
            "Voice Command 'Right'": "Disabled (None)",
            "Voice Command 'Fire'": "Disabled (None)"
        }

        self.input_map_combos = {}

        def create_input_map_row(input_name, default_action_text, row_idx):
            lbl = QLabel(input_name + ":")
            lbl.setStyleSheet("color: #cbd5e1; font-size: 11px; border: none; font-weight: bold;")
            grid.addWidget(lbl, row_idx, 0)
            cb = QComboBox()
            cb.addItems(self.action_options)
            cb.setCurrentText(default_action_text)
            cb.setStyleSheet("background-color: #0f172a; color: #cbd5e1; border: 1px solid #475569; padding: 3px; border-radius: 4px; font-size: 11px;")
            grid.addWidget(cb, row_idx, 1)
            self.input_map_combos[input_name] = cb
            return cb

        for r_idx, input_name in enumerate(self.options):
            default_act = default_trigger_actions.get(input_name, "Disabled (None)")
            create_input_map_row(input_name, default_act, r_idx)

        map_layout.addLayout(grid)
        left_layout.addWidget(map_group)

        # ── 2. SENSITIVITY THRESHOLDS GROUP ─────────
        thresh_group = QGroupBox("🎛️ Sensitivity Thresholds")
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

        class FloatFormattedSpinBox(QSpinBox):
            def __init__(self, divisor=1.0, precision=1, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.divisor = divisor
                self.precision = precision

            def textFromValue(self, value):
                return f"{value / self.divisor:.{self.precision}f}"

            def valueFromText(self, text):
                try:
                    clean_text = "".join(c for c in text if c.isdigit() or c == '.' or c == '-')
                    return int(float(clean_text) * self.divisor)
                except ValueError:
                    return self.value()

        def create_slider(parent_layout, label_text, min_val, max_val, default_val, recommended_str, divisor=1.0, precision=0, unit=""):
            row_layout = QHBoxLayout()
            lbl = QLabel(label_text)
            lbl.setStyleSheet("color: #cbd5e1; font-size: 11px;")
            
            if divisor != 1.0:
                spin = FloatFormattedSpinBox(divisor=divisor, precision=precision)
            else:
                spin = QSpinBox()
                
            spin.setRange(min_val, max_val)
            spin.setValue(default_val)
            if unit and divisor == 1.0:
                spin.setSuffix(unit)
            spin.setMinimumHeight(26)
            spin.setFixedWidth(90)
            spin.setStyleSheet("""
                QSpinBox {
                    background-color: #0f172a;
                    color: #e2e8f0;
                    border: 1px solid #334155;
                    border-radius: 6px;
                    padding: 2px 6px;
                    font-size: 11px;
                    font-weight: bold;
                }
                QSpinBox:focus {
                    border: 1px solid #38bdf8;
                }
            """)
            
            recom_lbl = QLabel(f"Rec: {recommended_str}")
            recom_lbl.setStyleSheet("color: #64748b; font-size: 10px; border: none; font-style: italic;")
            recom_lbl.setFixedWidth(120)
            recom_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
            
            row_layout.addWidget(lbl)
            row_layout.addWidget(spin, stretch=1)
            row_layout.addWidget(recom_lbl)
            parent_layout.addLayout(row_layout)
            return spin

        def create_combo_row(parent_layout, label_text, items, default_idx=0):
            row = QHBoxLayout()
            lbl = QLabel(label_text)
            lbl.setStyleSheet("color: #cbd5e1; font-size: 11px;")
            cb = QComboBox()
            for name, val in items:
                cb.addItem(name, val)
            cb.setCurrentIndex(default_idx)
            cb.setStyleSheet("background-color: #0f172a; color: #cbd5e1; border: 1px solid #475569; padding: 3px 6px; border-radius: 4px; font-size: 11px;")
            cb.setMinimumHeight(26)
            row.addWidget(lbl)
            row.addWidget(cb, stretch=1)
            parent_layout.addLayout(row)
            return cb

        self.blink_thresh_slider = create_slider(thresh_layout, "Muse Eye Blink:", 0, 100000, 80, "No limits (0-100k)", unit=" uV")
        self.eeg_blink_ch_cb = create_combo_row(thresh_layout, "  ↳ Blink Channel (🟡 yellow):", [
            ("AF7 (Left Frontal — Standard)", 1),
            ("AF8 (Right Frontal)", 2),
            ("Frontal Avg (AF7 + AF8)", "avg"),
        ], default_idx=0)

        self.clench_thresh_slider = create_slider(thresh_layout, "Muse Jaw Clench:", 0, 100000, 60, "No limits (0-100k)", unit=" uV")
        self.eeg_clench_ch_cb = create_combo_row(thresh_layout, "  ↳ Clench Channel (🔴 red):", [
            ("TP9 (Left Ear — Standard)", 0),
            ("TP10 (Right Ear)", 3),
            ("Temporal Avg (TP9 + TP10)", "avg"),
        ], default_idx=0)
        self.focus_thresh_slider = create_slider(thresh_layout, "Muse High Focus:", 0, 10000, 60, "No limits (0-10k)", unit="%")
        self.calm_thresh_slider = create_slider(thresh_layout, "Muse Deep Calm:", 0, 10000, 60, "No limits (0-10k)", unit="%")
        self.tilt_thresh_slider = create_slider(thresh_layout, "Phone Tilt Angle:", 0, 360, 15, "0-360°", unit="°")
        self.flick_thresh_slider = create_slider(thresh_layout, "Phone Flick:", 1, 100000, 35, "0.01-1000+", divisor=100.0, precision=2) 
        self.shake_thresh_slider = create_slider(thresh_layout, "Phone Shake:", 1, 100000, 80, "0.1-10000+", divisor=10.0, precision=1)
        self.shake_thresh_slider.valueChanged.connect(self.sync_imu_shake_threshold)

        left_layout.addWidget(thresh_group)

        # ── 3. MUSE EEG DSP & FILTER FINE-TUNING ────
        dsp_group = QGroupBox("🧠 Muse EEG Filter & Signal DSP Tuning")
        dsp_group.setStyleSheet("""
            QGroupBox {
                color: #a855f7;
                font-weight: bold;
                border: 1px solid #4c1d95;
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
        dsp_layout = QVBoxLayout(dsp_group)
        dsp_layout.setSpacing(6)

        self.eeg_notch_cb = create_combo_row(dsp_layout, "Mains Hum Notch:", [
            ("50 Hz (UK / EU / Asia / AUS)", 50.0),
            ("60 Hz (Americas / US / JP)", 60.0),
            ("Custom Notch Frequency", "custom"),
            ("Disabled (Raw Signals)", None),
        ], default_idx=0)

        # Custom Notch direct numeric input
        custom_notch_row = QHBoxLayout()
        lbl_cnotch = QLabel("  ↳ Custom Notch Hz:")
        lbl_cnotch.setStyleSheet("color: #94a3b8; font-size: 10px;")
        self.eeg_custom_notch_spin = FloatFormattedSpinBox(divisor=10.0, precision=1)
        self.eeg_custom_notch_spin.setRange(10, 2500) # 1.0 Hz - 250.0 Hz
        self.eeg_custom_notch_spin.setValue(500)      # 50.0 Hz default
        self.eeg_custom_notch_spin.setSuffix(" Hz")
        self.eeg_custom_notch_spin.setFixedWidth(85)
        self.eeg_custom_notch_spin.setStyleSheet("background-color: #0f172a; color: #cbd5e1; border: 1px solid #334155; border-radius: 4px; padding: 2px 4px; font-size: 10px;")
        custom_notch_row.addWidget(lbl_cnotch)
        custom_notch_row.addWidget(self.eeg_custom_notch_spin)
        custom_notch_row.addStretch(1)
        dsp_layout.addLayout(custom_notch_row)

        self.eeg_notch_q = create_slider(dsp_layout, "Notch Sharpness (Q):", 10, 60, 30, "20-40")

        self.eeg_bandpass_cb = create_combo_row(dsp_layout, "Bandpass Pre-Filter:", [
            ("1.0 - 45.0 Hz (Standard EEG)", (1.0, 45.0)),
            ("0.5 - 40.0 Hz (Wide Spectrum)", (0.5, 40.0)),
            ("2.0 - 30.0 Hz (Focus/Alpha Band)", (2.0, 30.0)),
            ("Custom High/Low Cutoffs", "custom"),
            ("Off (Raw FFT without Bandpass)", None),
        ], default_idx=0)

        # Custom HP/LP cutoffs
        custom_bp_row = QHBoxLayout()
        lbl_cbp = QLabel("  ↳ Custom HP / LP:")
        lbl_cbp.setStyleSheet("color: #94a3b8; font-size: 10px;")
        self.eeg_hp_spin = FloatFormattedSpinBox(divisor=10.0, precision=1)
        self.eeg_hp_spin.setRange(1, 500)  # 0.1 Hz - 50.0 Hz
        self.eeg_hp_spin.setValue(10)      # 1.0 Hz
        self.eeg_hp_spin.setSuffix(" Hz")
        self.eeg_hp_spin.setFixedWidth(70)
        self.eeg_hp_spin.setStyleSheet("background-color: #0f172a; color: #cbd5e1; border: 1px solid #334155; border-radius: 4px; padding: 2px 4px; font-size: 10px;")

        lbl_slash = QLabel("to")
        lbl_slash.setStyleSheet("color: #64748b; font-size: 10px;")

        self.eeg_lp_spin = FloatFormattedSpinBox(divisor=10.0, precision=1)
        self.eeg_lp_spin.setRange(10, 1200) # 1.0 Hz - 120.0 Hz
        self.eeg_lp_spin.setValue(450)      # 45.0 Hz
        self.eeg_lp_spin.setSuffix(" Hz")
        self.eeg_lp_spin.setFixedWidth(75)
        self.eeg_lp_spin.setStyleSheet("background-color: #0f172a; color: #cbd5e1; border: 1px solid #334155; border-radius: 4px; padding: 2px 4px; font-size: 10px;")

        custom_bp_row.addWidget(lbl_cbp)
        custom_bp_row.addWidget(self.eeg_hp_spin)
        custom_bp_row.addWidget(lbl_slash)
        custom_bp_row.addWidget(self.eeg_lp_spin)
        custom_bp_row.addStretch(1)
        dsp_layout.addLayout(custom_bp_row)

        self.eeg_focus_ch_cb = create_combo_row(dsp_layout, "Cognitive Target Ch:", [
            ("AF8 (Right Frontal - Recommended)", "af8"),
            ("AF7 (Left Frontal)", "af7"),
            ("Frontal Avg (AF7 + AF8)", "frontal_avg"),
            ("TP9 (Left Ear / Temporal)", "tp9"),
            ("TP10 (Right Ear / Temporal)", "tp10"),
        ], default_idx=0)

        self.eeg_ema_slider = create_slider(dsp_layout, "Cognitive Smoothing Rate:", 1, 25, 4, "0.02-0.08", divisor=100.0, precision=2)

        self.eeg_cooldown_slider = create_slider(dsp_layout, "Trigger Cooldown (Refractory):", 2, 25, 8, "0.5-1.2 s", divisor=10.0, precision=1, unit=" s")

        left_layout.addWidget(dsp_group)

        # ── 4. CUSTOM UNCONSTRAINED FREQUENCY BANDS ──
        bands_group = QGroupBox("📊 Custom Frequency Bands & Ratio Limits")
        bands_group.setStyleSheet("""
            QGroupBox {
                color: #38bdf8;
                font-weight: bold;
                border: 1px solid #0369a1;
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
        bands_layout = QVBoxLayout(bands_group)
        bands_layout.setSpacing(5)

        def create_band_range_row(parent_layout, label_text, min_d, max_d, min_init, max_init):
            row = QHBoxLayout()
            lbl = QLabel(label_text)
            lbl.setStyleSheet("color: #cbd5e1; font-size: 11px; font-weight: bold;")
            lbl.setFixedWidth(65)

            spin_min = FloatFormattedSpinBox(divisor=10.0, precision=1)
            spin_min.setRange(min_d, max_d)
            spin_min.setValue(min_init)
            spin_min.setSuffix(" Hz")
            spin_min.setFixedWidth(68)
            spin_min.setStyleSheet("background-color: #0f172a; color: #e2e8f0; border: 1px solid #334155; border-radius: 4px; padding: 2px 4px; font-size: 10px;")

            dash = QLabel("—")
            dash.setStyleSheet("color: #64748b; font-size: 10px;")

            spin_max = FloatFormattedSpinBox(divisor=10.0, precision=1)
            spin_max.setRange(min_d, max_d)
            spin_max.setValue(max_init)
            spin_max.setSuffix(" Hz")
            spin_max.setFixedWidth(68)
            spin_max.setStyleSheet("background-color: #0f172a; color: #e2e8f0; border: 1px solid #334155; border-radius: 4px; padding: 2px 4px; font-size: 10px;")

            row.addWidget(lbl)
            row.addWidget(spin_min)
            row.addWidget(dash)
            row.addWidget(spin_max)
            row.addStretch(1)
            parent_layout.addLayout(row)
            return spin_min, spin_max

        # Custom limits for standard 5 bands
        self.delta_min_spin, self.delta_max_spin = create_band_range_row(bands_layout, "Delta:", 1, 150, 5, 40)
        self.theta_min_spin, self.theta_max_spin = create_band_range_row(bands_layout, "Theta:", 10, 200, 40, 80)
        self.alpha_min_spin, self.alpha_max_spin = create_band_range_row(bands_layout, "Alpha:", 40, 250, 80, 130)
        self.beta_min_spin,  self.beta_max_spin  = create_band_range_row(bands_layout, "Beta:",  80, 500, 130, 300)
        self.gamma_min_spin, self.gamma_max_spin = create_band_range_row(bands_layout, "Gamma:", 200, 1000, 300, 500)

        bands_layout.addSpacing(4)
        lbl_ratios_hdr = QLabel("Cognitive Formula Frequency Windows:")
        lbl_ratios_hdr.setStyleSheet("color: #f59e0b; font-size: 10px; font-weight: bold;")
        bands_layout.addWidget(lbl_ratios_hdr)

        self.focus_num_min, self.focus_num_max = create_band_range_row(bands_layout, "Focus Num:", 10, 800, 130, 300)
        self.focus_den_min, self.focus_den_max = create_band_range_row(bands_layout, "Focus Den:", 10, 800, 80, 130)
        self.calm_num_min,  self.calm_num_max  = create_band_range_row(bands_layout, "Calm Num:",  10, 800, 80, 130)
        self.calm_den_min,  self.calm_den_max  = create_band_range_row(bands_layout, "Calm Den:",  10, 800, 40, 80)

        left_layout.addWidget(bands_group)
        left_layout.addSpacing(6)

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
        left_scroll.setWidget(left_panel)
        layout.addWidget(left_scroll, stretch=1)

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
        self.scope_widget.enableAutoRange(axis='y')
        self.scope_widget.addLegend(offset=(10, 10))

        # Track 6 curves: Focus, Calm, Blink, Clench, Tilt, Shake
        self.history_len = 100
        self.focus_hist = np.zeros(self.history_len)
        self.calm_hist = np.zeros(self.history_len)
        self.blink_hist = np.zeros(self.history_len)
        self.clench_hist = np.zeros(self.history_len)
        self.tilt_hist = np.zeros(self.history_len)
        self.shake_hist = np.zeros(self.history_len)

        # Color curves — names appear in the legend
        self.curve_focus = self.scope_widget.plot(pen=pg.mkPen('#06b6d4', width=2), name="🔵 Focus %")
        self.curve_calm = self.scope_widget.plot(pen=pg.mkPen('#d946ef', width=2), name="🟣 Calm %")
        self.curve_blink = self.scope_widget.plot(pen=pg.mkPen('#fbbf24', width=2), name="🟡 Blink uV (eye blink)")
        self.curve_clench = self.scope_widget.plot(pen=pg.mkPen('#ef4444', width=2), name="🔴 Clench uV (jaw clench)")
        self.curve_tilt = self.scope_widget.plot(pen=pg.mkPen('#10b981', width=2), name="🟢 Tilt Angle°")
        self.curve_shake = self.scope_widget.plot(pen=pg.mkPen('#f97316', width=2), name="🟠 Shake Force")

        
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

        if hasattr(self, "input_map_combos") and source_input in self.input_map_combos:
            combo_text = self.input_map_combos[source_input].currentText()
            return self.action_value_map.get(combo_text, None)

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

    def get_settings_dict(self):
        """Serializes all controller mappings, thresholds, custom bands, and DSP filters to a dict."""
        input_mappings = {}
        if hasattr(self, "input_map_combos"):
            for inp_name, cb in self.input_map_combos.items():
                input_mappings[inp_name] = cb.currentText()

        return {
            "version": "2.0",
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "input_mappings": input_mappings,
            "thresholds": {
                "blink_uv": self.blink_thresh_slider.value(),
                "clench_uv": self.clench_thresh_slider.value(),
                "focus_pct": self.focus_thresh_slider.value(),
                "calm_pct": self.calm_thresh_slider.value(),
                "tilt_deg": self.tilt_thresh_slider.value(),
                "flick": self.flick_thresh_slider.value() / 100.0,
                "shake": self.shake_thresh_slider.value() / 10.0,
            },
            "dsp": {
                "notch_index": self.eeg_notch_cb.currentIndex(),
                "custom_notch_hz": self.eeg_custom_notch_spin.value() / 10.0,
                "notch_q": self.eeg_notch_q.value(),
                "bandpass_index": self.eeg_bandpass_cb.currentIndex(),
                "custom_hp_hz": self.eeg_hp_spin.value() / 10.0,
                "custom_lp_hz": self.eeg_lp_spin.value() / 10.0,
                "focus_target_index": self.eeg_focus_ch_cb.currentIndex(),
                "smoothing_alpha": self.eeg_ema_slider.value() / 100.0,
                "blink_ch_index": self.eeg_blink_ch_cb.currentIndex(),
                "clench_ch_index": self.eeg_clench_ch_cb.currentIndex(),
                "cooldown_sec": self.eeg_cooldown_slider.value() / 10.0,
            },
            "frequency_bands": {
                "delta": [self.delta_min_spin.value() / 10.0, self.delta_max_spin.value() / 10.0],
                "theta": [self.theta_min_spin.value() / 10.0, self.theta_max_spin.value() / 10.0],
                "alpha": [self.alpha_min_spin.value() / 10.0, self.alpha_max_spin.value() / 10.0],
                "beta":  [self.beta_min_spin.value() / 10.0,  self.beta_max_spin.value() / 10.0],
                "gamma": [self.gamma_min_spin.value() / 10.0, self.gamma_max_spin.value() / 10.0],
            },
            "cognitive_ratios": {
                "focus_num": [self.focus_num_min.value() / 10.0, self.focus_num_max.value() / 10.0],
                "focus_den": [self.focus_den_min.value() / 10.0, self.focus_den_max.value() / 10.0],
                "calm_num":  [self.calm_num_min.value() / 10.0,  self.calm_num_max.value() / 10.0],
                "calm_den":  [self.calm_den_min.value() / 10.0,  self.calm_den_max.value() / 10.0],
            }
        }

    def apply_settings_dict(self, cfg):
        """Restores mapping, threshold, and DSP configuration from a dictionary."""
        try:
            # Support both new v2.0 input_mappings and legacy v1.0 mappings
            if "input_mappings" in cfg and hasattr(self, "input_map_combos"):
                for inp_name, action_str in cfg["input_mappings"].items():
                    if inp_name in self.input_map_combos:
                        if action_str in self.action_options:
                            self.input_map_combos[inp_name].setCurrentText(action_str)
                        elif action_str in self.action_key_map:
                            self.input_map_combos[inp_name].setCurrentText(self.action_key_map[action_str])
            elif "mappings" in cfg and hasattr(self, "input_map_combos"):
                # Reset all to disabled first for legacy import
                for cb in self.input_map_combos.values():
                    cb.setCurrentText("Disabled (None)")
                legacy_map = {
                    "left": "Move Left (left)",
                    "right": "Move Right (right)",
                    "up": "Move Up (up)",
                    "down": "Move Down (down)",
                    "action": "Primary Action (Fire / Pause)",
                    "sec": "Secondary Action (Spread / Food)",
                    "tert": "Tertiary Action (Smart Bomb / Color)"
                }
                for action_key, trigger_name in cfg["mappings"].items():
                    if trigger_name in self.input_map_combos and action_key in legacy_map:
                        self.input_map_combos[trigger_name].setCurrentText(legacy_map[action_key])

            if "thresholds" in cfg:
                t = cfg["thresholds"]
                if "blink_uv" in t: self.blink_thresh_slider.setValue(int(t["blink_uv"]))
                if "clench_uv" in t: self.clench_thresh_slider.setValue(int(t["clench_uv"]))
                if "focus_pct" in t: self.focus_thresh_slider.setValue(int(t["focus_pct"]))
                if "calm_pct" in t: self.calm_thresh_slider.setValue(int(t["calm_pct"]))
                if "tilt_deg" in t: self.tilt_thresh_slider.setValue(int(t["tilt_deg"]))
                if "flick" in t: self.flick_thresh_slider.setValue(int(float(t["flick"]) * 100))
                if "shake" in t: self.shake_thresh_slider.setValue(int(float(t["shake"]) * 10))

            if "dsp" in cfg:
                d = cfg["dsp"]
                if "notch_index" in d and d["notch_index"] < self.eeg_notch_cb.count():
                    self.eeg_notch_cb.setCurrentIndex(d["notch_index"])
                if "custom_notch_hz" in d:
                    self.eeg_custom_notch_spin.setValue(int(d["custom_notch_hz"] * 10))
                if "notch_q" in d:
                    self.eeg_notch_q.setValue(int(d["notch_q"]))
                if "bandpass_index" in d and d["bandpass_index"] < self.eeg_bandpass_cb.count():
                    self.eeg_bandpass_cb.setCurrentIndex(d["bandpass_index"])
                if "custom_hp_hz" in d:
                    self.eeg_hp_spin.setValue(int(d["custom_hp_hz"] * 10))
                if "custom_lp_hz" in d:
                    self.eeg_lp_spin.setValue(int(d["custom_lp_hz"] * 10))
                if "focus_target_index" in d and d["focus_target_index"] < self.eeg_focus_ch_cb.count():
                    self.eeg_focus_ch_cb.setCurrentIndex(d["focus_target_index"])
                if "smoothing_alpha" in d:
                    self.eeg_ema_slider.setValue(int(d["smoothing_alpha"] * 100))
                if "blink_ch_index" in d and hasattr(self, "eeg_blink_ch_cb") and d["blink_ch_index"] < self.eeg_blink_ch_cb.count():
                    self.eeg_blink_ch_cb.setCurrentIndex(d["blink_ch_index"])
                if "clench_ch_index" in d and hasattr(self, "eeg_clench_ch_cb") and d["clench_ch_index"] < self.eeg_clench_ch_cb.count():
                    self.eeg_clench_ch_cb.setCurrentIndex(d["clench_ch_index"])
                if "cooldown_sec" in d:
                    self.eeg_cooldown_slider.setValue(int(d["cooldown_sec"] * 10))

            if "frequency_bands" in cfg:
                fb = cfg["frequency_bands"]
                if "delta" in fb:
                    self.delta_min_spin.setValue(int(fb["delta"][0] * 10))
                    self.delta_max_spin.setValue(int(fb["delta"][1] * 10))
                if "theta" in fb:
                    self.theta_min_spin.setValue(int(fb["theta"][0] * 10))
                    self.theta_max_spin.setValue(int(fb["theta"][1] * 10))
                if "alpha" in fb:
                    self.alpha_min_spin.setValue(int(fb["alpha"][0] * 10))
                    self.alpha_max_spin.setValue(int(fb["alpha"][1] * 10))
                if "beta" in fb:
                    self.beta_min_spin.setValue(int(fb["beta"][0] * 10))
                    self.beta_max_spin.setValue(int(fb["beta"][1] * 10))
                if "gamma" in fb:
                    self.gamma_min_spin.setValue(int(fb["gamma"][0] * 10))
                    self.gamma_max_spin.setValue(int(fb["gamma"][1] * 10))

            if "cognitive_ratios" in cfg:
                cr = cfg["cognitive_ratios"]
                if "focus_num" in cr:
                    self.focus_num_min.setValue(int(cr["focus_num"][0] * 10))
                    self.focus_num_max.setValue(int(cr["focus_num"][1] * 10))
                if "focus_den" in cr:
                    self.focus_den_min.setValue(int(cr["focus_den"][0] * 10))
                    self.focus_den_max.setValue(int(cr["focus_den"][1] * 10))
                if "calm_num" in cr:
                    self.calm_num_max.setValue(int(float(cr["calm_num"][1]) * 10))
                if "calm_den" in cr:
                    self.calm_den_min.setValue(int(float(cr["calm_den"][0]) * 10))
                    self.calm_den_max.setValue(int(float(cr["calm_den"][1]) * 10))

            main_win = self.window()
            if hasattr(main_win, "append_system_log"):
                main_win.append_system_log("Applied BCI Controller configuration profile.")
        except Exception as e:
            QMessageBox.warning(self, "Import Error", f"Failed to apply configuration:\n{e}")

    def export_settings(self):
        file_path, _ = QFileDialog.getSaveFileName(self, "Export BCI Settings Profile", "bci_profile.json", "JSON Files (*.json)")
        if file_path:
            try:
                cfg = self.get_settings_dict()
                with open(file_path, "w", encoding="utf-8") as f:
                    json.dump(cfg, f, indent=2)
                QMessageBox.information(self, "Settings Exported", f"Successfully exported settings to:\n{os.path.basename(file_path)}")
            except Exception as e:
                QMessageBox.critical(self, "Export Error", f"Could not save profile:\n{e}")

    def import_settings(self):
        file_path, _ = QFileDialog.getOpenFileName(self, "Import BCI Settings Profile", "", "JSON Files (*.json);;All Files (*)")
        if file_path:
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                self.apply_settings_dict(cfg)
                QMessageBox.information(self, "Settings Loaded", f"Successfully loaded configuration profile from:\n{os.path.basename(file_path)}")
            except Exception as e:
                QMessageBox.critical(self, "Import Error", f"Could not read profile JSON:\n{e}")

    def apply_preset(self, index):
        key = self.preset_combo.currentData()
        if not key:
            return

        def set_mapping(trigger_name, action_label):
            if hasattr(self, "input_map_combos") and trigger_name in self.input_map_combos:
                self.input_map_combos[trigger_name].setCurrentText(action_label)

        def disable_all_mappings():
            if hasattr(self, "input_map_combos"):
                for cb in self.input_map_combos.values():
                    cb.setCurrentText("Disabled (None)")

        if key == "gaming":
            disable_all_mappings()
            set_mapping("Phone Tilt Left", "Move Left (left)")
            set_mapping("Phone Tilt Right", "Move Right (right)")
            set_mapping("Phone Flick Up", "Move Up (up)")
            set_mapping("Phone Flick Down", "Move Down (down)")
            set_mapping("Phone Screen Tap", "Primary Action (Fire / Pause)")
            set_mapping("Muse Eye Blink", "Secondary Action (Spread / Food)")
            set_mapping("Muse Jaw Clench", "Tertiary Action (Smart Bomb / Color)")
            self.blink_thresh_slider.setValue(60)
            self.clench_thresh_slider.setValue(45)
            self.tilt_thresh_slider.setValue(10)
            self.flick_thresh_slider.setValue(25)
            self.shake_thresh_slider.setValue(60)
            self.eeg_ema_slider.setValue(12)
            self.eeg_cooldown_slider.setValue(4)

        elif key == "meditation":
            disable_all_mappings()
            set_mapping("Muse Deep Calm", "Primary Action (Fire / Pause)")
            self.focus_thresh_slider.setValue(50)
            self.calm_thresh_slider.setValue(55)
            self.eeg_ema_slider.setValue(3)
            self.alpha_min_spin.setValue(70)
            self.alpha_max_spin.setValue(130)
            self.theta_min_spin.setValue(35)
            self.theta_max_spin.setValue(75)

        elif key == "focus":
            disable_all_mappings()
            set_mapping("Muse High Focus", "Primary Action (Fire / Pause)")
            self.focus_thresh_slider.setValue(60)
            self.eeg_ema_slider.setValue(5)
            self.beta_min_spin.setValue(140)
            self.beta_max_spin.setValue(280)
            self.focus_num_min.setValue(140)
            self.focus_num_max.setValue(280)

        elif key == "default":
            disable_all_mappings()
            set_mapping("Phone Tilt Left", "Move Left (left)")
            set_mapping("Phone Tilt Right", "Move Right (right)")
            set_mapping("Phone Tilt Forward", "Move Up (up)")
            set_mapping("Phone Tilt Backward", "Move Down (down)")
            set_mapping("Phone Screen Tap", "Primary Action (Fire / Pause)")
            set_mapping("Phone Shake", "Secondary Action (Spread / Food)")
            set_mapping("Muse Eye Blink", "Tertiary Action (Smart Bomb / Color)")
            self.blink_thresh_slider.setValue(80)
            self.clench_thresh_slider.setValue(60)
            self.focus_thresh_slider.setValue(60)
            self.calm_thresh_slider.setValue(60)
            self.tilt_thresh_slider.setValue(15)
            self.flick_thresh_slider.setValue(35)
            self.shake_thresh_slider.setValue(80)
            self.eeg_ema_slider.setValue(4)
            self.eeg_cooldown_slider.setValue(8)
            self.delta_min_spin.setValue(5); self.delta_max_spin.setValue(40)
            self.theta_min_spin.setValue(40); self.theta_max_spin.setValue(80)
            self.alpha_min_spin.setValue(80); self.alpha_max_spin.setValue(130)
            self.beta_min_spin.setValue(130); self.beta_max_spin.setValue(300)
            self.gamma_min_spin.setValue(300); self.gamma_max_spin.setValue(500)




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
            "smartwatch_bridge": {
                "name": "Smartwatch LSL Bridge",
                "script": "smartwatch_lsl_bridge.py",
                "sim_capable": False,
                "desc": "Receives PPG & 6-DOF IMU data from a real Galaxy Watch (or any UDP/WebSocket source) and streams both Smartwatch_PPG and Smartwatch_IMU over LSL. Default: UDP port 5005.",
                "process": None,
                "status": "Stopped",
                "color": "#10b981"  # emerald green
            },
            "phone_bridge": {
                "name": "Phone IMU LSL Bridge",
                "script": "phone_imu_bridge.py",
                "sim_capable": False,
                "desc": "Listens for real-time IMU streams from the free 'Sensor Logger' phone app over WebSockets (port 8000) and maps to the Smartwatch LSL format.",
                "process": None,
                "status": "Stopped",
                "color": "#a855f7"  # purple
            },
            "whisper_voice": {
                "name": "Whisper Voice Bridge",
                "script": "whisper_voice_bridge.py",
                "sim_capable": False,
                "desc": "Listens to the PC microphone and uses faster-whisper to transcribe speech in real time. Detected keywords (left / right / fire / stop) are pushed as a Voice_Commands LSL stream.",
                "process": None,
                "status": "Stopped",
                "color": "#f59e0b"  # amber
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

        if key == "smartwatch_bridge":
            sub_panel.setVisible(True)
            mode_lbl = QLabel("Mode:")
            mode_lbl.setStyleSheet("font-size: 11px; font-weight: bold; color: #94a3b8;")
            sub_panel_layout.addWidget(mode_lbl)

            self.bridge_mode_combo = QComboBox()
            self.bridge_mode_combo.addItem("UDP", "udp")
            self.bridge_mode_combo.addItem("WebSocket", "websocket")
            self.bridge_mode_combo.addItem("Mock (offline test)", "mock")
            sub_panel_layout.addWidget(self.bridge_mode_combo)

            port_lbl = QLabel("Port:")
            port_lbl.setStyleSheet("font-size: 11px; font-weight: bold; color: #94a3b8; margin-left: 8px;")
            sub_panel_layout.addWidget(port_lbl)

            from PyQt6.QtWidgets import QSpinBox
            self.bridge_port_spin = QSpinBox()
            self.bridge_port_spin.setRange(1024, 65535)
            self.bridge_port_spin.setValue(5005)
            self.bridge_port_spin.setFixedWidth(75)
            sub_panel_layout.addWidget(self.bridge_port_spin)
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

        elif key == "whisper_voice":
            sub_panel.setVisible(True)

            # Mic device selector
            mic_lbl = QLabel("Microphone:")
            mic_lbl.setStyleSheet("font-size: 11px; font-weight: bold; color: #94a3b8;")
            sub_panel_layout.addWidget(mic_lbl)

            self.whisper_mic_combo = QComboBox()
            self.whisper_mic_combo.setMinimumWidth(180)
            self.whisper_mic_combo.addItem("System Default", None)
            try:
                import sounddevice as sd
                for i, d in enumerate(sd.query_devices()):
                    if d["max_input_channels"] > 0:
                        self.whisper_mic_combo.addItem(f"[{i}] {d['name']}", i)
            except Exception:
                pass  # sounddevice not yet installed; will show only Default
            sub_panel_layout.addWidget(self.whisper_mic_combo)

            # Model size selector
            model_lbl = QLabel("Model:")
            model_lbl.setStyleSheet("font-size: 11px; font-weight: bold; color: #94a3b8; margin-left: 10px;")
            sub_panel_layout.addWidget(model_lbl)

            self.whisper_model_combo = QComboBox()
            for m in ["tiny", "base", "small", "medium"]:
                self.whisper_model_combo.addItem(m, m)
            sub_panel_layout.addWidget(self.whisper_model_combo)
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
        elif key == "smartwatch_bridge":
            mode = self.bridge_mode_combo.currentData()
            port = self.bridge_port_spin.value()
            if mode == "mock":
                cmd_args.append("--mock")
            else:
                cmd_args.extend(["--mode", mode, "--port", str(port)])
        elif key == "phone_bridge":
            selected_ip = self.ip_combo.currentData()
            cmd_args.extend(["--ip", selected_ip])
            # Auto-provision TLS cert before launch
            base_dir = os.path.dirname(os.path.abspath(__file__))
            cert_msg = ensure_ssl_cert(base_dir)
            if cert_msg:
                self.append_system_log(f"[SSL] {cert_msg}")
        elif key == "whisper_voice":
            model = self.whisper_model_combo.currentData()
            cmd_args.extend(["--model", model])
            mic_device = self.whisper_mic_combo.currentData()
            if mic_device is not None:
                cmd_args.extend(["--input-device", str(mic_device)])
            
        from PyQt6.QtCore import QProcessEnvironment
        process = QProcess()
        process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONUNBUFFERED", "1")
        # Force Python to use UTF-8 so emojis printed by child scripts (like Athena) don't crash Windows cp1252 encoders
        env.insert("PYTHONUTF8", "1")
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

        # Terminate the bridge process cleanly
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
            elif key == "smartwatch_bridge":
                self.bridge_mode_combo.setEnabled(False)
                self.bridge_port_spin.setEnabled(False)
            elif key == "phone_bridge":
                self.ip_combo.setEnabled(False)
            elif key == "whisper_voice":
                self.whisper_mic_combo.setEnabled(False)
                self.whisper_model_combo.setEnabled(False)
                
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
            elif key == "smartwatch_bridge":
                self.bridge_mode_combo.setEnabled(True)
                self.bridge_port_spin.setEnabled(True)
            elif key == "phone_bridge":
                self.ip_combo.setEnabled(True)
            elif key == "whisper_voice":
                self.whisper_mic_combo.setEnabled(True)
                self.whisper_model_combo.setEnabled(True)
                
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
        # Force UTF-8 so printing emojis in games doesn't crash cp1252 Windows decoders
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONUTF8", "1")
        proc.setProcessEnvironment(env)

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
        elif key == "smartwatch_bridge":
            if self.ppg_tab.inlet:
                self.ppg_tab.stop_feed()
            if self.imu_tab.inlet:
                self.imu_tab.stop_feed()
        elif key == "phone_bridge":
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
