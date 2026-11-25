"""Load and validate hima.toml.

Every section maps to a frozen dataclass. Missing keys, unknown keys (typos), wrong
types and out-of-range values all raise ConfigError naming the exact key.
"""
from __future__ import annotations

import hashlib
import json
import math
try:
    import tomllib
except ModuleNotFoundError:            # Python 3.10 (JetPack 6, Ubuntu 22.04 on the Jetson)
    import tomli as tomllib
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, get_type_hints

from hima.contract import SPECIES

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "hima.toml"
CONFIG_VERSION = 1


class ConfigError(ValueError):
    """hima.toml is missing, malformed or holds an invalid value."""


@dataclass(frozen=True)
class Detector:
    weights: str
    conf: float
    tracker: str
    imgsz: int                       # model input size; 0 = the frame's own width (no shrinking)
    class_map: dict[str, str]


@dataclass(frozen=True)
class Geometry:
    speed_window_s: float


@dataclass(frozen=True)
class Activity:
    provisional: bool
    walk_min_mps: float
    run_min_mps: float


@dataclass(frozen=True)
class Events:
    provisional: bool
    proximity_m: float
    stress_m: float
    isolation_m: float
    min_event_s: float
    merge_gap_s: float


@dataclass(frozen=True)
class Status:
    provisional: bool
    r_stress: float
    r_iso: float
    r_rest: float
    r_iso_transit: float
    r_run: float
    r_prox: float
    r_vis: float


@dataclass(frozen=True)
class Herd:
    timeline_step_s: float
    neighbour_memory_s: float        # isolation also counts animals seen this recently (0 = same frame only)


@dataclass(frozen=True)
class Identity:
    """Which boxes are one animal (analytics/identity.py)."""
    provisional: bool
    confirm_s: float                 # an animal is registered only after it was seen this long in a row (>= 1 s)
    confirm_gap_s: float             # two sightings this close (or in consecutive frames) count as "in a row"
    part_inside: float               # a box this share inside a bigger box of the same frame ...
    part_area_ratio: float           # ... and at most this share of its area is a part of that animal: dropped
    link_gap_s: float                # a new tracker id may continue an animal not seen for at most this long
    link_base_m: float               # allowed distance = base + margin x speed x gap, at most link_max_m
    link_speed_margin: float
    link_max_m: float


@dataclass(frozen=True)
class Hiding:
    """Hidden under cover: not seen although its place is still in the camera view."""
    provisional: bool
    min_s: float                     # hidden time in view, summed over spans, that makes a hidden animal
    span_min_s: float                # a single absence in view shorter than this is a missed detection, not hiding
    margin_px: float                # its last place must be this far inside the image edge


@dataclass(frozen=True)
class Safety:
    provisional: bool
    alt_min_m: float
    alt_max_m: float
    alt_default_m: float
    observe_min_m: float             # MAINTAIN_OBSERVATION never below this


@dataclass(frozen=True)
class Onboard:
    provisional: bool
    update_s: float
    ai_keep_alive_s: float           # flight AI models stay loaded this long after each call (Ollama keep_alive)
    ai_flight_only: bool             # before take-off: unload every other model, load the flight models


@dataclass(frozen=True)
class Alerts:
    provisional: bool
    min_visible_s: float
    cooldown_s: float
    max_message_bytes: int


@dataclass(frozen=True)
class Injury:
    """Staged injury alerts: the animal must stay alone AND in one place without a break."""
    provisional: bool
    stage1_s: float                  # alone and still this long: INJ1 (watch it)
    stage2_s: float                  # INJ2 (suspected injury; the camera window may open)
    stage3_s: float                  # INJ3 (strong suspicion; a ranger should come)
    still_radius_m: float            # "still" = all positions of the span fit in a box of side 2 * this
    max_gap_s: float                 # a longer gap between two sightings ends the span (not seen = not known)


@dataclass(frozen=True)
class Young:
    """Possible hidden young: an animal clearly smaller than an adult that would be called injured."""
    provisional: bool
    oryx_adult_m: float              # adult long side of the box seen from above
    gazelle_adult_m: float
    max_ratio: float                 # body length <= max_ratio * adult: possible young
    min_samples: int                 # clean boxes (not touching the image edge) needed to judge the size

    def adult_m(self, species: str) -> float:
        return {"arabian_oryx": self.oryx_adult_m, "reem_gazelle": self.gazelle_adult_m}[species]


@dataclass(frozen=True)
class Cause:
    """Who started a run, for SEVERE alerts."""
    provisional: bool
    approach_s: float                # look this far back before the run started
    approach_m: float                # the drone came at least this much closer to where the animal stood: DRONE
    vehicle_pre_s: float             # a vehicle seen from this long before the run up to the alert: VEHICLE


@dataclass(frozen=True)
class Link:
    provisional: bool
    bytes_per_s: float
    signed: bool


@dataclass(frozen=True)
class Triage:
    provisional: bool
    pre_s: float
    post_s: float
    max_clip_s: float
    min_track_s: float
    switch_gap_s: float
    switch_dist_px: float
    untracked_gap_s: float
    jpeg_quality: int
    clip_fourcc: str
    storage_budget_mb: float


@dataclass(frozen=True)
class Report:
    provisional: bool
    max_findings: int
    max_chars: int
    model: str
    timeout_s: float
    judge: bool
    judge_model: str
    num_ctx: int


@dataclass(frozen=True)
class Objects:
    provisional: bool
    weights: str
    imgsz: int
    conf: float
    every_s: float
    k: int
    cooldown_s: float


@dataclass(frozen=True)
class Periodic:
    provisional: bool
    every_s: float
    model: str
    max_sentences: int
    timeout_s: float


@dataclass(frozen=True)
class Window:
    provisional: bool
    model: str
    max_images: int
    image_every_s: float
    max_per_flight: int
    cooldown_s: float
    crop_px: int
    timeout_s: float
    num_ctx: int


@dataclass(frozen=True)
class Camera:
    """The drone camera for a live flight (python -m hima fly)."""
    device: str                      # "0" = /dev/video0, a device path, a GStreamer pipeline (has "!"), or a video file
    width_px: int                    # asked of the camera; the frames' real size goes into the session
    height_px: int
    hfov_deg: float                  # horizontal angle of view of the lens (measured: 24 deg)
    fps: float                       # frames handed to YOLO per second (newer camera frames replace older ones)


@dataclass(frozen=True)
class Mavlink:
    """The Pixhawk link (ArduPilot) on TELEM2."""
    url: str                         # serial port, or tcp:/udpin: for SITL and routers (telemetry/mavlink.py)
    baud: int                        # serial only; = SERIAL2_BAUD on the autopilot (921 -> 921600)
    heartbeat_timeout_s: float       # preflight: no autopilot HEARTBEAT within this: no flight
    position_hz: float               # GLOBAL_POSITION_INT rate asked for
    max_pose_age_s: float            # an older position is not used (no pose rather than a wrong one)


@dataclass(frozen=True)
class Flight:
    """The live flight runner."""
    out_dir: str                     # every flight gets a new folder in here (relative to hima.toml)
    min_free_gb: float               # preflight: free disk space needed
    min_satellites: int              # preflight
    min_fix_type: int                # preflight: 3 = 3D fix
    preflight_wait_s: float          # GPS / HOME / pose may come late after power-on: check again this long
    min_agl_m: float                 # frames below this height (take-off, landing) are not analysed
    start_on_arm: bool               # record from arming to disarming (else from start to Ctrl+C)
    max_flight_s: float              # the recorder stops after this (the drone is not affected)
    save_video: bool                 # flight video for the event clips of the package
    no_position_image_every_s: float  # no pose from the Pixhawk: save one camera image this often


@dataclass(frozen=True)
class Dwell:
    """How long the drone may stay over one animal."""
    provisional: bool
    max_over_animal_s: float         # one animal in view this long without a break: advise to leave
    max_gap_s: float                 # not seen longer than this: the stay ends
    repeat_s: float                  # no new advice for any animal within this time


@dataclass(frozen=True)
class Delivery:
    """After landing, the package goes to the ground station over the base Wi-Fi."""
    station_url: str                 # drone: the station's receiver, http://<fixed address of the station PC>:<port>
    key_file: str                    # both: the shared secret (python -m hima keygen), relative to hima.toml
    listen: str                      # station: HOST:PORT the receiver listens on
    inbox_dir: str                   # station: packages being received, and the rejected ones
    archive_dir: str                 # station: the ground archive (the dashboard reads it)
    chunk_mb: float                  # one upload piece; a cut connection resends at most this much
    retry_s: float                   # drone: try again this often while a package waits
    timeout_s: float                 # one request: connect or answer


@dataclass(frozen=True)
class Reserve:
    """The reserve the drone protects: its boundary, the fence, the grid, the launch points (autonomy)."""
    name: str
    boundary: str                    # GeoJSON of the boundary (WDPA download), relative to hima.toml; never in git
    history: str                     # what past flights left (autonomy/history.py), relative to hima.toml
    fence_max_vertices: int          # ArduPilot polygon fence: at most 70 points (common-polygon_fence docs)
    fence_margin_m: float            # ArduPilot FENCE_MARGIN: keep this far from the fence
    plan_margin_m: float             # the planned route stays this far inside the fence
    cell_m: float                    # grid cell side; a cell counts as visited when the route crosses it
    launch: dict[str, list[float]]   # launch points: name -> [lat_deg, lon_deg] (proposals, autonomy/reserve.py)


@dataclass(frozen=True)
class Vehicle:
    """The drone for planning: VTOL quadplane, 4 lift motors + 1 pusher (medium specs; design not final)."""
    provisional: bool
    cruise_speed_mps: float          # forward flight on the wing
    cruise_endurance_min: float      # full battery, forward flight only, no wind
    reserve_frac: float              # of the battery, never planned (wind, a go-around, a late landing)
    hover_power_ratio: float         # hover draws this many times the power of forward flight
    takeoff_hover_s: float           # vertical climb and the change to forward flight
    landing_hover_s: float           # change back, approach and vertical landing
    takeoff_alt_m: float             # VTOL take-off height before going forward
    patrol_alt_m: float              # height of the route (inside safety.alt_min_m .. alt_max_m)
    land_approach_m: float           # no target closer than this to the launch point (VTOL landing approach)


@dataclass(frozen=True)
class Planner:
    """How the route is chosen before take-off (autonomy/planner.py)."""
    provisional: bool
    herd_share: float                # of the flight time, kept free for following herds in flight
    w_age: float                     # weight: time since the cell was last seen
    w_risk: float                    # weight: poaching incidents near the cell
    w_herd: float                    # weight: herds seen near the cell
    age_full_days: float             # a cell not seen this long (or never) gets the full age score
    risk_radius_m: float             # an incident counts for cells within about this distance
    risk_half_life_days: float       # an incident counts half after this long
    herd_radius_m: float             # a sighting counts for cells within about this distance
    herd_half_life_days: float       # herds move: a sighting counts half after this long
    candidates: int                  # best cells offered to the route builder
    target_spacing_m: float          # two offered cells are at least this far apart


@dataclass(frozen=True)
class Behaviour:
    """In-flight autonomy (autonomy/behaviour.py)."""
    provisional: bool
    enabled: bool
    herd_min_animals: int
    herd_link_m: float
    herd_memory_s: float
    herd_lost_s: float
    follow_herd_max_s: float
    herd_revisit_s: float
    herd_revisit_m: float
    vehicle_near_herd_m: float       # a vehicle is a danger only this close to a herd (200 m)
    vehicle_close_m: float           # this close: "HIMA VEH CLOSE" to the station at once
    vehicle_herd_min_animals: int
    vehicle_leave_m: float           # the follow ends when the vehicle is this far from the herd ...
    vehicle_leave_s: float           # ... for this long (no time limit otherwise; only the energy)
    vehicle_lost_s: float
    vehicle_photo_every_s: float     # following a vehicle: one camera image this often, kept and sent after landing
    vehicle_lead_s: float            # a moving vehicle: hover this far ahead of it (its speed from the last sightings)
    hover_move_m: float              # hovering: a new hover point only when the target moved this far
    hover_arrive_m: float            # this close to the hover point counts as hovering (energy)
    herd_min_hover_s: float          # a herd is followed only with this much hover left in the budget
    tick_s: float
    energy_margin_s: float
    battery_margin_pct: float
    manual_grace_s: float
    upload_plan: bool


@dataclass(frozen=True)
class RouteReview:
    """Today's route is shown at the station before take-off and can be edited there."""
    enabled: bool
    wait_s: float                    # the drone waits this long for the station; no answer: its own route
    poll_s: float                    # Wi-Fi: ask the station for its answer this often
    wifi: bool                       # offer over the delivery Wi-Fi ([delivery].station_url, signed)
    radio: bool                      # offer over the telemetry radio (the route goes up to the autopilot first)
    plans_dir: str                   # station: offers and answers (the receiver, the radio bridge, the dashboard)
    radio_url: str                   # station: MAVLink connection to the ground radio ("off" = no radio bridge)
    station_sysid: int               # station: its MAVLink system id on the radio (not the Jetson's 1, not QGC's 255)
    imagery_dir: str                 # station: the satellite images under the route map (python -m hima imagery)


@dataclass(frozen=True)
class Demo:
    """A virtual flight always in the station's archive, so the dashboard is never empty."""
    enabled: bool
    package: str                     # a mission package (a simulated flight), ingested at the station's start
    route_offer: str                 # a route offer shown on the Route page (its wait is over: never pending)
    more: str                        # a folder of more virtual flights (synthetic packages) to compare; "" = none


@dataclass(frozen=True)
class Rangers:
    """The reserve's own vehicles: planned field work and roof markers. Off for now (one vehicle class until the reserve's vehicles are known)."""
    enabled: bool
    field_ops: str
    marker_dict: str
    marker_ids: str
    marker_pad: float


@dataclass(frozen=True)
class Llm:
    model: str
    url: str
    temperature: float
    seed: int
    timeout_s: float


_SECTIONS: dict[str, type] = {
    "detector": Detector,
    "geometry": Geometry,
    "activity": Activity,
    "events": Events,
    "status": Status,
    "herd": Herd,
    "identity": Identity,
    "hiding": Hiding,
    "safety": Safety,
    "llm": Llm,
    "onboard": Onboard,
    "alerts": Alerts,
    "link": Link,
    "triage": Triage,
    "report": Report,
    "injury": Injury,
    "young": Young,
    "cause": Cause,
    "objects": Objects,
    "periodic": Periodic,
    "window": Window,
    "dwell": Dwell,
    "camera": Camera,
    "mavlink": Mavlink,
    "flight": Flight,
    "delivery": Delivery,
    "reserve": Reserve,
    "vehicle": Vehicle,
    "planner": Planner,
    "behaviour": Behaviour,
    "rangers": Rangers,
    "route_review": RouteReview,
    "demo": Demo,
}


@dataclass(frozen=True)
class Config:
    config_version: int
    detector: Detector
    geometry: Geometry
    activity: Activity
    events: Events
    status: Status
    herd: Herd
    identity: Identity
    hiding: Hiding
    safety: Safety
    llm: Llm
    onboard: Onboard
    alerts: Alerts
    injury: Injury
    young: Young
    cause: Cause
    link: Link
    triage: Triage
    report: Report
    objects: Objects
    periodic: Periodic
    window: Window
    dwell: Dwell
    camera: Camera
    mavlink: Mavlink
    flight: Flight
    delivery: Delivery
    reserve: Reserve
    vehicle: Vehicle
    planner: Planner
    behaviour: Behaviour
    rangers: Rangers
    route_review: RouteReview
    demo: Demo
    sha256: str
    source: str | None

    def values(self) -> dict[str, Any]:
        """Plain dict of all settings (for analysis.config_used.values)."""
        d = asdict(self)
        d.pop("sha256")
        d.pop("source")
        return d


def load_config(path: str | Path | None = None) -> Config:
    p = Path(path) if path is not None else DEFAULT_PATH
    try:
        data = p.read_bytes()
    except OSError as e:
        raise ConfigError(f"cannot read config file {p}: {e}") from e
    try:
        raw = tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"{p} is not valid TOML: {e}") from e
    return config_from_dict(raw, sha256=hashlib.sha256(data).hexdigest(), source=str(p))


def override(cfg: Config, section: str, **values: Any) -> Config:
    """Copy of cfg with some values of one section changed (e.g. a CLI flag).

    The result is re-validated, and its sha256 is recomputed from the new values, so
    analysis.config_used never pairs the hash of hima.toml with values that differ from it.
    """
    raw = cfg.values()
    if section not in _SECTIONS:
        raise ConfigError(f"unknown section: [{section}]")
    unknown = sorted(set(values) - set(raw[section]))
    if unknown:
        raise ConfigError(f"unknown key(s) in [{section}]: {', '.join(unknown)}")
    raw[section].update(values)
    return config_from_dict(raw, source=cfg.source)


def config_from_dict(raw: dict[str, Any], *, sha256: str | None = None,
                     source: str | None = None) -> Config:
    if not isinstance(raw, dict):
        raise ConfigError("config root must be a table")
    allowed = {"config_version", *_SECTIONS}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ConfigError(f"unknown top-level key(s): {', '.join(unknown)}")
    if "config_version" not in raw:
        raise ConfigError("missing key: config_version")
    version = _typed("config_version", raw["config_version"], int)
    if version != CONFIG_VERSION:
        raise ConfigError(f"config_version must be {CONFIG_VERSION}, got {version}")

    sections = {}
    for name, cls in _SECTIONS.items():
        if name not in raw:
            raise ConfigError(f"missing section: [{name}]")
        sections[name] = _build(cls, name, raw[name])

    if sha256 is None:
        sha256 = hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()
    cfg = Config(config_version=version, sha256=sha256, source=source, **sections)
    _check_ranges(cfg)
    return cfg


def _build(cls: type, section: str, raw: Any):
    if not isinstance(raw, dict):
        raise ConfigError(f"[{section}] must be a table")
    hints = get_type_hints(cls)
    names = [f.name for f in fields(cls)]
    unknown = sorted(set(raw) - set(names))
    if unknown:
        raise ConfigError(f"unknown key(s) in [{section}]: {', '.join(unknown)}")
    kwargs = {}
    for name in names:
        key = f"{section}.{name}"
        if name not in raw:
            raise ConfigError(f"missing key: {key}")
        kwargs[name] = _typed(key, raw[name], hints[name])
    return cls(**kwargs)


def _typed(key: str, value: Any, typ: Any) -> Any:
    # bool is a subclass of int in Python, so it is rejected explicitly for numbers.
    if typ is bool:
        if not isinstance(value, bool):
            raise ConfigError(f"{key} must be true/false, got {value!r}")
        return value
    if typ is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{key} must be an integer, got {value!r}")
        return value
    if typ is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ConfigError(f"{key} must be a finite number, got {value!r}")
        return float(value)
    if typ is str:
        if not isinstance(value, str) or not value:
            raise ConfigError(f"{key} must be a non-empty string, got {value!r}")
        return value
    if typ == dict[str, str]:
        if not isinstance(value, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
            raise ConfigError(f"{key} must be a table of strings")
        return dict(value)
    if typ == dict[str, list[float]]:
        ok = isinstance(value, dict) and all(
            isinstance(k, str) and isinstance(v, list) and all(
                isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in v)
            for k, v in value.items())
        if not ok:
            raise ConfigError(f"{key} must be a table of number lists")
        return {k: [float(x) for x in v] for k, v in value.items()}
    raise TypeError(f"unsupported config type for {key}: {typ}")  # programming error


def _check_ranges(c: Config) -> None:
    def need(ok: bool, msg: str) -> None:
        if not ok:
            raise ConfigError(msg)

    need(0.0 < c.detector.conf <= 1.0, "detector.conf must be in (0, 1]")
    need(c.detector.imgsz == 0 or (c.detector.imgsz >= 32 and c.detector.imgsz % 32 == 0),
         "detector.imgsz must be 0 (the frame's own width) or a multiple of 32")
    need(bool(c.detector.class_map), "detector.class_map must not be empty")
    for raw_name, species in c.detector.class_map.items():
        need(species in SPECIES,
             f"detector.class_map[{raw_name!r}] = {species!r} is not one of {list(SPECIES)}")

    need(c.geometry.speed_window_s > 0, "geometry.speed_window_s must be > 0")

    need(c.activity.walk_min_mps > 0, "activity.walk_min_mps must be > 0")
    need(c.activity.run_min_mps > c.activity.walk_min_mps,
         "activity.run_min_mps must be > activity.walk_min_mps")

    for k in ("proximity_m", "stress_m", "isolation_m"):
        need(getattr(c.events, k) > 0, f"events.{k} must be > 0")
    for k in ("min_event_s", "merge_gap_s"):
        need(getattr(c.events, k) >= 0, f"events.{k} must be >= 0")

    for f in fields(Status):
        if f.name.startswith("r_"):
            v = getattr(c.status, f.name)
            need(0.0 <= v <= 1.0, f"status.{f.name} must be in [0, 1], got {v}")

    need(c.herd.timeline_step_s > 0, "herd.timeline_step_s must be > 0")
    need(c.herd.neighbour_memory_s >= 0, "herd.neighbour_memory_s must be >= 0")
    i = c.identity
    need(i.confirm_s >= 0, "identity.confirm_s must be >= 0")
    need(i.confirm_gap_s > 0, "identity.confirm_gap_s must be > 0")
    need(0.0 < i.part_inside <= 1.0, "identity.part_inside must be in (0, 1]")
    need(0.0 < i.part_area_ratio < 1.0, "identity.part_area_ratio must be in (0, 1)")
    need(i.link_gap_s >= 0, "identity.link_gap_s must be >= 0")
    need(i.link_base_m >= 0 and i.link_speed_margin >= 0, "identity.link_base_m and link_speed_margin must be >= 0")
    need(i.link_max_m >= i.link_base_m, "identity.link_max_m must be >= identity.link_base_m")
    need(c.hiding.min_s > 0, "hiding.min_s must be > 0")
    need(0 < c.hiding.span_min_s <= c.hiding.min_s, "hiding.span_min_s must be in (0, hiding.min_s]")
    need(c.hiding.margin_px >= 0,"hiding.margin_px must be >= 0")
    o = c.objects
    need(o.imgsz >= 32 and o.imgsz % 32 == 0, "objects.imgsz must be a multiple of 32")
    need(0.0 < o.conf <= 1.0, "objects.conf must be in (0, 1]")
    need(o.every_s > 0, "objects.every_s must be > 0")
    need(o.k >= 1, "objects.k must be >= 1")
    need(o.cooldown_s >= 0, "objects.cooldown_s must be >= 0")
    need(c.periodic.every_s > 0, "periodic.every_s must be > 0")
    need(1 <= c.periodic.max_sentences <= 5, "periodic.max_sentences must be in [1, 5]")
    need(c.periodic.timeout_s > 0, "periodic.timeout_s must be > 0")
    w = c.window
    need(1 <= w.max_images <= 8, "window.max_images must be in [1, 8]")
    need(w.image_every_s > 0, "window.image_every_s must be > 0")
    need(w.max_per_flight >= 0, "window.max_per_flight must be >= 0")
    need(w.cooldown_s >= 0, "window.cooldown_s must be >= 0")
    need(w.crop_px >= 64, "window.crop_px must be >= 64")
    need(w.timeout_s > 0, "window.timeout_s must be > 0")
    need(w.num_ctx >= 2048, "window.num_ctx must be >= 2048")

    s = c.safety
    need(0 < s.alt_min_m < s.alt_default_m < s.alt_max_m,
         "safety must satisfy 0 < alt_min_m < alt_default_m < alt_max_m, got "
         f"{s.alt_min_m} / {s.alt_default_m} / {s.alt_max_m}")
    need(s.alt_min_m <= s.observe_min_m <= s.alt_max_m, "safety.observe_min_m must be in [alt_min_m, alt_max_m]")

    need(c.llm.temperature >= 0, "llm.temperature must be >= 0")
    need(c.llm.timeout_s > 0, "llm.timeout_s must be > 0")
    need(c.llm.url.startswith(("http://", "https://")), "llm.url must start with http:// or https://")

    need(c.onboard.update_s > 0, "onboard.update_s must be > 0")
    need(c.onboard.ai_keep_alive_s > 0, "onboard.ai_keep_alive_s must be > 0")

    d = c.dwell
    need(d.max_over_animal_s > c.injury.stage3_s,
         "dwell.max_over_animal_s must be > injury.stage3_s (the injury stages must be able to finish)")
    need(d.max_gap_s > 0 and d.repeat_s >= 0, "dwell.max_gap_s must be > 0 and dwell.repeat_s >= 0")
    cam = c.camera
    need(bool(cam.device.strip()), "camera.device must not be empty")
    need(cam.width_px > 0 and cam.height_px > 0, "camera.width_px and camera.height_px must be > 0")
    need(0 < cam.hfov_deg < 180, "camera.hfov_deg must be in (0, 180)")
    need(0 < cam.fps <= 60, "camera.fps must be in (0, 60]")
    mv = c.mavlink
    need(bool(mv.url.strip()), "mavlink.url must not be empty")
    need(mv.baud > 0, "mavlink.baud must be > 0")
    need(mv.heartbeat_timeout_s > 0 and mv.position_hz > 0 and mv.max_pose_age_s > 0,
         "mavlink timeouts and rates must be > 0")
    fl = c.flight
    need(bool(fl.out_dir.strip()), "flight.out_dir must not be empty")
    need(fl.min_free_gb >= 0 and fl.min_satellites >= 0, "flight.min_free_gb and flight.min_satellites must be >= 0")
    need(0 <= fl.min_fix_type <= 6, "flight.min_fix_type must be in [0, 6] (GPS_FIX_TYPE)")
    need(fl.max_flight_s > 0, "flight.max_flight_s must be > 0")
    need(fl.no_position_image_every_s > 0, "flight.no_position_image_every_s must be > 0")
    need(fl.preflight_wait_s >= 0, "flight.preflight_wait_s must be >= 0")
    need(fl.min_agl_m > 0, "flight.min_agl_m must be > 0 (the session needs agl_m > 0)")
    dv = c.delivery
    need(dv.station_url.startswith("http://") and len(dv.station_url) > len("http://"),
         "delivery.station_url must start with http:// (e.g. http://192.168.1.10:8765)")
    need(dv.listen.rpartition(":")[2].isdigit() and 0 < int(dv.listen.rpartition(":")[2]) < 65536,
         "delivery.listen must be HOST:PORT (e.g. 0.0.0.0:8765)")
    for k in ("key_file", "inbox_dir", "archive_dir"):
        need(bool(getattr(dv, k).strip()), f"delivery.{k} must not be empty")
    need(0 < dv.chunk_mb <= 64, "delivery.chunk_mb must be in (0, 64]")
    need(dv.retry_s > 0 and dv.timeout_s > 0, "delivery.retry_s and delivery.timeout_s must be > 0")

    rs = c.reserve
    for k in ("name", "boundary", "history"):
        need(bool(getattr(rs, k).strip()), f"reserve.{k} must not be empty")
    need(3 <= rs.fence_max_vertices <= 70, "reserve.fence_max_vertices must be in [3, 70] (ArduPilot polygon fence)")
    need(rs.fence_margin_m >= 0 and rs.plan_margin_m >= 0, "reserve.fence_margin_m and plan_margin_m must be >= 0")
    need(rs.cell_m >= 50, "reserve.cell_m must be >= 50")
    need(bool(rs.launch), "reserve.launch must name at least one launch point")
    for k, v in rs.launch.items():
        need(len(v) == 2 and -90 <= v[0] <= 90 and -180 <= v[1] <= 180,
             f"reserve.launch.{k} must be [lat_deg, lon_deg]")
    vh = c.vehicle
    need(vh.cruise_speed_mps > 0 and vh.cruise_endurance_min > 0, "vehicle speed and endurance must be > 0")
    need(0 <= vh.reserve_frac < 1, "vehicle.reserve_frac must be in [0, 1)")
    need(vh.hover_power_ratio >= 1, "vehicle.hover_power_ratio must be >= 1")
    need(vh.takeoff_hover_s >= 0 and vh.landing_hover_s >= 0, "vehicle take-off / landing seconds must be >= 0")
    need(0 < vh.takeoff_alt_m <= c.safety.alt_max_m, "vehicle.takeoff_alt_m must be in (0, safety.alt_max_m]")
    need(c.safety.alt_min_m <= vh.patrol_alt_m <= c.safety.alt_max_m,
         "vehicle.patrol_alt_m must be within safety.alt_min_m .. alt_max_m")
    need(vh.land_approach_m > 0, "vehicle.land_approach_m must be > 0")
    need(rs.fence_margin_m < c.safety.alt_max_m - vh.patrol_alt_m,
         "reserve.fence_margin_m must be < safety.alt_max_m - vehicle.patrol_alt_m (ArduPlane applies it to the ceiling)")
    pl = c.planner
    need(0 <= pl.herd_share < 1, "planner.herd_share must be in [0, 1)")
    need(min(pl.w_age, pl.w_risk, pl.w_herd) >= 0 and pl.w_age + pl.w_risk + pl.w_herd > 0,
         "planner weights must be >= 0 and not all 0")
    for k in ("age_full_days", "risk_radius_m", "risk_half_life_days", "herd_radius_m", "herd_half_life_days",
              "target_spacing_m"):
        need(getattr(pl, k) > 0, f"planner.{k} must be > 0")
    need(pl.candidates >= 1, "planner.candidates must be >= 1")

    bh = c.behaviour
    need(bh.herd_min_animals >= 1 and bh.vehicle_herd_min_animals >= 1, "behaviour herd sizes must be >= 1")
    for k in ("herd_link_m", "herd_memory_s", "herd_lost_s", "follow_herd_max_s", "herd_revisit_s", "herd_revisit_m",
              "vehicle_near_herd_m", "vehicle_close_m", "vehicle_leave_m", "vehicle_lost_s", "vehicle_photo_every_s",
              "vehicle_lead_s",
              "hover_move_m", "hover_arrive_m", "tick_s"):
        need(getattr(bh, k) > 0, f"behaviour.{k} must be > 0")
    need(bh.vehicle_close_m <= bh.vehicle_near_herd_m < bh.vehicle_leave_m,
         "behaviour: vehicle_close_m <= vehicle_near_herd_m < vehicle_leave_m")
    need(bh.vehicle_leave_s >= 0 and bh.herd_min_hover_s >= 0, "behaviour.vehicle_leave_s / herd_min_hover_s >= 0")
    need(bh.energy_margin_s >= 0 and bh.battery_margin_pct >= 0 and bh.manual_grace_s >= 0,
         "behaviour margins must be >= 0")
    rg = c.rangers
    need(bool(rg.field_ops.strip()) and rg.marker_dict.startswith("DICT_"), "rangers.field_ops / marker_dict")
    try:
        ids = [int(x) for x in rg.marker_ids.split(",") if x.strip()]
    except ValueError:
        ids = None
    need(ids is not None and all(i >= 0 for i in ids), "rangers.marker_ids must be comma-separated integers")
    need(rg.marker_pad >= 0, "rangers.marker_pad must be >= 0")
    rr = c.route_review
    need(rr.wait_s >= 0 and rr.poll_s > 0, "route_review.wait_s >= 0 and poll_s > 0")
    need(1 <= rr.station_sysid <= 255 and rr.station_sysid != 1, "route_review.station_sysid must be 2..255")

    need(c.alerts.min_visible_s >= 0, "alerts.min_visible_s must be >= 0")
    need(c.alerts.cooldown_s >= 0, "alerts.cooldown_s must be >= 0")
    # MAVLink STATUSTEXT.text is char[50] (common.xml, message 253): one alert = one message.
    need(1 <= c.alerts.max_message_bytes <= 50, "alerts.max_message_bytes must be in [1, 50]")
    j = c.injury
    need(0 < j.stage1_s < j.stage2_s < j.stage3_s, "injury must satisfy 0 < stage1_s < stage2_s < stage3_s")
    need(j.still_radius_m > 0, "injury.still_radius_m must be > 0")
    need(j.max_gap_s > 0, "injury.max_gap_s must be > 0")
    y = c.young
    need(y.oryx_adult_m > 0 and y.gazelle_adult_m > 0, "young adult lengths must be > 0")
    need(0 < y.max_ratio < 1, "young.max_ratio must be in (0, 1)")
    need(y.min_samples >= 1, "young.min_samples must be >= 1")
    cs = c.cause
    need(cs.approach_s > 0 and cs.approach_m > 0, "cause.approach_s and cause.approach_m must be > 0")
    need(cs.vehicle_pre_s >= 0, "cause.vehicle_pre_s must be >= 0")
    need(c.link.bytes_per_s > 0, "link.bytes_per_s must be > 0")

    tr = c.triage
    for k in ("pre_s", "post_s", "min_track_s", "switch_gap_s", "switch_dist_px", "untracked_gap_s"):
        need(getattr(tr, k) >= 0, f"triage.{k} must be >= 0")
    need(tr.max_clip_s > 0, "triage.max_clip_s must be > 0")
    need(0 <= tr.jpeg_quality <= 100, "triage.jpeg_quality must be in [0, 100]")
    need(len(tr.clip_fourcc) == 4, "triage.clip_fourcc must be 4 characters")
    need(tr.storage_budget_mb > 0, "triage.storage_budget_mb must be > 0")

    need(c.report.max_findings >= 1, "report.max_findings must be >= 1")
    need(c.report.max_chars >= 100, "report.max_chars must be >= 100")
    need(bool(c.report.model.strip()), "report.model must not be empty")
    need(c.report.timeout_s > 0, "report.timeout_s must be > 0")
    need(bool(c.report.judge_model.strip()), "report.judge_model must not be empty")
    need(c.report.num_ctx >= 2048, "report.num_ctx must be >= 2048")
