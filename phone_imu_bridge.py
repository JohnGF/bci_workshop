# -*- coding: utf-8 -*-
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pylsl",
#     "websockets",
#     "cryptography"
# ]
# ///

import asyncio
import json
import logging
import logging.handlers
import socket
import sys
import threading
import ssl
import os
import subprocess
import time
import websockets
from http.server import BaseHTTPRequestHandler, HTTPServer
from pylsl import StreamInfo, StreamOutlet, cf_float32

# ==========================================
# CONNECTION LOGGER  (connections.log)
# ==========================================
_log_dir = os.path.dirname(os.path.abspath(__file__))
_log_path = os.path.join(_log_dir, "connections.log")

conn_logger = logging.getLogger("connections")
conn_logger.setLevel(logging.DEBUG)
_handler = logging.handlers.RotatingFileHandler(
    _log_path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
)
_handler.setFormatter(
    logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
)
conn_logger.addHandler(_handler)
conn_logger.addHandler(logging.StreamHandler())  # mirror to console as well

# Resolve local IP address to print instructions
def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Doesn't need to be reachable
        s.connect(('8.8.8.8', 1))
        IP = s.getsockname()[0]
    except Exception:
        IP = '127.0.0.1'
    finally:
        s.close()
    return IP

# Setup LSL
info = StreamInfo("Smartwatch_IMU", "IMU", 7, 100, cf_float32, "phone_imu_bridge_uid777")
channels = info.desc().append_child("channels")
labels = ["Acc_X", "Acc_Y", "Acc_Z", "Gyro_X", "Gyro_Y", "Gyro_Z", "Touch"]
units  = ["m/s^2", "m/s^2", "m/s^2", "rad/s", "rad/s", "rad/s", "state"]
for lbl, unit in zip(labels, units):
    ch = channels.append_child("channel")
    ch.append_child_value("label", lbl)
    ch.append_child_value("type", "IMU")
    ch.append_child_value("unit", unit)
    
outlet = StreamOutlet(info)

# Keep track of latest values to handle async updates (for third-party apps)
latest_acc = [0.0, 0.0, 9.81]
latest_gyro = [0.0, 0.0, 0.0]
latest_touch = 0.0

async def echo(websocket):
    global latest_acc, latest_gyro, latest_touch
    remote = websocket.remote_address
    session_start = time.monotonic()
    conn_logger.info(f"CONNECT    remote={remote}")
    print(f"✅ Phone connected from: {remote}")
    first_message = True
    try:
        async for message in websocket:
            if first_message:
                conn_logger.info(f"FIRST_MSG  remote={remote}  (first sensor data received)")
                print("📥 First sensor message received from phone!")
                first_message = False
            data = json.loads(message)
            
            # 1. Custom Web Browser Touch Action
            if "touch" in data:
                latest_touch = 1.0
                outlet.push_sample(latest_acc + latest_gyro + [latest_touch])
                latest_touch = 0.0
                continue
            
            # 2. Custom Web Browser direct sensor format
            elif "acc" in data and "gyro" in data:
                latest_acc = data.get("acc", [0.0, 0.0, 9.81])
                latest_gyro = data.get("gyro", [0.0, 0.0, 0.0])
                outlet.push_sample(latest_acc + latest_gyro + [latest_touch])
                latest_touch = 0.0
            
            # 3. Third-Party 'Sensor Logger' app format (fallback)
            else:
                payload = data.get("payload", [])
                for item in payload:
                    name = item.get("name")
                    values = item.get("values", {})
                    if name == "accelerometer":
                        latest_acc = [values.get("x", 0.0), values.get("y", 0.0), values.get("z", 0.0)]
                    elif name == "gyroscope":
                        latest_gyro = [values.get("x", 0.0), values.get("y", 0.0), values.get("z", 0.0)]
                outlet.push_sample(latest_acc + latest_gyro + [latest_touch])
                latest_touch = 0.0
            
    except websockets.exceptions.ConnectionClosed as e:
        duration = time.monotonic() - session_start
        conn_logger.info(f"DISCONNECT remote={remote}  reason={e}  duration={duration:.1f}s")
        print(f"🛑 Phone disconnected: {e}")
    except Exception as e:
        conn_logger.error(f"ERROR      remote={remote}  error={e}")
        print(f"⚠️ Parsing Error: {e}")
    else:
        duration = time.monotonic() - session_start
        conn_logger.info(f"DISCONNECT remote={remote}  reason=stream_ended  duration={duration:.1f}s")
def start_udp_listener(ip, port):
    """Listens for UDP packets (JSON or CSV) from mobile apps like HyperIMU and pushes to LSL."""
    global latest_acc, latest_gyro, latest_touch
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind((ip, port))
        conn_logger.info(f"UDP_START  Listening on {ip}:{port}")
        print(f"📡 UDP Listener active on {ip}:{port}")
    except Exception as e:
        conn_logger.error(f"UDP_BIND_ERROR on {ip}:{port}: {e}")
        print(f"⚠️ Failed to bind UDP listener on {ip}:{port}: {e}")
        return

    first_packet = True
    while True:
        try:
            data, addr = sock.recvfrom(4096)
            if first_packet:
                conn_logger.info(f"UDP_FIRST_MSG remote={addr}")
                print(f"📥 First UDP sensor packet received from {addr}!")
                first_packet = False

            text = data.decode('utf-8', errors='ignore').strip()

            # 1. JSON format
            if text.startswith('{'):
                try:
                    payload = json.loads(text)
                    if "touch" in payload:
                        latest_touch = 1.0
                        outlet.push_sample(latest_acc + latest_gyro + [latest_touch])
                        latest_touch = 0.0
                        continue
                    if "acc" in payload and "gyro" in payload:
                        latest_acc = payload.get("acc", [0.0, 0.0, 9.81])
                        latest_gyro = payload.get("gyro", [0.0, 0.0, 0.0])
                        outlet.push_sample(latest_acc + latest_gyro + [latest_touch])
                        latest_touch = 0.0
                        continue
                    elif "accel" in payload and "gyro" in payload:
                        latest_acc = payload["accel"]
                        latest_gyro = payload["gyro"]
                        outlet.push_sample(latest_acc + latest_gyro + [latest_touch])
                        latest_touch = 0.0
                        continue
                    elif "payload" in payload:
                        for item in payload.get("payload", []):
                            name = item.get("name")
                            values = item.get("values", {})
                            if name == "accelerometer":
                                latest_acc = [values.get("x", 0.0), values.get("y", 0.0), values.get("z", 0.0)]
                            elif name == "gyroscope":
                                latest_gyro = [values.get("x", 0.0), values.get("y", 0.0), values.get("z", 0.0)]
                        outlet.push_sample(latest_acc + latest_gyro + [latest_touch])
                        latest_touch = 0.0
                        continue
                except Exception:
                    pass

            # 2. CSV format
            # Galaxy Watch / Mobile Streamer format: accX,accY,accZ,gyroX,gyroY,gyroZ,hr,touchX,touchY,isTouched (10 values)
            # HyperIMU standard format: accX,accY,accZ,gyroX,gyroY,gyroZ (6 values)
            parts = text.split(',')
            if len(parts) >= 6:
                try:
                    vals = [float(p.strip()) for p in parts if p.strip()]
                    if len(vals) >= 6:
                        latest_acc = vals[:3]
                        latest_gyro = vals[3:6]
                        # If 10 items (from galaxy_watch_lsl mobile/watch streamer)
                        if len(vals) >= 10:
                            # vals[9] is isTouched (1.0 if touched, 0.0 otherwise)
                            touch_state = 1.0 if vals[9] > 0.5 else 0.0
                            outlet.push_sample(latest_acc + latest_gyro + [touch_state])
                        else:
                            outlet.push_sample(latest_acc + latest_gyro + [latest_touch])
                            latest_touch = 0.0
                except ValueError:
                    pass
        except Exception as e:
            conn_logger.error(f"UDP_ERROR: {e}")

HTML_CONTENT = """<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>📲 BCI Mobile Controller</title>
    <style>
        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
            -webkit-tap-highlight-color: transparent;
        }
        body {
            background-color: #0b0f19;
            color: #f1f5f9;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            user-select: none;
            -webkit-user-select: none;
            overflow: hidden;
            height: 100vh;
            width: 100vw;
        }

        /* ── CONNECT SCREEN ───────────────────────────── */
        #connectScreen {
            display: flex;
            align-items: center;
            justify-content: center;
            height: 100vh;
            padding: 20px;
        }
        .card {
            background: rgba(30, 41, 59, 0.85);
            backdrop-filter: blur(16px);
            border: 1px solid rgba(255, 255, 255, 0.08);
            border-radius: 20px;
            padding: 28px 24px;
            width: 100%;
            max-width: 380px;
            box-shadow: 0 20px 40px -10px rgba(0, 0, 0, 0.5);
            text-align: center;
        }
        h1 {
            font-size: 22px;
            font-weight: 800;
            margin-bottom: 8px;
            background: linear-gradient(to right, #38bdf8, #818cf8);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }
        .subtitle {
            color: #94a3b8;
            font-size: 13px;
            line-height: 1.5;
            margin-bottom: 20px;
        }
        .btn {
            background: linear-gradient(135deg, #0284c7 0%, #4f46e5 100%);
            border: none;
            border-radius: 12px;
            color: white;
            padding: 15px 28px;
            font-size: 16px;
            font-weight: bold;
            cursor: pointer;
            width: 100%;
            box-shadow: 0 4px 16px rgba(79, 70, 229, 0.4);
            transition: all 0.2s ease;
        }
        .btn:active { transform: scale(0.97); opacity: 0.9; }
        .btn.stop {
            background: linear-gradient(135deg, #ef4444 0%, #b91c1c 100%);
            box-shadow: 0 4px 16px rgba(239, 68, 68, 0.35);
        }
        .status-badge {
            display: inline-flex;
            align-items: center;
            padding: 5px 12px;
            border-radius: 20px;
            font-size: 12px;
            font-weight: bold;
            margin-bottom: 18px;
            background-color: #1e293b;
            border: 1px solid rgba(255,255,255,0.06);
        }
        .status-dot {
            width: 8px; height: 8px;
            border-radius: 50%;
            margin-right: 8px;
            background-color: #ef4444;
        }
        .status-badge.connected .status-dot {
            background-color: #10b981;
            box-shadow: 0 0 8px #10b981;
            animation: pulse-dot 1.5s infinite;
        }
        @keyframes pulse-dot {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.4; }
        }

        /* ── LANDING ZONE SCREEN ─────────────────────── */
        #landingScreen {
            display: none;
            position: fixed;
            inset: 0;
            flex-direction: column;
            background: #080c14;
        }
        #landingScreen.active { display: flex; }

        /* Top HUD bar */
        #hud {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 14px 18px;
            background: rgba(15, 23, 42, 0.9);
            border-bottom: 1px solid rgba(255,255,255,0.05);
            flex-shrink: 0;
        }
        #hudStatus {
            display: flex;
            align-items: center;
            gap: 8px;
            font-size: 13px;
            font-weight: bold;
            color: #10b981;
        }
        #hudDot {
            width: 8px; height: 8px;
            border-radius: 50%;
            background: #10b981;
            box-shadow: 0 0 8px #10b981;
            animation: pulse-dot 1.5s infinite;
        }
        #stopBtn {
            background: rgba(239,68,68,0.15);
            border: 1px solid rgba(239,68,68,0.4);
            border-radius: 8px;
            color: #f87171;
            font-size: 12px;
            font-weight: bold;
            padding: 6px 12px;
            cursor: pointer;
        }
        #stopBtn:active { opacity: 0.7; }

        /* The big tap zone */
        #tapZone {
            flex: 1;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            position: relative;
            overflow: hidden;
            cursor: pointer;
            background: radial-gradient(ellipse at center, #0f172a 0%, #080c14 70%);
        }

        /* Pulsing ring graphic */
        .ring-wrap {
            position: relative;
            width: 180px;
            height: 180px;
            display: flex;
            align-items: center;
            justify-content: center;
            pointer-events: none;
        }
        .ring {
            position: absolute;
            border-radius: 50%;
            border: 2px solid rgba(56,189,248,0.35);
            animation: expand 3s ease-out infinite;
        }
        .ring:nth-child(1) { width: 180px; height: 180px; animation-delay: 0s; }
        .ring:nth-child(2) { width: 180px; height: 180px; animation-delay: 1s; }
        .ring:nth-child(3) { width: 180px; height: 180px; animation-delay: 2s; }
        @keyframes expand {
            0%   { transform: scale(0.4); opacity: 0.8; }
            100% { transform: scale(1.8); opacity: 0; }
        }
        .tap-inner {
            width: 90px; height: 90px;
            border-radius: 50%;
            background: radial-gradient(circle, rgba(56,189,248,0.25) 0%, rgba(56,189,248,0.05) 70%);
            border: 2px solid rgba(56,189,248,0.6);
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 32px;
            box-shadow: 0 0 30px rgba(56,189,248,0.15);
            z-index: 1;
        }
        .tap-label {
            margin-top: 24px;
            font-size: 13px;
            color: #475569;
            letter-spacing: 0.1em;
            text-transform: uppercase;
            pointer-events: none;
        }
        #tapFeedback {
            margin-top: 12px;
            font-size: 14px;
            font-weight: bold;
            color: #38bdf8;
            min-height: 20px;
            opacity: 0;
            transition: opacity 0.1s;
            pointer-events: none;
        }

        /* Ripple on tap */
        .ripple {
            position: absolute;
            border-radius: 50%;
            background: rgba(56,189,248,0.25);
            transform: scale(0);
            animation: ripple-anim 0.6s ease-out forwards;
            pointer-events: none;
        }
        @keyframes ripple-anim {
            to { transform: scale(6); opacity: 0; }
        }

        /* Bottom IMU readout */
        #imuBar {
            padding: 12px 18px;
            background: rgba(15, 23, 42, 0.9);
            border-top: 1px solid rgba(255,255,255,0.05);
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 4px 16px;
            flex-shrink: 0;
        }
        .imu-row {
            display: flex;
            justify-content: space-between;
            font-size: 11px;
            font-family: monospace;
            color: #64748b;
        }
        .imu-row span { color: #94a3b8; font-weight: bold; }
        .imu-head {
            font-size: 10px;
            letter-spacing: 0.08em;
            text-transform: uppercase;
            margin-bottom: 4px;
            font-weight: bold;
        }
    </style>
</head>
<body>

    <!-- ── CONNECT SCREEN ─────────────────────────── -->
    <div id="connectScreen">
        <div class="card">
            <h1>📲 Mobile LSL Streamer</h1>
            <p class="subtitle">Motion sensors streamed live to LSL.<br>
            <small style="color:#475569;">Voice is handled by the PC — just move!</small></p>

            <div class="status-badge" id="statusBadge">
                <span class="status-dot"></span>
                <span id="statusText">Disconnected</span>
            </div>

            <button class="btn" id="actionBtn">Enable Sensors</button>
        </div>
    </div>

    <!-- ── FULL-SCREEN LANDING ZONE ───────────────── -->
    <div id="landingScreen">

        <!-- Top HUD -->
        <div id="hud">
            <div id="hudStatus">
                <div id="hudDot"></div>
                Streaming Live
            </div>
            <button id="stopBtn">✕ Stop</button>
        </div>

        <!-- Big tap zone -->
        <div id="tapZone">
            <div class="ring-wrap">
                <div class="ring"></div>
                <div class="ring"></div>
                <div class="ring"></div>
                <div class="tap-inner">👆</div>
            </div>
            <div class="tap-label">Tap anywhere</div>
            <div id="tapFeedback">TAP!</div>
        </div>

        <!-- Bottom IMU bar -->
        <div id="imuBar">
            <div>
                <div class="imu-head" style="color:#38bdf8;">Accel m/s²</div>
                <div class="imu-row">X <span id="accX">0.00</span></div>
                <div class="imu-row">Y <span id="accY">0.00</span></div>
                <div class="imu-row">Z <span id="accZ">9.81</span></div>
            </div>
            <div>
                <div class="imu-head" style="color:#818cf8;">Gyro rad/s</div>
                <div class="imu-row">X <span id="gyroX">0.00</span></div>
                <div class="imu-row">Y <span id="gyroY">0.00</span></div>
                <div class="imu-row">Z <span id="gyroZ">0.00</span></div>
            </div>
        </div>

    </div>

    <script>
        const statusBadge = document.getElementById('statusBadge');
        const statusText  = document.getElementById('statusText');
        const actionBtn   = document.getElementById('actionBtn');
        const connectScreen  = document.getElementById('connectScreen');
        const landingScreen  = document.getElementById('landingScreen');
        const tapZone     = document.getElementById('tapZone');
        const tapFeedback = document.getElementById('tapFeedback');
        const stopBtn     = document.getElementById('stopBtn');

        const accX  = document.getElementById('accX');
        const accY  = document.getElementById('accY');
        const accZ  = document.getElementById('accZ');
        const gyroX = document.getElementById('gyroX');
        const gyroY = document.getElementById('gyroY');
        const gyroZ = document.getElementById('gyroZ');

        let ws = null;
        let isStreaming = false;
        let feedbackTimer = null;

        // Spawn a ripple at the touch/click coordinates
        function spawnRipple(x, y) {
            const size = 80;
            const el = document.createElement('div');
            el.className = 'ripple';
            el.style.cssText = `width:${size}px;height:${size}px;left:${x - size/2}px;top:${y - size/2}px;`;
            tapZone.appendChild(el);
            el.addEventListener('animationend', () => el.remove());
        }

        // Show TAP! flash
        function flashFeedback() {
            tapFeedback.style.opacity = '1';
            clearTimeout(feedbackTimer);
            feedbackTimer = setTimeout(() => { tapFeedback.style.opacity = '0'; }, 400);
        }

        function sendTouchTap(x, y) {
            if (ws && ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify({ touch: "tap" }));
            }
            spawnRipple(x, y);
            flashFeedback();
        }

        // Tap zone — touch events
        tapZone.addEventListener('touchstart', (e) => {
            e.preventDefault();
            const t = e.changedTouches[0];
            const rect = tapZone.getBoundingClientRect();
            sendTouchTap(t.clientX - rect.left, t.clientY - rect.top);
        }, { passive: false });

        // Tap zone — mouse fallback (desktop testing)
        tapZone.addEventListener('click', (e) => {
            const rect = tapZone.getBoundingClientRect();
            sendTouchTap(e.clientX - rect.left, e.clientY - rect.top);
        });

        // Stop button in landing zone
        stopBtn.addEventListener('click', () => {
            if (ws) ws.close();
        });

        function showLanding() {
            connectScreen.style.display = 'none';
            landingScreen.classList.add('active');
        }

        function showConnect() {
            landingScreen.classList.remove('active');
            connectScreen.style.display = 'flex';
        }

        function connectWS() {
            // Target Secure WS server. If running on standard 443, we map explicitly to 8001/8002 to avoid Windows SMB port collisions
            const isHttps = window.location.protocol === 'https:';
            let basePort = window.location.port ? parseInt(window.location.port) : (isHttps ? 443 : 80);

            let wsPort = 0;
            if (basePort === 443) {
                wsPort = isHttps ? 8002 : 8001;
            } else {
                wsPort = basePort + (isHttps ? 2 : 1);
            }

            const protocol = isHttps ? 'wss:' : 'ws:';
            const wsUrl = `${protocol}//${window.location.hostname}:${wsPort}`;
            
            statusText.innerText = "Connecting...";
            statusBadge.className = "status-badge";
            
            ws = new WebSocket(wsUrl);
            
            ws.onopen = () => {
                isStreaming = true;
                showLanding();
            };
            
            ws.onclose = () => {
                isStreaming = false;
                statusText.innerText = "Disconnected";
                statusBadge.className = "status-badge";
                actionBtn.innerText = "Enable Sensors";
                showConnect();
            };
            
            ws.onerror = (err) => {
                console.error("WS error:", err);
            };
        }

        function handleMotion(event) {
            if (!isStreaming || !ws || ws.readyState !== WebSocket.OPEN) return;

            const acc = event.accelerationIncludingGravity || { x: 0, y: 0, z: 9.81 };
            const rot = event.rotationRate || { alpha: 0, beta: 0, gamma: 0 };

            // Update UI feedback values
            accX.innerText = acc.x ? acc.x.toFixed(2) : "0.00";
            accY.innerText = acc.y ? acc.y.toFixed(2) : "0.00";
            accZ.innerText = acc.z ? acc.z.toFixed(2) : "0.00";

            // Convert rotationRate (degrees/sec) to rad/s for standard IMU
            const toRad = Math.PI / 180;
            const gX = rot.alpha ? (rot.alpha * toRad) : 0;
            const gY = rot.beta ? (rot.beta * toRad) : 0;
            const gZ = rot.gamma ? (rot.gamma * toRad) : 0;

            gyroX.innerText = gX.toFixed(2);
            gyroY.innerText = gY.toFixed(2);
            gyroZ.innerText = gZ.toFixed(2);

            // Send standard IMU payload
            ws.send(JSON.stringify({
                acc: [acc.x || 0, acc.y || 0, acc.z || 9.81],
                gyro: [gX, gY, gZ]
            }));
        }

        function requestPermissions() {
            // iOS 13+ requires explicit permissions request callback on user gesture
            if (typeof DeviceMotionEvent !== 'undefined' && typeof DeviceMotionEvent.requestPermission === 'function') {
                DeviceMotionEvent.requestPermission()
                    .then(response => {
                        if (response === 'granted') {
                            window.addEventListener('devicemotion', handleMotion, true);
                            connectWS();
                        } else {
                            alert('Motion permission is required to stream sensors.');
                        }
                    })
                    .catch(error => {
                        console.error('DeviceMotion permission error:', error);
                    });
            } else {
                // Android / desktop mock browsers
                window.addEventListener('devicemotion', handleMotion, true);
                connectWS();
            }
        }

        actionBtn.addEventListener('click', () => {
            if (isStreaming) {
                if (ws) ws.close();
            } else {
                requestPermissions();
            }
        });
    </script>
</body>
</html>
"""

# ==========================================
# LIGHTWEIGHT EMBEDDED HTTP SERVER
# ==========================================
from socketserver import ThreadingMixIn

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    allow_reuse_address = True

class WebBridgeHTTPHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        client_ip = self.client_address[0] if self.client_address else "unknown"
        msg = f"WEB_REQUEST client={client_ip} {format % args}"
        conn_logger.info(msg)
        print(f"🌐 [HTTPS] {client_ip} -> {format % args}")

    def do_GET(self):
        client_ip = self.client_address[0] if self.client_address else "unknown"
        conn_logger.info(f"SERVE_UI client={client_ip} path={self.path}")

        if self.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return

        # Dynamically load mobile_controller.html from disk so HTML/CSS/JS edits are live instantly
        html_to_serve = HTML_CONTENT
        html_file_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mobile_controller.html")
        if os.path.exists(html_file_path):
            try:
                with open(html_file_path, "r", encoding="utf-8") as f:
                    html_to_serve = f.read()
            except Exception as e:
                conn_logger.warning(f"Failed to read mobile_controller.html from disk: {e}")

        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        self.wfile.write(html_to_serve.encode("utf-8"))

    def do_POST(self):
        client_ip = self.client_address[0] if self.client_address else "unknown"
        content_length = int(self.headers.get('Content-Length', 0))
        post_data = self.rfile.read(content_length)
        
        try:
            data = json.loads(post_data.decode('utf-8'))
            payload = data.get("payload", [])
            
            global latest_acc, latest_gyro, latest_touch, latest_voice
            for item in payload:
                name = item.get("name")
                values = item.get("values", {})
                if name == "accelerometer":
                    latest_acc = [values.get("x", 0.0), values.get("y", 0.0), values.get("z", 0.0)]
                elif name == "gyroscope":
                    latest_gyro = [values.get("x", 0.0), values.get("y", 0.0), values.get("z", 0.0)]
            
            # Push combined sample to LSL
            outlet.push_sample(latest_acc + latest_gyro + [latest_touch])
            
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status": "ok"}')
        except Exception as e:
            conn_logger.error(f"HTTP_POST_ERROR client={client_ip} error={e}")
            print(f"⚠️ HTTP Push Error: {e}")
            self.send_response(400)
            self.end_headers()

def generate_self_signed_cert(ip_address):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import hashes
    from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
    from cryptography import x509
    import datetime
    import ipaddress

    cert_ip = ip_address
    if cert_ip == "0.0.0.0":
        cert_ip = get_local_ip()

    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )

    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "localhost"),
    ])

    san_list = [
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
    ]
    seen_ips = {"127.0.0.1"}
    if cert_ip and cert_ip != "0.0.0.0" and cert_ip not in seen_ips:
        try:
            san_list.append(x509.IPAddress(ipaddress.IPv4Address(cert_ip)))
            seen_ips.add(cert_ip)
        except Exception:
            pass

    # Also add the primary local IP
    primary_ip = get_local_ip()
    if primary_ip not in seen_ips:
        try:
            san_list.append(x509.IPAddress(ipaddress.IPv4Address(primary_ip)))
            seen_ips.add(primary_ip)
        except Exception:
            pass

    cert = x509.CertificateBuilder().subject_name(
        subject
    ).issuer_name(
        issuer
    ).public_key(
        private_key.public_key()
    ).serial_number(
        x509.random_serial_number()
    ).not_valid_before(
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)
    ).not_valid_after(
        datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=3650)
    ).add_extension(
        x509.SubjectAlternativeName(san_list),
        critical=False,
    ).add_extension(
        x509.BasicConstraints(ca=True, path_length=None),
        critical=True,
    ).add_extension(
        x509.KeyUsage(
            digitalSignature=True,
            keyEncipherment=True,
            keyCertSign=True,
            cRLSign=False,
            contentCommitment=False,
            dataEncipherment=False,
            keyAgreement=False,
            encipherOnly=False,
            decipherOnly=False,
        ),
        critical=True,
    ).add_extension(
        x509.ExtendedKeyUsage([
            ExtendedKeyUsageOID.SERVER_AUTH,
            ExtendedKeyUsageOID.CLIENT_AUTH,
        ]),
        critical=False,
    ).sign(private_key, hashes.SHA256())

    with open("key.pem", "wb") as f:
        f.write(private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ))

    with open("cert.pem", "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))

def start_http_server(ip, port):
    # Always generate self-signed certificate natively on startup to ensure the SAN matches the current IP
    print(f"🔑 Generating self-signed SSL certificate for IP: {ip if ip != '0.0.0.0' else get_local_ip()}...")
    try:
        generate_self_signed_cert(ip)
        print("✅ SSL Certificate generated successfully.")
    except Exception as e:
        print(f"⚠️ Failed to generate native SSL certificate: {e}")

    server = ThreadedHTTPServer((ip, port), WebBridgeHTTPHandler)

    # Wrap the listener socket directly in TLS
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain("cert.pem", "key.pem")
        server.socket = context.wrap_socket(server.socket, server_side=True)
        print(f"🔒 HTTPS Server SSL socket established on port {port}.")
    except Exception as e:
        print(f"❌ Error wrapping SSL socket for HTTPS server: {e}. Check cert.pem/key.pem.")

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server

# ==========================================
# MAIN EXECUTION LOOP
# ==========================================
async def main():
    import argparse
    parser = argparse.ArgumentParser(description="Phone IMU LSL Bridge")
    parser.add_argument("--ip", type=str, default="0.0.0.0", help="IP address to bind the WebSocket/HTTP servers to")
    parser.add_argument("--port", type=int, default=8000, help="HTTP Server Port (WebSockets runs on port+1 and port+2)")
    parser.add_argument("--udp-port", type=int, default=5005, help="UDP Server Port for apps like HyperIMU (default: 5005)")
    args, _ = parser.parse_known_args()

    # Resolve display IP for console instructions
    display_ip = args.ip
    if display_ip == "0.0.0.0":
        display_ip = get_local_ip()

    if args.port == 443:
        # Avoid trying to bind to 445 on Windows (Reserved by system for SMB file sharing)
        ws_port_insecure = 8001
        ws_port_secure = 8002
    else:
        ws_port_insecure = args.port + 1
        ws_port_secure = args.port + 2

    # Start background HTTPS server
    print(f"📡 Starting HTTPS Interface Server on https://{args.ip}:{args.port}...")
    http_server = start_http_server(args.ip, args.port)

    # Start background UDP listener
    udp_thread = threading.Thread(target=start_udp_listener, args=(args.ip, args.udp_port), daemon=True)
    udp_thread.start()

    # Setup SSL context for Secure WebSockets (WSS)
    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        ssl_context.load_cert_chain("cert.pem", "key.pem")
    except Exception as e:
        print(f"❌ Error loading SSL certs for Secure WebSockets: {e}")
        ssl_context = None

    print("==================================================")
    print("📲 Multi-Protocol Phone IMU LSL Bridge Active")
    print("==================================================")
    if sys.platform == "win32":
        print("⚠️  WINDOWS SECURITY REMINDER:")
        print(f"   - Ensure Windows Defender Firewall allows incoming TCP on ports 8000-8002 and UDP on port {args.udp_port}.")
        print("   - Set your Wi-Fi Network Profile type to 'Private' in Windows settings.")
        print("--------------------------------------------------")
        is_virtual = (
            display_ip.startswith("172.") or 
            display_ip.startswith("192.168.56.") or 
            display_ip.startswith("169.254.")
        )
        if is_virtual:
            print(f"⚠️  VIRTUAL NETWORK IP DETECTED: {display_ip}")
            print("   This IP seems to belong to a virtual adapter (WSL, Docker, or VM).")
            print("   Your phone won't connect unless you use your physical Wi-Fi IP address.")
            print("   Please check your physical Wi-Fi IP using 'ipconfig' in command prompt.")
            print("--------------------------------------------------")
    print("Option A: ZERO INSTALL (Mobile Web)")
    print("  1. Connect your phone to the same Wi-Fi network as this PC.")
    print(f"  2. Open: https://{display_ip}:{args.port}")
    print("  3. Bypass the local SSL warning (tap Advanced -> Proceed).")
    print("  4. Tap 'Enable Sensors' to begin streaming.")
    print("  5. Toggle 'Voice Commands' to control with speech!")
    print("--------------------------------------------------")
    print("Option B: UDP STREAMER APP (HyperIMU / Custom UDP)")
    print("  1. In your app settings, set target IP and Port:")
    print(f"     IP: {display_ip} | Port: {args.udp_port} | Protocol: UDP")
    print("  2. Format: JSON (accel/gyro or acc/gyro) or CSV (ax,ay,az,gx,gy,gz)")
    print("--------------------------------------------------")
    print("Option C: CUSTOM ANDROID APP (WebSocket)")
    print("  1. Build and install the minimal streamer APK located in:")
    print("     /android-imu-streamer")
    print("  2. Open the app on your phone and connect to URL:")
    print(f"     ws://{display_ip}:{ws_port_insecure}")
    print("--------------------------------------------------")
    print("Option D: THIRD-PARTY APP (Sensor Logger - WebSockets)")
    print("  1. In the app Settings -> Live Data Streaming:")
    print("     - Set Protocol to 'WebSockets' (JSON).")
    print(f"     - Set URL to: ws://{display_ip}:{ws_port_insecure}")
    print("  2. Tap orange 'Start' on the main screen to begin streaming.")
    print("==================================================")
    print(f"⌛ Listening for UDP ({args.udp_port}), WS ({ws_port_insecure}), and WSS ({ws_port_secure})...")

    # Start both WebSocket servers concurrently
    insecure_server = websockets.serve(echo, args.ip, ws_port_insecure, reuse_address=True)
    secure_server = websockets.serve(echo, args.ip, ws_port_secure, ssl=ssl_context, reuse_address=True)

    async with insecure_server, secure_server:
        await asyncio.Future()  # Run forever

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Bridge stopped.")
