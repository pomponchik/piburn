import _thread
import argparse
import base64
import dataclasses
import http.client
import io
import json
import lzma
import re
import subprocess
import tempfile
import threading
from pathlib import Path
from unittest import mock

import pytest

from piburn import cli as burn


def test_terminal_parser_handles_fragmented_arrow_sequences():
    parser = burn.TerminalInputParser()
    assert parser.feed(b"\x1b") == []
    assert parser.feed(b"[B") == ["down"]
    assert parser.feed(b"\x1b[B\x1b[B\x1b[A") == ["down", "down", "up"]
    assert parser.buffer == b""


def test_terminal_parser_preserves_fragmented_utf8_text():
    parser = burn.TerminalInputParser()
    encoded = "Сеть".encode("utf-8")
    events = []
    for byte in encoded:
        events.extend(parser.feed(bytes([byte])))
    assert events == ["text:С", "text:е", "text:т", "text:ь"]


def test_selection_survives_refresh_by_value_and_scrolls():
    before = [("disk4", "four"), ("disk5", "five")]
    after = [("disk3", "three"), ("disk4", "four"), ("disk5", "five")]
    assert burn.remap_selection(before, 1, after, True) == 2
    assert burn.remap_selection(before, 1, after[:-1], True) is None
    items = [(str(index), str(index)) for index in range(30)]
    start, visible = burn.selection_window(items, 20)
    assert start <= 20
    assert items[20] in visible
    assert len(visible) == 15


def test_selector_recomputes_filter_before_batched_enter():
    class FakeInput:
        def isatty(self):
            return True

        def fileno(self):
            return 0

    class FakeOutput(io.StringIO):
        def isatty(self):
            return True

    output = FakeOutput()
    with mock.patch.object(burn.sys, "stdin", FakeInput()), mock.patch.object(
        burn.sys, "stdout", output
    ), mock.patch.object(burn, "cbreak_terminal", return_value=burn.contextlib.nullcontext()), mock.patch.object(
        burn.select, "select", return_value=([0], [], [])
    ), mock.patch.object(burn.os, "read", return_value=b"B\r"):
        selected = burn.choose_dynamic("Wi-Fi", lambda: [("A", "Alpha"), ("B", "Beta")], allow_custom=True)
    assert selected == "B"


def test_tty_selector_calls_heartbeat_on_each_wait_iteration():
    """Call the heartbeat on each TTY wait iteration, including candidate refreshes."""

    class FakeInput:
        def isatty(self):
            return True

        def fileno(self):
            return 0

    class FakeOutput(io.StringIO):
        def isatty(self):
            return True

    events = []

    def heartbeat():
        events.append("heartbeat")

    def provider():
        events.append("poll")
        return [("disk4", "SD Card")]

    select_results = [([], [], []), ([], [], []), ([0], [], [])]

    def wait_for_input(_readers, _writers, _errors, timeout):
        events.append(("select", timeout))
        return select_results.pop(0)

    with mock.patch.object(burn.sys, "stdin", FakeInput()), mock.patch.object(
        burn.sys, "stdout", FakeOutput()
    ), mock.patch.object(burn, "cbreak_terminal", return_value=burn.contextlib.nullcontext()), mock.patch.object(
        burn.select,
        "select",
        side_effect=wait_for_input,
    ), mock.patch.object(burn.os, "read", return_value=b"\r"), mock.patch.object(
        burn.time, "monotonic", side_effect=[0.0, 1.1, 1.2]
    ):
        selection = burn.choose_dynamic(
            "Disk",
            provider,
            heartbeat=heartbeat,
        )

    assert selection == "disk4"
    assert events == [
        "heartbeat",
        "poll",
        ("select", 0.5),
        "heartbeat",
        "poll",
        ("select", 0.5),
        "heartbeat",
        ("select", 0.5),
    ]


def test_tty_selector_propagates_heartbeat_failure_before_polling():
    """Propagate a heartbeat failure before the TTY selector's device poll."""

    class FakeInput:
        def isatty(self):
            return True

        def fileno(self):
            return 0

    class FakeOutput(io.StringIO):
        def isatty(self):
            return True

    sudo_error = burn.SudoError("administrator authorization failed")
    heartbeat = mock.Mock(side_effect=sudo_error)
    provider = mock.Mock()
    with mock.patch.object(burn.sys, "stdin", FakeInput()), mock.patch.object(
        burn.sys, "stdout", FakeOutput()
    ), mock.patch.object(
        burn, "cbreak_terminal", return_value=burn.contextlib.nullcontext()
    ), mock.patch.object(burn.select, "select") as select, mock.patch.object(
        burn.os, "read"
    ) as read, pytest.raises(burn.SudoError) as raised:
        burn.choose_dynamic("Disk", provider, heartbeat=heartbeat)

    assert raised.value is sudo_error
    heartbeat.assert_called_once_with()
    provider.assert_not_called()
    select.assert_not_called()
    read.assert_not_called()


@pytest.mark.parametrize("with_heartbeat", [True, False])
def test_non_tty_selector_calls_optional_heartbeat_before_and_after_each_input_attempt(with_heartbeat):
    """Call an optional heartbeat around every non-TTY input attempt."""

    events = []

    def heartbeat():
        events.append("heartbeat")

    answers = iter(["invalid", "1"])

    def answer(_prompt):
        events.append("input")
        return next(answers)

    with mock.patch.object(burn.sys, "stdin", io.StringIO()), mock.patch.object(
        burn.sys, "stdout", io.StringIO()
    ), mock.patch.object(burn, "input", side_effect=answer):
        selection = burn.choose_dynamic(
            "Disk",
            lambda: [("disk4", "SD Card")],
            heartbeat=heartbeat if with_heartbeat else None,
        )

    assert selection == "disk4"
    expected_events = ["heartbeat", "input", "heartbeat", "heartbeat", "input", "heartbeat"]
    if not with_heartbeat:
        expected_events = ["input", "input"]
    assert events == expected_events


def test_non_tty_selector_propagates_heartbeat_failure_before_input():
    """Propagate a heartbeat failure before reading non-TTY input."""

    sudo_error = burn.SudoError("administrator authorization failed")
    heartbeat = mock.Mock(side_effect=sudo_error)
    provider = mock.Mock(return_value=[("disk4", "SD Card")])
    with mock.patch.object(burn.sys, "stdin", io.StringIO()), mock.patch.object(
        burn.sys, "stdout", io.StringIO()
    ), mock.patch.object(burn, "input") as input_mock, pytest.raises(burn.SudoError) as raised:
        burn.choose_dynamic("Disk", provider, heartbeat=heartbeat)

    assert raised.value is sudo_error
    provider.assert_called_once_with()
    heartbeat.assert_called_once_with()
    input_mock.assert_not_called()


def test_choose_disk_forwards_heartbeat_to_selector():
    """Forward labeled candidates and the heartbeat to the selector without calling get_disk."""

    disk = burn.Disk("disk4", "SD Card", 32 * 1024**3, "USB", False, True, True)
    heartbeat = mock.Mock()

    def select(_prompt, provider, **_kwargs):
        assert provider() == [("disk4", disk.label + " — WILL BE COMPLETELY ERASED")]
        return "disk4"

    with mock.patch.object(burn, "list_candidate_disks", return_value=[disk]), mock.patch.object(
        burn, "choose_dynamic", side_effect=select
    ) as choose, mock.patch.object(burn, "get_disk") as get_disk:
        assert burn.choose_disk(heartbeat=heartbeat) == disk

    choose.assert_called_once_with(
        "Select a memory card:",
        mock.ANY,
        refresh_interval=1.0,
        heartbeat=heartbeat,
    )
    get_disk.assert_not_called()


@pytest.mark.parametrize(("action", "expected_result"), [("skip", True), ("back", False)])
def test_failed_check_action_maps_back_and_skip_to_booleans_and_forwards_heartbeat(action, expected_result):
    """Map labeled back and skip actions to False and True, respectively, and forward the heartbeat."""

    heartbeat = mock.Mock()
    with mock.patch.object(burn, "choose_dynamic", return_value=action) as choose:
        assert burn.failed_check_action(heartbeat=heartbeat) is expected_result

    choose.assert_called_once_with(
        "The card failed the integrity test. Choose what to do:",
        mock.ANY,
        heartbeat=heartbeat,
    )
    provider = choose.call_args.args[1]
    assert provider() == [
        ("back", "return to card selection"),
        ("skip", "skip the test and continue with this card"),
    ]


def test_parse_sha256sums_selects_raspberry_pi_image():
    digest = "a" * 64
    name = "ubuntu-26.04-preinstalled-server-arm64+raspi.img.xz"
    parsed = burn.parse_sha256sums("{} *{}\ninvalid".format(digest, name))
    assert parsed[name] == digest
    assert re.search(burn.IMAGE_PATTERN, name)


def test_latest_release_skips_unpublished_development_version():
    original_fetch = burn.fetch_bytes
    image_name = "ubuntu-26.04-preinstalled-server-arm64+raspi.img.xz"
    responses = {
        burn.RELEASES_URL: b'<a href="26.04/">26.04</a><a href="26.10/">26.10</a>',
        burn.RELEASES_URL + "26.04/release/SHA256SUMS": (("b" * 64) + " *" + image_name).encode(),
    }

    def fake_fetch(url):
        if url.endswith("26.10/release/SHA256SUMS"):
            raise burn.FetchError("not released", status=404)
        return responses[url]

    burn.fetch_bytes = fake_fetch
    try:
        url, name, digest = burn.find_latest_release_image()
    finally:
        burn.fetch_bytes = original_fetch
    assert name == image_name
    assert digest == "b" * 64
    assert url == burn.RELEASES_URL + "26.04/release/" + image_name


def test_latest_release_does_not_hide_network_failure():
    original_fetch = burn.fetch_bytes

    def fake_fetch(url):
        if url == burn.RELEASES_URL:
            return b'<a href="26.04/">26.04</a>'
        raise burn.FetchError("timeout")

    burn.fetch_bytes = fake_fetch
    try:
        with pytest.raises(burn.FetchError, match="timeout"):
            burn.find_latest_release_image()
    finally:
        burn.fetch_bytes = original_fetch


@pytest.mark.parametrize(
    "unsafe_override",
    [
        {"Internal": True},
        {"VirtualOrPhysical": "Virtual"},
        {"RemovableMedia": False},
        {"DeviceIdentifier": "disk4s1"},
        {"WholeDisk": False},
        {"WritableMedia": False},
        {"TotalSize": burn.MIN_CARD_SIZE - 1},
    ],
    ids=["internal", "virtual", "fixed-media", "partition-id", "not-whole", "read-only", "too-small"],
)
def test_disk_from_info_builds_card_fingerprint_and_rejects_unsafe_devices(unsafe_override):
    """Build an eligible card's fingerprint and reject each unsafe disk property."""

    valid_disk_info = {
        "DeviceIdentifier": "disk4",
        "WholeDisk": True,
        "Internal": False,
        "RemovableMedia": True,
        "Ejectable": True,
        "WritableMedia": True,
        "VirtualOrPhysical": "Physical",
        "TotalSize": burn.MIN_CARD_SIZE,
        "MediaName": "SD Card",
        "BusProtocol": "USB",
        "DeviceTreePath": "IODeviceTree:/reader/card",
        "MediaUUID": "MEDIA-UUID",
    }
    disk = burn.disk_from_info(valid_disk_info)
    assert disk is not None
    assert disk.fingerprint == (
        "disk4",
        burn.MIN_CARD_SIZE,
        "SD Card",
        "USB",
        "IODeviceTree:/reader/card",
        "MEDIA-UUID",
    )
    assert burn.disk_from_info(dict(valid_disk_info, **unsafe_override)) is None


def test_ensure_same_disk_returns_current_matching_media():
    """Return the current disk when only the ejectable flag, excluded from the fingerprint, differs."""

    selected_disk = burn.Disk(
        "disk4",
        "SD Card",
        32 * 1024**3,
        "USB",
        False,
        True,
        True,
        "IODeviceTree:/reader/card",
        "MEDIA-UUID",
    )
    current_disk = dataclasses.replace(selected_disk, ejectable=False)
    with mock.patch.object(burn, "get_disk", return_value=current_disk) as get_disk:
        assert burn.ensure_same_disk(selected_disk) is current_disk

    get_disk.assert_called_once_with("/dev/disk4")


@pytest.mark.parametrize(
    ("field_name", "replacement_value"),
    [
        ("identifier", "disk5"),
        ("size", 16 * 1024**3),
        ("name", "Different Card"),
        ("protocol", "Thunderbolt"),
        ("device_tree_path", "IODeviceTree:/other-reader/card"),
        ("media_uuid", "OTHER-UUID"),
    ],
)
def test_ensure_same_disk_rejects_changed_fingerprint(field_name, replacement_value):
    """Stop when any fingerprint field differs from the selected media."""

    selected_disk = burn.Disk(
        "disk4",
        "SD Card",
        32 * 1024**3,
        "USB",
        False,
        True,
        True,
        "IODeviceTree:/reader/card",
        "MEDIA-UUID",
    )
    current_disk = dataclasses.replace(selected_disk, **{field_name: replacement_value})
    expected_error_message = "The media in /dev/disk4 changed after selection; the operation was stopped"
    with mock.patch.object(burn, "get_disk", return_value=current_disk) as get_disk, pytest.raises(
        burn.BurnError
    ) as raised:
        burn.ensure_same_disk(selected_disk)

    assert type(raised.value) is burn.BurnError
    assert str(raised.value) == expected_error_message
    get_disk.assert_called_once_with("/dev/disk4")


def test_unmount_disk_runs_diskutil_for_selected_whole_disk():
    """Run diskutil unmountDisk on the selected whole disk."""

    disk = burn.Disk("disk4", "SD Card", 32 * 1024**3, "USB", False, True, True)
    with mock.patch.object(burn, "run") as run:
        burn.unmount_disk(disk)

    run.assert_called_once_with(["diskutil", "unmountDisk", "/dev/disk4"])


def test_wifi_fallback_scans_available_networks_even_with_saved_names():
    preferred = subprocess.CompletedProcess([], 0, b"Preferred networks:\n\tSaved\n", b"")
    current = subprocess.CompletedProcess([], 0, b"You are not associated with an AirPort network.\n", b"")
    profiler_data = json.dumps(
        {"SPAirPortDataType": [{"spairport_other_local_wireless_networks": [{"_name": "NewNetwork"}]}]}
    ).encode()
    profiler = subprocess.CompletedProcess([], 0, profiler_data, b"")

    def fake_run(args, check=True, capture=True, input_data=None):
        if "-getairportnetwork" in args:
            return current
        if "-listpreferredwirelessnetworks" in args:
            return preferred
        if args[0] == "system_profiler":
            return profiler
        raise AssertionError(args)

    with mock.patch.object(burn, "wifi_interface", return_value="en0"), mock.patch.object(
        burn, "run", side_effect=fake_run
    ), mock.patch.object(burn.os.path, "exists", return_value=False), mock.patch.object(
        burn.shutil, "which", return_value="/usr/sbin/system_profiler"
    ):
        assert burn.scan_wifi_networks() == ["NewNetwork", "Saved"]


def test_ssh_public_key_validation_checks_blob_type():
    key_type = b"ssh-ed25519"
    public_key = b"\0" * 32
    blob = len(key_type).to_bytes(4, "big") + key_type + len(public_key).to_bytes(4, "big") + public_key
    valid = "ssh-ed25519 {} test".format(base64.b64encode(blob).decode("ascii"))
    assert burn.valid_ssh_public_key(valid)
    assert not burn.valid_ssh_public_key("ssh-ed25519 not-base64")
    wrong_type = "ssh-rsa {}".format(base64.b64encode(blob).decode("ascii"))
    assert not burn.valid_ssh_public_key(wrong_type)
    empty_blob = len(key_type).to_bytes(4, "big") + key_type
    empty_key = "ssh-ed25519 {}".format(base64.b64encode(empty_blob).decode("ascii"))
    assert not burn.valid_ssh_public_key(empty_key)
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "bad.pub"
        path.write_bytes(b"\xff\xfe")
        with pytest.raises(burn.BurnError, match="SSH public key"):
            burn.find_ssh_public_key(str(path))


def test_cloud_init_uses_safe_hostname_and_hides_password_from_user_data():
    hostname = burn.hostname_for("pi", 1)
    assert hostname == "pi-1"
    user_data = burn.render_user_data(hostname, "pomponchik", "Europe/Moscow", "ssh-ed25519 AAAA test", None)
    network = burn.render_network_config('wifi: "lab"', 'p@ss:"word"')
    assert 'hostname: "pi-1"' in user_data
    assert 'final_message: "cluster node pi-1 is ready"' in user_data
    assert "avahi-daemon" in user_data
    assert "p@ss" not in user_data
    assert '"wifi: \\"lab\\""' in network
    assert '"p@ss:\\"word\\""' in network
    assert "optional: false" in network
    assert "eth0:" in network
    assert "optional: true" in network


def test_password_auth_uses_sha512_crypt_hash():
    password_hash = burn.sha512_crypt("password", "saltstring")
    assert (
        password_hash
        == "$6$saltstring$adDbXsJjcDlq2662QPgd.tkSOVmnG9Tt3oXl4HR60SusC3AGjirnDenVZp3DGwLwqy6iYKCzannhaX9DR72nN1"
    )
    user_data = burn.render_user_data("pi-1", "pomponchik", "UTC", None, password_hash)
    assert "ssh_pwauth: true" in user_data
    assert "lock_passwd: false" in user_data
    assert password_hash in user_data
    assert "ssh_authorized_keys" not in user_data


def test_generated_password_contains_only_letters_and_digits():
    password = burn.generate_password()
    assert len(password) == 20
    assert re.search(r"^[A-Za-z0-9]+$", password)


def test_positive_int_prompt_accepts_default_and_retries_invalid_input(capsys):
    with mock.patch.object(burn, "input", side_effect=["zero", "0", ""]):
        assert burn.ask_positive_int("Starting hostname number [1]: ", default=1) == 1
    assert capsys.readouterr().err.count("Error: enter a positive integer") == 2


def test_inventory_is_replaced():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "ansible" / "inventory.ini"
        path.parent.mkdir()
        path.write_text("old")
        burn.write_inventory(path, ["pi-1.local", "pi-2.local"], "pomponchik")
        contents = path.read_text()
        assert "old" not in contents
        assert "pi-1.local ansible_user=pomponchik" in contents
        assert "[raspberry_pi:vars]" in contents


def test_local_image_extension_is_rejected_before_write():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "not-an-image.txt"
        path.write_bytes(b"data")
        with pytest.raises(burn.BurnError):
            burn.resolve_image(str(path), None, Path(directory))


def test_oversized_image_is_rejected_before_disk_operations(tmp_path):
    """Reject an image declared larger than the card before sudo or card operations."""

    path = tmp_path / "ubuntu.img"
    path.write_bytes(b"ab")
    disk = burn.Disk("disk4", "SD Card", 1, "USB", False, True, True)
    image = burn.ImageSpec(path, None, "test", uncompressed_size=2)
    sudo_session = mock.Mock(spec=burn.SudoSession)
    expected_error_message = "The decompressed image (2.0 B) is larger than the selected card (1.0 B)"
    with mock.patch.object(burn, "ensure_same_disk") as ensure_same_disk, mock.patch.object(
        burn, "unmount_disk"
    ) as unmount, mock.patch.object(burn, "popen_or_error") as popen, pytest.raises(burn.BurnError) as raised:
        burn.write_image(disk, image, sudo_session)
    assert type(raised.value) is burn.BurnError
    assert str(raised.value) == expected_error_message
    sudo_session.authenticate.assert_not_called()
    sudo_session.keep_alive.assert_not_called()
    ensure_same_disk.assert_not_called()
    unmount.assert_not_called()
    popen.assert_not_called()


def test_changed_image_size_is_rejected_before_disk_operations(tmp_path):
    """Reject a local image whose size differs from preflight before sudo or card operations."""

    path = tmp_path / "ubuntu.img"
    path.write_bytes(b"changed")
    disk = burn.Disk("disk4", "SD Card", 1024, "USB", False, True, True)
    stale_image = burn.ImageSpec(path, None, "test", uncompressed_size=3)
    sudo_session = mock.Mock(spec=burn.SudoSession)
    expected_error_message = "The image size changed after preflight verification"
    with mock.patch.object(burn, "ensure_same_disk") as ensure_same_disk, mock.patch.object(
        burn, "unmount_disk"
    ) as unmount, mock.patch.object(burn, "popen_or_error") as popen, pytest.raises(burn.BurnError) as raised:
        burn.write_image(disk, stale_image, sudo_session)
    assert type(raised.value) is burn.BurnError
    assert str(raised.value) == expected_error_message
    sudo_session.authenticate.assert_not_called()
    sudo_session.keep_alive.assert_not_called()
    ensure_same_disk.assert_not_called()
    unmount.assert_not_called()
    popen.assert_not_called()


def test_ensure_sudo_authenticates_once_and_returns_session(capsys):
    """Print password guidance, run one interactive sudo -v, and schedule the first refresh."""

    events = []

    def run_sudo(*_args, **_kwargs):
        events.append("sudo")

    def read_clock():
        events.append("monotonic")
        return 100.0

    with mock.patch.object(burn, "run", side_effect=run_sudo) as run, mock.patch.object(
        burn.time, "monotonic", side_effect=read_clock
    ):
        session = burn.ensure_sudo()

    assert isinstance(session, burn.SudoSession)
    run.assert_called_once_with(["sudo", "-v"], capture=False)
    assert events == ["sudo", "monotonic"]
    assert session.refresh_interval == 60.0
    assert session.next_refresh == 160.0
    assert capsys.readouterr().out == (
        "macOS will normally ask once for an administrator password; "
        "it may ask again if authorization is revoked or expires unusually quickly.\n"
    )


def test_sudo_session_uses_configured_refresh_interval():
    """Use the configured refresh interval after authentication and renewal."""

    session = burn.SudoSession(refresh_interval=17.0)
    successful_sudo_result = subprocess.CompletedProcess(["sudo"], 0)
    events = []
    clock_values = iter([100.0, 117.0, 118.0])

    def run_sudo(*_args, **_kwargs):
        events.append("sudo")
        return successful_sudo_result

    def read_clock():
        events.append("monotonic")
        return next(clock_values)

    with mock.patch.object(burn, "run", side_effect=run_sudo) as run, mock.patch.object(
        burn.time, "monotonic", side_effect=read_clock
    ):
        session.authenticate()
        assert session.next_refresh == 117.0
        session.keep_alive()

    assert run.call_args_list == [
        mock.call(["sudo", "-v"], capture=False),
        mock.call(["sudo", "-n", "-v"], check=False),
    ]
    assert events == ["sudo", "monotonic", "monotonic", "sudo", "monotonic"]
    assert session.next_refresh == 135.0


def test_sudo_session_lifecycle_does_not_start_a_background_thread():
    """Authenticate and renew without starting a thread."""

    successful_sudo_result = subprocess.CompletedProcess(["sudo"], 0)
    thread_start = mock.Mock()
    with mock.patch.object(threading.Thread, "start", thread_start), mock.patch.object(
        threading, "_start_new_thread", thread_start
    ), mock.patch.object(
        _thread, "start_new_thread", thread_start
    ), mock.patch.object(
        _thread, "start_new", thread_start
    ), mock.patch.object(
        burn, "run", return_value=successful_sudo_result
    ) as run, mock.patch.object(
        burn.time, "monotonic", side_effect=[100.0, 160.0, 161.0]
    ):
        session = burn.ensure_sudo()
        session.keep_alive()

    thread_start.assert_not_called()
    assert run.call_args_list == [
        mock.call(["sudo", "-v"], capture=False),
        mock.call(["sudo", "-n", "-v"], check=False),
    ]
    assert session.next_refresh == 221.0


def test_sudo_session_does_not_refresh_before_deadline():
    """Do not run sudo before the refresh deadline."""

    session = burn.SudoSession()
    session.next_refresh = 160.0
    with mock.patch.object(burn, "run") as run, mock.patch.object(
        burn.time, "monotonic", side_effect=[100.0, 159.999]
    ):
        session.keep_alive()
        session.keep_alive()

    run.assert_not_called()
    assert session.next_refresh == 160.0


def test_sudo_session_refreshes_noninteractively_when_due(capsys):
    """Refresh silently at most once per overdue heartbeat and reschedule from completion."""

    session = burn.SudoSession()
    session.next_refresh = 160.0
    successful_sudo_result = subprocess.CompletedProcess(["sudo"], 0)
    with mock.patch.object(
        burn, "run", return_value=successful_sudo_result
    ) as run, mock.patch.object(
        burn.time,
        "monotonic",
        side_effect=[400.0, 401.0, 460.999, 461.0, 462.0],
    ):
        session.keep_alive()
        session.keep_alive()
        session.keep_alive()

    assert run.call_args_list == [
        mock.call(["sudo", "-n", "-v"], check=False),
        mock.call(["sudo", "-n", "-v"], check=False),
    ]
    assert session.next_refresh == 522.0
    captured_output = capsys.readouterr()
    assert captured_output.out == ""
    assert captured_output.err == ""


@pytest.mark.parametrize("returncode", [1, 2, -15])
def test_sudo_session_reauthenticates_when_noninteractive_refresh_returns_nonzero(returncode, capsys):
    """If sudo -n -v returns nonzero, warn, run one interactive sudo -v, and reschedule on success."""

    session = burn.SudoSession()
    session.next_refresh = 60.0
    failed_refresh = subprocess.CompletedProcess(["sudo"], returncode)
    reauthentication_result = subprocess.CompletedProcess(["sudo"], 0)
    events = []
    clock_values = iter([60.0, 61.0])
    sudo_results = iter([failed_refresh, reauthentication_result])
    original_print = print

    def run_sudo(*_args, **_kwargs):
        events.append("sudo")
        return next(sudo_results)

    def read_clock():
        events.append("monotonic")
        return next(clock_values)

    def record_print(*args, **kwargs):
        events.append("warning")
        original_print(*args, **kwargs)

    with mock.patch.object(burn, "run", side_effect=run_sudo) as run, mock.patch.object(
        burn.time, "monotonic", side_effect=read_clock
    ), mock.patch("builtins.print", side_effect=record_print):
        session.keep_alive()

    assert run.call_args_list == [
        mock.call(["sudo", "-n", "-v"], check=False),
        mock.call(["sudo", "-v"], capture=False),
    ]
    assert events == ["monotonic", "sudo", "warning", "sudo", "monotonic"]
    assert session.next_refresh == 121.0
    assert capsys.readouterr().out == (
        "Administrator authorization expired; macOS will ask for the password again.\n"
    )


def test_sudo_session_preserves_deadline_and_raises_sudo_error_when_reauthentication_fails():
    """Preserve the expired deadline and wrap failed reauthentication in SudoError."""

    session = burn.SudoSession()
    session.next_refresh = 59.0
    failed_refresh = subprocess.CompletedProcess(["sudo"], 1)
    authentication_error = burn.BurnError("authentication failed")
    with mock.patch.object(
        burn,
        "run",
        side_effect=[failed_refresh, authentication_error],
    ) as run, mock.patch.object(burn.time, "monotonic", return_value=60.0), pytest.raises(
        burn.SudoError,
        match="authentication failed",
    ) as raised:
        session.keep_alive()

    assert run.call_args_list == [
        mock.call(["sudo", "-n", "-v"], check=False),
        mock.call(["sudo", "-v"], capture=False),
    ]
    assert raised.value.__cause__ is authentication_error
    assert session.next_refresh == 59.0


@pytest.mark.parametrize("failing_stage", ["write", "verify"])
def test_check_media_does_not_report_sudo_failure_as_bad_card(failing_stage, capsys):
    """Propagate either integrity stage's SudoError without blaming media."""

    disk = burn.Disk("disk4", "SD Card", 1024, "USB", False, True, True)
    sudo_session = mock.Mock(spec=burn.SudoSession)
    sudo_error = burn.SudoError("administrator authorization failed")
    seed = b"s" * 32
    write_side_effect = sudo_error if failing_stage == "write" else None
    verify_side_effect = sudo_error if failing_stage == "verify" else None
    with mock.patch.object(burn.secrets, "token_bytes", return_value=seed), mock.patch.object(
        burn, "write_integrity_pattern", side_effect=write_side_effect
    ) as write, mock.patch.object(
        burn, "verify_integrity_pattern", side_effect=verify_side_effect
    ) as verify, pytest.raises(burn.SudoError) as raised:
        burn.check_media(disk, sudo_session)

    assert raised.value is sudo_error
    sudo_session.authenticate.assert_not_called()
    write.assert_called_once_with(disk, sudo_session, seed)
    if failing_stage == "write":
        verify.assert_not_called()
    else:
        verify.assert_called_once_with(disk, sudo_session, seed)
    captured_output = capsys.readouterr()
    assert captured_output.err == ""
    assert "The card failed the integrity test" not in captured_output.out


@pytest.mark.parametrize("failing_stage", ["write", "verify"])
def test_check_media_returns_false_and_reports_integrity_errors(failing_stage, capsys):
    """Return False and report either integrity stage's BurnError, except SudoError."""

    disk = burn.Disk("disk4", "SD Card", 1024, "USB", False, True, True)
    sudo_session = mock.Mock(spec=burn.SudoSession)
    media_error = burn.BurnError("media corrupted")
    seed = b"s" * 32
    write_side_effect = media_error if failing_stage == "write" else None
    verify_side_effect = media_error if failing_stage == "verify" else None
    with mock.patch.object(burn.secrets, "token_bytes", return_value=seed), mock.patch.object(
        burn, "write_integrity_pattern", side_effect=write_side_effect
    ) as write, mock.patch.object(
        burn, "verify_integrity_pattern", side_effect=verify_side_effect
    ) as verify:
        media_ok = burn.check_media(disk, sudo_session)

    assert media_ok is False
    sudo_session.authenticate.assert_not_called()
    write.assert_called_once_with(disk, sudo_session, seed)
    if failing_stage == "write":
        verify.assert_not_called()
    else:
        verify.assert_called_once_with(disk, sudo_session, seed)
    captured_output = capsys.readouterr()
    assert captured_output.err == "The card failed the integrity test: media corrupted\n"
    assert "The card passed the full write/read integrity test." not in captured_output.out


def test_check_media_returns_true_and_reports_success_after_both_stages(capsys):
    """Return True and report success when both integrity stages pass."""

    disk = burn.Disk("disk4", "SD Card", 1024, "USB", False, True, True)
    sudo_session = mock.Mock(spec=burn.SudoSession)
    seed = b"s" * 32
    with mock.patch.object(burn.secrets, "token_bytes", return_value=seed) as token_bytes, mock.patch.object(
        burn, "write_integrity_pattern"
    ) as write, mock.patch.object(
        burn, "verify_integrity_pattern"
    ) as verify:
        media_ok = burn.check_media(disk, sudo_session)

    assert media_ok is True
    sudo_session.authenticate.assert_not_called()
    token_bytes.assert_called_once_with(32)
    write.assert_called_once_with(disk, sudo_session, seed)
    verify.assert_called_once_with(disk, sudo_session, seed)
    captured_output = capsys.readouterr()
    assert captured_output.err == ""
    assert captured_output.out.endswith("The card passed the full write/read integrity test.\n")


def test_integrity_verifier_rejects_data_written_with_a_different_seed():
    """Prove that both real integrity stages use their supplied seed."""

    disk = burn.Disk("disk4", "SD Card", 10, "USB", False, True, True)
    write_seed = bytes(range(32))
    verify_seed = bytes(reversed(range(32)))
    write_process = FakeDDProcess(require_closed_stdin=True)
    read_process = FakeDDProcess()
    sudo_session = mock.Mock(spec=burn.SudoSession)
    with mock.patch.object(burn, "CHUNK_SIZE", 4), mock.patch.object(
        burn, "INTEGRITY_PATTERN_BLOCK_SIZE", 4
    ), mock.patch.object(burn, "ensure_same_disk"), mock.patch.object(
        burn, "unmount_disk"
    ), mock.patch.object(
        burn, "popen_or_error", side_effect=[write_process, read_process]
    ) as popen, mock.patch.object(burn, "show_progress"), mock.patch.object(
        burn, "run"
    ), mock.patch.object(burn.os, "killpg") as killpg:
        expected_written_data = burn.integrity_pattern(write_seed, 0, disk.size)
        burn.write_integrity_pattern(disk, sudo_session, write_seed)
        written_data = write_process.input_stream.getvalue()
        read_process.stdout = RecordingOutput(written_data, [])
        with pytest.raises(burn.BurnError) as raised:
            burn.verify_integrity_pattern(disk, sudo_session, verify_seed)

    assert written_data == expected_written_data
    assert str(raised.value) == "Integrity test data differs at offset 0.0 B"
    assert popen.call_count == 2
    killpg.assert_called_once_with(read_process.pid, burn.signal.SIGTERM)
    assert read_process.wait_calls == 2


@pytest.mark.parametrize(
    ("action", "expected_call"),
    [
        ("authenticate", mock.call(["sudo", "-v"], capture=False)),
        ("refresh", mock.call(["sudo", "-n", "-v"], check=False)),
    ],
)
def test_sudo_session_wraps_command_errors_without_moving_deadline(action, expected_call):
    """Wrap sudo launch errors in SudoError without moving the refresh deadline."""

    session = burn.SudoSession()
    session.next_refresh = 59.0
    command_error = burn.BurnError("could not start sudo")
    with mock.patch.object(burn, "run", side_effect=command_error) as run, mock.patch.object(
        burn.time, "monotonic", return_value=60.0
    ), pytest.raises(burn.SudoError, match="could not start sudo") as raised:
        if action == "authenticate":
            session.authenticate()
        else:
            session.keep_alive()

    assert raised.value.__cause__ is command_error
    assert run.call_args_list == [expected_call]
    assert session.next_refresh == 59.0


class RecordingInput(io.BytesIO):
    """Retain pipe contents after production code closes the fake stdin."""

    def __init__(self, events):
        super().__init__()
        self.events = events
        self.was_closed = False

    def write(self, data):
        self.events.append("block")
        return super().write(data)

    def close(self):
        self.was_closed = True


class RecordingOutput(io.BytesIO):
    """Record each non-empty block consumed from fake dd stdout."""

    def __init__(self, data, events):
        super().__init__(data)
        self.events = events

    def read(self, size=-1):
        data = super().read(size)
        if data:
            self.events.append("block")
        return data


class FakeDDProcess:
    """Model the small subprocess surface used by the transfer functions."""

    def __init__(
        self,
        output=b"",
        events=None,
        stderr=b"",
        final_returncode=0,
        completed=False,
        require_closed_stdin=False,
    ):
        self.events = [] if events is None else events
        self.input_stream = RecordingInput(self.events)
        self.stdin = self.input_stream
        self.stdout = RecordingOutput(output, self.events)
        self.final_returncode = final_returncode
        self.returncode = final_returncode if completed else None
        self.pid = 4321
        self.communicate_calls = 0
        self.wait_calls = 0
        self.stderr = stderr
        self.require_closed_stdin = require_closed_stdin

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.wait_calls += 1
        if self.returncode is None:
            self.returncode = -15
        return self.returncode

    def communicate(self):
        if self.require_closed_stdin:
            assert self.input_stream.was_closed is True
            assert self.stdin is None
        self.communicate_calls += 1
        if self.returncode is None:
            self.returncode = self.final_returncode
        return None, self.stderr


def test_write_all_retries_short_pipe_writes():
    """Retry partial pipe writes until the complete block has been accepted."""

    class ShortWriter:
        def __init__(self):
            self.data = bytearray()
            self.calls = 0

        def write(self, data):
            self.calls += 1
            accepted = bytes(data[:2])
            self.data.extend(accepted)
            return len(accepted)

    stream = ShortWriter()

    burn.write_all(stream, b"abcdef")

    assert bytes(stream.data) == b"abcdef"
    assert stream.calls == 3


@pytest.mark.parametrize(
    ("write_results", "expected_write_count"),
    [([None], 1), ([0], 1), ([-1], 1), ([5], 1), ([2, 3], 2)],
    ids=["none", "zero", "negative", "oversized-block", "oversized-remainder"],
)
def test_write_all_rejects_invalid_progress(write_results, expected_write_count):
    """Reject pipe results that make no valid forward progress."""

    stream = mock.Mock()
    stream.write.side_effect = write_results

    with pytest.raises(OSError, match="did not accept the complete block"):
        burn.write_all(stream, b"abcd")

    assert stream.write.call_count == expected_write_count


@pytest.mark.parametrize("operation", ["integrity-write", "image-write"])
def test_write_paths_retry_partial_pipe_writes(operation):
    """Send every source byte when a write-path pipe accepts only short fragments."""

    class PartialInput(io.BytesIO):
        def __init__(self):
            super().__init__()
            self.was_closed = False

        def write(self, data):
            return super().write(bytes(data[:2]))

        def close(self):
            self.was_closed = True

    data = b"abcdefghij"
    disk = burn.Disk("disk4", "SD Card", len(data), "USB", False, True, True)
    image = burn.ImageSpec(Path("ubuntu.img"), None, "test", uncompressed_size=len(data), file_identity=(1, 2, 10, 3))
    process = FakeDDProcess(require_closed_stdin=True)
    process.input_stream = PartialInput()
    process.stdin = process.input_stream
    sudo_session = mock.Mock(spec=burn.SudoSession)
    heartbeat_payload_sizes = []
    progress_payload_sizes = []
    sudo_session.keep_alive.side_effect = lambda: heartbeat_payload_sizes.append(
        len(process.input_stream.getvalue())
    )
    with mock.patch.object(burn, "CHUNK_SIZE", 4), mock.patch.object(
        burn, "INTEGRITY_PATTERN_BLOCK_SIZE", 4
    ), mock.patch.object(burn, "ensure_same_disk"), mock.patch.object(
        burn, "unmount_disk"
    ), mock.patch.object(burn, "popen_or_error", return_value=process), mock.patch.object(
        burn,
        "show_progress",
        side_effect=lambda *_args: progress_payload_sizes.append(
            len(process.input_stream.getvalue())
        ),
    ) as progress, mock.patch.object(burn, "run"), mock.patch.object(
        burn,
        "verified_source_stream",
        return_value=burn.contextlib.nullcontext(io.BytesIO(data)),
    ):
        result = run_transfer(operation, disk, image, sudo_session)
        expected = (
            burn.integrity_pattern(b"s" * 32, 0, len(data))
            if operation == "integrity-write"
            else data
        )

    assert process.input_stream.getvalue() == expected
    assert process.input_stream.was_closed is True
    assert [call.args[1] for call in progress.call_args_list] == [4, 8, 10]
    assert progress_payload_sizes == [4, 8, 10]
    assert heartbeat_payload_sizes == [0, 4, 8, 10]
    if operation == "image-write":
        assert result == (burn.hashlib.sha256(data).hexdigest(), len(data))
    else:
        assert result is None


def run_transfer(operation, disk, image, sudo_session):
    """Invoke one transfer path while keeping parametrized tests readable."""

    seed = b"s" * 32
    if operation == "integrity-write":
        return burn.write_integrity_pattern(disk, sudo_session, seed)
    if operation == "integrity-read":
        return burn.verify_integrity_pattern(disk, sudo_session, seed)
    if operation == "image-write":
        return burn.write_image(disk, image, sudo_session)
    if operation == "image-read":
        return burn.verify_written_image(
            disk,
            image,
            burn.hashlib.sha256(b"abcdefghij").hexdigest(),
            image.uncompressed_size,
            sudo_session,
        )
    raise AssertionError("unknown transfer operation: {}".format(operation))


@pytest.mark.parametrize("operation", ["integrity-write", "integrity-read", "image-write", "image-read"])
def test_long_transfer_preserves_safety_and_heartbeat_contract(operation):
    """Check each transfer's exact sudo -n dd command; heartbeat, fingerprint,
    and unmount order; per-block data, progress, and heartbeats; cleanup; sync;
    and result.
    """

    data = b"abcdefghij"
    is_write = operation.endswith("write")
    disk = burn.Disk("disk4", "SD Card", len(data), "USB", False, True, True)
    image = burn.ImageSpec(
        Path("ubuntu.img"),
        None,
        "test",
        uncompressed_size=len(data),
        file_identity=(1, 2, len(data), 3),
    )
    events = []
    image_read_output = data + b"XY"
    process = FakeDDProcess(
        image_read_output if operation == "image-read" else b"",
        events=events,
        require_closed_stdin=is_write,
    )
    sudo_session = mock.Mock(spec=burn.SudoSession)
    sudo_session.keep_alive.side_effect = lambda: events.append("heartbeat")

    def check_disk(_disk):
        events.append("fingerprint")

    def unmount_card(_disk):
        events.append("unmount")

    def start_dd(*_args, **_kwargs):
        assert sudo_session.keep_alive.call_count == 1
        events.append("dd")
        return process

    def run_system_command(*_args, **_kwargs):
        assert process.communicate_calls == 1
        assert process.input_stream.was_closed is True
        assert process.stdin is None
        events.append("sync")

    with mock.patch.object(burn, "CHUNK_SIZE", 4), mock.patch.object(
        burn, "INTEGRITY_PATTERN_BLOCK_SIZE", 4
    ), mock.patch.object(burn, "ensure_same_disk", side_effect=check_disk) as ensure_same_disk, mock.patch.object(
        burn, "unmount_disk", side_effect=unmount_card
    ) as unmount, mock.patch.object(
        burn, "popen_or_error", side_effect=start_dd
    ) as popen, mock.patch.object(burn, "show_progress") as progress, mock.patch.object(
        burn, "run", side_effect=run_system_command
    ) as run, mock.patch.object(burn.time, "monotonic", return_value=123.0), mock.patch.object(
        burn,
        "verified_source_stream",
        return_value=burn.contextlib.nullcontext(io.BytesIO(data)),
    ) as source_stream:
        expected_integrity_payload = burn.integrity_pattern(b"s" * 32, 0, len(data))
        if operation == "integrity-read":
            process.stdout = RecordingOutput(expected_integrity_payload, events)
        transfer_result = run_transfer(operation, disk, image, sudo_session)

    assert sudo_session.keep_alive.call_args_list == [mock.call()] * 4
    sudo_session.authenticate.assert_not_called()
    expected_events = ["heartbeat", "fingerprint"]
    if is_write:
        expected_events.extend(["unmount", "fingerprint"])
    expected_events.extend(["dd", "block", "heartbeat", "block", "heartbeat", "block", "heartbeat"])
    if is_write:
        expected_events.append("sync")
    assert events == expected_events
    progress_label, progress_total = {
        "integrity-write": ("Integrity test write", len(data)),
        "integrity-read": ("Integrity test read", len(data)),
        "image-write": ("Writing Ubuntu", 0),
        "image-read": ("Verifying written image", len(data)),
    }[operation]
    assert progress.call_args_list == [
        mock.call(progress_label, completed_bytes, progress_total, 123.0)
        for completed_bytes in (4, 8, 10)
    ]
    device_arg_prefix = "of=" if is_write else "if="
    expected_command = ["sudo", "-n", "dd", device_arg_prefix + "/dev/rdisk4", "bs=4"]
    if is_write:
        expected_command.append("conv=fsync")
    else:
        expected_command.append("iflag=direct,fullblock")
    if operation == "image-read":
        expected_command.append("count=3")
    expected_popen_kwargs = {
        "stdout": burn.subprocess.DEVNULL if is_write else burn.subprocess.PIPE,
        "stderr": burn.subprocess.PIPE,
        "preexec_fn": burn.os.setpgrp,
    }
    if is_write:
        expected_popen_kwargs["stdin"] = burn.subprocess.PIPE
    popen.assert_called_once_with(expected_command, **expected_popen_kwargs)
    assert process.communicate_calls == 1
    expected_disk_check_count = 2 if is_write else 1
    assert ensure_same_disk.call_args_list == [mock.call(disk)] * expected_disk_check_count

    if is_write:
        unmount.assert_called_once_with(disk)
        run.assert_called_once_with(["sync"], capture=False)
        expected_payload = expected_integrity_payload if operation == "integrity-write" else data
        assert process.input_stream.getvalue() == expected_payload
        assert process.input_stream.was_closed is True
        assert process.stdin is None
    else:
        unmount.assert_not_called()
        run.assert_not_called()
        assert process.stdout.tell() == len(data)

    if operation == "image-write":
        source_stream.assert_called_once_with(image)
        assert transfer_result == (burn.hashlib.sha256(data).hexdigest(), len(data))
    elif operation == "image-read":
        source_stream.assert_called_once_with(image)
        assert transfer_result is None
    else:
        source_stream.assert_not_called()
        assert transfer_result is None


@pytest.mark.parametrize(("byte_limit", "expected_dd_block_count"), [(8, 2), (9, 3)])
def test_verify_written_image_uses_image_size_instead_of_full_card_size(byte_limit, expected_dd_block_count):
    """Compare exactly the image bytes and clamp progress despite whole-block dd reads."""

    disk = burn.Disk("disk4", "SD Card", 32, "USB", False, True, True)
    requested_data = bytes(range(byte_limit))
    dd_output = requested_data + b"X" * (expected_dd_block_count * 4 - byte_limit)
    process = FakeDDProcess(dd_output)
    sudo_session = mock.Mock(spec=burn.SudoSession)
    with mock.patch.object(burn, "CHUNK_SIZE", 4), mock.patch.object(
        burn, "ensure_same_disk"
    ) as ensure_same_disk, mock.patch.object(
        burn, "popen_or_error", return_value=process
    ) as popen, mock.patch.object(burn, "show_progress") as progress, mock.patch.object(
        burn,
        "verified_source_stream",
        return_value=burn.contextlib.nullcontext(io.BytesIO(requested_data)),
    ) as source_stream:
        result = burn.verify_written_image(
            disk,
            burn.ImageSpec(Path("ubuntu.img"), None, "test", uncompressed_size=byte_limit),
            burn.hashlib.sha256(requested_data).hexdigest(),
            byte_limit,
            sudo_session,
        )

    expected_command = [
        "sudo",
        "-n",
        "dd",
        "if=/dev/rdisk4",
        "bs=4",
        "iflag=direct,fullblock",
        "count={}".format(expected_dd_block_count),
    ]
    assert popen.call_args.args[0] == expected_command
    assert result is None
    assert process.stdout.tell() == byte_limit
    ensure_same_disk.assert_called_once_with(disk)
    assert sudo_session.keep_alive.call_args_list == [mock.call()] * (expected_dd_block_count + 1)
    sudo_session.authenticate.assert_not_called()
    source_stream.assert_called_once()
    completed_byte_counts = [
        min(block_number * 4, byte_limit)
        for block_number in range(1, expected_dd_block_count + 1)
    ]
    assert progress.call_args_list == [
        mock.call("Verifying written image", completed_bytes, byte_limit, mock.ANY)
        for completed_bytes in completed_byte_counts
    ]


def test_verify_written_image_joins_short_source_and_card_reads():
    """Accept matching non-aligned data when both streams return short reads."""

    class FragmentedReader(io.BytesIO):
        def __init__(self, data, fragment_size):
            super().__init__(data)
            self.fragment_size = fragment_size

        def read(self, size=-1):
            return super().read(min(size, self.fragment_size) if size >= 0 else self.fragment_size)

    data = b"abcdefghij"
    source = FragmentedReader(data, 2)
    process = FakeDDProcess()
    process.stdout = FragmentedReader(data, 3)
    disk = burn.Disk("disk4", "SD Card", 32, "USB", False, True, True)
    image = burn.ImageSpec(Path("ubuntu.img"), None, "test", uncompressed_size=len(data))
    sudo_session = mock.Mock(spec=burn.SudoSession)
    with mock.patch.object(burn, "CHUNK_SIZE", 4), mock.patch.object(
        burn, "ensure_same_disk"
    ), mock.patch.object(burn, "popen_or_error", return_value=process), mock.patch.object(
        burn, "show_progress"
    ) as progress, mock.patch.object(
        burn,
        "verified_source_stream",
        return_value=burn.contextlib.nullcontext(source),
    ):
        result = burn.verify_written_image(
            disk,
            image,
            burn.hashlib.sha256(data).hexdigest(),
            len(data),
            sudo_session,
        )

    assert result is None
    assert source.tell() == len(data)
    assert process.stdout.tell() == len(data)
    assert [call.args[1] for call in progress.call_args_list] == [4, 8, 10]
    assert sudo_session.keep_alive.call_args_list == [mock.call()] * 4


def test_verify_written_image_reopens_and_revalidates_source(tmp_path):
    """Recheck the compressed checksum and decompress the original image for comparison."""

    data = b"abcdefghij"
    compressed_data = lzma.compress(data)
    image_path = tmp_path / "ubuntu.img.xz"
    image_path.write_bytes(compressed_data)
    image = burn.ImageSpec(
        image_path,
        burn.hashlib.sha256(compressed_data).hexdigest(),
        "https://example.com/ubuntu.img.xz",
        uncompressed_size=len(data),
        file_identity=burn.image_file_identity(image_path),
    )
    disk = burn.Disk("disk4", "SD Card", 32, "USB", False, True, True)
    process = FakeDDProcess(data)
    sudo_session = mock.Mock(spec=burn.SudoSession)
    with mock.patch.object(burn, "CHUNK_SIZE", 4), mock.patch.object(
        burn, "ensure_same_disk"
    ) as ensure_same_disk, mock.patch.object(
        burn, "popen_or_error", return_value=process
    ) as popen, mock.patch.object(
        burn, "show_progress"
    ):
        result = burn.verify_written_image(
            disk,
            image,
            burn.hashlib.sha256(data).hexdigest(),
            len(data),
            sudo_session,
        )
        sudo_session.reset_mock()
        ensure_same_disk.reset_mock()
        popen.reset_mock()
        invalid_checksum_image = dataclasses.replace(image, compressed_sha256="0" * 64)
        with pytest.raises(burn.BurnError, match="no longer matches the verified checksum"):
            burn.verify_written_image(
                disk,
                invalid_checksum_image,
                burn.hashlib.sha256(data).hexdigest(),
                len(data),
                sudo_session,
            )

    assert result is None
    assert process.stdout.tell() == len(data)
    sudo_session.keep_alive.assert_not_called()
    ensure_same_disk.assert_not_called()
    popen.assert_not_called()


def test_verify_written_image_stops_dd_after_decompression_failure(tmp_path):
    """Stop and reap dd if the freshly checksummed XZ stream cannot be decompressed."""

    data = b"abcdefghij"
    truncated_xz = lzma.compress(data)[:-1]
    image_path = tmp_path / "ubuntu.img.xz"
    image_path.write_bytes(truncated_xz)
    image = burn.ImageSpec(
        image_path,
        burn.hashlib.sha256(truncated_xz).hexdigest(),
        "https://example.com/ubuntu.img.xz",
        uncompressed_size=len(data),
        file_identity=burn.image_file_identity(image_path),
    )
    disk = burn.Disk("disk4", "SD Card", 32, "USB", False, True, True)
    process = FakeDDProcess(data)
    sudo_session = mock.Mock(spec=burn.SudoSession)
    with mock.patch.object(burn, "CHUNK_SIZE", 4), mock.patch.object(
        burn, "ensure_same_disk"
    ), mock.patch.object(
        burn, "popen_or_error", return_value=process
    ) as popen, mock.patch.object(burn, "show_progress"), mock.patch.object(
        burn.os, "killpg"
    ) as killpg, pytest.raises(burn.BurnError) as raised:
        burn.verify_written_image(
            disk,
            image,
            burn.hashlib.sha256(data).hexdigest(),
            len(data),
            sudo_session,
        )

    assert str(raised.value) == (
        "Could not verify the written image: "
        "Compressed file ended before the end-of-stream marker was reached"
    )
    popen.assert_called_once()
    killpg.assert_called_once_with(process.pid, burn.signal.SIGTERM)
    assert process.wait_calls == 2
    assert process.stdout.tell() == len(data)


@pytest.mark.parametrize(
    (
        "source_data",
        "expected_message",
        "expected_card_bytes_read",
        "expected_progress_calls",
        "expected_heartbeat_count",
    ),
    [
        (
            b"abc",
            "The image size changed before verification: expected 4.0 B, got 3.0 B",
            0,
            [],
            1,
        ),
        (
            b"abcde",
            "The image size changed before verification: it contains more data than expected",
            4,
            [mock.call("Verifying written image", 4, 4, mock.ANY)],
            2,
        ),
    ],
    ids=["truncated", "extended"],
)
def test_verify_written_image_rejects_source_size_changes(
    source_data,
    expected_message,
    expected_card_bytes_read,
    expected_progress_calls,
    expected_heartbeat_count,
):
    """Reject a source that is shorter or longer than its write-time size."""

    disk = burn.Disk("disk4", "SD Card", 32, "USB", False, True, True)
    image = burn.ImageSpec(Path("ubuntu.img"), None, "test", uncompressed_size=4)
    process = FakeDDProcess(b"abcd")
    sudo_session = mock.Mock(spec=burn.SudoSession)
    with mock.patch.object(burn, "CHUNK_SIZE", 4), mock.patch.object(
        burn, "ensure_same_disk"
    ), mock.patch.object(burn, "popen_or_error", return_value=process), mock.patch.object(
        burn, "show_progress"
    ) as progress, mock.patch.object(
        burn,
        "verified_source_stream",
        return_value=burn.contextlib.nullcontext(io.BytesIO(source_data)),
    ), mock.patch.object(burn.os, "killpg") as killpg, pytest.raises(burn.BurnError) as raised:
        burn.verify_written_image(
            disk,
            image,
            burn.hashlib.sha256(b"abcd").hexdigest(),
            4,
            sudo_session,
        )

    assert str(raised.value) == expected_message
    assert process.stdout.tell() == expected_card_bytes_read
    assert progress.call_args_list == expected_progress_calls
    assert sudo_session.keep_alive.call_args_list == [mock.call()] * expected_heartbeat_count
    assert process.communicate_calls == 0
    killpg.assert_called_once_with(process.pid, burn.signal.SIGTERM)
    assert process.wait_calls == 2


@pytest.mark.parametrize(
    ("block_start", "length", "dd_output", "expected_block", "expected_skip"),
    [(4, 4, b"efgh", b"efgh", 1), (8, 2, b"ijXY", b"ij", 2)],
    ids=["full", "partial-final"],
)
def test_read_disk_block_joins_short_reads_with_direct_aligned_dd(
    block_start, length, dd_output, expected_block, expected_skip
):
    """Join short pipe reads from direct full-block dd after checking the disk fingerprint."""

    class FragmentedReader(io.BytesIO):
        def read(self, size=-1):
            return super().read(min(size, 2) if size >= 0 else 2)

    disk = burn.Disk("disk4", "SD Card", 32, "USB", False, True, True)
    process = FakeDDProcess()
    process.stdout = FragmentedReader(dd_output)
    sudo_session = mock.Mock(spec=burn.SudoSession)
    with mock.patch.object(burn, "CHUNK_SIZE", 4), mock.patch.object(
        burn, "ensure_same_disk"
    ) as ensure_same_disk, mock.patch.object(
        burn, "popen_or_error", return_value=process
    ) as popen:
        block = burn.read_disk_block(disk, block_start, length, sudo_session)

    assert block == expected_block
    sudo_session.keep_alive.assert_called_once_with()
    ensure_same_disk.assert_called_once_with(disk)
    popen.assert_called_once_with(
        [
            "sudo",
            "-n",
            "dd",
            "if=/dev/rdisk4",
            "bs=4",
            "iflag=direct,fullblock",
            "skip={}".format(expected_skip),
            "count=1",
        ],
        stdout=burn.subprocess.PIPE,
        stderr=burn.subprocess.PIPE,
        preexec_fn=burn.os.setpgrp,
    )


@pytest.mark.parametrize(
    ("block_start", "length", "expected_message"),
    [
        (-1, 4, "The diagnostic block offset is not aligned"),
        (2, 4, "The diagnostic block offset is not aligned"),
        (4, -1, "The diagnostic block length is invalid"),
        (4, 5, "The diagnostic block length is invalid"),
    ],
    ids=["negative-offset", "unaligned-offset", "negative-length", "oversized-length"],
)
def test_read_disk_block_rejects_invalid_range_before_disk_access(
    block_start, length, expected_message
):
    """Reject an invalid diagnostic range before sudo, fingerprint, or dd operations."""

    disk = burn.Disk("disk4", "SD Card", 32, "USB", False, True, True)
    sudo_session = mock.Mock(spec=burn.SudoSession)
    with mock.patch.object(burn, "CHUNK_SIZE", 4), mock.patch.object(
        burn, "ensure_same_disk"
    ) as ensure_same_disk, mock.patch.object(burn, "popen_or_error") as popen, pytest.raises(
        burn.BurnError
    ) as raised:
        burn.read_disk_block(disk, block_start, length, sudo_session)

    assert str(raised.value) == expected_message
    sudo_session.keep_alive.assert_not_called()
    ensure_same_disk.assert_not_called()
    popen.assert_not_called()


@pytest.mark.parametrize("failing_check", ["heartbeat", "fingerprint"])
def test_read_disk_block_propagates_preflight_failure_before_starting_dd(failing_check):
    """Propagate a sudo or fingerprint failure before diagnostic dd starts."""

    disk = burn.Disk("disk4", "SD Card", 32, "USB", False, True, True)
    sudo_session = mock.Mock(spec=burn.SudoSession)
    preflight_error = (
        burn.SudoError("sudo ticket expired")
        if failing_check == "heartbeat"
        else burn.BurnError("disk fingerprint changed")
    )
    if failing_check == "heartbeat":
        sudo_session.keep_alive.side_effect = preflight_error
    fingerprint_side_effect = preflight_error if failing_check == "fingerprint" else None
    with mock.patch.object(burn, "CHUNK_SIZE", 4), mock.patch.object(
        burn, "ensure_same_disk", side_effect=fingerprint_side_effect
    ) as ensure_same_disk, mock.patch.object(burn, "popen_or_error") as popen, pytest.raises(
        type(preflight_error)
    ) as raised:
        burn.read_disk_block(disk, 4, 4, sudo_session)

    assert raised.value is preflight_error
    sudo_session.keep_alive.assert_called_once_with()
    if failing_check == "heartbeat":
        ensure_same_disk.assert_not_called()
    else:
        ensure_same_disk.assert_called_once_with(disk)
    popen.assert_not_called()


def test_read_disk_block_reports_dd_failure():
    """Report rejected direct-read flags without retrying diagnostic dd."""

    disk = burn.Disk("disk4", "SD Card", 32, "USB", False, True, True)
    process = FakeDDProcess(stderr=b"dd: iflag: illegal conversion", final_returncode=1)
    sudo_session = mock.Mock(spec=burn.SudoSession)
    with mock.patch.object(burn, "CHUNK_SIZE", 4), mock.patch.object(
        burn, "ensure_same_disk"
    ), mock.patch.object(burn, "popen_or_error", return_value=process) as popen, pytest.raises(
        burn.BurnError
    ) as raised:
        burn.read_disk_block(disk, 4, 4, sudo_session)

    assert str(raised.value) == "Could not repeat the card read at 4.0 B: dd: iflag: illegal conversion"
    popen.assert_called_once()
    assert process.communicate_calls == 1
    assert process.returncode == 1


@pytest.mark.parametrize(
    "read_error",
    [OSError("pipe failed"), KeyboardInterrupt("cancelled")],
    ids=["pipe-error", "cancellation"],
)
def test_read_disk_block_stops_dd_after_pipe_failure_or_cancellation(read_error):
    """Stop and reap diagnostic dd after a stdout error or cancellation."""

    process = FakeDDProcess()
    process.stdout = mock.Mock()
    process.stdout.read.side_effect = read_error
    disk = burn.Disk("disk4", "SD Card", 32, "USB", False, True, True)
    sudo_session = mock.Mock(spec=burn.SudoSession)
    with mock.patch.object(burn, "CHUNK_SIZE", 4), mock.patch.object(
        burn, "ensure_same_disk"
    ), mock.patch.object(burn, "popen_or_error", return_value=process), mock.patch.object(
        burn.os, "killpg"
    ) as killpg, pytest.raises((burn.BurnError, KeyboardInterrupt)) as raised:
        burn.read_disk_block(disk, 4, 4, sudo_session)

    if isinstance(read_error, KeyboardInterrupt):
        assert raised.value is read_error
    else:
        assert type(raised.value) is burn.BurnError
        assert str(raised.value) == "Could not repeat the card read at 4.0 B: pipe failed"
    killpg.assert_called_once_with(process.pid, burn.signal.SIGTERM)
    assert process.wait_calls == 2
    process.stdout.read.assert_called_once_with(4)


def test_read_disk_block_rejects_short_output():
    """Reject a successful diagnostic dd read shorter than the requested block."""

    process = FakeDDProcess(b"ef")
    disk = burn.Disk("disk4", "SD Card", 32, "USB", False, True, True)
    sudo_session = mock.Mock(spec=burn.SudoSession)
    with mock.patch.object(burn, "CHUNK_SIZE", 4), mock.patch.object(
        burn, "ensure_same_disk"
    ), mock.patch.object(burn, "popen_or_error", return_value=process), pytest.raises(
        burn.BurnError
    ) as raised:
        burn.read_disk_block(disk, 4, 4, sudo_session)

    assert str(raised.value) == "Could not repeat the card read at 4.0 B: expected 4.0 B, got 2.0 B"
    assert process.communicate_calls == 1


@pytest.mark.parametrize(
    ("repeat_mode", "classification"),
    [
        ("stable", "the card contains stable data that differs from the image"),
        ("transient", "the initial direct read was transiently inconsistent"),
        ("unstable", "the card, reader, or USB path returned unstable data"),
        ("mixed", "the card, reader, or USB path returned unstable data"),
        ("settled-new", "the card, reader, or USB path returned unstable data"),
    ],
)
def test_verify_written_image_classifies_repeated_block_reads(repeat_mode, classification):
    """Classify stable, transient, and unstable rereads of the first mismatched block."""

    source_data = b"abcdefghij"
    card_data = b"abcdeXghij"
    expected_block = b"efgh"
    initial_block = b"eXgh"
    repeated_blocks = {
        "stable": [initial_block, initial_block],
        "transient": [expected_block, expected_block],
        "unstable": [b"eYgh", b"eZgh"],
        "mixed": [expected_block, initial_block],
        "settled-new": [b"eYgh", b"eYgh"],
    }[repeat_mode]
    disk = burn.Disk("disk4", "SD Card", 32, "USB", False, True, True)
    image = burn.ImageSpec(Path("ubuntu.img"), None, "test", uncompressed_size=len(source_data))
    process = FakeDDProcess(card_data)
    sudo_session = mock.Mock(spec=burn.SudoSession)
    with mock.patch.object(burn, "CHUNK_SIZE", 4), mock.patch.object(
        burn, "ensure_same_disk"
    ), mock.patch.object(burn, "popen_or_error", return_value=process), mock.patch.object(
        burn, "show_progress"
    ), mock.patch.object(
        burn,
        "verified_source_stream",
        return_value=burn.contextlib.nullcontext(io.BytesIO(source_data)),
    ), mock.patch.object(burn, "read_disk_block", side_effect=repeated_blocks) as read_block, pytest.raises(
        burn.BurnError
    ) as raised:
        burn.verify_written_image(
            disk,
            image,
            burn.hashlib.sha256(source_data).hexdigest(),
            len(source_data),
            sudo_session,
        )

    message = str(raised.value)
    assert "Write verification failed at byte 5 (5.0 B): {}.".format(classification) in message
    assert "Expected image bytes: 10; card bytes read: 10." in message
    assert "Expected image SHA-256: {}.".format(burn.hashlib.sha256(source_data).hexdigest()) in message
    assert "Card SHA-256: {}.".format(burn.hashlib.sha256(card_data).hexdigest()) in message
    assert "Expected block SHA-256: {}.".format(burn.hashlib.sha256(expected_block).hexdigest()) in message
    assert "Initial card block SHA-256: {}.".format(burn.hashlib.sha256(initial_block).hexdigest()) in message
    for number, repeated_block in enumerate(repeated_blocks, 1):
        assert "Repeated card block {} SHA-256: {}.".format(
            number, burn.hashlib.sha256(repeated_block).hexdigest()
        ) in message
    assert read_block.call_args_list == [
        mock.call(disk, 4, 4, sudo_session),
        mock.call(disk, 4, 4, sudo_session),
    ]


def test_verification_mismatch_message_reports_hashes_without_raw_blocks():
    """Report every block hash without exposing block bytes as text, repr, or hex."""

    expected_block = b"RAW_EXPECTED_BLOCK"
    initial_block = b"RAW_INITIAL_BLOCK"
    repeated_blocks = [b"RAW_REPEAT_ONE", b"RAW_REPEAT_TWO"]

    message = burn.verification_mismatch_message(
        4,
        64,
        64,
        "a" * 64,
        "b" * 64,
        expected_block,
        initial_block,
        repeated_blocks,
        [None, None],
        "the card, reader, or USB path returned unstable data",
    )

    assert message.splitlines() == [
        "Write verification failed at byte 4 (4.0 B): "
        "the card, reader, or USB path returned unstable data.",
        "Expected image bytes: 64; card bytes read: 64.",
        "Expected image SHA-256: {}.".format("a" * 64),
        "Card SHA-256: {}.".format("b" * 64),
        "Expected block SHA-256: {}.".format(burn.hashlib.sha256(expected_block).hexdigest()),
        "Initial card block SHA-256: {}.".format(burn.hashlib.sha256(initial_block).hexdigest()),
        "Repeated card block 1 SHA-256: {}.".format(
            burn.hashlib.sha256(repeated_blocks[0]).hexdigest()
        ),
        "Repeated card block 2 SHA-256: {}.".format(
            burn.hashlib.sha256(repeated_blocks[1]).hexdigest()
        ),
    ]


@pytest.mark.parametrize(
    "mismatch_offsets",
    [(0, 9), (1, 9), (6, 9), (9,)],
    ids=["first-byte", "first-block", "middle-block", "partial-final-block"],
)
def test_verify_written_image_reports_exact_first_mismatch(mismatch_offsets):
    """Report the exact first differing byte in the first, middle, or final block."""

    source_data = b"abcdefghij"
    card_data = bytearray(source_data)
    for mismatch_offset in mismatch_offsets:
        card_data[mismatch_offset] ^= 0xFF
    first_mismatch_offset = mismatch_offsets[0]
    disk = burn.Disk("disk4", "SD Card", 32, "USB", False, True, True)
    image = burn.ImageSpec(Path("ubuntu.img"), None, "test", uncompressed_size=len(source_data))
    process = FakeDDProcess(bytes(card_data))
    sudo_session = mock.Mock(spec=burn.SudoSession)
    block_start = first_mismatch_offset - first_mismatch_offset % 4
    block_length = min(4, len(source_data) - block_start)
    wrong_block = bytes(card_data[block_start : block_start + block_length])
    with mock.patch.object(burn, "CHUNK_SIZE", 4), mock.patch.object(
        burn, "ensure_same_disk"
    ), mock.patch.object(burn, "popen_or_error", return_value=process), mock.patch.object(
        burn, "show_progress"
    ), mock.patch.object(
        burn,
        "verified_source_stream",
        return_value=burn.contextlib.nullcontext(io.BytesIO(source_data)),
    ), mock.patch.object(
        burn, "read_disk_block", side_effect=[wrong_block, wrong_block]
    ) as read_block, pytest.raises(burn.BurnError) as raised:
        burn.verify_written_image(
            disk,
            image,
            burn.hashlib.sha256(source_data).hexdigest(),
            len(source_data),
            sudo_session,
        )

    assert "Write verification failed at byte {} ".format(first_mismatch_offset) in str(raised.value)
    assert process.stdout.tell() == len(source_data)
    assert read_block.call_args_list == [
        mock.call(disk, block_start, block_length, sudo_session),
        mock.call(disk, block_start, block_length, sudo_session),
    ]


def test_verify_written_image_rejects_changed_source_before_block_diagnostics():
    """Report a changed source digest without blaming or rereading the card."""

    source_data = b"changed image"
    disk = burn.Disk("disk4", "SD Card", 32, "USB", False, True, True)
    image = burn.ImageSpec(Path("ubuntu.img"), None, "test", uncompressed_size=len(source_data))
    process = FakeDDProcess(source_data)
    sudo_session = mock.Mock(spec=burn.SudoSession)
    with mock.patch.object(burn, "CHUNK_SIZE", 4), mock.patch.object(
        burn, "ensure_same_disk"
    ), mock.patch.object(burn, "popen_or_error", return_value=process), mock.patch.object(
        burn, "show_progress"
    ), mock.patch.object(
        burn,
        "verified_source_stream",
        return_value=burn.contextlib.nullcontext(io.BytesIO(source_data)),
    ), mock.patch.object(burn, "read_disk_block") as read_block, pytest.raises(burn.BurnError) as raised:
        burn.verify_written_image(disk, image, "0" * 64, len(source_data), sudo_session)

    expected_message = (
        "Image data changed between writing and verification: "
        "expected SHA-256 {}, got {}"
    ).format("0" * 64, burn.hashlib.sha256(source_data).hexdigest())
    assert str(raised.value) == expected_message
    read_block.assert_not_called()


def test_verify_written_image_reports_mismatch_and_changed_source_together():
    """Keep card diagnostics when the reopened source also changed since writing."""

    source_data = b"abcdefgh"
    card_data = b"abcdXfgh"
    write_time_digest = "0" * 64
    disk = burn.Disk("disk4", "SD Card", 32, "USB", False, True, True)
    image = burn.ImageSpec(Path("ubuntu.img"), None, "test", uncompressed_size=len(source_data))
    process = FakeDDProcess(card_data)
    sudo_session = mock.Mock(spec=burn.SudoSession)
    with mock.patch.object(burn, "CHUNK_SIZE", 4), mock.patch.object(
        burn, "ensure_same_disk"
    ), mock.patch.object(burn, "popen_or_error", return_value=process), mock.patch.object(
        burn, "show_progress"
    ), mock.patch.object(
        burn,
        "verified_source_stream",
        return_value=burn.contextlib.nullcontext(io.BytesIO(source_data)),
    ), mock.patch.object(
        burn, "read_disk_block", side_effect=[b"Xfgh", b"Xfgh"]
    ) as read_block, pytest.raises(burn.BurnError) as raised:
        burn.verify_written_image(
            disk,
            image,
            write_time_digest,
            len(source_data),
            sudo_session,
        )

    message = str(raised.value)
    assert (
        "Write verification failed at byte 4 (4.0 B): "
        "the card contains stable data that differs from the image."
    ) in message
    assert "Expected image SHA-256: {}".format(burn.hashlib.sha256(source_data).hexdigest()) in message
    assert "Card SHA-256: {}".format(burn.hashlib.sha256(card_data).hexdigest()) in message
    assert "Expected block SHA-256: {}".format(burn.hashlib.sha256(b"efgh").hexdigest()) in message
    assert "Initial card block SHA-256: {}".format(burn.hashlib.sha256(b"Xfgh").hexdigest()) in message
    assert message.count(
        "Repeated card block 1 SHA-256: {}".format(burn.hashlib.sha256(b"Xfgh").hexdigest())
    ) == 1
    assert message.count(
        "Repeated card block 2 SHA-256: {}".format(burn.hashlib.sha256(b"Xfgh").hexdigest())
    ) == 1
    assert "Source changed since writing: write-time SHA-256 {}; verification SHA-256 {}.".format(
        write_time_digest, burn.hashlib.sha256(source_data).hexdigest()
    ) in message
    assert read_block.call_args_list == [
        mock.call(disk, 4, 4, sudo_session),
        mock.call(disk, 4, 4, sudo_session),
    ]


@pytest.mark.parametrize(
    ("diagnostic_outcomes", "failed_attempt", "successful_attempt", "diagnostic_error"),
    [
        ([burn.BurnError("first reread failed"), b"Xfgh"], 1, 2, "first reread failed"),
        ([b"Xfgh", burn.BurnError("second reread failed")], 2, 1, "second reread failed"),
    ],
    ids=["first-reread", "second-reread"],
)
def test_verification_preserves_mismatch_when_one_diagnostic_read_fails(
    diagnostic_outcomes,
    failed_attempt,
    successful_attempt,
    diagnostic_error,
):
    """Keep the primary mismatch and the successful reread when the other reread fails."""

    source_data = b"abcdefgh"
    card_data = b"abcdXfgh"
    disk = burn.Disk("disk4", "SD Card", 32, "USB", False, True, True)
    image = burn.ImageSpec(Path("ubuntu.img"), None, "test", uncompressed_size=len(source_data))
    process = FakeDDProcess(card_data)
    sudo_session = mock.Mock(spec=burn.SudoSession)
    with mock.patch.object(burn, "CHUNK_SIZE", 4), mock.patch.object(
        burn, "ensure_same_disk"
    ), mock.patch.object(burn, "popen_or_error", return_value=process), mock.patch.object(
        burn, "show_progress"
    ), mock.patch.object(
        burn,
        "verified_source_stream",
        return_value=burn.contextlib.nullcontext(io.BytesIO(source_data)),
    ), mock.patch.object(
        burn, "read_disk_block", side_effect=diagnostic_outcomes
    ) as read_block, pytest.raises(burn.BurnError) as raised:
        burn.verify_written_image(
            disk,
            image,
            burn.hashlib.sha256(source_data).hexdigest(),
            len(source_data),
            sudo_session,
        )

    message = str(raised.value)
    assert (
        "Write verification failed at byte 4 (4.0 B): "
        "the card data differs from the image."
    ) in message
    assert "Expected image bytes: 8; card bytes read: 8." in message
    assert "Expected image SHA-256: {}.".format(burn.hashlib.sha256(source_data).hexdigest()) in message
    assert "Card SHA-256: {}.".format(burn.hashlib.sha256(card_data).hexdigest()) in message
    assert "Expected block SHA-256: {}.".format(burn.hashlib.sha256(b"efgh").hexdigest()) in message
    assert "Initial card block SHA-256: {}.".format(burn.hashlib.sha256(b"Xfgh").hexdigest()) in message
    assert "Repeated card block {} diagnostic failed: {}.".format(
        failed_attempt, diagnostic_error
    ) in message
    assert "Repeated card block {} SHA-256: {}.".format(
        successful_attempt, burn.hashlib.sha256(b"Xfgh").hexdigest()
    ) in message
    assert read_block.call_args_list == [
        mock.call(disk, 4, 4, sudo_session),
        mock.call(disk, 4, 4, sudo_session),
    ]


@pytest.mark.parametrize(
    ("operation", "dd_stderr", "expected_error_message"),
    [
        ("integrity-write", b"dd failed", "Integrity test write failed for /dev/disk4: dd failed"),
        (
            "integrity-read",
            b"dd: iflag: illegal conversion",
            "Integrity verification failed for /dev/disk4: dd: iflag: illegal conversion",
        ),
        ("image-write", b"dd failed", "dd could not write the image: dd failed"),
        (
            "image-read",
            b"dd: iflag: illegal conversion",
            "Could not read /dev/disk4: dd: iflag: illegal conversion",
        ),
    ],
)
def test_transfer_reports_dd_failure(operation, dd_stderr, expected_error_message):
    """Report dd stderr without fallback and skip sync after failed writes."""

    data = b"abcdefghij"
    disk = burn.Disk("disk4", "SD Card", len(data), "USB", False, True, True)
    image = burn.ImageSpec(
        Path("ubuntu.img"),
        None,
        "test",
        uncompressed_size=len(data),
        file_identity=(1, 2, len(data), 3),
    )
    dd_output = data[:4] if operation == "image-read" else b""
    process = FakeDDProcess(dd_output, stderr=dd_stderr, final_returncode=1)
    sudo_session = mock.Mock(spec=burn.SudoSession)

    with mock.patch.object(burn, "CHUNK_SIZE", 4), mock.patch.object(
        burn, "INTEGRITY_PATTERN_BLOCK_SIZE", 4
    ), mock.patch.object(burn, "ensure_same_disk"), mock.patch.object(
        burn, "unmount_disk"
    ), mock.patch.object(
        burn, "popen_or_error", return_value=process
    ) as popen, mock.patch.object(burn, "show_progress"), mock.patch.object(burn, "run") as run, mock.patch.object(
        burn,
        "verified_source_stream",
        return_value=burn.contextlib.nullcontext(io.BytesIO(data)),
    ):
        if operation == "integrity-read":
            process.stdout = RecordingOutput(burn.integrity_pattern(b"s" * 32, 0, 4), [])
        with pytest.raises(burn.BurnError) as raised:
            run_transfer(operation, disk, image, sudo_session)

    assert type(raised.value) is burn.BurnError
    assert str(raised.value) == expected_error_message
    popen.assert_called_once()
    assert process.communicate_calls == 1
    expected_heartbeat_count = 4 if operation != "integrity-read" else 2
    assert sudo_session.keep_alive.call_args_list == [mock.call()] * expected_heartbeat_count
    sudo_session.authenticate.assert_not_called()
    run.assert_not_called()


@pytest.mark.parametrize("operation", ["integrity-write", "integrity-read", "image-write", "image-read"])
@pytest.mark.parametrize(
    "heartbeat_exception_type", [burn.SudoError, KeyboardInterrupt], ids=["sudo", "interrupt"]
)
def test_transfer_terminates_dd_and_propagates_sudo_failure_or_cancellation(
    operation, heartbeat_exception_type
):
    """Stop after one block, reap dd, and propagate the second heartbeat's SudoError or
    KeyboardInterrupt unchanged.
    """

    data = b"abcdefghij"
    disk = burn.Disk("disk4", "SD Card", len(data), "USB", False, True, True)
    image = burn.ImageSpec(
        Path("ubuntu.img"),
        None,
        "test",
        uncompressed_size=len(data),
        file_identity=(1, 2, len(data), 3),
    )
    events = []
    process = FakeDDProcess(data, events=events)
    sudo_session = mock.Mock(spec=burn.SudoSession)
    heartbeat_exception = heartbeat_exception_type("transfer interrupted")

    def heartbeat():
        events.append("heartbeat")
        if events.count("heartbeat") == 2:
            raise heartbeat_exception

    sudo_session.keep_alive.side_effect = heartbeat

    def start_dd(*_args, **_kwargs):
        assert sudo_session.keep_alive.call_count == 1
        return process

    with mock.patch.object(burn, "CHUNK_SIZE", 4), mock.patch.object(
        burn, "INTEGRITY_PATTERN_BLOCK_SIZE", 4
    ), mock.patch.object(burn, "ensure_same_disk") as ensure_same_disk, mock.patch.object(
        burn, "unmount_disk"
    ) as unmount, mock.patch.object(
        burn, "popen_or_error", side_effect=start_dd
    ), mock.patch.object(burn, "show_progress"), mock.patch.object(burn, "run") as run, mock.patch.object(
        burn.os, "killpg"
    ) as killpg, mock.patch.object(
        burn,
        "verified_source_stream",
        return_value=burn.contextlib.nullcontext(io.BytesIO(data)),
    ) as source_stream, pytest.raises(heartbeat_exception_type) as raised:
        if operation == "integrity-read":
            process.stdout = RecordingOutput(burn.integrity_pattern(b"s" * 32, 0, len(data)), events)
        run_transfer(operation, disk, image, sudo_session)

    killpg.assert_called_once_with(process.pid, burn.signal.SIGTERM)
    assert raised.value is heartbeat_exception
    sudo_session.authenticate.assert_not_called()
    assert events == ["heartbeat", "block", "heartbeat"]
    assert process.returncode == -15
    assert process.wait_calls == (1 if operation == "image-write" else 2)
    run.assert_not_called()
    assert ensure_same_disk.call_count == (2 if operation.endswith("write") else 1)
    if operation.endswith("write"):
        unmount.assert_called_once_with(disk)
        expected_payload = burn.integrity_pattern(b"s" * 32, 0, 4) if operation == "integrity-write" else data[:4]
        assert process.input_stream.getvalue() == expected_payload
        assert process.input_stream.was_closed is True
        assert process.stdin is None
    else:
        unmount.assert_not_called()
        assert process.stdout.tell() == 4
    if operation.startswith("image-"):
        source_stream.assert_called_once_with(image)
    else:
        source_stream.assert_not_called()


@pytest.mark.parametrize(
    "heartbeat_exception_type", [burn.SudoError, KeyboardInterrupt], ids=["sudo", "interrupt"]
)
@pytest.mark.parametrize("close_error_type", [BrokenPipeError, OSError])
def test_image_write_preserves_sudo_failure_or_cancellation_when_cleanup_fails(
    heartbeat_exception_type, close_error_type
):
    """Preserve cancellation or sudo failure when stdin closure and stderr collection fail."""

    data = b"abcdefghij"
    disk = burn.Disk("disk4", "SD Card", len(data), "USB", False, True, True)
    image = burn.ImageSpec(
        Path("ubuntu.img"),
        None,
        "test",
        uncompressed_size=len(data),
        file_identity=(1, 2, len(data), 3),
    )
    process = FakeDDProcess()
    process.input_stream.close = mock.Mock(side_effect=close_error_type("close failed"))
    process.communicate = mock.Mock(side_effect=OSError("stderr failed"))
    heartbeat_exception = heartbeat_exception_type("transfer interrupted")
    sudo_session = mock.Mock(spec=burn.SudoSession)
    sudo_session.keep_alive.side_effect = [None, heartbeat_exception]

    with mock.patch.object(burn, "CHUNK_SIZE", 4), mock.patch.object(
        burn, "ensure_same_disk"
    ), mock.patch.object(burn, "unmount_disk"), mock.patch.object(
        burn, "popen_or_error", return_value=process
    ), mock.patch.object(burn, "show_progress"), mock.patch.object(burn, "run") as run, mock.patch.object(
        burn.os, "killpg"
    ) as killpg, mock.patch.object(
        burn,
        "verified_source_stream",
        return_value=burn.contextlib.nullcontext(io.BytesIO(data)),
    ), pytest.raises(heartbeat_exception_type) as raised:
        burn.write_image(disk, image, sudo_session)

    assert raised.value is heartbeat_exception
    sudo_session.authenticate.assert_not_called()
    assert process.input_stream.getvalue() == data[:4]
    assert process.stdin is None
    process.input_stream.close.assert_called_once_with()
    killpg.assert_called_once_with(process.pid, burn.signal.SIGTERM)
    assert process.wait_calls == 1
    process.communicate.assert_called_once_with()
    run.assert_not_called()


def test_process_stderr_describes_communication_failure():
    """Return a user-facing fallback when stderr collection fails."""

    process = mock.Mock()
    process.communicate.side_effect = OSError("stderr pipe failed")

    assert burn.process_stderr(process) == "could not read stderr: stderr pipe failed"
    process.communicate.assert_called_once_with()


@pytest.mark.parametrize(
    ("operation", "expected_error_message"),
    [
        ("integrity-write", "Integrity test write failed for /dev/disk4: pipe failed"),
        ("integrity-read", "Integrity verification failed for /dev/disk4: pipe failed"),
        ("image-write", "Could not write the image: pipe failed"),
        ("image-read", "Could not verify the written image: pipe failed"),
    ],
)
def test_transfer_pipe_error_stops_dd_and_reports_io_failure(operation, expected_error_message):
    """Stop dd and wrap a pipe OSError after one block in the operation-specific BurnError."""

    chunk_size = 4

    class FailingInput(RecordingInput):
        def write(self, data):
            if self.tell() >= chunk_size:
                raise OSError("pipe failed")
            return super().write(data)

    class FailingOutput(RecordingOutput):
        def read(self, size=-1):
            if self.tell() >= chunk_size:
                raise OSError("pipe failed")
            return super().read(size)

    data = b"abcdefghij"
    disk = burn.Disk("disk4", "SD Card", len(data), "USB", False, True, True)
    image = burn.ImageSpec(
        Path("ubuntu.img"),
        None,
        "test",
        uncompressed_size=len(data),
        file_identity=(1, 2, len(data), 3),
    )
    process = FakeDDProcess()
    if operation.endswith("write"):
        process.input_stream = FailingInput([])
        process.stdin = process.input_stream
    else:
        dd_output = burn.integrity_pattern(b"s" * 32, 0, len(data)) if operation == "integrity-read" else data
        process.stdout = FailingOutput(dd_output, [])
    sudo_session = mock.Mock(spec=burn.SudoSession)

    with mock.patch.object(burn, "CHUNK_SIZE", chunk_size), mock.patch.object(
        burn, "INTEGRITY_PATTERN_BLOCK_SIZE", chunk_size
    ), mock.patch.object(burn, "ensure_same_disk"), mock.patch.object(
        burn, "unmount_disk"
    ), mock.patch.object(
        burn, "popen_or_error", return_value=process
    ), mock.patch.object(burn, "show_progress"), mock.patch.object(burn, "run") as run, mock.patch.object(
        burn.os, "killpg"
    ) as killpg, mock.patch.object(
        burn,
        "verified_source_stream",
        return_value=burn.contextlib.nullcontext(io.BytesIO(data)),
    ), pytest.raises(burn.BurnError) as raised:
        run_transfer(operation, disk, image, sudo_session)

    assert type(raised.value) is burn.BurnError
    assert str(raised.value) == expected_error_message
    killpg.assert_called_once_with(process.pid, burn.signal.SIGTERM)
    assert process.wait_calls == (1 if operation == "image-write" else 2)
    assert process.communicate_calls == (1 if operation == "image-write" else 0)
    assert sudo_session.keep_alive.call_args_list == [mock.call(), mock.call()]
    sudo_session.authenticate.assert_not_called()
    run.assert_not_called()
    if operation.endswith("write"):
        assert process.input_stream.getvalue() == (
            burn.integrity_pattern(b"s" * 32, 0, chunk_size)
            if operation == "integrity-write"
            else data[:chunk_size]
        )
        assert process.input_stream.was_closed is True
        assert process.stdin is None
    else:
        assert process.stdout.tell() == chunk_size


@pytest.mark.parametrize(
    (
        "source_data",
        "disk_size",
        "expected_error_message",
        "expected_written_size",
        "expected_heartbeat_count",
    ),
    [
        (b"abcdefgh", 10, "The image size changed while writing: expected 10.0 B, got 8.0 B", 8, 3),
        (b"abcdefghijkl", 10, "The decompressed image is larger than the selected card", 8, 3),
        (
            b"abcdefghijkl",
            16,
            "The image size changed while writing: expected 10.0 B, got 12.0 B",
            12,
            4,
        ),
    ],
    ids=["shorter", "larger-than-card", "larger-but-fits-card"],
)
def test_write_image_stops_when_source_size_changes(
    source_data,
    disk_size,
    expected_error_message,
    expected_written_size,
    expected_heartbeat_count,
):
    """Stop dd when stream length differs from preflight or exceeds card capacity."""

    disk = burn.Disk("disk4", "SD Card", disk_size, "USB", False, True, True)
    image = burn.ImageSpec(
        Path("ubuntu.img"),
        None,
        "test",
        uncompressed_size=10,
        file_identity=(1, 2, 10, 3),
    )
    process = FakeDDProcess()
    sudo_session = mock.Mock(spec=burn.SudoSession)
    with mock.patch.object(burn, "CHUNK_SIZE", 4), mock.patch.object(
        burn, "ensure_same_disk"
    ), mock.patch.object(burn, "unmount_disk"), mock.patch.object(
        burn, "popen_or_error", return_value=process
    ), mock.patch.object(burn, "show_progress"), mock.patch.object(burn, "run") as run, mock.patch.object(
        burn.os, "killpg"
    ) as killpg, mock.patch.object(
        burn,
        "verified_source_stream",
        return_value=burn.contextlib.nullcontext(io.BytesIO(source_data)),
    ), pytest.raises(burn.BurnError) as raised:
        burn.write_image(disk, image, sudo_session)

    assert type(raised.value) is burn.BurnError
    assert str(raised.value) == expected_error_message
    assert process.input_stream.getvalue() == source_data[:expected_written_size]
    assert process.input_stream.was_closed is True
    assert process.stdin is None
    killpg.assert_called_once_with(process.pid, burn.signal.SIGTERM)
    assert process.wait_calls == 1
    assert process.communicate_calls == 1
    assert sudo_session.keep_alive.call_args_list == [mock.call()] * expected_heartbeat_count
    sudo_session.authenticate.assert_not_called()
    run.assert_not_called()


@pytest.mark.parametrize("operation", ["integrity-write", "integrity-read", "image-write", "image-read"])
def test_transfer_validates_sudo_before_disk_operations(operation):
    """Abort before fingerprint checks, unmounting, or dd if sudo is unavailable."""

    data = b"abcdefghij"
    disk = burn.Disk("disk4", "SD Card", len(data), "USB", False, True, True)
    image = burn.ImageSpec(
        Path("ubuntu.img"),
        None,
        "test",
        uncompressed_size=len(data),
        file_identity=(1, 2, len(data), 3),
    )
    sudo_session = mock.Mock(spec=burn.SudoSession)
    sudo_session.keep_alive.side_effect = burn.SudoError("sudo authorization failed")
    with mock.patch.object(burn, "ensure_same_disk") as ensure_same_disk, mock.patch.object(
        burn, "unmount_disk"
    ) as unmount, mock.patch.object(burn, "popen_or_error") as popen, mock.patch.object(
        burn,
        "verified_source_stream",
        return_value=burn.contextlib.nullcontext(io.BytesIO(data)),
    ) as source_stream, pytest.raises(burn.SudoError, match="sudo authorization failed"):
        run_transfer(operation, disk, image, sudo_session)

    sudo_session.keep_alive.assert_called_once_with()
    sudo_session.authenticate.assert_not_called()
    ensure_same_disk.assert_not_called()
    unmount.assert_not_called()
    popen.assert_not_called()
    if operation.startswith("image-"):
        source_stream.assert_called_once_with(image)
    else:
        source_stream.assert_not_called()


def test_verify_integrity_pattern_joins_short_pipe_reads():
    """Accept a matching integrity pattern when the stdout pipe returns short reads."""

    class FragmentedReader(io.BytesIO):
        def read(self, size=-1):
            return super().read(min(size, 2) if size >= 0 else 2)

    seed = b"s" * 32
    disk = burn.Disk("disk4", "SD Card", 10, "USB", False, True, True)
    process = FakeDDProcess()
    sudo_session = mock.Mock(spec=burn.SudoSession)
    with mock.patch.object(burn, "CHUNK_SIZE", 4), mock.patch.object(
        burn, "INTEGRITY_PATTERN_BLOCK_SIZE", 4
    ), mock.patch.object(burn, "ensure_same_disk"), mock.patch.object(
        burn, "popen_or_error", return_value=process
    ), mock.patch.object(burn, "show_progress"):
        process.stdout = FragmentedReader(burn.integrity_pattern(seed, 0, disk.size))
        result = burn.verify_integrity_pattern(disk, sudo_session, seed)

    assert result is None
    assert process.stdout.tell() == disk.size
    assert sudo_session.keep_alive.call_args_list == [mock.call()] * 4


@pytest.mark.parametrize(
    (
        "corruption_offset",
        "expected_reported_offset",
        "expected_bytes_read",
        "expected_heartbeat_count",
    ),
    [(0, 0, 4, 1), (4, 4, 8, 2), (9, 8, 10, 3)],
    ids=["first-block", "middle-block", "partial-final-block"],
)
def test_verify_integrity_pattern_rejects_corrupted_data(
    corruption_offset,
    expected_reported_offset,
    expected_bytes_read,
    expected_heartbeat_count,
):
    """Reject corruption in the first, middle, or partial final block and report the block's starting offset."""

    disk = burn.Disk("disk4", "SD Card", 10, "USB", False, True, True)
    process = FakeDDProcess()
    sudo_session = mock.Mock(spec=burn.SudoSession)
    with mock.patch.object(burn, "CHUNK_SIZE", 4), mock.patch.object(
        burn, "INTEGRITY_PATTERN_BLOCK_SIZE", 4
    ), mock.patch.object(burn, "ensure_same_disk"), mock.patch.object(
        burn, "popen_or_error", return_value=process
    ), mock.patch.object(burn, "show_progress"), mock.patch.object(burn.os, "killpg") as killpg:
        corrupted_data = bytearray(burn.integrity_pattern(b"s" * 32, 0, disk.size))
        corrupted_data[corruption_offset] ^= 0xFF
        process.stdout = io.BytesIO(bytes(corrupted_data))
        expected_error_message = "Integrity test data differs at offset {}".format(
            burn.human_size(expected_reported_offset)
        )
        with pytest.raises(burn.BurnError) as raised:
            burn.verify_integrity_pattern(disk, sudo_session, b"s" * 32)

    assert type(raised.value) is burn.BurnError
    assert str(raised.value) == expected_error_message
    killpg.assert_called_once_with(process.pid, burn.signal.SIGTERM)
    assert process.wait_calls == 2
    assert process.stdout.tell() == expected_bytes_read
    assert sudo_session.keep_alive.call_args_list == [mock.call()] * expected_heartbeat_count
    sudo_session.authenticate.assert_not_called()


@pytest.mark.parametrize(
    ("operation", "card_data", "expected_bytes_read"),
    [("integrity-read", None, 4), ("image-read", b"abcdef", 6)],
)
def test_disk_read_rejects_truncated_stream(operation, card_data, expected_bytes_read):
    """Reject successful dd output shorter than requested."""

    disk = burn.Disk("disk4", "SD Card", 8, "USB", False, True, True)
    process = FakeDDProcess()
    sudo_session = mock.Mock(spec=burn.SudoSession)
    with mock.patch.object(burn, "CHUNK_SIZE", 4), mock.patch.object(
        burn, "INTEGRITY_PATTERN_BLOCK_SIZE", 4
    ), mock.patch.object(burn, "ensure_same_disk"), mock.patch.object(
        burn, "popen_or_error", return_value=process
    ), mock.patch.object(burn, "show_progress"), mock.patch.object(
        burn,
        "verified_source_stream",
        return_value=burn.contextlib.nullcontext(io.BytesIO(b"abcdefgh")),
    ), mock.patch.object(burn, "read_disk_block", side_effect=[b"zzzz", b"zzzz"]):
        if operation == "integrity-read":
            process.stdout = io.BytesIO(burn.integrity_pattern(b"s" * 32, 0, 4))
            expected_error_message = "The card reports 8.0 B, but only 4.0 B could be verified"
        else:
            assert card_data is not None
            process.stdout = io.BytesIO(card_data)
            expected_error_message = "Expected image bytes: 8; card bytes read: 6."
        with pytest.raises(burn.BurnError) as raised:
            if operation == "integrity-read":
                burn.verify_integrity_pattern(disk, sudo_session, b"s" * 32)
            else:
                burn.verify_written_image(
                    disk,
                    burn.ImageSpec(Path("ubuntu.img"), None, "test", uncompressed_size=8),
                    burn.hashlib.sha256(b"abcdefgh").hexdigest(),
                    8,
                    sudo_session,
                )

    assert type(raised.value) is burn.BurnError
    error_message = str(raised.value)
    assert expected_error_message in error_message
    if operation == "image-read":
        assert "Write verification failed at byte 6" in error_message
        assert "Expected image SHA-256: {}.".format(
            burn.hashlib.sha256(b"abcdefgh").hexdigest()
        ) in error_message
        assert "Card SHA-256: {}.".format(burn.hashlib.sha256(card_data).hexdigest()) in error_message
        assert "Expected block SHA-256: {}.".format(burn.hashlib.sha256(b"efgh").hexdigest()) in error_message
        assert "Initial card block SHA-256: {}.".format(burn.hashlib.sha256(b"ef").hexdigest()) in error_message
    assert process.stdout.tell() == expected_bytes_read
    assert process.communicate_calls == 1
    expected_heartbeat_count = 2 if operation == "integrity-read" else 3
    assert sudo_session.keep_alive.call_args_list == [mock.call()] * expected_heartbeat_count
    sudo_session.authenticate.assert_not_called()


def test_terminate_process_group_escalates_to_sigkill():
    """Escalate to SIGKILL when dd exceeds the graceful timeout."""

    events = []

    class StubbornProcess:
        pid = 4321

        def poll(self):
            events.append(("poll",))
            return None

        def wait(self, timeout=None):
            events.append(("wait", timeout))
            if timeout is not None:
                raise subprocess.TimeoutExpired("dd", timeout)
            return -9

    process = StubbornProcess()

    def send_signal(pid, signal_number):
        events.append(("signal", pid, signal_number))

    with mock.patch.object(burn.os, "killpg", side_effect=send_signal):
        burn.terminate_process_group(process)

    assert events == [
        ("poll",),
        ("signal", process.pid, burn.signal.SIGTERM),
        ("wait", 3),
        ("signal", process.pid, burn.signal.SIGKILL),
        ("wait", None),
    ]


@pytest.mark.parametrize("undeliverable_signal", [burn.signal.SIGTERM, burn.signal.SIGKILL])
def test_terminate_process_group_tolerates_disappearing_group(undeliverable_signal):
    """Reap the process if its group disappears before SIGTERM or SIGKILL."""

    events = []

    class DisappearingProcess:
        pid = 4321

        def poll(self):
            events.append(("poll",))
            return None

        def wait(self, timeout=None):
            events.append(("wait", timeout))
            if undeliverable_signal == burn.signal.SIGKILL and timeout is not None:
                raise subprocess.TimeoutExpired("dd", timeout)
            return 0

    process = DisappearingProcess()

    def send_signal(pid, signal_number):
        events.append(("signal", pid, signal_number))
        if signal_number == undeliverable_signal:
            raise ProcessLookupError

    with mock.patch.object(burn.os, "killpg", side_effect=send_signal):
        burn.terminate_process_group(process)

    expected_events = [
        ("poll",),
        ("signal", process.pid, burn.signal.SIGTERM),
        ("wait", 3),
    ]
    if undeliverable_signal == burn.signal.SIGKILL:
        expected_events.extend(
            [
                ("signal", process.pid, burn.signal.SIGKILL),
                ("wait", None),
            ]
        )
    assert events == expected_events


def test_terminate_process_group_ignores_completed_process():
    """Do not signal or wait for a completed process."""

    process = FakeDDProcess(completed=True)
    with mock.patch.object(burn.os, "killpg") as killpg:
        burn.terminate_process_group(process)

    killpg.assert_not_called()
    assert process.wait_calls == 0


@pytest.mark.parametrize(
    ("returncode", "quiet"),
    [(0, False), (0, True), (1, False), (1, True)],
)
def test_eject_disk_suppresses_failure_only_when_quiet(returncode, quiet):
    """Accept successful ejection in both modes, suppressing only quiet-mode failures."""

    disk = burn.Disk("disk4", "SD Card", 32 * 1024**3, "USB", False, True, True)
    eject_result = subprocess.CompletedProcess(
        ["diskutil", "eject", "/dev/disk4"],
        returncode,
        stderr=b"device is busy",
    )
    with mock.patch.object(burn, "run", return_value=eject_result) as run:
        if returncode != 0 and not quiet:
            expected_error_message = "Could not eject /dev/disk4: device is busy"
            with pytest.raises(burn.BurnError) as raised:
                burn.eject_disk(disk, quiet=quiet)
            assert type(raised.value) is burn.BurnError
            assert str(raised.value) == expected_error_message
        else:
            burn.eject_disk(disk, quiet=quiet)

    run.assert_called_once_with(["diskutil", "eject", "/dev/disk4"], check=False)


def test_process_launch_errors_are_user_facing():
    with mock.patch.object(burn.subprocess, "Popen", side_effect=PermissionError("denied")):
        with pytest.raises(burn.BurnError, match="Could not start sudo"):
            burn.popen_or_error(["sudo", "-n", "dd"])

    with mock.patch.object(burn.subprocess, "run", side_effect=PermissionError("denied")):
        with pytest.raises(burn.BurnError, match="Could not start diskutil"):
            burn.run(["diskutil", "list"])


@pytest.mark.parametrize(
    ("operation", "stderr_outcome", "expected_error_message"),
    [
        (
            "integrity-write",
            b"dd: /dev/rdisk4: Input/output error",
            "Integrity test write failed for /dev/disk4: dd: /dev/rdisk4: Input/output error",
        ),
        (
            "image-write",
            b"dd: /dev/rdisk4: Input/output error",
            "dd could not write the image (exit code 1): dd: /dev/rdisk4: Input/output error",
        ),
        (
            "integrity-write",
            b"",
            "Integrity test write failed for /dev/disk4: dd closed its input pipe without an error message",
        ),
        (
            "image-write",
            b"",
            "dd could not write the image (exit code 1): dd closed its input pipe without an error message",
        ),
        (
            "image-write",
            OSError("stderr pipe failed"),
            "dd could not write the image: could not read stderr: stderr pipe failed",
        ),
        (
            "integrity-write",
            OSError("stderr pipe failed"),
            "Integrity test write failed for /dev/disk4: could not read stderr: stderr pipe failed",
        ),
    ],
    ids=[
        "integrity-stderr",
        "image-stderr",
        "integrity-empty-stderr",
        "image-empty-stderr",
        "image-stderr-read-error",
        "integrity-stderr-read-error",
    ],
)
def test_broken_dd_pipe_cleans_up_and_reports_diagnostic(
    operation, stderr_outcome, expected_error_message
):
    """Clean up dd, skip sync, and report stderr or a precise fallback after a broken pipe."""

    class BrokenInput(RecordingInput):
        def write(self, data):
            raise BrokenPipeError(32, "Broken pipe")

    class FailedProcess(FakeDDProcess):
        def __init__(self):
            super().__init__()
            self.input_stream = BrokenInput(self.events)
            self.stdin = self.input_stream

        def communicate(self):
            assert self.stdin is None
            self.communicate_calls += 1
            if isinstance(stderr_outcome, OSError):
                raise stderr_outcome
            self.returncode = 1
            return None, stderr_outcome

    disk = burn.Disk("disk4", "SD Card", 1024, "USB", False, True, True)
    image = burn.ImageSpec(
        Path("ubuntu.img"),
        None,
        "test",
        uncompressed_size=4,
        file_identity=(1, 2, 4, 5),
    )
    source_context = burn.contextlib.nullcontext(io.BytesIO(b"data"))
    sudo_session = mock.Mock(spec=burn.SudoSession)
    process = FailedProcess()
    with mock.patch.object(
        burn, "verified_source_stream", return_value=source_context
    ), mock.patch.object(
        burn, "ensure_same_disk"
    ), mock.patch.object(burn, "unmount_disk"), mock.patch.object(
        burn, "popen_or_error", return_value=process
    ) as popen, mock.patch.object(burn.os, "killpg") as killpg, mock.patch.object(
        burn, "run"
    ) as run, pytest.raises(burn.BurnError) as raised:
        run_transfer(operation, disk, image, sudo_session)
    assert type(raised.value) is burn.BurnError
    assert str(raised.value) == expected_error_message
    popen_kwargs = popen.call_args.kwargs
    assert popen_kwargs["preexec_fn"] is burn.os.setpgrp
    assert "start_new_session" not in popen_kwargs
    sudo_session.keep_alive.assert_called_once_with()
    sudo_session.authenticate.assert_not_called()
    run.assert_not_called()
    assert process.input_stream.was_closed is True
    assert process.stdin is None
    assert process.communicate_calls == 1
    if isinstance(stderr_outcome, OSError):
        killpg.assert_called_once_with(process.pid, burn.signal.SIGTERM)
        assert process.wait_calls == 1
    else:
        killpg.assert_not_called()
        assert process.wait_calls == 0


def test_http_and_ssh_read_errors_are_user_facing():
    response = mock.MagicMock()
    response.__enter__.return_value.read.side_effect = http.client.IncompleteRead(b"partial", 10)
    with mock.patch.object(burn.urllib.request, "urlopen", return_value=response):
        with pytest.raises(burn.BurnError):
            burn.fetch_bytes("https://example.invalid/file")

    with mock.patch.object(burn.Path, "is_file", return_value=True), mock.patch.object(
        burn.Path, "read_text", side_effect=PermissionError("denied")
    ):
        with pytest.raises(burn.BurnError, match="SSH public key"):
            burn.find_ssh_public_key("/tmp/key.pub")


def test_integrity_pattern_is_seeded_and_offset_stable():
    """Keep slices stable for one seed while changing the pattern for another seed."""

    size = burn.INTEGRITY_PATTERN_BLOCK_SIZE
    first_seed = b"a" * 32
    second_seed = b"b" * 32
    combined = burn.integrity_pattern(first_seed, 0, size * 2)
    split = burn.integrity_pattern(first_seed, 0, size) + burn.integrity_pattern(first_seed, size, size)
    slices = [(17, 113), (size - 29, 83), (size + 41, 97)]

    assert len(combined) == size * 2
    assert combined == split
    for offset, length in slices:
        generated_slice = burn.integrity_pattern(first_seed, offset, length)
        assert len(generated_slice) == length
        assert generated_slice == combined[offset : offset + length]
    assert combined[:size] != combined[size:]
    assert burn.integrity_pattern(first_seed, 0, 512) != burn.integrity_pattern(first_seed, 512, 512)
    second_pattern = burn.integrity_pattern(second_seed, 0, 512)
    assert len(second_pattern) == 512
    assert combined[:512] != second_pattern
    assert burn.integrity_pattern(first_seed, size + 41, 97) != burn.integrity_pattern(
        second_seed, size + 41, 97
    )


def test_empty_image_is_rejected():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "empty.img"
        path.touch()
        with pytest.raises(burn.BurnError, match="empty"):
            burn.uncompressed_image_size(path)


def test_truncated_xz_is_reported_as_burn_error():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "broken.img.xz"
        path.write_bytes(lzma.compress(b"image contents")[:-5])
        with pytest.raises(burn.BurnError):
            burn.uncompressed_image_size(path)


def test_inventory_write_error_is_user_facing():
    with tempfile.TemporaryDirectory() as directory, mock.patch.object(
        burn.Path, "mkdir", side_effect=PermissionError("denied")
    ):
        with pytest.raises(burn.BurnError, match="inventory"):
            burn.write_inventory(Path(directory) / "inventory.ini", ["pi-1.local"], "pomponchik")


def test_verified_source_rejects_replaced_image():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "ubuntu.img"
        path.write_bytes(b"original")
        image = burn.ImageSpec(
            path,
            None,
            "test",
            uncompressed_size=8,
            file_identity=burn.image_file_identity(path),
        )
        replacement = Path(directory) / "replacement.img"
        replacement.write_bytes(b"changed!")
        replacement.replace(path)
        with pytest.raises(burn.BurnError, match="changed"):
            with burn.verified_source_stream(image):
                pass


def test_noninteractive_validation():
    args = argparse.Namespace(
        count=2,
        check_cards=False,
        ssid="wifi",
        wifi_password_env="WIFI",
        prefix="pi",
        start_number=1,
        device=["/dev/disk4"],
        inventory=False,
        yes=True,
        non_interactive=True,
        sha256=None,
        image=None,
        auth_mode="ssh-key",
        user_password_env=None,
        ssh_public_key=None,
    )
    with pytest.raises(burn.BurnError):
        burn.validate_args(args)

    for invalid in ("/dev/disk4s1", "typo", "/tmp/disk4"):
        with pytest.raises(burn.BurnError):
            burn.normalize_disk_identifier(invalid)
    assert burn.normalize_disk_identifier("/dev/rdisk4") == "disk4"


def test_image_cache_option_is_not_exposed():
    args = burn.parse_args([])
    assert not hasattr(args, "cache_dir")


def test_keep_image_on_failure_argument_accepts_directory():
    """Expose an optional failure-artifact directory without making it mandatory."""

    assert burn.parse_args([]).keep_image_on_failure is None
    assert burn.parse_args(["--keep-image-on-failure", "diagnostics"]).keep_image_on_failure == "diagnostics"
    noninteractive_arguments = [
        "--non-interactive",
        "--count",
        "1",
        "--no-check",
        "--ssid",
        "wifi",
        "--wifi-password-env",
        "WIFI",
        "--prefix",
        "pi",
        "--auth-mode",
        "ssh-key",
        "--device",
        "/dev/disk4",
        "--no-inventory",
        "--yes",
    ]
    for optional_arguments in ([], ["--keep-image-on-failure", "diagnostics"]):
        noninteractive_args = burn.parse_args(noninteractive_arguments + optional_arguments)
        burn.validate_args(noninteractive_args)


def test_remote_image_is_moved_to_unique_failure_directory(tmp_path):
    """Move a downloaded image and write a sanitized report when preservation is requested."""

    image_paths = []
    for number in (1, 2):
        temporary_directory = tmp_path / "temporary-{}".format(number)
        temporary_directory.mkdir()
        image_path = temporary_directory / "ubuntu.img.xz"
        image_path.write_bytes("compressed image {}".format(number).encode())
        image_paths.append(image_path)
    disk = burn.Disk("disk4", "SD Card", 32 * 1024**3, "USB", False, True, True)
    verification_error = burn.BurnError(
        "Write verification failed at byte 4\n"
        "Expected block SHA-256: {}\n"
        "Repeated card block 2 SHA-256: {}".format("b" * 64, "c" * 64)
    )

    real_replace = burn.os.replace
    with mock.patch.object(burn.os, "replace", wraps=real_replace) as replace:
        artifacts = []
        for image_path in image_paths:
            image = burn.ImageSpec(
                image_path,
                "a" * 64,
                "https://user:password@example.com/ubuntu.img.xz?token=secret",
                uncompressed_size=1024,
            )
            artifacts.append(
                burn.preserve_failure_artifacts(
                    tmp_path / "diagnostics", image, 1024, disk, verification_error
                )
            )

    saved_image, report = artifacts[0]
    assert saved_image is not None
    assert saved_image.read_bytes() == b"compressed image 1"
    assert saved_image.name == "ubuntu.img.xz"
    assert saved_image.parent == report.parent
    assert saved_image.parent.name.startswith("piburn-failure-")
    assert artifacts[1][0] is not None
    assert artifacts[1][0].name == "ubuntu.img.xz"
    assert artifacts[1][0].parent != saved_image.parent
    assert all(image_path.exists() is False for image_path in image_paths)
    contents = report.read_text()
    assert "Source: https://example.com/ubuntu.img.xz" in contents
    assert "Compressed SHA-256: {}".format("a" * 64) in contents
    assert "Decompressed size: 1024" in contents
    assert "Disk: /dev/disk4" in contents
    assert "Error: {}".format(verification_error) in contents
    assert "password" not in contents
    assert "token=secret" not in contents
    assert replace.call_args_list == [
        mock.call(str(image_paths[0]), str(artifacts[0][0])),
        mock.call(str(image_paths[1]), str(artifacts[1][0])),
    ]


def test_remote_image_preservation_never_falls_back_to_copying(tmp_path):
    """Leave the source intact if an atomic image move unexpectedly fails."""

    image_path = tmp_path / "ubuntu.img.xz"
    image_path.write_bytes(b"compressed image")
    image = burn.ImageSpec(
        image_path,
        "a" * 64,
        "https://example.com/ubuntu.img.xz",
        uncompressed_size=1024,
    )

    with mock.patch.object(
        burn.os, "replace", side_effect=OSError("cross-device link")
    ), pytest.raises(burn.BurnError) as raised:
        burn.preserve_failure_artifacts(
            tmp_path / "diagnostics",
            image,
            1024,
            None,
            burn.BurnError("verification failed"),
        )

    assert "cross-device link" in str(raised.value)
    assert image_path.read_bytes() == b"compressed image"
    assert list((tmp_path / "diagnostics").rglob("ubuntu.img.xz")) == []


def test_main_does_not_duplicate_local_image_on_failure(tmp_path, capsys, monkeypatch):
    """Keep a local image in place and preserve only a report after a run failure."""

    image_path = tmp_path / "ubuntu.img"
    image_path.write_bytes(b"local image")
    diagnostics = tmp_path / "diagnostics"
    primary_error = burn.SudoError("authorization failed")
    monkeypatch.setenv("WIFI", "secret")
    with mock.patch.object(
        burn, "find_ssh_public_key", return_value=("ssh-ed25519 AAAA test", None)
    ), mock.patch.object(burn, "ensure_sudo", side_effect=primary_error), pytest.raises(
        burn.SudoError
    ) as raised:
        burn.main(
            [
                "--non-interactive",
                "--count",
                "1",
                "--no-check",
                "--ssid",
                "wifi",
                "--wifi-password-env",
                "WIFI",
                "--prefix",
                "pi",
                "--auth-mode",
                "ssh-key",
                "--device",
                "/dev/disk4",
                "--image",
                str(image_path),
                "--no-inventory",
                "--yes",
                "--keep-image-on-failure",
                str(diagnostics),
            ]
        )

    assert raised.value is primary_error
    assert image_path.read_bytes() == b"local image"
    artifact_directories = list(diagnostics.glob("piburn-failure-*"))
    assert len(artifact_directories) == 1
    report = artifact_directories[0] / "diagnostic.txt"
    assert list(report.parent.iterdir()) == [report]
    assert "Source: {}".format(image_path) in report.read_text()
    captured_output = capsys.readouterr()
    assert "Local image remains at: {}".format(image_path) in captured_output.err
    assert str(report.resolve()) in captured_output.err


def test_failure_artifact_destination_error_is_user_facing(tmp_path):
    """Wrap a real destination-directory creation failure for the caller."""

    blocked_destination = tmp_path / "not-a-directory"
    blocked_destination.write_text("file")
    image_path = tmp_path / "ubuntu.img"
    image_path.write_bytes(b"local image")
    image = burn.ImageSpec(image_path, None, str(image_path), uncompressed_size=11)

    with pytest.raises(burn.BurnError, match="Could not preserve failure artifacts"):
        burn.preserve_failure_artifacts(
            blocked_destination,
            image,
            11,
            None,
            burn.BurnError("primary failure"),
        )

    assert image_path.read_bytes() == b"local image"


@pytest.mark.parametrize(
    ("outcome", "keep_on_failure"),
    [("success", False), ("failure", False), ("success", True)],
)
def test_main_removes_temporary_image_without_a_preserved_failure(outcome, keep_on_failure, tmp_path, monkeypatch):
    """Remove a downloaded image after success or an unpreserved failure."""

    disk = burn.Disk("disk4", "SD Card", 32 * 1024**3, "USB", False, True, True)
    sudo_session = mock.Mock(spec=burn.SudoSession)
    downloaded_paths = []

    def resolve_remote(_image, _sha256, download_directory):
        path = download_directory / "ubuntu.img.xz"
        path.write_bytes(b"temporary image")
        downloaded_paths.append(path)
        return burn.ImageSpec(
            path,
            "a" * 64,
            "https://example.com/ubuntu.img.xz",
            uncompressed_size=1024,
        )

    arguments = [
        "--non-interactive",
        "--count",
        "1",
        "--no-check",
        "--ssid",
        "wifi",
        "--wifi-password-env",
        "WIFI",
        "--prefix",
        "pi",
        "--auth-mode",
        "ssh-key",
        "--device",
        "/dev/disk4",
        "--no-inventory",
        "--yes",
    ]
    if keep_on_failure:
        arguments.extend(["--keep-image-on-failure", str(tmp_path / "diagnostics")])
    write_result = ("b" * 64, 1024)
    if outcome == "failure":
        write_result = burn.BurnError("write failed")
    monkeypatch.setenv("WIFI", "wifi-secret")
    with mock.patch.object(
        burn, "find_ssh_public_key", return_value=("ssh-ed25519 AAAA test", None)
    ), mock.patch.object(burn, "resolve_image", side_effect=resolve_remote), mock.patch.object(
        burn, "ensure_sudo", return_value=sudo_session
    ), mock.patch.object(burn, "wait_for_disk", return_value=disk), mock.patch.object(
        burn, "write_image", side_effect=[write_result]
    ), mock.patch.object(burn, "write_cloud_init"), mock.patch.object(
        burn, "eject_disk"
    ), mock.patch.object(burn, "preserve_failure_artifacts") as preserve:
        if outcome == "failure":
            with pytest.raises(burn.BurnError, match="write failed"):
                burn.main(arguments)
        else:
            assert burn.main(arguments) == 0

    assert len(downloaded_paths) == 1
    assert downloaded_paths[0].exists() is False
    preserve.assert_not_called()
    diagnostics = tmp_path / "diagnostics"
    if keep_on_failure:
        assert diagnostics.is_dir()
        assert list(diagnostics.iterdir()) == []
        assert downloaded_paths[0].parent.parent == diagnostics
    else:
        assert diagnostics.exists() is False


@pytest.mark.parametrize(
    "primary_failure",
    [burn.BurnError("inventory write failed"), KeyboardInterrupt("inventory cancelled")],
    ids=["error", "cancellation"],
)
def test_main_preserves_secret_free_report_after_inventory_failure_or_cancellation(
    primary_failure, tmp_path, capsys, monkeypatch
):
    """Preserve the remote image and selected-disk report without leaking configured secrets."""

    disk = burn.Disk(
        "disk4",
        "SD Card",
        32 * 1024**3,
        "USB",
        False,
        True,
        True,
        "IODeviceTree:/card-reader",
        "MEDIA-UUID",
    )
    sudo_session = mock.Mock(spec=burn.SudoSession)
    diagnostics = tmp_path / "diagnostics"

    def resolve_remote(_image, _sha256, download_directory):
        path = download_directory / "ubuntu.img.xz"
        path.write_bytes(b"temporary image")
        return burn.ImageSpec(
            path,
            "a" * 64,
            "https://url-user:url-password@example.com:8443/ubuntu.img.xz"
            "?token=url-secret#token=fragment-secret",
            uncompressed_size=1024,
        )

    monkeypatch.setenv("WIFI", "wifi-secret")
    monkeypatch.setenv("LOGIN", "login-secret")
    with mock.patch.object(burn, "sha512_crypt", return_value="password-hash-secret"), mock.patch.object(
        burn, "resolve_image", side_effect=resolve_remote
    ), mock.patch.object(burn, "ensure_sudo", return_value=sudo_session), mock.patch.object(
        burn, "wait_for_disk", return_value=disk
    ), mock.patch.object(burn, "write_image", return_value=("b" * 64, 1024)), mock.patch.object(
        burn, "write_cloud_init"
    ), mock.patch.object(burn, "eject_disk") as eject, mock.patch.object(
        burn, "write_inventory", side_effect=primary_failure
    ), pytest.raises(type(primary_failure)) as raised:
        burn.main(
            [
                "--non-interactive",
                "--count",
                "1",
                "--no-check",
                "--ssid",
                "wifi",
                "--wifi-password-env",
                "WIFI",
                "--prefix",
                "pi",
                "--auth-mode",
                "password",
                "--user-password-env",
                "LOGIN",
                "--device",
                "/dev/disk4",
                "--inventory",
                "--inventory-path",
                str(tmp_path / "inventory.ini"),
                "--yes",
                "--keep-image-on-failure",
                str(diagnostics),
            ]
        )

    assert raised.value is primary_failure
    artifact_directories = list(diagnostics.glob("piburn-failure-*"))
    assert len(artifact_directories) == 1
    saved_image = artifact_directories[0] / "ubuntu.img.xz"
    report = artifact_directories[0] / "diagnostic.txt"
    assert saved_image.read_bytes() == b"temporary image"
    report_text = report.read_text()
    assert "Source: https://example.com:8443/ubuntu.img.xz" in report_text
    assert "Compressed SHA-256: {}".format("a" * 64) in report_text
    assert "Decompressed size: 1024" in report_text
    assert "Disk: /dev/disk4" in report_text
    assert json.dumps(disk.fingerprint) in report_text
    assert "Error: {}".format(primary_failure) in report_text
    for secret in (
        "wifi-secret",
        "login-secret",
        "password-hash-secret",
        "url-user",
        "url-password",
        "url-secret",
        "fragment-secret",
    ):
        assert secret not in report_text
    captured_output = capsys.readouterr()
    assert str(saved_image.resolve()) in captured_output.err
    assert str(report.resolve()) in captured_output.err
    eject.assert_called_once_with(disk)


@pytest.mark.parametrize(
    "preservation_error",
    [RuntimeError("destination is unwritable"), KeyboardInterrupt("preservation cancelled")],
    ids=["error", "cancellation"],
)
def test_artifact_preservation_failure_does_not_mask_primary_error(
    preservation_error, tmp_path, capsys, monkeypatch
):
    """Warn about artifact failure while propagating the original run error unchanged."""

    image = burn.ImageSpec(Path("ubuntu.img.xz"), "a" * 64, "https://example.com/ubuntu.img.xz", 1024)
    primary_error = burn.SudoError("authorization failed")
    monkeypatch.setenv("WIFI", "secret")
    with mock.patch.object(
        burn, "find_ssh_public_key", return_value=("ssh-ed25519 AAAA test", None)
    ), mock.patch.object(burn, "resolve_image", return_value=image), mock.patch.object(
        burn, "ensure_sudo", side_effect=primary_error
    ), mock.patch.object(
        burn,
        "preserve_failure_artifacts",
        side_effect=preservation_error,
    ) as preserve, pytest.raises(burn.SudoError) as raised:
        burn.main(
            [
                "--non-interactive",
                "--count",
                "1",
                "--no-check",
                "--ssid",
                "wifi",
                "--wifi-password-env",
                "WIFI",
                "--prefix",
                "pi",
                "--auth-mode",
                "ssh-key",
                "--device",
                "/dev/disk4",
                "--no-inventory",
                "--yes",
                "--keep-image-on-failure",
                str(tmp_path),
            ]
        )

    assert raised.value is primary_error
    preserve.assert_called_once_with(tmp_path, image, 1024, None, primary_error)
    assert capsys.readouterr().err == "Warning: {}\n".format(preservation_error)


def test_artifact_staging_failure_does_not_mask_primary_error(tmp_path, capsys, monkeypatch):
    """Keep the run error when the remote-image staging directory cannot be prepared."""

    blocked_destination = tmp_path / "not-a-directory"
    blocked_destination.write_text("file")
    downloaded_paths = []
    primary_error = burn.SudoError("authorization failed")

    def resolve_remote(_image, _sha256, download_directory):
        path = download_directory / "ubuntu.img.xz"
        path.write_bytes(b"temporary image")
        downloaded_paths.append(path)
        return burn.ImageSpec(
            path,
            "a" * 64,
            "https://example.com/ubuntu.img.xz",
            uncompressed_size=1024,
        )

    monkeypatch.setenv("WIFI", "secret")
    with mock.patch.object(
        burn, "find_ssh_public_key", return_value=("ssh-ed25519 AAAA test", None)
    ), mock.patch.object(burn, "resolve_image", side_effect=resolve_remote), mock.patch.object(
        burn, "ensure_sudo", side_effect=primary_error
    ), mock.patch.object(burn, "preserve_failure_artifacts") as preserve, pytest.raises(
        burn.SudoError
    ) as raised:
        burn.main(
            [
                "--non-interactive",
                "--count",
                "1",
                "--no-check",
                "--ssid",
                "wifi",
                "--wifi-password-env",
                "WIFI",
                "--prefix",
                "pi",
                "--auth-mode",
                "ssh-key",
                "--device",
                "/dev/disk4",
                "--no-inventory",
                "--yes",
                "--keep-image-on-failure",
                str(blocked_destination),
            ]
        )

    assert raised.value is primary_error
    assert len(downloaded_paths) == 1
    assert downloaded_paths[0].exists() is False
    preserve.assert_not_called()
    assert "Warning: could not prepare failure artifacts:" in capsys.readouterr().err


@pytest.mark.parametrize("value", [0, -1])
def test_start_number_must_be_positive(value):
    args = burn.parse_args(["--start-number", str(value)])
    with pytest.raises(burn.BurnError, match="--start-number must be a positive integer"):
        burn.validate_args(args)


def test_interactive_start_number_prompt_follows_prefix_and_defaults_to_one(monkeypatch):
    """Prompt for the prefix first, retry the start number, and default it to one."""

    disk = burn.Disk("disk4", "SD Card", 32 * 1024**3, "USB", False, True, True)
    image = burn.ImageSpec(Path("ubuntu.img"), None, "test image", uncompressed_size=1024)
    sudo_session = mock.Mock(spec=burn.SudoSession)
    monkeypatch.setenv("WIFI", "secret")

    with mock.patch.object(
        burn, "input", side_effect=["node", "invalid", ""]
    ) as input_mock, mock.patch.object(
        burn, "find_ssh_public_key", return_value=("ssh-ed25519 AAAA test", None)
    ), mock.patch.object(
        burn, "resolve_image", return_value=image
    ), mock.patch.object(
        burn, "ensure_sudo", return_value=sudo_session
    ), mock.patch.object(
        burn, "get_disk", return_value=disk
    ), mock.patch.object(
        burn, "write_image", return_value=("b" * 64, 1024)
    ), mock.patch.object(
        burn, "write_cloud_init"
    ) as cloud_init, mock.patch.object(
        burn, "eject_disk"
    ):
        exit_code = burn.main(
            [
                "--count",
                "1",
                "--no-check",
                "--ssid",
                "wifi",
                "--wifi-password-env",
                "WIFI",
                "--auth-mode",
                "ssh-key",
                "--device",
                "/dev/disk4",
                "--no-inventory",
                "--yes",
            ]
        )

    assert exit_code == 0
    assert input_mock.call_args_list == [
        mock.call("Hostname prefix [pi]: "),
        mock.call("Starting hostname number [1]: "),
        mock.call("Starting hostname number [1]: "),
    ]
    assert cloud_init.call_args.args[1] == "node-1"
    sudo_session.authenticate.assert_not_called()


@pytest.mark.parametrize("check_cards", [False, True], ids=["unchecked", "checked"])
def test_noninteractive_flow_can_reuse_same_card_reader(check_cards, tmp_path, capsys, monkeypatch):
    """Process two cards in one reader with optional checks and one sudo session."""

    disk = burn.Disk("disk4", "SD Card", 32 * 1024**3, "USB", False, True, True)
    downloaded_paths = []
    resolved_images = []
    sudo_session = mock.Mock(spec=burn.SudoSession)
    monkeypatch.setenv("WIFI", "secret")

    def fake_resolve(_image, _sha256, download_dir):
        path = download_dir / "ubuntu.img.xz"
        path.write_bytes(b"temporary image")
        downloaded_paths.append(path)
        image = burn.ImageSpec(path, "a" * 64, "test image", uncompressed_size=1024)
        resolved_images.append(image)
        return image

    inventory_path = tmp_path / "inventory.ini"
    with mock.patch.object(
        burn, "find_ssh_public_key", return_value=("ssh-ed25519 AAAA test", None)
    ), mock.patch.object(
        burn,
        "resolve_image",
        side_effect=fake_resolve,
    ), mock.patch.object(burn, "ensure_sudo", return_value=sudo_session) as ensure_sudo, mock.patch.object(
        burn, "get_disk", return_value=disk
    ), mock.patch.object(burn, "wait_for_disk", wraps=burn.wait_for_disk) as wait_for_disk, mock.patch.object(
        burn, "check_media", return_value=True
    ) as check_media, mock.patch.object(
        burn, "write_image", return_value=("b" * 64, 1024)
    ) as write_image, mock.patch.object(
        burn, "verify_written_image"
    ) as verify_image, mock.patch.object(
        burn, "write_cloud_init"
    ) as cloud_init, mock.patch.object(burn, "eject_disk"):
        arguments = [
                "--non-interactive",
                "--count",
                "2",
                "--check" if check_cards else "--no-check",
                "--ssid",
                "wifi",
                "--wifi-password-env",
                "WIFI",
                "--prefix",
                "pi",
                "--start-number",
                "5",
                "--auth-mode",
                "ssh-key",
                "--device",
                "/dev/disk4",
                "--device",
                "/dev/disk4",
                "--inventory",
                "--inventory-path",
                str(inventory_path),
                "--yes",
            ]
        exit_code = burn.main(arguments)
        assert exit_code == 0
        assert cloud_init.call_count == 2
        inventory_text = inventory_path.read_text()
        assert "pi-5.local" in inventory_text
        assert "pi-6.local" in inventory_text
        assert "ansible_user=pomponchik" in inventory_text
        ensure_sudo.assert_called_once_with()
        sudo_session.authenticate.assert_not_called()
        assert wait_for_disk.call_args_list == [
            mock.call("/dev/disk4", heartbeat=sudo_session.keep_alive),
            mock.call("/dev/disk4", heartbeat=sudo_session.keep_alive),
        ]
        assert [write_call.args[2] for write_call in write_image.call_args_list] == [
            sudo_session,
            sudo_session,
        ]
        if check_cards:
            assert check_media.call_args_list == [mock.call(disk, sudo_session)] * 2
            assert verify_image.call_args_list == [
                mock.call(disk, resolved_images[0], "b" * 64, 1024, sudo_session),
                mock.call(disk, resolved_images[0], "b" * 64, 1024, sudo_session),
            ]
        else:
            check_media.assert_not_called()
            verify_image.assert_not_called()
    assert len(downloaded_paths) == 1
    assert not downloaded_paths[0].exists()
    assert "SSH commands:\nssh pomponchik@pi-5.local\nssh pomponchik@pi-6.local\n" in capsys.readouterr().out


@pytest.mark.parametrize("selection_mode", ["forced", "interactive"])
@pytest.mark.parametrize("auth_mode", ["ssh-key", "password"])
def test_main_uses_one_sudo_session_for_all_cards(auth_mode, selection_mode, monkeypatch):
    """Reuse one sudo session for two cards across selection and login modes."""

    first_disk = burn.Disk("disk4", "First SD Card", 32 * 1024**3, "USB", False, True, True)
    second_disk = burn.Disk("disk5", "Second SD Card", 32 * 1024**3, "USB", False, True, True)
    image = burn.ImageSpec(Path("ubuntu.img"), None, "test image", uncompressed_size=1024)
    sudo_session = mock.Mock(spec=burn.SudoSession)
    write_results = [("b" * 64, 1001), ("c" * 64, 1002)]
    arguments = [
        "--count",
        "2",
        "--check",
        "--ssid",
        "wifi",
        "--wifi-password-env",
        "WIFI",
        "--prefix",
        "pi",
        "--start-number",
        "1",
        "--username",
        "nodeadmin",
        "--timezone",
        "UTC",
        "--auth-mode",
        auth_mode,
        "--no-inventory",
    ]
    if selection_mode == "forced":
        arguments.extend(
            [
                "--non-interactive",
                "--device",
                "/dev/disk4",
                "--device",
                "/dev/disk5",
                "--yes",
            ]
        )
    if auth_mode == "password":
        arguments.extend(["--user-password-env", "USER_PASSWORD"])
    expected_ssh_key = "ssh-ed25519 AAAA test" if auth_mode == "ssh-key" else None
    expected_password_hash = "hashed-secret" if auth_mode == "password" else None
    monkeypatch.setenv("WIFI", "secret")
    monkeypatch.setenv("USER_PASSWORD", "node-password")

    with mock.patch.object(
        burn, "find_ssh_public_key", return_value=("ssh-ed25519 AAAA test", None)
    ) as find_key, mock.patch.object(
        burn, "sha512_crypt", return_value="hashed-secret"
    ) as hash_password, mock.patch.object(burn, "resolve_image", return_value=image), mock.patch.object(
        burn, "ensure_sudo", return_value=sudo_session
    ) as ensure_sudo, mock.patch.object(burn, "get_disk", side_effect=[first_disk, second_disk]), mock.patch.object(
        burn, "wait_for_disk", wraps=burn.wait_for_disk
    ) as wait_for_disk, mock.patch.object(
        burn, "choose_disk", side_effect=[first_disk, second_disk]
    ) as choose_disk, mock.patch.object(burn, "check_media", return_value=True
    ) as check_media, mock.patch.object(
        burn, "write_image", side_effect=write_results
    ) as write_image, mock.patch.object(
        burn, "verify_written_image"
    ) as verify_image, mock.patch.object(burn, "write_cloud_init") as cloud_init, mock.patch.object(
        burn, "eject_disk"
    ) as eject:
        exit_code = burn.main(arguments)

    assert exit_code == 0
    ensure_sudo.assert_called_once_with()
    sudo_session.authenticate.assert_not_called()
    if auth_mode == "ssh-key":
        find_key.assert_called_once_with(None)
        hash_password.assert_not_called()
    else:
        find_key.assert_not_called()
        hash_password.assert_called_once_with("node-password")
    if selection_mode == "forced":
        assert wait_for_disk.call_args_list == [
            mock.call("/dev/disk4", heartbeat=sudo_session.keep_alive),
            mock.call("/dev/disk5", heartbeat=sudo_session.keep_alive),
        ]
        choose_disk.assert_not_called()
    else:
        wait_for_disk.assert_not_called()
        assert choose_disk.call_args_list == [
            mock.call(heartbeat=sudo_session.keep_alive),
            mock.call(heartbeat=sudo_session.keep_alive),
        ]
    assert check_media.call_args_list == [
        mock.call(first_disk, sudo_session),
        mock.call(second_disk, sudo_session),
    ]
    assert write_image.call_args_list == [
        mock.call(first_disk, image, sudo_session),
        mock.call(second_disk, image, sudo_session),
    ]
    assert verify_image.call_args_list == [
        mock.call(first_disk, image, write_results[0][0], write_results[0][1], sudo_session),
        mock.call(second_disk, image, write_results[1][0], write_results[1][1], sudo_session),
    ]
    assert cloud_init.call_args_list == [
        mock.call(
            first_disk,
            "pi-1",
            "nodeadmin",
            "UTC",
            expected_ssh_key,
            expected_password_hash,
            "wifi",
            "secret",
        ),
        mock.call(
            second_disk,
            "pi-2",
            "nodeadmin",
            "UTC",
            expected_ssh_key,
            expected_password_hash,
            "wifi",
            "secret",
        ),
    ]
    assert eject.call_args_list == [mock.call(first_disk), mock.call(second_disk)]
    assert sudo_session.keep_alive.call_count == (2 if selection_mode == "forced" else 0)


def test_main_does_not_eject_completed_card_again_when_next_card_wait_fails(monkeypatch):
    """Do not re-eject a completed card if authorization fails while awaiting the next card."""

    first_disk = burn.Disk("disk4", "First SD Card", 32 * 1024**3, "USB", False, True, True)
    image = burn.ImageSpec(Path("ubuntu.img"), None, "test image", uncompressed_size=1024)
    sudo_session = mock.Mock(spec=burn.SudoSession)
    sudo_error = burn.SudoError("administrator authorization failed")
    monkeypatch.setenv("WIFI", "secret")
    with mock.patch.object(
        burn, "find_ssh_public_key", return_value=("ssh-ed25519 AAAA test", None)
    ), mock.patch.object(burn, "resolve_image", return_value=image), mock.patch.object(
        burn, "ensure_sudo", return_value=sudo_session
    ), mock.patch.object(
        burn, "wait_for_disk", side_effect=[first_disk, sudo_error]
    ) as wait_for_disk, mock.patch.object(
        burn, "check_media"
    ) as check_media, mock.patch.object(
        burn, "write_image", return_value=("b" * 64, 1024)
    ) as write_image, mock.patch.object(
        burn, "verify_written_image"
    ) as verify_image, mock.patch.object(
        burn, "write_cloud_init"
    ) as cloud_init, mock.patch.object(burn, "eject_disk") as eject, pytest.raises(
        burn.SudoError
    ) as raised:
        burn.main(
            [
                "--non-interactive",
                "--count",
                "2",
                "--no-check",
                "--ssid",
                "wifi",
                "--wifi-password-env",
                "WIFI",
                "--prefix",
                "pi",
                "--start-number",
                "1",
                "--auth-mode",
                "ssh-key",
                "--device",
                "/dev/disk4",
                "--device",
                "/dev/disk5",
                "--no-inventory",
                "--yes",
            ]
        )

    assert raised.value is sudo_error
    sudo_session.authenticate.assert_not_called()
    assert wait_for_disk.call_args_list == [
        mock.call("/dev/disk4", heartbeat=sudo_session.keep_alive),
        mock.call("/dev/disk5", heartbeat=sudo_session.keep_alive),
    ]
    check_media.assert_not_called()
    write_image.assert_called_once_with(first_disk, image, sudo_session)
    verify_image.assert_not_called()
    cloud_init.assert_called_once()
    eject.assert_called_once_with(first_disk)


def test_main_does_not_eject_rejected_forced_card_when_reselection_fails(monkeypatch):
    """Do not eject a rejected forced card if authorization fails during reselection."""

    bad_disk = burn.Disk("disk4", "Bad SD Card", 32 * 1024**3, "USB", False, True, True)
    image = burn.ImageSpec(Path("ubuntu.img"), None, "test image", uncompressed_size=1024)
    sudo_session = mock.Mock(spec=burn.SudoSession)
    sudo_error = burn.SudoError("administrator authorization failed")
    monkeypatch.setenv("WIFI", "secret")
    with mock.patch.object(
        burn, "find_ssh_public_key", return_value=("ssh-ed25519 AAAA test", None)
    ), mock.patch.object(burn, "resolve_image", return_value=image), mock.patch.object(
        burn, "ensure_sudo", return_value=sudo_session
    ), mock.patch.object(
        burn, "wait_for_disk", return_value=bad_disk
    ) as wait_for_disk, mock.patch.object(
        burn, "choose_disk", side_effect=sudo_error
    ) as choose_disk, mock.patch.object(
        burn, "check_media", return_value=False
    ) as check_media, mock.patch.object(
        burn, "failed_check_action", return_value=False
    ) as failed_check_action, mock.patch.object(
        burn, "write_image"
    ) as write_image, mock.patch.object(
        burn, "write_cloud_init"
    ) as cloud_init, mock.patch.object(burn, "eject_disk") as eject, pytest.raises(
        burn.SudoError
    ) as raised:
        burn.main(
            [
                "--count",
                "1",
                "--check",
                "--ssid",
                "wifi",
                "--wifi-password-env",
                "WIFI",
                "--prefix",
                "pi",
                "--start-number",
                "1",
                "--auth-mode",
                "ssh-key",
                "--device",
                "/dev/disk4",
                "--no-inventory",
                "--yes",
            ]
        )

    assert raised.value is sudo_error
    sudo_session.authenticate.assert_not_called()
    wait_for_disk.assert_called_once_with("/dev/disk4", heartbeat=sudo_session.keep_alive)
    choose_disk.assert_called_once_with(heartbeat=sudo_session.keep_alive)
    check_media.assert_called_once_with(bad_disk, sudo_session)
    failed_check_action.assert_called_once_with(heartbeat=sudo_session.keep_alive)
    write_image.assert_not_called()
    cloud_init.assert_not_called()
    eject.assert_not_called()


@pytest.mark.parametrize("continue_with_failed_card", [True, False], ids=["skip", "back"])
def test_main_forwards_heartbeat_through_interactive_failed_check_actions(
    continue_with_failed_card, monkeypatch
):
    """Forward the sudo heartbeat through card selection and the failed-check prompt for skip and back."""

    bad_disk = burn.Disk("disk4", "Bad SD Card", 32 * 1024**3, "USB", False, True, True)
    good_disk = burn.Disk("disk5", "Good SD Card", 32 * 1024**3, "USB", False, True, True)
    image = burn.ImageSpec(Path("ubuntu.img"), None, "test image", uncompressed_size=1024)
    sudo_session = mock.Mock(spec=burn.SudoSession)
    digest = "b" * 64
    selected_disks = [bad_disk] if continue_with_failed_card else [bad_disk, good_disk]
    check_results = [False] if continue_with_failed_card else [False, True]
    target_disk = bad_disk if continue_with_failed_card else good_disk
    monkeypatch.setenv("WIFI", "secret")

    with mock.patch.object(
        burn, "find_ssh_public_key", return_value=("ssh-ed25519 AAAA test", None)
    ), mock.patch.object(burn, "resolve_image", return_value=image), mock.patch.object(
        burn, "ensure_sudo", return_value=sudo_session
    ), mock.patch.object(burn, "choose_disk", side_effect=selected_disks) as choose_disk, mock.patch.object(
        burn, "check_media", side_effect=check_results
    ) as check_media, mock.patch.object(
        burn, "failed_check_action", return_value=continue_with_failed_card
    ) as failed_check_action, mock.patch.object(
        burn, "write_image", return_value=(digest, 1024)
    ) as write_image, mock.patch.object(burn, "verify_written_image"), mock.patch.object(
        burn, "write_cloud_init"
    ), mock.patch.object(burn, "eject_disk"):
        exit_code = burn.main(
            [
                "--count",
                "1",
                "--check",
                "--ssid",
                "wifi",
                "--wifi-password-env",
                "WIFI",
                "--prefix",
                "pi",
                "--start-number",
                "1",
                "--auth-mode",
                "ssh-key",
                "--no-inventory",
            ]
        )

    assert exit_code == 0
    sudo_session.authenticate.assert_not_called()
    assert choose_disk.call_args_list == [mock.call(heartbeat=sudo_session.keep_alive)] * len(selected_disks)
    assert check_media.call_args_list == [mock.call(disk, sudo_session) for disk in selected_disks]
    failed_check_action.assert_called_once_with(heartbeat=sudo_session.keep_alive)
    write_image.assert_called_once_with(target_disk, image, sudo_session)


@pytest.mark.parametrize("policy", ["abort", "skip"])
def test_noninteractive_main_honors_failed_check_policy(policy, monkeypatch):
    """Abort by default or continue explicitly after a failed non-interactive check."""

    disk = burn.Disk("disk4", "SD Card", 32 * 1024**3, "USB", False, True, True)
    image = burn.ImageSpec(Path("ubuntu.img"), None, "test image", uncompressed_size=1024)
    sudo_session = mock.Mock(spec=burn.SudoSession)
    digest = "b" * 64
    arguments = [
        "--non-interactive",
        "--count",
        "1",
        "--check",
        "--ssid",
        "wifi",
        "--wifi-password-env",
        "WIFI",
        "--prefix",
        "pi",
        "--auth-mode",
        "ssh-key",
        "--device",
        "/dev/disk4",
        "--no-inventory",
        "--yes",
    ]
    if policy == "skip":
        arguments.extend(["--on-check-failure", "skip"])
    monkeypatch.setenv("WIFI", "secret")

    with mock.patch.object(
        burn, "find_ssh_public_key", return_value=("ssh-ed25519 AAAA test", None)
    ), mock.patch.object(burn, "resolve_image", return_value=image), mock.patch.object(
        burn, "ensure_sudo", return_value=sudo_session
    ), mock.patch.object(burn, "get_disk", return_value=disk), mock.patch.object(
        burn, "check_media", return_value=False
    ) as check_media, mock.patch.object(
        burn, "write_image", return_value=(digest, 1024)
    ) as write_image, mock.patch.object(
        burn, "verify_written_image"
    ) as verify_image, mock.patch.object(
        burn, "write_cloud_init"
    ) as cloud_init, mock.patch.object(burn, "eject_disk") as eject:
        if policy == "abort":
            expected_error_message = "Card /dev/disk4 failed the integrity test"
            with pytest.raises(burn.BurnError, match=re.escape(expected_error_message)) as raised:
                burn.main(arguments)
            assert type(raised.value) is burn.BurnError
        else:
            assert burn.main(arguments) == 0

    check_media.assert_called_once_with(disk, sudo_session)
    sudo_session.authenticate.assert_not_called()
    if policy == "abort":
        write_image.assert_not_called()
        verify_image.assert_not_called()
        cloud_init.assert_not_called()
        eject.assert_called_once_with(disk, quiet=True)
    else:
        write_image.assert_called_once_with(disk, image, sudo_session)
        verify_image.assert_called_once_with(disk, image, digest, 1024, sudo_session)
        cloud_init.assert_called_once()
        eject.assert_called_once_with(disk)


@pytest.mark.parametrize(
    "failure_scenario",
    ["check", "write", "verify", "interrupt", "cloud-init", "eject", "emergency-eject"],
)
def test_main_quietly_ejects_current_card_after_failure_or_cancellation(
    failure_scenario, capsys, monkeypatch
):
    """Attempt quiet ejection after failure without masking the primary exception."""

    disk = burn.Disk("disk4", "SD Card", 32 * 1024**3, "USB", False, True, True)
    image = burn.ImageSpec(Path("ubuntu.img"), None, "test image", uncompressed_size=1024)
    sudo_session = mock.Mock(spec=burn.SudoSession)
    expected_digest = "b" * 64
    sudo_error = burn.SudoError("administrator authorization failed")
    cancellation = KeyboardInterrupt("cancelled")
    stage_error = burn.BurnError("{} failed".format(failure_scenario))
    check_error = sudo_error if failure_scenario in ("check", "emergency-eject") else None
    write_error = None
    if failure_scenario == "write":
        write_error = sudo_error
    elif failure_scenario == "interrupt":
        write_error = cancellation
    verification_error = burn.BurnError("diagnostic verification failed")
    operations = mock.Mock()
    operations.check.return_value = True
    operations.write.return_value = (expected_digest, 1024)
    operations.check.side_effect = check_error
    operations.write.side_effect = write_error
    operations.verify.side_effect = verification_error if failure_scenario == "verify" else None
    operations.cloud_init.side_effect = stage_error if failure_scenario == "cloud-init" else None
    if failure_scenario == "eject":
        operations.eject.side_effect = [stage_error, None]
    elif failure_scenario == "emergency-eject":
        operations.eject.side_effect = burn.BurnError("emergency eject failed")
    monkeypatch.setenv("WIFI", "secret")

    with mock.patch.object(
        burn, "find_ssh_public_key", return_value=("ssh-ed25519 AAAA test", None)
    ), mock.patch.object(burn, "resolve_image", return_value=image), mock.patch.object(
        burn, "ensure_sudo", return_value=sudo_session
    ), mock.patch.object(burn, "get_disk", return_value=disk), mock.patch.object(
        burn, "check_media", side_effect=operations.check
    ), mock.patch.object(
        burn, "write_image", side_effect=operations.write
    ), mock.patch.object(
        burn, "verify_written_image", side_effect=operations.verify
    ), mock.patch.object(
        burn, "write_cloud_init", side_effect=operations.cloud_init
    ), mock.patch.object(
        burn, "eject_disk", side_effect=operations.eject
    ), pytest.raises((burn.BurnError, KeyboardInterrupt)) as raised:
        burn.main(
            [
                "--non-interactive",
                "--count",
                "1",
                "--check",
                "--ssid",
                "wifi",
                "--wifi-password-env",
                "WIFI",
                "--prefix",
                "pi",
                "--auth-mode",
                "ssh-key",
                "--device",
                "/dev/disk4",
                "--no-inventory",
                "--yes",
            ]
        )

    if failure_scenario == "interrupt":
        assert raised.value is cancellation
    elif failure_scenario == "verify":
        assert raised.value is verification_error
    elif failure_scenario in ("cloud-init", "eject"):
        assert raised.value is stage_error
    else:
        assert raised.value is sudo_error
    sudo_session.authenticate.assert_not_called()
    expected_operations = [mock.call.check(disk, sudo_session)]
    if failure_scenario not in ("check", "emergency-eject"):
        expected_operations.append(mock.call.write(disk, image, sudo_session))
    if failure_scenario not in ("check", "write", "interrupt", "emergency-eject"):
        expected_operations.append(mock.call.verify(disk, image, expected_digest, 1024, sudo_session))
        if failure_scenario in ("cloud-init", "eject"):
            expected_operations.append(
                mock.call.cloud_init(
                    disk,
                    "pi-1",
                    "pomponchik",
                    mock.ANY,
                    "ssh-ed25519 AAAA test",
                    None,
                    "wifi",
                    "secret",
                )
            )
            if failure_scenario == "eject":
                expected_operations.append(mock.call.eject(disk))
    expected_operations.append(mock.call.eject(disk, quiet=True))
    assert operations.mock_calls == expected_operations
    captured_output = capsys.readouterr()
    if failure_scenario == "emergency-eject":
        assert "Warning: emergency ejection failed: emergency eject failed" in captured_output.err


@pytest.mark.parametrize("with_heartbeat", [True, False])
def test_wait_for_disk_polls_until_requested_media_appears(with_heartbeat, capsys):
    """Poll until the requested disk appears; optionally call the heartbeat before each
    poll, sleep after misses, and print each status once.
    """

    disk = burn.Disk("disk4", "SD Card", 32 * 1024**3, "USB", False, True, True)
    events = []
    poll_outcomes = iter([burn.BurnError("absent"), burn.BurnError("still absent"), disk])

    def heartbeat():
        events.append("heartbeat")

    def get_disk(_identifier):
        events.append("poll")
        poll_outcome = next(poll_outcomes)
        if isinstance(poll_outcome, Exception):
            raise poll_outcome
        return poll_outcome

    def sleep(_interval):
        events.append("sleep")

    with mock.patch.object(burn, "get_disk", side_effect=get_disk) as get_disk_mock, mock.patch.object(
        burn.os.path, "exists", return_value=False
    ) as exists, mock.patch.object(burn.time, "sleep", side_effect=sleep) as sleep_mock:
        assert (
            burn.wait_for_disk(
                "/dev/disk4",
                poll_interval=0.25,
                heartbeat=heartbeat if with_heartbeat else None,
            )
            is disk
        )
    expected_events = [
        "heartbeat",
        "poll",
        "sleep",
        "heartbeat",
        "poll",
        "sleep",
        "heartbeat",
        "poll",
    ]
    if not with_heartbeat:
        expected_events = ["poll", "sleep", "poll", "sleep", "poll"]
    assert events == expected_events
    assert get_disk_mock.call_args_list == [mock.call("/dev/disk4")] * 3
    assert exists.call_args_list == [mock.call("/dev/disk4")] * 2
    assert sleep_mock.call_args_list == [mock.call(0.25), mock.call(0.25)]
    captured_stdout = capsys.readouterr().out
    assert captured_stdout.count("Waiting for /dev/disk4.") == 1
    assert captured_stdout.count("Media detected: {}".format(disk.label)) == 1


def test_wait_for_disk_propagates_heartbeat_failure_before_polling():
    """Propagate a sudo heartbeat failure before polling or sleeping."""

    sudo_error = burn.SudoError("administrator authorization failed")
    heartbeat = mock.Mock(side_effect=sudo_error)
    with mock.patch.object(burn, "get_disk") as get_disk, mock.patch.object(
        burn.os.path, "exists"
    ) as exists, mock.patch.object(burn.time, "sleep") as sleep, pytest.raises(
        burn.SudoError
    ) as raised:
        burn.wait_for_disk("/dev/disk4", heartbeat=heartbeat)

    assert raised.value is sudo_error
    heartbeat.assert_called_once_with()
    get_disk.assert_not_called()
    exists.assert_not_called()
    sleep.assert_not_called()


def test_wait_for_disk_rejects_existing_unsuitable_device():
    """Propagate an existing device's validation error without retrying."""

    disk_error = burn.BurnError("the selected disk is not removable")
    heartbeat = mock.Mock()
    with mock.patch.object(burn, "get_disk", side_effect=disk_error) as get_disk, mock.patch.object(
        burn.os.path, "exists", return_value=True
    ) as exists, mock.patch.object(burn.time, "sleep") as sleep, pytest.raises(
        burn.BurnError
    ) as raised:
        burn.wait_for_disk("/dev/disk4", heartbeat=heartbeat)

    assert raised.value is disk_error
    heartbeat.assert_called_once_with()
    get_disk.assert_called_once_with("/dev/disk4")
    exists.assert_called_once_with("/dev/disk4")
    sleep.assert_not_called()
