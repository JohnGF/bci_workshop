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
import socket
import sys
import threading
import ssl
import os
import subprocess
import websockets
from http.server import BaseHTTPRequestHandler, HTTPServer
from pylsl import StreamInfo, StreamOutlet, cf_float32

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
info = StreamInfo("Smartwatch_IMU", "IMU", 8, 100, cf_float32, "phone_imu_bridge_uid777")
channels = info.desc().append_child("channels")
labels = ["Acc_X", "Acc_Y", "Acc_Z", "Gyro_X", "Gyro_Y", "Gyro_Z", "Touch", "Voice"]
units = ["m/s^2", "m/s^2", "m/s^2", "rad/s", "rad/s", "rad/s", "state", "command"]
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
latest_voice = 0.0

async def echo(websocket):
    global latest_acc, latest_gyro, latest_touch, latest_voice
    print(f"✅ Phone connected from: {websocket.remote_address}")
    try:
        async for message in websocket:
            data = json.loads(message)
            
            # 1. Custom Web Browser Touch Action
            if "touch" in data:
                print("🎯 Phone Web: Touch TAP action detected!")
                latest_touch = 1.0
                outlet.push_sample(latest_acc + latest_gyro + [latest_touch, latest_voice])
                latest_touch = 0.0
                continue
                
            # 2. Custom Web Browser Voice Command format
            elif "voice" in data:
                cmd = data.get("voice")
                if cmd == "left":
                    print("🗣️ Phone Web: Voice command LEFT recognized")
                    latest_voice = 99.0
                elif cmd == "right":
                    print("🗣️ Phone Web: Voice command RIGHT recognized")
                    latest_voice = -99.0
                outlet.push_sample(latest_acc + latest_gyro + [latest_touch, latest_voice])
                latest_voice = 0.0
                continue
            
            # 3. Custom Web Browser direct sensor format
            elif "acc" in data and "gyro" in data:
                latest_acc = data.get("acc", [0.0, 0.0, 9.81])
                latest_gyro = data.get("gyro", [0.0, 0.0, 0.0])
                outlet.push_sample(latest_acc + latest_gyro + [latest_touch, latest_voice])
                latest_touch = 0.0
                latest_voice = 0.0
            
            # 4. Third-Party 'Sensor Logger' app format (fallback)
            else:
                payload = data.get("payload", [])
                for item in payload:
                    name = item.get("name")
                    values = item.get("values", {})
                    if name == "accelerometer":
                        latest_acc = [values.get("x", 0.0), values.get("y", 0.0), values.get("z", 0.0)]
                    elif name == "gyroscope":
                        latest_gyro = [values.get("x", 0.0), values.get("y", 0.0), values.get("z", 0.0)]
                outlet.push_sample(latest_acc + latest_gyro + [latest_touch, latest_voice])
                latest_touch = 0.0
                latest_voice = 0.0
            
    except websockets.exceptions.ConnectionClosed as e:
        print(f"🛑 Phone disconnected: {e}")
    except Exception as e:
        print(f"⚠️ Parsing Error: {e}")

# ==========================================
# EMBEDDED ZERO-INSTALL MOBILE WEB INTERFACE
# ==========================================
HTML_CONTENT = """<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>📲 BCI Mobile Controller</title>
    <style>
        body {
            background-color: #0b0f19;
            color: #f1f5f9;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            margin: 0;
            padding: 20px;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            min-height: 90vh;
            user-select: none;
            overflow: hidden;
        }
        .card {
            background: rgba(30, 41, 59, 0.7);
            backdrop-filter: blur(12px);
            border: 1px solid rgba(255, 255, 255, 0.08);
            border-radius: 16px;
            padding: 24px;
            width: 90%;
            max-width: 380px;
            box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.3);
            text-align: center;
        }
        h1 {
            font-size: 22px;
            font-weight: 800;
            margin-top: 0;
            color: #38bdf8;
            background: linear-gradient(to right, #38bdf8, #818cf8);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }
        p {
            color: #94a3b8;
            font-size: 14px;
            line-height: 1.5;
            margin-bottom: 15px;
        }
        .status-badge {
            display: inline-flex;
            align-items: center;
            padding: 6px 12px;
            border-radius: 20px;
            font-size: 12px;
            font-weight: bold;
            margin-bottom: 15px;
            background-color: #1e293b;
        }
        .status-dot {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            margin-right: 8px;
            background-color: #ef4444; /* red default */
        }
        .status-badge.connected .status-dot {
            background-color: #10b981; /* green */
            box-shadow: 0 0 8px #10b981;
        }
        .btn {
            background: linear-gradient(135deg, #0284c7 0%, #4f46e5 100%);
            border: none;
            border-radius: 10px;
            color: white;
            padding: 14px 28px;
            font-size: 16px;
            font-weight: bold;
            cursor: pointer;
            width: 100%;
            box-sizing: border-box;
            box-shadow: 0 4px 12px rgba(79, 70, 229, 0.3);
            transition: all 0.2s ease;
            margin-top: 5px;
            margin-bottom: 15px;
        }
        .btn:active {
            transform: scale(0.98);
            opacity: 0.9;
        }
        .sensor-vals {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 12px;
            text-align: left;
            font-family: monospace;
            font-size: 12px;
            background: rgba(15, 23, 42, 0.6);
            padding: 12px;
            border-radius: 8px;
            border: 1px solid rgba(255, 255, 255, 0.05);
            margin-top: 15px;
        }
        .val-group {
            margin-bottom: 4px;
        }
        .val-label {
            color: #64748b;
        }
        .val-num {
            color: #cbd5e1;
            font-weight: bold;
        }
        .voice-section {
            margin-top: 15px;
            padding-top: 15px;
            border-top: 1px solid rgba(255, 255, 255, 0.08);
            display: flex;
            flex-direction: column;
            align-items: center;
        }
        #touchPad:active {
            transform: scale(0.96);
            background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%) !important;
            border-color: #38bdf8 !important;
            box-shadow: 0 0 12px rgba(56, 189, 248, 0.4);
        }
    </style>
</head>
<body>
    <div class="card">
        <h1>📲 Mobile LSL Streamer</h1>
        <p>Stream your phone's real-time motion sensors or voice commands directly to LSL.</p>
        
        <div class="status-badge" id="statusBadge">
            <span class="status-dot"></span>
            <span id="statusText">Disconnected</span>
        </div>

        <button class="btn" id="actionBtn">Enable Sensors</button>

        <!-- Screen TapPad Area -->
        <div id="touchPad" style="margin: 15px 0; padding: 25px; border-radius: 10px; background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%); border: 1.5px dashed #475569; text-align: center; font-weight: bold; color: #38bdf8; cursor: pointer; user-select: none; -webkit-user-select: none; transition: all 0.15s ease-in-out;">
            🎯 TOUCH SCREEN ACTIONPAD
        </div>

        <div class="voice-section">
            <div style="display: flex; align-items: center; gap: 8px;">
                <input type="checkbox" id="voiceToggle" style="width: 18px; height: 18px; cursor: pointer;">
                <label for="voiceToggle" style="font-size: 13px; color: #94a3b8; cursor: pointer; font-weight: bold;">🗣️ Voice Commands (Speak "Left" / "Right")</label>
            </div>
            <div id="voiceStatus" style="font-size: 12px; color: #64748b; margin-top: 8px; font-style: italic; min-height: 16px;"></div>
        </div>

        <div class="sensor-vals">
            <div>
                <div style="font-weight: bold; margin-bottom: 6px; color: #38bdf8;">ACCEL (m/s²)</div>
                <div class="val-group"><span class="val-label">X:</span> <span class="val-num" id="accX">0.00</span></div>
                <div class="val-group"><span class="val-label">Y:</span> <span class="val-num" id="accY">0.00</span></div>
                <div class="val-group"><span class="val-label">Z:</span> <span class="val-num" id="accZ">9.81</span></div>
            </div>
            <div>
                <div style="font-weight: bold; margin-bottom: 6px; color: #818cf8;">GYRO (rad/s)</div>
                <div class="val-group"><span class="val-label">X:</span> <span class="val-num" id="gyroX">0.00</span></div>
                <div class="val-group"><span class="val-label">Y:</span> <span class="val-num" id="gyroY">0.00</span></div>
                <div class="val-group"><span class="val-label">Z:</span> <span class="val-num" id="gyroZ">0.00</span></div>
            </div>
        </div>
    </div>

    <script>
        const statusBadge = document.getElementById('statusBadge');
        const statusText = document.getElementById('statusText');
        const actionBtn = document.getElementById('actionBtn');
        const voiceToggle = document.getElementById('voiceToggle');
        const voiceStatus = document.getElementById('voiceStatus');
        
        const accX = document.getElementById('accX');
        const accY = document.getElementById('accY');
        const accZ = document.getElementById('accZ');
        
        const gyroX = document.getElementById('gyroX');
        const gyroY = document.getElementById('gyroY');
        const gyroZ = document.getElementById('gyroZ');

        let ws = null;
        let isStreaming = false;
        let recognition = null;

        // Initialize Web Speech API
        if ('webkitSpeechRecognition' in window || 'SpeechRecognition' in window) {
            const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
            recognition = new SpeechRecognition();
            recognition.continuous = true;
            recognition.interimResults = false;
            recognition.lang = 'en-US';

            recognition.onresult = (event) => {
                const lastResultIndex = event.results.length - 1;
                const transcript = event.results[lastResultIndex][0].transcript.trim().toLowerCase();
                console.log("Speech recognized: ", transcript);
                voiceStatus.innerText = `Heard: "${transcript}"`;
                
                // Clear word after 1.2s
                setTimeout(() => {
                    if (voiceStatus.innerText.includes(transcript)) {
                        voiceStatus.innerText = "Listening...";
                    }
                }, 1200);

                if (transcript.includes("left")) {
                    sendVoiceCommand("left");
                } else if (transcript.includes("right")) {
                    sendVoiceCommand("right");
                }
            };

            recognition.onstart = () => {
                voiceStatus.innerText = "Listening...";
                voiceStatus.style.color = "#38bdf8";
            };

            recognition.onend = () => {
                if (voiceToggle.checked) {
                    recognition.start(); // Auto-restart if toggle remains active
                } else {
                    voiceStatus.innerText = "";
                }
            };

            recognition.onerror = (err) => {
                console.error("Speech Recognition Error:", err.error);
                if (err.error === 'not-allowed') {
                    alert("Microphone access was denied.");
                    voiceToggle.checked = false;
                }
            };
        } else {
            voiceToggle.disabled = true;
            voiceStatus.innerText = "Voice Recognition not supported in this browser.";
            voiceStatus.style.color = "#ef4444";
        }

        function sendVoiceCommand(cmd) {
            if (ws && ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify({ voice: cmd }));
            }
        }

        voiceToggle.addEventListener('change', () => {
            if (voiceToggle.checked) {
                if (recognition) {
                    try {
                        recognition.start();
                    } catch (e) {
                        console.error("Error starting speech:", e);
                    }
                }
            } else {
                if (recognition) {
                    recognition.stop();
                }
            }
        });

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
                statusText.innerText = "Connected";
                statusBadge.className = "status-badge connected";
                actionBtn.innerText = "Stop Stream";
                actionBtn.style.background = "linear-gradient(135deg, #ef4444 0%, #b91c1c 100%)";
                actionBtn.style.boxShadow = "0 4px 12px rgba(239, 68, 68, 0.3)";
                isStreaming = true;
            };
            
            ws.onclose = () => {
                statusText.innerText = "Disconnected";
                statusBadge.className = "status-badge";
                actionBtn.innerText = "Start Stream";
                actionBtn.style.background = "linear-gradient(135deg, #0284c7 0%, #4f46e5 100%)";
                actionBtn.style.boxShadow = "0 4px 12px rgba(79, 70, 229, 0.3)";
                isStreaming = false;
                
                // Stop speech recognition
                voiceToggle.checked = false;
                if (recognition) recognition.stop();
                voiceStatus.innerText = "";
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
            const packet = {
                acc: [acc.x || 0, acc.y || 0, acc.z || 9.81],
                gyro: [gX, gY, gZ]
            };
            
            ws.send(JSON.stringify(packet));
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

        const touchPad = document.getElementById('touchPad');
        
        function triggerTouchTap() {
            if (ws && ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify({ touch: "tap" }));
                console.log("Touch TAP sent to WebSocket");
            }
        }
        
        // Listen to both click/tap and touch events
        touchPad.addEventListener('click', (e) => {
            e.preventDefault();
            triggerTouchTap();
        });
        touchPad.addEventListener('touchstart', (e) => {
            e.preventDefault();
            triggerTouchTap();
        });

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
        # Suppress spamming request logs in the main console
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/html")
        self.end_headers()
        self.wfile.write(HTML_CONTENT.encode("utf-8"))

    def do_POST(self):
        content_length = int(self.headers['Content-Length'])
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
            outlet.push_sample(latest_acc + latest_gyro + [latest_touch, latest_voice])
            
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status": "ok"}')
        except Exception as e:
            print(f"⚠️ HTTP Push Error: {e}")
            self.send_response(400)
            self.end_headers()

def generate_self_signed_cert():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import hashes
    from cryptography.x509.oid import NameOID
    from cryptography import x509
    import datetime
    import ipaddress

    local_ip = get_local_ip()
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )

    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "localhost"),
    ])

    cert = x509.CertificateBuilder().subject_name(
        subject
    ).issuer_name(
        issuer
    ).public_key(
        private_key.public_key()
    ).serial_number(
        x509.random_serial_number()
    ).not_valid_before(
        datetime.datetime.now(datetime.timezone.utc)
    ).not_valid_after(
        datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=365)
    ).add_extension(
        x509.SubjectAlternativeName([
            x509.DNSName("localhost"),
            x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
            x509.IPAddress(ipaddress.IPv4Address(local_ip))
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
    server = ThreadedHTTPServer((ip, port), WebBridgeHTTPHandler)
    
    # Generate self-signed certificate if key files are missing
    if not os.path.exists("cert.pem") or not os.path.exists("key.pem"):
        print("🔑 Generating self-signed SSL certificate for local HTTPS serving natively...")
        try:
            generate_self_signed_cert()
            print("✅ SSL Certificate generated successfully.")
        except Exception as e:
            print(f"⚠️ Failed to generate native SSL certificate: {e}")

    # Wrap the HTTP socket with SSL context
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain("cert.pem", "key.pem")
        server.socket = context.wrap_socket(server.socket, server_side=True)
    except Exception as e:
        print(f"❌ Error wrapping HTTP server in SSL: {e}. Check if cert.pem/key.pem exist.")
        
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
    parser.add_argument("--port", type=int, default=443, help="HTTP Server Port (WebSockets runs on port+1 and port+2)")
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

    # Setup SSL context for Secure WebSockets (WSS)
    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        ssl_context.load_cert_chain("cert.pem", "key.pem")
    except Exception as e:
        print(f"❌ Error loading SSL certs for Secure WebSockets: {e}")
        ssl_context = None

    print("==================================================")
    print("📲 Dual-Protocol Phone IMU LSL Bridge Active")
    print("==================================================")
    print("Option A: ZERO INSTALL (Mobile Web)")
    print("  1. Connect your phone to the same Wi-Fi network as this PC.")
    print(f"  2. Open: https://{display_ip}:{args.port}")
    print("  3. Bypass the local SSL warning (tap Advanced -> Proceed).")
    print("  4. Tap 'Enable Sensors' to begin streaming.")
    print("  5. Toggle 'Voice Commands' to control with speech!")
    print("--------------------------------------------------")
    print("Option B: NATIVE APP (Sensor Logger)")
    print("  1. Connect your phone to the same Wi-Fi network as this PC.")
    print("  2. In the app Settings -> Live Data Streaming:")
    print("     - Set Protocol to 'WebSockets' (JSON).")
    print(f"     - Set URL to: ws://{display_ip}:{ws_port_insecure}")
    print("  3. Tap orange 'Start' on the main screen to begin streaming.")
    print("==================================================")
    print(f"⌛ Listening for Insecure WS (port {ws_port_insecure}) and Secure WSS (port {ws_port_secure})...")

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
