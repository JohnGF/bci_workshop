# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "cryptography",
#     "qrcode[pil]"
# ]
# ///
"""
Generates a self-signed TLS certificate (cert.pem + key.pem) for local HTTPS,
and generates QR codes (apk_qr.png) for the Android Watch app download and web controller.
"""

import datetime
import ipaddress
import socket
import os

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

try:
    import qrcode
    qr_available = True
except ImportError:
    qr_available = False


def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 1))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def generate_qr_code(url: str, filename: str, out_dir="."):
    """Generates a high-contrast QR code image."""
    if not qr_available:
        print(f"⚠️ 'qrcode' package not found. Skipping QR code generation for {url}")
        return None
    qr_path = os.path.join(out_dir, filename)
    qr = qrcode.QRCode(
        version=1,
        box_size=10,
        border=3,
    )
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    img.save(qr_path)
    print(f"📱 QR Code saved → {os.path.abspath(qr_path)} ({url})")
    return qr_path


def generate_cert(out_dir="."):
    local_ip = get_local_ip()
    print(f"📡 Detected local IP: {local_ip}")

    # --- Generate RSA private key ---
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    # --- Build certificate ---
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, local_ip),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "BCI Workshop Local"),
        x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, "Dev"),
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
        .not_valid_after(now + datetime.timedelta(days=3650))  # 10 years
        .add_extension(san, critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    # --- Write files ---
    cert_path = os.path.join(out_dir, "cert.pem")
    key_path  = os.path.join(out_dir, "key.pem")

    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))

    with open(key_path, "wb") as f:
        f.write(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ))

    print(f"✅ cert.pem written  → {os.path.abspath(cert_path)}")
    print(f"✅ key.pem  written  → {os.path.abspath(key_path)}")

    # --- Generate QR Codes ---
    apk_url = "https://github.com/JohnGF/galaxy_watch_lsl/releases"
    generate_qr_code(apk_url, "apk_qr.png", out_dir=out_dir)

    web_url_p1 = f"https://{local_ip}:8000/?p=1"
    generate_qr_code(web_url_p1, "web_controller_p1_qr.png", out_dir=out_dir)

    web_url_p2 = f"https://{local_ip}:8000/?p=2"
    generate_qr_code(web_url_p2, "web_controller_p2_qr.png", out_dir=out_dir)

    print()
    print("━" * 54)
    print("  Next steps:")
    print(f"  1. Galaxy Watch APK Download URL:")
    print(f"     {apk_url}  (Scan apk_qr.png)")
    print(f"  2. Mobile Web Controller:")
    print(f"     Player 1: https://{local_ip}:8000/?p=1  (Scan web_controller_p1_qr.png)")
    print(f"     Player 2: https://{local_ip}:8000/?p=2  (Scan web_controller_p2_qr.png)")
    print("  3. Start Bridge:  uv run phone_imu_bridge.py")
    print("━" * 54)


if __name__ == "__main__":
    generate_cert(out_dir=os.path.dirname(os.path.abspath(__file__)))
