import contextlib
import dataclasses
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from piburn import cli as burn

REAL_CHECK_MEDIA = burn.check_media
REAL_ENSURE_SAME_DISK = burn.ensure_same_disk
REAL_WAIT_FOR_SAME_DISK = burn.wait_for_same_disk
REAL_QUIETLY_EJECT_SAME_DISK = burn.quietly_eject_same_disk


class FakeSleepGuard:
    """Record the commit point of a guarded card attempt."""

    def __init__(self, events):
        self.events = events
        self.committed = False

    def commit(self):
        """Mark the attempt committed and expose its position in the event log."""
        self.committed = True
        self.events.append("power-commit")


@pytest.fixture
def main_rig(monkeypatch):
    """Isolate ``main`` while retaining an ordered trace of card operations."""
    events = []
    disk = burn.Disk(
        "disk4",
        "SD Card",
        32 * 1024**3,
        "USB",
        False,
        True,
        True,
        "IODeviceTree:/reader/card",
        "CARD-UUID",
    )
    image = burn.ImageSpec(Path("ubuntu.img"), None, "test image", uncompressed_size=1024)
    sudo_session = mock.Mock(spec=burn.SudoSession)
    sudo_session.keep_alive.side_effect = lambda: events.append("heartbeat")
    rig = SimpleNamespace(events=events, disk=disk, image=image, sudo_session=sudo_session)

    rig.resolve_image = mock.Mock(side_effect=lambda *_args: events.append("download") or image)
    rig.ensure_sudo = mock.Mock(return_value=sudo_session)
    rig.wait_for_disk = mock.Mock(side_effect=lambda *_args, **_kwargs: events.append("selection") or disk)
    rig.wait_for_same_disk = mock.Mock(side_effect=lambda *_args, **_kwargs: events.append("recovery-wait") or disk)
    rig.ensure_same_disk = mock.Mock(side_effect=lambda *_args: events.append("fingerprint") or disk)
    rig.quietly_eject_same_disk = mock.Mock(side_effect=lambda *_args: events.append("quiet-eject") or True)
    rig.check_media = mock.Mock(side_effect=lambda *_args: events.append("integrity") or True)
    rig.write_image = mock.Mock(side_effect=lambda *_args: events.append("write") or ("b" * 64, 1024))
    rig.verify_written_image = mock.Mock(side_effect=lambda *_args: events.append("verify"))
    rig.write_cloud_init = mock.Mock(side_effect=lambda *_args, **_kwargs: events.append("cloud-init"))
    rig.eject_disk = mock.Mock(side_effect=lambda *_args, **_kwargs: events.append("eject"))
    rig.write_inventory = mock.Mock(side_effect=lambda *_args: events.append("inventory"))
    rig.failed_check_action = mock.Mock(return_value=True)

    @contextlib.contextmanager
    def power_guard(session):
        """Provide a visible power-guard lifecycle around each attempt."""
        assert session is sudo_session
        guard = FakeSleepGuard(events)
        events.append("power-enter")
        try:
            yield guard
        finally:
            events.append("power-exit")

    @contextlib.contextmanager
    def mount_guard(selected_disk, session):
        """Record automatic-mount protection without starting the helper."""
        assert session is sudo_session
        events.append(("mount-enter", selected_disk.identifier))
        try:
            yield
        finally:
            events.append(("mount-exit", selected_disk.identifier))

    monkeypatch.setenv("WIFI", "secret")
    monkeypatch.setattr(burn, "find_ssh_public_key", mock.Mock(return_value=("ssh-ed25519 AAAA test", None)))
    monkeypatch.setattr(burn, "resolve_image", rig.resolve_image)
    monkeypatch.setattr(burn, "ensure_sudo", rig.ensure_sudo)
    monkeypatch.setattr(burn, "wait_for_disk", rig.wait_for_disk)
    monkeypatch.setattr(burn, "wait_for_same_disk", rig.wait_for_same_disk)
    monkeypatch.setattr(burn, "ensure_same_disk", rig.ensure_same_disk)
    monkeypatch.setattr(burn, "quietly_eject_same_disk", rig.quietly_eject_same_disk)
    monkeypatch.setattr(burn, "check_media", rig.check_media)
    monkeypatch.setattr(burn, "write_image", rig.write_image)
    monkeypatch.setattr(burn, "verify_written_image", rig.verify_written_image)
    monkeypatch.setattr(burn, "write_cloud_init", rig.write_cloud_init)
    monkeypatch.setattr(burn, "eject_disk", rig.eject_disk)
    monkeypatch.setattr(burn, "write_inventory", rig.write_inventory)
    monkeypatch.setattr(burn, "failed_check_action", rig.failed_check_action)
    monkeypatch.setattr(burn, "prevent_system_sleep", power_guard)
    monkeypatch.setattr(burn, "prevent_automatic_mounts", mount_guard)

    def run_main(*, check=True, count=1, inventory=False, on_check_failure="abort", non_interactive=True):
        """Run the CLI with deterministic arguments and the requested card policy."""
        arguments = [
            "--count",
            str(count),
            "--check" if check else "--no-check",
            "--ssid",
            "wifi",
            "--wifi-password-env",
            "WIFI",
            "--prefix",
            "pi",
            "--start-number",
            "4",
            "--auth-mode",
            "ssh-key",
        ]
        if non_interactive:
            arguments.insert(0, "--non-interactive")
        for _number in range(count):
            arguments.extend(["--device", "/dev/disk4"])
        arguments.extend(
            [
                "--inventory" if inventory else "--no-inventory",
                "--inventory-path",
                "inventory.ini",
                "--on-check-failure",
                on_check_failure,
                "--yes",
            ]
        )
        return burn.main(arguments)

    rig.run_main = run_main
    return rig


@pytest.mark.parametrize("check", [False, True], ids=["unchecked", "checked"])
def test_main_holds_power_guard_for_exact_card_critical_section(main_rig, check):
    """Start the guard after image resolution and card selection, and keep it through commit.

    The optional integrity check runs inside it; inventory creation remains
    outside after eject and commit.
    """
    assert main_rig.run_main(check=check, inventory=True) == 0

    protected_events = ["integrity", ("mount-enter", "disk4"), "write", "verify"] if check else [
        ("mount-enter", "disk4"),
        "write",
    ]
    protected_events.extend(
        [
            ("mount-exit", "disk4"),
            "cloud-init",
            "heartbeat",
            "fingerprint",
            "eject",
            "power-commit",
        ]
    )
    assert main_rig.events == [
        "download",
        "selection",
        "power-enter",
        *protected_events,
        "power-exit",
        "inventory",
    ]


def test_main_numbers_unlimited_sleep_attempts_and_eventually_succeeds(main_rig, capsys):
    """Complete a thirteenth attempt after twelve numbered sleep interruptions.

    No retry limit stops the sequence. Each interruption requests a recovery
    eject and waits for the same card; only success performs the regular eject
    and adds one inventory entry.
    """
    interrupted_attempt_count = 12
    sleep_error = burn.SystemSleepError("forced sleep")
    main_rig.write_image.side_effect = [sleep_error] * interrupted_attempt_count + [
        ("b" * 64, 1024)
    ]

    assert main_rig.run_main(check=False, inventory=True) == 0

    captured_output = capsys.readouterr().out
    attempt_headers = [
        line for line in captured_output.splitlines() if line.startswith("Preparation attempt ")
    ]
    assert attempt_headers == [
        "Preparation attempt {} for pi-4.local on /dev/disk4".format(attempt_number)
        for attempt_number in range(1, interrupted_attempt_count + 2)
    ]
    assert "maximum" not in captured_output.lower()
    assert captured_output.count("Done: /dev/disk4 was ejected") == 1
    assert main_rig.write_image.call_count == interrupted_attempt_count + 1
    assert main_rig.wait_for_same_disk.call_count == interrupted_attempt_count
    assert main_rig.quietly_eject_same_disk.call_count == interrupted_attempt_count
    quiet_eject_positions = [
        index for index, event in enumerate(main_rig.events) if event == "quiet-eject"
    ]
    assert len(quiet_eject_positions) == interrupted_attempt_count
    for quiet_eject_position in quiet_eject_positions:
        assert main_rig.events[quiet_eject_position - 1 : quiet_eject_position + 3] == [
            "power-exit",
            "quiet-eject",
            "recovery-wait",
            "power-enter",
        ]
    main_rig.eject_disk.assert_called_once_with(main_rig.disk)
    main_rig.write_inventory.assert_called_once()
    assert main_rig.write_inventory.call_args.args[1] == ["pi-4.local"]


def test_main_retries_same_card_after_sleep_during_power_guard_entry(
    main_rig, monkeypatch, capsys
):
    """Retry the same card when the power guard detects sleep during startup.

    The interrupted attempt must not start an image write; writing begins only
    after the next attempt enters the guard successfully.
    """

    sleep_error = burn.SystemSleepError("forced sleep immediately after READY")
    guard_entry_count = 0

    @contextlib.contextmanager
    def power_guard(session):
        """Fail the first guard entry, then provide a normal committed attempt."""
        nonlocal guard_entry_count
        assert session is main_rig.sudo_session
        guard_entry_count += 1
        main_rig.events.append("power-enter")
        if guard_entry_count == 1:
            main_rig.events.append("power-exit")
            raise sleep_error
        guard = FakeSleepGuard(main_rig.events)
        try:
            yield guard
        finally:
            main_rig.events.append("power-exit")

    monkeypatch.setattr(burn, "prevent_system_sleep", power_guard)

    assert main_rig.run_main(check=False, inventory=True) == 0

    main_rig.write_image.assert_called_once_with(
        main_rig.disk, main_rig.image, main_rig.sudo_session
    )
    main_rig.quietly_eject_same_disk.assert_called_once_with(main_rig.disk)
    main_rig.wait_for_same_disk.assert_called_once_with(
        main_rig.disk,
        heartbeat=main_rig.sudo_session.keep_alive,
    )
    assert main_rig.events.count("power-enter") == 2
    assert main_rig.events.count("power-commit") == 1
    attempt_headers = [
        line for line in capsys.readouterr().out.splitlines() if line.startswith("Preparation attempt ")
    ]
    assert attempt_headers == [
        "Preparation attempt 1 for pi-4.local on /dev/disk4",
        "Preparation attempt 2 for pi-4.local on /dev/disk4",
    ]


def test_main_recovers_when_sleep_recovery_eject_fails(main_rig, capsys):
    """After a best-effort recovery eject fails, still wait for and rewrite the same card."""

    sleep_error = burn.SystemSleepError("forced sleep")
    recovery_eject_error = burn.BurnError("reader remained busy")
    main_rig.write_image.side_effect = [sleep_error, ("b" * 64, 1024)]

    def fail_recovery_eject(*_args):
        """Expose the attempted recovery eject before reporting its failure."""
        main_rig.events.append("quiet-eject")
        raise recovery_eject_error

    main_rig.quietly_eject_same_disk.side_effect = fail_recovery_eject

    assert main_rig.run_main(check=False, inventory=True) == 0

    assert "sleep-recovery ejection failed: reader remained busy" in capsys.readouterr().err
    assert main_rig.events.count("quiet-eject") == 1
    assert main_rig.write_image.call_args_list == [
        mock.call(main_rig.disk, main_rig.image, main_rig.sudo_session),
        mock.call(main_rig.disk, main_rig.image, main_rig.sudo_session),
    ]
    main_rig.wait_for_same_disk.assert_called_once_with(
        main_rig.disk,
        heartbeat=main_rig.sudo_session.keep_alive,
    )
    main_rig.eject_disk.assert_called_once_with(main_rig.disk)
    main_rig.write_inventory.assert_called_once()
    assert main_rig.events.count("power-commit") == 1


@pytest.mark.parametrize("error_source", ["write", "guard-entry"])
def test_main_does_not_retry_non_sleep_write_or_guard_startup_errors(
    main_rig, monkeypatch, error_source
):
    """Do not start sleep retry for ordinary write or guard-startup errors.

    Emergency-eject cleanup still runs, but neither error waits for the card
    or begins another preparation attempt.
    """

    ordinary_error = burn.BurnError("power helper stopped unexpectedly")
    if error_source == "write":
        main_rig.write_image.side_effect = ordinary_error
    else:
        class FailingPowerGuard:
            """Fail while entering the real power-guard lifecycle boundary."""

            def __enter__(self):
                """Report the guard-entry failure before any card operation starts."""
                main_rig.events.append("power-enter")
                raise ordinary_error

            def __exit__(self, *_args):
                """Reject an impossible exit after the failed entry."""
                raise AssertionError("__exit__ cannot run after a failed __enter__")

        def failing_power_guard(_session):
            """Return a context manager that fails during helper startup."""
            return FailingPowerGuard()

        monkeypatch.setattr(burn, "prevent_system_sleep", failing_power_guard)

    with pytest.raises(burn.BurnError) as raised:
        main_rig.run_main(check=False)

    assert raised.value is ordinary_error
    if error_source == "write":
        main_rig.write_image.assert_called_once_with(
            main_rig.disk, main_rig.image, main_rig.sudo_session
        )
    else:
        main_rig.write_image.assert_not_called()
    main_rig.wait_for_same_disk.assert_not_called()
    main_rig.quietly_eject_same_disk.assert_called_once_with(main_rig.disk)
    assert main_rig.events.count("power-enter") == 1


@pytest.mark.parametrize("interrupted_stage", ["write", "read"])
def test_main_restarts_interrupted_integrity_test_with_new_seed(
    main_rig, monkeypatch, interrupted_stage, capsys
):
    """After sleep interrupts either integrity stage, restart at integrity write with a fresh seed.

    Complete the restarted integrity write and read before starting the image write.
    """
    seeds = [b"a" * 32, b"b" * 32]
    sleep_error = burn.SystemSleepError("sleep in integrity {}".format(interrupted_stage))
    write_integrity = mock.Mock()
    verify_integrity = mock.Mock()

    def write_integrity_pattern(_disk, _sudo_session, seed):
        """Record the seed and interrupt the first integrity write when requested."""
        main_rig.events.append(("integrity-write", seed))
        if interrupted_stage == "write" and write_integrity.call_count == 1:
            raise sleep_error

    def verify_integrity_pattern(_disk, _sudo_session, seed):
        """Record the seed and interrupt the first integrity read when requested."""
        main_rig.events.append(("integrity-read", seed))
        if interrupted_stage == "read" and verify_integrity.call_count == 1:
            raise sleep_error

    write_integrity.side_effect = write_integrity_pattern
    verify_integrity.side_effect = verify_integrity_pattern
    monkeypatch.setattr(burn.secrets, "token_bytes", mock.Mock(side_effect=seeds))
    monkeypatch.setattr(burn, "write_integrity_pattern", write_integrity)
    monkeypatch.setattr(burn, "verify_integrity_pattern", verify_integrity)
    main_rig.check_media.side_effect = REAL_CHECK_MEDIA

    assert main_rig.run_main(check=True) == 0

    assert write_integrity.call_args_list == [
        mock.call(main_rig.disk, main_rig.sudo_session, seeds[0]),
        mock.call(main_rig.disk, main_rig.sudo_session, seeds[1]),
    ]
    expected_integrity_read_calls = [
        mock.call(main_rig.disk, main_rig.sudo_session, seeds[1])
    ]
    if interrupted_stage == "read":
        expected_integrity_read_calls.insert(
            0, mock.call(main_rig.disk, main_rig.sudo_session, seeds[0])
        )
    assert verify_integrity.call_args_list == expected_integrity_read_calls
    main_rig.write_image.assert_called_once_with(main_rig.disk, main_rig.image, main_rig.sudo_session)
    integrity_and_image_events = [
        event
        for event in main_rig.events
        if event == "write"
        or (isinstance(event, tuple) and event[0] in {"integrity-write", "integrity-read"})
    ]
    expected_first_attempt_events = [("integrity-write", seeds[0])]
    if interrupted_stage == "read":
        expected_first_attempt_events.append(("integrity-read", seeds[0]))
    assert integrity_and_image_events == [
        *expected_first_attempt_events,
        ("integrity-write", seeds[1]),
        ("integrity-read", seeds[1]),
        "write",
    ]
    main_rig.quietly_eject_same_disk.assert_called_once_with(main_rig.disk)
    main_rig.wait_for_same_disk.assert_called_once_with(
        main_rig.disk,
        heartbeat=main_rig.sudo_session.keep_alive,
    )
    assert main_rig.events.count("power-enter") == 2
    assert main_rig.events.count("power-exit") == 2
    assert [
        line for line in capsys.readouterr().out.splitlines() if line.startswith("Preparation attempt ")
    ] == [
        "Preparation attempt 1 for pi-4.local on /dev/disk4",
        "Preparation attempt 2 for pi-4.local on /dev/disk4",
    ]


@pytest.mark.parametrize("resolution", ["passed", "noninteractive-skip", "interactive-skip"])
def test_main_does_not_repeat_resolved_integrity_test_after_sleep(main_rig, resolution):
    """Retain a successful integrity result or a decision to continue after failure.

    Cover interactive and non-interactive decisions across a later sleep retry.
    """
    if resolution != "passed":
        main_rig.check_media.return_value = False
        main_rig.check_media.side_effect = None
    main_rig.write_image.side_effect = [burn.SystemSleepError("sleep after check"), ("b" * 64, 1024)]

    assert main_rig.run_main(
        check=True,
        on_check_failure="skip",
        non_interactive=resolution != "interactive-skip",
    ) == 0

    main_rig.check_media.assert_called_once_with(main_rig.disk, main_rig.sudo_session)
    if resolution == "interactive-skip":
        main_rig.failed_check_action.assert_called_once_with(heartbeat=main_rig.sudo_session.keep_alive)
    else:
        main_rig.failed_check_action.assert_not_called()
    expected_write_call = mock.call(main_rig.disk, main_rig.image, main_rig.sudo_session)
    assert main_rig.write_image.call_args_list == [expected_write_call, expected_write_call]
    assert main_rig.verify_written_image.call_count == 1
    main_rig.wait_for_disk.assert_called_once()
    main_rig.wait_for_same_disk.assert_called_once_with(
        main_rig.disk,
        heartbeat=main_rig.sudo_session.keep_alive,
    )
    main_rig.write_cloud_init.assert_called_once()
    assert main_rig.write_cloud_init.call_args.args[:2] == (main_rig.disk, "pi-4")


def test_main_rechecks_integrity_after_sleep_interrupts_interactive_skip_decision(
    main_rig, capsys
):
    """Sleep during the post-failure action prompt leaves integrity unresolved.

    The next attempt reruns the check and prompt; image writing begins only
    after the user then chooses to skip.
    """

    sleep_error = burn.SystemSleepError("sleep while asking whether to skip")
    main_rig.check_media.side_effect = None
    main_rig.check_media.return_value = False
    main_rig.failed_check_action.side_effect = [sleep_error, True]

    assert main_rig.run_main(check=True, non_interactive=False) == 0

    assert main_rig.check_media.call_args_list == [
        mock.call(main_rig.disk, main_rig.sudo_session),
        mock.call(main_rig.disk, main_rig.sudo_session),
    ]
    assert main_rig.failed_check_action.call_args_list == [
        mock.call(heartbeat=main_rig.sudo_session.keep_alive),
        mock.call(heartbeat=main_rig.sudo_session.keep_alive),
    ]
    main_rig.write_image.assert_called_once_with(
        main_rig.disk, main_rig.image, main_rig.sudo_session
    )
    main_rig.verify_written_image.assert_called_once()
    main_rig.quietly_eject_same_disk.assert_called_once_with(main_rig.disk)
    main_rig.wait_for_same_disk.assert_called_once_with(
        main_rig.disk,
        heartbeat=main_rig.sudo_session.keep_alive,
    )
    assert main_rig.events.count("power-enter") == 2
    assert main_rig.events.count("power-exit") == 2
    assert [
        line for line in capsys.readouterr().out.splitlines() if line.startswith("Preparation attempt ")
    ] == [
        "Preparation attempt 1 for pi-4.local on /dev/disk4",
        "Preparation attempt 2 for pi-4.local on /dev/disk4",
    ]


@pytest.mark.parametrize("stage", ["write", "verify", "cloud-init"])
def test_main_restarts_image_from_zero_after_sleep_in_any_late_stage(main_rig, stage):
    """Sleep during image write, verification, or cloud-init forces a fresh write at byte zero."""
    sleep_error = burn.SystemSleepError("sleep during {}".format(stage))

    def write_image(*_args):
        """Record the write and interrupt its first invocation when requested."""
        main_rig.events.append("write")
        if stage == "write" and main_rig.write_image.call_count == 1:
            raise sleep_error
        return "b" * 64, 1024

    def verify_written_image(*_args):
        """Record verification and interrupt its first invocation when requested."""
        main_rig.events.append("verify")
        if stage == "verify" and main_rig.verify_written_image.call_count == 1:
            raise sleep_error

    def write_cloud_init(*_args, **_kwargs):
        """Record customization and interrupt its first invocation when requested."""
        main_rig.events.append("cloud-init")
        if stage == "cloud-init" and main_rig.write_cloud_init.call_count == 1:
            raise sleep_error

    main_rig.write_image.side_effect = write_image
    main_rig.verify_written_image.side_effect = verify_written_image
    main_rig.write_cloud_init.side_effect = write_cloud_init

    assert main_rig.run_main(check=True) == 0

    assert main_rig.write_image.call_args_list == [
        mock.call(main_rig.disk, main_rig.image, main_rig.sudo_session),
        mock.call(main_rig.disk, main_rig.image, main_rig.sudo_session),
    ]
    assert main_rig.check_media.call_count == 1
    assert main_rig.verify_written_image.call_count == (1 if stage == "write" else 2)
    assert main_rig.write_cloud_init.call_count == (2 if stage == "cloud-init" else 1)
    main_rig.eject_disk.assert_called_once_with(main_rig.disk)
    expected_first_attempt_events = [
        "power-enter",
        "integrity",
        ("mount-enter", "disk4"),
        "write",
    ]
    if stage != "write":
        expected_first_attempt_events.append("verify")
    expected_first_attempt_events.append(("mount-exit", "disk4"))
    if stage == "cloud-init":
        expected_first_attempt_events.append("cloud-init")
    expected_first_attempt_events.extend(["power-exit", "quiet-eject", "recovery-wait"])
    expected_successful_attempt_events = [
        "power-enter",
        ("mount-enter", "disk4"),
        "write",
        "verify",
        ("mount-exit", "disk4"),
        "cloud-init",
        "heartbeat",
        "fingerprint",
        "eject",
        "power-commit",
        "power-exit",
    ]
    assert (
        main_rig.events
        == ["download", "selection"]
        + expected_first_attempt_events
        + expected_successful_attempt_events
    )
    main_rig.quietly_eject_same_disk.assert_called_once_with(main_rig.disk)
    main_rig.wait_for_same_disk.assert_called_once_with(
        main_rig.disk,
        heartbeat=main_rig.sudo_session.keep_alive,
    )


@pytest.mark.parametrize("sleep_boundary", ["final-heartbeat", "commit"])
def test_main_rewrites_card_after_sleep_during_final_heartbeat_or_commit(
    main_rig, monkeypatch, sleep_boundary
):
    """Sleep at the pre-eject heartbeat or during commit forces a rewrite from byte zero."""

    sleep_error = burn.SystemSleepError("forced sleep at {}".format(sleep_boundary))
    attempt_guards = []

    class BoundaryGuard:
        """Expose commit calls and interrupt the first one when requested."""

        def __init__(self, attempt_number):
            self.attempt_number = attempt_number

        def commit(self):
            """Commit one attempt unless this is the selected sleep boundary."""
            main_rig.events.append("commit-call")
            if sleep_boundary == "commit" and self.attempt_number == 1:
                raise sleep_error
            main_rig.events.append("power-commit")

    @contextlib.contextmanager
    def power_guard(session):
        """Record a realistic guard lifecycle for each card attempt."""
        assert session is main_rig.sudo_session
        attempt_guard = BoundaryGuard(len(attempt_guards) + 1)
        attempt_guards.append(attempt_guard)
        main_rig.events.append("power-enter")
        try:
            yield attempt_guard
        finally:
            main_rig.events.append("power-exit")

    monkeypatch.setattr(burn, "prevent_system_sleep", power_guard)
    if sleep_boundary == "final-heartbeat":

        def heartbeat():
            main_rig.events.append("heartbeat")
            if main_rig.sudo_session.keep_alive.call_count == 1:
                raise sleep_error

        main_rig.sudo_session.keep_alive.side_effect = heartbeat

    assert main_rig.run_main(check=False, inventory=True) == 0

    assert main_rig.write_image.call_args_list == [
        mock.call(main_rig.disk, main_rig.image, main_rig.sudo_session),
        mock.call(main_rig.disk, main_rig.image, main_rig.sudo_session),
    ]
    main_rig.quietly_eject_same_disk.assert_called_once_with(main_rig.disk)
    main_rig.wait_for_same_disk.assert_called_once_with(
        main_rig.disk,
        heartbeat=main_rig.sudo_session.keep_alive,
    )
    assert main_rig.eject_disk.call_count == (1 if sleep_boundary == "final-heartbeat" else 2)
    assert main_rig.events.count("commit-call") == (1 if sleep_boundary == "final-heartbeat" else 2)
    assert main_rig.events.count("power-commit") == 1
    main_rig.write_inventory.assert_called_once()


def test_main_waits_for_same_fingerprint_before_sleep_retry(main_rig, monkeypatch, capsys):
    """Heartbeat before each recovery poll and wait one second after each miss.

    Start the second write only when a card with the original fingerprint
    returns.
    """
    sleep_error = burn.SystemSleepError("sleep")

    def write_image(*_args):
        """Record both raw-write attempts and interrupt only the first."""
        main_rig.events.append("write")
        if main_rig.write_image.call_count == 1:
            raise sleep_error
        return "b" * 64, 1024

    def wait_for_same_disk(*args, **kwargs):
        """Expose the recovery boundary before running the production polling loop."""
        main_rig.events.append("recovery-wait")
        return REAL_WAIT_FOR_SAME_DISK(*args, **kwargs)

    main_rig.write_image.side_effect = write_image
    main_rig.wait_for_same_disk.side_effect = wait_for_same_disk
    heartbeat_counts_at_polls = []

    def poll_disk(_device):
        """Return the selected card only after two absent-device polls."""
        heartbeat_counts_at_polls.append(main_rig.sudo_session.keep_alive.call_count)
        if len(heartbeat_counts_at_polls) < 3:
            raise burn.BurnError("not present")
        return main_rig.disk

    sleep_between_polls = mock.Mock()
    monkeypatch.setattr(burn, "get_disk", poll_disk)
    monkeypatch.setattr(burn.os.path, "exists", mock.Mock(return_value=False))
    monkeypatch.setattr(burn.time, "sleep", sleep_between_polls)

    assert main_rig.run_main(check=False) == 0

    assert heartbeat_counts_at_polls == [1, 2, 3]
    assert sleep_between_polls.call_args_list == [mock.call(1.0), mock.call(1.0)]
    assert main_rig.write_image.call_count == 2
    write_event_positions = [
        index for index, event in enumerate(main_rig.events) if event == "write"
    ]
    recovery_wait_position = main_rig.events.index("recovery-wait")
    retry_guard_entry_position = main_rig.events.index(
        "power-enter", recovery_wait_position
    )
    assert main_rig.events[recovery_wait_position - 2 : recovery_wait_position + 1] == [
        "power-exit",
        "quiet-eject",
        "recovery-wait",
    ]
    assert write_event_positions[0] < recovery_wait_position
    assert (
        main_rig.events[recovery_wait_position:retry_guard_entry_position].count("heartbeat")
        == 3
    )
    assert retry_guard_entry_position < write_event_positions[1]
    main_rig.quietly_eject_same_disk.assert_called_once_with(main_rig.disk)
    assert "Physically remove and reinsert it if necessary" in capsys.readouterr().out
    main_rig.wait_for_same_disk.assert_called_once_with(
        main_rig.disk,
        heartbeat=main_rig.sudo_session.keep_alive,
    )


def test_wait_for_same_disk_rejects_existing_unsuitable_device_without_retrying(
    main_rig, monkeypatch
):
    """Treat an existing target that fails validation as a hard error, not an absent card."""
    media_error = burn.BurnError("disk4 is unsuitable")
    get_disk = mock.Mock(side_effect=media_error)
    sleep_between_polls = mock.Mock()
    monkeypatch.setattr(burn, "get_disk", get_disk)
    monkeypatch.setattr(burn.os.path, "exists", mock.Mock(return_value=True))
    monkeypatch.setattr(burn.time, "sleep", sleep_between_polls)

    with pytest.raises(burn.BurnError) as raised:
        REAL_WAIT_FOR_SAME_DISK(main_rig.disk, heartbeat=main_rig.sudo_session.keep_alive)

    assert raised.value is media_error
    get_disk.assert_called_once_with(main_rig.disk.device)
    main_rig.sudo_session.keep_alive.assert_called_once_with()
    sleep_between_polls.assert_not_called()


@pytest.mark.parametrize(
    ("fingerprint_field", "replacement_value"),
    [
        ("identifier", "disk5"),
        ("size", 16 * 1024**3),
        ("name", "Other Card"),
        ("protocol", "Thunderbolt"),
        ("device_tree_path", "IODeviceTree:/other-reader/card"),
        ("media_uuid", "OTHER-UUID"),
    ],
)
def test_main_aborts_when_different_media_appears_during_sleep_retry(
    main_rig, monkeypatch, fingerprint_field, replacement_value
):
    """A changed fingerprint aborts recovery without unmounting or ejecting the replacement."""
    main_rig.write_image.side_effect = burn.SystemSleepError("sleep")
    main_rig.wait_for_same_disk.side_effect = REAL_WAIT_FOR_SAME_DISK
    replacement_disk = dataclasses.replace(
        main_rig.disk, **{fingerprint_field: replacement_value}
    )
    get_disk = mock.Mock(return_value=replacement_disk)
    unmount_disk = mock.Mock()
    monkeypatch.setattr(burn, "get_disk", get_disk)
    monkeypatch.setattr(burn, "ensure_same_disk", REAL_ENSURE_SAME_DISK)
    monkeypatch.setattr(burn, "quietly_eject_same_disk", REAL_QUIETLY_EJECT_SAME_DISK)
    monkeypatch.setattr(burn, "unmount_disk", unmount_disk)

    with pytest.raises(burn.BurnError, match="Different media appeared"):
        main_rig.run_main(check=False)

    main_rig.write_image.assert_called_once_with(main_rig.disk, main_rig.image, main_rig.sudo_session)
    assert get_disk.call_args_list == [mock.call(main_rig.disk.device), mock.call(main_rig.disk.device)]
    assert main_rig.events.count("power-enter") == 1
    unmount_disk.assert_not_called()
    main_rig.eject_disk.assert_not_called()


def test_main_sleep_retry_reuses_sudo_without_advancing_card_or_hostname(main_rig):
    """Retries reuse sudo without consuming hostnames or duplicating inventory."""
    second_disk = dataclasses.replace(
        main_rig.disk,
        name="Second Card",
        device_tree_path="IODeviceTree:/reader/second-card",
        media_uuid="SECOND-UUID",
    )
    main_rig.wait_for_disk.side_effect = [main_rig.disk, second_disk]
    main_rig.wait_for_same_disk.return_value = main_rig.disk
    main_rig.write_image.side_effect = [
        burn.SystemSleepError("sleep"),
        ("b" * 64, 1024),
        ("c" * 64, 1024),
    ]

    assert main_rig.run_main(check=False, count=2, inventory=True) == 0

    assert [
        cloud_init_call.args[1]
        for cloud_init_call in main_rig.write_cloud_init.call_args_list
    ] == ["pi-4", "pi-5"]
    assert [
        cloud_init_call.args[0]
        for cloud_init_call in main_rig.write_cloud_init.call_args_list
    ] == [main_rig.disk, second_disk]
    main_rig.write_inventory.assert_called_once()
    assert main_rig.write_inventory.call_args.args[1] == ["pi-4.local", "pi-5.local"]
    main_rig.ensure_sudo.assert_called_once_with()
    assert all(
        write_call.args[2] is main_rig.sudo_session
        for write_call in main_rig.write_image.call_args_list
    )
    assert main_rig.events.count("power-enter") == 3


def test_main_final_fingerprint_failure_prevents_regular_eject_and_commit(main_rig):
    """A failed final fingerprint check prevents regular eject and card commit."""
    fingerprint_error = burn.BurnError("media changed before eject")

    def reject_fingerprint(*_args):
        """Record the final identity check before reporting changed media."""
        main_rig.events.append("fingerprint")
        raise fingerprint_error

    main_rig.ensure_same_disk.side_effect = reject_fingerprint

    with pytest.raises(burn.BurnError) as raised:
        main_rig.run_main(check=False)

    assert raised.value is fingerprint_error
    assert main_rig.events == [
        "download",
        "selection",
        "power-enter",
        ("mount-enter", "disk4"),
        "write",
        ("mount-exit", "disk4"),
        "cloud-init",
        "heartbeat",
        "fingerprint",
        "power-exit",
        "quiet-eject",
    ]
    main_rig.eject_disk.assert_not_called()
    assert "power-commit" not in main_rig.events


def test_main_commits_card_only_after_successful_eject(main_rig, capsys):
    """Commit the matching card once, after eject and before the power guard exits."""
    assert main_rig.run_main(check=False) == 0

    eject_position = main_rig.events.index("eject")
    assert main_rig.events[eject_position - 2 : eject_position + 3] == [
        "heartbeat",
        "fingerprint",
        "eject",
        "power-commit",
        "power-exit",
    ]
    assert main_rig.events.count("power-commit") == 1
    assert capsys.readouterr().out.count("Done: /dev/disk4 was ejected") == 1
    main_rig.eject_disk.assert_called_once_with(main_rig.disk)
    main_rig.write_image.assert_called_once_with(main_rig.disk, main_rig.image, main_rig.sudo_session)
    main_rig.wait_for_same_disk.assert_not_called()


@pytest.mark.parametrize("matching", [True, False], ids=["matching", "missing-or-changed"])
def test_main_quietly_ejects_only_still_matching_current_card(main_rig, monkeypatch, matching):
    """Emergency cleanup ejects the current card only when its fingerprint check succeeds."""
    write_error = burn.BurnError("ordinary write failure")
    main_rig.write_image.side_effect = write_error
    monkeypatch.setattr(burn, "quietly_eject_same_disk", REAL_QUIETLY_EJECT_SAME_DISK)
    if matching:
        main_rig.ensure_same_disk.return_value = main_rig.disk
    else:
        main_rig.ensure_same_disk.side_effect = burn.BurnError("missing or changed")

    with pytest.raises(burn.BurnError) as raised:
        main_rig.run_main(check=False)

    assert raised.value is write_error
    if matching:
        main_rig.eject_disk.assert_called_once_with(main_rig.disk, quiet=True)
    else:
        main_rig.eject_disk.assert_not_called()
