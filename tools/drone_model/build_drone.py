"""Build public/DRONE.glb: a parametric model of the Hima VTOL drone.

Modelled by hand from four photos of the real airframe (top, front and two
three-quarter views): white fuselage with a nose tractor propeller, shoulder
wing, two green booms carrying four lift motors, conventional tail with a
green fin tip, four splayed landing legs, GPS mast, and the green/gold
triangle livery seen in the top view. Sizes are estimates scaled to a 1.8 m
wingspan. The propellers spin in a looping animation.

Usage:  python tools/drone_model/build_drone.py [out.glb]
Needs numpy and Pillow only.
"""
import io
import json
import math
import random
import struct
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[2] / "public" / "DRONE.glb"

# ---------------------------------------------------------------- colours
def hex_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))

def srgb_to_linear(c):
    c = c / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

def lin(h):
    return [srgb_to_linear(c) for c in hex_rgb(h)] + [1.0]

CREAM = "#EEEAE1"
GREEN = "#0D5C3A"
GREEN_DARK = "#0A4A2F"
GOLD = "#B8996A"
BEIGE = "#D9C7A3"
SEAM = "#CFC9BC"

# ---------------------------------------------------------------- geometry
def unit(v):
    n = np.linalg.norm(v, axis=-1, keepdims=True)
    n[n == 0] = 1
    return v / n

class Part:
    """A closed surface: vertices, uvs, triangles; normals computed at the end."""

    def __init__(self):
        self.P, self.UV, self.I, self.seams = [], [], [], []
        self.n = 0

    def _add(self, P, UV, I, seams=()):
        self.P.append(np.asarray(P, float))
        self.UV.append(np.asarray(UV, float))
        self.I.append(np.asarray(I, np.int64) + self.n)
        self.seams += [(a + self.n, b + self.n) for a, b in seams]
        self.n += len(P)
        return self

    def loft(self, rings, v=None, u=None, cap0=True, cap1=True, cap_uv=(0.5, 0.75)):
        rings = np.asarray(rings, float)
        M, N, _ = rings.shape
        v = np.linspace(0, 1, M) if v is None else np.asarray(v, float)
        u = np.linspace(0, 1, N + 1) if u is None else np.asarray(u, float)
        P = np.concatenate([rings, rings[:, :1]], axis=1).reshape(-1, 3)
        uu, vv = np.meshgrid(u, v)
        UV = np.stack([uu, vv], -1).reshape(-1, 2)
        W = N + 1
        I = []
        for i in range(M - 1):
            for j in range(N):
                a, b, c, d = i * W + j, (i + 1) * W + j, (i + 1) * W + j + 1, i * W + j + 1
                I += [(a, b, c), (a, c, d)]
        seams = [(i * W, i * W + N) for i in range(M)]
        I = np.array(I)
        caps = []
        for k, nb, on in ((0, 1, cap0), (M - 1, M - 2, cap1)):
            ring = rings[k]
            if not on or np.ptp(ring, axis=0).max() < 1e-6:
                continue
            c = ring.mean(0)
            Pc = np.concatenate([ring, c[None]])
            Ic = np.array([(j, (j + 1) % N, N) for j in range(N)])
            n = np.cross(Pc[Ic[:, 1]] - Pc[Ic[:, 0]], Pc[Ic[:, 2]] - Pc[Ic[:, 0]]).sum(0)
            if np.dot(n, c - rings[nb].mean(0)) < 0:  # caps face away from the body
                Ic = Ic[:, ::-1]
            caps.append((Pc, Ic))
        # orient the side wall so the closed loft has positive volume
        allP = [P] + [c[0] for c in caps]
        allI, off = [I], len(P)
        for Pc, Ic in caps:
            allI.append(Ic + off)
            off += len(Pc)
        AP, AI = np.concatenate(allP), np.concatenate(allI)
        vol = np.einsum("ij,ij->i", AP[AI[:, 0]], np.cross(AP[AI[:, 1]], AP[AI[:, 2]])).sum()
        if vol < 0:
            I = I[:, ::-1]
        self._add(P, UV, I, seams)
        for Pc, Ic in caps:
            self._add(Pc, np.tile(cap_uv, (N + 1, 1)), Ic)
        return self

    def box(self, center, size, uv=(0.5, 0.75)):
        c, s = np.asarray(center, float), np.asarray(size, float) / 2
        for ax in range(3):
            for sign in (-1, 1):
                o = [a for a in range(3) if a != ax]
                quad = []
                for du, dv in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
                    p = c.copy()
                    p[ax] += sign * s[ax]
                    p[o[0]] += du * s[o[0]]
                    p[o[1]] += dv * s[o[1]]
                    quad.append(p)
                q = np.array(quad)
                tris = [(0, 1, 2), (0, 2, 3)]
                if np.cross(q[1] - q[0], q[2] - q[0])[ax] * sign < 0:
                    tris = [(0, 2, 1), (0, 3, 2)]
                self._add(quad, np.tile(uv, (4, 1)), tris)
        return self

    def build(self):
        P = np.concatenate(self.P)
        UV = np.concatenate(self.UV)
        I = np.concatenate(self.I)
        a, b, c = P[I[:, 0]], P[I[:, 1]], P[I[:, 2]]
        vol = np.einsum("ij,ij->i", a, np.cross(b, c)).sum() / 6
        if vol < 0:
            I = I[:, ::-1].copy()
            a, b, c = P[I[:, 0]], P[I[:, 1]], P[I[:, 2]]
        fn = np.cross(b - a, c - a)
        N = np.zeros_like(P)
        for k in range(3):
            np.add.at(N, I[:, k], fn)
        for i, j in self.seams:
            s = N[i] + N[j]
            N[i] = N[j] = s
        return P, unit(N), UV, I

def ring_basis(axis):
    axis = unit(np.asarray(axis, float)[None])[0]
    ref = np.array([0, 1, 0]) if abs(axis[1]) < 0.9 else np.array([1, 0, 0])
    e1 = unit(np.cross(axis, ref)[None])[0]
    e2 = np.cross(axis, e1)
    return e1, e2

def cylinder(part, p0, p1, r0, r1=None, seg=28):
    r1 = r0 if r1 is None else r1
    p0, p1 = np.asarray(p0, float), np.asarray(p1, float)
    e1, e2 = ring_basis(p1 - p0)
    t = np.linspace(0, 2 * np.pi, seg, endpoint=False)
    circ = np.cos(t)[:, None] * e1 + np.sin(t)[:, None] * e2
    return part.loft([p0 + r0 * circ, p1 + r1 * circ])

def rot_x(a):
    c, s = math.cos(a), math.sin(a)
    return np.array([[1, 0, 0], [0, c, -s], [0, s, c]])

def rot_y(a):
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])

# ---------------------------------------------------------------- layout
# Axes (glTF): x right wing, y up, z forward. s = distance back from the nose.
Z0 = 0.75
def zs(s):
    return Z0 - s

FUSE = [  # s, half-width, half-height, centre y
    (0.000, 0.020, 0.020, 0.000),
    (0.020, 0.040, 0.042, 0.000),
    (0.060, 0.062, 0.066, 0.002),
    (0.130, 0.080, 0.084, 0.006),
    (0.230, 0.090, 0.096, 0.012),
    (0.350, 0.094, 0.100, 0.015),
    (0.500, 0.092, 0.098, 0.016),
    (0.650, 0.079, 0.086, 0.020),
    (0.800, 0.060, 0.066, 0.030),
    (0.950, 0.045, 0.049, 0.039),
    (1.100, 0.035, 0.038, 0.045),
    (1.300, 0.028, 0.030, 0.049),
    (1.450, 0.022, 0.024, 0.050),
    (1.520, 0.010, 0.012, 0.050),
]
FUSE_L = FUSE[-1][0]
FUSE_SEG = 48

def fuse_at(s):
    tab = np.array(FUSE)
    return [float(np.interp(s, tab[:, 0], tab[:, k])) for k in (1, 2, 3)]

def spow(x, p):
    return np.sign(x) * np.abs(x) ** p

def fuse_ring(s):
    w, h, cy = fuse_at(s)
    phi = np.linspace(0, 2 * np.pi, FUSE_SEG, endpoint=False)  # 0 = bottom, pi = top
    x = w * spow(np.sin(phi), 0.8)
    y = cy - h * spow(np.cos(phi), 0.8)
    return np.stack([x, y, np.full_like(x, zs(s))], -1)

def fuse_perimeter(s):
    r = fuse_ring(s)
    return float(np.linalg.norm(np.roll(r, -1, 0) - r, axis=1).sum())

# Wing
SPAN = 1.80
HALF = SPAN / 2
W_LE_S = 0.33
W_ROOT_C, W_TIP_C = 0.30, 0.27
W_Y = 0.097
W_TIP_ROUND = 0.07
# Booms and lift rotors
BOOM_X = 0.40
BOOM_Y = 0.050
BOOM_R = 0.019
ROTOR_S = (0.12, 0.91)
POD_R, POD_H = 0.028, 0.062
LIFT_PROP_R = 0.19
NOSE_PROP_R = 0.15
# Tail
STAB_HALF, STAB_C, STAB_LE_S, STAB_Y = 0.36, 0.16, 1.34, 0.050
GROUND_Y = -0.20

# ---------------------------------------------------------------- airfoils
def naca(m, p, t, n=40, closed_te=True):
    beta = np.linspace(0, np.pi, n)
    x = (1 - np.cos(beta)) / 2
    a4 = -0.1036 if closed_te else -0.1015
    yt = 5 * t * (0.2969 * np.sqrt(x) - 0.1260 * x - 0.3516 * x**2 + 0.2843 * x**3 + a4 * x**4)
    if m == 0:
        yc = np.zeros_like(x)
        dyc = np.zeros_like(x)
    else:
        yc = np.where(x < p, m / p**2 * (2 * p * x - x**2), m / (1 - p) ** 2 * ((1 - 2 * p) + 2 * p * x - x**2))
        dyc = np.where(x < p, 2 * m / p**2 * (p - x), 2 * m / (1 - p) ** 2 * (p - x))
    th = np.arctan(dyc)
    xu, yu = x - yt * np.sin(th), yc + yt * np.cos(th)
    xl, yl = x + yt * np.sin(th), yc - yt * np.cos(th)
    # loop: TE (top) -> LE -> TE (bottom); v runs 0 -> 0.5 -> 1
    X = np.concatenate([xu[::-1], xl[1:-1]])
    Y = np.concatenate([yu[::-1], yl[1:-1]])
    V = np.concatenate([0.5 * (1 - xu[::-1]), 0.5 + 0.5 * xl[1:-1]])
    return X, Y, V

def chord_and_le(x, half, root_c, tip_c, le_z, round_len, le_curve=True):
    """Return chord and leading-edge z at span position x (rounded tip on the LE)."""
    ax = abs(x)
    x0 = half - round_len
    c_lin = root_c + (tip_c - root_c) * min(ax, x0) / x0
    te = le_z - (root_c + (tip_c - root_c) * min(ax, x0) / x0)
    if ax > x0:
        t = min((ax - x0) / round_len, 1.0)
        c = c_lin * (0.45 + 0.55 * math.sqrt(max(1 - t * t, 0)))
    else:
        c = c_lin
    return c, te + c

def span_stations(half, round_len, n_mid=24, n_tip=10):
    x0 = half - round_len
    mid = np.linspace(-x0, x0, n_mid)
    tip = x0 + round_len * np.sin(np.linspace(0, np.pi / 2, n_tip + 1))[1:]
    return np.concatenate([-tip[::-1], mid, tip])

def lifting_surface(part, half, root_c, tip_c, le_z, y, round_len, foil, incidence=0.0):
    X, Y, V = foil
    xs = span_stations(half, round_len)
    rings = []
    for x in xs:
        c, lez = chord_and_le(x, half, root_c, tip_c, le_z, round_len)
        xc = X - 0.25
        yy = Y * math.cos(incidence) - xc * math.sin(incidence)
        zz = lez - (0.25 + xc * math.cos(incidence) + Y * math.sin(incidence)) * c
        rings.append(np.stack([np.full_like(X, x), y + yy * c, zz], -1))
    rings = np.array(rings).transpose(0, 1, 2)
    v = (xs + half) / (2 * half)  # loft "v" (along stations) = span -> becomes texture u
    part.loft(rings, v=v, u=np.concatenate([V, [1.0]]), cap_uv=(0.5, 0.9))
    # loft gives (u = around profile, v = span); swap so u = span, v = chord
    part.UV[-3] = part.UV[-3][:, ::-1].copy()
    return part

# ---------------------------------------------------------------- textures
def tri_pattern(img, ppm, x_min, rows_m, coverage, seed, solid_green_from=None, gap=0.93):
    """Triangle lattice livery. coverage(x) -> (probability, shrink)."""
    rnd = random.Random(seed)
    d = ImageDraw.Draw(img)
    a = 0.085
    h = a * math.sqrt(3) / 2
    W = img.width / ppm
    palette = [GREEN, GREEN, GREEN_DARK, GOLD, GOLD, BEIGE, CREAM]
    k = 0
    y = -h
    while y < rows_m + h:
        off = (k % 2) * a / 2
        x = x_min - a + off
        while x < x_min + W + a:
            for up in (True, False):
                if up:
                    tri = [(x, y + h), (x + a, y + h), (x + a / 2, y)]
                else:
                    tri = [(x + a / 2, y), (x + 3 * a / 2, y), (x + a, y + h)]
                cx = sum(p[0] for p in tri) / 3
                cy = sum(p[1] for p in tri) / 3
                prob, shrink = coverage(cx)
                if rnd.random() < prob:
                    col = rnd.choice(palette)
                    f = gap * shrink
                    if solid_green_from is not None and abs(cx) > solid_green_from:
                        col, f = GREEN, 1.02
                    pts = [((cx + (px - cx) * f - x_min) * ppm, (cy + (py - cy) * f) * ppm) for px, py in tri]
                    d.polygon(pts, fill=hex_rgb(col))
            x += a
        y += h
        k += 1

def surface_texture(half, chord, coverage, seed, ppm=1100, solid_green_from=None):
    """Top half (v 0..0.5) = livery across span x chord; bottom half plain."""
    W = int(2 * half * ppm)
    Hc = int(chord * ppm)
    img = Image.new("RGB", (W, 2 * Hc), hex_rgb(CREAM))
    top = Image.new("RGB", (W, Hc), hex_rgb(CREAM))
    tri_pattern(top, ppm, -half, chord, coverage, seed, solid_green_from)
    img.paste(top, (0, 0))
    return img

def wing_cov(x):
    t = (abs(x) - 0.20) / (0.52 - 0.20)
    if t <= 0:
        return 0.0, 1.0
    t = min(t, 1.0)
    return t**1.4, 0.45 + 0.55 * t

def stab_cov(x):
    t = (abs(x) - 0.05) / (0.17 - 0.05)
    if t <= 0:
        return 0.0, 1.0
    t = min(t, 1.0)
    return t, 0.55 + 0.45 * t

def fuselage_texture():
    W, H = 768, 3072
    img = Image.new("RGB", (W, H), hex_rgb(CREAM))
    d = ImageDraw.Draw(img)

    def uv_px(s, arc):
        return ((0.5 + arc / fuse_perimeter(s)) * W, s / FUSE_L * H)

    # panel seams
    for s in (0.135, 0.70, 0.96):
        y = int(s / FUSE_L * H)
        d.rectangle([0, y - 1, W, y + 1], fill=hex_rgb(SEAM))
    # diamond chain along the spine
    s = 0.10
    k = 0
    while s < 1.38:
        L = 0.13
        hw = min(0.042, 0.17 * fuse_perimeter(s + L / 2))
        front, mid, back = s, s + L / 2, s + L
        tris = [
            [(front, 0), (mid, -hw), (mid, 0)],
            [(front, 0), (mid, 0), (mid, hw)],
            [(mid, -hw), (back, 0), (mid, 0)],
            [(mid, 0), (back, 0), (mid, hw)],
        ]
        cols = [GREEN, GOLD, GOLD, GREEN] if k % 2 == 0 else [GOLD, GREEN, GREEN, BEIGE]
        for tri, col in zip(tris, cols):
            cs = sum(p[0] for p in tri) / 3
            ca = sum(p[1] for p in tri) / 3
            f = 0.9
            d.polygon([uv_px(cs + (ps - cs) * f, ca + (pa - ca) * f) for ps, pa in tri], fill=hex_rgb(col))
        s += L + 0.012
        k += 1
    return img

def jpeg_bytes(img, q=88):
    b = io.BytesIO()
    img.save(b, "JPEG", quality=q, optimize=True, progressive=False)
    return b.getvalue()

# ---------------------------------------------------------------- parts
def build_fuselage():
    p = Part()
    ss = np.concatenate([np.linspace(0, 0.13, 8), np.linspace(0.15, 1.45, 40), [1.49, 1.52]])
    rings = [fuse_ring(s) for s in ss]
    p.loft(rings, v=ss / FUSE_L, cap_uv=(0.5, 0.02))
    return p

def build_wing():
    p = Part()
    lifting_surface(p, HALF, W_ROOT_C, W_TIP_C, zs(W_LE_S), W_Y, W_TIP_ROUND, naca(0.02, 0.4, 0.12), incidence=math.radians(2))
    return p

def build_stab():
    p = Part()
    lifting_surface(p, STAB_HALF, STAB_C, 0.13, zs(STAB_LE_S), STAB_Y, 0.10, naca(0, 0, 0.08))
    return p

def build_fin():
    """Two parts: white root section and green upper section."""
    X, Y, _ = naca(0, 0, 0.09, n=30)
    root_y = 0.066
    height = 0.185
    split = 0.45

    def section(f):
        c = 0.16 + (0.085 - 0.16) * f
        te = zs(1.505) - 0.0 * f
        le = te + c
        z = le - X * c
        x = Y * c
        return np.stack([x, np.full_like(x, root_y + f * height), z], -1)

    lo, hi = Part(), Part()
    lo.loft([section(f) for f in np.linspace(0, split, 4)], cap_uv=(0.5, 0.75))
    tip_f = np.linspace(split, 1.0, 8)
    rings = [section(f) for f in tip_f]
    # round the top: last ring shrinks
    rings.append(section(1.0) * [0.4, 1, 1] + [0, 0.008, -0.01 * 0])
    hi.loft(rings, cap_uv=(0.5, 0.75))
    return lo, hi

def build_booms():
    green, white = Part(), Part()
    for sx in (-1, 1):
        x = sx * BOOM_X
        z0, z1 = zs(ROTOR_S[0]), zs(ROTOR_S[1])
        cylinder(green, (x, BOOM_Y, z0), (x, BOOM_Y, z1), BOOM_R)
        for s in ROTOR_S:
            z = zs(s)
            cylinder(green, (x, BOOM_Y - POD_H / 2, z), (x, BOOM_Y + POD_H / 2, z), POD_R, seg=32)
        # white sleeve and pylon where the boom meets the wing
        cylinder(white, (x, BOOM_Y, zs(0.36)), (x, BOOM_Y, zs(0.66)), BOOM_R + 0.006)
        white.box((x, (BOOM_Y + W_Y) / 2 + 0.005, zs(0.51)), (0.022, W_Y - BOOM_Y, 0.24))
    return green, white

def motor_stack(orange, black, base, axis):
    """Brushless outrunner on top of a pod: orange can, black bell, adapter."""
    base, axis = np.asarray(base, float), np.asarray(axis, float)
    cylinder(black, base, base + axis * 0.004, 0.017)
    cylinder(orange, base + axis * 0.004, base + axis * 0.026, 0.022, seg=32)
    cylinder(black, base + axis * 0.012, base + axis * 0.016, 0.0225, seg=32)
    cylinder(black, base + axis * 0.026, base + axis * 0.031, 0.020, 0.016)
    cylinder(black, base + axis * 0.031, base + axis * 0.040, 0.006, 0.004)
    return base + axis * 0.040

def propeller(radius, pitch, hand=1):
    """Two-blade prop around +y, hub at origin."""
    p = Part()
    rs = np.linspace(0.012, radius, 16)
    phi = np.linspace(0, 2 * np.pi, 14, endpoint=False)
    for blade in (0, 1):
        rings = []
        for r in rs:
            f = r / radius
            c = radius * (0.11 + 0.10 * math.sin(math.pi * min(f / 0.7, 1) * 0.5) - 0.13 * max(f - 0.7, 0) / 0.3)
            if f > 0.97:
                c *= 0.6
            tk = 0.0045 * (1 - 0.6 * f) + 0.0012
            beta = math.atan2(pitch, 2 * math.pi * r) * hand
            cc = (c / 2) * np.cos(phi)
            tt = (tk / 2) * np.sin(phi) + 0.25 * tk * np.cos(phi) ** 2
            tang = cc * math.cos(beta) - tt * math.sin(beta)
            axial = cc * math.sin(beta) + tt * math.cos(beta)
            ring = np.stack([np.full_like(cc, r), axial, tang], -1)
            if blade:
                ring = ring @ rot_y(math.pi).T
            rings.append(ring)
        p.loft(rings)
    cylinder(p, (0, -0.006, 0), (0, 0.008, 0), 0.011, seg=24)
    cylinder(p, (0, 0.008, 0), (0, 0.016, 0), 0.008, 0.002, seg=24)
    return p

def build_legs():
    white = Part()
    for sx in (-1, 1):
        for root_s, foot_dz in ((0.36, 0.02), (0.62, -0.02)):
            w, h, cy = fuse_at(root_s)
            root = np.array([sx * 0.050, cy - h * 0.80, zs(root_s)])
            foot = np.array([sx * 0.085, GROUND_Y + 0.020, zs(root_s) + foot_dz])
            cylinder(white, root, foot, 0.021, 0.019, seg=24)
            white.box(foot + [0, -0.010, 0], (0.058, 0.020, 0.058))
    return white

def build_gps(black, grey):
    s = 0.76
    w, h, cy = fuse_at(s)
    top = cy + h
    cylinder(black, (0, top - 0.01, zs(s)), (0, top + 0.115, zs(s)), 0.0045, seg=12)
    cylinder(black, (0, top + 0.115, zs(s)), (0, top + 0.121, zs(s)), 0.022)
    cylinder(grey, (0, top + 0.121, zs(s)), (0, top + 0.136, zs(s)), 0.036, 0.031, seg=36)

# ---------------------------------------------------------------- glTF
class GLB:
    def __init__(self):
        self.bin = bytearray()
        self.j = {
            "asset": {"version": "2.0", "generator": "Hima drone generator (tools/drone_model/build_drone.py)"},
            "scene": 0, "scenes": [{"name": "Hima", "nodes": []}], "nodes": [], "meshes": [],
            "materials": [], "accessors": [], "bufferViews": [], "buffers": [],
            "images": [], "textures": [], "samplers": [], "animations": [],
        }

    def view(self, data, target=None):
        while len(self.bin) % 4:
            self.bin.append(0)
        bv = {"buffer": 0, "byteOffset": len(self.bin), "byteLength": len(data)}
        if target:
            bv["target"] = target
        self.bin += data
        self.j["bufferViews"].append(bv)
        return len(self.j["bufferViews"]) - 1

    def accessor(self, arr, typ, ctype=5126, target=34962, minmax=False):
        arr = np.ascontiguousarray(arr, np.uint32 if ctype == 5125 else np.float32)
        acc = {"bufferView": self.view(arr.tobytes(), target), "componentType": ctype,
               "count": int(arr.shape[0]), "type": typ}
        if minmax:
            flat = arr.reshape(arr.shape[0], -1)
            acc["min"] = flat.min(0).tolist()
            acc["max"] = flat.max(0).tolist()
        self.j["accessors"].append(acc)
        return len(self.j["accessors"]) - 1

    def texture(self, jpg):
        if not self.j["samplers"]:
            self.j["samplers"].append({"magFilter": 9729, "minFilter": 9987, "wrapS": 33071, "wrapT": 33071})
        self.j["images"].append({"bufferView": self.view(jpg), "mimeType": "image/jpeg"})
        self.j["textures"].append({"source": len(self.j["images"]) - 1, "sampler": 0})
        return len(self.j["textures"]) - 1

    def material(self, name, color, rough=0.5, metal=0.0, tex=None):
        pbr = {"baseColorFactor": lin(color) if tex is None else [1, 1, 1, 1],
               "metallicFactor": metal, "roughnessFactor": rough}
        if tex is not None:
            pbr["baseColorTexture"] = {"index": tex}
        self.j["materials"].append({"name": name, "pbrMetallicRoughness": pbr})
        return len(self.j["materials"]) - 1

    def mesh(self, name, prims):
        out = []
        for part, mat in prims:
            P, N, UV, I = part.build()
            out.append({
                "attributes": {
                    "POSITION": self.accessor(P, "VEC3", minmax=True),
                    "NORMAL": self.accessor(N, "VEC3"),
                    "TEXCOORD_0": self.accessor(UV, "VEC2"),
                },
                "indices": self.accessor(I.reshape(-1), "SCALAR", 5125, 34963),
                "material": mat,
            })
        self.j["meshes"].append({"name": name, "primitives": out})
        return len(self.j["meshes"]) - 1

    def node(self, name, mesh=None, t=None, r=None, parent=None):
        n = {"name": name}
        if mesh is not None:
            n["mesh"] = mesh
        if t is not None:
            n["translation"] = [float(v) for v in t]
        if r is not None:
            n["rotation"] = [float(v) for v in r]
        self.j["nodes"].append(n)
        idx = len(self.j["nodes"]) - 1
        if parent is None:
            self.j["scenes"][0]["nodes"].append(idx)
        else:
            self.j["nodes"][parent].setdefault("children", []).append(idx)
        return idx

    def spin(self, nodes_axes, period=0.5, keys=9):
        times = np.linspace(0, period, keys)
        samplers, channels = [], []
        t_acc = self.accessor(times[:, None], "SCALAR", target=None, minmax=True)
        for node, axis, direction, base_q in nodes_axes:
            qs = []
            for t in times:
                a = direction * 2 * math.pi * t / period
                q = axis_angle(axis, a)
                qs.append(quat_mul(base_q, q))
            samplers.append({"input": t_acc, "output": self.accessor(np.array(qs), "VEC4", target=None), "interpolation": "LINEAR"})
            channels.append({"sampler": len(samplers) - 1, "target": {"node": node, "path": "rotation"}})
        self.j["animations"].append({"name": "Rotors", "samplers": samplers, "channels": channels})

    def save(self, path):
        while len(self.bin) % 4:
            self.bin.append(0)
        self.j["buffers"] = [{"byteLength": len(self.bin)}]
        for k in [k for k, v in self.j.items() if v == []]:
            del self.j[k]
        js = json.dumps(self.j, separators=(",", ":")).encode()
        js += b" " * (-len(js) % 4)
        total = 12 + 8 + len(js) + 8 + len(self.bin)
        with open(path, "wb") as f:
            f.write(struct.pack("<III", 0x46546C67, 2, total))
            f.write(struct.pack("<II", len(js), 0x4E4F534A) + js)
            f.write(struct.pack("<II", len(self.bin), 0x004E4942) + bytes(self.bin))
        return total

def axis_angle(axis, a):
    axis = np.asarray(axis, float) / np.linalg.norm(axis)
    s = math.sin(a / 2)
    return [axis[0] * s, axis[1] * s, axis[2] * s, math.cos(a / 2)]

def quat_mul(q1, q2):
    x1, y1, z1, w1 = q1
    x2, y2, z2, w2 = q2
    return [w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
            w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
            w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2,
            w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2]

# ---------------------------------------------------------------- main
def main():
    g = GLB()
    t_wing = g.texture(jpeg_bytes(surface_texture(HALF, W_ROOT_C, wing_cov, seed=7)))
    t_stab = g.texture(jpeg_bytes(surface_texture(STAB_HALF, STAB_C, stab_cov, seed=11, solid_green_from=0.385)))
    t_fuse = g.texture(jpeg_bytes(fuselage_texture()))

    m_fuse = g.material("Fuselage", CREAM, 0.42, tex=t_fuse)
    m_wing = g.material("Wing", CREAM, 0.40, tex=t_wing)
    m_stab = g.material("Tailplane", CREAM, 0.40, tex=t_stab)
    m_white = g.material("White", CREAM, 0.45)
    m_green = g.material("Green", "#0F6B45", 0.38)
    m_orange = g.material("Motor", "#E2721B", 0.35, 0.55)
    m_black = g.material("Black", "#141414", 0.5)
    m_grey = g.material("GPS", "#2A2D2F", 0.55)

    fin_lo, fin_hi = build_fin()
    boom_green, boom_white = build_booms()
    orange, black, grey = Part(), Part(), Part()

    # lift motors (axis up) and nose motor (axis forward)
    hubs = []
    for sx in (-1, 1):
        for s in ROTOR_S:
            base = (sx * BOOM_X, BOOM_Y + POD_H / 2, zs(s))
            hubs.append(motor_stack(orange, black, base, (0, 1, 0)))
    nose_base = (0.0, 0.0, Z0 - 0.004)
    nose_hub = motor_stack(black, black, nose_base, (0, 0, 1))
    build_gps(black, grey)

    body = g.mesh("Airframe", [
        (build_fuselage(), m_fuse), (build_wing(), m_wing), (build_stab(), m_stab),
        (fin_lo, m_white), (fin_hi, m_green), (boom_green, m_green), (boom_white, m_white),
        (build_legs(), m_white), (orange, m_orange), (black, m_black), (grey, m_grey),
    ])
    root = g.node("Hima VTOL", body)

    prop_cw = g.mesh("Propeller CW", [(propeller(LIFT_PROP_R, 0.13, 1), m_black)])
    prop_ccw = g.mesh("Propeller CCW", [(propeller(LIFT_PROP_R, 0.13, -1), m_black)])
    prop_nose = g.mesh("Propeller nose", [(propeller(NOSE_PROP_R, 0.11, 1), m_black)])

    spins = []
    # diagonal pairs turn the same way
    hands = [1, -1, -1, 1]
    for k, (hub, hand) in enumerate(zip(hubs, hands)):
        start = axis_angle((0, 1, 0), math.radians(37 * k + 15))
        n = g.node(f"Lift rotor {k + 1}", prop_cw if hand > 0 else prop_ccw, t=hub - [0, 0.004, 0], r=start, parent=root)
        spins.append((n, (0, 1, 0), -hand, start))
    nose_q = axis_angle((1, 0, 0), math.pi / 2)  # prop +y -> +z
    n = g.node("Nose rotor", prop_nose, t=nose_hub - [0, 0, 0.004], r=nose_q, parent=root)
    spins.append((n, (0, 1, 0), 1, nose_q))
    g.spin(spins, period=0.6)

    size = g.save(OUT)
    print(f"wrote {OUT} ({size / 1024:.0f} KB)")

if __name__ == "__main__":
    main()
