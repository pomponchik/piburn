import ctypes
import signal
from unittest import mock

import pytest

from piburn import _mount_guard as mount_guard


def test_configure_functions_declares_disk_arbitration_and_core_foundation_abi():
    """Declare the exact ctypes ABI for every Disk Arbitration and CoreFoundation function used."""

    disk_arbitration = mock.Mock()
    core_foundation = mock.Mock()

    callback_type = mount_guard.configure_functions(disk_arbitration, core_foundation)

    assert callback_type._argtypes_ == (ctypes.c_void_p, ctypes.c_void_p)
    assert callback_type._restype_ is ctypes.c_void_p
    assert disk_arbitration.DASessionCreate.argtypes == [ctypes.c_void_p]
    assert disk_arbitration.DASessionCreate.restype is ctypes.c_void_p
    assert disk_arbitration.DASessionScheduleWithRunLoop.argtypes == [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    assert disk_arbitration.DASessionScheduleWithRunLoop.restype is None
    assert disk_arbitration.DASessionUnscheduleFromRunLoop.argtypes == [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    assert disk_arbitration.DASessionUnscheduleFromRunLoop.restype is None
    assert disk_arbitration.DARegisterDiskMountApprovalCallback.argtypes == [
        ctypes.c_void_p,
        ctypes.c_void_p,
        callback_type,
        ctypes.c_void_p,
    ]
    assert disk_arbitration.DARegisterDiskMountApprovalCallback.restype is None
    assert disk_arbitration.DAUnregisterApprovalCallback.argtypes == [
        ctypes.c_void_p,
        callback_type,
        ctypes.c_void_p,
    ]
    assert disk_arbitration.DAUnregisterApprovalCallback.restype is None
    assert disk_arbitration.DADiskCopyWholeDisk.argtypes == [ctypes.c_void_p]
    assert disk_arbitration.DADiskCopyWholeDisk.restype is ctypes.c_void_p
    assert disk_arbitration.DADiskGetBSDName.argtypes == [ctypes.c_void_p]
    assert disk_arbitration.DADiskGetBSDName.restype is ctypes.c_char_p
    assert disk_arbitration.DADissenterCreate.argtypes == [
        ctypes.c_void_p,
        ctypes.c_int32,
        ctypes.c_void_p,
    ]
    assert disk_arbitration.DADissenterCreate.restype is ctypes.c_void_p
    assert core_foundation.CFRunLoopGetCurrent.argtypes == []
    assert core_foundation.CFRunLoopGetCurrent.restype is ctypes.c_void_p
    assert core_foundation.CFRunLoopRunInMode.argtypes == [ctypes.c_void_p, ctypes.c_double, ctypes.c_bool]
    assert core_foundation.CFRunLoopRunInMode.restype is ctypes.c_int32
    assert core_foundation.CFRunLoopStop.argtypes == [ctypes.c_void_p]
    assert core_foundation.CFRunLoopStop.restype is None
    assert core_foundation.CFRelease.argtypes == [ctypes.c_void_p]
    assert core_foundation.CFRelease.restype is None


def test_mount_approval_callback_denies_only_the_selected_whole_disk():
    """Deny the selected disk and its partitions while allowing every other disk."""

    disk_arbitration = mock.Mock()
    disk_arbitration.DADiskCopyWholeDisk.side_effect = lambda disk: {71: 7, 72: 7, 41: 4}.get(disk)
    disk_arbitration.DADiskGetBSDName.side_effect = lambda disk: {7: b"disk7", 4: b"disk4"}[disk]
    disk_arbitration.DADissenterCreate.return_value = 1234
    core_foundation = mock.Mock()
    callback_type = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
    callback = mount_guard.create_mount_approval_callback(
        "disk7",
        disk_arbitration,
        core_foundation,
        callback_type,
    )

    assert callback(71, None) == 1234
    assert callback(72, None) == 1234
    assert callback(7, None) == 1234
    assert callback(41, None) is None
    assert disk_arbitration.DADissenterCreate.call_args_list == [
        mock.call(None, mount_guard.K_DA_RETURN_EXCLUSIVE_ACCESS, None),
        mock.call(None, mount_guard.K_DA_RETURN_EXCLUSIVE_ACCESS, None),
        mock.call(None, mount_guard.K_DA_RETURN_EXCLUSIVE_ACCESS, None),
    ]
    assert core_foundation.CFRelease.call_args_list == [mock.call(7), mock.call(7), mock.call(4)]


def test_run_guard_announces_ready_after_registration_and_cleans_up_on_signal(capsys):
    """Print a flushed READY after registering the callback and scheduling the session.

    On SIGTERM, unregister the callback, unschedule and release the session,
    and restore the previous signal handlers.
    """

    events = []
    disk_arbitration = mock.Mock()
    disk_arbitration.DASessionCreate.return_value = 11
    disk_arbitration.DARegisterDiskMountApprovalCallback.side_effect = lambda *_args: events.append(
        "register"
    )
    disk_arbitration.DASessionScheduleWithRunLoop.side_effect = lambda *_args: events.append(
        "schedule"
    )
    disk_arbitration.DAUnregisterApprovalCallback.side_effect = lambda *_args: events.append(
        "unregister"
    )
    disk_arbitration.DASessionUnscheduleFromRunLoop.side_effect = lambda *_args: events.append(
        "unschedule"
    )
    core_foundation = mock.Mock()
    core_foundation.CFRunLoopGetCurrent.return_value = 22
    core_foundation.CFRelease.side_effect = lambda *_args: events.append("release")
    ctypes_api = mock.Mock()
    ctypes_api.CDLL.side_effect = [disk_arbitration, core_foundation]
    ctypes_api.c_void_p.in_dll.return_value.value = 33
    signal_handlers = {}
    original_handlers = {signal.SIGTERM: object(), signal.SIGINT: object()}

    def install_signal_handler(signal_number, handler):
        previous_handler = signal_handlers.get(
            signal_number, original_handlers[signal_number]
        )
        signal_handlers[signal_number] = handler
        return previous_handler

    signal_api = mock.Mock(SIGTERM=signal.SIGTERM, SIGINT=signal.SIGINT)
    signal_api.signal.side_effect = install_signal_handler

    def stop_after_first_iteration(*_args):
        events.append("run-loop")
        signal_handlers[signal.SIGTERM](signal.SIGTERM, None)

    core_foundation.CFRunLoopRunInMode.side_effect = stop_after_first_iteration
    approval_callback = object()
    with mock.patch.object(mount_guard, "ctypes", ctypes_api), mock.patch.object(
        mount_guard, "signal", signal_api
    ), mock.patch.object(mount_guard, "configure_functions", return_value="callback-type"), mock.patch.object(
        mount_guard, "create_mount_approval_callback", return_value=approval_callback
    ) as create_callback, mock.patch(
        "builtins.print", side_effect=lambda *_args, **_kwargs: events.append("ready")
    ) as print_ready:
        assert mount_guard.run_guard("disk7") == 0

    assert capsys.readouterr().out == ""
    assert ctypes_api.CDLL.call_args_list == [
        mock.call(mount_guard.DISK_ARBITRATION_FRAMEWORK),
        mock.call(mount_guard.CORE_FOUNDATION_FRAMEWORK),
    ]
    ctypes_api.c_void_p.in_dll.assert_called_once_with(
        core_foundation, "kCFRunLoopDefaultMode"
    )
    print_ready.assert_called_once_with("READY", flush=True)
    assert events == [
        "register",
        "schedule",
        "ready",
        "run-loop",
        "unregister",
        "unschedule",
        "release",
    ]
    create_callback.assert_called_once_with("disk7", disk_arbitration, core_foundation, "callback-type")
    disk_arbitration.DARegisterDiskMountApprovalCallback.assert_called_once_with(
        11, None, approval_callback, None
    )
    disk_arbitration.DASessionScheduleWithRunLoop.assert_called_once_with(11, 22, 33)
    core_foundation.CFRunLoopRunInMode.assert_called_once_with(33, 0.25, False)
    core_foundation.CFRunLoopStop.assert_called_once_with(22)
    disk_arbitration.DAUnregisterApprovalCallback.assert_called_once_with(11, approval_callback, None)
    disk_arbitration.DASessionUnscheduleFromRunLoop.assert_called_once_with(11, 22, 33)
    core_foundation.CFRelease.assert_called_once_with(11)
    assert signal_handlers == original_handlers


def test_run_guard_reports_platform_framework_and_session_failures(monkeypatch):
    """Reject unsupported systems and turn framework or session failures into clear errors."""

    monkeypatch.setattr(mount_guard.sys, "platform", "linux")
    with pytest.raises(RuntimeError, match="requires macOS"):
        mount_guard.run_guard("disk7")

    monkeypatch.setattr(mount_guard.sys, "platform", "darwin")
    with mock.patch.object(mount_guard.ctypes, "CDLL", side_effect=OSError("framework missing")), pytest.raises(
        RuntimeError, match="could not load Disk Arbitration: framework missing"
    ):
        mount_guard.run_guard("disk7")

    disk_arbitration = mock.Mock()
    disk_arbitration.DASessionCreate.return_value = None
    with mock.patch.object(
        mount_guard.ctypes, "CDLL", side_effect=[disk_arbitration, mock.Mock()]
    ), mock.patch.object(mount_guard, "configure_functions"), pytest.raises(
        RuntimeError, match="could not create a Disk Arbitration session"
    ):
        mount_guard.run_guard("disk7")


@pytest.mark.parametrize("arguments", [[], ["disk"], ["rdisk7"], ["disk7", "disk8"]])
def test_private_helper_main_rejects_invalid_identifiers(arguments, capsys):
    """Reject malformed private helper arguments without starting Disk Arbitration."""

    with mock.patch.object(mount_guard, "run_guard") as run_guard:
        assert mount_guard.main(arguments) == 2

    run_guard.assert_not_called()
    assert capsys.readouterr().err == "usage: python -m piburn._mount_guard diskN\n"


def test_private_helper_main_reports_runtime_failure(capsys):
    """Report helper startup errors on stderr and return a failing exit status."""

    with mock.patch.object(mount_guard, "run_guard", side_effect=RuntimeError("session failed")):
        assert mount_guard.main(["disk7"]) == 1

    assert capsys.readouterr().err == "automatic-mount guard failed: session failed\n"
