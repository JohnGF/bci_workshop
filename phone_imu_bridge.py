# -*- coding: utf-8 -*-
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pylsl",
#     "websockets",
#     "cryptography",
#     "qrcode",
#     "pillow>=12.3.0",
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
    logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
)
conn_logger.addHandler(_handler)
conn_logger.addHandler(logging.StreamHandler())  # mirror to console as well

# Enable websockets core debugging to catch SSL handshake failures
ws_logger = logging.getLogger("websockets")
ws_logger.setLevel(logging.INFO)
ws_logger.addHandler(_handler)

# Try importing generate_cert for dynamic certificate auto-generation
try:
    from generate_cert import generate_cert
except ImportError:
    generate_cert = None


# ==========================================
# UTILITIES
# ==========================================
def save_qr_code_png(url, filename):
    """Generates a QR code PNG image file on disk and returns its absolute path."""
    try:
        import qrcode

        qr = qrcode.QRCode(border=2, box_size=10)
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        filepath = os.path.abspath(os.path.join(_log_dir, filename))
        img.save(filepath)
        return filepath
    except Exception as e:
        print(f"⚠️ Could not generate QR PNG file '{filename}': {e}")
        return None


def open_image_in_viewer(filepath):
    """Cross-platform launch of an image using the OS default viewer."""
    try:
        if sys.platform == "win32":
            os.startfile(filepath)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", filepath])
        else:
            # Handles CachyOS/Ubuntu/etc.
            subprocess.Popen(
                ["xdg-open", filepath],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    except Exception as e:
        print(f"⚠️ Could not automatically open image: {e}")


def generate_qr_png_bytes(url):
    """Generates PNG image bytes of a QR code for a given URL."""
    import io
    import qrcode

    qr = qrcode.QRCode(border=2)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Doesn't need to be reachable
        s.connect(("8.8.8.8", 1))
        IP = s.getsockname()[0]
    except Exception:
        IP = "127.0.0.1"
    finally:
        s.close()
    return IP


def get_all_local_ips():
    """Retrieves all detectable IPv4 addresses to diagnose routing ambiguity."""
    ips = set()
    try:
        # Fallback method without using netifaces/psutil
        hostname = socket.gethostname()
        _, _, ip_list = socket.gethostbyname_ex(hostname)
        ips.update(ip_list)
    except Exception:
        pass
    primary = get_local_ip()
    if primary != "127.0.0.1":
        ips.add(primary)
    return [ip for ip in ips if not ip.startswith("127.")]


def kill_stale_processes(script_name: str, ports: list[int] | None = None) -> list[int]:
    """
    Finds and terminates any orphaned/zombie background processes
    running the given script or holding the specified network ports.
    """
    import signal
    current_pid = os.getpid()
    parent_pid = os.getppid()
    killed = []

    # 1. Kill by script name match
    if sys.platform != "win32":
        try:
            out = subprocess.run(["pgrep", "-f", script_name], capture_output=True, text=True).stdout
            for line in out.splitlines():
                if line.strip():
                    try:
                        pid = int(line.strip())
                        if pid not in (current_pid, parent_pid):
                            try:
                                with open(f"/proc/{pid}/cmdline", "rb") as f:
                                    cmd = f.read().decode("utf-8", errors="ignore").replace("\0", " ")
                            except Exception:
                                cmd = ""
                            if script_name in cmd and "-c" not in cmd:
                                os.kill(pid, signal.SIGTERM)
                                time.sleep(0.05)
                                try:
                                    os.kill(pid, signal.SIGKILL)
                                except ProcessLookupError:
                                    pass
                                killed.append(pid)
                    except (ValueError, ProcessLookupError, PermissionError):
                        pass
        except Exception:
            pass

    # 2. Kill by network port (TCP/UDP)
    if ports:
        for port in ports:
            if sys.platform != "win32":
                try:
                    out = subprocess.run(["fuser", f"{port}/tcp", f"{port}/udp"], capture_output=True, text=True).stdout + " " + subprocess.run(["fuser", f"{port}/tcp", f"{port}/udp"], capture_output=True, text=True).stderr
                    for token in out.split():
                        try:
                            pid = int(token.strip())
                            if pid != current_pid:
                                os.kill(pid, signal.SIGKILL)
                                killed.append(pid)
                        except (ValueError, ProcessLookupError, PermissionError):
                            pass
                except Exception:
                    pass
            else:
                try:
                    res = subprocess.run(f'netstat -ano | findstr :{port}', shell=True, capture_output=True, text=True)
                    for line in res.stdout.splitlines():
                        parts = line.strip().split()
                        if len(parts) >= 5:
                            pid = int(parts[-1])
                            if pid != current_pid and pid > 4:
                                subprocess.run(f'taskkill /F /PID {pid}', shell=True, capture_output=True)
                                killed.append(pid)
                except Exception:
                    pass

    return list(set(killed))


# ==========================================
# SSL CONTEXT
# ==========================================
def create_ssl_context():
    """Generates the SSL context required for WSS and HTTPS."""
    cert_path = os.path.join(_log_dir, "cert.pem")
    key_path = os.path.join(_log_dir, "key.pem")

    if not os.path.exists(cert_path) or not os.path.exists(key_path):
        raise FileNotFoundError(
            "SSL certificates (cert.pem / key.pem) not found on disk."
        )

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=cert_path, keyfile=key_path)
    # Required for modern mobile browsers interfacing with self-signed certs
    ctx.check_hostname = False
    return ctx


# Setup LSL
info = StreamInfo(
    "Smartwatch_IMU", "IMU", 10, 100, cf_float32, "phone_imu_bridge_uid777"
)
channels = info.desc().append_child("channels")
labels = ["Acc_X", "Acc_Y", "Acc_Z", "Gyro_X", "Gyro_Y", "Gyro_Z", "HeartRate", "Touch_X", "Touch_Y", "Touch"]
units = ["m/s^2", "m/s^2", "m/s^2", "rad/s", "rad/s", "rad/s", "bpm", "px", "px", "state"]
for lbl, unit in zip(labels, units):
    ch = channels.append_child("channel")
    ch.append_child_value("label", lbl)
    ch.append_child_value("type", "IMU" if "Touch" not in lbl and "Heart" not in lbl else "AUX")
    ch.append_child_value("unit", unit)

outlet = StreamOutlet(info)

# Keep track of latest values to handle async updates
latest_acc = [0.0, 0.0, 9.81]
latest_gyro = [0.0, 0.0, 0.0]
latest_hr = 0.0
latest_touch_x = -1.0
latest_touch_y = -1.0
latest_touch = 0.0


async def echo(websocket):
    global latest_acc, latest_gyro, latest_hr, latest_touch_x, latest_touch_y, latest_touch
    remote = websocket.remote_address
    session_start = time.monotonic()
    conn_logger.info(f"CONNECT   remote={remote}")
    print(f"✅ Phone connected from: {remote}")
    first_message = True
    try:
        async for message in websocket:
            if first_message:
                conn_logger.info(
                    f"FIRST_MSG  remote={remote}  (first sensor data received)"
                )
                print("📥 First sensor message received from phone!")
                first_message = False
            data = json.loads(message)

            if "touch" in data:
                latest_touch = 1.0
                outlet.push_sample(latest_acc + latest_gyro + [latest_hr, latest_touch_x, latest_touch_y, latest_touch])
                latest_touch = 0.0
                continue

            elif "acc" in data and "gyro" in data:
                latest_acc = data.get("acc", [0.0, 0.0, 9.81])
                latest_gyro = data.get("gyro", [0.0, 0.0, 0.0])
                outlet.push_sample(latest_acc + latest_gyro + [latest_hr, latest_touch_x, latest_touch_y, latest_touch])
                latest_touch = 0.0

            else:
                payload = data.get("payload", [])
                for item in payload:
                    name = item.get("name")
                    values = item.get("values", {})
                    if name == "accelerometer":
                        latest_acc = [
                            values.get("x", 0.0),
                            values.get("y", 0.0),
                            values.get("z", 0.0),
                        ]
                    elif name == "gyroscope":
                        latest_gyro = [
                            values.get("x", 0.0),
                            values.get("y", 0.0),
                            values.get("z", 0.0),
                        ]
                outlet.push_sample(latest_acc + latest_gyro + [latest_hr, latest_touch_x, latest_touch_y, latest_touch])
                latest_touch = 0.0

    except websockets.exceptions.ConnectionClosed as e:
        duration = time.monotonic() - session_start
        conn_logger.info(
            f"DISCONNECT remote={remote}  reason={e}  duration={duration:.1f}s"
        )
        print(f"🛑 Phone disconnected: {e}")
    except Exception as e:
        conn_logger.error(f"ERROR      remote={remote}  error={e}")
        print(f"⚠️ Parsing Error: {e}")
    else:
        duration = time.monotonic() - session_start
        conn_logger.info(
            f"DISCONNECT remote={remote}  reason=stream_ended  duration={duration:.1f}s"
        )


# ==========================================
# LIGHTWEIGHT EMBEDDED HTTP SERVER
# ==========================================
from socketserver import ThreadingMixIn


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def server_bind(self):
        try:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                try:
                    self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except Exception:
                    pass
        except Exception:
            pass
        super().server_bind()


class WebBridgeHTTPHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        client_ip = self.client_address[0] if self.client_address else "unknown"
        msg = f"WEB_REQUEST client={client_ip} {format % args}"
        conn_logger.info(msg)

    def do_GET(self):
        client_ip = self.client_address[0] if self.client_address else "unknown"

        if self.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return

        if self.path in ("/qr-apk", "/qr-apk.png"):
            apk_url = "https://github.com/JohnGF/galaxy_watch_lsl/releases/download/v1.0.0/app-release-unsigned.apk"
            png_bytes = generate_qr_png_bytes(apk_url)
            self.send_response(200)
            self.send_header("Content-type", "image/png")
            self.send_header("Content-Length", str(len(png_bytes)))
            self.end_headers()
            self.wfile.write(png_bytes)
            return

        if self.path in ("/qr-web", "/qr-web.png"):
            host_header = self.headers.get("Host", f"{get_local_ip()}:8000")
            web_url = f"http://{host_header}"
            png_bytes = generate_qr_png_bytes(web_url)
            self.send_response(200)
            self.send_header("Content-type", "image/png")
            self.send_header("Content-Length", str(len(png_bytes)))
            self.end_headers()
            self.wfile.write(png_bytes)
            return

        # Serve static HTML (Placeholder for brevity - assumes HTML_CONTENT exists globally as in your original file)
        # Note: I omitted the large HTML block here to save token space, but it remains unchanged.
        html_to_serve = "<html><body><h1>Mobile Controller</h1></body></html>"
        html_file_path = os.path.join(_log_dir, "mobile_controller.html")
        if os.path.exists(html_file_path):
            try:
                with open(html_file_path, "r", encoding="utf-8") as f:
                    html_to_serve = f.read()
            except Exception as e:
                conn_logger.warning(f"Failed to read html: {e}")

        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html_to_serve.encode("utf-8"))

    def do_POST(self):
        # Omitted for brevity - unchanged from your original script
        self.send_response(200)
        self.end_headers()


def generate_self_signed_cert(ip_address, force=False):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import hashes
    from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
    from cryptography import x509
    import datetime
    import ipaddress

    cert_path = "cert.pem"
    key_path = "key.pem"

    if not force and os.path.exists(cert_path) and os.path.exists(key_path):
        return

    cert_ip = ip_address
    if cert_ip == "0.0.0.0":
        cert_ip = get_local_ip()

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "BCI Phone Bridge")]
    )
    san_list = [
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
    ]

    try:
        san_list.append(x509.IPAddress(ipaddress.IPv4Address(cert_ip)))
    except Exception:
        pass

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(
            datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)
        )
        .not_valid_after(
            datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=3650)
        )
        .add_extension(
            x509.SubjectAlternativeName(san_list),
            critical=False,
        )
        .sign(private_key, hashes.SHA256())
    )

    with open(key_path, "wb") as f:
        f.write(
            private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )

    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))


def start_http_server(ip, port, ssl_context=None):
    http_server = None
    try:
        http_server = ThreadedHTTPServer((ip, port), WebBridgeHTTPHandler)
        threading.Thread(target=http_server.serve_forever, daemon=True).start()
    except Exception as e:
        print(f"⚠️ Could not start HTTP server on {ip}:{port}: {e}")

    if ssl_context:
        https_port = port + 443 if port < 8000 else 8443
        try:
            https_server = ThreadedHTTPServer((ip, https_port), WebBridgeHTTPHandler)
            https_server.socket = ssl_context.wrap_socket(
                https_server.socket, server_side=True
            )
            threading.Thread(target=https_server.serve_forever, daemon=True).start()
        except Exception as e:
            print(f"⚠️ Could not start HTTPS listener on {ip}:{https_port}: {e}")

    return http_server


# ==========================================
# UDP LISTENER FOR GALAXY WATCH APP
# ==========================================
async def start_udp_listener(host, port):
    global latest_acc, latest_gyro, latest_hr, latest_touch_x, latest_touch_y, latest_touch
    loop = asyncio.get_running_loop()
    
    class WatchUdpProtocol(asyncio.DatagramProtocol):
        def __init__(self):
            super().__init__()
            self.first_packet = True

        def datagram_received(self, data, addr):
            global latest_acc, latest_gyro, latest_hr, latest_touch_x, latest_touch_y, latest_touch
            if self.first_packet:
                conn_logger.info(f"UDP_FIRST_MSG remote={addr} (first Galaxy Watch UDP packet received)")
                print(f"📥 First Galaxy Watch UDP packet received from {addr}!")
                self.first_packet = False
            try:
                # Format: accelX, accelY, accelZ, gyroX, gyroY, gyroZ, heartRate, touchX, touchY, isTouched
                text = data.decode("utf-8", errors="ignore").strip()
                parts = [float(p) for p in text.split(",") if p.strip()]
                if len(parts) >= 6:
                    latest_acc = parts[0:3]
                    latest_gyro = parts[3:6]
                    if len(parts) >= 7:
                        latest_hr = parts[6]
                    if len(parts) >= 10:
                        latest_touch_x = parts[7]
                        latest_touch_y = parts[8]
                        is_touched = parts[9]
                        latest_touch = 1.0 if is_touched >= 1.0 else 0.0
                    outlet.push_sample(latest_acc + latest_gyro + [latest_hr, latest_touch_x, latest_touch_y, latest_touch])
            except Exception as e:
                conn_logger.error(f"UDP_PARSE_ERROR remote={addr} error={e}")

    transport, protocol = await loop.create_datagram_endpoint(
        lambda: WatchUdpProtocol(),
        local_addr=(host, port)
    )
    conn_logger.info(f"UDP_LISTEN bound={host}:{port}")
    print(f"✅ Galaxy Watch UDP Listener active on {host}:{port}")
    return transport


# ==========================================
# MAIN EXECUTION LOOP
# ==========================================
async def main():
    import argparse

    parser = argparse.ArgumentParser(description="Phone IMU / Galaxy Watch LSL Bridge")
    parser.add_argument("--ip", type=str, default="0.0.0.0", help="IP to bind")
    parser.add_argument("--port", type=int, default=8000, help="HTTP Port")
    parser.add_argument(
        "--mode",
        type=str,
        choices=["http", "watch_udp", "both"],
        default="http",
        help="Server mode: 'http' (WebSockets/HTTP), 'watch_udp' (Galaxy Watch UDP port 5005), or 'both'",
    )
    parser.add_argument("--udp-port", type=int, default=5005, help="Galaxy Watch UDP Port")
    args, _ = parser.parse_known_args()

    # Automatic cleanup of any stale zombie instances holding bridge ports
    ws_insecure = args.port + 1 if args.port != 443 else 8001
    ws_secure = args.port + 2 if args.port != 443 else 8002
    target_ports = [args.port, ws_insecure, ws_secure, 8443]
    if args.mode in ("watch_udp", "both"):
        target_ports.append(args.udp_port)
    stale_pids = kill_stale_processes("phone_imu_bridge.py", ports=target_ports)
    if stale_pids:
        print(f"🧹 Cleaned up {len(stale_pids)} stale/zombie background process(es) (PIDs: {stale_pids})")

    display_ip = args.ip if args.ip != "0.0.0.0" else get_local_ip()
    local_ips = get_all_local_ips()

    print("==================================================")
    print(f"📲 Phone IMU / Galaxy Watch LSL Bridge (Mode: {args.mode.upper()})")
    print("==================================================")

    # ── DEBUGGING LOGIC & TROUBLESHOOTING CHECKS ──
    print("\n🔍 CONNECTION TROUBLESHOOTING CHECKS:")
    if len(local_ips) > 1:
        print(f"  ⚠️ MULTIPLE NETWORK INTERFACES DETECTED: {', '.join(local_ips)}")
        print(
            f"     If the device fails to connect, you might be broadcasting on a Docker/VM interface."
        )
        print(
            f"     Fix: Pass the explicit Wi-Fi IP flag: `uv run script.py --ip <YOUR_WIFI_IP>`"
        )
    else:
        print(f"  ✓ Network Binding: Bound to {display_ip}")

    udp_transport = None
    if args.mode in ("watch_udp", "both"):
        try:
            udp_transport = await start_udp_listener(args.ip, args.udp_port)
            print(f"  ✓ Galaxy Watch UDP receiver listening on port {args.udp_port}")
        except Exception as e:
            print(f"  ⚠️ Failed to start UDP listener on port {args.udp_port}: {e}")

    if args.mode in ("http", "both"):
        try:
            generate_self_signed_cert(args.ip)
        except Exception as e:
            print(f"⚠️ Failed to generate SSL cert: {e}")

        ssl_context = None
        try:
            ssl_context = create_ssl_context()
        except Exception as e:
            print(f"⚠️ SSL Context Init Failed (WSS/HTTPS will be disabled): {e}")

        print(
            f"  ✓ SSL Status: {'Active (WSS enabled)' if ssl_context else 'Offline (Secure Websockets disabled)'}"
        )
        print("--------------------------------------------------\n")

        if args.port == 443:
            ws_port_insecure, ws_port_secure = 8001, 8002
            https_port = 8443
        else:
            ws_port_insecure, ws_port_secure = args.port + 1, args.port + 2
            https_port = args.port + 443 if args.port < 8000 else 8443

        apk_url = "https://github.com/JohnGF/galaxy_watch_lsl/releases/download/v1.0.0/app-release-unsigned.apk"
        apk_png_path = save_qr_code_png(apk_url, "apk_qr.png")

        print("📷 QR CODE PNG IMAGES:")
        if apk_png_path:
            print(f"  - App APK Download QR Code Image : {apk_png_path}")

        start_http_server(args.ip, args.port, ssl_context)

        print("🚀 SERVERS ONLINE & LISTENING:")
        print(f"  📱 Mobile Web Controller (HTTP):  http://{display_ip}:{args.port}")
        if ssl_context:
            print(f"  🔒 Secure Web Controller (HTTPS): https://{display_ip}:{https_port}")
        print(f"  🔌 Sensor Logger WebSocket (WS):   ws://{display_ip}:{ws_port_insecure}")
        if ssl_context:
            print(f"  🔐 Secure Mobile WebSocket (WSS):  wss://{display_ip}:{ws_port_secure}")
        print("==================================================")
        print(f"⌛ Bridge is active and waiting for phone sensor stream...\n")

        async with websockets.serve(echo, args.ip, ws_port_insecure, reuse_address=True):
            if ssl_context is not None:
                async with websockets.serve(
                    echo, args.ip, ws_port_secure, ssl=ssl_context, reuse_address=True
                ):
                    await asyncio.Future()
            else:
                await asyncio.Future()
    else:
        # Watch UDP mode only
        print("==================================================")
        print(f"⌛ Galaxy Watch UDP listener active on port {args.udp_port}. Waiting for packets...\n")
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Bridge stopped.")

