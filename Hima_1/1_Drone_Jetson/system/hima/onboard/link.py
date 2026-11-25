"""Simulated text-only link drone -> ground (weak link, text only in flight).

One message at a time, no pre-emption: when the channel becomes free it sends the most
urgent waiting message (lowest priority number, then oldest). Each alert travels as one
MAVLink 2 STATUSTEXT packet. Size on the wire (https://mavlink.io/en/guide/serialization.html):
  12 bytes header + checksum (+13 if signed) + payload,
  payload = severity (1) + text; the text padding and the zero id / chunk_seq extension
  fields are trailing zeros, which MAVLink 2 truncates.
Times are mission seconds; nothing here sleeps.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from typing import Any

from hima.onboard.alerts import Alert

HEADER_CRC_BYTES = 12
SIGNATURE_BYTES = 13


CHUNK_PAYLOAD_BYTES = 54          # severity 1 + text 50 + id 2 + chunk_seq 1 (common.xml STATUSTEXT)
TEXT_BYTES = 50


def frame_bytes(text: str, signed: bool) -> int:
    return HEADER_CRC_BYTES + (SIGNATURE_BYTES if signed else 0) + 1 + len(text.encode("ascii"))


def chunk_bytes(signed: bool) -> int:
    """A chunk of a long text: id and chunk_seq come after the text, so nothing is truncated: 66 bytes
    unsigned (measured with pymavlink)."""
    return HEADER_CRC_BYTES + (SIGNATURE_BYTES if signed else 0) + CHUNK_PAYLOAD_BYTES


@dataclass(frozen=True)
class TextChunk:
    """One STATUSTEXT of a long message (periodic analysis, camera-window result)."""
    seq: int                      # unique, from CHUNK_SEQ_BASE up (never equal to an alert number)
    priority: int
    severity: int
    text: str
    msg_id: int                   # 0 = a single-packet message
    chunk_seq: int
    source: str                   # "periodic" or "window"


CHUNK_SEQ_BASE = 1_000_000


def split_text(text: str, limit: int = TEXT_BYTES) -> list[str]:
    """Split ASCII text into pieces of at most limit bytes, at spaces where possible."""
    text.encode("ascii")
    words, out, cur = text.split(), [], ""
    for w in words:
        while len(w) > limit:                       # a word longer than a packet is cut
            if cur:
                out.append(cur)
                cur = ""
            out.append(w[:limit])
            w = w[limit:]
        if not cur:
            cur = w
        elif len(cur) + 1 + len(w) <= limit:
            cur += " " + w
        else:
            out.append(cur)
            cur = w
    if cur:
        out.append(cur)
    return out


def make_chunks(text: str, *, msg_id: int, priority: int, severity: int, source: str, first_seq: int) -> list[TextChunk]:
    parts = split_text(text)
    if len(parts) == 1:
        return [TextChunk(first_seq, priority, severity, parts[0], 0, 0, source)]
    if not 1 <= msg_id <= 65535:
        raise ValueError("msg_id of a chunked message must be in 1..65535")
    return [TextChunk(first_seq + i, priority, severity, p, msg_id, i, source) for i, p in enumerate(parts)]


@dataclass(frozen=True)
class Delivery:
    alert: Alert | Any            # Alert, ObjectAlert or TextChunk (anything with seq, priority, text)
    queued_t: float
    start_t: float
    delivered_t: float
    bytes: int


@dataclass
class LinkSim:
    bytes_per_s: float
    signed: bool = False
    delivered: list[Delivery] = field(default_factory=list)
    _queue: list[tuple[float, Any]] = field(default_factory=list)
    _free_at: float = -math.inf
    _last_t: float = -math.inf

    def send(self, alert: Any, t: float) -> None:
        if t < self._last_t:
            raise ValueError(f"send at t = {t} is before the previous call t = {self._last_t}")
        self._last_t = t
        self._queue.append((t, alert))

    def advance(self, t: float) -> list[Delivery]:
        """Start every transmission whose start time is <= t (it may finish after t)."""
        if t < self._last_t:
            raise ValueError(f"advance to t = {t} is before the previous call t = {self._last_t}")
        self._last_t = t
        out = []
        while self._queue:
            start = max(self._free_at, min(q for q, _ in self._queue))
            if start > t:
                break
            ready = [(a.priority, q, a.seq, q, a) for q, a in self._queue if q <= start]
            _, _, _, q, a = min(ready, key=lambda r: r[:3])
            self._queue.remove((q, a))
            n = chunk_bytes(self.signed) if getattr(a, "msg_id", 0) else frame_bytes(a.text, self.signed)
            end = start + n / self.bytes_per_s
            self._free_at = end
            d = Delivery(a, q, start, end, n)
            self.delivered.append(d)
            out.append(d)
        return out

    def flush(self) -> list[Delivery]:
        """Send everything still waiting (e.g. after the last frame)."""
        return self.advance(math.inf) if self._queue else []

    @property
    def waiting(self) -> int:
        return len(self._queue)
