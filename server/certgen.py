"""Create the server's self-signed TLS certificate (pure Python, no extra libraries).

Python's ``ssl`` module can *use* certificates but cannot *create* them, so this
module builds one by hand the first time the server starts:

1. generate an RSA-2048 key pair (two random 1024-bit primes, Miller-Rabin test);
2. write the X.509 certificate structure in DER (ASN.1) encoding;
3. sign it with the private key (RSA PKCS#1 v1.5 + SHA-256);
4. save ``server.crt`` and ``server.key`` as PEM files in the server's data folder.

The private key never leaves the server machine (the data folder is not committed
to git).  Clients pin the certificate's SHA-256 fingerprint on first connect.
"""
from __future__ import annotations

import base64
import datetime
import hashlib
import os
import secrets
import socket

KEY_BITS = 2048
PUBLIC_EXPONENT = 65537

_SMALL_PRIMES = [p for p in range(3, 2000, 2) if all(p % d for d in range(3, int(p ** 0.5) + 1, 2))]


# ------------------------------------------------------------------ RSA keys
def _is_probable_prime(n: int, rounds: int = 24) -> bool:
    if n < 2:
        return False
    for p in _SMALL_PRIMES:
        if n % p == 0:
            return n == p
    d, r = n - 1, 0
    while d % 2 == 0:
        d //= 2
        r += 1
    for _ in range(rounds):
        a = secrets.randbelow(n - 3) + 2
        x = pow(a, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _random_prime(bits: int) -> int:
    while True:
        candidate = secrets.randbits(bits) | (0b11 << (bits - 2)) | 1   # top two bits set, odd
        if (candidate - 1) % PUBLIC_EXPONENT and _is_probable_prime(candidate):
            return candidate


def generate_rsa_key(bits: int = KEY_BITS) -> dict:
    while True:
        p, q = _random_prime(bits // 2), _random_prime(bits // 2)
        if p == q:
            continue
        n = p * q
        if n.bit_length() != bits:
            continue
        phi = (p - 1) * (q - 1)
        d = pow(PUBLIC_EXPONENT, -1, phi)
        return {"n": n, "e": PUBLIC_EXPONENT, "d": d, "p": p, "q": q}


# ------------------------------------------------------------- DER encoding
def _length(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(raw)]) + raw


def _tlv(tag: int, content: bytes) -> bytes:
    return bytes([tag]) + _length(len(content)) + content


def _int(value: int) -> bytes:
    return _tlv(0x02, value.to_bytes(value.bit_length() // 8 + 1, "big"))


def _seq(*items: bytes) -> bytes:
    return _tlv(0x30, b"".join(items))


def _oid(dotted: str) -> bytes:
    parts = [int(x) for x in dotted.split(".")]
    body = bytes([40 * parts[0] + parts[1]])
    for part in parts[2:]:
        chunk = [part & 0x7F]
        part >>= 7
        while part:
            chunk.append(0x80 | (part & 0x7F))
            part >>= 7
        body += bytes(reversed(chunk))
    return _tlv(0x06, body)


_NULL = b"\x05\x00"
OID_SHA256_RSA = "1.2.840.113549.1.1.11"
OID_RSA = "1.2.840.113549.1.1.1"
OID_SHA256 = "2.16.840.1.101.3.4.2.1"
OID_COMMON_NAME = "2.5.4.3"
OID_ORGANIZATION = "2.5.4.10"


def _name(common_name: str) -> bytes:
    def rdn(oid, value):
        return _tlv(0x31, _seq(_oid(oid), _tlv(0x0C, value.encode("utf-8"))))
    return _seq(rdn(OID_ORGANIZATION, "Co-op Block Adventure"), rdn(OID_COMMON_NAME, common_name))


def _utc_time(when: datetime.datetime) -> bytes:
    return _tlv(0x17, when.strftime("%y%m%d%H%M%SZ").encode("ascii"))


def _sign(message: bytes, key: dict) -> bytes:
    """RSASSA-PKCS1-v1_5 signature with SHA-256."""
    k = (key["n"].bit_length() + 7) // 8
    digest_info = _seq(_seq(_oid(OID_SHA256), _NULL), _tlv(0x04, hashlib.sha256(message).digest()))
    padded = b"\x00\x01" + b"\xff" * (k - len(digest_info) - 3) + b"\x00" + digest_info
    return pow(int.from_bytes(padded, "big"), key["d"], key["n"]).to_bytes(k, "big")


def build_certificate(key: dict, common_name: str, days: int = 3650) -> bytes:
    now = datetime.datetime.now(datetime.timezone.utc)
    public_key = _seq(_int(key["n"]), _int(key["e"]))
    spki = _seq(_seq(_oid(OID_RSA), _NULL), _tlv(0x03, b"\x00" + public_key))
    algorithm = _seq(_oid(OID_SHA256_RSA), _NULL)
    tbs = _seq(
        _tlv(0xA0, _int(2)),                                   # version 3
        _int(secrets.randbits(63) + 1),                        # serial number
        algorithm,
        _name(common_name),                                    # issuer (self-signed)
        _seq(_utc_time(now - datetime.timedelta(days=1)), _utc_time(now + datetime.timedelta(days=days))),
        _name(common_name),                                    # subject
        spki,
    )
    return _seq(tbs, algorithm, _tlv(0x03, b"\x00" + _sign(tbs, key)))


def _private_key_der(key: dict) -> bytes:
    p, q, d = key["p"], key["q"], key["d"]
    return _seq(_int(0), _int(key["n"]), _int(key["e"]), _int(d), _int(p), _int(q),
                _int(d % (p - 1)), _int(d % (q - 1)), _int(pow(q, -1, p)))


def _pem(label: str, der: bytes) -> str:
    b64 = base64.b64encode(der).decode("ascii")
    lines = [b64[i:i + 64] for i in range(0, len(b64), 64)]
    return f"-----BEGIN {label}-----\n" + "\n".join(lines) + f"\n-----END {label}-----\n"


def ensure_certificate(folder: str) -> tuple[str, str, bool]:
    """Return (cert_path, key_path, created).  Creates them if they do not exist."""
    cert_path = os.path.join(folder, "server.crt")
    key_path = os.path.join(folder, "server.key")
    if os.path.exists(cert_path) and os.path.exists(key_path):
        return cert_path, key_path, False
    os.makedirs(folder, exist_ok=True)
    key = generate_rsa_key()
    cert = build_certificate(key, f"Co-op Block server on {socket.gethostname()}")
    with open(key_path, "w", encoding="ascii") as f:
        f.write(_pem("RSA PRIVATE KEY", _private_key_der(key)))
    with open(cert_path, "w", encoding="ascii") as f:
        f.write(_pem("CERTIFICATE", cert))
    return cert_path, key_path, True
