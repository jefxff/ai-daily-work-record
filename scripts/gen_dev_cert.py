"""生成本地自签 CA 与服务器证书（内网 HTTPS 用，无需联网、无需 openssl 可执行文件）。

为什么是「本地 CA + 服务器证书」而不是单张自签证书：
- 单张自签证书每次生成都是新的「未知颁发者」，浏览器会持续红警告，用户习惯性
  点「继续访问」——安全提示被无视，等于没有加密的收益；
- 生成一次本地 CA 后，把它装进系统/浏览器的「受信任根」，之后**重新签发的
  服务器证书依然受信**，续期、换 IP 都不用再让用户点警告。

输出（默认 ./certs/，已在 .gitignore 中排除）：
    ca.key.pem / ca.crt.pem          本地根证书（私钥务必离线保存，不要分发）
    server.key.pem / server.crt.pem  服务器证书（SAN 含 localhost / 回环 / 主机名 / 内网 IP）

用法：
    python scripts/gen_dev_cert.py                  # 生成 CA（若不存在）+ 服务器证书
    python scripts/gen_dev_cert.py --days 730       # 指定服务器证书有效期
    python scripts/gen_dev_cert.py --force          # 强制重新签发服务器证书

装信任（Windows，管理员 PowerShell）：
    certutil -addstore -f "ROOT" certs\\ca.crt.pem
"""
import argparse
import datetime
import ipaddress
import socket
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

BASE_DIR = Path(__file__).resolve().parent.parent
CERT_DIR = BASE_DIR / "certs"


def _local_names():
    """收集本机可用于证书 SAN 的名字：主机名、回环、探测到的内网 IP。"""
    names = {"localhost", "127.0.0.1", "::1", socket.gethostname()}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            names.add(info[4][0])
    except OSError:
        pass
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))  # UDP 不实际发包，只为取出口网卡地址
            names.add(sock.getsockname()[0])
        finally:
            sock.close()
    except OSError:
        pass
    return sorted(names)


def _write_key(path: Path, key) -> None:
    path.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _build_ca(days: int = 3650):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "WorkRecord Local CA"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "WorkRecord (self-signed)"),
    ])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False, content_commitment=False,
                key_encipherment=False, data_encipherment=False,
                key_agreement=False, key_cert_sign=True, crl_sign=True,
                encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    return key, cert


def _build_server(ca_key, ca_cert, days: int):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "workrecord.local")])
    names = _local_names()
    san = []
    for name in names:
        try:
            san.append(x509.IPAddress(ipaddress.ip_address(name)))
        except ValueError:
            san.append(x509.DNSName(name))
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    return key, cert, names


def main() -> int:
    ap = argparse.ArgumentParser(description="生成本地 CA 与 HTTPS 服务器证书")
    ap.add_argument("--days", type=int, default=730, help="服务器证书有效期（默认 730 天）")
    ap.add_argument("--force", action="store_true", help="强制重新签发服务器证书")
    args = ap.parse_args()

    CERT_DIR.mkdir(parents=True, exist_ok=True)
    ca_key_p, ca_crt_p = CERT_DIR / "ca.key.pem", CERT_DIR / "ca.crt.pem"
    srv_key_p, srv_crt_p = CERT_DIR / "server.key.pem", CERT_DIR / "server.crt.pem"

    if ca_key_p.is_file() and ca_crt_p.is_file():
        ca_key = serialization.load_pem_private_key(ca_key_p.read_bytes(), password=None)
        ca_cert = x509.load_pem_x509_certificate(ca_crt_p.read_bytes())
        print(f"复用已有本地 CA：{ca_crt_p}")
    else:
        ca_key, ca_cert = _build_ca()
        _write_key(ca_key_p, ca_key)
        ca_crt_p.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
        print(f"已生成本地 CA：{ca_crt_p}（私钥 {ca_key_p} 请离线保存，勿分发）")

    if srv_crt_p.is_file() and not args.force:
        print(f"服务器证书已存在：{srv_crt_p}（如需重新签发加 --force）")
        return 0

    key, cert, names = _build_server(ca_key, ca_cert, args.days)
    _write_key(srv_key_p, key)
    srv_crt_p.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    print(f"已签发服务器证书：{srv_crt_p}（{args.days} 天）")
    print(f"证书覆盖名称：{', '.join(names)}")
    print("\n下一步：")
    print("  1) 把本地 CA 装进受信任根（管理员 PowerShell）：")
    print(f"     certutil -addstore -f \"ROOT\" {ca_crt_p}")
    print("  2) 用 HTTPS 方式启动：scripts\\windows\\start_windows_https.bat（或 ./start_https.sh）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
