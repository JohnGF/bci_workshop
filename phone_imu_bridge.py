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
        s.connect(('8.8.8.8', 1))
        IP = s.getsockname()[0]
    except Exception:
        IP = '127.0.0.1'
    finally:
        s.close()
    return IP


def get_all_local_ips():
    """Retrieves all detectable IPv4 addresses to diagnose routing ambiguity."""
    ips = set()
    try:
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
    """Finds and terminates any orphaned background processes holding ports."""
    import signal
    current_pid = os.getpid()
    parent_pid = os.getppid()
    killed = []

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

    if ports:
        for port in ports:
            if sys.platform != "win32":
                try:
                    out = subprocess.run(["fuser", f"{port}/tcp", f"{port}/udp"], capture_output=True, text=True).stdout + " " + subprocess.run(["fuser", f"{port}/tcp", f"{port}/udp"], capture_output=True, text=True).stderr
                    for token in out.split():
                        try:
                            pid = int(token.strip())
                            if pid != current_pid and pid != parent_pid:
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


# Setup Multi-Player LSL Stream Factory
def create_imu_stream_outlet(player_id: int = 1, legacy_name: str | None = None) -> StreamOutlet:
    name = legacy_name if legacy_name else f"Smartwatch_IMU_P{player_id}"
    uid = f"phone_imu_bridge_p{player_id}_uid777" if not legacy_name else "phone_imu_bridge_uid777"
    info = StreamInfo(name, "IMU", 7, 100, cf_float32, uid)
    channels = info.desc().append_child("channels")
    labels = ["Acc_X", "Acc_Y", "Acc_Z", "Gyro_X", "Gyro_Y", "Gyro_Z", "Touch"]
    units  = ["m/s^2", "m/s^2", "m/s^2", "rad/s", "rad/s", "rad/s", "state"]
    for lbl, unit in zip(labels, units):
        ch = channels.append_child("channel")
        ch.append_child_value("label", lbl)
        ch.append_child_value("type", "IMU")
        ch.append_child_value("unit", unit)
    return StreamOutlet(info)

# Outlets for Player 1, Player 2, and legacy fallback alias
outlets: dict[int, StreamOutlet] = {
    1: create_imu_stream_outlet(1),
    2: create_imu_stream_outlet(2),
}
legacy_outlet = create_imu_stream_outlet(1, legacy_name="Smartwatch_IMU")

# Per-player sensor states: { player_id: {"acc": [...], "gyro": [...], "touch": 0.0} }
player_states: dict[int, dict] = {}

def get_player_state(player_id: int) -> dict:
    if player_id not in player_states:
        player_states[player_id] = {
            "acc": [0.0, 0.0, 9.81],
            "gyro": [0.0, 0.0, 0.0],
            "touch": 0.0
        }
    return player_states[player_id]

def get_player_outlet(player_id: int) -> StreamOutlet:
    if player_id not in outlets:
        outlets[player_id] = create_imu_stream_outlet(player_id)
    return outlets[player_id]

def push_player_sample(player_id: int, touch_event: bool = False):
    st = get_player_state(player_id)
    t_val = 1.0 if touch_event else st["touch"]
    sample = st["acc"] + st["gyro"] + [t_val]
    get_player_outlet(player_id).push_sample(sample)
    if player_id == 1:
        legacy_outlet.push_sample(sample)
    st["touch"] = 0.0


async def echo(websocket):
    remote = websocket.remote_address
    session_start = time.monotonic()
    conn_logger.info(f"CONNECT    remote={remote}")
    print(f"✅ Phone connected from: {remote}")
    first_message = True
    assigned_player = 1
    try:
        async for message in websocket:
            if isinstance(message, bytes):
                message = message.decode("utf-8", errors="ignore")

            try:
                data = json.loads(message)
            except Exception:
                continue

            # Extract player ID if provided in packet, otherwise default to 1
            if isinstance(data, dict) and "player" in data:
                try:
                    assigned_player = max(1, int(data["player"]))
                except (ValueError, TypeError):
                    assigned_player = 1

            if first_message:
                conn_logger.info(f"FIRST_MSG  remote={remote}  player={assigned_player} (first sensor data received)")
                print(f"📥 First sensor message received from Phone (Player {assigned_player})!")
                first_message = False

            st = get_player_state(assigned_player)

            # 1. Custom Web Browser / Mobile App Touch Action
            if isinstance(data, dict) and ("touch" in data or "action" in data or data.get("isTouched")):
                push_player_sample(assigned_player, touch_event=True)
                push_player_sample(assigned_player, touch_event=True) # Double pulse ensures non-blocking chunk readers capture it
                continue

            # 2. Custom Android App / Web Browser Direct Sensor Format: {"acc": [...], "gyro": [...]}
            elif isinstance(data, dict) and "acc" in data and "gyro" in data:
                st["acc"] = data.get("acc", [0.0, 0.0, 9.81])
                st["gyro"] = data.get("gyro", [0.0, 0.0, 0.0])
                push_player_sample(assigned_player)

            # 3. Third-Party 'Sensor Logger' App Format
            elif isinstance(data, dict) and "payload" in data:
                payload = data.get("payload", [])
                for item in payload:
                    name = item.get("name", "")
                    values = item.get("values", {})
                    if "accelerometer" in name or "gravity" in name or "total_acceleration" in name:
                        st["acc"] = [values.get("x", st["acc"][0]), values.get("y", st["acc"][1]), values.get("z", st["acc"][2])]
                    elif "gyroscope" in name or "orientation" in name:
                        st["gyro"] = [values.get("x", st["gyro"][0]), values.get("y", st["gyro"][1]), values.get("z", st["gyro"][2])]
                push_player_sample(assigned_player)

            # 4. Fallback array of items: [{"name": "accelerometer", ...}]
            elif isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        name = item.get("name", "")
                        values = item.get("values", {})
                        if "accelerometer" in name:
                            st["acc"] = [values.get("x", st["acc"][0]), values.get("y", st["acc"][1]), values.get("z", st["acc"][2])]
                        elif "gyroscope" in name:
                            st["gyro"] = [values.get("x", st["gyro"][0]), values.get("y", st["gyro"][1]), values.get("z", st["gyro"][2])]
                push_player_sample(assigned_player)

    except websockets.exceptions.ConnectionClosed as e:
        duration = time.monotonic() - session_start
        conn_logger.info(f"DISCONNECT remote={remote}  player={assigned_player}  reason={e}  duration={duration:.1f}s")
        print(f"🛑 Phone (Player {assigned_player}) disconnected: {e}")
    except Exception as e:
        conn_logger.error(f"ERROR      remote={remote}  error={e}")
        print(f"⚠️ Parsing Error: {e}")
    else:
        duration = time.monotonic() - session_start
        conn_logger.info(f"DISCONNECT remote={remote}  reason=stream_ended  duration={duration:.1f}s")


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
        if self.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return

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


def generate_self_signed_cert(ip_address, force=False):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import hashes
    from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
    from cryptography import x509
    import datetime
    import ipaddress

    cert_path = os.path.join(_log_dir, "cert.pem")
    key_path = os.path.join(_log_dir, "key.pem")

    cert_ip = ip_address if ip_address != "0.0.0.0" else get_local_ip()

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

    primary_ip = get_local_ip()
    if primary_ip not in seen_ips:
        try:
            san_list.append(x509.IPAddress(ipaddress.IPv4Address(primary_ip)))
            seen_ips.add(primary_ip)
        except Exception:
            pass

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1))
        .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(san_list), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_encipherment=True,
                key_cert_sign=True,
                crl_sign=False,
                content_commitment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([
                ExtendedKeyUsageOID.SERVER_AUTH,
                ExtendedKeyUsageOID.CLIENT_AUTH,
            ]),
            critical=False,
        )
        .sign(private_key, hashes.SHA256())
    )

    with open(key_path, "wb") as f:
        f.write(private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ))

    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))


def start_http_server(ip, port, ssl_context=None):
    server = None
    try:
        server = ThreadedHTTPServer((ip, port), WebBridgeHTTPHandler)
        if ssl_context:
            try:
                server.socket = ssl_context.wrap_socket(server.socket, server_side=True)
                print(f"🔒 HTTPS Server SSL socket active on port {port}.")
            except Exception as e:
                print(f"⚠️ Could not wrap HTTPS server: {e}")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
    except Exception as e:
        print(f"⚠️ Could not start HTTP/HTTPS server on {ip}:{port}: {e}")
    return server


# ==========================================
# UDP LISTENER (Galaxy Watch & Custom Android UDP)
# ==========================================
async def start_udp_listener(host, port):
    global latest_acc, latest_gyro, latest_touch
    loop = asyncio.get_running_loop()

    class SensorUdpProtocol(asyncio.DatagramProtocol):
        def __init__(self):
            super().__init__()
            self.first_packet = True

        def datagram_received(self, data, addr):
            if self.first_packet:
                conn_logger.info(f"UDP_FIRST_MSG remote={addr} (first UDP sensor packet received)")
                print(f"📥 First UDP sensor packet received from {addr}!")
                self.first_packet = False
            try:
                text = data.decode("utf-8", errors="ignore").strip()
                assigned_player = 1

                # 1. JSON UDP packet
                if text.startswith("{"):
                    try:
                        p = json.loads(text)
                        if "player" in p:
                            try:
                                assigned_player = max(1, int(p["player"]))
                            except (ValueError, TypeError):
                                assigned_player = 1
                        st = get_player_state(assigned_player)

                        if "touch" in p:
                            push_player_sample(assigned_player, touch_event=True)
                            return
                        elif "acc" in p and "gyro" in p:
                            st["acc"] = p["acc"]
                            st["gyro"] = p["gyro"]
                            push_player_sample(assigned_player)
                            return
                        elif "accel" in p and "gyro" in p:
                            st["acc"] = p["accel"]
                            st["gyro"] = p["gyro"]
                            push_player_sample(assigned_player)
                            return
                    except Exception:
                        pass

                # 2. CSV format: accelX,accelY,accelZ,gyroX,gyroY,gyroZ,[hr],[touchX],[touchY],[isTouched],[player]
                parts = [float(p) for p in text.split(",") if p.strip()]
                if len(parts) >= 6:
                    if len(parts) >= 11:
                        try:
                            assigned_player = max(1, int(parts[10]))
                        except Exception:
                            assigned_player = 1
                    st = get_player_state(assigned_player)
                    st["acc"] = parts[0:3]
                    st["gyro"] = parts[3:6]
                    is_touch = False
                    if len(parts) >= 10:
                        is_touch = (parts[9] >= 1.0)
                    push_player_sample(assigned_player, touch_event=is_touch)
            except Exception as e:
                conn_logger.error(f"UDP_PARSE_ERROR remote={addr} error={e}")

    try:
        transport, protocol = await loop.create_datagram_endpoint(
            lambda: SensorUdpProtocol(),
            local_addr=(host, port)
        )
        conn_logger.info(f"UDP_LISTEN bound={host}:{port}")
        print(f"📡 UDP Sensor Receiver active on port {port}")
        return transport
    except Exception as e:
        print(f"⚠️ Could not bind UDP port {port}: {e}")
        return None


# ==========================================
# MAIN EXECUTION LOOP
# ==========================================
async def main():
    import argparse
    parser = argparse.ArgumentParser(description="Phone IMU LSL Bridge")
    parser.add_argument("--ip", type=str, default="0.0.0.0", help="IP address to bind the WebSocket/HTTP servers to")
    parser.add_argument("--port", type=int, default=8000, help="HTTP Server Port (WebSockets runs on port+1 and port+2)")
    parser.add_argument("--udp-port", type=int, default=5005, help="UDP Port for custom Android / Watch streaming")
    args, _ = parser.parse_known_args()

    display_ip = args.ip if args.ip != "0.0.0.0" else get_local_ip()

    if args.port == 443:
        ws_port_insecure = 8001
        ws_port_secure = 8002
    else:
        ws_port_insecure = args.port + 1
        ws_port_secure = args.port + 2

    # Automatic cleanup of stale zombie instances
    stale_pids = kill_stale_processes("phone_imu_bridge.py", ports=[args.port, ws_port_insecure, ws_port_secure, args.udp_port])
    if stale_pids:
        print(f"🧹 Cleaned up {len(stale_pids)} stale background process(es) (PIDs: {stale_pids})")

    # Start UDP listener for custom Android / Watch streaming
    await start_udp_listener(args.ip, args.udp_port)

    # Generate fresh self-signed SSL certificate matching current IP
    try:
        generate_self_signed_cert(args.ip)
    except Exception as e:
        print(f"⚠️ Failed to generate SSL cert: {e}")

    # Setup SSL context for Secure WebSockets (WSS) and HTTPS
    ssl_context = None
    cert_path = os.path.join(_log_dir, "cert.pem")
    key_path = os.path.join(_log_dir, "key.pem")
    if os.path.exists(cert_path) and os.path.exists(key_path):
        try:
            ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ssl_context.load_cert_chain(cert_path, key_path)
        except Exception as e:
            print(f"❌ Error loading SSL certs: {e}")
            ssl_context = None

    # Start background HTTP/HTTPS server
    http_server = start_http_server(args.ip, args.port, ssl_context)

    print("==================================================")
    print("📲 Dual-Protocol Phone IMU LSL Bridge Active")
    print("==================================================")
    print(f"Option A: ZERO INSTALL (Mobile Web Controller)")
    print(f"  - Open on phone: https://{display_ip}:{args.port}")
    print("--------------------------------------------------")
    print(f"Option B: CUSTOM UDP ANDROID APP (UDP Broadcast)")
    print(f"  - Destination IP:   {display_ip}")
    print(f"  - Destination Port: {args.udp_port}")
    print("--------------------------------------------------")
    print(f"Option C: WEBSOCKET APPS (Sensor Logger / Custom WS)")
    print(f"  - Set URL to: ws://{display_ip}:{ws_port_insecure}")
    print("==================================================")
    print(f"⌛ Listening for UDP packets on port {args.udp_port} & WebSockets on port {ws_port_insecure}...")

    # Start both WebSocket servers concurrently with zero-latency streaming
    ws_kwargs = {"ping_interval": None, "ping_timeout": None, "max_size": 2**20, "reuse_address": True}
    insecure_server = websockets.serve(echo, args.ip, ws_port_insecure, **ws_kwargs)
    if ssl_context is not None:
        secure_server = websockets.serve(echo, args.ip, ws_port_secure, ssl=ssl_context, **ws_kwargs)
        async with insecure_server, secure_server:
            await asyncio.Future()  # Run forever
    else:
        async with insecure_server:
            await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Bridge stopped.")
