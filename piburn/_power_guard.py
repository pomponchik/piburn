"""Hold macOS power assertions and report non-cancellable system sleep."""

from __future__ import annotations

import contextlib
import ctypes
import signal
import sys
from typing import Any, Callable, Dict, List, Optional, Sequence

IOKIT_FRAMEWORK = "/System/Library/Frameworks/IOKit.framework/IOKit"
CORE_FOUNDATION_FRAMEWORK = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"

K_IO_MESSAGE_CAN_SYSTEM_SLEEP = 0xE0000270
K_IO_MESSAGE_SYSTEM_WILL_SLEEP = 0xE0000280
K_IO_MESSAGE_SYSTEM_HAS_POWERED_ON = 0xE0000300
K_IO_MESSAGE_SYSTEM_WILL_POWER_ON = 0xE0000320
K_IO_PM_ASSERTION_LEVEL_ON = 255
K_CF_STRING_ENCODING_UTF8 = 0x08000100
SLEEP_EXIT_CODE = 75

ASSERTION_TYPES = (
    "PreventUserIdleSystemSleep",
    "PreventSystemSleep",
)
ASSERTION_REASON = "piburn is preparing a removable boot card"


def configure_functions(iokit: Any, core_foundation: Any) -> Any:
    """Declare the exact C ABI used by the helper and return its callback type."""
    callback_type = ctypes.CFUNCTYPE(
        None,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    )

    iokit.IORegisterForSystemPower.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        callback_type,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    iokit.IORegisterForSystemPower.restype = ctypes.c_uint32
    iokit.IODeregisterForSystemPower.argtypes = [ctypes.POINTER(ctypes.c_uint32)]
    iokit.IODeregisterForSystemPower.restype = ctypes.c_int32
    iokit.IOAllowPowerChange.argtypes = [ctypes.c_uint32, ctypes.c_ssize_t]
    iokit.IOAllowPowerChange.restype = ctypes.c_int32
    iokit.IOCancelPowerChange.argtypes = [ctypes.c_uint32, ctypes.c_ssize_t]
    iokit.IOCancelPowerChange.restype = ctypes.c_int32
    iokit.IONotificationPortGetRunLoopSource.argtypes = [ctypes.c_void_p]
    iokit.IONotificationPortGetRunLoopSource.restype = ctypes.c_void_p
    iokit.IONotificationPortDestroy.argtypes = [ctypes.c_void_p]
    iokit.IONotificationPortDestroy.restype = None
    iokit.IOServiceClose.argtypes = [ctypes.c_uint32]
    iokit.IOServiceClose.restype = ctypes.c_int32
    iokit.IOPMAssertionCreateWithName.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    iokit.IOPMAssertionCreateWithName.restype = ctypes.c_int32
    iokit.IOPMAssertionRelease.argtypes = [ctypes.c_uint32]
    iokit.IOPMAssertionRelease.restype = ctypes.c_int32

    core_foundation.CFStringCreateWithCString.argtypes = [
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.c_uint32,
    ]
    core_foundation.CFStringCreateWithCString.restype = ctypes.c_void_p
    core_foundation.CFRunLoopGetCurrent.argtypes = []
    core_foundation.CFRunLoopGetCurrent.restype = ctypes.c_void_p
    core_foundation.CFRunLoopAddSource.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    core_foundation.CFRunLoopAddSource.restype = None
    core_foundation.CFRunLoopRemoveSource.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    core_foundation.CFRunLoopRemoveSource.restype = None
    core_foundation.CFRunLoopRunInMode.argtypes = [
        ctypes.c_void_p,
        ctypes.c_double,
        ctypes.c_ubyte,
    ]
    core_foundation.CFRunLoopRunInMode.restype = ctypes.c_int32
    core_foundation.CFRunLoopStop.argtypes = [ctypes.c_void_p]
    core_foundation.CFRunLoopStop.restype = None
    core_foundation.CFRelease.argtypes = [ctypes.c_void_p]
    core_foundation.CFRelease.restype = None
    return callback_type


def _notification_id(argument: Optional[int]) -> int:
    return int(argument or 0)


def create_power_callback(
    iokit: Any,
    core_foundation: Any,
    callback_type: Any,
    connection: Callable[[], int],
    run_loop: int,
    state: Dict[str, bool],
) -> Any:
    """Create and retain the callback that distinguishes idle and forced sleep."""

    def power_changed(
        _refcon: Optional[int],
        _service: int,
        message_type: int,
        message_argument: Optional[int],
    ) -> None:
        notification_id = _notification_id(message_argument)
        if message_type == K_IO_MESSAGE_CAN_SYSTEM_SLEEP:
            iokit.IOCancelPowerChange(connection(), notification_id)
            return
        if message_type == K_IO_MESSAGE_SYSTEM_WILL_SLEEP:
            first_notification = not state["forced_sleep"]
            # Publish the durable state and protocol event before acknowledging
            # the notification.  Once IOAllowPowerChange returns, macOS may
            # suspend this process before it executes another Python bytecode.
            state["forced_sleep"] = True
            state["stopping"] = True
            try:
                if first_notification:
                    print("SLEEP", flush=True)
            finally:
                try:
                    iokit.IOAllowPowerChange(connection(), notification_id)
                finally:
                    core_foundation.CFRunLoopStop(run_loop)

    return callback_type(power_changed)


def _cf_string(core_foundation: Any, value: str) -> int:
    result = core_foundation.CFStringCreateWithCString(
        None,
        value.encode("utf-8"),
        K_CF_STRING_ENCODING_UTF8,
    )
    if not result:
        raise RuntimeError("could not create CoreFoundation string {!r}".format(value))
    return int(result)


def _create_assertion(iokit: Any, core_foundation: Any, assertion_type: str) -> int:
    type_string = _cf_string(core_foundation, assertion_type)
    reason_string = None  # type: Optional[int]
    assertion_id = ctypes.c_uint32()
    try:
        reason_string = _cf_string(core_foundation, ASSERTION_REASON)
        result = iokit.IOPMAssertionCreateWithName(
            type_string,
            K_IO_PM_ASSERTION_LEVEL_ON,
            reason_string,
            ctypes.byref(assertion_id),
        )
        if result != 0:
            raise RuntimeError(
                "could not create {} assertion (IOKit error {})".format(assertion_type, result)
            )
        return int(assertion_id.value)
    finally:
        if reason_string is not None:
            core_foundation.CFRelease(reason_string)
        core_foundation.CFRelease(type_string)


def _default_run_loop_mode(core_foundation: Any) -> int:
    mode = ctypes.c_void_p.in_dll(core_foundation, "kCFRunLoopDefaultMode").value
    if not mode:
        raise RuntimeError("could not get the default CoreFoundation run-loop mode")
    return int(mode)


def run_guard() -> int:
    """Block idle sleep and report a non-cancellable sleep through stdout."""
    if sys.platform != "darwin":
        raise RuntimeError("the power guard requires macOS")
    try:
        iokit = ctypes.CDLL(IOKIT_FRAMEWORK)
        core_foundation = ctypes.CDLL(CORE_FOUNDATION_FRAMEWORK)
    except OSError as exc:
        raise RuntimeError("could not load macOS power-management frameworks: {}".format(exc))

    callback_type = configure_functions(iokit, core_foundation)
    run_loop = core_foundation.CFRunLoopGetCurrent()
    if not run_loop:
        raise RuntimeError("could not get the CoreFoundation run loop")
    run_loop_mode = _default_run_loop_mode(core_foundation)

    notification_port = ctypes.c_void_p()
    notifier = ctypes.c_uint32()
    root_power = 0
    run_loop_source = None  # type: Optional[int]
    source_scheduled = False
    assertions: List[int] = []
    state = {"stopping": False, "forced_sleep": False}
    callback = create_power_callback(
        iokit,
        core_foundation,
        callback_type,
        lambda: root_power,
        int(run_loop),
        state,
    )

    previous_sigterm = None  # type: Any
    previous_sigint = None  # type: Any

    def stop_guard(_signal_number: int, _frame: object) -> None:
        state["stopping"] = True
        core_foundation.CFRunLoopStop(run_loop)

    try:
        # Install termination handlers before acquiring any IOKit resources so
        # a parent-side cancellation during initialization still takes the
        # normal cleanup path below.
        previous_sigterm = signal.signal(signal.SIGTERM, stop_guard)
        previous_sigint = signal.signal(signal.SIGINT, stop_guard)
        root_power = int(
            iokit.IORegisterForSystemPower(
                None,
                ctypes.byref(notification_port),
                callback,
                ctypes.byref(notifier),
            )
        )
        if not root_power or not notification_port.value or not notifier.value:
            raise RuntimeError("could not register for macOS system-power notifications")
        if state["stopping"]:
            return 0

        run_loop_source_value = iokit.IONotificationPortGetRunLoopSource(notification_port)
        if not run_loop_source_value:
            raise RuntimeError("could not create the power notification run-loop source")
        run_loop_source = int(run_loop_source_value)
        if state["stopping"]:
            return 0
        core_foundation.CFRunLoopAddSource(run_loop, run_loop_source, run_loop_mode)
        source_scheduled = True
        if state["stopping"]:
            return 0

        for assertion_type in ASSERTION_TYPES:
            if state["stopping"]:
                return 0
            assertions.append(_create_assertion(iokit, core_foundation, assertion_type))
            if state["stopping"]:
                return 0

        print("READY", flush=True)
        while not state["stopping"]:
            core_foundation.CFRunLoopRunInMode(run_loop_mode, 0.25, False)
    finally:
        for assertion_id in reversed(assertions):
            with contextlib.suppress(Exception):
                iokit.IOPMAssertionRelease(assertion_id)
        if source_scheduled and run_loop_source is not None:
            with contextlib.suppress(Exception):
                core_foundation.CFRunLoopRemoveSource(run_loop, run_loop_source, run_loop_mode)
        if notifier.value:
            with contextlib.suppress(Exception):
                iokit.IODeregisterForSystemPower(ctypes.byref(notifier))
        if notification_port.value:
            with contextlib.suppress(Exception):
                iokit.IONotificationPortDestroy(notification_port)
        if root_power:
            with contextlib.suppress(Exception):
                iokit.IOServiceClose(root_power)
        if previous_sigterm is not None:
            signal.signal(signal.SIGTERM, previous_sigterm)
        if previous_sigint is not None:
            signal.signal(signal.SIGINT, previous_sigint)
    return SLEEP_EXIT_CODE if state["forced_sleep"] else 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments:
        print("usage: python -m piburn._power_guard", file=sys.stderr)
        return 2
    if sys.platform != "darwin":
        print("power guard requires macOS", file=sys.stderr)
        return 2
    try:
        return run_guard()
    except Exception as exc:
        print("power guard failed: {}".format(exc), file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
