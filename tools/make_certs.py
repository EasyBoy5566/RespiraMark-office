"""
make_certs — 一鍵產生 TLS 憑證（自建 CA + 伺服器憑證）
========================================================
用法（在專案根目錄執行）：
    python tools/make_certs.py                          # 自動偵測本機區網 IP
    python tools/make_certs.py --host 192.168.0.126 --host 172.19.18.70
    python tools/make_certs.py --dir certs              # 自訂輸出目錄

產出（預設寫入專案根目錄 certs/，已 gitignore）：
    ca.pem / ca.key          自建 CA（發證機關）——Pi 與瀏覽器要信任的就是這張
    server.pem / server.key  伺服器憑證——config.json 的 tls_cert / tls_key 指向這兩個檔

信任模型（CA 釘選，見 PROTOCOL.md「傳輸安全」）：
    Pi 與瀏覽器信任 ca.pem，而不是單張伺服器憑證。伺服器搬家/換 IP 時
    重跑本工具（CA 已存在會自動沿用、只重簽伺服器憑證），Pi 端不用動。
    ⚠ ca.key 是發證私鑰，只留在伺服器上，絕不要複製到 Pi 或其他電腦。

相依：cryptography（僅本工具需要；伺服器運行本體不需要）
    python -m pip install cryptography
"""

import argparse
import ipaddress
import os
import socket
import sys
from datetime import datetime, timedelta, timezone

try:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
except ImportError:
    print("錯誤：未安裝 cryptography 套件（僅產憑證需要）。請先執行：")
    print("    python -m pip install cryptography")
    sys.exit(1)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CA_DAYS = 3650          # CA 有效 10 年
SERVER_DAYS = 3650      # 院內自建 CA 不受公開憑證 398 天限制，簽長一點減少維運負擔


def lan_ip() -> str:
    """找出本機對外的區網 IP（與 main.py 相同做法，不會真的發封包）"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def _write_key(path: str, key):
    with open(path, "wb") as f:
        f.write(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ))


def _write_cert(path: str, cert):
    with open(path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))


def load_or_create_ca(cert_dir: str):
    """CA 已存在 → 沿用（伺服器換 IP 重簽時 Pi 不用動）；否則新建"""
    ca_cert_path = os.path.join(cert_dir, "ca.pem")
    ca_key_path = os.path.join(cert_dir, "ca.key")
    if os.path.exists(ca_cert_path) and os.path.exists(ca_key_path):
        with open(ca_key_path, "rb") as f:
            key = serialization.load_pem_private_key(f.read(), password=None)
        with open(ca_cert_path, "rb") as f:
            cert = x509.load_pem_x509_certificate(f.read())
        print(f"沿用既有 CA: {ca_cert_path}")
        return key, cert, False

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "RespiraMark Local CA")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=CA_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=False, content_commitment=False,
            key_encipherment=False, data_encipherment=False, key_agreement=False,
            key_cert_sign=True, crl_sign=True,
            encipher_only=False, decipher_only=False,
        ), critical=True)
        .sign(key, hashes.SHA256())
    )
    _write_key(ca_key_path, key)
    _write_cert(ca_cert_path, cert)
    print(f"已建立新 CA: {ca_cert_path}")
    return key, cert, True


def make_server_cert(cert_dir: str, ca_key, ca_cert, hosts):
    """簽發伺服器憑證，SAN 含所有指定的 IP/主機名稱（重跑會覆蓋舊憑證）"""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    san = []
    for h in hosts:
        try:
            san.append(x509.IPAddress(ipaddress.ip_address(h)))
        except ValueError:
            san.append(x509.DNSName(h))
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hosts[0])]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=SERVER_DAYS))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
                       critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    _write_key(os.path.join(cert_dir, "server.key"), key)
    _write_cert(os.path.join(cert_dir, "server.pem"), cert)


def main():
    ap = argparse.ArgumentParser(description="產生自建 CA 與伺服器 TLS 憑證")
    ap.add_argument("--host", action="append", default=[],
                    help="伺服器的 IP 或主機名稱（可重複；預設自動偵測區網 IP）")
    ap.add_argument("--dir", default=os.path.join(PROJECT_ROOT, "certs"),
                    help="輸出目錄（預設: 專案根目錄 certs/）")
    args = ap.parse_args()

    # 預設 SAN：使用者指定的 host + 自動偵測區網 IP + 本機（開發/測試用）
    hosts = list(dict.fromkeys(
        args.host + ([] if args.host else [lan_ip()]) + ["localhost", "127.0.0.1"]
    ))
    os.makedirs(args.dir, exist_ok=True)

    ca_key, ca_cert, ca_is_new = load_or_create_ca(args.dir)
    make_server_cert(args.dir, ca_key, ca_cert, hosts)
    print(f"已簽發伺服器憑證: {os.path.join(args.dir, 'server.pem')}")
    print(f"  適用位址（SAN）: {', '.join(hosts)}")
    print()
    print("後續步驟：")
    print("  1. 伺服器 config.json 設定：")
    print('       "tls_cert": "certs/server.pem", "tls_key": "certs/server.key"')
    print("  2. 把 ca.pem（只有這個檔！）複製到每台 Pi，telemetry.json 設定：")
    print('       "tls": true, "tls_ca": "ca.pem 的路徑"')
    if ca_is_new:
        print("  3. 看儀表板的電腦把 ca.pem 匯入「受信任的根憑證授權單位」，")
        print("     瀏覽器就不會跳安全警告（不匯入也能用，點進階、繼續前往即可）")
    else:
        print("  3. CA 沿用既有的，Pi 端與瀏覽器端都不需要重新設定")
    print()
    print("注意：ca.key（發證私鑰）只留在伺服器，絕不要複製給任何其他機器")


if __name__ == "__main__":
    main()
