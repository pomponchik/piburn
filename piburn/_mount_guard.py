"""Run a macOS Disk Arbitration mount-approval guard for one whole disk."""

from __future__ import annotations

import ctypes
import signal
import sys
from typing import Any, Optional, Sequence, cast

DISK_ARBITRATION_FRAMEWORK = "/System/Library/Frameworks/DiskArbitration.framework/DiskArbitration"
CORE_FOUNDATION_FRAMEWORK = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
K_DA_RETURN_EXCLUSIVE_ACCESS = ctypes.c_int32(0xF8DA0004).value


def configure_functions(disk_arbitration: Any, core_foundation: Any) -> Any:
    """Configure the small subset of CoreFoundation and Disk Arbitration used here."""
    callback_type = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)

    disk_arbitration.DASessionCreate.argtypes = [ctypes.c_void_p]
    disk_arbitration.DASessionCreate.restype = ctypes.c_void_p
    disk_arbitration.DASessionScheduleWithRunLoop.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    disk_arbitration.DASessionUnscheduleFromRunLoop.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    disk_arbitration.DARegisterDiskMountApprovalCallback.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        callback_type,
        ctypes.c_void_p,
    ]
    disk_arbitration.DAUnregisterApprovalCallback.argtypes = [
        ctypes.c_void_p,
        callback_type,
        ctypes.c_void_p,
    ]
    disk_arbitration.DADiskCopyWholeDisk.argtypes = [ctypes.c_void_p]
    disk_arbitration.DADiskCopyWholeDisk.restype = ctypes.c_void_p
    disk_arbitration.DADiskGetBSDName.argtypes = [ctypes.c_void_p]
    disk_arbitration.DADiskGetBSDName.restype = ctypes.c_char_p
    disk_arbitration.DADissenterCreate.argtypes = [ctypes.c_void_p, ctypes.c_int32, ctypes.c_void_p]
    disk_arbitration.DADissenterCreate.restype = ctypes.c_void_p

    core_foundation.CFRunLoopGetCurrent.argtypes = []
    core_foundation.CFRunLoopGetCurrent.restype = ctypes.c_void_p
    core_foundation.CFRunLoopRunInMode.argtypes = [ctypes.c_void_p, ctypes.c_double, ctypes.c_bool]
    core_foundation.CFRunLoopRunInMode.restype = ctypes.c_int32
    core_foundation.CFRunLoopStop.argtypes = [ctypes.c_void_p]
    core_foundation.CFRelease.argtypes = [ctypes.c_void_p]
    return callback_type


def create_mount_approval_callback(
    identifier: str,
    disk_arbitration: Any,
    core_foundation: Any,
    callback_type: Any,
) -> Any:
    """Create a callback that denies only mounts belonging to one whole disk."""
    target_name = identifier.encode("ascii")

    def approve_mount_impl(disk: int, _context: Optional[int]) -> Optional[int]:
        whole_disk = disk_arbitration.DADiskCopyWholeDisk(disk)
        inspected_disk = whole_disk or disk
        try:
            bsd_name = disk_arbitration.DADiskGetBSDName(inspected_disk)
            if bsd_name == target_name:
                return cast(
                    Optional[int],
                    disk_arbitration.DADissenterCreate(None, K_DA_RETURN_EXCLUSIVE_ACCESS, None),
                )
            return None
        finally:
            if whole_disk:
                core_foundation.CFRelease(whole_disk)

    return callback_type(approve_mount_impl)


def run_guard(identifier: str) -> int:
    """Deny mount requests for identifier and its partitions until terminated."""
    if sys.platform != "darwin":
        raise RuntimeError("the automatic-mount guard requires macOS")
    try:
        disk_arbitration = ctypes.CDLL(DISK_ARBITRATION_FRAMEWORK)
        core_foundation = ctypes.CDLL(CORE_FOUNDATION_FRAMEWORK)
    except OSError as exc:
        raise RuntimeError("could not load Disk Arbitration: {}".format(exc))

    callback_type = configure_functions(disk_arbitration, core_foundation)
    session = disk_arbitration.DASessionCreate(None)
    if not session:
        raise RuntimeError("could not create a Disk Arbitration session")

    run_loop = core_foundation.CFRunLoopGetCurrent()
    run_loop_mode = ctypes.c_void_p.in_dll(core_foundation, "kCFRunLoopDefaultMode").value
    approve_mount = create_mount_approval_callback(
        identifier,
        disk_arbitration,
        core_foundation,
        callback_type,
    )

    stopping = False

    def stop_guard(_signal_number: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True
        core_foundation.CFRunLoopStop(run_loop)

    previous_sigterm = signal.signal(signal.SIGTERM, stop_guard)
    previous_sigint = signal.signal(signal.SIGINT, stop_guard)
    try:
        disk_arbitration.DARegisterDiskMountApprovalCallback(session, None, approve_mount, None)
        disk_arbitration.DASessionScheduleWithRunLoop(session, run_loop, run_loop_mode)
        print("READY", flush=True)
        while not stopping:
            core_foundation.CFRunLoopRunInMode(run_loop_mode, 0.25, False)
    finally:
        disk_arbitration.DAUnregisterApprovalCallback(session, approve_mount, None)
        disk_arbitration.DASessionUnscheduleFromRunLoop(session, run_loop, run_loop_mode)
        core_foundation.CFRelease(session)
        signal.signal(signal.SIGTERM, previous_sigterm)
        signal.signal(signal.SIGINT, previous_sigint)
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 1 or not arguments[0].startswith("disk") or not arguments[0][4:].isdigit():
        print("usage: python -m piburn._mount_guard diskN", file=sys.stderr)
        return 2
    try:
        return run_guard(arguments[0])
    except Exception as exc:
        print("automatic-mount guard failed: {}".format(exc), file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
