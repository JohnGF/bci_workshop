# -*- coding: utf-8 -*-
# /// script
# requires-python = ">=3.12"
# dependencies = ["cryptography"]
# ///
"""
Generates a self-signed TLS certificate (cert.pem + key.pem) for local HTTPS.
The cert covers the machine's local IP and localhost so the phone can connect.
Valid for 10 years so you don't have to regenerate it often.
"""

import datetime
import ipaddress
import socket
import os

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 1))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


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
    print()
    print("━" * 54)
    print("  Next steps:")
    print(f"  1. Run:  uv run phone_imu_bridge.py")
    print(f"  2. On your phone open:  https://{local_ip}:8000")
    print("  3. Tap 'Advanced' → 'Proceed' to bypass the")
    print("     browser warning (self-signed cert is expected).")
    print("━" * 54)


if __name__ == "__main__":
    generate_cert(out_dir=os.path.dirname(os.path.abspath(__file__)))
