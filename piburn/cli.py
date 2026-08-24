#!/usr/bin/env python3
"""Prepare Ubuntu Server microSD cards for a Raspberry Pi Kubernetes cluster.

The script intentionally uses only Python's standard library and macOS tools.
Run ``piburn --help`` for non-interactive options.
"""

from __future__ import annotations, print_function

import argparse
import base64
import binascii
import contextlib
import dataclasses
import getpass
import hashlib
import http.client
import json
import lzma
import os
import plistlib
import re
import secrets
import select
import shutil
import signal
import string
import subprocess
import sys
import tempfile
import termios
import time
import tty
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Set, Tuple, cast

RELEASES_URL = "https://cdimage.ubuntu.com/releases/"
IMAGE_PATTERN = re.compile(r"ubuntu-[\w.]+-preinstalled-server-arm64\+raspi\.img\.xz$")
MIN_CARD_SIZE = 4 * 1024**3
CHUNK_SIZE = 4 * 1024**2
INTEGRITY_PATTERN_BLOCK_SIZE = CHUNK_SIZE
SUDO_REFRESH_INTERVAL = 60.0
MOUNT_GUARD_READY_TIMEOUT = 10.0
USER_AGENT = "cluster-burn/1.0"
PASSWORD_ALPHABET = string.ascii_letters + string.digits
CRYPT_ALPHABET = "./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


class BurnError(RuntimeError):
    """An expected, user-facing failure."""


class SudoError(BurnError):
    """Administrator authorization could not be obtained or refreshed."""


class FetchError(BurnError):
    def __init__(self, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


@dataclasses.dataclass(frozen=True)
class Disk:
    identifier: str
    name: str
    size: int
    protocol: str
    internal: bool
    removable: bool
    ejectable: bool
    device_tree_path: str = ""
    media_uuid: str = ""

    @property
    def device(self) -> str:
        return "/dev/" + self.identifier

    @property
    def raw_device(self) -> str:
        return "/dev/r" + self.identifier

    @property
    def fingerprint(self) -> Tuple[str, int, str, str, str, str]:
        return (
            self.identifier,
            self.size,
            self.name,
            self.protocol,
            self.device_tree_path,
            self.media_uuid,
        )

    @property
    def label(self) -> str:
        traits = [self.protocol] if self.protocol else []
        if self.removable:
            traits.append("removable")
        suffix = ", ".join(traits)
        return "{} — {} — {}{}".format(
            self.device,
            self.name,
            human_size(self.size),
            " ({})".format(suffix) if suffix else "",
        )


@dataclasses.dataclass(frozen=True)
class ImageSpec:
    path: Path
    compressed_sha256: Optional[str]
    source: str
    uncompressed_size: Optional[int] = None
    file_identity: Optional[Tuple[int, int, int, int]] = None


class SudoSession:
    """Keep one sudo authentication valid during a long card-writing run."""

    def __init__(self, refresh_interval: float = SUDO_REFRESH_INTERVAL) -> None:
        self.refresh_interval = refresh_interval
        self.next_refresh = 0.0
        self._heartbeats = []  # type: List[Callable[[], None]]

    def add_heartbeat(self, heartbeat: Callable[[], None]) -> None:
        self._heartbeats.append(heartbeat)

    def remove_heartbeat(self, heartbeat: Callable[[], None]) -> None:
        self._heartbeats.remove(heartbeat)

    def authenticate(self) -> None:
        try:
            run(["sudo", "-v"], capture=False)
        except BurnError as exc:
            raise SudoError(str(exc)) from exc
        self.next_refresh = time.monotonic() + self.refresh_interval

    def keep_alive(self) -> None:
        for heartbeat in tuple(self._heartbeats):
            heartbeat()
        if time.monotonic() < self.next_refresh:
            return
        try:
            result = run(["sudo", "-n", "-v"], check=False)
        except BurnError as exc:
            raise SudoError(str(exc)) from exc
        if result.returncode != 0:
            print("Administrator authorization expired; macOS will ask for the password again.")
            try:
                run(["sudo", "-v"], capture=False)
            except BurnError as exc:
                raise SudoError(str(exc)) from exc
        self.next_refresh = time.monotonic() + self.refresh_interval


class AutomaticMountGuard:
    """Keep a separate Disk Arbitration helper alive for one whole disk."""

    def __init__(self, disk: Disk) -> None:
        self.disk = disk
        self.process = None  # type: Optional[subprocess.Popen[bytes]]

    def start(self) -> None:
        ensure_same_disk(self.disk)
        try:
            self.process = subprocess.Popen(
                [sys.executable, "-m", "piburn._mount_guard", self.disk.identifier],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            raise BurnError("Could not start the automatic-mount guard: {}".format(exc))
        assert self.process.stdout is not None
        try:
            readable, _writable, _exceptional = select.select(
                [self.process.stdout], [], [], MOUNT_GUARD_READY_TIMEOUT
            )
            ready_message = self.process.stdout.readline() if readable else b""
        except OSError as exc:
            detail = self.stop()
            raise BurnError(
                "Could not initialize the automatic-mount guard: {}{}".format(
                    exc,
                    ": " + detail if detail else "",
                )
            )
        if ready_message != b"READY\n":
            detail = self.stop()
            raise BurnError(
                "Could not initialize the automatic-mount guard for {}{}".format(
                    self.disk.device,
                    ": " + detail if detail else "",
                )
            )

    def keep_alive(self) -> None:
        if self.process is None:
            raise BurnError("The automatic-mount guard is not running")
        returncode = self.process.poll()
        if returncode is None:
            return
        _stdout, stderr = self.process.communicate()
        self.process = None
        detail = (stderr or b"").decode("utf-8", "replace").strip()
        raise BurnError(
            "The automatic-mount guard for {} stopped unexpectedly (exit code {}){}".format(
                self.disk.device,
                returncode,
                ": " + detail if detail else "",
            )
        )

    def stop(self) -> str:
        process = self.process
        self.process = None
        if process is None:
            return ""
        if process.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
        try:
            _stdout, stderr = process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            _stdout, stderr = process.communicate()
        except OSError as exc:
            return str(exc)
        return (stderr or b"").decode("utf-8", "replace").strip()


@contextlib.contextmanager
def prevent_automatic_mounts(disk: Disk, sudo_session: SudoSession) -> Iterator[AutomaticMountGuard]:
    """Deny automatic mounts while raw image bytes are written and verified."""
    guard = AutomaticMountGuard(disk)
    guard.start()
    sudo_session.add_heartbeat(guard.keep_alive)
    try:
        guard.keep_alive()
        yield guard
        guard.keep_alive()
    finally:
        sudo_session.remove_heartbeat(guard.keep_alive)
        guard.stop()


class TerminalInputParser:
    """Decode terminal key sequences without mixing OS and TextIO buffers."""

    def __init__(self) -> None:
        self.buffer = b""

    def feed(self, data: bytes) -> List[str]:
        self.buffer += data
        events = []  # type: List[str]
        while self.buffer:
            first = self.buffer[0]

            if first == 0x1B:  # Escape / ANSI control sequence
                if len(self.buffer) < 2:
                    break
                introducer = self.buffer[1]
                if introducer == ord("["):
                    final_index = None
                    for index in range(2, len(self.buffer)):
                        if 0x40 <= self.buffer[index] <= 0x7E:
                            final_index = index
                            break
                    if final_index is None:
                        break
                    final = chr(self.buffer[final_index])
                    self.buffer = self.buffer[final_index + 1 :]
                    if final == "A":
                        events.append("up")
                    elif final == "B":
                        events.append("down")
                    continue
                if introducer == ord("O"):  # Application cursor-key mode
                    if len(self.buffer) < 3:
                        break
                    final = chr(self.buffer[2])
                    self.buffer = self.buffer[3:]
                    if final == "A":
                        events.append("up")
                    elif final == "B":
                        events.append("down")
                    continue
                # Unknown Alt/Escape combination: ignore Escape but preserve the
                # following byte so it can still be handled as text.
                self.buffer = self.buffer[1:]
                continue

            if first < 0x80:
                self.buffer = self.buffer[1:]
                if first == 0x03:
                    events.append("interrupt")
                elif first in (0x0A, 0x0D):
                    events.append("enter")
                elif first in (0x08, 0x7F):
                    events.append("backspace")
                elif chr(first).isprintable():
                    events.append("text:" + chr(first))
                continue

            if 0xC2 <= first <= 0xDF:
                character_length = 2
            elif 0xE0 <= first <= 0xEF:
                character_length = 3
            elif 0xF0 <= first <= 0xF4:
                character_length = 4
            else:
                self.buffer = self.buffer[1:]
                continue
            if len(self.buffer) < character_length:
                break
            encoded = self.buffer[:character_length]
            try:
                character = encoded.decode("utf-8")
            except UnicodeDecodeError:
                self.buffer = self.buffer[1:]
                continue
            self.buffer = self.buffer[character_length:]
            if character.isprintable():
                events.append("text:" + character)
        return events


def eprint(message: str = "") -> None:
    print(message, file=sys.stderr, flush=True)


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024.0 or unit == "TiB":
            return "{:.1f} {}".format(value, unit)
        value /= 1024.0
    return "{} B".format(size)


def run(
    args: Sequence[str],
    check: bool = True,
    capture: bool = True,
    input_data: Optional[bytes] = None,
) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            list(args),
            input=input_data,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
            check=check,
        )
    except FileNotFoundError:
        raise BurnError("Required system command not found: {}".format(args[0]))
    except OSError as exc:
        raise BurnError("Could not start {}: {}".format(args[0], exc))
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or b"").decode("utf-8", "replace").strip()
        raise BurnError("Command {} failed: {}".format(args[0], detail or exc.returncode))


def diskutil_plist(*args: str) -> Dict[str, object]:
    if not args:
        raise BurnError("No diskutil command was specified")
    # Unlike some diskutil subcommands, `info` only accepts -plist directly
    # after the subcommand (`diskutil info -plist /dev/diskN`).
    result = run(["diskutil", args[0], "-plist"] + list(args[1:]))
    try:
        parsed = plistlib.loads(result.stdout)
        if not isinstance(parsed, dict):
            raise BurnError("diskutil returned a plist that is not a dictionary")
        return cast(Dict[str, object], parsed)
    except (plistlib.InvalidFileException, ValueError) as exc:
        raise BurnError("diskutil returned invalid data: {}".format(exc))


def disk_from_info(info: Dict[str, object]) -> Optional[Disk]:
    identifier = str(info.get("DeviceIdentifier", ""))
    if not re.fullmatch(r"disk\d+", identifier):
        return None
    whole = bool(info.get("WholeDisk", info.get("Whole", False)))
    internal = bool(info.get("Internal", True))
    removable = bool(info.get("RemovableMedia", False))
    ejectable = bool(info.get("Ejectable", False))
    writable = bool(info.get("WritableMedia", info.get("Writable", True)))
    physical = str(info.get("VirtualOrPhysical") or "Physical").lower() != "virtual"
    size = int(str(info.get("TotalSize") or info.get("Size") or 0))
    if not whole or internal or not physical or not writable or not removable or size < MIN_CARD_SIZE:
        return None
    return Disk(
        identifier=identifier,
        name=str(info.get("MediaName") or info.get("VolumeName") or "unnamed"),
        size=size,
        protocol=str(info.get("BusProtocol") or info.get("Protocol") or ""),
        internal=internal,
        removable=removable,
        ejectable=ejectable,
        device_tree_path=str(info.get("DeviceTreePath") or ""),
        media_uuid=str(info.get("MediaUUID") or ""),
    )


def list_candidate_disks() -> List[Disk]:
    listing = diskutil_plist("list")
    roots = listing.get("WholeDisks", listing.get("AllDisks", []))
    disks = []
    for identifier in roots if isinstance(roots, list) else []:
        if not re.fullmatch(r"disk\d+", str(identifier)):
            continue
        try:
            candidate = disk_from_info(diskutil_plist("info", "/dev/" + str(identifier)))
        except BurnError:
            continue
        if candidate is not None:
            disks.append(candidate)
    return sorted(disks, key=lambda disk: disk.identifier)


def normalize_disk_identifier(identifier_or_path: str) -> str:
    match = re.fullmatch(r"(?:/dev/)?(r?disk\d+)", identifier_or_path)
    if not match:
        raise BurnError("Invalid whole-disk identifier: {}".format(identifier_or_path))
    identifier = match.group(1)
    return identifier[1:] if identifier.startswith("r") else identifier


def get_disk(identifier_or_path: str) -> Disk:
    identifier = normalize_disk_identifier(identifier_or_path)
    candidate = disk_from_info(diskutil_plist("info", "/dev/" + identifier))
    if candidate is None:
        raise BurnError("{} is not an external removable writable disk of at least 4 GiB".format(identifier_or_path))
    return candidate


def ensure_same_disk(original: Disk) -> Disk:
    current = get_disk(original.device)
    if current.fingerprint != original.fingerprint:
        raise BurnError("The media in {} changed after selection; the operation was stopped".format(original.device))
    return current


def ask_positive_int(prompt: str, default: Optional[int] = None) -> int:
    while True:
        answer = input(prompt).strip()
        if not answer and default is not None:
            return default
        try:
            value = int(answer)
            if value > 0:
                return value
        except ValueError:
            pass
        eprint("Error: enter a positive integer (1, 2, 3, ...).")


def ask_yes_no(prompt: str, default: Optional[bool] = None) -> bool:
    hint = " [Y/n] " if default is True else " [y/N] " if default is False else " [y/n] "
    while True:
        answer = input(prompt + hint).strip().lower()
        if not answer and default is not None:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        eprint("Enter yes or no.")


@contextlib.contextmanager
def cbreak_terminal() -> Iterator[None]:
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def selection_window(
    items: Sequence[Tuple[str, str]], selected: Optional[int], limit: int = 15
) -> Tuple[int, Sequence[Tuple[str, str]]]:
    if len(items) <= limit:
        return 0, items
    if selected is None:
        start = 0
    else:
        start = max(0, min(selected - limit // 2, len(items) - limit))
    return start, items[start : start + limit]


def remap_selection(
    previous_items: Sequence[Tuple[str, str]],
    selected: Optional[int],
    refreshed_items: Sequence[Tuple[str, str]],
    had_options: bool,
) -> Optional[int]:
    previous_value = None
    if selected is not None and selected < len(previous_items):
        previous_value = previous_items[selected][0]
    if previous_value is not None:
        return next(
            (index for index, item in enumerate(refreshed_items) if item[0] == previous_value),
            None,
        )
    if not had_options and refreshed_items:
        return 0
    return None


def choose_dynamic(
    prompt: str,
    provider: Callable[[], Sequence[Tuple[str, str]]],
    allow_custom: bool = False,
    refresh_interval: float = 1.0,
    heartbeat: Optional[Callable[[], None]] = None,
) -> str:
    """Arrow-key selector with live filtering and periodically refreshed choices."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        fallback_options = list(provider())
        for number, (_, label) in enumerate(fallback_options, 1):
            print("  {}) {}".format(number, label))
        while True:
            if heartbeat is not None:
                heartbeat()
            answer = input(prompt + (" (number or value): " if allow_custom else " (number): ")).strip()
            if heartbeat is not None:
                heartbeat()
            if allow_custom and answer and not answer.isdigit():
                return answer
            if answer.isdigit() and 1 <= int(answer) <= len(fallback_options):
                return fallback_options[int(answer) - 1][0]
            eprint("Invalid selection.")

    query = ""
    selected = None  # type: Optional[int]
    options = []  # type: List[Tuple[str, str]]
    # Some platforms start their monotonic clock near zero. Use an explicit
    # sentinel so the provider is always called before the first key event.
    last_refresh = -float("inf")
    last_render = None  # type: object
    input_parser = TerminalInputParser()
    result = None  # type: Optional[str]
    rendered_line_count = 0
    print()
    with cbreak_terminal():
        while True:
            if heartbeat is not None:
                heartbeat()
            now = time.monotonic()
            if now - last_refresh >= refresh_interval:
                previous_filtered = [item for item in options if query.lower() in item[1].lower()]
                had_options = bool(options)
                options = list(provider())
                last_refresh = now
                refreshed = [item for item in options if query.lower() in item[1].lower()]
                selected = remap_selection(previous_filtered, selected, refreshed, had_options)
            filtered = [item for item in options if query.lower() in item[1].lower()]
            if not filtered:
                selected = None
            state = (query, selected, tuple(filtered))
            if state != last_render:
                lines = [prompt, "Search/type: {}".format(query or "—")]
                if not filtered:
                    lines.append("  (no matches; the list refreshes automatically)")
                window_start, visible = selection_window(filtered, selected)
                if window_start:
                    lines.append("  … {} more above".format(window_start))
                for offset, (_, label) in enumerate(visible):
                    index = window_start + offset
                    marker = "\033[1;36m❯" if selected is not None and index == selected else " "
                    reset = "\033[0m" if selected is not None and index == selected else ""
                    lines.append("{} {}{}".format(marker, label, reset))
                hidden_below = len(filtered) - window_start - len(visible)
                if hidden_below:
                    lines.append("  … {} more below".format(hidden_below))
                if filtered and selected is None:
                    lines.append("  (the selected item disappeared; select another with the arrow keys)")
                lines.append("↑/↓ — select, type — filter, Enter — confirm, Ctrl+C — quit")
                sys.stdout.write("\r")
                if rendered_line_count > 1:
                    sys.stdout.write("\033[{}A".format(rendered_line_count - 1))
                sys.stdout.write("\033[J" + "\n".join(lines))
                sys.stdout.flush()
                rendered_line_count = len(lines)
                last_render = state

            ready, _, _ = select.select([sys.stdin], [], [], min(refresh_interval, 0.5))
            if not ready:
                continue
            for event in input_parser.feed(os.read(sys.stdin.fileno(), 64)):
                # A single os.read() commonly contains text plus Enter. Keep the
                # filtered view in sync between every decoded event.
                filtered = [item for item in options if query.lower() in item[1].lower()]
                if not filtered:
                    selected = None
                elif selected is not None:
                    selected %= len(filtered)
                if event == "interrupt":
                    raise KeyboardInterrupt
                if event == "enter":
                    if allow_custom and query and not any(value == query for value, _ in filtered):
                        result = query
                        break
                    if filtered and selected is not None:
                        result = filtered[selected][0]
                        break
                elif event == "backspace":
                    query = query[:-1]
                    selected = 0 if any(query.lower() in item[1].lower() for item in options) else None
                elif event == "up" and filtered:
                    selected = len(filtered) - 1 if selected is None else (selected - 1) % len(filtered)
                elif event == "down" and filtered:
                    selected = 0 if selected is None else (selected + 1) % len(filtered)
                elif event.startswith("text:"):
                    query += event[5:]
                    selected = 0 if any(query.lower() in item[1].lower() for item in options) else None
            if result is not None:
                break
    assert result is not None
    sys.stdout.write("\r")
    if rendered_line_count > 1:
        sys.stdout.write("\033[{}A".format(rendered_line_count - 1))
    sys.stdout.write("\033[JSelected: {}\n".format(result))
    sys.stdout.flush()
    return result


def wifi_interface() -> Optional[str]:
    result = run(["networksetup", "-listallhardwareports"], check=False)
    text = result.stdout.decode("utf-8", "replace")
    match = re.search(r"Hardware Port: (?:Wi-Fi|AirPort)\s+Device: (\S+)", text)
    return match.group(1) if match else None


def _names_from_system_profiler(value: object, found: Set[str], in_network_list: bool = False) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            is_network = in_network_list or key in (
                "spairport_current_network_information",
                "spairport_other_local_wireless_networks",
            )
            if key == "_name" and is_network and isinstance(nested, str):
                found.add(nested)
            else:
                _names_from_system_profiler(nested, found, is_network)
    elif isinstance(value, list):
        for nested in value:
            _names_from_system_profiler(nested, found, in_network_list)


def scan_wifi_networks() -> List[str]:
    names = set()  # type: Set[str]
    active_scan_completed = False
    interface = wifi_interface()
    if interface:
        current = run(["networksetup", "-getairportnetwork", interface], check=False)
        current_text = current.stdout.decode("utf-8", "replace").strip()
        if ": " in current_text and "not associated" not in current_text.lower():
            names.add(current_text.split(": ", 1)[1])
        preferred = run(["networksetup", "-listpreferredwirelessnetworks", interface], check=False)
        lines = preferred.stdout.decode("utf-8", "replace").splitlines()[1:]
        names.update(line.strip() for line in lines if line.strip())

    airport_paths = [
        "/System/Library/PrivateFrameworks/Apple80211.framework/Versions/Current/Resources/airport",
        "/System/Library/PrivateFrameworks/Apple80211.framework/Resources/airport",
    ]
    for airport in airport_paths:
        if os.path.exists(airport):
            result = run([airport, "-s"], check=False)
            if result.returncode == 0:
                active_scan_completed = True
                for line in result.stdout.decode("utf-8", "replace").splitlines()[1:]:
                    match = re.match(r"\s*(.*?)\s+(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\s", line)
                    if match and match.group(1):
                        names.add(match.group(1))
                break

    if not active_scan_completed and shutil.which("system_profiler"):
        result = run(["system_profiler", "SPAirPortDataType", "-json", "-detailLevel", "full"], check=False)
        if result.returncode == 0:
            try:
                _names_from_system_profiler(json.loads(result.stdout.decode("utf-8")), names)
            except (ValueError, UnicodeDecodeError):
                pass
    return sorted(name for name in names if name and not name.startswith("spairport_"))


class CachedWifiProvider:
    def __init__(self) -> None:
        self.values = []  # type: List[str]
        self.last_scan = 0.0

    def __call__(self) -> Sequence[Tuple[str, str]]:
        if time.monotonic() - self.last_scan > 5.0:
            self.values = scan_wifi_networks()
            self.last_scan = time.monotonic()
        return [(name, name) for name in self.values]


def validate_prefix(prefix: str) -> str:
    prefix = prefix.strip().lower()
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", prefix):
        raise BurnError("The prefix must be a DNS label containing letters, digits, and hyphens")
    return prefix


def hostname_for(prefix: str, number: int) -> str:
    hostname = "{}-{}".format(prefix, number)
    if len(hostname) > 63:
        raise BurnError("Hostname {} is too long (maximum 63 characters)".format(hostname))
    return hostname


def validate_username(username: str) -> str:
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", username):
        raise BurnError("Invalid Linux username: {}".format(username))
    return username


def detect_timezone() -> str:
    try:
        target = os.path.realpath("/etc/localtime")
        marker = "/zoneinfo/"
        if marker in target:
            return target.split(marker, 1)[1]
    except OSError:
        pass
    return "UTC"


def find_ssh_public_key(explicit: Optional[str]) -> Tuple[str, Optional[Path]]:
    candidates = (
        [Path(explicit).expanduser()]
        if explicit
        else [
            Path.home() / ".ssh" / "id_ed25519.pub",
            Path.home() / ".ssh" / "id_ecdsa.pub",
            Path.home() / ".ssh" / "id_rsa.pub",
        ]
    )
    for path in candidates:
        try:
            is_file = bool(path and path.is_file())
            key = path.read_text(encoding="utf-8").strip() if is_file else ""
        except (OSError, UnicodeError) as exc:
            raise BurnError("Could not read SSH public key {}: {}".format(path, exc))
        if is_file:
            if valid_ssh_public_key(key):
                private = Path(str(path)[:-4]) if str(path).endswith(".pub") else None
                return key, private if private and private.exists() else None
            raise BurnError("{} does not look like a valid SSH public key".format(path))
    raise BurnError("No SSH public key was found. Create one with ssh-keygen -t ed25519 or specify --ssh-public-key")


def valid_ssh_public_key(key: str) -> bool:
    parts = key.split()
    if len(parts) < 2:
        return False
    key_type, encoded = parts[:2]
    if not re.fullmatch(r"(?:ssh-|ecdsa-|sk-)[A-Za-z0-9@._+-]+", key_type):
        return False
    try:
        blob = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError):
        return False

    def read_field(offset: int) -> Tuple[Optional[bytes], int]:
        if len(blob) < offset + 4:
            return None, offset
        field_length = int.from_bytes(blob[offset : offset + 4], "big")
        start = offset + 4
        end = start + field_length
        if field_length <= 0 or end > len(blob):
            return None, offset
        return blob[start:end], end

    embedded, offset = read_field(0)
    if embedded is None:
        return False
    try:
        embedded_type = embedded.decode("ascii")
    except UnicodeDecodeError:
        return False
    if embedded_type != key_type:
        return False

    if key_type == "ssh-ed25519":
        public_key, offset = read_field(offset)
        return public_key is not None and len(public_key) == 32 and offset == len(blob)
    if key_type == "ssh-rsa":
        exponent, offset = read_field(offset)
        modulus, offset = read_field(offset)
        return exponent is not None and modulus is not None and len(modulus) >= 128 and offset == len(blob)
    if key_type.startswith("ecdsa-sha2-"):
        curve, offset = read_field(offset)
        point, offset = read_field(offset)
        expected_curve = key_type[len("ecdsa-sha2-") :].encode("ascii")
        return curve == expected_curve and point is not None and len(point) > 1 and offset == len(blob)
    if key_type == "sk-ssh-ed25519@openssh.com":
        public_key, offset = read_field(offset)
        application, offset = read_field(offset)
        return public_key is not None and len(public_key) == 32 and application is not None and offset == len(blob)
    if key_type == "sk-ecdsa-sha2-nistp256@openssh.com":
        curve, offset = read_field(offset)
        point, offset = read_field(offset)
        application, offset = read_field(offset)
        return curve == b"nistp256" and point is not None and application is not None and offset == len(blob)
    return False


def generate_password(length: int = 20) -> str:
    return "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(length))


def _crypt_base64(byte2: int, byte1: int, byte0: int, length: int) -> str:
    value = (byte2 << 16) | (byte1 << 8) | byte0
    result = []
    for _ in range(length):
        result.append(CRYPT_ALPHABET[value & 0x3F])
        value >>= 6
    return "".join(result)


def sha512_crypt(password: str, salt: Optional[str] = None) -> str:
    """Create a standard $6$ SHA-512 crypt hash without external packages."""
    salt = salt or "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(16))
    if not re.fullmatch(r"[./0-9A-Za-z]{1,16}", salt):
        raise BurnError("Invalid SHA-512 crypt salt")
    password_bytes = password.encode("utf-8")
    salt_bytes = salt.encode("ascii")

    alternate = hashlib.sha512(password_bytes + salt_bytes + password_bytes).digest()
    digest = hashlib.sha512()
    digest.update(password_bytes)
    digest.update(salt_bytes)
    for _ in range(len(password_bytes) // 64):
        digest.update(alternate)
    digest.update(alternate[: len(password_bytes) % 64])
    remaining = len(password_bytes)
    while remaining:
        digest.update(alternate if remaining & 1 else password_bytes)
        remaining >>= 1
    alternate = digest.digest()

    digest = hashlib.sha512()
    for _ in range(len(password_bytes)):
        digest.update(password_bytes)
    repeated_password = digest.digest()
    password_sequence = (repeated_password * ((len(password_bytes) + 63) // 64))[: len(password_bytes)]

    digest = hashlib.sha512()
    for _ in range(16 + alternate[0]):
        digest.update(salt_bytes)
    repeated_salt = digest.digest()
    salt_sequence = (repeated_salt * ((len(salt_bytes) + 63) // 64))[: len(salt_bytes)]

    for round_number in range(5000):
        digest = hashlib.sha512()
        digest.update(password_sequence if round_number & 1 else alternate)
        if round_number % 3:
            digest.update(salt_sequence)
        if round_number % 7:
            digest.update(password_sequence)
        digest.update(alternate if round_number & 1 else password_sequence)
        alternate = digest.digest()

    groups = (
        (0, 21, 42),
        (22, 43, 1),
        (44, 2, 23),
        (3, 24, 45),
        (25, 46, 4),
        (47, 5, 26),
        (6, 27, 48),
        (28, 49, 7),
        (50, 8, 29),
        (9, 30, 51),
        (31, 52, 10),
        (53, 11, 32),
        (12, 33, 54),
        (34, 55, 13),
        (56, 14, 35),
        (15, 36, 57),
        (37, 58, 16),
        (59, 17, 38),
        (18, 39, 60),
        (40, 61, 19),
        (62, 20, 41),
    )
    encoded = "".join(_crypt_base64(alternate[a], alternate[b], alternate[c], 4) for a, b, c in groups)
    encoded += _crypt_base64(0, 0, alternate[63], 2)
    return "$6${}${}".format(salt, encoded)


def ask_user_password() -> str:
    generated = generate_password()
    print("Generated default password: {}".format(generated))
    while True:
        password = getpass.getpass("New password (press Enter to use the generated password): ")
        if not password:
            return generated
        confirmation = getpass.getpass("Repeat the new password: ")
        if password == confirmation:
            return password
        eprint("Passwords do not match; try again.")


def yaml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def render_user_data(
    hostname: str,
    username: str,
    timezone: str,
    ssh_key: Optional[str],
    password_hash: Optional[str],
) -> str:
    if bool(ssh_key) == bool(password_hash):
        raise BurnError("Configure exactly one login method: SSH key or password")
    if ssh_key:
        authentication = """    lock_passwd: true
    ssh_authorized_keys:
      - {ssh_key}
ssh_pwauth: false""".format(ssh_key=yaml_string(ssh_key))
    else:
        authentication = """    lock_passwd: false
    passwd: {password_hash}
ssh_pwauth: true""".format(password_hash=yaml_string(password_hash or ""))
    return """#cloud-config
hostname: {hostname}
fqdn: {fqdn}
manage_etc_hosts: true
timezone: {timezone}
locale: en_US.UTF-8
users:
  - name: {username}
    groups: [adm, sudo]
    shell: /bin/bash
    sudo: "ALL=(ALL) NOPASSWD:ALL"
{authentication}
disable_root: true
growpart:
  mode: auto
  devices: ['/']
resize_rootfs: true
package_update: true
packages:
  - avahi-daemon
runcmd:
  - [systemctl, enable, --now, avahi-daemon]
final_message: {final_message}
""".format(
        hostname=yaml_string(hostname),
        fqdn=yaml_string(hostname + ".local"),
        timezone=yaml_string(timezone),
        username=yaml_string(username),
        authentication=authentication,
        final_message=yaml_string("cluster node {} is ready".format(hostname)),
    )


def render_network_config(ssid: str, password: str) -> str:
    return """version: 2
ethernets:
  eth0:
    dhcp4: true
    optional: true
wifis:
  wlan0:
    dhcp4: true
    optional: false
    access-points:
      {ssid}:
        password: {password}
""".format(ssid=yaml_string(ssid), password=yaml_string(password))


def render_meta_data(hostname: str) -> str:
    return "instance-id: {}\nlocal-hostname: {}\n".format(
        yaml_string("cluster-" + hostname + "-" + uuid.uuid4().hex), yaml_string(hostname)
    )


def parse_sha256sums(text: str) -> Dict[str, str]:
    sums = {}
    for line in text.splitlines():
        match = re.fullmatch(r"([0-9a-fA-F]{64})\s+\*?(.+)", line.strip())
        if match:
            sums[match.group(2)] = match.group(1).lower()
    return sums


def find_latest_release_image() -> Tuple[str, str, str]:
    """Return image URL, filename and SHA-256 for the newest published GA release.

    Ubuntu exposes directories for development releases too, so choosing the
    greatest directory name is insufficient. A release counts as published only
    if release/SHA256SUMS exists and contains the Raspberry Pi server image.
    """
    index = fetch_bytes(RELEASES_URL).decode("utf-8", "replace")
    versions = set(re.findall(r'href=["\'](\d{2}\.\d{2})/["\']', index))
    ordered = sorted(versions, key=lambda item: tuple(int(part) for part in item.split(".")), reverse=True)
    for version in ordered:
        release_url = urllib.parse.urljoin(RELEASES_URL, version + "/release/")
        try:
            sums_text = fetch_bytes(urllib.parse.urljoin(release_url, "SHA256SUMS")).decode("utf-8", "replace")
        except FetchError as exc:
            # Development release directories exist before their final images.
            if exc.status == 404:
                continue
            raise
        sums = parse_sha256sums(sums_text)
        matches = sorted(name for name in sums if IMAGE_PATTERN.fullmatch(name))
        if matches:
            filename = matches[-1]
            return urllib.parse.urljoin(release_url, filename), filename, sums[filename]
    raise BurnError("No published stable Ubuntu Server image for Raspberry Pi was found")


def fetch_bytes(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return cast(bytes, response.read())
    except urllib.error.HTTPError as exc:
        raise FetchError("Could not download {}: HTTP {}".format(url, exc.code), exc.code)
    except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
        raise FetchError("Could not download {}: {}".format(url, exc))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            while True:
                chunk = source.read(CHUNK_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError as exc:
        raise BurnError("Could not read {}: {}".format(path, exc))
    return digest.hexdigest()


def download_file(url: str, destination: Path, expected_sha256: Optional[str]) -> None:
    temporary = destination.with_name(destination.name + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    digest = hashlib.sha256()
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(request, timeout=60) as response, temporary.open("wb") as target:
            try:
                total = int(response.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                total = 0
            written = 0
            while True:
                chunk = response.read(CHUNK_SIZE)
                if not chunk:
                    break
                target.write(chunk)
                digest.update(chunk)
                written += len(chunk)
                show_progress("Downloading image", written, total)
        print()
        actual = digest.hexdigest()
        if expected_sha256 and actual != expected_sha256.lower():
            raise BurnError("The downloaded image SHA-256 does not match the published checksum")
        os.replace(str(temporary), str(destination))
    except BurnError:
        raise
    except urllib.error.HTTPError as exc:
        raise BurnError("Could not download the image: HTTP {}".format(exc.code))
    except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
        raise BurnError("Could not download the image: {}".format(exc))
    finally:
        with contextlib.suppress(OSError):
            if temporary.exists():
                temporary.unlink()


def resolve_image(image: Optional[str], sha256: Optional[str], download_dir: Path) -> ImageSpec:
    if image and urllib.parse.urlparse(image).scheme not in ("http", "https"):
        path = Path(image).expanduser().resolve()
        if not path.is_file():
            raise BurnError("Image not found: {}".format(path))
        if not (path.name.endswith(".img") or path.name.endswith(".img.xz")):
            raise BurnError("Only .img and .img.xz images are supported")
        if sha256 and sha256_file(path) != sha256.lower():
            raise BurnError("The local image SHA-256 does not match --sha256")
        return ImageSpec(path, sha256.lower() if sha256 else None, str(path))

    if image:
        url = image
        filename = os.path.basename(urllib.parse.urlparse(url).path)
        expected = sha256.lower() if sha256 else None
        if not filename:
            raise BurnError("The image URL does not contain a filename")
        if expected is None:
            raise BurnError("--sha256 is required for an image from a custom URL")
    else:
        url, filename, expected = find_latest_release_image()

    if not (filename.endswith(".img") or filename.endswith(".img.xz")):
        raise BurnError("Only .img and .img.xz images are supported")

    # Remote images live only in the TemporaryDirectory owned by main().
    # The directory is removed after the run, including on errors or Ctrl+C.
    destination = download_dir.resolve() / filename
    download_file(url, destination, expected)
    return ImageSpec(destination, expected, url)


def show_progress(label: str, done: int, total: int, started: Optional[float] = None) -> None:
    speed = ""
    if started is not None and time.monotonic() > started:
        speed = ", {}/s".format(human_size(int(done / (time.monotonic() - started))))
    if total:
        percent = min(100.0, done * 100.0 / total)
        status = "{:.1f}% ({}/{}{})".format(percent, human_size(done), human_size(total), speed)
    else:
        status = "{}{}".format(human_size(done), speed)
    sys.stdout.write("\r{}: {}".format(label, status))
    sys.stdout.flush()


def ensure_sudo() -> SudoSession:
    print(
        "macOS will normally ask once for an administrator password; "
        "it may ask again if authorization is revoked or expires unusually quickly."
    )
    session = SudoSession()
    session.authenticate()
    return session


def popen_or_error(args: Sequence[str], **kwargs: Any) -> subprocess.Popen[bytes]:
    try:
        return subprocess.Popen(list(args), **kwargs)
    except OSError as exc:
        raise BurnError("Could not start {}: {}".format(args[0], exc))


def unmount_disk(disk: Disk) -> None:
    run(["diskutil", "unmountDisk", disk.device])


def eject_disk(disk: Disk, quiet: bool = False) -> None:
    result = run(["diskutil", "eject", disk.device], check=False)
    if result.returncode != 0 and not quiet:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise BurnError("Could not eject {}: {}".format(disk.device, detail))


def write_all(stream: Any, data: bytes) -> None:
    """Write a complete block to a pipe whose write method may make partial progress."""
    remaining = memoryview(data)
    while remaining:
        written = stream.write(remaining)
        if not isinstance(written, int) or written <= 0 or written > len(remaining):
            raise OSError("the dd input pipe did not accept the complete block")
        remaining = remaining[written:]


def read_up_to(stream: Any, size: int) -> bytes:
    """Read up to size bytes, joining short reads until EOF or the requested size."""
    chunks = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def integrity_pattern(seed: bytes, offset: int, length: int) -> bytes:
    result = bytearray()
    position = offset
    while len(result) < length:
        block_number = position // INTEGRITY_PATTERN_BLOCK_SIZE
        offset_in_block = position % INTEGRITY_PATTERN_BLOCK_SIZE
        block_seed = b"piburn-integrity-v2\0" + seed + block_number.to_bytes(8, "big")
        # SHAKE produces a deterministic, non-periodic byte stream for the
        # whole chunk. Sector aliases within a chunk therefore cannot compare
        # equal merely because a short digest was repeated.
        block = hashlib.shake_256(block_seed).digest(INTEGRITY_PATTERN_BLOCK_SIZE)
        take = min(length - len(result), INTEGRITY_PATTERN_BLOCK_SIZE - offset_in_block)
        result.extend(block[offset_in_block : offset_in_block + take])
        position += take
    return bytes(result)


def write_integrity_pattern(disk: Disk, sudo_session: SudoSession, seed: bytes) -> None:
    sudo_session.keep_alive()
    ensure_same_disk(disk)
    unmount_disk(disk)
    ensure_same_disk(disk)
    process = popen_or_error(
        ["sudo", "-n", "dd", "of=" + disk.raw_device, "bs=" + str(CHUNK_SIZE), "conv=fsync"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        preexec_fn=os.setpgrp,
    )
    written = 0
    started = time.monotonic()
    try:
        assert process.stdin is not None
        while written < disk.size:
            chunk_size = min(CHUNK_SIZE, disk.size - written)
            write_all(process.stdin, integrity_pattern(seed, written, chunk_size))
            written += chunk_size
            show_progress("Integrity test write", written, disk.size, started)
            sudo_session.keep_alive()
        process.stdin.close()
        process.stdin = None
        stderr = process.communicate()[1].decode("utf-8", "replace")
    except (KeyboardInterrupt, BurnError, BrokenPipeError, OSError) as exc:
        close_process_stdin(process)
        detail = ""
        if isinstance(exc, BrokenPipeError):
            # dd has already closed its input; let it finish so its stderr
            # explains the underlying device failure.
            detail = process_stderr(process)
            if process.poll() is None:
                terminate_process_group(process)
        else:
            terminate_process_group(process)
            process.wait()
        if isinstance(exc, KeyboardInterrupt):
            raise
        if isinstance(exc, BurnError):
            raise
        if isinstance(exc, BrokenPipeError):
            suffix = detail or "dd closed its input pipe without an error message"
            raise BurnError("Integrity test write failed for {}: {}".format(disk.device, suffix))
        raise BurnError("Integrity test write failed for {}: {}".format(disk.device, exc))
    print()
    if process.returncode != 0:
        raise BurnError("Integrity test write failed for {}: {}".format(disk.device, stderr.strip()))
    run(["sync"], capture=False)


def verify_integrity_pattern(disk: Disk, sudo_session: SudoSession, seed: bytes) -> None:
    sudo_session.keep_alive()
    ensure_same_disk(disk)
    process = popen_or_error(
        [
            "sudo",
            "-n",
            "dd",
            "if=" + disk.raw_device,
            "bs=" + str(CHUNK_SIZE),
            "iflag=direct,fullblock",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        preexec_fn=os.setpgrp,
    )
    consumed = 0
    started = time.monotonic()
    try:
        assert process.stdout is not None
        while True:
            chunk = read_up_to(process.stdout, CHUNK_SIZE)
            if not chunk:
                break
            if chunk != integrity_pattern(seed, consumed, len(chunk)):
                raise BurnError("Integrity test data differs at offset {}".format(human_size(consumed)))
            consumed += len(chunk)
            show_progress("Integrity test read", consumed, disk.size, started)
            sudo_session.keep_alive()
        stderr = process.communicate()[1].decode("utf-8", "replace")
    except (KeyboardInterrupt, BurnError, OSError) as exc:
        terminate_process_group(process)
        process.wait()
        if isinstance(exc, KeyboardInterrupt):
            raise
        if isinstance(exc, BurnError):
            raise
        raise BurnError("Integrity verification failed for {}: {}".format(disk.device, exc))
    print()
    if process.returncode != 0:
        raise BurnError("Integrity verification failed for {}: {}".format(disk.device, stderr.strip()))
    if consumed != disk.size:
        raise BurnError(
            "The card reports {}, but only {} could be verified".format(human_size(disk.size), human_size(consumed))
        )


def check_media(disk: Disk, sudo_session: SudoSession) -> bool:
    print(
        "Full write/read integrity test for {} ({}). All data will be erased; this may take a long time.".format(
            disk.device, human_size(disk.size)
        )
    )
    try:
        seed = secrets.token_bytes(32)
        write_integrity_pattern(disk, sudo_session, seed)
        verify_integrity_pattern(disk, sudo_session, seed)
        print("The card passed the full write/read integrity test.")
        return True
    except SudoError:
        raise
    except BurnError as exc:
        eprint("The card failed the integrity test: {}".format(exc))
        return False


def stat_identity(stat_result: os.stat_result) -> Tuple[int, int, int, int]:
    return (
        stat_result.st_dev,
        stat_result.st_ino,
        stat_result.st_size,
        stat_result.st_mtime_ns,
    )


def image_file_identity(path: Path) -> Tuple[int, int, int, int]:
    try:
        return stat_identity(path.stat())
    except OSError as exc:
        raise BurnError("Could not inspect image file {}: {}".format(path, exc))


@contextlib.contextmanager
def verified_source_stream(image: ImageSpec) -> Iterator[Any]:
    try:
        raw = image.path.open("rb")
    except OSError as exc:
        raise BurnError("Could not open image {}: {}".format(image.path, exc))
    with raw:
        initial_identity = stat_identity(os.fstat(raw.fileno()))
        if image.file_identity is not None and initial_identity != image.file_identity:
            raise BurnError("The image file changed after preflight verification")
        if image.compressed_sha256:
            digest = hashlib.sha256()
            while True:
                chunk = raw.read(CHUNK_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
            if digest.hexdigest() != image.compressed_sha256.lower():
                raise BurnError("The image SHA-256 no longer matches the verified checksum")
            raw.seek(0)
        source: Any
        if image.path.name.endswith(".img.xz"):
            source = lzma.LZMAFile(raw, "rb")
        elif image.path.name.endswith(".img"):
            source = raw
        else:
            raise BurnError("Only .img and .img.xz images are supported")
        try:
            yield source
        finally:
            final_identity = stat_identity(os.fstat(raw.fileno()))
            if source is not raw:
                source.close()
            if final_identity != initial_identity:
                raise BurnError("The image file changed while it was being read")


def inspect_image_file(path: Path) -> Tuple[int, Tuple[int, int, int, int]]:
    try:
        raw = path.open("rb")
    except OSError as exc:
        raise BurnError("Could not open image {}: {}".format(path, exc))
    with raw:
        initial_identity = stat_identity(os.fstat(raw.fileno()))
        if path.name.endswith(".img"):
            size = initial_identity[2]
            if size <= 0:
                raise BurnError("The image is empty")
            return size, initial_identity
        if not path.name.endswith(".img.xz"):
            raise BurnError("Only .img and .img.xz images are supported")
        total = 0
        try:
            with lzma.LZMAFile(raw, "rb") as source:
                while True:
                    chunk = source.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    total += len(chunk)
        except (OSError, EOFError, lzma.LZMAError) as exc:
            raise BurnError("Could not verify the decompressed image: {}".format(exc))
        if stat_identity(os.fstat(raw.fileno())) != initial_identity:
            raise BurnError("The image file changed during preflight verification")
        if total <= 0:
            raise BurnError("The decompressed image is empty")
        return total, initial_identity


def uncompressed_image_size(path: Path) -> int:
    return inspect_image_file(path)[0]


def terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def close_process_stdin(process: subprocess.Popen[bytes]) -> None:
    if process.stdin is not None:
        with contextlib.suppress(BrokenPipeError, OSError):
            process.stdin.close()
        # communicate() tries to flush stdin when the attribute is present,
        # even if it has already been closed.
        process.stdin = None


def process_stderr(process: subprocess.Popen[bytes]) -> str:
    try:
        _stdout, stderr = process.communicate()
    except OSError as exc:
        return "could not read stderr: {}".format(exc)
    return (stderr or b"").decode("utf-8", "replace").strip()


def read_disk_block(disk: Disk, block_start: int, length: int, sudo_session: SudoSession) -> bytes:
    """Read one aligned block directly from a previously fingerprinted disk."""
    if block_start < 0 or block_start % CHUNK_SIZE != 0:
        raise BurnError("The diagnostic block offset is not aligned")
    if length < 0 or length > CHUNK_SIZE:
        raise BurnError("The diagnostic block length is invalid")
    sudo_session.keep_alive()
    ensure_same_disk(disk)
    unmount_disk(disk)
    ensure_same_disk(disk)
    process = popen_or_error(
        [
            "sudo",
            "-n",
            "dd",
            "if=" + disk.raw_device,
            "bs=" + str(CHUNK_SIZE),
            "iflag=direct,fullblock",
            "skip={}".format(block_start // CHUNK_SIZE),
            "count=1",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        preexec_fn=os.setpgrp,
    )
    try:
        assert process.stdout is not None
        block = read_up_to(process.stdout, CHUNK_SIZE)
        stderr = process.communicate()[1].decode("utf-8", "replace")
    except (KeyboardInterrupt, BurnError, OSError) as exc:
        terminate_process_group(process)
        process.wait()
        if isinstance(exc, KeyboardInterrupt):
            raise
        if isinstance(exc, BurnError):
            raise
        raise BurnError("Could not repeat the card read at {}: {}".format(human_size(block_start), exc))
    if process.returncode != 0:
        raise BurnError(
            "Could not repeat the card read at {}: {}".format(human_size(block_start), stderr.strip())
        )
    if len(block) < length:
        raise BurnError(
            "Could not repeat the card read at {}: expected {}, got {}".format(
                human_size(block_start), human_size(length), human_size(len(block))
            )
        )
    return block[:length]


def first_difference(expected: bytes, actual: bytes) -> Optional[int]:
    for index, (expected_byte, actual_byte) in enumerate(zip(expected, actual)):
        if expected_byte != actual_byte:
            return index
    if len(expected) != len(actual):
        return min(len(expected), len(actual))
    return None


def verification_mismatch_message(
    offset: int,
    expected_size: int,
    card_size: int,
    source_digest: str,
    card_digest: str,
    expected_block: bytes,
    initial_block: bytes,
    repeated_blocks: Sequence[Optional[bytes]],
    diagnostic_errors: Sequence[Optional[str]],
    classification: str,
    write_time_source_digest: Optional[str] = None,
) -> str:
    lines = [
        "Write verification failed at byte {} ({}): {}.".format(offset, human_size(offset), classification),
        "Expected image bytes: {}; card bytes read: {}.".format(expected_size, card_size),
        "Expected image SHA-256: {}.".format(source_digest),
        "Card SHA-256: {}.".format(card_digest),
        "Expected block SHA-256: {}.".format(hashlib.sha256(expected_block).hexdigest()),
        "Initial card block SHA-256: {}.".format(hashlib.sha256(initial_block).hexdigest()),
    ]
    for number, block in enumerate(repeated_blocks, 1):
        if block is not None:
            lines.append("Repeated card block {} SHA-256: {}.".format(number, hashlib.sha256(block).hexdigest()))
        diagnostic_error = diagnostic_errors[number - 1]
        if diagnostic_error is not None:
            lines.append("Repeated card block {} diagnostic failed: {}.".format(number, diagnostic_error))
    if write_time_source_digest is not None:
        lines.append(
            "Source changed since writing: write-time SHA-256 {}; verification SHA-256 {}.".format(
                write_time_source_digest, source_digest
            )
        )
    return "\n".join(lines)


def verify_written_image(
    disk: Disk,
    image: ImageSpec,
    expected_digest: str,
    expected_size: int,
    sudo_session: SudoSession,
) -> None:
    """Compare a freshly verified image stream directly with uncached card reads."""
    process = None  # type: Optional[subprocess.Popen[bytes]]
    source_digest = hashlib.sha256()
    card_digest = hashlib.sha256()
    source_consumed = 0
    card_consumed = 0
    mismatch_offset = None  # type: Optional[int]
    mismatch_block_start = None  # type: Optional[int]
    mismatch_expected = b""
    mismatch_actual = b""
    started = time.monotonic()
    try:
        with verified_source_stream(image) as source:
            sudo_session.keep_alive()
            ensure_same_disk(disk)
            unmount_disk(disk)
            ensure_same_disk(disk)
            process = popen_or_error(
                [
                    "sudo",
                    "-n",
                    "dd",
                    "if=" + disk.raw_device,
                    "bs=" + str(CHUNK_SIZE),
                    "iflag=direct,fullblock",
                    "count={}".format((expected_size + CHUNK_SIZE - 1) // CHUNK_SIZE),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                preexec_fn=os.setpgrp,
            )
            assert process.stdout is not None
            while source_consumed < expected_size:
                block_start = source_consumed
                block_size = min(CHUNK_SIZE, expected_size - source_consumed)
                expected_block = read_up_to(source, block_size)
                if len(expected_block) != block_size:
                    raise BurnError(
                        "The image size changed before verification: expected {}, got {}".format(
                            human_size(expected_size), human_size(source_consumed + len(expected_block))
                        )
                    )
                actual_block = read_up_to(process.stdout, block_size)
                source_digest.update(expected_block)
                card_digest.update(actual_block)
                source_consumed += len(expected_block)
                card_consumed += len(actual_block)
                if mismatch_offset is None:
                    difference = first_difference(expected_block, actual_block)
                    if difference is not None:
                        mismatch_offset = block_start + difference
                        mismatch_block_start = block_start
                        mismatch_expected = expected_block
                        mismatch_actual = actual_block
                show_progress("Verifying written image", card_consumed, expected_size, started)
                sudo_session.keep_alive()
            if read_up_to(source, 1):
                raise BurnError("The image size changed before verification: it contains more data than expected")
            stderr = process.communicate()[1].decode("utf-8", "replace")
    except (KeyboardInterrupt, BurnError, EOFError, lzma.LZMAError, OSError) as exc:
        if process is not None:
            terminate_process_group(process)
            process.wait()
        if isinstance(exc, KeyboardInterrupt):
            raise
        if isinstance(exc, BurnError):
            raise
        raise BurnError("Could not verify the written image: {}".format(exc))
    print()
    assert process is not None
    if process.returncode != 0:
        raise BurnError("Could not read {}: {}".format(disk.device, stderr.strip()))

    fresh_source_digest = source_digest.hexdigest()
    source_changed = fresh_source_digest != expected_digest
    if source_changed and mismatch_offset is None:
        raise BurnError(
            "Image data changed between writing and verification: expected SHA-256 {}, got {}".format(
                expected_digest, fresh_source_digest
            )
        )
    if mismatch_offset is None and card_consumed != expected_size:
        mismatch_offset = card_consumed
        mismatch_block_start = card_consumed - card_consumed % CHUNK_SIZE
        mismatch_expected = b""
        mismatch_actual = b""
    if mismatch_offset is None:
        if card_digest.hexdigest() != fresh_source_digest:
            raise BurnError("Write verification failed despite byte-for-byte comparison")
        return

    assert mismatch_block_start is not None
    repeated_blocks = []  # type: List[Optional[bytes]]
    diagnostic_errors = []  # type: List[Optional[str]]
    for _attempt in range(2):
        try:
            repeated_blocks.append(
                read_disk_block(disk, mismatch_block_start, len(mismatch_expected), sudo_session)
            )
            diagnostic_errors.append(None)
        except BurnError as exc:
            repeated_blocks.append(None)
            diagnostic_errors.append(str(exc))

    if all(block is not None and block == mismatch_expected for block in repeated_blocks):
        classification = "the initial direct read was transiently inconsistent"
    elif (
        mismatch_actual != mismatch_expected
        and all(block is not None and block == mismatch_actual for block in repeated_blocks)
    ):
        classification = "the card contains stable data that differs from the image"
    elif all(block is not None for block in repeated_blocks):
        classification = "the card, reader, or USB path returned unstable data"
    else:
        classification = "the card data differs from the image"
    raise BurnError(
        verification_mismatch_message(
            mismatch_offset,
            expected_size,
            card_consumed,
            fresh_source_digest,
            card_digest.hexdigest(),
            mismatch_expected,
            mismatch_actual,
            repeated_blocks,
            diagnostic_errors,
            classification,
            expected_digest if source_changed else None,
        )
    )


def write_image(disk: Disk, image: ImageSpec, sudo_session: SudoSession) -> Tuple[str, int]:
    image_size = image.uncompressed_size
    if image_size is None:
        image_size, identity = inspect_image_file(image.path)
        image = dataclasses.replace(image, uncompressed_size=image_size, file_identity=identity)
    if image_size <= 0:
        raise BurnError("The decompressed image is empty")
    if image_size > disk.size:
        raise BurnError(
            "The decompressed image ({}) is larger than the selected card ({})".format(
                human_size(image_size), human_size(disk.size)
            )
        )
    if image.file_identity is None:
        measured_size, identity = inspect_image_file(image.path)
        if measured_size != image_size:
            raise BurnError("The image size changed after preflight verification")
        image = dataclasses.replace(image, file_identity=identity)
    process = None  # type: Optional[subprocess.Popen[bytes]]
    digest = hashlib.sha256()
    written = 0
    started = time.monotonic()
    try:
        with verified_source_stream(image) as source:
            # The source has been opened and re-verified before the first
            # destructive operation, closing the common replace-after-check race.
            sudo_session.keep_alive()
            ensure_same_disk(disk)
            unmount_disk(disk)
            ensure_same_disk(disk)
            process = popen_or_error(
                ["sudo", "-n", "dd", "of=" + disk.raw_device, "bs=" + str(CHUNK_SIZE), "conv=fsync"],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                preexec_fn=os.setpgrp,
            )
            assert process.stdin is not None
            while True:
                chunk = source.read(CHUNK_SIZE)
                if not chunk:
                    break
                if written + len(chunk) > disk.size:
                    raise BurnError("The decompressed image is larger than the selected card")
                write_all(process.stdin, chunk)
                digest.update(chunk)
                written += len(chunk)
                show_progress("Writing Ubuntu", written, 0, started)
                sudo_session.keep_alive()
            if written != image_size:
                raise BurnError(
                    "The image size changed while writing: expected {}, got {}".format(
                        human_size(image_size), human_size(written)
                    )
                )
            process.stdin.close()
            process.stdin = None
            stderr = process.communicate()[1].decode("utf-8", "replace")
    except (KeyboardInterrupt, BurnError, EOFError, lzma.LZMAError, BrokenPipeError, OSError) as exc:
        detail = ""
        returncode = None  # type: Optional[int]
        if process is not None:
            close_process_stdin(process)
            if isinstance(exc, BrokenPipeError):
                # A broken pipe means dd closed its input. Let it finish and
                # preserve its actual diagnostic instead of masking it with
                # Python's secondary EPIPE exception.
                detail = process_stderr(process)
                returncode = process.returncode
                if returncode is None:
                    terminate_process_group(process)
            else:
                if process.poll() is None:
                    terminate_process_group(process)
                detail = process_stderr(process)
                returncode = process.returncode
        if isinstance(exc, KeyboardInterrupt):
            raise
        if isinstance(exc, BurnError):
            raise
        if isinstance(exc, BrokenPipeError) and process is not None:
            suffix = detail or "dd closed its input pipe without an error message"
            if returncode is not None:
                raise BurnError("dd could not write the image (exit code {}): {}".format(returncode, suffix))
            raise BurnError("dd could not write the image: {}".format(suffix))
        raise BurnError("Could not write the image: {}".format(exc))
    print()
    assert process is not None
    if process.returncode != 0:
        raise BurnError("dd could not write the image: {}".format(stderr.strip()))
    run(["sync"], capture=False)
    # Once dd publishes the image's partition table, Disk Arbitration may
    # automatically mount its FAT boot partition and let macOS services modify
    # it.  Unmount immediately so post-flash verification observes the bytes
    # written by dd rather than host-generated filesystem changes.
    unmount_disk(disk)
    ensure_same_disk(disk)
    return digest.hexdigest(), written


def find_boot_mount(disk: Disk) -> Path:
    ensure_same_disk(disk)
    run(["diskutil", "mountDisk", disk.device], check=False)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        listing = diskutil_plist("list", disk.device)
        entries = listing.get("AllDisksAndPartitions", [])
        partitions = []  # type: List[object]
        if isinstance(entries, list) and entries and isinstance(entries[0], dict):
            raw_partitions = entries[0].get("Partitions", [])
            if isinstance(raw_partitions, list):
                partitions = raw_partitions
        for partition in partitions:
            identifier = partition.get("DeviceIdentifier") if isinstance(partition, dict) else None
            if not identifier:
                continue
            info = diskutil_plist("info", "/dev/" + str(identifier))
            mount = info.get("MountPoint")
            volume = str(info.get("VolumeName") or "").lower()
            filesystem = str(info.get("FilesystemType") or info.get("Type (Bundle)") or "").lower()
            if mount and (volume == "system-boot" or "msdos" in filesystem or "fat" in filesystem):
                return Path(str(mount))
        time.sleep(1)
    raise BurnError("Could not mount the Ubuntu boot partition on {}".format(disk.device))


def write_cloud_init(
    disk: Disk,
    hostname: str,
    username: str,
    timezone: str,
    ssh_key: Optional[str],
    password_hash: Optional[str],
    ssid: str,
    wifi_password: str,
) -> None:
    mount = find_boot_mount(disk)
    files = {
        "user-data": render_user_data(hostname, username, timezone, ssh_key, password_hash),
        "network-config": render_network_config(ssid, wifi_password),
        "meta-data": render_meta_data(hostname),
    }
    try:
        for name, contents in files.items():
            path = mount / name
            temporary = mount / ("." + name + ".cluster-burn")
            temporary.write_text(contents, encoding="utf-8")
            os.replace(str(temporary), str(path))
    except OSError as exc:
        raise BurnError("Could not write cloud-init files to {}: {}".format(mount, exc))
    finally:
        run(["sync"], capture=False)


def write_inventory(path: Path, hosts: Sequence[str], username: str) -> None:
    temporary = None  # type: Optional[Path]
    try:
        path = path.expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = ["[raspberry_pi]"]
        lines.extend("{} ansible_user={}".format(host, username) for host in hosts)
        lines.extend(["", "[raspberry_pi:vars]", "ansible_python_interpreter=/usr/bin/python3", ""])
        temporary = path.with_name("." + path.name + ".tmp")
        temporary.write_text("\n".join(lines), encoding="utf-8")
        os.replace(str(temporary), str(path))
    except OSError as exc:
        if temporary is not None:
            with contextlib.suppress(OSError):
                temporary.unlink()
        raise BurnError("Could not write Ansible inventory {}: {}".format(path, exc))


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="piburn",
        description="Flash Ubuntu Server microSD cards for a Raspberry Pi 5 cluster on macOS."
    )
    parser.add_argument("--count", type=int, help="number of cards")
    check_group = parser.add_mutually_exclusive_group()
    check_group.add_argument("--check", dest="check_cards", action="store_true", help="fully test each card")
    check_group.add_argument("--no-check", dest="check_cards", action="store_false", help="skip card integrity tests")
    parser.set_defaults(check_cards=None)
    parser.add_argument("--ssid", help="Wi-Fi network name")
    parser.add_argument(
        "--wifi-password-env",
        metavar="VAR",
        help="read the Wi-Fi password from environment variable VAR (do not pass passwords in argv)",
    )
    parser.add_argument("--prefix", help="hostname prefix; defaults to pi")
    parser.add_argument(
        "--start-number",
        type=int,
        help="number of the first hostname; defaults to 1",
    )
    parser.add_argument("--username", default="pomponchik", help="Linux username (pomponchik)")
    parser.add_argument(
        "--auth-mode",
        choices=("ssh-key", "password"),
        help="use passwordless SSH-key login or one shared password",
    )
    parser.add_argument(
        "--user-password-env",
        metavar="VAR",
        help="read the shared user password from environment variable VAR",
    )
    parser.add_argument("--timezone", help="IANA timezone; defaults to the macOS timezone")
    parser.add_argument("--ssh-public-key", help="path to an SSH public key")
    parser.add_argument(
        "--device",
        action="append",
        default=[],
        help="card device /dev/diskN; repeat for every card",
    )
    parser.add_argument("--image", help="local .img/.img.xz or URL; defaults to the latest stable Ubuntu")
    parser.add_argument("--sha256", help="SHA-256 checksum for --image")
    parser.add_argument(
        "--keep-image-on-failure",
        metavar="DIR",
        help="preserve a downloaded image and diagnostic report under DIR if the run fails",
    )
    inventory_group = parser.add_mutually_exclusive_group()
    inventory_group.add_argument("--inventory", dest="inventory", action="store_true", help="create an inventory")
    inventory_group.add_argument(
        "--no-inventory",
        dest="inventory",
        action="store_false",
        help="do not create an inventory",
    )
    parser.set_defaults(inventory=None)
    parser.add_argument("--inventory-path", help="inventory path (ansible/inventory.ini)")
    parser.add_argument(
        "--on-check-failure",
        choices=("abort", "skip"),
        default="abort",
        help="action after a failed integrity test in --non-interactive mode (abort)",
    )
    parser.add_argument("--yes", action="store_true", help="allow erasing devices passed through --device")
    parser.add_argument("--non-interactive", action="store_true", help="do not prompt; requires all options")
    return parser.parse_args(argv)


def validate_args(args: argparse.Namespace) -> None:
    if args.count is not None and args.count <= 0:
        raise BurnError("--count must be a positive integer")
    if args.start_number is not None and args.start_number <= 0:
        raise BurnError("--start-number must be a positive integer")
    if args.device and args.count is not None and len(args.device) != args.count:
        raise BurnError("The number of --device options must match --count")
    for device in args.device:
        normalize_disk_identifier(device)
    if args.device and not args.yes:
        raise BurnError("Add --yes to allow erasing devices passed through --device")
    if args.sha256 and not re.fullmatch(r"[0-9a-fA-F]{64}", args.sha256):
        raise BurnError("--sha256 must contain exactly 64 hexadecimal characters")
    if args.sha256 and not args.image:
        raise BurnError("--sha256 can only be used together with --image")
    if args.ssh_public_key and args.user_password_env:
        raise BurnError("--ssh-public-key and --user-password-env cannot be used together")
    if args.auth_mode == "ssh-key" and args.user_password_env:
        raise BurnError("--user-password-env is incompatible with --auth-mode ssh-key")
    if args.auth_mode == "password" and args.ssh_public_key:
        raise BurnError("--ssh-public-key is incompatible with --auth-mode password")
    if args.non_interactive:
        effective_auth = args.auth_mode
        if effective_auth is None and args.user_password_env:
            effective_auth = "password"
        elif effective_auth is None and args.ssh_public_key:
            effective_auth = "ssh-key"
        missing = []
        for value, name in (
            (args.count, "--count"),
            (args.check_cards, "--check/--no-check"),
            (args.ssid, "--ssid"),
            (args.wifi_password_env, "--wifi-password-env"),
            (args.prefix, "--prefix"),
            (args.device, "--device"),
            (args.inventory, "--inventory/--no-inventory"),
            (effective_auth, "--auth-mode"),
        ):
            if value is None or value == []:
                missing.append(name)
        if missing:
            raise BurnError("Missing options for --non-interactive: {}".format(", ".join(missing)))
        if effective_auth == "password" and not args.user_password_env:
            raise BurnError("Password login in --non-interactive mode requires --user-password-env")


def choose_disk(heartbeat: Optional[Callable[[], None]] = None) -> Disk:
    latest = {}  # type: Dict[str, Disk]

    def provider() -> Sequence[Tuple[str, str]]:
        latest.clear()
        for disk in list_candidate_disks():
            latest[disk.identifier] = disk
        return [(key, disk.label + " — WILL BE COMPLETELY ERASED") for key, disk in latest.items()]

    identifier = choose_dynamic(
        "Select a memory card:",
        provider,
        refresh_interval=1.0,
        heartbeat=heartbeat,
    )
    return latest.get(identifier) or get_disk(identifier)


def wait_for_disk(
    identifier_or_path: str,
    poll_interval: float = 1.0,
    heartbeat: Optional[Callable[[], None]] = None,
) -> Disk:
    identifier = normalize_disk_identifier(identifier_or_path)
    device_path = "/dev/" + identifier
    waiting_message_shown = False
    while True:
        if heartbeat is not None:
            heartbeat()
        try:
            disk = get_disk(identifier_or_path)
            if waiting_message_shown:
                print("Media detected: {}".format(disk.label))
            return disk
        except BurnError:
            # An existing but unsuitable target is a configuration error, not
            # something that polling can repair.
            if os.path.exists(device_path):
                raise
            if not waiting_message_shown:
                print("Waiting for {}. Insert the next card; press Ctrl+C to quit.".format(device_path))
                waiting_message_shown = True
            time.sleep(poll_interval)


def failed_check_action(heartbeat: Optional[Callable[[], None]] = None) -> bool:
    """Return True to skip the failed check, False to return to disk selection."""
    choice = choose_dynamic(
        "The card failed the integrity test. Choose what to do:",
        lambda: [
            ("back", "return to card selection"),
            ("skip", "skip the test and continue with this card"),
        ],
        heartbeat=heartbeat,
    )
    return choice == "skip"


def diagnostic_source(source: str) -> str:
    parsed = urllib.parse.urlsplit(source)
    if parsed.scheme not in ("http", "https"):
        return source
    hostname = parsed.hostname or ""
    netloc = hostname
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port is not None:
        netloc += ":{}".format(port)
    return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def preserve_failure_artifacts(
    destination: Path,
    image: ImageSpec,
    image_size: Optional[int],
    disk: Optional[Disk],
    error: BaseException,
) -> Tuple[Optional[Path], Path]:
    """Preserve a downloaded image and a secret-free report after a failed run."""
    try:
        destination = destination.expanduser().resolve()
        destination.mkdir(parents=True, exist_ok=True)
        artifact_directory = Path(tempfile.mkdtemp(prefix="piburn-failure-", dir=str(destination)))
        saved_image = None  # type: Optional[Path]
        if urllib.parse.urlsplit(image.source).scheme in ("http", "https") and image.path.exists():
            saved_image = artifact_directory / image.path.name
            if image.path.stat().st_dev != artifact_directory.stat().st_dev:
                raise BurnError(
                    "The downloaded image and failure directory are on different filesystems; "
                    "refusing to copy the image"
                )
            os.replace(str(image.path), str(saved_image))
        report = artifact_directory / "diagnostic.txt"
        lines = [
            "Source: {}".format(diagnostic_source(image.source)),
            "Compressed SHA-256: {}".format(image.compressed_sha256 or "not provided"),
            "Decompressed size: {}".format(image_size if image_size is not None else "unknown"),
        ]
        if disk is not None:
            lines.extend(
                [
                    "Disk: {}".format(disk.device),
                    "Disk fingerprint: {}".format(json.dumps(disk.fingerprint)),
                ]
            )
        lines.extend(
            [
                "Error type: {}".format(type(error).__name__),
                "Error: {}".format(error),
                "",
            ]
        )
        report.write_text("\n".join(lines), encoding="utf-8")
        return saved_image, report
    except OSError as exc:
        raise BurnError("Could not preserve failure artifacts: {}".format(exc))


def main(argv: Optional[Sequence[str]] = None) -> int:
    if sys.platform != "darwin":
        raise BurnError("This script currently supports macOS only")
    args = parse_args(argv)
    validate_args(args)

    count = args.count if args.count is not None else ask_positive_int("How many cards should be flashed? ")
    if args.device and len(args.device) != count:
        raise BurnError("The number of --device options must match the selected card count")
    check_cards = args.check_cards
    if check_cards is None:
        check_cards = ask_yes_no("Run a full integrity test on every card?", default=False)

    ssid = args.ssid
    if not ssid:
        ssid = choose_dynamic(
            "Select a Wi-Fi network or type an SSID:",
            CachedWifiProvider(),
            allow_custom=True,
            refresh_interval=1.0,
        )
    if not ssid:
        raise BurnError("SSID cannot be empty")

    if args.wifi_password_env:
        if args.wifi_password_env not in os.environ:
            raise BurnError("Environment variable {} is not set".format(args.wifi_password_env))
        wifi_password = os.environ[args.wifi_password_env]
    else:
        if args.non_interactive:
            raise BurnError("--non-interactive requires --wifi-password-env")
        wifi_password = getpass.getpass("Wi-Fi password (hidden): ")
    if not wifi_password:
        raise BurnError("Wi-Fi password cannot be empty")

    prefix = args.prefix
    if prefix is None:
        prefix = input("Hostname prefix [pi]: ").strip() or "pi"
    prefix = validate_prefix(prefix)
    start_number = args.start_number
    if start_number is None:
        start_number = 1 if args.non_interactive else ask_positive_int("Starting hostname number [1]: ", default=1)
    username = validate_username(args.username)
    timezone = args.timezone or detect_timezone()

    auth_mode = args.auth_mode
    if auth_mode is None and args.user_password_env:
        auth_mode = "password"
    elif auth_mode is None and args.ssh_public_key:
        auth_mode = "ssh-key"
    if auth_mode is None:
        auth_mode = choose_dynamic(
            "How should SSH login be configured?",
            lambda: [
                ("ssh-key", "passwordless — use an SSH key"),
                ("password", "use the same password on every node"),
            ],
        )

    ssh_key = None  # type: Optional[str]
    password_hash = None  # type: Optional[str]
    if auth_mode == "ssh-key":
        ssh_key, _private_key = find_ssh_public_key(args.ssh_public_key)
    else:
        if args.user_password_env:
            if args.user_password_env not in os.environ:
                raise BurnError("Environment variable {} is not set".format(args.user_password_env))
            user_password = os.environ[args.user_password_env]
        else:
            if args.non_interactive:
                raise BurnError("Password login requires --user-password-env in non-interactive mode")
            user_password = ask_user_password()
        if not user_password:
            raise BurnError("The user password cannot be empty")
        password_hash = sha512_crypt(user_password)
        del user_password

    hosts = []  # type: List[str]
    artifact_destination = Path(args.keep_image_on_failure) if args.keep_image_on_failure else None
    artifact_setup_error = None  # type: Optional[Exception]
    download_parent = None  # type: Optional[Path]
    remote_image_requested = not args.image or urllib.parse.urlsplit(args.image).scheme in ("http", "https")
    if artifact_destination is not None and remote_image_requested:
        try:
            artifact_destination = artifact_destination.expanduser().resolve()
            artifact_destination.mkdir(parents=True, exist_ok=True)
            download_parent = artifact_destination
        except Exception as exc:
            artifact_setup_error = exc
    with tempfile.TemporaryDirectory(
        prefix=".piburn-working-",
        dir=str(download_parent) if download_parent is not None else None,
    ) as temporary_directory:
        image = None  # type: Optional[ImageSpec]
        image_size = None  # type: Optional[int]
        current_disk = None  # type: Optional[Disk]
        last_selected_disk = None  # type: Optional[Disk]
        try:
            print("Finding and verifying the latest stable Ubuntu Server image for Raspberry Pi...")
            image = resolve_image(args.image, args.sha256, Path(temporary_directory))
            print("Image: {}".format(image.source))
            image_size = image.uncompressed_size
            if image_size is None:
                print("Verifying the decompressed image before card selection...")
                image_size, identity = inspect_image_file(image.path)
                image = dataclasses.replace(
                    image,
                    uncompressed_size=image_size,
                    file_identity=identity,
                )
            print("Decompressed image size: {}".format(human_size(image_size)))
            sudo_session = ensure_sudo()

            for card_number in range(1, count + 1):
                hostname = hostname_for(prefix, start_number + card_number - 1)
                print("\nCard {}/{} → {}.local".format(card_number, count, hostname))
                forced_device = args.device[card_number - 1] if args.device else None
                while True:
                    if forced_device:
                        current_disk = wait_for_disk(forced_device, heartbeat=sudo_session.keep_alive)
                    else:
                        current_disk = choose_disk(heartbeat=sudo_session.keep_alive)
                    last_selected_disk = current_disk
                    print("Selected: {}".format(current_disk.label))
                    if not check_cards or check_media(current_disk, sudo_session):
                        break
                    if args.non_interactive:
                        if args.on_check_failure == "skip":
                            break
                        raise BurnError("Card {} failed the integrity test".format(current_disk.device))
                    if failed_check_action(heartbeat=sudo_session.keep_alive):
                        break
                    current_disk = None
                    forced_device = None

                with prevent_automatic_mounts(current_disk, sudo_session):
                    image_digest, image_size = write_image(current_disk, image, sudo_session)
                    if check_cards:
                        print("Verifying the written image...")
                        verify_written_image(
                            current_disk,
                            image,
                            image_digest,
                            image_size,
                            sudo_session,
                        )
                write_cloud_init(
                    current_disk,
                    hostname,
                    username,
                    timezone,
                    ssh_key,
                    password_hash,
                    ssid,
                    wifi_password,
                )
                eject_disk(current_disk)
                print(
                    "Done: {} was ejected; the node will be available as {}.local".format(current_disk.device, hostname)
                )
                hosts.append(hostname + ".local")
                current_disk = None

            create_inventory = args.inventory
            if create_inventory is None:
                create_inventory = ask_yes_no("Generate an Ansible inventory?", default=True)
            inventory_default = args.inventory_path or "ansible/inventory.ini"
            inventory_path = Path(inventory_default)
            if create_inventory:
                if args.inventory is None and args.inventory_path is None and not args.non_interactive:
                    entered = input("Inventory path [{}]: ".format(inventory_default)).strip()
                    if entered:
                        inventory_path = Path(entered)
                write_inventory(inventory_path, hosts, username)
                print("Inventory written to: {}".format(inventory_path.expanduser().resolve()))

            print("\nFlashed cards: {}".format(len(hosts)))
            print("SSH commands:")
            for host in hosts:
                print("ssh {}@{}".format(username, host))
        except BaseException as exc:
            if current_disk is not None:
                try:
                    eject_disk(current_disk, quiet=True)
                except BurnError as eject_error:
                    eprint("Warning: emergency ejection failed: {}".format(eject_error))
            if artifact_destination is not None and image is not None:
                if artifact_setup_error is not None:
                    eprint("Warning: could not prepare failure artifacts: {}".format(artifact_setup_error))
                    raise
                try:
                    saved_image, report = preserve_failure_artifacts(
                        artifact_destination, image, image_size, last_selected_disk, exc
                    )
                    if saved_image is not None:
                        eprint("Failure image preserved at: {}".format(saved_image))
                    else:
                        eprint("Local image remains at: {}".format(image.path))
                    eprint("Diagnostic report written to: {}".format(report))
                except BaseException as preservation_error:
                    eprint("Warning: {}".format(preservation_error))
            raise
    return 0


def entrypoint() -> None:
    """Run the command-line interface and translate expected failures to exit codes."""
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        eprint("\nOperation cancelled by the user (Ctrl+C).")
        sys.exit(130)
    except BurnError as exc:
        eprint("Error: {}".format(exc))
        sys.exit(1)


if __name__ == "__main__":
    entrypoint()
