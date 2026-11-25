"""Wi-Fi delivery of mission packages, drone -> ground station: the parts both sides share.

After landing, the drone joins the base Wi-Fi and sends its package to the station PC (hima.onboard.deliver);
the station checks it and adds it to the ground archive the dashboard reads (hima.ground.receiver).

Protocol hima-delivery/1: HTTP/1.1 on the base network, JSON bodies except the file pieces.
  GET  /v1/ping                                     -> {"station", "protocol"}
  POST /v1/offer  {"mission_id", "files": [{"path", "bytes", "sha256"}]}
                                                    -> {"state": "receiving", "have": {path: bytes held}}
                                                       | {"state": "already_ingested"}       (nothing to send)
                                                       | 409 {"state": "conflict", "error"}  (never overwritten)
  PUT  /v1/file?mission=M&path=P&offset=N  <bytes>  -> {"have": bytes now held}
                                                       | 409 {"have": n}  (the station holds n bytes: go on from n)
                                                       | 422 {"error", "have": 0}  (the whole file does not match
                                                                                     its SHA-256: send it again)
  POST /v1/commit {"mission_id"}                    -> {"status": "ingested" | "already_ingested", "entry"}
                                                       | 422 {"status": "rejected", "errors"}

Every request and every answer is signed with HMAC-SHA256 and the key both sides hold (python -m hima keygen,
the same file copied to the drone and the station). A request's signature covers the method, the path with its
query and the SHA-256 of the body; an answer's signature covers the request's signature, the status and the
answer body, so an answer recorded earlier cannot pass as the answer to a new request. Every step is idempotent
and every file is checked by its SHA-256, so a replayed request can only repeat something already done: no clock
is needed (the drone may not know the time in the field). The key itself never travels. The bodies are not
encrypted by this layer: the base Wi-Fi (WPA2/WPA3) encrypts the air.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
from pathlib import Path

PROTOCOL = "hima-delivery/1"
KEY_BYTES = 32
MAX_JSON_BYTES = 1 << 20                 # an offer of a few thousand files is far below this
MAX_PIECE_BYTES = 64 << 20               # [delivery].chunk_mb is checked against this (config.py)
H_BODY = "X-Hima-Body-SHA256"
H_SIG = "X-Hima-Signature"

_MISSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_PART = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]{0,99}")
_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)), *(f"LPT{i}" for i in range(10))}


class DeliveryKeyError(ValueError):
    """The key file is missing or is not a key made by python -m hima keygen."""


def new_key_hex() -> str:
    return secrets.token_hex(KEY_BYTES)


def write_key(path: str | Path, *, force: bool = False) -> bytes:
    """Write a new random key (64 hex characters) readable by its owner only. Returns the key."""
    path = Path(path)
    if path.exists() and not force:
        raise FileExistsError(f"{path} already exists; a key is never replaced without --force "
                              "(the drone and the station must keep the same key)")
    path.parent.mkdir(parents=True, exist_ok=True)
    text = new_key_hex()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="ascii", newline="\n") as f:
        f.write(text + "\n")
    return bytes.fromhex(text)


def load_key(path: str | Path) -> bytes:
    path = Path(path)
    try:
        text = path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        raise DeliveryKeyError(f"no delivery key at {path}: make one with python -m hima keygen, then copy the same "
                               "file to the other machine (drone and station)") from None
    except (OSError, UnicodeDecodeError) as e:
        raise DeliveryKeyError(f"cannot read the delivery key {path}: {e}") from e
    if not re.fullmatch(r"[0-9a-fA-F]{64}", text):
        raise DeliveryKeyError(f"{path} is not a delivery key (64 hex characters, made by python -m hima keygen)")
    return bytes.fromhex(text)


def fingerprint(key: bytes) -> str:
    """Safe to show: compare it on the drone and the station to see that both hold the same key."""
    h = hashlib.sha256(b"hima-delivery key fingerprint\n" + key).hexdigest()[:16]
    return "-".join(h[i:i + 4] for i in range(0, 16, 4))


def body_sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def request_signature(key: bytes, method: str, target: str, body_sha: str) -> str:
    msg = f"{PROTOCOL} request\n{method.upper()}\n{target}\n{body_sha}".encode()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def response_signature(key: bytes, request_sig: str, status: int, body_sha: str) -> str:
    msg = f"{PROTOCOL} response\n{request_sig}\n{int(status)}\n{body_sha}".encode()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def valid_mission_id(s: object) -> bool:
    return isinstance(s, str) and _MISSION.fullmatch(s) is not None


def valid_file_path(p: object) -> bool:
    """A package file path the station may create: relative, '/'-separated, no '..', no hidden or reserved names."""
    if not isinstance(p, str) or not 0 < len(p) <= 240:
        return False
    parts = p.split("/")
    return all(_PART.fullmatch(x) is not None and x.split(".")[0].upper() not in _WINDOWS_RESERVED for x in parts)


def parse_listen(s: str) -> tuple[str, int]:
    """'0.0.0.0:8765' -> ('0.0.0.0', 8765)."""
    host, sep, port = s.rpartition(":")
    if not sep or not host or not port.isdigit() or not 0 < int(port) < 65536:
        raise ValueError(f"listen must be HOST:PORT (e.g. 0.0.0.0:8765), got {s!r}")
    return host.strip("[]"), int(port)
