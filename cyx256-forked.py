#!/usr/bin/env python3
"""CYX Prompt Auto - interactive XMRig/RandomX controller.

Interactive controller for a locally installed XMRig executable. It manages a
small JSON config, launches/stops XMRig, reports status, and can generate
RandomX test jobs through the existing cyx-randomx-jobs-nanswap.py module.

It does not download software, alter system startup, or run hidden/background
persistence. Mining starts only after an explicit `start` command.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "cyx-xmrig-config.json"
STATE_PATH = APP_DIR / "cyx-prompt-state.json"
DEFAULT_POOL = "xmrig.nanswap.com:3333"
DEFAULT_ALGO = "rx/0"

# Accepted controller-level algorithm names. These are passed to XMRig as
# --algo values; availability ultimately depends on the installed XMRig build.
ALGO_ALIASES = {
    "rx/0": "rx/0",
    "randomx": "rx/0",
    "randomx/0": "rx/0",
    "scrypt": "scrypt",
    "x11": "x11",
    "x11blake3": "x11blake3",
    "blake3": "blake3",
    "x11-blake3": "x11blake3",
    "sha256d": "sha256d",
    "sha-256d": "sha256d",
    "cryptonight": "cn/r",
    "crypto-night": "cn/r",
    "crypto night": "cn/r",
    "cn": "cn/r",
    "cn/r": "cn/r",
    "argon": "argon2",
    "argon2": "argon2",
    "argon2/ninja": "argon2/ninja",
    "ninja": "ninja",
    "minotaurx": "minotaurx",
}

# Algorithms documented by current official XMRig. Names outside this set may
# still work with a custom/forked XMRig build, but are not stock-XMRig names.
XMRIG_STOCK_ALGOS = {
    "rx/0", "argon2/ninja", "argon2/chukwa", "argon2/chukwav2",
    "cn/r", "cn/0", "cn/1", "cn/2", "cn/fast", "cn/rwz", "cn/zls",
    "cn/double", "cn/half", "cn/xao", "cn/rto", "cn/ccx", "cn/upx2",
    "cn-pico", "cn-pico/tlo", "cn-heavy/0", "cn-heavy/tube",
    "cn-heavy/xhv", "cn-lite/0", "cn-lite/1", "gr", "kawpow",
    "rx/arq", "rx/keva", "rx/sfx", "rx/wow", "rx/graft",
}

DEFAULT_PASS = "x"
CYTHANX_MODULE_NAME = "cyx_pool_genesis_runtime"


def locate_cythanx_source() -> Path | None:
    """Find the CYTHANX source without assuming the controller's directory."""
    candidates = [
        APP_DIR / "cyx-pool-genesis.py",
        Path.cwd() / "cyx-pool-genesis.py",
        APP_DIR.parent / "cyx-pool-genesis.py",
    ]
    seen: set[Path] = set()
    for candidate in candidates:
        candidate = candidate.resolve()
        if candidate in seen:
            continue
        seen.add(candidate)
        if candidate.is_file():
            return candidate
    return None


DEFAULT_CONFIG: dict[str, Any] = {
    "autosave": True,
    "cpu": True,
    "opencl": False,
    "cuda": False,
    "threads": 0,
    "pools": [{
        "algo": DEFAULT_ALGO,
        "coin": "monero",
        "url": DEFAULT_POOL,
        "user": "",
        "pass": DEFAULT_PASS,
        "keepalive": True,
        "tls": False,
    }],
}


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return json.loads(json.dumps(default))
    try:
        with path.open("r", encoding="utf-8") as f:
            value = json.load(f)
        if not isinstance(value, dict):
            raise ValueError("JSON root must be an object")
        return value
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"Config warning: {exc}; using defaults.")
        return json.loads(json.dumps(default))


def save_json(path: Path, value: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(value, f, indent=2)
        f.write("\n")
    tmp.replace(path)


def pool(cfg: dict[str, Any]) -> dict[str, Any]:
    pools = cfg.setdefault("pools", [])
    if not pools:
        pools.append(dict(DEFAULT_CONFIG["pools"][0]))
    return pools[0]


def find_xmrig(explicit: str | None = None) -> str | None:
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    candidates.extend([
        APP_DIR / "xmrig",
        APP_DIR / "xmrig.exe",
        Path.cwd() / "xmrig",
        Path.cwd() / "xmrig.exe",
    ])
    for p in candidates:
        if p.is_file():
            return str(p)
    import shutil
    return shutil.which("xmrig") or shutil.which("xmrig.exe")


def process_alive(pid: int | None) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def load_saved_pid() -> int | None:
    """Recover a controller-owned XMRig PID when the controller is restarted."""
    state = load_json(STATE_PATH, {"pid": None})
    pid = state.get("pid")
    if isinstance(pid, int) and process_alive(pid):
        return pid
    return None


def status(cfg: dict[str, Any], proc: subprocess.Popen | None) -> None:
    p = pool(cfg)
    running = bool(proc and proc.poll() is None)
    pid = proc.pid if running and proc else None
    print(f"\nXMRig: {'RUNNING' if running else 'STOPPED'}" + (f" (PID {pid})" if pid else ""))
    print(f"Pool:  {p.get('url', '')}")
    print(f"Algo:  {p.get('algo', '')}")
    print(f"User:  {'set' if p.get('user') else 'NOT SET'}")
    print(f"TLS:   {bool(p.get('tls', False))}")
    print(f"Threads: {cfg.get('threads', 0) or "auto"}")
    print(f"Config: {CONFIG_PATH}")


def validate(cfg: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    try:
        p = pool(cfg)
    except Exception as exc:
        return [f"invalid pools configuration: {exc}"]
    for key in ("url", "user", "pass"):
        if not isinstance(p.get(key), str) or not p.get(key, "").strip():
            errors.append(f"pool.{key} is empty")
    algo = str(p.get("algo", "")).strip().lower()
    if algo not in set(ALGO_ALIASES.values()):
        errors.append("pool.algo must be one of: rx/0, x11, scrypt, x11blake3, sha256d, blake3, cn/r, argon2, argon2/ninja, ninja, minotaurx")
    url = str(p.get("url", "")).strip()
    if url and not re.match(r"^(?:[a-zA-Z][a-zA-Z0-9+.-]*://)?[^\s:]+:\d{1,5}$", url):
        errors.append("pool.url must be HOST:PORT or scheme://HOST:PORT")
    threads = cfg.get("threads", 0)
    try:
        threads = int(threads)
        if threads < 0 or threads > 256:
            errors.append("threads must be 0 (auto) or an integer from 1-256")
    except (TypeError, ValueError):
        errors.append("threads must be 0 (auto) or an integer from 1-256")
    o = cfg.get("organism", {})
    if o:
        command = str(o.get("command", "status")).strip().lower()
        allowed = {"status", "info", "show", "vector", "cythan", "cythanize", "transform", "transmogrify", "joatt", "genesis", "job"}
        if command not in allowed:
            errors.append(f"organism.command is unsupported: {command}")
        payload = o.get("payload", "")
        if payload:
            try:
                data = bytes.fromhex(payload[2:]) if str(payload).lower().startswith("0x") else str(payload).encode("utf-8")
                if not (ORGANISM_PAYLOAD_MIN <= len(data) <= ORGANISM_PAYLOAD_MAX):
                    errors.append(f"organism.payload must be {ORGANISM_PAYLOAD_MIN}-{ORGANISM_PAYLOAD_MAX} bytes; got {len(data)}")
            except (ValueError, UnicodeEncodeError) as exc:
                errors.append(f"organism.payload is invalid: {exc}")
        previous = o.get("previous", "")
        if previous:
            try:
                raw = str(previous)[2:] if str(previous).lower().startswith("0x") else str(previous)
                if len(bytes.fromhex(raw)) != 32:
                    errors.append("organism.previous must be exactly 32 bytes")
            except ValueError as exc:
                errors.append(f"organism.previous is invalid hex: {exc}")
    return errors


def save_state(pid: int | None = None) -> None:
    save_json(STATE_PATH, {"pid": pid, "updated": int(time.time())})


def algorithm_diagnostic(name: str, xmrig: str | None) -> None:
    """Explain stock-XMRig compatibility and optionally dry-run the binary."""
    requested = " ".join(name.strip().lower().split())
    canonical = ALGO_ALIASES.get(requested)
    if canonical is None:
        print(f"Unknown controller algorithm: {name}")
        return
    print(f"Requested: {requested}")
    print(f"Canonical: {canonical}")
    if canonical in XMRIG_STOCK_ALGOS:
        print("Stock XMRig: documented")
    else:
        print("Stock XMRig: NOT documented")
        print("This requires a custom/forked miner that implements this algorithm.")
    exe = find_xmrig(xmrig)
    if not exe:
        print("Executable test: XMRig not found")
        return
    print(f"Executable: {exe}")
    try:
        result = subprocess.run(
            [exe, f"--algo={canonical}", "--dry-run", "--no-color"],
            cwd=str(Path(exe).resolve().parent),
            capture_output=True, text=True, timeout=20, check=False,
        )
        output = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()
        if result.returncode == 0:
            print("Binary test: ACCEPTED by this XMRig build")
        else:
            print(f"Binary test: REJECTED (exit {result.returncode})")
        if output:
            lines = output.splitlines()
            print("\n".join(lines[-20:]))
    except subprocess.TimeoutExpired:
        print("Binary test: timed out after 20 seconds")
    except OSError as exc:
        print(f"Binary test failed: {exc}")


def start(cfg: dict[str, Any], proc: subprocess.Popen | None, xmrig: str | None) -> subprocess.Popen | None:
    if proc and proc.poll() is None:
        print("Already running.")
        return proc
    errors = validate(cfg)
    if errors:
        print("Cannot start:")
        for e in errors:
            print(f"  - {e}")
        print("Use `wallet`, `pool`, or `set` to configure it first.")
        return None
    xmrig = find_xmrig(xmrig)
    if not xmrig:
        print("XMRig executable not found. Put xmrig/xmrig.exe beside this script or use `xmrig <path>`.")
        return None
    save_json(CONFIG_PATH, cfg)
    print(f"Starting {xmrig} with {CONFIG_PATH} ...")
    try:
        kwargs: dict[str, Any] = {"cwd": str(Path(xmrig).resolve().parent)}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        cmd = [xmrig, "--config", str(CONFIG_PATH)]
        threads = int(cfg.get("threads", 0) or 0)
        if threads > 0:
            cmd.append(f"--threads={threads}")
        proc = subprocess.Popen(cmd, **kwargs)
        save_state(proc.pid)
        print(f"Started PID {proc.pid}.")
        return proc
    except OSError as exc:
        print(f"Start failed: {exc}")
        return None


def stop(proc: subprocess.Popen | None) -> subprocess.Popen | None:
    """Stop the session process, or the last controller-owned persisted PID."""
    pid = None
    session_proc = proc if proc and proc.poll() is None else None
    if session_proc:
        pid = session_proc.pid
    else:
        pid = load_saved_pid()
    if not pid:
        print("XMRig is not running.")
        save_state(None)
        return None
    print(f"Stopping PID {pid} ...")
    try:
        if session_proc is not None:
            if os.name == "nt":
                session_proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(pid, signal.SIGTERM)
            session_proc.wait(timeout=8)
        else:
            # The controller persisted this PID, so it can recover control
            # after a restart without pretending it has a Popen handle.
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                os.kill(pid, signal.SIGTERM)
                deadline = time.time() + 8
                while process_alive(pid) and time.time() < deadline:
                    time.sleep(0.2)
                if process_alive(pid):
                    os.kill(pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"Graceful stop failed: {exc}")
        try:
            if session_proc is not None:
                session_proc.kill()
            elif process_alive(pid):
                os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    save_state(None)
    print("Stopped.")
    return None


def set_value(cfg: dict[str, Any], key: str, value: str) -> None:
    """Set miner or CYTHANX organism parameters from the prompt.

    Organism parameters are stored under cfg["organism"] so they can be
    reused by later organism commands without altering pool settings.
    """
    p = pool(cfg)
    aliases = {
        "pool": "url", "wallet": "user", "password": "pass",
        "algorithm": "algo", "algo": "algo", "coin": "coin",
        "scrypt": "algo", "x11": "algo", "x11blake3": "algo", "x11-blake3": "algo", "blake3": "algo",
        "sha256d": "algo", "sha-256d": "algo", "cryptonight": "algo",
        "crypto-night": "algo", "crypto night": "algo", "cn": "algo",
        "argon": "algo", "argon2": "algo", "argon2/ninja": "algo", "ninja": "algo",
        "minotaurx": "algo",
        "organism_command": "organism.command",
        "organism_action": "organism.command",
        "organism_payload": "organism.payload",
        "organism_previous": "organism.previous",
        "thread": "threads",
        "cpu_threads": "threads",
    }
    raw_key = key.strip().lower()
    key = aliases.get(raw_key, raw_key)
    if raw_key in ALGO_ALIASES and not value.strip():
        value = raw_key

    if key == "algo":
        requested = " ".join(value.strip().lower().split())
        canonical = ALGO_ALIASES.get(requested)
        if canonical is None:
            print("Unsupported algorithm. Use: rx/0, x11, scrypt, sha256d, blake3, cryptonight, argon2, argon2/ninja, or minotaurx")
            return
        p["algo"] = canonical
        save_json(CONFIG_PATH, cfg)
        print(f"Set algo = {canonical}")
        return

    if key == "threads":
        try:
            n = int(value.strip())
            if n < 0 or n > 256:
                raise ValueError
        except ValueError:
            print("threads must be 0 (auto) or an integer from 1-256")
            return
        cfg["threads"] = n
        save_json(CONFIG_PATH, cfg)
        print(f"Set threads = {n if n else "auto"}")
        return

    if key.startswith("organism."):
        o = cfg.setdefault("organism", {})
        field = key.split(".", 1)[1]
        if field == "pool":
            # Accept either HOST:PORT or a URL-like endpoint. Keep this
            # separate from the XMRig RandomX pool: CYTHANX jobs require the
            # source-defined mining.notify consensus fields.
            endpoint = value.strip()
            if not endpoint:
                print("organism.pool cannot be empty")
                return
            o["pool"] = endpoint
            save_json(CONFIG_PATH, cfg)
            print(f"Set organism.pool = {endpoint}")
            return
        if field not in {"command", "payload", "previous", "pool"}:
            print("Organism parameters: organism.command, organism.payload, organism.previous, organism.pool")
            return
        if field == "payload":
            try:
                data = bytes.fromhex(value[2:]) if value.lower().startswith("0x") else value.encode("utf-8")
            except (ValueError, UnicodeEncodeError) as exc:
                print(f"Invalid organism.payload: {exc}")
                return
            if not (ORGANISM_PAYLOAD_MIN <= len(data) <= ORGANISM_PAYLOAD_MAX):
                print(f"organism.payload must be {ORGANISM_PAYLOAD_MIN}-{ORGANISM_PAYLOAD_MAX} bytes; got {len(data)}")
                return
        elif field == "previous" and value.strip():
            try:
                _bitcoin_address_to_previous(value)
            except ValueError as exc:
                print(f"Invalid organism.previous: {exc}")
                return
        o[field] = value.strip()
        save_json(CONFIG_PATH, cfg)
        print(f"Set {key} = {o[field]}")
        return

    key = aliases.get(key, key)
    if key not in {"url", "user", "pass", "algo", "coin", "tls", "keepalive", "threads"}:
        print("Settable keys: pool, wallet, password, algorithm, coin, tls, keepalive, "
              "threads, organism.command, organism.payload, organism.previous, organism.pool")
        return
    if key in {"tls", "keepalive"}:
        value = value.lower() in {"1", "true", "yes", "on"}
    if key == "coin" and value.strip().lower() in {"", "auto", "none", "null"}:
        p.pop("coin", None)
        save_json(CONFIG_PATH, cfg)
        print("Cleared XMRig coin hint (automatic detection).")
        return
    p[key] = value.strip() if isinstance(value, str) else value
    save_json(CONFIG_PATH, cfg)
    print(f"Set {key} = {p[key]}")


def run_configured_organism(cfg: dict[str, Any]) -> None:
    """Execute the organism action stored by set organism.command."""
    o = cfg.get("organism", {})
    command = str(o.get("command", "status")).strip().lower()
    payload = str(o.get("payload", ""))
    previous = str(o.get("previous", ""))

    # The interactive organism functions remain available for commands that
    # need arbitrary input. Configured payload/previous values are supplied
    # through temporary stdin-free wrappers below.
    if command in {"status", "info", "show"}:
        organism_status()
    elif command in {"vector"}:
        organism_vector()
    elif command in {"genesis", "job"}:
        organism_genesis_job()
    elif command in {"cythanize", "cythan", "transform", "transmogrify", "joatt"}:
        try:
            mod = load_cythanx()
            data = payload.encode("utf-8")
            if payload.lower().startswith("0x"):
                data = bytes.fromhex(payload[2:])
            if not (ORGANISM_PAYLOAD_MIN <= len(data) <= ORGANISM_PAYLOAD_MAX):
                raise ValueError(
                    f"organism.payload must be {ORGANISM_PAYLOAD_MIN}-{ORGANISM_PAYLOAD_MAX} bytes; got {len(data)}"
                )
            prev = _bitcoin_address_to_previous(previous)
            fn = {
                "cythan": mod.cythanize,
                "cythanize": mod.cythanize,
                "transform": mod.transmogrify,
                "transmogrify": mod.transmogrify,
                "joatt": mod.joatt,
            }[command]
            result = fn(data, prev)
            print(json.dumps(result, indent=2))
        except Exception as exc:
            print(f"Configured organism {command} failed: {exc}")
    else:
        print("Unknown organism.command. Use status, vector, cythanize, transform, joatt, or genesis.")



def load_cythanx():
    """Load the existing CYTHANX organism/consensus implementation."""
    source = locate_cythanx_source()
    if source is None:
        raise FileNotFoundError(
            "Missing cyx-pool-genesis.py; place it beside cyx-prompt-auto.py "
            "or in the current working directory."
        )
    from importlib.util import spec_from_file_location, module_from_spec
    spec = spec_from_file_location(CYTHANX_MODULE_NAME, source)
    if not spec or not spec.loader:
        raise RuntimeError("unable to load CYTHANX source")
    mod = module_from_spec(spec)
    sys.modules[CYTHANX_MODULE_NAME] = mod
    spec.loader.exec_module(mod)
    return mod


ORGANISM_PAYLOAD_MAX = 256
ORGANISM_PAYLOAD_MIN = 1


def _bitcoin_address_to_previous(value: str) -> bytes:
    """Derive the 32-byte organism previous state from a Bitcoin mainnet address.

    Bitcoin addresses are not 32-byte hashes themselves, so the controller uses
    SHA-256 of the canonical address text as the deterministic previous state.
    Raw 32-byte hex (0x + 64 hex chars) remains supported.
    """
    raw = value.strip()
    if not raw:
        return b"\x00" * 32
    if raw.lower().startswith("0x"):
        hex_value = raw[2:]
        if len(hex_value) != 64:
            raise ValueError("0x previous state must contain exactly 32 bytes (64 hex characters)")
        try:
            return bytes.fromhex(hex_value)
        except ValueError as exc:
            raise ValueError("invalid previous-state hex") from exc
    if re.fullmatch(r"(?:1|3)[1-9A-HJ-NP-Za-km-z]{25,34}", raw):
        address = raw
    elif re.fullmatch(r"bc1[ac-hj-np-z02-9]{11,71}", raw.lower()):
        address = raw.lower()
    else:
        raise ValueError("expected a Bitcoin mainnet address (1..., 3..., bc1q..., or bc1p...) or 32-byte 0x hex")
    return hashlib.sha256(address.encode("ascii")).digest()

def _organism_payload(prompt: str) -> bytes:
    raw = input(f"{prompt} [1-{ORGANISM_PAYLOAD_MAX} bytes]: ").strip()
    if raw.lower().startswith("0x"):
        data = bytes.fromhex(raw[2:])
    else:
        data = raw.encode("utf-8")
    if not (ORGANISM_PAYLOAD_MIN <= len(data) <= ORGANISM_PAYLOAD_MAX):
        raise ValueError(
            f"organism payload must be {ORGANISM_PAYLOAD_MIN}-{ORGANISM_PAYLOAD_MAX} bytes; got {len(data)}"
        )
    return data


def _organism_previous(prompt: str, size: int = 32) -> bytes:
    if size != 32:
        raise ValueError("organism previous state must be 32 bytes")
    raw = input(f"{prompt} [zero, 32-byte hex, or Bitcoin address]: ").strip()
    return _bitcoin_address_to_previous(raw)


def organism_vector() -> None:
    try:
        mod = load_cythanx()
        vector = mod.cythan_vector()
        print(json.dumps({"vector": vector["vector"].hex(), "joaat": f'{vector["joaat"]:08x}'}, indent=2))
    except Exception as exc:
        print(f"Organism vector failed: {exc}")


def organism_cythanize() -> None:
    try:
        mod = load_cythanx()
        payload = _organism_payload("CYTHAN payload (text or 0xhex): ")
        previous = _organism_previous("Previous state hex")
        print(json.dumps(mod.cythanize(payload, previous), indent=2))
    except Exception as exc:
        print(f"CYTHANIZE failed: {exc}")


def organism_transform() -> None:
    """Run the source-defined CYTHAN transmogrification pipeline."""
    try:
        mod = load_cythanx()
        payload = _organism_payload("Organism payload (text or 0xhex): ")
        previous = _organism_previous("Previous state hex")
        print(json.dumps(mod.transmogrify(payload, previous), indent=2))
    except Exception as exc:
        print(f"Organism transform failed: {exc}")


def organism_joatt() -> None:
    """Run the source-defined JOATT state derivation."""
    try:
        mod = load_cythanx()
        payload = _organism_payload("JOATT payload (text or 0xhex): ")
        previous = _organism_previous("Previous JOATT state hex")
        print(json.dumps(mod.joatt(payload, previous), indent=2))
    except Exception as exc:
        print(f"JOATT derivation failed: {exc}")


def organism_genesis_job() -> None:
    """Inspect/emit the deterministic CYTHANX genesis bootstrap payload.

    The source defines genesis as a fixed height-0 block with timestamp 0 and
    zero parent.  It is not a normal PoW work item: the chain validator only
    applies the target check to blocks after genesis.  Therefore this command
    reports that distinction explicitly and never attempts to mine genesis.
    """
    try:
        mod = load_cythanx()
        result = mod.pool_genesis_payload()
        params = result.get("params", [])
        if len(params) < 9:
            raise ValueError("CYTHANX genesis payload is missing required fields")
        job = mod.CYTHANXJob.from_notify(params)
        block = mod.block_from_cythanx_job(job)
        digest = block.calculate_hash()
        target = mod.parse_hex_int(job.target, "genesis target")
        print(json.dumps({
            "bootstrap_only": True,
            "mineable": False,
            "reason": "CYTHANX genesis is a fixed height-0 bootstrap block; do not submit it as ordinary PoW work.",
            "job_id": job.job_id,
            "height": job.height,
            "previous_hash": job.previous_hash,
            "hash_at_nonce_0": digest,
            "target": job.target,
            "nonce_0_meets_target": int(digest, 16) <= target,
            "notify": result,
        }, indent=2))
    except Exception as exc:
        print(f"Genesis job generation failed: {exc}")


def organism_command(args2: list[str]) -> None:
    """Dispatch the explicit CYTHANX organism command family."""
    sub = args2[0].lower() if args2 else "status"
    aliases = {
        "status": "status", "info": "status", "show": "status",
        "vector": "vector", "cythanize": "cythanize", "cythan": "cythanize",
        "transform": "transform", "transmogrify": "transform",
        "joatt": "joatt", "genesis": "genesis", "job": "genesis",
        "genesis-check": "genesis", "check-genesis": "genesis",
        "help": "help", "list": "help",
    }
    action = aliases.get(sub)
    if action == "status":
        organism_status()
    elif action == "vector":
        organism_vector()
    elif action == "cythanize":
        organism_cythanize()
    elif action == "transform":
        organism_transform()
    elif action == "joatt":
        organism_joatt()
    elif action == "genesis":
        organism_genesis_job()
    elif action == "help":
        print("Organism commands: status, vector, cythanize, transform, joatt, genesis")
    else:
        print("Organism commands: status, vector, cythanize, transform, joatt, genesis")


def organism_status() -> None:
    """Show the CYTHANX organism primitives and current local chain state."""
    try:
        mod = load_cythanx()
        vector = mod.cythan_vector()
        print("\nCYTHANX ORGANISM")
        print(f"Network:   {getattr(mod, 'NETWORK', 'unknown')}")
        print(f"Chain ID:  {getattr(mod, 'CHAIN_ID', 'unknown')}")
        print(f"Vector:    {vector['vector'].hex()}")
        print(f"JOAAT:     {vector['joaat']:08x}")
        print(f"Protocol:  {getattr(mod, 'POOL_PROTOCOL', 'unknown')}")
        chain_cls = getattr(mod, 'CythanMainnet', None)
        if chain_cls:
            chain = chain_cls()
            print(f"Height:    {len(chain.chain) - 1}")
            print(f"Tip:       {chain.latest.hash}")
            print(f"Target:    {chain.latest.target}")
    except Exception as exc:
        print(f"Organism status failed: {exc}")

def generate_job() -> None:
    """Generate a synthetic RandomX test job without implying it is live pool work."""
    try:
        from importlib.util import spec_from_file_location, module_from_spec
        source = APP_DIR / "cyx-randomx-jobs-nanswap.py"
        if not source.exists():
            print(f"Missing {source}")
            return
        spec = spec_from_file_location("cyx_randomx_jobs_nanswap", source)
        if not spec or not spec.loader:
            raise RuntimeError("unable to load job module")
        mod = module_from_spec(spec)
        sys.modules["cyx_randomx_jobs_nanswap"] = mod
        spec.loader.exec_module(mod)

        raw = input("Block height [1]: ").strip() or "1"
        height = int(raw, 0)
        if height < 0:
            raise ValueError("block height must be non-negative")

        previous = input("Previous hash [zero32]: ").strip() or ("00" * 32)
        target = input("Target [difficulty 1]: ").strip()
        difficulty = None
        if target:
            target = target.removeprefix("0x")
        else:
            raw_diff = input("Difficulty [1.0]: ").strip() or "1.0"
            difficulty = float(raw_diff)
            if difficulty <= 0:
                raise ValueError("difficulty must be positive")

        job = mod.RandomXJobGenerator().generate(
            height=height,
            previous_hash=previous,
            target=target or None,
            difficulty=difficulty,
        )
        print(json.dumps({
            "job": job.notify_params(),
            "job_id": job.job_id,
            "height": job.height,
            "algo": job.algo,
            "blob_bytes": len(job.blob_bytes),
            "target": job.target,
            "clean_jobs": job.clean_jobs,
            "synthetic": True,
            "mineable": False,
            "note": "Test job only; a real pool must provide the canonical RandomX blob/target/seed parameters."
        }, indent=2))
    except (ValueError, TypeError) as exc:
        print(f"Job input failed: {exc}")
    except Exception as exc:
        print(f"Job generation failed: {exc}")


def edit_config(cfg: dict[str, Any]) -> None:
    """Interactively edit the managed XMRig + CYTHANX organism config."""
    p = pool(cfg)
    o = cfg.setdefault("organism", {})
    fields = [
        ("pool", p.get("url", ""), "XMRig pool"),
        ("user", p.get("user", ""), "XMRig wallet/user"),
        ("pass", p.get("pass", "x"), "XMRig password"),
        ("algo", p.get("algo", "rx/0"), "XMRig algorithm"),
        ("coin", p.get("coin", "monero"), "XMRig coin identifier (blank keeps current)"),
        ("tls", "on" if p.get("tls", False) else "off", "XMRig TLS"),
        ("keepalive", "on" if p.get("keepalive", True) else "off", "XMRig keepalive"),
        ("threads", str(cfg.get("threads", 0) or "auto"), "XMRig threads (0=auto)"),
        ("organism.pool", o.get("pool", ""), "CYTHANX organism pool"),
        ("organism.command", o.get("command", "status"), "organism command"),
        ("organism.payload", o.get("payload", ""), "organism payload"),
        ("organism.previous", o.get("previous", ""), "organism previous state"),
    ]
    print("Edit config — press Enter to keep the current value.")
    for key, current, label in fields:
        value = input(f"{label} [{current}]: ").strip()
        if not value:
            continue
        set_value(cfg, key, value)
    save_json(CONFIG_PATH, cfg)
    print(f"Saved {CONFIG_PATH}")

def help_text() -> None:
    print("""
Commands:
  start                         Start XMRig using the managed config
  stop                          Stop XMRig started by this controller
  restart                       Stop then start XMRig
  status                        Show miner/config status
  pool [HOST:PORT]              Set pool endpoint
  wallet [ADDRESS]              Set payout/user address
  password [VALUE]              Set pool password
  algorithm [NAME]              Set algorithm
  set algo NAME                 Set algorithm (rx/0, x11, scrypt, sha256d, blake3, cryptonight, argon2, argon2/ninja, minotaurx)
  set x11                       Shorthand for set algo x11
  set scrypt                    Shorthand for set algo scrypt
  set x11blake3                 Shorthand for set algo x11blake3
  set blake3                    Shorthand for set algo blake3
  set sha256d                   Shorthand for set algo sha256d
  set cryptonight               Shorthand for set algo cryptonight (cn/r)
  set argon                     Shorthand for set algo argon (argon2)
  set argon2                    Shorthand for set algo argon2
  set argon2/ninja              Shorthand for set algo argon2/ninja
  set ninja                     Shorthand for set algo ninja
  set minotaurx                 Shorthand for set algo minotaurx
  set forked ALGO               Set a forked/custom-miner algorithm (x11, scrypt, sha256d, blake3, minotaurx)
  check algo NAME               Check stock-XMRig compatibility and dry-run the installed binary
  threads [N]                    Set XMRig CPU threads (0=auto, 1-256)
  coin [monero]                 Set XMRig coin identifier
  set KEY VALUE                 Set miner or CYTHANX organism parameters
  set coin NAME                 Set XMRig coin hint (e.g. monero; blank/auto to omit)
  set coin VALUE                Set XMRig coin identifier
  set organism pool HOST:PORT   Set the CYTHANX organism pool endpoint
  tls on|off                    Toggle TLS in generated config
  config                        Print the current XMRig JSON config
  edit config                   Interactively edit XMRig + organism settings
  save                          Save current config
  job                           Generate a synthetic RandomX test job
  organism status               Show CYTHANX organism/chain state
  organism vector               Show CYTHAN vector and JOAAT seed
  organism cythanize            Derive source-defined CYTHAN state
  organism transform            Run CYTHAN transmogrification
  organism joatt                Derive source-defined JOATT state
  organism genesis              Emit deterministic CYTHANX genesis job
  xmrig [PATH]                  Set the XMRig executable path for this session
  debug                         Print validation diagnostics
  help                          Show commands
  quit                          Exit (does not stop a running miner)

Prompts also accept natural aliases such as:
  "start mining", "stop mining", "show status", "configure pool",
  "set wallet", and "generate job".
""")


def main() -> int:
    global CONFIG_PATH
    ap = argparse.ArgumentParser(description="Prompt-driven XMRig/RandomX controller")
    ap.add_argument("--config", default=str(CONFIG_PATH))
    ap.add_argument("--xmrig", default=None, help="path to xmrig executable")
    args = ap.parse_args()

    CONFIG_PATH = Path(args.config).expanduser().resolve()
    cfg = load_json(CONFIG_PATH, DEFAULT_CONFIG)
    if not isinstance(cfg.get("pools"), list):
        cfg["pools"] = []
    pool(cfg)
    proc: subprocess.Popen | None = None
    xmrig = args.xmrig
    recovered_pid = load_saved_pid()

    print("CYX Prompt Auto — type `help` for commands.")
    if recovered_pid:
        print(f"Recovered controller state: XMRig PID {recovered_pid} is still alive; `stop` can terminate the recovered process.")
    status(cfg, proc)
    while True:
        try:
            raw = input("\ncyx> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not raw:
            continue
        try:
            parts = shlex.split(raw)
        except ValueError as exc:
            print(f"Input error: {exc}")
            continue
        cmd = parts[0].lower()
        args2 = parts[1:]
        normalized = raw.lower()

        if cmd in {"quit", "exit", "q"}:
            break
        if cmd in {"help", "?"}:
            help_text(); continue
        if cmd == "start" or "start mining" in normalized:
            proc = start(cfg, proc, xmrig); continue
        if cmd == "stop" or "stop mining" in normalized:
            proc = stop(proc); continue
        if cmd == "restart":
            proc = stop(proc); proc = start(cfg, proc, xmrig); continue
        if cmd == "status" or "show status" in normalized:
            status(cfg, proc); continue
        if cmd == "config":
            print(json.dumps(cfg, indent=2)); continue
        if (cmd == "edit" and args2 and args2[0].lower() == "config") or cmd == "edit-config":
            edit_config(cfg); continue
        if cmd == "save":
            save_json(CONFIG_PATH, cfg); print(f"Saved {CONFIG_PATH}"); continue
        if cmd == "job" or "generate job" in normalized:
            generate_job(); continue
        if cmd in {"organism", "cythanx"} or "show organism" in normalized:
            organism_command(args2)
            continue
        if cmd in {"check", "test"} and args2 and args2[0].lower() in {"algo", "algorithm"}:
            name = " ".join(args2[1:])
            if not name:
                print("Usage: check algo NAME")
            else:
                algorithm_diagnostic(name, xmrig)
            continue
        if cmd == "xmrig":
            if args2: xmrig = args2[0]
            print(f"XMRig path: {xmrig or find_xmrig() or 'not found'}"); continue
        if cmd in {"pool", "wallet", "password", "algorithm", "coin", "tls", "keepalive", "threads"}:
            key = "pool" if cmd == "pool" else cmd
            if cmd == "algorithm" and len(args2) >= 2 and " ".join(args2[:2]).lower() == "crypto night":
                value = "cryptonight"
            else:
                value = args2[0] if args2 else input(f"{key}: ").strip()
            if cmd in {"tls", "keepalive"} and not args2:
                value = input(f"{key} on/off: ").strip()
            set_value(cfg, key, value); continue
        if cmd == "set" and args2 and args2[0].lower() == "organism" and len(args2) >= 3 and args2[1].lower() == "pool":
            set_value(cfg, "organism.pool", " ".join(args2[2:])); continue
        if cmd == "set" and args2 and args2[0].lower() in {"organism", "organism.command", "organism_action"} and len(args2) >= 2:
            # `set organism <command>` selects the organism action.
            if args2[0].lower() == "organism" and len(args2) == 2:
                set_value(cfg, "organism.command", args2[1])
            else:
                set_value(cfg, args2[0], " ".join(args2[1:]))
            continue
        if cmd == "set" and len(args2) >= 2:
            if args2[0].lower() == "crypto" and args2[1].lower() == "night":
                set_value(cfg, "algo", "cryptonight")
            else:
                set_value(cfg, args2[0], " ".join(args2[1:]))
            continue
        if cmd == "set" and len(args2) == 1 and args2[0].lower() in ALGO_ALIASES:
            set_value(cfg, "algo", args2[0])
            continue
        if cmd in {"organism-run", "run-organism"}:
            run_configured_organism(cfg); continue
        if cmd == "debug":
            print("Validation:")
            errors = validate(cfg)
            print("  OK" if not errors else "  " + "\n  ".join(errors))
            print(f"XMRig discovered: {find_xmrig(xmrig) or 'NO'}")
            print(f"Config path: {CONFIG_PATH}")
            source = locate_cythanx_source()
            print(f"CYTHANX source: {source or 'MISSING'}")
            organism_cfg = cfg.get("organism", {})
            print(f"Organism pool: {organism_cfg.get('pool') or 'NOT SET'}")
            print(f"Organism command: {organism_cfg.get('command') or 'status'}")
            if organism_cfg.get('pool'):
                print("Organism pool wiring: CONFIGURED (runtime connection requires a CYTHANX-compatible Stratum endpoint)")
            else:
                print("Organism pool wiring: NOT CONFIGURED")
            continue
        print("Unknown command. Type `help`.")

    print("Exiting controller. Any XMRig process already started is left running.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
