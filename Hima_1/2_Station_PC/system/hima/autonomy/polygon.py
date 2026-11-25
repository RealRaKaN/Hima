"""Plain polygon geometry in local metres (no shapely on the Jetson image; a ring is a list of (x, y)
points without the closing repeat)."""
from __future__ import annotations

import math
from typing import Sequence

Pt = tuple[float, float]


def signed_area(ring: Sequence[Pt]) -> float:
    """> 0 when the ring runs counter-clockwise."""
    s = 0.0
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return s / 2


def ccw(ring: Sequence[Pt]) -> list[Pt]:
    r = [(float(x), float(y)) for x, y in ring]
    if len(r) > 1 and r[0] == r[-1]:
        r.pop()
    return r if signed_area(r) > 0 else r[::-1]


def contains(ring: Sequence[Pt], x: float, y: float) -> bool:
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def seg_dist(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
    return math.hypot(px - ax - t * dx, py - ay - t * dy)


def boundary_dist(ring: Sequence[Pt], x: float, y: float) -> float:
    n = len(ring)
    return min(seg_dist(x, y, *ring[i], *ring[(i + 1) % n]) for i in range(n))


def perimeter(ring: Sequence[Pt]) -> float:
    n = len(ring)
    return sum(math.dist(ring[i], ring[(i + 1) % n]) for i in range(n))


def _dp(pts: Sequence[Pt], tol: float) -> list[Pt]:
    """Douglas-Peucker on an open chain (iterative; keeps both ends)."""
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        best, idx = -1.0, -1
        for i in range(a + 1, b):
            d = seg_dist(*pts[i], *pts[a], *pts[b])
            if d > best:
                best, idx = d, i
        if idx > 0 and best > tol:
            keep[idx] = True
            stack += [(a, idx), (idx, b)]
    return [p for p, k in zip(pts, keep) if k]


def simplify(ring: Sequence[Pt], tol: float) -> list[Pt]:
    """Douglas-Peucker on a closed ring: every dropped point stays within tol of the new edges."""
    r = list(ring)
    far = max(range(len(r)), key=lambda i: math.dist(r[0], r[i]))
    a = _dp(r[:far + 1], tol)
    b = _dp(r[far:] + [r[0]], tol)
    return ccw(a[:-1] + b[:-1])


def offset_inward(ring: Sequence[Pt], d: float, miter_limit: float = 4.0) -> list[Pt]:
    """Every vertex moved d metres inside (mitred corners) of a counter-clockwise ring.

    Not a full polygon offset: callers check the result with is_simple and inside_of.
    """
    n = len(ring)
    out = []
    for i in range(n):
        px, py = ring[i - 1]
        x, y = ring[i]
        nx_, ny_ = ring[(i + 1) % n]
        e1 = (x - px, y - py)
        e2 = (nx_ - x, ny_ - y)
        l1, l2 = math.hypot(*e1) or 1.0, math.hypot(*e2) or 1.0
        n1 = (-e1[1] / l1, e1[0] / l1)               # left normal = inside for a counter-clockwise ring
        n2 = (-e2[1] / l2, e2[0] / l2)
        bx, by = n1[0] + n2[0], n1[1] + n2[1]
        dot = n1[0] * n2[0] + n1[1] * n2[1]
        k = d / max(1e-9, (1 + dot))                 # miter: |offset| = d / cos(half angle)
        if math.hypot(bx, by) * k > miter_limit * d or math.hypot(bx, by) < 1e-9:
            bl = math.hypot(bx, by) or 1.0
            bx, by, k = bx / bl, by / bl, miter_limit * d
        out.append((x + bx * k, y + by * k))
    return out


def _orient(a: Pt, b: Pt, c: Pt) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def segments_cross(a: Pt, b: Pt, c: Pt, d: Pt) -> bool:
    """True when segment ab and segment cd touch or cross."""
    if (max(a[0], b[0]) < min(c[0], d[0]) or max(c[0], d[0]) < min(a[0], b[0]) or
            max(a[1], b[1]) < min(c[1], d[1]) or max(c[1], d[1]) < min(a[1], b[1])):
        return False
    o1, o2, o3, o4 = _orient(a, b, c), _orient(a, b, d), _orient(c, d, a), _orient(c, d, b)
    return (o1 * o2 <= 0) and (o3 * o4 <= 0)


def is_simple(ring: Sequence[Pt]) -> bool:
    """No two edges that are not neighbours touch."""
    n = len(ring)
    for i in range(n):
        a, b = ring[i], ring[(i + 1) % n]
        for j in range(i + 2, n):
            if i == 0 and j == n - 1:
                continue
            if segments_cross(a, b, ring[j], ring[(j + 1) % n]):
                return False
    return True


def inside_of(inner: Sequence[Pt], outer: Sequence[Pt]) -> bool:
    """Every vertex of inner is inside outer and no edges of the two rings touch."""
    if not all(contains(outer, x, y) for x, y in inner):
        return False
    n, m = len(inner), len(outer)
    obox = [(min(outer[j][0], outer[(j + 1) % m][0]), max(outer[j][0], outer[(j + 1) % m][0]),
             min(outer[j][1], outer[(j + 1) % m][1]), max(outer[j][1], outer[(j + 1) % m][1])) for j in range(m)]
    for i in range(n):
        a, b = inner[i], inner[(i + 1) % n]
        x0, x1, y0, y1 = min(a[0], b[0]), max(a[0], b[0]), min(a[1], b[1]), max(a[1], b[1])
        for j, (bx0, bx1, by0, by1) in enumerate(obox):
            if bx1 < x0 or bx0 > x1 or by1 < y0 or by0 > y1:
                continue
            if segments_cross(a, b, outer[j], outer[(j + 1) % m]):
                return False
    return True
