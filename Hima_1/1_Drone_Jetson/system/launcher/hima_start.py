#!/usr/bin/env python3
"""HIMA launcher: one program that prepares this computer and checks everything before a flight.

    Windows (ground PC or bench test):  double-click launcher\\start.bat
    Jetson (the drone computer):        bash launcher/start.sh
    Ground station PC:                  double-click launcher\\start_station.bat  (or bash launcher/start_station.sh)

What it does, in order (each step prints OK / WARN / FAIL, and a summary at the end):
    1. device      Windows PC, Jetson (model, JetPack, memory) or another Linux PC; NVIDIA GPU
    2. python      the virtual environment system/.venv: created and filled with the missing libraries
                   (Windows + NVIDIA: torch CUDA 12.8; Jetson: the Jetson torch build; otherwise CPU torch)
    3. weights     best_4_1.pt (SHA-256 checked) and the general YOLO yolo26s.pt (downloaded if missing)
    4. ai          Ollama installed, started, and the models in hima.toml pulled
    5. camera      the camera in hima.toml, or the first camera found, gives a frame
    6. pixhawk     finds the autopilot: TELEM2 (Jetson UART /dev/ttyTHS*, a USB-TTL adapter) or USB,
                   and the baud rate, by waiting for its HEARTBEAT on each port
    7. station     the ground station answers over the network with the same delivery key (WARN only: after
                   landing the package waits on the drone until the station answers)
    8. preflight   the system's own check: python -m hima preflight, with what steps 5 and 6 found
    9. fly         only with --fly: python -m hima fly (it waits for ARM)

With --station (the ground station PC), after step 1 and step 2 with requirements-station.txt (no torch, no
YOLO, no Ollama: the station only receives the packages, shows them, and edits the route before take-off):
    3. key        the delivery key shared with the drone (made here the first time: copy it to the drone)
    4. network     this PC's addresses, for [delivery].station_url on the drone
    5. dashboard   opens the dashboard on the archive (unless --no-dashboard)
    6. receiver    python -m hima receive: takes the drone's packages, checks them, adds them to the archive

With --links (launcher\\test_links.bat, bash launcher/test_links.sh), after steps 1 and 2: every test of the
links to the Pixhawk (USB, the TELEM2 adapter, the telemetry radio), the camera and the station, with a report in
runs/link_tests/ (launcher/link_tests.py lists the 14 tests). It changes nothing on the Pixhawk.

Options:
    --no-install       check only: install nothing (what the drone service uses at each boot)
    --fly              after the checks, start the flight program
    --ai-off           no Ollama: the flight runs without the AI (alerts are rules and still work)
    --camera X         use this camera (0, /dev/video0, or a video file for a bench test)
    --mavlink X        use this link (COM5, /dev/ttyTHS1, tcp:127.0.0.1:5763 for ArduPilot SITL)
    --baud N           with --mavlink on a serial port
    --tests            also run the test suite (only where the tests folder exists)
    --install-service  Jetson / Linux: install the hima-fly service (this launcher with --fly at every boot) and
                       the hima-deliver service (sends each new package to the station); installed, not enabled
    --station          this PC is the ground station: receive the packages and open the dashboard
    --listen H:P       with --station: where the receiver listens (default [delivery].listen, 0.0.0.0:8765)
    --no-dashboard     with --station: do not open the dashboard
    --links            the link tests instead of the flight checks; with it:
      --ground X         the ground radio's port (found by itself otherwise), --ground-baud N
      --no-hands         skip the tests that need someone at the drone (rotate, lift, arm, cut)
      --window S         seconds of the link measurement (default 60)
      --gps-wait S       longest wait for the GPS fix (default 120; s + Enter skips it)
      --no-camera, --no-station   skip those tests
      --no-ui            no page in the browser (it shows the test, what to do, and buttons)
      --no-preflight     skip test 14, the flight program's own checks (python -m hima preflight)
      --compare [FILE]   compare with an earlier report (default: the newest in runs/link_tests/)

It never touches the autopilot's parameters, and it never deletes anything.
Stage 1 runs on the computer's own Python (standard library only); stage 2 runs inside system/.venv.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent            # system/launcher
SYSTEM = HERE.parent                              # system/
VENV = SYSTEM / ".venv"
WINDOWS = os.name == "nt"
VENV_PY = VENV / ("Scripts/python.exe" if WINDOWS else "bin/python")
MARKER = VENV / ".hima_installed.json"

ANIMAL_WEIGHTS = SYSTEM / "best_4_1.pt"
ANIMAL_SHA256 = "712340651a57e15d41d2ec4621a3aa116d4188d36e6d8d2b9d33c74bd7286724"   # README, section 3

# torch builds (the same pins as requirements.txt, README and the Ultralytics Jetson guide)
TORCH_CUDA_PC = ["torch==2.11.0", "torchvision==0.26.0"]
TORCH_CUDA_INDEX = "https://download.pytorch.org/whl/cu128"
TORCH_JETSON = [
    "https://github.com/ultralytics/assets/releases/download/v0.0.0/torch-2.10.0-cp310-cp310-linux_aarch64.whl",
    "https://github.com/ultralytics/assets/releases/download/v0.0.0/torchvision-0.25.0-cp310-cp310-linux_aarch64.whl",
]
JETSON_APT = ["python3-venv", "python3-pip", "python3-dev", "libopenblas-dev", "curl", "v4l-utils"]
FLIGHT_IMPORTS = "import ultralytics, cv2, numpy, torch, pymavlink, serial, requests, jsonschema"
STATION_IMPORTS = "import cv2, numpy, pandas, pymavlink, serial, requests, jsonschema"

# USB ids, to tell an autopilot's own USB port from a USB-TTL adapter wired to TELEM2
AUTOPILOT_VIDS = {0x26AC: "3DR / Pixhawk", 0x1209: "ArduPilot", 0x2DAE: "CubePilot", 0x3162: "Holybro"}
AUTOPILOT_WORDS = ("pixhawk", "px4", "ardupilot", "cube", "fmu", "holybro")
ADAPTER_VIDS = {0x0403: "FTDI", 0x10C4: "Silicon Labs CP210x", 0x1A86: "WCH CH340", 0x067B: "Prolific"}

RESULTS: list[tuple[str, str, str]] = []          # (step, level, detail)


# ---------------------------------------------------------------------------------------------- printing
def say(text: str = "") -> None:
    print(text, flush=True)


def step(n: int, name: str) -> None:
    say(f"\n== {n}. {name}")


def result(name: str, level: str, detail: str) -> str:
    RESULTS.append((name, level, detail))
    say(f"  [{level:<4}] {detail}")
    return level


def run(cmd: list[str], *, check: bool = True, cwd: Path | None = None, quiet: bool = False,
        env: dict | None = None) -> int:
    say("  $ " + " ".join(str(c) for c in cmd))
    out = subprocess.DEVNULL if quiet else None
    rc = subprocess.call([str(c) for c in cmd], cwd=str(cwd or SYSTEM), stdout=out, env=env)
    if check and rc != 0:
        raise SystemExit(f"\nstopped: the command above failed (exit {rc}). Read its output, fix it, run again.")
    return rc


def capture(cmd: list[str], timeout: float = 60) -> tuple[int, str]:
    try:
        p = subprocess.run([str(c) for c in cmd], capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, f"{type(e).__name__}: {e}"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------------------------- 1. device
def detect_device() -> dict:
    d = {"os": platform.system(), "machine": platform.machine(), "python": platform.python_version(),
         "kind": "windows" if WINDOWS else "linux", "gpu": None, "memory_gb": None}
    model = Path("/proc/device-tree/model")
    tegra = Path("/etc/nv_tegra_release")
    if not WINDOWS and (tegra.exists() or (model.exists() and "jetson" in model.read_bytes().decode(errors="ignore").lower())):
        d["kind"] = "jetson"
        d["model"] = model.read_bytes().decode(errors="ignore").strip("\x00 \n") if model.exists() else "Jetson"
        d["l4t"] = tegra.read_text(errors="ignore").splitlines()[0].strip() if tegra.exists() else "unknown"
        d["gpu"] = "Jetson integrated GPU"
    if Path("/proc/meminfo").exists():
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                d["memory_gb"] = round(int(line.split()[1]) / 1024 / 1024, 1)
    if d["gpu"] is None and shutil.which("nvidia-smi"):
        rc, out = capture(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"], 20)
        if rc == 0 and out:
            d["gpu"] = out.splitlines()[0].strip()
    return d


def step_device(dev: dict) -> None:
    step(1, "device")
    if dev["kind"] == "jetson":
        result("device", "OK", f"{dev['model']}, {dev['l4t']}, {dev['memory_gb']} GB, Python {dev['python']}")
        if "R36" not in dev["l4t"]:
            result("device", "WARN", "JetPack 6 (L4T R36) was expected: the Jetson torch build is for JetPack 6 / Python 3.10")
        if dev["memory_gb"] and dev["memory_gb"] < 12:
            result("device", "WARN", f"{dev['memory_gb']} GB shared memory: two YOLO models plus two AI models may not fit "
                   "(not measured). If the flight runs out of memory, start it with --ai-off or the flight option --no-vlm")
    else:
        result("device", "OK", f"{dev['os']} {dev['machine']}, Python {dev['python']}, GPU: {dev['gpu'] or 'none (CPU)'}")
    if sys.version_info < (3, 10):
        result("device", "FAIL", "Python 3.10 or newer is needed")
        raise SystemExit(1)


# ---------------------------------------------------------------------------------------------- 2. python
def venv_ok(code: str) -> bool:
    return VENV_PY.exists() and capture([VENV_PY, "-c", code], 300)[0] == 0


def install_profile(dev: dict, station: bool = False) -> dict:
    if station:                                   # receive, show, edit the route: no detection, no AI
        req, torch = SYSTEM / "requirements-station.txt", "none"
    else:
        req = SYSTEM / ("requirements-jetson.txt" if dev["kind"] == "jetson" else "requirements.txt")
        torch = "jetson" if dev["kind"] == "jetson" else ("cuda" if dev["gpu"] else "cpu")
    return {"requirements": req.name, "requirements_sha256": sha256(req), "torch": torch,
            "python": f"{sys.version_info[0]}.{sys.version_info[1]}"}


def step_python(dev: dict, install: bool, station: bool = False) -> None:
    step(2, "python environment (system/.venv)")
    profile = install_profile(dev, station)
    imports = STATION_IMPORTS if station else FLIGHT_IMPORTS
    try:
        done = json.loads(MARKER.read_text(encoding="utf-8")) if MARKER.exists() else None
    except (OSError, ValueError):
        done = None
    if done == profile and venv_ok(imports):
        result("python", "OK", f"ready ({profile['requirements']}, torch {profile['torch']}); nothing to install")
        return
    if not install:
        level = "OK" if venv_ok(imports) else "FAIL"
        result("python", level, "libraries import" if level == "OK" else
               "libraries missing and --no-install was given: run the launcher once without --no-install")
        if level == "FAIL":
            raise SystemExit(1)
        return

    if dev["kind"] == "jetson" and not station:
        missing = [p for p in JETSON_APT if capture(["dpkg", "-s", p], 30)[0] != 0]
        if missing:
            say(f"  apt packages missing: {missing} (sudo asks for your password)")
            run(["sudo", "apt-get", "update"])
            run(["sudo", "apt-get", "install", "-y", *missing])
    if not VENV_PY.exists():
        run([sys.executable, "-m", "venv", VENV])
    run([VENV_PY, "-m", "pip", "install", "-q", "-U", "pip", "wheel"])
    req = SYSTEM / profile["requirements"]
    if profile["torch"] == "cuda" and not venv_ok("import torch; assert torch.cuda.is_available()"):
        run([VENV_PY, "-m", "pip", "install", *TORCH_CUDA_PC, "--index-url", TORCH_CUDA_INDEX])
    run([VENV_PY, "-m", "pip", "install", "-r", req])
    if profile["torch"] == "jetson" and not venv_ok("import torch; assert torch.cuda.is_available()"):
        # requirements pulled the PyPI torch (no CUDA on aarch64): replace it with the Jetson builds
        run([VENV_PY, "-m", "pip", "install", "--force-reinstall", "--no-deps", *TORCH_JETSON])
    if not venv_ok(imports):
        result("python", "FAIL", f"installed, but these still do not import: {imports[7:]}")
        raise SystemExit(1)
    MARKER.write_text(json.dumps(profile, indent=1), encoding="utf-8")
    result("python", "OK", f"installed from {profile['requirements']} (torch {profile['torch']})")


# ---------------------------------------------------------------------------------------------- stage 2: in the venv
def stage2(args: argparse.Namespace, dev: dict) -> int:
    sys.path.insert(0, str(SYSTEM))
    os.chdir(SYSTEM)
    from hima.config import load_config
    cfg = load_config()
    if args.station:
        return stage2_station(cfg, args)
    if args.links:
        return stage2_links(cfg, dev, args)

    step_weights(cfg, args.install)
    step_ai(cfg, dev, args)
    cam = step_camera(cfg, args.camera)
    link = step_pixhawk(cfg, dev, args)
    step_station(cfg)

    if args.tests and not (SYSTEM / "tests").is_dir():
        step(0, "tests")
        result("tests", "WARN", "this copy has no tests folder (the tests are not part of this copy)")
    elif args.tests:
        step(0, "tests")
        rc = run([sys.executable, "-m", "pytest", "-q", "-m", "not ollama"], check=False)
        result("tests", "OK" if rc == 0 else "FAIL", "all passed" if rc == 0 else f"pytest exit {rc}: read the output")

    if cam is None or link is None:
        summary()
        say("\nThe system's preflight was not run: fix the FAIL lines above first.")
        return 1
    ai_flags = ["--no-llm", "--no-vlm"] if args.ai_off else []
    common = ["--mavlink", link[0], "--baud", str(link[1]), "--camera", cam, *ai_flags]
    if args.fly:
        summary()
        step(9, "fly (waits for ARM; Ctrl+C lands cleanly)")
        return run([sys.executable, "-m", "hima", "fly", *common], check=False)
    step(8, "preflight (the system's own check)")
    rc = run([sys.executable, "-m", "hima", "preflight", *common], check=False)
    result("preflight", "OK" if rc == 0 else "FAIL", "all checks passed" if rc == 0 else "a check FAILed (lines above)")
    summary()
    if rc == 0:
        py = ".venv\\Scripts\\python" if WINDOWS else ".venv/bin/python"
        say("\nReady. To start the flight program with what was found:\n"
            f"  {py} -m hima fly {' '.join(common)}\n"
            "or run this launcher with --fly.")
    return rc


# ---------------------------------------------------------------------------------------------- 3. weights
def step_weights(cfg, install: bool) -> None:
    step(3, "model weights")
    if not ANIMAL_WEIGHTS.exists():
        result("weights", "FAIL", f"{ANIMAL_WEIGHTS.name} is missing: copy it into {SYSTEM} (it is not in git; README section 3)")
    else:
        got = sha256(ANIMAL_WEIGHTS)
        result("weights", "OK" if got == ANIMAL_SHA256 else "WARN",
               f"{ANIMAL_WEIGHTS.name} SHA-256 matches" if got == ANIMAL_SHA256 else
               f"{ANIMAL_WEIGHTS.name} SHA-256 is {got[:16]}..., README says {ANIMAL_SHA256[:16]}...: a different file?")
    general = (SYSTEM / cfg.objects.weights) if not Path(cfg.objects.weights).is_absolute() else Path(cfg.objects.weights)
    if general.exists():
        result("weights", "OK", f"{general.relative_to(SYSTEM)} present ({general.stat().st_size:,} bytes)")
    elif install:
        # the general COCO model is public: Ultralytics downloads it from its own release assets
        from ultralytics.utils.downloads import attempt_download_asset
        general.parent.mkdir(parents=True, exist_ok=True)
        got = Path(attempt_download_asset(general))
        if got.exists():
            result("weights", "OK", f"downloaded {got.name} from the Ultralytics assets: {got.stat().st_size:,} bytes, "
                   f"SHA-256 {sha256(got)}")
        else:
            result("weights", "FAIL", f"could not download {general.name}: copy it to {general}")
    else:
        result("weights", "FAIL", f"{general} is missing (--no-install: not downloaded)")


# ---------------------------------------------------------------------------------------------- 4. ai (Ollama)
def ollama_exe() -> str | None:
    exe = shutil.which("ollama")
    if exe:
        return exe
    if WINDOWS:
        p = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
        if p.exists():
            return str(p)
    return None


def ollama_tags(base: str) -> set[str] | None:
    try:
        with urllib.request.urlopen(base + "/api/tags", timeout=4) as r:
            return {m["name"] for m in json.loads(r.read()).get("models", [])}
    except Exception:                                   # noqa: BLE001 - not running / not reachable
        return None


def start_ollama(exe: str, base: str) -> bool:
    if not WINDOWS and shutil.which("systemctl") and capture(["systemctl", "cat", "ollama"], 10)[0] == 0:
        if sys.stdin.isatty():                          # someone at the keyboard can type the sudo password
            run(["sudo", "systemctl", "start", "ollama"], check=False)
        # at boot (the hima-fly service, no terminal) systemd starts ollama.service itself: just wait for it
    else:
        log = open(SYSTEM / "flights" / "ollama_serve.log", "ab") if (SYSTEM / "flights").is_dir() else subprocess.DEVNULL
        flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP) if WINDOWS else 0
        say(f"  $ {exe} serve   (in the background)")
        subprocess.Popen([exe, "serve"], stdout=log, stderr=log, stdin=subprocess.DEVNULL, creationflags=flags,
                         start_new_session=not WINDOWS)
    for _ in range(30):
        if ollama_tags(base) is not None:
            return True
        time.sleep(1)
    return False


def step_ai(cfg, dev: dict, args) -> None:
    step(4, "AI (Ollama)")
    if args.ai_off:
        result("ai", "OK", "off (--ai-off): the flight runs with the AI off; alerts are rules and still work")
        return
    from hima.onboard.ai import flight_models
    need = list(dict.fromkeys(flight_models(cfg, True, True) + [cfg.report.model, cfg.report.judge_model]))
    base = cfg.llm.url.rstrip("/")
    base = base[: base.index("/api")] if "/api" in base else base
    exe = ollama_exe()
    if exe is None and args.install:
        if WINDOWS and shutil.which("winget"):
            run(["winget", "install", "-e", "--id", "Ollama.Ollama", "--accept-source-agreements",
                 "--accept-package-agreements"], check=False)
        elif not WINDOWS:
            run(["sh", "-c", "curl -fsSL https://ollama.com/install.sh | sh"], check=False)   # Ollama's official script
        exe = ollama_exe()
    if exe is None:
        result("ai", "WARN", "Ollama is not installed (https://ollama.com/download): the flight runs with the AI off")
        return
    have = ollama_tags(base)
    if have is None:
        if not start_ollama(exe, base):
            result("ai", "WARN", f"Ollama did not answer at {base}: the flight runs with the AI off")
            return
        have = ollama_tags(base) or set()
    missing = [m for m in need if m not in have and f"{m}:latest" not in have]
    if missing and args.install:
        for m in missing:
            run([exe, "pull", m], check=False)
        have = ollama_tags(base) or set()
        missing = [m for m in need if m not in have and f"{m}:latest" not in have]
    if missing:
        result("ai", "WARN", f"models not pulled: {missing}; those parts fall back to fixed text")
    else:
        result("ai", "OK", f"Ollama running at {base} with {need}")


# ---------------------------------------------------------------------------------------------- 5. camera
def camera_candidates(configured: str) -> list[tuple[str, str]]:
    out = [(configured, "hima.toml [camera].device")]
    if WINDOWS:
        out += [(str(i), f"camera index {i}") for i in range(5)]
    else:
        for dev in sorted(Path("/dev").glob("video*")):
            name_file = Path("/sys/class/video4linux") / dev.name / "name"
            name = name_file.read_text().strip() if name_file.exists() else dev.name
            out.append((str(dev), name))
    seen, uniq = set(), []
    for d, why in out:
        if d not in seen:
            seen.add(d)
            uniq.append((d, why))
    return uniq


def try_camera(cfg, device: str) -> tuple[bool, str]:
    from hima.sources.camera import Camera, CameraError
    try:
        cam = Camera(device, cfg.camera.width_px, cfg.camera.height_px, open_timeout_s=5.0)
    except CameraError as e:
        return False, str(e)
    try:
        t0 = time.monotonic()
        got = None
        while got is None and time.monotonic() - t0 < 5:
            got = cam.latest(0)
            time.sleep(0.05)
        if got is None:
            return False, "opened, but no frame in 5 s"
        h, w = got[2].shape[:2]
        size = f"{w}x{h}"
        want = f"{cfg.camera.width_px}x{cfg.camera.height_px}"
        return True, size + ("" if size == want or cam.is_file else f" (hima.toml asks {want})")
    finally:
        cam.close()


def step_camera(cfg, forced: str | None) -> str | None:
    step(5, "camera")
    cands = [(forced, "--camera")] if forced else camera_candidates(str(cfg.camera.device))
    for device, why in cands:
        ok, detail = try_camera(cfg, device)
        say(f"  {device:<14} {why}: {'frame ' + detail if ok else detail}")
        if ok:
            level = "OK" if device == str(cfg.camera.device) or forced else "WARN"
            result("camera", level, f"{device} gives frames, {detail}" + ("" if level == "OK" else
                   f"; hima.toml says {cfg.camera.device!r}, so this one is passed with --camera"))
            return device
    result("camera", "FAIL", "no camera gave a frame: check the cable (USB 3), and on the Jetson: v4l2-ctl --list-devices")
    return None


# ---------------------------------------------------------------------------------------------- 6. pixhawk
def port_candidates(cfg, dev: dict) -> list[tuple[str, list[int], str]]:
    """(port, bauds to try, why) in the order to try them. TELEM2 first: it is how the drone is wired."""
    telem_bauds = list(dict.fromkeys([cfg.mavlink.baud, 921600, 115200, 57600]))
    out: list[tuple[str, list[int], str]] = []
    url = str(cfg.mavlink.url)
    if not WINDOWS and url.startswith("/dev/") and Path(url).exists():
        out.append((url, telem_bauds, "hima.toml [mavlink].url (TELEM2)"))
    if not WINDOWS:
        for p in sorted(Path("/dev").glob("ttyTHS*")):
            out.append((str(p), telem_bauds, "Jetson UART (40-pin header, pins 8/10)"))
    try:
        from serial.tools import list_ports
        ports = list(list_ports.comports())
    except ImportError:
        ports = []
    for p in ports:
        text = f"{p.description} {p.manufacturer or ''} {p.product or ''}".lower()
        if p.vid in AUTOPILOT_VIDS or any(w in text for w in AUTOPILOT_WORDS):
            out.append((p.device, [115200], f"autopilot USB port ({AUTOPILOT_VIDS.get(p.vid, p.description)})"))
        elif p.vid in ADAPTER_VIDS:
            out.append((p.device, telem_bauds, f"USB-TTL adapter ({ADAPTER_VIDS[p.vid]}): TELEM2 wire"))
        elif p.vid is not None:
            out.append((p.device, telem_bauds, f"USB serial ({p.description})"))
    seen, uniq = set(), []
    for c in out:
        if c[0] not in seen:
            seen.add(c[0])
            uniq.append(c)
    return uniq


def probe(port: str, baud: int, wait_s: float = 3.0) -> tuple[bool, str]:
    os.environ.setdefault("MAVLINK20", "1")
    from pymavlink import mavutil
    try:
        conn = mavutil.mavlink_connection(port, baud=baud, source_system=1, source_component=191, autoreconnect=False)
    except Exception as e:                              # noqa: BLE001 - busy port, no permission, gone
        text = str(e)
        if "denied" in text.lower() or "permission" in text.lower():
            hint = (" (in use by another program, e.g. Mission Planner: close it)" if WINDOWS else
                    " (no permission: sudo usermod -aG dialout $USER, then log out and in)")
            return False, f"cannot open: {text}{hint}"
        return False, f"cannot open: {text}"
    try:
        t_end = time.monotonic() + wait_s
        while time.monotonic() < t_end:
            m = conn.recv_match(type="HEARTBEAT", blocking=True, timeout=0.5)
            if m is None:
                continue
            if m.autopilot == mavutil.mavlink.MAV_AUTOPILOT_INVALID or m.type == mavutil.mavlink.MAV_TYPE_GCS:
                continue                                    # a ground station or a companion, not the autopilot
            firmware = {3: "ArduPilot", 12: "PX4"}.get(m.autopilot, f"autopilot {m.autopilot}")
            return True, f"{firmware}, system {m.get_srcSystem()}"
        return False, "no autopilot HEARTBEAT"
    finally:
        conn.close()


def step_pixhawk(cfg, dev: dict, args) -> tuple[str, int] | None:
    step(6, "Pixhawk (autopilot link)")
    if args.mavlink:
        bauds = [args.baud] if args.baud else ([cfg.mavlink.baud] if not args.mavlink.startswith(("tcp", "udp")) else [0])
        cands = [(args.mavlink, bauds, "--mavlink")]
    else:
        cands = port_candidates(cfg, dev)
    if not cands:
        try:
            from serial.tools import list_ports
            builtin = [p.device for p in list_ports.comports() if p.vid is None]
        except ImportError:
            builtin = []
        result("pixhawk", "FAIL", "no USB serial port and no Jetson UART found" +
               (f" ({', '.join(builtin)}: built-in port, not tried)" if builtin else "") +
               ": plug the Pixhawk (USB) or the TELEM2 adapter in, then run again")
        return None
    for port, bauds, why in cands:
        for b in bauds:
            ok, detail = probe(port, b or 115200, wait_s=4.0 if port.startswith(("tcp", "udp")) else 3.0)
            say(f"  {port:<14} {('@ ' + str(b)) if b else '':<9} {why}: {detail}")
            if ok:
                if "USB port" in why:
                    note = "; this is the USB port: on the drone the system uses TELEM2"
                else:
                    note = ""
                result("pixhawk", "OK", f"{detail} on {port}" + (f" at {b} baud" if b else "") + note)
                return port, (b or cfg.mavlink.baud)
            if detail.startswith("cannot open"):
                break                                       # the port itself failed: other bauds will too
    result("pixhawk", "FAIL", "no autopilot answered. TELEM2: TX to RX, RX to TX, GND to GND (no 5V), "
           "SERIAL2_PROTOCOL = 2, SERIAL2_BAUD = 921, then reboot the Pixhawk. USB: close Mission Planner")
    return None


# ---------------------------------------------------------------------------------------------- 7. station (drone side)
def step_station(cfg) -> None:
    step(7, "ground station (Wi-Fi delivery of the package after landing)")
    from hima.contract.delivery import DeliveryKeyError, fingerprint, load_key
    from hima.onboard.deliver import config_path, ping
    try:
        key = load_key(config_path(cfg, cfg.delivery.key_file))
    except DeliveryKeyError as e:
        result("station", "WARN", f"delivery not set up: {e}. Until then the packages stay on the drone")
        return
    ok, detail = ping(cfg, key)
    if ok:
        result("station", "OK", f"{detail}, same key (fingerprint {fingerprint(key)})")
    else:
        result("station", "WARN", f"not reachable now: {detail}. The flight is not affected: after landing the "
               "package waits on the drone and goes as soon as the station answers")


# ---------------------------------------------------------------------------------------------- --links
def stage2_links(cfg, dev: dict, args) -> int:
    """The link tests (launcher/link_tests.py), with this launcher's list of serial ports."""
    sys.path.insert(0, str(HERE))
    import link_tests
    return link_tests.main_launcher(cfg, dev, args, system=SYSTEM, candidates=port_candidates(cfg, dev), say=say)


# ---------------------------------------------------------------------------------------------- --station
def stage2_station(cfg, args) -> int:
    from hima.contract.delivery import DeliveryKeyError, fingerprint, load_key, parse_listen, write_key
    from hima.ground.receiver import local_ipv4, serve
    from hima.onboard.deliver import config_path

    step(3, "delivery key (the same file on the drone and here)")
    kp = config_path(cfg, cfg.delivery.key_file)
    if kp.exists():
        try:
            key = load_key(kp)
        except DeliveryKeyError as e:
            result("key", "FAIL", str(e))
            summary()
            return 1
        result("key", "OK", f"{kp}, fingerprint {fingerprint(key)} (the drone must show the same)")
    else:
        key = write_key(kp)
        result("key", "OK", f"made a new key: {kp}, fingerprint {fingerprint(key)}. Copy this file to the drone, to "
               f"system/{cfg.delivery.key_file} (USB stick or cable; never git, chat or e-mail)")

    step(4, "network")
    host, port = parse_listen(args.listen or cfg.delivery.listen)
    local_only = host.startswith("127.") or host == "localhost"
    ips = local_ipv4()
    if local_only:
        result("network", "WARN", f"listening on {host} only: the drone cannot reach it (bench test)")
    elif ips:
        result("network", "OK", f"this PC: {', '.join(ips)}; on the drone set [delivery].station_url = "
               f"\"http://{ips[0]}:{port}\" (the base network address; keep it fixed with a DHCP reservation)")
    else:
        result("network", "WARN", "no network address found: connect this PC to the base router")
    if WINDOWS and not local_only:
        say("  Windows asks once whether Python may accept connections: choose Allow, on Private networks.\n"
            "  The base Wi-Fi must be a Private network (Settings > Network > the network > Private). If the drone\n"
            "  still cannot connect, allow the port once in an administrator PowerShell:\n"
            f"    New-NetFirewallRule -DisplayName 'HIMA receiver' -Direction Inbound -Protocol TCP -LocalPort {port} "
            "-Action Allow -Profile Private")

    archive, inbox = config_path(cfg, cfg.delivery.archive_dir), config_path(cfg, cfg.delivery.inbox_dir)
    archive.mkdir(parents=True, exist_ok=True)
    try:                                                  # the virtual flight: the dashboard never opens empty
        from hima.ground.demo import ensure_demo
        d = ensure_demo(cfg, archive, config_path(cfg, cfg.route_review.plans_dir), log=lambda s: None)
        ok = str(d["flight"]).startswith(("ingested", "already", "off"))
        result("demo", "OK" if ok else "WARN", f"virtual flight {d['flight']}; more virtual flights {d.get('more')}; "
               f"route {d['route']}")
    except Exception as e:                                # noqa: BLE001 - never stops the station
        result("demo", "WARN", f"virtual flight not added: {type(e).__name__}: {e}")
    step(5, "dashboard")
    if args.no_dashboard:
        result("dashboard", "OK", "not opened (--no-dashboard)")
    else:
        url = start_dashboard(archive, inbox)
        result("dashboard", "OK" if url else "WARN", f"open at {url} (a new mission shows by itself)" if url else
               f"the dashboard did not start: see {inbox / 'dashboard.log'}")
    summary()
    step(6, "receiver (Ctrl+C or closing this window stops it)")
    serve(key, host, port, inbox, archive, log=say)
    return 0


def port_free(port: int) -> bool:
    """Nothing answers on it and it can be bound (another program on 8501 must not pass for our dashboard)."""
    import socket
    with socket.socket() as s:
        s.settimeout(0.3)
        if s.connect_ex(("127.0.0.1", port)) == 0:
            return False
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def start_dashboard(archive: Path, inbox: Path) -> str | None:
    import webbrowser
    inbox.mkdir(parents=True, exist_ok=True)
    port = next((p for p in range(8501, 8521) if port_free(p)), 8501)
    cmd = [sys.executable, "-m", "hima", "dashboard", "--archive", str(archive), "--port", str(port)]
    if os.environ.get("HIMA_CONFIG"):
        cmd += ["--config", os.environ["HIMA_CONFIG"]]
    say("  $ " + " ".join(cmd) + "   (in the background)")
    with open(inbox / "dashboard.log", "ab") as log:     # the child keeps its own copy of the handle
        proc = subprocess.Popen(cmd, stdout=log, stderr=log, stdin=subprocess.DEVNULL, cwd=str(SYSTEM))
    url = f"http://localhost:{port}"
    for _ in range(40):
        if proc.poll() is not None:                     # it stopped (read dashboard.log)
            return None
        try:
            with urllib.request.urlopen(url, timeout=2):
                webbrowser.open(url)
                return url
        except Exception:                               # noqa: BLE001 - still starting
            time.sleep(0.5)
    return None


# ---------------------------------------------------------------------------------------------- service, summary
def install_service() -> None:
    if WINDOWS or not shutil.which("systemctl"):
        result("service", "WARN", "the hima-fly and hima-deliver services are for the Jetson (Linux with systemd)")
        return
    user = os.environ.get("SUDO_USER") or os.environ.get("USER") or "root"
    for name in ("hima-fly", "hima-deliver"):
        unit = (SYSTEM / "deploy" / f"{name}.service").read_text(encoding="utf-8")
        unit = unit.replace("@USER@", user).replace("@DIR@", str(SYSTEM))
        tmp = HERE / f"{name}.service.filled"
        tmp.write_text(unit, encoding="utf-8")
        run(["sudo", "cp", tmp, f"/etc/systemd/system/{name}.service"])
        tmp.unlink()
    run(["sudo", "systemctl", "daemon-reload"])
    if capture(["id", "-nG", user], 10)[1].split().count("dialout") == 0:
        run(["sudo", "usermod", "-aG", "dialout", user], check=False)
        say("  added to the dialout group (serial ports): log out and in once")
    result("service", "OK", "hima-fly and hima-deliver installed, NOT enabled. After a bench test: "
           "sudo systemctl enable --now hima-fly hima-deliver")


def summary() -> None:
    say("\n== summary")
    for name, level, detail in RESULTS:
        say(f"  [{level:<4}] {name:<10} {detail}")


def main() -> int:
    ap = argparse.ArgumentParser(description="HIMA launcher: prepare this computer and check everything before a flight")
    ap.add_argument("--no-install", dest="install", action="store_false", help="check only, install nothing")
    ap.add_argument("--fly", action="store_true", help="after the checks, start the flight program")
    ap.add_argument("--ai-off", action="store_true", help="no Ollama: the flight runs with the AI off")
    ap.add_argument("--camera", help="camera to use (0, /dev/video0, or a video file)")
    ap.add_argument("--mavlink", help="link to use (COM5, /dev/ttyTHS1, tcp:127.0.0.1:5763)")
    ap.add_argument("--baud", type=int, help="baud rate with --mavlink on a serial port")
    ap.add_argument("--tests", action="store_true", help="also run the test suite")
    ap.add_argument("--install-service", action="store_true", help="Jetson: install the hima-fly and hima-deliver "
                    "boot services")
    ap.add_argument("--station", action="store_true", help="ground station PC: receive the drone's packages over "
                    "Wi-Fi and open the dashboard")
    ap.add_argument("--listen", help="with --station: HOST:PORT for the receiver (default [delivery].listen)")
    ap.add_argument("--no-dashboard", action="store_true", help="with --station: do not open the dashboard")
    lk = ap.add_argument_group("link tests (--links; launcher/test_links.bat, launcher/test_links.sh)")
    lk.add_argument("--links", action="store_true", help="test every link to the Pixhawk (USB, TELEM2, radio), the "
                    "camera and the station; report in runs/link_tests/")
    lk.add_argument("--ground", help="the ground telemetry radio's port (found by itself otherwise)")
    lk.add_argument("--ground-baud", type=int, help="baud rate with --ground (default: 57600, then 115200)")
    lk.add_argument("--no-hands", action="store_true", help="skip the tests that need someone at the drone")
    lk.add_argument("--window", type=float, default=60.0, help="seconds of the link measurement (default 60)")
    lk.add_argument("--gps-wait", type=float, default=120.0, help="longest wait for the GPS fix (default 120)")
    lk.add_argument("--no-camera", action="store_true", help="skip the camera test")
    lk.add_argument("--no-station", action="store_true", help="skip the ground station test")
    lk.add_argument("--no-ui", action="store_true", help="no page in the browser (console only)")
    lk.add_argument("--no-preflight", action="store_true", help="skip test 14, the flight program's own checks "
                    "before take-off (python -m hima preflight)")
    lk.add_argument("--compare", nargs="?", const="latest", metavar="FILE",
                    help="compare with an earlier report .json (default: the newest in runs/link_tests/)")
    ap.add_argument("--inside-venv", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if not args.inside_venv:
        say(f"HIMA launcher in {SYSTEM}")
        dev = detect_device()
        step_device(dev)
        step_python(dev, args.install, args.station)
        if args.install_service:
            install_service()
        env = dict(os.environ, HIMA_LAUNCHER_DEVICE=json.dumps(dev),
                   HIMA_LAUNCHER_RESULTS=json.dumps(RESULTS))
        return subprocess.call([str(VENV_PY), str(Path(__file__).resolve()), *sys.argv[1:], "--inside-venv"],
                               cwd=str(SYSTEM), env=env)
    dev = json.loads(os.environ.get("HIMA_LAUNCHER_DEVICE", "{}")) or detect_device()
    RESULTS.extend(tuple(r) for r in json.loads(os.environ.get("HIMA_LAUNCHER_RESULTS", "[]")))
    return stage2(args, dev)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        say("\nstopped (Ctrl+C)")
        sys.exit(130)
