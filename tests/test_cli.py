import argparse
import base64
import http.client
import io
import json
import lzma
import re
import subprocess
import tempfile
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


def test_disk_filter_rejects_internal_and_accepts_external():
    base = {
        "DeviceIdentifier": "disk4",
        "WholeDisk": True,
        "Internal": False,
        "RemovableMedia": True,
        "Ejectable": True,
        "WritableMedia": True,
        "VirtualOrPhysical": "Physical",
        "TotalSize": 32 * 1024**3,
        "MediaName": "SD Card",
        "BusProtocol": "USB",
    }
    assert burn.disk_from_info(base) is not None
    internal = dict(base, Internal=True)
    assert burn.disk_from_info(internal) is None
    virtual = dict(base, VirtualOrPhysical="Virtual", BusProtocol="Disk Image")
    assert burn.disk_from_info(virtual) is None
    external_ssd = dict(base, RemovableMedia=False, Ejectable=True, MediaName="External SSD")
    assert burn.disk_from_info(external_ssd) is None
    partition = dict(base, DeviceIdentifier="disk4s1", WholeDisk=False)
    assert burn.disk_from_info(partition) is None


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


def test_oversized_image_is_rejected_before_disk_operations():
    disk = burn.Disk("disk4", "SD Card", 1, "USB", False, True, True)
    image = burn.ImageSpec(Path("ubuntu.img"), None, "test", uncompressed_size=2)
    with mock.patch.object(burn, "refresh_sudo") as sudo, mock.patch.object(
        burn, "unmount_disk"
    ) as unmount, pytest.raises(burn.BurnError):
        burn.write_image(disk, image)
    sudo.assert_not_called()
    unmount.assert_not_called()


def test_changed_image_size_is_rejected_before_disk_operations():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "ubuntu.img"
        path.write_bytes(b"changed")
        disk = burn.Disk("disk4", "SD Card", 1024, "USB", False, True, True)
        stale = burn.ImageSpec(path, None, "test", uncompressed_size=3)
        with mock.patch.object(burn, "refresh_sudo") as sudo, pytest.raises(burn.BurnError, match="image size changed"):
            burn.write_image(disk, stale)
        sudo.assert_not_called()


def test_process_launch_errors_are_user_facing():
    with mock.patch.object(burn.subprocess, "Popen", side_effect=PermissionError("denied")):
        with pytest.raises(burn.BurnError, match="Could not start sudo"):
            burn.popen_or_error(["sudo", "-n", "dd"])

    with mock.patch.object(burn.subprocess, "run", side_effect=PermissionError("denied")):
        with pytest.raises(burn.BurnError, match="Could not start diskutil"):
            burn.run(["diskutil", "list"])


def test_broken_dd_pipe_reports_dd_stderr():
    class BrokenInput(io.BytesIO):
        def write(self, data):
            raise BrokenPipeError(32, "Broken pipe")

    class FailedProcess:
        def __init__(self):
            self.stdin = BrokenInput()
            self.returncode = 1

        def poll(self):
            return self.returncode

        def communicate(self):
            return None, b"dd: /dev/rdisk4: Input/output error"

    disk = burn.Disk("disk4", "SD Card", 1024, "USB", False, True, True)
    image = burn.ImageSpec(
        Path("ubuntu.img"),
        None,
        "test",
        uncompressed_size=4,
        file_identity=(1, 2, 4, 5),
    )
    source = burn.contextlib.nullcontext(io.BytesIO(b"data"))
    with mock.patch.object(burn, "verified_source_stream", return_value=source), mock.patch.object(
        burn, "refresh_sudo"
    ), mock.patch.object(burn, "ensure_same_disk"), mock.patch.object(burn, "unmount_disk"), mock.patch.object(
        burn, "popen_or_error", return_value=FailedProcess()
    ) as popen, pytest.raises(burn.BurnError, match="Input/output error"):
        burn.write_image(disk, image)
    kwargs = popen.call_args.kwargs
    assert kwargs["preexec_fn"] is burn.os.setpgrp
    assert "start_new_session" not in kwargs


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


def test_integrity_pattern_is_offset_stable():
    size = burn.INTEGRITY_PATTERN_BLOCK_SIZE
    combined = burn.integrity_pattern(0, size * 2)
    split = burn.integrity_pattern(0, size) + burn.integrity_pattern(size, size)
    assert combined == split
    assert combined[:size] != combined[size:]
    assert burn.integrity_pattern(0, 512) != burn.integrity_pattern(512, 512)


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


@pytest.mark.parametrize("value", [0, -1])
def test_start_number_must_be_positive(value):
    args = burn.parse_args(["--start-number", str(value)])
    with pytest.raises(burn.BurnError, match="--start-number must be a positive integer"):
        burn.validate_args(args)


def test_interactive_start_number_prompt_follows_prefix_and_defaults_to_one():
    disk = burn.Disk("disk4", "SD Card", 32 * 1024**3, "USB", False, True, True)
    image = burn.ImageSpec(Path("ubuntu.img"), None, "test image", uncompressed_size=1024)

    with mock.patch.dict(burn.os.environ, {"WIFI": "secret"}), mock.patch.object(
        burn, "input", side_effect=["node", "invalid", ""]
    ) as prompt, mock.patch.object(
        burn, "find_ssh_public_key", return_value=("ssh-ed25519 AAAA test", None)
    ), mock.patch.object(
        burn, "resolve_image", return_value=image
    ), mock.patch.object(
        burn, "ensure_sudo"
    ), mock.patch.object(
        burn, "get_disk", return_value=disk
    ), mock.patch.object(
        burn, "write_image", return_value=("b" * 64, 1024)
    ), mock.patch.object(
        burn, "write_cloud_init"
    ) as cloud_init, mock.patch.object(
        burn, "eject_disk"
    ):
        result = burn.main(
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

    assert result == 0
    assert prompt.call_args_list == [
        mock.call("Hostname prefix [pi]: "),
        mock.call("Starting hostname number [1]: "),
        mock.call("Starting hostname number [1]: "),
    ]
    assert cloud_init.call_args.args[1] == "node-1"


def test_noninteractive_flow_can_reuse_same_card_reader():
    disk = burn.Disk("disk4", "SD Card", 32 * 1024**3, "USB", False, True, True)
    downloaded_paths = []

    def fake_resolve(_image, _sha256, download_dir):
        path = download_dir / "ubuntu.img.xz"
        path.write_bytes(b"temporary image")
        downloaded_paths.append(path)
        return burn.ImageSpec(path, "a" * 64, "test image", uncompressed_size=1024)

    output = io.StringIO()
    with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
        burn.os.environ, {"WIFI": "secret"}
    ), mock.patch.object(burn, "find_ssh_public_key", return_value=("ssh-ed25519 AAAA test", None)), mock.patch.object(
        burn,
        "resolve_image",
        side_effect=fake_resolve,
    ), mock.patch.object(burn, "ensure_sudo"), mock.patch.object(
        burn, "get_disk", return_value=disk
    ), mock.patch.object(burn, "write_image", return_value=("b" * 64, 1024)), mock.patch.object(
        burn, "write_cloud_init"
    ) as cloud_init, mock.patch.object(burn, "eject_disk"), mock.patch.object(burn.sys, "stdout", output):
        inventory = Path(directory) / "inventory.ini"
        result = burn.main(
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
                "5",
                "--auth-mode",
                "ssh-key",
                "--device",
                "/dev/disk4",
                "--device",
                "/dev/disk4",
                "--inventory",
                "--inventory-path",
                str(inventory),
                "--yes",
            ]
        )
        assert result == 0
        assert cloud_init.call_count == 2
        assert "pi-5.local" in inventory.read_text()
        assert "pi-6.local" in inventory.read_text()
        assert "ansible_user=pomponchik" in inventory.read_text()
    assert len(downloaded_paths) == 1
    assert not downloaded_paths[0].exists()
    assert "SSH commands:\nssh pomponchik@pi-5.local\nssh pomponchik@pi-6.local\n" in output.getvalue()


def test_forced_device_waits_for_next_card():
    disk = burn.Disk("disk4", "SD Card", 32 * 1024**3, "USB", False, True, True)
    with mock.patch.object(
        burn, "get_disk", side_effect=[burn.BurnError("absent"), disk]
    ) as get_disk, mock.patch.object(burn.os.path, "exists", return_value=False), mock.patch.object(
        burn.time, "sleep"
    ) as sleep:
        assert burn.wait_for_disk("/dev/disk4") == disk
    assert get_disk.call_count == 2
    sleep.assert_called_once_with(1.0)
