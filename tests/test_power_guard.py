import ctypes
import gc
import io
import signal
import subprocess
import weakref
from types import SimpleNamespace
from unittest import mock

import pytest

from piburn import _power_guard as helper
from piburn import cli as burn


class FakeFunction:
    """Record calls while behaving like a configurable ctypes function."""

    def __init__(self, implementation=None, result=0):
        self.implementation = implementation
        self.result = result
        self.calls = []
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        self.calls.append(args)
        if self.implementation is not None:
            return self.implementation(*args)
        return self.result


def test_power_guard_constants_match_the_macos_abi():
    """Power messages, assertion level, and string encoding retain their native values."""

    assert helper.K_IO_MESSAGE_CAN_SYSTEM_SLEEP == 0xE0000270
    assert helper.K_IO_MESSAGE_SYSTEM_WILL_SLEEP == 0xE0000280
    assert helper.K_IO_MESSAGE_SYSTEM_HAS_POWERED_ON == 0xE0000300
    assert helper.K_IO_MESSAGE_SYSTEM_WILL_POWER_ON == 0xE0000320
    assert helper.K_IO_PM_ASSERTION_LEVEL_ON == 255
    assert helper.K_CF_STRING_ENCODING_UTF8 == 0x08000100


def fake_abi_libraries():
    """Return complete fake IOKit and CoreFoundation function tables."""

    iokit_names = [
        "IORegisterForSystemPower",
        "IODeregisterForSystemPower",
        "IOAllowPowerChange",
        "IOCancelPowerChange",
        "IONotificationPortGetRunLoopSource",
        "IONotificationPortDestroy",
        "IOServiceClose",
        "IOPMAssertionCreateWithName",
        "IOPMAssertionRelease",
    ]
    core_names = [
        "CFStringCreateWithCString",
        "CFRunLoopGetCurrent",
        "CFRunLoopAddSource",
        "CFRunLoopRemoveSource",
        "CFRunLoopRunInMode",
        "CFRunLoopStop",
        "CFRelease",
    ]
    return (
        SimpleNamespace(**{name: FakeFunction() for name in iokit_names}),
        SimpleNamespace(**{name: FakeFunction() for name in core_names}),
    )


def test_configure_power_functions_declares_iokit_and_core_foundation_abi():
    """The helper declares every IOKit and CoreFoundation function with the exact ABI."""

    iokit, core = fake_abi_libraries()
    callback_type = helper.configure_functions(iokit, core)

    assert iokit.IORegisterForSystemPower.argtypes == [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        callback_type,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    assert iokit.IORegisterForSystemPower.restype is ctypes.c_uint32
    assert iokit.IODeregisterForSystemPower.argtypes == [ctypes.POINTER(ctypes.c_uint32)]
    assert iokit.IODeregisterForSystemPower.restype is ctypes.c_int32
    assert iokit.IOAllowPowerChange.argtypes == [ctypes.c_uint32, ctypes.c_ssize_t]
    assert iokit.IOAllowPowerChange.restype is ctypes.c_int32
    assert iokit.IOCancelPowerChange.argtypes == [ctypes.c_uint32, ctypes.c_ssize_t]
    assert iokit.IOCancelPowerChange.restype is ctypes.c_int32
    assert iokit.IONotificationPortGetRunLoopSource.argtypes == [ctypes.c_void_p]
    assert iokit.IONotificationPortGetRunLoopSource.restype is ctypes.c_void_p
    assert iokit.IONotificationPortDestroy.argtypes == [ctypes.c_void_p]
    assert iokit.IONotificationPortDestroy.restype is None
    assert iokit.IOServiceClose.argtypes == [ctypes.c_uint32]
    assert iokit.IOServiceClose.restype is ctypes.c_int32
    assert iokit.IOPMAssertionCreateWithName.argtypes == [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    assert iokit.IOPMAssertionCreateWithName.restype is ctypes.c_int32
    assert iokit.IOPMAssertionRelease.argtypes == [ctypes.c_uint32]
    assert iokit.IOPMAssertionRelease.restype is ctypes.c_int32
    assert core.CFStringCreateWithCString.argtypes == [
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.c_uint32,
    ]
    assert core.CFStringCreateWithCString.restype is ctypes.c_void_p
    assert core.CFRunLoopGetCurrent.argtypes == []
    assert core.CFRunLoopGetCurrent.restype is ctypes.c_void_p
    assert core.CFRunLoopAddSource.argtypes == [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    assert core.CFRunLoopAddSource.restype is None
    assert core.CFRunLoopRemoveSource.argtypes == [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    assert core.CFRunLoopRemoveSource.restype is None
    assert core.CFRunLoopRunInMode.argtypes == [ctypes.c_void_p, ctypes.c_double, ctypes.c_ubyte]
    assert core.CFRunLoopRunInMode.restype is ctypes.c_int32
    assert core.CFRunLoopStop.argtypes == [ctypes.c_void_p]
    assert core.CFRunLoopStop.restype is None
    assert core.CFRelease.argtypes == [ctypes.c_void_p]
    assert core.CFRelease.restype is None


def test_power_callback_declares_exact_cfunctype_abi():
    """The registered callback uses the four macOS arguments and a void return type."""

    iokit, core = fake_abi_libraries()
    callback_type = helper.configure_functions(iokit, core)

    assert callback_type._argtypes_ == (
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    )
    assert callback_type._restype_ is None
    callback = helper.create_power_callback(
        iokit,
        core,
        callback_type,
        lambda: 7,
        8,
        {"stopping": False, "forced_sleep": False},
    )
    assert isinstance(callback, callback_type)


@pytest.mark.parametrize("symbol_value", [None, 0], ids=["missing", "null"])
def test_default_run_loop_mode_reads_exact_symbol_and_rejects_null_pointer(symbol_value):
    """Look up kCFRunLoopDefaultMode by its exact symbol name and reject null pointers."""

    pointer_type = mock.Mock()
    pointer_type.in_dll.return_value = SimpleNamespace(value=symbol_value)
    core_foundation = object()
    with mock.patch.object(helper.ctypes, "c_void_p", pointer_type), pytest.raises(
        RuntimeError, match="could not get the default CoreFoundation run-loop mode"
    ):
        helper._default_run_loop_mode(core_foundation)

    pointer_type.in_dll.assert_called_once_with(core_foundation, "kCFRunLoopDefaultMode")


def test_default_run_loop_mode_returns_the_native_symbol_address():
    """A valid kCFRunLoopDefaultMode pointer is returned as a Python integer."""

    pointer_type = mock.Mock()
    pointer_type.in_dll.return_value = SimpleNamespace(value=42)
    core_foundation = object()
    with mock.patch.object(helper.ctypes, "c_void_p", pointer_type):
        assert helper._default_run_loop_mode(core_foundation) == 42

    pointer_type.in_dll.assert_called_once_with(core_foundation, "kCFRunLoopDefaultMode")


@pytest.mark.parametrize(
    "failure_stage", ["iokit-framework", "core-foundation-framework", "run-loop", "mode"]
)
def test_power_guard_reports_bootstrap_failure_before_acquiring_resources(
    failure_stage, capsys
):
    """Report precise framework, run-loop, or mode failures before power registration."""

    iokit, core = fake_abi_libraries()
    core.CFRunLoopGetCurrent.result = 24
    if failure_stage == "run-loop":
        core.CFRunLoopGetCurrent.result = 0
    framework_error = OSError("framework missing")
    if failure_stage == "iokit-framework":
        framework_load_side_effect = framework_error
    elif failure_stage == "core-foundation-framework":
        framework_load_side_effect = [iokit, framework_error]
    else:
        framework_load_side_effect = [iokit, core]
    mode_error = RuntimeError("could not get the default CoreFoundation run-loop mode")
    run_loop_mode_outcome = mode_error if failure_stage == "mode" else 25
    with mock.patch.object(helper.sys, "platform", "darwin"), mock.patch.object(
        helper.ctypes, "CDLL", side_effect=framework_load_side_effect
    ) as load_framework, mock.patch.object(
        helper,
        "_default_run_loop_mode",
        side_effect=[run_loop_mode_outcome],
    ):
        assert helper.main([]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    expected_error_detail = {
        "iokit-framework": "could not load macOS power-management frameworks: framework missing",
        "core-foundation-framework": (
            "could not load macOS power-management frameworks: framework missing"
        ),
        "run-loop": "could not get the CoreFoundation run loop",
        "mode": "could not get the default CoreFoundation run-loop mode",
    }[failure_stage]
    assert captured.err == "power guard failed: {}\n".format(expected_error_detail)
    if failure_stage == "iokit-framework":
        load_framework.assert_called_once_with(helper.IOKIT_FRAMEWORK)
    else:
        assert load_framework.call_args_list == [
            mock.call(helper.IOKIT_FRAMEWORK),
            mock.call(helper.CORE_FOUNDATION_FRAMEWORK),
        ]
    assert iokit.IORegisterForSystemPower.calls == []


def make_callback():
    """Build a power callback with observable libraries and mutable state."""

    iokit, core = fake_abi_libraries()
    state = {"stopping": False, "forced_sleep": False}
    callback = helper.create_power_callback(iokit, core, lambda function: function, lambda: 44, 55, state)
    return iokit, core, state, callback


def test_power_guard_vetoes_idle_sleep_without_stopping(capsys):
    """An idle-sleep request is cancelled without stopping the helper."""

    iokit, core, state, callback = make_callback()

    callback(None, 1, helper.K_IO_MESSAGE_CAN_SYSTEM_SLEEP, 987)

    assert iokit.IOCancelPowerChange.calls == [(44, 987)]
    assert iokit.IOAllowPowerChange.calls == []
    assert core.CFRunLoopStop.calls == []
    assert state == {"stopping": False, "forced_sleep": False}
    assert capsys.readouterr().out == ""


def test_power_guard_acknowledges_forced_sleep_and_reports_it(capsys):
    """Acknowledge repeated forced-sleep notices, emit SLEEP once, and exit 75 after cleanup."""

    iokit, core, state, callback = make_callback()

    callback(None, 1, helper.K_IO_MESSAGE_SYSTEM_WILL_SLEEP, 321)
    callback(None, 1, helper.K_IO_MESSAGE_SYSTEM_WILL_SLEEP, 322)

    assert iokit.IOAllowPowerChange.calls == [(44, 321), (44, 322)]
    assert iokit.IOCancelPowerChange.calls == []
    assert core.CFRunLoopStop.calls == [(55,), (55,)]
    assert state == {"stopping": True, "forced_sleep": True}
    assert capsys.readouterr().out == "SLEEP\n"

    events = []
    runtime_iokit, runtime_core, callbacks = runtime_libraries(events)

    def force_sleep(_handlers):
        callbacks["callback"](None, 1, helper.K_IO_MESSAGE_SYSTEM_WILL_SLEEP, 654)
        return 0

    assert run_fake_guard(runtime_iokit, runtime_core, events, force_sleep) == 75
    assert capsys.readouterr().out == "READY\nSLEEP\n"
    assert runtime_iokit.IOPMAssertionRelease.calls == [(32,), (31,)]
    assert runtime_core.CFRunLoopRemoveSource.calls == [(24, 23, 25)]
    assert runtime_iokit.IODeregisterForSystemPower.calls[0][0]._obj.value == 21
    assert runtime_iokit.IONotificationPortDestroy.calls[0][0].value == 20
    assert runtime_iokit.IOServiceClose.calls == [(22,)]


def test_power_guard_publishes_sleep_before_allowing_forced_sleep():
    """The SLEEP line is flushed before macOS may suspend the process."""

    iokit, _, state, callback = make_callback()
    events = []
    iokit.IOAllowPowerChange.implementation = lambda *_args: events.append("allow") or 0

    with mock.patch(
        "builtins.print", side_effect=lambda *_args, **_kwargs: events.append("SLEEP")
    ) as print_sleep:
        callback(None, 1, helper.K_IO_MESSAGE_SYSTEM_WILL_SLEEP, 321)

    assert events == ["SLEEP", "allow"]
    print_sleep.assert_called_once_with("SLEEP", flush=True)
    assert state == {"stopping": True, "forced_sleep": True}


def test_power_guard_allows_sleep_and_stops_if_sleep_publication_fails():
    """A failed SLEEP print cannot withhold acknowledgement or leave the run loop active."""

    iokit, core, state, callback = make_callback()

    with mock.patch("builtins.print", side_effect=OSError("stdout closed")), pytest.raises(
        OSError, match="stdout closed"
    ):
        callback(None, 1, helper.K_IO_MESSAGE_SYSTEM_WILL_SLEEP, 321)

    assert iokit.IOAllowPowerChange.calls == [(44, 321)]
    assert core.CFRunLoopStop.calls == [(55,)]
    assert state == {"stopping": True, "forced_sleep": True}


@pytest.mark.parametrize(
    "notification",
    [helper.K_IO_MESSAGE_SYSTEM_WILL_POWER_ON, helper.K_IO_MESSAGE_SYSTEM_HAS_POWERED_ON],
)
def test_power_guard_ignores_wake_notifications(notification, capsys):
    """Wake notifications neither acknowledge sleep nor create a retry event."""

    iokit, core, state, callback = make_callback()

    callback(None, 1, notification, 123)

    assert iokit.IOAllowPowerChange.calls == []
    assert iokit.IOCancelPowerChange.calls == []
    assert core.CFRunLoopStop.calls == []
    assert state == {"stopping": False, "forced_sleep": False}
    assert capsys.readouterr().out == ""


def runtime_libraries(events, assertion_failure=None, registration_failure=False):
    """Build runtime fakes that expose resource acquisition and cleanup order."""

    iokit, core = fake_abi_libraries()
    strings = {}
    next_string = iter(range(100, 120))
    assertion_number = 0
    callback_holder = {}

    def register(_refcon, port_pointer, callback, notifier_pointer):
        events.append("register")
        ctypes.cast(port_pointer, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.c_void_p(20)
        ctypes.cast(notifier_pointer, ctypes.POINTER(ctypes.c_uint32))[0] = ctypes.c_uint32(21)
        callback_holder["callback"] = callback
        return 0 if registration_failure else 22

    def create_string(_allocator, value, _encoding):
        reference = next(next_string)
        strings[reference] = value.decode()
        return reference

    def create_assertion(type_reference, level, reason_reference, assertion_pointer):
        nonlocal assertion_number
        assertion_number += 1
        assertion_type = strings[int(type_reference)]
        assertion_reason = strings[int(reason_reference)]
        events.append(("assert", assertion_type, level, assertion_reason))
        if assertion_failure == assertion_number:
            return 99
        ctypes.cast(assertion_pointer, ctypes.POINTER(ctypes.c_uint32))[0] = ctypes.c_uint32(
            30 + assertion_number
        )
        return 0

    iokit.IORegisterForSystemPower.implementation = register
    iokit.IONotificationPortGetRunLoopSource.result = 23
    iokit.IOPMAssertionCreateWithName.implementation = create_assertion
    iokit.IOPMAssertionRelease.implementation = lambda assertion_id: events.append(
        ("release", assertion_id)
    ) or 0
    iokit.IODeregisterForSystemPower.implementation = lambda _notifier: events.append("deregister") or 0
    iokit.IONotificationPortDestroy.implementation = lambda _port: events.append("destroy-port")
    iokit.IOServiceClose.implementation = lambda _connection: events.append("close-connection") or 0
    core.CFStringCreateWithCString.implementation = create_string
    core.CFRunLoopGetCurrent.result = 24
    core.CFRunLoopAddSource.implementation = lambda *_args: events.append("schedule")
    core.CFRunLoopRemoveSource.implementation = lambda *_args: events.append("unschedule")
    core.CFRelease.implementation = lambda reference: events.append(("cf-release", reference))
    return iokit, core, callback_holder


def run_fake_guard(iokit, core, events, loop_action):
    """Run the helper against fake frameworks and a caller-controlled run loop."""

    handlers = {}

    def install_handler(signum, handler):
        handlers[signum] = handler
        return signal.SIG_DFL

    def load_framework(framework):
        events.append(("load", framework))
        return {
            helper.IOKIT_FRAMEWORK: iokit,
            helper.CORE_FOUNDATION_FRAMEWORK: core,
        }[framework]

    core.CFRunLoopRunInMode.implementation = lambda *_args: loop_action(handlers)
    with mock.patch.object(helper.sys, "platform", "darwin"), mock.patch.object(
        helper.ctypes, "CDLL", side_effect=load_framework
    ), mock.patch.object(helper, "_default_run_loop_mode", return_value=25), mock.patch.object(
        helper.signal, "signal", side_effect=install_handler
    ):
        return helper.run_guard()


def test_power_guard_becomes_ready_only_after_full_initialization():
    """READY is flushed only after registration, scheduling, and both assertions."""

    events = []
    iokit, core, _callbacks = runtime_libraries(events)

    def stop_normally(handlers):
        handlers[signal.SIGTERM](signal.SIGTERM, None)
        return 0

    with mock.patch(
        "builtins.print", side_effect=lambda value, **_kwargs: events.append(("print", value))
    ) as print_ready:
        assert run_fake_guard(iokit, core, events, stop_normally) == 0

    assert events[:2] == [
        ("load", helper.IOKIT_FRAMEWORK),
        ("load", helper.CORE_FOUNDATION_FRAMEWORK),
    ]
    assert helper.ASSERTION_REASON == "piburn is preparing a removable boot card"
    expected_assertion_events = [
        (
            "assert",
            "PreventUserIdleSystemSleep",
            helper.K_IO_PM_ASSERTION_LEVEL_ON,
            helper.ASSERTION_REASON,
        ),
        (
            "assert",
            "PreventSystemSleep",
            helper.K_IO_PM_ASSERTION_LEVEL_ON,
            helper.ASSERTION_REASON,
        ),
    ]
    initialization_events = [
        event
        for event in events
        if event in {"register", "schedule"}
        or (isinstance(event, tuple) and event[0] in {"assert", "print"})
    ]
    assert initialization_events == [
        "register",
        "schedule",
        *expected_assertion_events,
        ("print", "READY"),
    ]
    assert events.index(("print", "READY")) < events.index(("release", 32))
    assert events.index(("print", "READY")) < events.index("unschedule")
    print_ready.assert_called_once_with("READY", flush=True)
    assert iokit.IONotificationPortGetRunLoopSource.calls[0][0].value == 20
    assert core.CFRunLoopGetCurrent.calls == [()]
    assert core.CFRunLoopAddSource.calls == [(24, 23, 25)]
    assert core.CFRunLoopRunInMode.calls == [(25, 0.25, False)]
    assert core.CFStringCreateWithCString.calls == [
        (None, b"PreventUserIdleSystemSleep", helper.K_CF_STRING_ENCODING_UTF8),
        (None, helper.ASSERTION_REASON.encode("utf-8"), helper.K_CF_STRING_ENCODING_UTF8),
        (None, b"PreventSystemSleep", helper.K_CF_STRING_ENCODING_UTF8),
        (None, helper.ASSERTION_REASON.encode("utf-8"), helper.K_CF_STRING_ENCODING_UTF8),
    ]
def test_power_guard_continues_run_loop_after_vetoing_idle_sleep(capsys):
    """An idle-sleep veto returns to the run loop until a later shutdown signal."""

    events = []
    iokit, core, callbacks = runtime_libraries(events)
    run_loop_iteration_count = 0

    def idle_then_stop(handlers):
        nonlocal run_loop_iteration_count
        run_loop_iteration_count += 1
        if run_loop_iteration_count == 1:
            callbacks["callback"](None, 1, helper.K_IO_MESSAGE_CAN_SYSTEM_SLEEP, 987)
        else:
            handlers[signal.SIGTERM](signal.SIGTERM, None)
        return 0

    assert run_fake_guard(iokit, core, events, idle_then_stop) == 0

    assert core.CFRunLoopRunInMode.calls == [
        (25, 0.25, False),
        (25, 0.25, False),
    ]
    assert iokit.IOCancelPowerChange.calls == [(22, 987)]
    assert iokit.IOAllowPowerChange.calls == []
    assert capsys.readouterr().out == "READY\n"


def test_power_guard_retains_callback_for_the_whole_run_loop_lifetime():
    """Keep the registered CFUNCTYPE callback alive for the entire run loop.

    The fake registration keeps only a weak reference, so forced collection
    proves that the helper owns the callback until shutdown.
    """

    iokit, core, _ = runtime_libraries([])
    callback_reference = None
    handlers = {}

    def register_without_retaining(_refcon, port_pointer, callback, notifier_pointer):
        nonlocal callback_reference
        ctypes.cast(port_pointer, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.c_void_p(20)
        ctypes.cast(notifier_pointer, ctypes.POINTER(ctypes.c_uint32))[0] = ctypes.c_uint32(21)
        callback_reference = weakref.ref(callback)
        iokit.IORegisterForSystemPower.calls.clear()
        return 22

    def install_handler(signum, handler):
        handlers[signum] = handler
        return signal.SIG_DFL

    def collect_and_stop(*_args):
        gc.collect()
        assert callback_reference is not None
        callback = callback_reference()
        assert callback is not None
        callback(None, 1, helper.K_IO_MESSAGE_CAN_SYSTEM_SLEEP, 987)
        handlers[signal.SIGTERM](signal.SIGTERM, None)
        return 0

    iokit.IORegisterForSystemPower.implementation = register_without_retaining
    core.CFRunLoopRunInMode.implementation = collect_and_stop
    with mock.patch.object(helper.sys, "platform", "darwin"), mock.patch.object(
        helper.ctypes, "CDLL", side_effect=[iokit, core]
    ), mock.patch.object(helper, "_default_run_loop_mode", return_value=25), mock.patch.object(
        helper.signal, "signal", side_effect=install_handler
    ):
        assert helper.run_guard() == 0

    assert iokit.IORegisterForSystemPower.calls == []
    assert iokit.IOCancelPowerChange.calls == [(22, 987)]


@pytest.mark.parametrize(
    (
        "interrupt_stage",
        "expected_assertion_count",
        "expected_unschedule_call_count",
    ),
    [
        ("registration", 0, 0),
        ("source", 0, 0),
        ("scheduling", 0, 1),
        ("first-assertion", 1, 1),
        ("second-assertion", 2, 1),
    ],
)
def test_power_guard_stops_initialization_immediately_after_sigterm(
    interrupt_stage, expected_assertion_count, expected_unschedule_call_count, capsys
):
    """SIGTERM stops later initialization and releases registered resources and assertions."""

    iokit, core, _ = runtime_libraries([])
    handlers = {}

    def install_handler(signum, handler):
        handlers[signum] = handler
        return signal.SIG_DFL

    def interrupt() -> None:
        handlers[signal.SIGTERM](signal.SIGTERM, None)

    if interrupt_stage == "registration":
        register = iokit.IORegisterForSystemPower.implementation

        def interrupt_registration(*args):
            power_connection = register(*args)
            interrupt()
            return power_connection

        iokit.IORegisterForSystemPower.implementation = interrupt_registration
    elif interrupt_stage == "source":
        iokit.IONotificationPortGetRunLoopSource.implementation = lambda *_args: interrupt() or 23
    elif interrupt_stage == "scheduling":
        core.CFRunLoopAddSource.implementation = lambda *_args: interrupt()
    else:
        create_assertion = iokit.IOPMAssertionCreateWithName.implementation
        assertion_call_count = 0

        def interrupt_assertion(*args):
            nonlocal assertion_call_count
            assertion_call_count += 1
            assertion_status = create_assertion(*args)
            interrupting_assertion_number = (
                1 if interrupt_stage == "first-assertion" else 2
            )
            if assertion_call_count == interrupting_assertion_number:
                interrupt()
            return assertion_status

        iokit.IOPMAssertionCreateWithName.implementation = interrupt_assertion

    with mock.patch.object(helper.sys, "platform", "darwin"), mock.patch.object(
        helper.ctypes, "CDLL", side_effect=[iokit, core]
    ), mock.patch.object(helper, "_default_run_loop_mode", return_value=25), mock.patch.object(
        helper.signal, "signal", side_effect=install_handler
    ):
        assert helper.run_guard() == 0

    assert capsys.readouterr().out == ""
    assert core.CFRunLoopRunInMode.calls == []
    assert len(iokit.IOPMAssertionCreateWithName.calls) == expected_assertion_count
    assert len(iokit.IOPMAssertionRelease.calls) == expected_assertion_count
    assert len(core.CFRunLoopRemoveSource.calls) == expected_unschedule_call_count
    assert len(iokit.IODeregisterForSystemPower.calls) == 1
    assert len(iokit.IONotificationPortDestroy.calls) == 1
    assert len(iokit.IOServiceClose.calls) == 1


@pytest.mark.parametrize(
    "failing_resource", ["assertion", "source", "notifier", "port", "connection"]
)
def test_power_guard_continues_cleanup_after_resource_release_failure(failing_resource):
    """Failures while cleaning up one resource kind do not block cleanup of the others."""

    events = []
    iokit, core, _callbacks = runtime_libraries(events)

    def fail_cleanup(*_args):
        raise RuntimeError("{} cleanup failed".format(failing_resource))

    release_resource = {
        "assertion": iokit.IOPMAssertionRelease,
        "source": core.CFRunLoopRemoveSource,
        "notifier": iokit.IODeregisterForSystemPower,
        "port": iokit.IONotificationPortDestroy,
        "connection": iokit.IOServiceClose,
    }[failing_resource]
    release_resource.implementation = fail_cleanup

    def stop_normally(handlers):
        handlers[signal.SIGTERM](signal.SIGTERM, None)
        return 0

    assert run_fake_guard(iokit, core, events, stop_normally) == 0
    assert iokit.IOPMAssertionRelease.calls == [(32,), (31,)]
    assert core.CFRunLoopRemoveSource.calls == [(24, 23, 25)]
    assert iokit.IODeregisterForSystemPower.calls[0][0]._obj.value == 21
    assert iokit.IONotificationPortDestroy.calls[0][0].value == 20
    assert iokit.IOServiceClose.calls == [(22,)]


def test_power_guard_cleans_up_after_post_ready_run_loop_failure(capsys):
    """Report a post-READY run-loop failure and release both assertions.

    Also remove the run-loop source and tear down the notifier, notification
    port, and power connection.
    """

    iokit, core, _ = runtime_libraries([])
    core.CFRunLoopRunInMode.implementation = mock.Mock(side_effect=RuntimeError("run loop failed"))
    with mock.patch.object(helper.sys, "platform", "darwin"), mock.patch.object(
        helper.ctypes, "CDLL", side_effect=[iokit, core]
    ), mock.patch.object(helper, "_default_run_loop_mode", return_value=25):
        exit_code = helper.main([])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == "READY\n"
    assert captured.err == "power guard failed: run loop failed\n"
    assert iokit.IOPMAssertionRelease.calls == [(32,), (31,)]
    assert core.CFRunLoopRemoveSource.calls == [(24, 23, 25)]
    assert iokit.IODeregisterForSystemPower.calls[0][0]._obj.value == 21
    assert iokit.IONotificationPortDestroy.calls[0][0].value == 20
    assert iokit.IOServiceClose.calls == [(22,)]


def test_power_guard_releases_every_resource_on_normal_shutdown():
    """Normal shutdown releases every assertion, CF string, and IOKit resource once."""

    events = []
    iokit, core, _callbacks = runtime_libraries(events)

    def stop_normally(handlers):
        handlers[signal.SIGTERM](signal.SIGTERM, None)
        return 0

    assert run_fake_guard(iokit, core, events, stop_normally) == 0
    assert [event for event in events if isinstance(event, tuple) and event[0] == "cf-release"] == [
        ("cf-release", 101),
        ("cf-release", 100),
        ("cf-release", 103),
        ("cf-release", 102),
    ]
    assert events.count("deregister") == 1
    assert events.count("destroy-port") == 1
    assert iokit.IOPMAssertionRelease.calls == [(32,), (31,)]
    assert core.CFRunLoopRemoveSource.calls == [(24, 23, 25)]
    assert iokit.IODeregisterForSystemPower.calls[0][0]._obj.value == 21
    assert iokit.IONotificationPortDestroy.calls[0][0].value == 20
    assert iokit.IOServiceClose.calls == [(22,)]


@pytest.mark.parametrize(
    "failure_stage", ["registration", "source", "scheduling", "first-assertion", "second-assertion"]
)
def test_power_guard_cleans_up_partial_initialization_failure(failure_stage, capsys):
    """Release resources acquired before a registration, source, scheduling, or assertion failure."""

    events = []
    failing_assertion_number = {"first-assertion": 1, "second-assertion": 2}.get(
        failure_stage
    )
    iokit, core, _callbacks = runtime_libraries(
        events,
        assertion_failure=failing_assertion_number,
        registration_failure=failure_stage == "registration",
    )
    if failure_stage == "scheduling":
        core.CFRunLoopAddSource.implementation = mock.Mock(side_effect=RuntimeError("schedule failed"))
    elif failure_stage == "source":
        iokit.IONotificationPortGetRunLoopSource.result = 0
    with mock.patch.object(helper.sys, "platform", "darwin"), mock.patch.object(
        helper.ctypes, "CDLL", side_effect=[iokit, core]
    ), mock.patch.object(helper, "_default_run_loop_mode", return_value=25):
        exit_code = helper.main([])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    expected_assertion_release_count = 1 if failure_stage == "second-assertion" else 0
    assert (
        len([event for event in events if isinstance(event, tuple) and event[0] == "release"])
        == expected_assertion_release_count
    )
    expected_cf_releases = {
        "registration": [],
        "source": [],
        "scheduling": [],
        "first-assertion": [("cf-release", 101), ("cf-release", 100)],
        "second-assertion": [
            ("cf-release", 101),
            ("cf-release", 100),
            ("cf-release", 103),
            ("cf-release", 102),
        ],
    }[failure_stage]
    assert [
        event for event in events if isinstance(event, tuple) and event[0] == "cf-release"
    ] == expected_cf_releases
    expected_unschedule_call_count = (
        1 if failure_stage in ("first-assertion", "second-assertion") else 0
    )
    assert events.count("unschedule") == expected_unschedule_call_count
    assert events.count("destroy-port") == 1
    assert events.count("deregister") == 1
    assert events.count("close-connection") == (0 if failure_stage == "registration" else 1)
    expected_error_detail = {
        "registration": "could not register for macOS system-power notifications",
        "source": "could not create the power notification run-loop source",
        "scheduling": "schedule failed",
        "first-assertion": "could not create PreventUserIdleSystemSleep assertion (IOKit error 99)",
        "second-assertion": "could not create PreventSystemSleep assertion (IOKit error 99)",
    }[failure_stage]
    assert captured.err == "power guard failed: {}\n".format(expected_error_detail)


@pytest.mark.parametrize(
    (
        "missing_resource",
        "expected_deregister_call_count",
        "expected_destroy_call_count",
        "expected_close_call_count",
    ),
    [
        ("connection", 1, 1, 0),
        ("port", 1, 0, 1),
        ("notifier", 0, 1, 1),
    ],
)
def test_power_guard_rejects_incomplete_power_registration_and_releases_present_resources(
    missing_resource,
    expected_deregister_call_count,
    expected_destroy_call_count,
    expected_close_call_count,
    capsys,
):
    """Reject an incomplete power registration and release every returned registration resource."""

    iokit, core, _ = runtime_libraries([])
    register_for_power = iokit.IORegisterForSystemPower.implementation

    def register_with_missing_resource(refcon, port_pointer, callback, notifier_pointer):
        connection = register_for_power(refcon, port_pointer, callback, notifier_pointer)
        if missing_resource == "connection":
            return 0
        if missing_resource == "port":
            ctypes.cast(port_pointer, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.c_void_p()
        else:
            ctypes.cast(notifier_pointer, ctypes.POINTER(ctypes.c_uint32))[0] = ctypes.c_uint32()
        return connection

    iokit.IORegisterForSystemPower.implementation = register_with_missing_resource
    with mock.patch.object(helper.sys, "platform", "darwin"), mock.patch.object(
        helper.ctypes, "CDLL", side_effect=[iokit, core]
    ), mock.patch.object(helper, "_default_run_loop_mode", return_value=25):
        assert helper.main([]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "could not register for macOS system-power notifications" in captured.err
    assert len(iokit.IODeregisterForSystemPower.calls) == expected_deregister_call_count
    assert len(iokit.IONotificationPortDestroy.calls) == expected_destroy_call_count
    assert len(iokit.IOServiceClose.calls) == expected_close_call_count
    assert iokit.IONotificationPortGetRunLoopSource.calls == []
    assert iokit.IOPMAssertionCreateWithName.calls == []


@pytest.mark.parametrize("failed_string", ["type", "reason"])
def test_create_assertion_releases_strings_after_core_foundation_failure(failed_string):
    """If either CF string creation fails, skip the assertion and release any earlier string."""

    iokit, core = fake_abi_libraries()
    if failed_string == "type":
        core.CFStringCreateWithCString.result = 0
    else:
        core.CFStringCreateWithCString.implementation = mock.Mock(side_effect=[100, 0])

    with pytest.raises(RuntimeError, match="could not create CoreFoundation string"):
        helper._create_assertion(iokit, core, "PreventSystemSleep")

    assert iokit.IOPMAssertionCreateWithName.calls == []
    assert core.CFRelease.calls == ([] if failed_string == "type" else [(100,)])


def test_power_guard_restores_previous_signal_handlers():
    """Helper shutdown restores the SIGTERM and SIGINT handlers it replaced."""

    iokit, core, _ = runtime_libraries([])
    installed_handlers = {}
    previous_sigterm = object()
    previous_sigint = object()

    def install_handler(signum, handler):
        if signum not in installed_handlers:
            installed_handlers[signum] = handler
            return previous_sigterm if signum == signal.SIGTERM else previous_sigint
        return signal.SIG_DFL

    def stop_normally(*_args):
        installed_handlers[signal.SIGTERM](signal.SIGTERM, None)
        return 0

    core.CFRunLoopRunInMode.implementation = stop_normally
    with mock.patch.object(helper.sys, "platform", "darwin"), mock.patch.object(
        helper.ctypes, "CDLL", side_effect=[iokit, core]
    ), mock.patch.object(helper, "_default_run_loop_mode", return_value=25), mock.patch.object(
        helper.signal, "signal", side_effect=install_handler
    ) as set_signal:
        assert helper.run_guard() == 0

    assert set_signal.call_args_list == [
        mock.call(signal.SIGTERM, installed_handlers[signal.SIGTERM]),
        mock.call(signal.SIGINT, installed_handlers[signal.SIGINT]),
        mock.call(signal.SIGTERM, previous_sigterm),
        mock.call(signal.SIGINT, previous_sigint),
    ]


@pytest.mark.parametrize(
    ("arguments", "platform", "expected_stderr"),
    [
        (["unexpected"], "darwin", "usage: python -m piburn._power_guard\n"),
        ([], "linux", "power guard requires macOS\n"),
    ],
)
def test_power_guard_private_main_validates_platform_and_arguments(
    arguments, platform, expected_stderr, capsys, monkeypatch
):
    """Each invalid invocation exits early with its own exact diagnostic."""

    load_framework = mock.Mock()
    monkeypatch.setattr(helper.sys, "platform", platform)
    monkeypatch.setattr(helper.ctypes, "CDLL", load_framework)

    assert helper.main(arguments) == 2

    load_framework.assert_not_called()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == expected_stderr


@pytest.mark.parametrize(
    ("failing_process_operation", "expected_diagnostic"),
    [
        ("poll", "could not inspect helper: poll failed"),
        ("terminate", "could not terminate helper: terminate failed"),
        ("communicate", "could not reap helper: communicate failed"),
        ("kill", "could not kill helper: kill failed"),
        ("final-wait", "could not wait for helper after kill: wait failed"),
    ],
)
def test_stop_helper_process_returns_process_api_failures_as_cleanup_diagnostics(
    failing_process_operation, expected_diagnostic
):
    """Return process-control failures as cleanup diagnostics instead of raising them."""

    process = mock.Mock()
    process.poll.return_value = None
    process.communicate.return_value = (b"", b"")
    if failing_process_operation == "poll":
        process.poll.side_effect = [OSError("poll failed")]
    elif failing_process_operation == "terminate":
        process.terminate.side_effect = OSError("terminate failed")
    elif failing_process_operation == "communicate":
        process.communicate.side_effect = OSError("communicate failed")
        process.wait.return_value = 0
    elif failing_process_operation == "kill":
        process.communicate.side_effect = [
            subprocess.TimeoutExpired("power guard", 3),
            subprocess.TimeoutExpired("power guard", 3),
        ]
        process.kill.side_effect = OSError("kill failed")
        process.wait.side_effect = subprocess.TimeoutExpired("power guard", 3)
    else:
        process.communicate.side_effect = [
            subprocess.TimeoutExpired("power guard", 3),
            OSError("final communicate failed"),
        ]
        process.wait.side_effect = OSError("wait failed")

    cleanup_diagnostic = burn.stop_helper_process(process)

    assert expected_diagnostic in cleanup_diagnostic
    if failing_process_operation in {"poll", "terminate"}:
        process.terminate.assert_called_once_with()
        process.communicate.assert_called_once_with(timeout=3)
        process.kill.assert_not_called()
        process.wait.assert_not_called()
    elif failing_process_operation == "communicate":
        process.kill.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=3)
    elif failing_process_operation == "kill":
        process.kill.assert_called_once_with()
        assert process.communicate.call_args_list == [mock.call(timeout=3), mock.call(timeout=3)]
        process.wait.assert_called_once_with(timeout=3)
        assert "could not reap helper after kill:" in cleanup_diagnostic
        assert "could not wait for helper after kill:" in cleanup_diagnostic
    elif failing_process_operation == "final-wait":
        assert "could not reap helper after kill: final communicate failed" in cleanup_diagnostic
        process.wait.assert_called_once_with(timeout=3)


def test_stop_helper_process_reports_all_fallback_failures_after_communicate_error():
    """After communicate fails, return every fallback cleanup failure as diagnostics.

    Cover errors while polling, killing, and waiting for the helper process.
    """

    process = mock.Mock()
    process.poll.side_effect = [None, OSError("recheck failed")]
    process.communicate.side_effect = OSError("communicate failed")
    process.kill.side_effect = OSError("fallback kill failed")
    process.wait.side_effect = subprocess.TimeoutExpired("power guard", 3)

    cleanup_diagnostic = burn.stop_helper_process(process)

    assert "could not reap helper: communicate failed" in cleanup_diagnostic
    assert "could not inspect helper after reap failure: recheck failed" in cleanup_diagnostic
    assert "could not kill helper after reap failure: fallback kill failed" in cleanup_diagnostic
    assert "could not stop helper after reap failure:" in cleanup_diagnostic
    process.terminate.assert_called_once_with()
    process.kill.assert_called_once_with()
    process.wait.assert_called_once_with(timeout=3)


def test_system_sleep_guard_starts_exact_helper_and_reaps_it():
    """The parent starts the private helper with this interpreter and reaps it."""

    process = mock.Mock()
    process.stdout = io.BytesIO(b"READY\n")
    process.poll.return_value = None
    process.communicate.return_value = (b"", b"")
    with mock.patch.object(burn.subprocess, "Popen", return_value=process) as popen, mock.patch.object(
        burn.select, "select", return_value=([process.stdout], [], [])
    ):
        guard = burn.SystemSleepGuard()
        guard.start()
        assert guard.stop() == ""

    popen.assert_called_once_with(
        [burn.sys.executable, "-m", "piburn._power_guard"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    process.terminate.assert_called_once_with()
    process.communicate.assert_called_once_with(timeout=3)


def test_system_sleep_guard_reports_helper_launch_failure():
    """An OS error before a child exists is reported as a launch failure."""

    with mock.patch.object(
        burn.subprocess, "Popen", side_effect=OSError("executable unavailable")
    ), pytest.raises(burn.BurnError, match="Could not start.*executable unavailable"):
        burn.SystemSleepGuard().start()


def test_system_sleep_guard_reaps_helper_with_missing_stdout():
    """Reject a child without protocol stdout, include its stderr diagnostic, and reap it."""

    process = mock.Mock()
    process.stdout = None
    process.poll.return_value = None
    process.communicate.return_value = (b"", b"missing protocol pipe")

    with mock.patch.object(burn.subprocess, "Popen", return_value=process), pytest.raises(
        burn.BurnError, match="missing protocol pipe"
    ):
        burn.SystemSleepGuard().start()

    process.terminate.assert_called_once_with()
    process.communicate.assert_called_once_with(timeout=3)


@pytest.mark.parametrize(
    ("readiness_failure", "protocol_output", "stdout_readable"),
    [
        ("timeout", b"READY\n", False),
        ("end-of-file", b"", True),
        ("wrong-message", b"WAIT\n", True),
    ],
)
def test_system_sleep_guard_reaps_helper_that_does_not_become_ready(
    readiness_failure, protocol_output, stdout_readable
):
    """Reject and reap the helper after a startup timeout, EOF, or unexpected message."""

    process = mock.Mock()
    process.stdout = io.BytesIO(protocol_output)
    process.poll.return_value = None
    process.communicate.return_value = (
        b"",
        "{} diagnostic".format(readiness_failure).encode(),
    )
    readable_streams = [process.stdout] if stdout_readable else []

    with mock.patch.object(burn.subprocess, "Popen", return_value=process), mock.patch.object(
        burn.select, "select", return_value=(readable_streams, [], [])
    ) as wait_until_ready, pytest.raises(
        burn.BurnError, match="{} diagnostic".format(readiness_failure)
    ):
        burn.SystemSleepGuard().start()

    wait_until_ready.assert_called_once_with(
        [process.stdout], [], [], burn.POWER_GUARD_READY_TIMEOUT
    )
    process.terminate.assert_called_once_with()
    process.communicate.assert_called_once_with(timeout=3)


@pytest.mark.parametrize("failing_protocol_operation", ["select", "readline"])
def test_system_sleep_guard_reaps_helper_after_startup_protocol_os_error(
    failing_protocol_operation,
):
    """An OS error while waiting for READY is reported only after the child is reaped."""

    process = mock.Mock()
    process.stdout = mock.Mock()
    process.poll.return_value = None
    process.communicate.return_value = (b"", b"cleanup diagnostic")
    if failing_protocol_operation == "readline":
        process.stdout.readline.side_effect = OSError("readline failed")

    guard = burn.SystemSleepGuard()
    with mock.patch.object(burn.subprocess, "Popen", return_value=process), mock.patch.object(
        burn.select,
        "select",
        side_effect=(
            OSError("select failed") if failing_protocol_operation == "select" else None
        ),
        return_value=([process.stdout], [], []),
    ), pytest.raises(
        burn.BurnError,
        match="{} failed.*cleanup diagnostic".format(failing_protocol_operation),
    ):
        guard.start()

    assert guard.process is None
    process.terminate.assert_called_once_with()
    process.communicate.assert_called_once_with(timeout=3)


def test_system_sleep_guard_rejects_helper_that_exits_immediately_after_ready():
    """READY is insufficient when the helper has already exited unexpectedly."""

    process = mock.Mock()
    process.stdout = io.BytesIO(b"READY\n")
    process.poll.return_value = 1
    process.communicate.return_value = (b"", b"assertion disappeared")

    with mock.patch.object(burn.subprocess, "Popen", return_value=process), mock.patch.object(
        burn.select, "select", return_value=([process.stdout], [], [])
    ), pytest.raises(burn.BurnError, match="exit code 1.*assertion disappeared"):
        burn.SystemSleepGuard().start()

    process.communicate.assert_called_once_with()


def test_system_sleep_guard_maps_sleep_immediately_after_ready_to_retryable_error():
    """Map post-READY SLEEP followed by exit code 75 to a retryable error."""

    process = mock.Mock()
    process.stdout = io.BytesIO(b"READY\n")
    process.poll.return_value = helper.SLEEP_EXIT_CODE
    process.communicate.return_value = (b"SLEEP\n", b"")

    with mock.patch.object(burn.subprocess, "Popen", return_value=process), mock.patch.object(
        burn.select, "select", return_value=([process.stdout], [], [])
    ), pytest.raises(burn.SystemSleepError):
        burn.SystemSleepGuard().start()

    process.communicate.assert_called_once_with()
    process.terminate.assert_not_called()


@pytest.mark.parametrize(
    ("returncode", "stdout", "error_type"),
    [
        (75, b"SLEEP\n", burn.SystemSleepError),
        (75, b"", burn.BurnError),
        (75, b"NOTSLEEP\n", burn.BurnError),
        (75, b"SLEEPING\n", burn.BurnError),
        (1, b"SLEEP\n", burn.BurnError),
        (1, b"", burn.BurnError),
    ],
)
def test_system_sleep_guard_maps_only_sleep_protocol_to_retryable_error(returncode, stdout, error_type):
    """Only a helper that emits SLEEP and exits with code 75 produces a retryable error."""

    process = mock.Mock()
    process.poll.return_value = returncode
    process.communicate.return_value = (stdout, b"helper diagnostic")
    guard = burn.SystemSleepGuard()
    guard.process = process

    with pytest.raises(error_type) as raised:
        guard.keep_alive()

    assert type(raised.value) is error_type
    assert guard.process is None


@pytest.mark.parametrize("failing_process_operation", ["poll", "communicate"])
def test_system_sleep_guard_maps_process_api_failures_to_burn_error(
    failing_process_operation,
):
    """Map poll or result-collection failures to non-retryable BurnError.

    Retain the process handle so later cleanup can reap it.
    """

    process = mock.Mock()
    if failing_process_operation == "poll":
        process.poll.side_effect = OSError("poll failed")
    else:
        process.poll.return_value = 1
        process.communicate.side_effect = OSError("communicate failed")
    guard = burn.SystemSleepGuard()
    guard.process = process

    with pytest.raises(burn.BurnError, match="macOS power guard") as raised:
        guard.keep_alive()

    assert type(raised.value) is burn.BurnError
    assert guard.process is process


def test_system_sleep_guard_keeps_process_for_cleanup_after_live_select_failure():
    """Treat a select failure while polling a live helper as non-retryable.

    Keep the process handle attached so later cleanup can terminate and reap
    the helper.
    """

    process = mock.Mock()
    process.stdout = io.BytesIO(b"")
    process.poll.return_value = None
    guard = burn.SystemSleepGuard()
    guard.process = process

    with mock.patch.object(
        burn.select, "select", side_effect=OSError("select failed")
    ), pytest.raises(burn.BurnError, match="power-guard protocol.*select failed") as raised:
        guard.keep_alive()

    assert type(raised.value) is burn.BurnError
    assert guard.process is process


def test_system_sleep_guard_reports_non_sleep_exit_after_live_protocol_poll():
    """Report non-retryable BurnError when a helper exits after an empty protocol poll.

    The helper emits no SLEEP event before its ordinary nonzero exit.
    """

    process = mock.Mock()
    process.stdout = io.BytesIO(b"")
    process.poll.side_effect = [None, 1]
    process.communicate.return_value = (b"", b"assertion vanished")
    guard = burn.SystemSleepGuard()
    guard.process = process

    with mock.patch.object(
        burn.select, "select", return_value=([], [], [])
    ) as wait_for_protocol, pytest.raises(
        burn.BurnError, match="exit code 1.*assertion vanished"
    ) as raised:
        guard.keep_alive()

    assert type(raised.value) is burn.BurnError
    wait_for_protocol.assert_called_once_with([process.stdout], [], [], 0.0)
    process.communicate.assert_called_once_with()
    assert guard.process is None


def test_system_sleep_guard_preserves_sleep_protocol_across_communication_failure_until_exit_75():
    """Retain a read SLEEP event across a result-collection failure.

    A later heartbeat converts it to SystemSleepError only after exit code 75.
    """

    process = mock.Mock()
    process.stdout = io.BytesIO(b"SLEEP\n")
    process.poll.return_value = None
    process.communicate.side_effect = OSError("communicate failed")
    guard = burn.SystemSleepGuard()
    guard.process = process

    with mock.patch.object(
        burn.select, "select", return_value=([process.stdout], [], [])
    ), pytest.raises(burn.BurnError, match="macOS power guard") as raised:
        guard.keep_alive()

    assert type(raised.value) is burn.BurnError
    assert guard.process is process

    process.poll.return_value = helper.SLEEP_EXIT_CODE
    process.communicate.side_effect = None
    process.communicate.return_value = (b"", b"")
    with pytest.raises(burn.SystemSleepError):
        guard.keep_alive()

    assert guard.process is None
    assert guard.protocol_stdout == b"SLEEP\n"


def test_system_sleep_guard_observes_sleep_protocol_before_helper_exit():
    """A live SLEEP line is retained until the helper supplies exit code 75."""

    process = mock.Mock()
    process.stdout = io.BytesIO(b"SLEEP\n")
    process.poll.side_effect = [None, helper.SLEEP_EXIT_CODE]
    process.communicate.return_value = (b"", b"")
    guard = burn.SystemSleepGuard()
    guard.process = process

    with mock.patch.object(
        burn.select, "select", return_value=([process.stdout], [], [])
    ), pytest.raises(burn.SystemSleepError):
        guard.keep_alive()

    process.communicate.assert_called_once_with(timeout=3)
    assert guard.process is None


def test_system_sleep_guard_rejects_sleep_protocol_if_helper_does_not_terminate():
    """A SLEEP line without timely helper termination remains non-retryable."""

    process = mock.Mock()
    process.stdout = io.BytesIO(b"SLEEP\n")
    process.poll.return_value = None
    process.communicate.side_effect = subprocess.TimeoutExpired("power guard", 3)
    guard = burn.SystemSleepGuard()
    guard.process = process

    with mock.patch.object(
        burn.select, "select", return_value=([process.stdout], [], [])
    ), pytest.raises(burn.BurnError, match="reported sleep but did not terminate") as raised:
        guard.keep_alive()

    assert type(raised.value) is burn.BurnError
    process.communicate.assert_called_once_with(timeout=3)
    assert guard.process is process


def test_system_sleep_guard_rejects_sleep_protocol_without_exit_code():
    """A collected SLEEP result without an exit code cannot authorize a retry."""

    process = mock.Mock()
    process.stdout = io.BytesIO(b"SLEEP\n")
    process.poll.side_effect = [None, None]
    process.communicate.return_value = (b"", b"")
    guard = burn.SystemSleepGuard()
    guard.process = process

    with mock.patch.object(
        burn.select, "select", return_value=([process.stdout], [], [])
    ), pytest.raises(burn.BurnError, match="did not report an exit code") as raised:
        guard.keep_alive()

    assert type(raised.value) is burn.BurnError
    process.communicate.assert_called_once_with(timeout=3)
    assert guard.process is process


def test_system_sleep_guard_commit_checks_for_sleep_before_marking_success():
    """Commit checks for a final sleep event before marking the card complete."""

    process = mock.Mock()
    process.poll.return_value = helper.SLEEP_EXIT_CODE
    process.communicate.return_value = (b"SLEEP\n", b"")
    guard = burn.SystemSleepGuard()
    guard.process = process

    with pytest.raises(burn.SystemSleepError):
        guard.commit()

    assert guard.committed is False
    assert guard.process is None


def test_system_sleep_guard_commit_short_circuits_future_heartbeats():
    """After commit, heartbeats stop polling the still-attached helper.

    Even a helper ready to return SLEEP and exit 75 is not collected and does
    not raise a retryable error.
    """

    process = mock.Mock()
    process.stdout = io.BytesIO(b"")
    process.poll.return_value = None
    guard = burn.SystemSleepGuard()
    guard.process = process

    with mock.patch.object(burn.select, "select", return_value=([], [], [])) as check_protocol:
        guard.commit()

    assert guard.committed is True
    assert process.poll.call_count == 2
    process.communicate.assert_not_called()
    check_protocol.assert_called_once_with([process.stdout], [], [], 0.0)

    process.reset_mock()
    process.poll.return_value = helper.SLEEP_EXIT_CODE
    process.communicate.return_value = (b"SLEEP\n", b"")
    guard.keep_alive()
    process.poll.assert_not_called()
    process.communicate.assert_not_called()
    assert guard.process is process


def test_prevent_system_sleep_ignores_helper_sleep_after_commit():
    """Context teardown cannot turn a helper sleep after the commit point into a retry."""

    process = mock.Mock()
    process.stdout = io.BytesIO(b"")
    process.poll.return_value = None
    guard = burn.SystemSleepGuard()
    guard.process = process
    guard.start = mock.Mock()
    session = burn.SudoSession()
    session.next_refresh = float("inf")

    with mock.patch.object(burn, "SystemSleepGuard", return_value=guard), mock.patch.object(
        burn.select, "select", return_value=([], [], [])
    ):
        with burn.prevent_system_sleep(session) as active_guard:
            active_guard.commit()
            process.reset_mock()
            process.poll.return_value = helper.SLEEP_EXIT_CODE
            process.communicate.return_value = (b"SLEEP\n", b"")

    process.terminate.assert_not_called()
    process.communicate.assert_called_once_with(timeout=3)
    assert guard.committed is True
    assert guard.process is None
    assert session._heartbeats == []


def test_system_sleep_guard_reaps_helper_when_startup_is_cancelled():
    """Cancellation while waiting for READY terminates and reaps the child."""

    process = mock.Mock()
    process.stdout = io.BytesIO(b"READY\n")
    process.poll.return_value = None
    process.communicate.return_value = (b"", b"")
    with mock.patch.object(burn.subprocess, "Popen", return_value=process), mock.patch.object(
        burn.select, "select", side_effect=KeyboardInterrupt("cancel startup")
    ), pytest.raises(KeyboardInterrupt, match="cancel startup"):
        burn.SystemSleepGuard().start()

    process.terminate.assert_called_once_with()
    process.communicate.assert_called_once_with(timeout=3)


def test_system_sleep_guard_reaps_helper_when_cancelled_immediately_after_spawn():
    """Cancellation immediately after spawning still terminates and reaps the child."""

    class InterruptOnStdout:
        def __init__(self):
            self.terminate = mock.Mock()
            self.communicate = mock.Mock(return_value=(b"", b""))
            self.poll = mock.Mock(return_value=None)

        @property
        def stdout(self):
            raise KeyboardInterrupt("cancel after spawn")

    process = InterruptOnStdout()
    with mock.patch.object(burn.subprocess, "Popen", return_value=process), pytest.raises(
        KeyboardInterrupt, match="cancel after spawn"
    ):
        burn.SystemSleepGuard().start()

    process.terminate.assert_called_once_with()
    process.communicate.assert_called_once_with(timeout=3)


@pytest.mark.parametrize("body_fails", [False, True], ids=["success", "failure"])
def test_prevent_system_sleep_runs_and_detaches_guard_heartbeat(body_fails):
    """Invoke the guard heartbeat on entry, through SudoSession, and on exit, then detach it."""

    session = burn.SudoSession()
    session.next_refresh = float("inf")
    guard = mock.Mock()
    guard.committed = False
    guard.stop.return_value = ""
    body_error = burn.BurnError("body failed")
    with mock.patch.object(burn, "SystemSleepGuard", return_value=guard):
        if body_fails:
            with pytest.raises(burn.BurnError) as raised:
                with burn.prevent_system_sleep(session):
                    session.keep_alive()
                    raise body_error
            assert raised.value is body_error
        else:
            with burn.prevent_system_sleep(session):
                session.keep_alive()

    guard.start.assert_called_once_with()
    guard.stop.assert_called_once_with()
    assert session._heartbeats == []
    expected_heartbeat_calls = (
        [mock.call(), mock.call(), mock.call(protocol_timeout=burn.POWER_GUARD_SLEEP_GRACE_TIMEOUT)]
        if body_fails
        else [mock.call(), mock.call(), mock.call()]
    )
    assert guard.keep_alive.call_args_list == expected_heartbeat_calls


@pytest.mark.parametrize(
    "heartbeat_error",
    [burn.SystemSleepError("forced sleep"), burn.BurnError("power helper failed")],
    ids=["sleep", "ordinary-helper-failure"],
)
def test_prevent_system_sleep_propagates_guard_failure_and_detaches_heartbeat(heartbeat_error):
    """Preserve the exact error from the guard heartbeat attached to SudoSession.

    Detach that callback from the session during context cleanup.
    """

    session = burn.SudoSession()
    session.next_refresh = float("inf")
    guard = mock.Mock(spec=burn.SystemSleepGuard)
    guard.stop.return_value = ""
    if isinstance(heartbeat_error, burn.SystemSleepError):
        guard.keep_alive.side_effect = [None, heartbeat_error]
        expected_heartbeat_calls = [mock.call(), mock.call()]
    else:
        guard.keep_alive.side_effect = [None, heartbeat_error, None]
        expected_heartbeat_calls = [
            mock.call(),
            mock.call(),
            mock.call(protocol_timeout=burn.POWER_GUARD_SLEEP_GRACE_TIMEOUT),
        ]

    with mock.patch.object(burn, "SystemSleepGuard", return_value=guard), pytest.raises(
        type(heartbeat_error)
    ) as raised:
        with burn.prevent_system_sleep(session):
            session.keep_alive()

    assert raised.value is heartbeat_error
    assert guard.keep_alive.call_args_list == expected_heartbeat_calls
    guard.stop.assert_called_once_with()
    assert session._heartbeats == []


def test_power_guard_cleanup_escalates_shutdown_without_masking_primary_error(capsys):
    """Verify direct cleanup kills a stuck helper.

    Separately, verify that teardown diagnostics do not replace a body error.
    """

    process = mock.Mock()
    process.poll.return_value = None
    process.communicate.side_effect = [subprocess.TimeoutExpired("power guard", 3), (b"", b"killed")]
    stuck_guard = burn.SystemSleepGuard()
    stuck_guard.process = process
    assert stuck_guard.stop() == "killed"
    process.terminate.assert_called_once_with()
    process.kill.assert_called_once_with()
    assert process.communicate.call_args_list == [mock.call(timeout=3), mock.call(timeout=3)]

    body_error = burn.BurnError("primary")
    context_guard = mock.Mock(spec=burn.SystemSleepGuard)
    context_guard.stop.return_value = "secondary cleanup"
    with mock.patch.object(burn, "SystemSleepGuard", return_value=context_guard), pytest.raises(
        burn.BurnError
    ) as raised:
        with burn.prevent_system_sleep(burn.SudoSession()):
            raise body_error

    assert raised.value is body_error
    assert "secondary cleanup" in capsys.readouterr().err


def test_system_sleep_guard_stop_returns_cleanup_failure_without_raising():
    """Return a post-kill communication failure as a cleanup diagnostic.

    After collection fails, a bounded fallback wait still reaps the helper
    without allowing the cleanup error to escape.
    """

    process = mock.Mock()
    process.poll.return_value = None
    process.communicate.side_effect = [
        subprocess.TimeoutExpired("power guard", 3),
        OSError("communicate failed"),
    ]
    process.wait.return_value = helper.SLEEP_EXIT_CODE
    guard = burn.SystemSleepGuard()
    guard.process = process

    cleanup_diagnostic = guard.stop()

    assert "communicate failed" in cleanup_diagnostic
    process.terminate.assert_called_once_with()
    process.kill.assert_called_once_with()
    process.wait.assert_called_once_with(timeout=3)
    assert guard.process is None


def test_prevent_system_sleep_prioritizes_sleep_over_derived_io_error(capsys):
    """A confirmed forced sleep supersedes the I/O error caused by that sleep."""

    io_error = burn.BurnError("disk disappeared")
    sleep_error = burn.SystemSleepError("forced sleep")
    guard = mock.Mock(spec=burn.SystemSleepGuard)
    guard.keep_alive.side_effect = [None, sleep_error]
    guard.stop.return_value = ""
    session = burn.SudoSession()
    with mock.patch.object(burn, "SystemSleepGuard", return_value=guard), pytest.raises(
        burn.SystemSleepError
    ) as raised:
        with burn.prevent_system_sleep(session):
            raise io_error

    assert raised.value is sleep_error
    assert raised.value.__cause__ is io_error
    guard.stop.assert_called_once_with()
    assert session._heartbeats == []
    assert capsys.readouterr().err == ""


def test_prevent_system_sleep_preserves_body_error_when_grace_check_reports_non_sleep_failure():
    """Keep the body error when the final grace-period heartbeat reports non-sleep failure."""

    body_error = burn.BurnError("disk disappeared")
    helper_error = burn.BurnError("power helper stopped unexpectedly")
    guard = mock.Mock(spec=burn.SystemSleepGuard)
    guard.keep_alive.side_effect = [None, helper_error]
    guard.stop.return_value = ""
    session = burn.SudoSession()

    with mock.patch.object(burn, "SystemSleepGuard", return_value=guard), pytest.raises(
        burn.BurnError
    ) as raised:
        with burn.prevent_system_sleep(session):
            raise body_error

    assert raised.value is body_error
    assert guard.keep_alive.call_args_list == [
        mock.call(),
        mock.call(protocol_timeout=burn.POWER_GUARD_SLEEP_GRACE_TIMEOUT),
    ]
    guard.stop.assert_called_once_with()
    assert session._heartbeats == []


def test_prevent_system_sleep_gives_delayed_sleep_protocol_priority_over_io_error():
    """Wait briefly for delayed SLEEP proof and let confirmed sleep supersede the I/O failure."""

    io_error = burn.BurnError("disk disappeared")
    process = mock.Mock()
    process.stdout = io.BytesIO(b"SLEEP\n")
    process.poll.side_effect = [None, None, None, helper.SLEEP_EXIT_CODE]
    process.communicate.return_value = (b"", b"")
    guard = burn.SystemSleepGuard()
    guard.process = process
    guard.start = mock.Mock()

    with mock.patch.object(burn, "SystemSleepGuard", return_value=guard), mock.patch.object(
        burn.select,
        "select",
        side_effect=[([], [], []), ([process.stdout], [], [])],
    ) as wait_for_protocol, pytest.raises(burn.SystemSleepError) as raised:
        with burn.prevent_system_sleep(burn.SudoSession()):
            raise io_error

    assert raised.value.__cause__ is io_error
    assert [select_call.args[3] for select_call in wait_for_protocol.call_args_list] == [
        0,
        burn.POWER_GUARD_SLEEP_GRACE_TIMEOUT,
    ]
    assert guard.process is None


def test_prevent_system_sleep_never_turns_ctrl_c_into_an_automatic_retry():
    """Ctrl+C exits directly and is never converted into a sleep retry."""

    cancellation = KeyboardInterrupt("cancelled")
    guard = mock.Mock(spec=burn.SystemSleepGuard)
    guard.stop.return_value = ""
    session = burn.SudoSession()
    with mock.patch.object(burn, "SystemSleepGuard", return_value=guard), pytest.raises(
        KeyboardInterrupt
    ) as raised:
        with burn.prevent_system_sleep(session):
            raise cancellation

    assert raised.value is cancellation
    guard.keep_alive.assert_called_once_with()
    guard.stop.assert_called_once_with()
    assert session._heartbeats == []
