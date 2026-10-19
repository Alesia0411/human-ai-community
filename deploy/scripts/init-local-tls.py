# 文件职责：生成本机 HTTPS 开发证书与 CA 文件；信任安装由外部部署流程处理。
"""Create a project-local development CA and localhost TLS certificate.

No trust-store mutations. Import only local-ca.crt into the current user's trust
store when localhost HTTPS is desired. Private keys stay in ignored directories.
"""

import ipaddress
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

ROOT = Path(__file__).resolve().parents[2]


# main：在目标文件不存在时生成开发 CA 与 localhost 证书及私钥，不修改系统信任库。
def main():
    certs, secrets = ROOT / "deploy/certs", ROOT / "deploy/secrets"
    certs.mkdir(parents=True, exist_ok=True)
    secrets.mkdir(parents=True, exist_ok=True)
    outputs = [
        certs / "local-ca.crt",
        secrets / "local-ca.key",
        certs / "server.crt",
        certs / "server.key",
    ]
    if any(p.exists() for p in outputs):
        raise SystemExit("Existing TLS files found; no certificates were overwritten.")
    now = datetime.now(timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "Human AI Local Development CA")]
    )
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )
    server = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
        .issuer_name(ca_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=90))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    outputs[0].write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    outputs[1].write_bytes(
        ca_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    outputs[2].write_bytes(server.public_bytes(serialization.Encoding.PEM))
    outputs[3].write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    print(
        "Created localhost TLS files; CA SHA256: "
        + ca.fingerprint(hashes.SHA256()).hex()
    )


if __name__ == "__main__":
    main()
