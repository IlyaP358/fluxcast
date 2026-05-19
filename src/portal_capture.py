import asyncio
import os
import secrets
import threading
from dataclasses import dataclass
from typing import Any, Optional

try:
    from dbus_next import Message, MessageType, Variant
    from dbus_next.aio import MessageBus
    from dbus_next.constants import BusType
except ImportError:
    Message = None
    MessageType = None
    Variant = None
    MessageBus = None
    BusType = None


PORTAL_DEST = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
PORTAL_IFACE = "org.freedesktop.portal.ScreenCast"
REQUEST_IFACE = "org.freedesktop.portal.Request"
SESSION_IFACE = "org.freedesktop.portal.Session"


class PortalCaptureError(RuntimeError):
    pass


@dataclass
class PortalCaptureSession:
    session_handle: str
    pw_node_id: int
    pw_fd: int
    restore_token: Optional[str] = None
    source_type: Optional[int] = None
    position: Optional[tuple[int, int]] = None
    size: Optional[tuple[int, int]] = None
    stream_label: str = ""
    runtime: Optional["_PortalRuntime"] = None
    bus: Any = None


class _PortalRuntime:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def run(self, coro, timeout: Optional[float] = None):
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result(timeout=timeout)

    def call_soon(self, callback, *args) -> None:
        self.loop.call_soon_threadsafe(callback, *args)

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=2.0)


def _require_dbus_next() -> None:
    if MessageBus is None or Message is None or Variant is None:
        raise PortalCaptureError(
            "Portal capture requires Python package 'dbus-next'. "
            "Install dependencies with: pip install -r requirements.txt"
        )


def _sender_path_component(unique_name: str) -> str:
    if unique_name.startswith(":"):
        unique_name = unique_name[1:]
    return unique_name.replace(".", "_")


def _request_path(unique_name: str, token: str) -> str:
    sender = _sender_path_component(unique_name)
    return f"/org/freedesktop/portal/desktop/request/{sender}/{token}"


def _as_string(value: Any) -> str:
    if hasattr(value, "value"):
        value = value.value
    return str(value)


def _unwrap(value: Any) -> Any:
    while hasattr(value, "value"):
        value = value.value
    return value


def _stream_entry_payload(entry: Any) -> tuple[Optional[int], dict[str, Any]]:
    """
    Parse one ScreenCast stream entry.
    Expected shape is usually struct: (u node_id, a{sv} props).
    """
    entry = _unwrap(entry)
    props: dict[str, Any] = {}

    if isinstance(entry, (list, tuple)) and entry:
        first = _unwrap(entry[0])
        if len(entry) > 1 and isinstance(_unwrap(entry[1]), dict):
            props = {str(key): _unwrap(val) for key, val in _unwrap(entry[1]).items()}
        if isinstance(first, int) and not isinstance(first, bool) and first > 0:
            return int(first), props

    if isinstance(entry, dict):
        props = {str(key): _unwrap(val) for key, val in entry.items()}
        for key in ("node_id", "pw_node_id"):
            value = _unwrap(entry.get(key))
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                return int(value), props
    return None, props


def _as_int_pair(value: Any) -> Optional[tuple[int, int]]:
    value = _unwrap(value)
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        first = _unwrap(value[0])
        second = _unwrap(value[1])
        if isinstance(first, int) and isinstance(second, int):
            return int(first), int(second)
    return None


def _monitor_match_score(
    props: dict[str, Any],
    preferred_position: Optional[tuple[int, int]],
    preferred_size: Optional[tuple[int, int]],
) -> int:
    score = 0
    position = _as_int_pair(props.get("position"))
    size = _as_int_pair(props.get("size"))
    source_type = _unwrap(props.get("source_type"))

    if isinstance(source_type, int) and source_type == 1:
        score += 50
    # Larger areas are usually real monitors, while camera-like sources are smaller.
    if size:
        score += max(0, (size[0] * size[1]) // 200000)
    if preferred_position and position == preferred_position:
        score += 10
    if preferred_size and size == preferred_size:
        score += 20
    return score


async def _portal_call_with_response(
    bus: Any,
    member: str,
    signature: str,
    body: list[Any],
    timeout: float,
) -> dict[str, Any]:
    token = "fluxcast_" + secrets.token_hex(6)
    options = dict(body[-1])
    options["handle_token"] = Variant("s", token)
    body = [*body[:-1], options]
    request_path = _request_path(bus.unique_name, token)

    loop = asyncio.get_running_loop()
    done: asyncio.Future[tuple[int, dict[str, Any]]] = loop.create_future()

    def _handler(msg) -> None:
        if msg.message_type != MessageType.SIGNAL:
            return
        if msg.path != request_path:
            return
        if msg.interface != REQUEST_IFACE or msg.member != "Response":
            return
        if len(msg.body) < 2:
            if not done.done():
                done.set_exception(PortalCaptureError(f"{member} returned malformed portal response."))
            return
        response_code = int(msg.body[0])
        results = msg.body[1] if isinstance(msg.body[1], dict) else {}
        if not done.done():
            done.set_result((response_code, results))

    bus.add_message_handler(_handler)
    try:
        reply = await bus.call(
            Message(
                destination=PORTAL_DEST,
                path=PORTAL_PATH,
                interface=PORTAL_IFACE,
                member=member,
                signature=signature,
                body=body,
            )
        )
        if reply.message_type == MessageType.ERROR:
            raise PortalCaptureError(
                f"{member} D-Bus call failed: {reply.error_name or 'unknown'} "
                f"{reply.body[0] if reply.body else ''}".strip()
            )

        response_code, results = await asyncio.wait_for(done, timeout=timeout)
        if response_code != 0:
            raise PortalCaptureError(f"{member} portal request was rejected (code={response_code}).")
        return results
    finally:
        bus.remove_message_handler(_handler)


def _extract_stream_node(
    start_results: dict[str, Any],
    preferred_position: Optional[tuple[int, int]] = None,
    preferred_size: Optional[tuple[int, int]] = None,
) -> tuple[int, Optional[int], Optional[tuple[int, int]], Optional[tuple[int, int]], str]:
    streams_var = start_results.get("streams")
    streams = _unwrap(streams_var)
    if not isinstance(streams, list) or not streams:
        raise PortalCaptureError("Portal did not return ScreenCast streams.")
    candidates: list[tuple[int, dict[str, Any], int]] = []
    for entry in streams:
        node_id, props = _stream_entry_payload(entry)
        if node_id is None:
            continue
        score = _monitor_match_score(props, preferred_position, preferred_size)
        candidates.append((node_id, props, score))

    if not candidates:
        # Keep payload preview for compositor-specific layout debugging.
        preview = repr(streams)
        if len(preview) > 220:
            preview = preview[:220] + "..."
        raise PortalCaptureError(f"Portal returned malformed stream metadata: {preview}")

    monitor_candidates = []
    for candidate in candidates:
        source_type = _unwrap(candidate[1].get("source_type"))
        if isinstance(source_type, int) and source_type == 1:
            monitor_candidates.append(candidate)

    pool = monitor_candidates if monitor_candidates else candidates
    best = max(pool, key=lambda item: item[2])
    node_id, props, _ = best
    source_type = _unwrap(props.get("source_type"))
    source_type_int = int(source_type) if isinstance(source_type, int) else None
    position = _as_int_pair(props.get("position"))
    size = _as_int_pair(props.get("size"))
    label = str(_unwrap(props.get("id")) or _unwrap(props.get("mapping_id")) or "")
    return node_id, source_type_int, position, size, label


async def _connect_portal_bus() -> Any:
    _require_dbus_next()
    return await MessageBus(bus_type=BusType.SESSION, negotiate_unix_fd=True).connect()


async def _start_portal_capture_async(
    bus: Any,
    timeout: float,
    preferred_position: Optional[tuple[int, int]] = None,
    preferred_size: Optional[tuple[int, int]] = None,
) -> PortalCaptureSession:
    create_opts = {
        "session_handle_token": Variant("s", "fluxcast_session_" + secrets.token_hex(4)),
    }
    create_results = await _portal_call_with_response(
        bus,
        member="CreateSession",
        signature="a{sv}",
        body=[create_opts],
        timeout=timeout,
    )
    session_handle_var = create_results.get("session_handle")
    if session_handle_var is None:
        raise PortalCaptureError("Portal did not return session_handle.")
    session_handle = _as_string(session_handle_var)

    select_opts = {
        # 1: Monitor (full screen). Let the portal UI pick the monitor.
        "types": Variant("u", 1),
        "multiple": Variant("b", False),
        # 2: Embedded cursor.
        "cursor_mode": Variant("u", 2),
        "persist_mode": Variant("u", 0),
    }
    await _portal_call_with_response(
        bus,
        member="SelectSources",
        signature="oa{sv}",
        body=[session_handle, select_opts],
        timeout=timeout,
    )

    start_results = await _portal_call_with_response(
        bus,
        member="Start",
        signature="osa{sv}",
        body=[session_handle, "", {}],
        timeout=timeout,
    )
    node_id, source_type, position, size, label = _extract_stream_node(
        start_results,
        preferred_position=preferred_position,
        preferred_size=preferred_size,
    )
    restore_token_var = start_results.get("restore_token")
    restore_token = _as_string(restore_token_var) if restore_token_var is not None else None

    try:
        remote_reply = await bus.call(
            Message(
                destination=PORTAL_DEST,
                path=PORTAL_PATH,
                interface=PORTAL_IFACE,
                member="OpenPipeWireRemote",
                signature="oa{sv}",
                body=[session_handle, {}],
            )
        )
    except EOFError as exc:
        raise PortalCaptureError(
            "OpenPipeWireRemote failed: D-Bus connection closed while receiving PipeWire FD."
        ) from exc
    if remote_reply.message_type == MessageType.ERROR:
        raise PortalCaptureError(
            "OpenPipeWireRemote failed: "
            f"{remote_reply.error_name or 'unknown'} "
            f"{remote_reply.body[0] if remote_reply.body else ''}".strip()
        )
    if not remote_reply.body:
        raise PortalCaptureError("OpenPipeWireRemote returned no file descriptor handle.")

    handle_index = int(remote_reply.body[0])
    if not remote_reply.unix_fds or handle_index < 0 or handle_index >= len(remote_reply.unix_fds):
        raise PortalCaptureError("OpenPipeWireRemote did not include a usable PipeWire FD.")
    pw_fd = remote_reply.unix_fds[handle_index]
    return PortalCaptureSession(
        session_handle=session_handle,
        pw_node_id=node_id,
        pw_fd=pw_fd,
        restore_token=restore_token,
        source_type=source_type,
        position=position,
        size=size,
        stream_label=label,
    )


def start_portal_capture(
    timeout: float = 120.0,
    preferred_position: Optional[tuple[int, int]] = None,
    preferred_size: Optional[tuple[int, int]] = None,
) -> PortalCaptureSession:
    if (os.environ.get("XDG_SESSION_TYPE") or "").lower() != "wayland" and not os.environ.get("WAYLAND_DISPLAY"):
        raise PortalCaptureError("Portal backend requires an active Wayland session.")
    runtime = _PortalRuntime()
    bus = None
    try:
        bus = runtime.run(_connect_portal_bus(), timeout=max(5.0, min(timeout, 30.0)))
        session = runtime.run(
            _start_portal_capture_async(
                bus,
                timeout=timeout,
                preferred_position=preferred_position,
                preferred_size=preferred_size,
            ),
            timeout=timeout + 5.0,
        )
        session.runtime = runtime
        session.bus = bus
        return session
    except PortalCaptureError:
        if bus is not None:
            try:
                runtime.call_soon(bus.disconnect)
            except Exception:
                pass
        runtime.stop()
        raise
    except EOFError as exc:
        if bus is not None:
            try:
                runtime.call_soon(bus.disconnect)
            except Exception:
                pass
        runtime.stop()
        raise PortalCaptureError(
            "Portal D-Bus stream closed unexpectedly while opening PipeWire remote."
        ) from exc
    except Exception as exc:
        if bus is not None:
            try:
                runtime.call_soon(bus.disconnect)
            except Exception:
                pass
        runtime.stop()
        raise PortalCaptureError(f"Portal capture setup failed: {exc}") from exc


async def _close_portal_capture_async(session_handle: str, bus: Optional[Any] = None) -> None:
    _require_dbus_next()
    own_bus = bus is None
    if own_bus:
        bus = await MessageBus(bus_type=BusType.SESSION).connect()
    assert bus is not None
    try:
        reply = await bus.call(
            Message(
                destination=PORTAL_DEST,
                path=session_handle,
                interface=SESSION_IFACE,
                member="Close",
            )
        )
        if reply.message_type == MessageType.ERROR:
            raise PortalCaptureError(
                f"Portal session close failed: {reply.error_name or 'unknown'}"
            )
    finally:
        if own_bus:
            bus.disconnect()


def close_portal_capture(session: Optional[PortalCaptureSession]) -> None:
    if session is None:
        return
    if session.runtime is not None and session.bus is not None:
        try:
            session.runtime.run(
                _close_portal_capture_async(session.session_handle, session.bus),
                timeout=5.0,
            )
        except Exception:
            pass
        try:
            session.runtime.call_soon(session.bus.disconnect)
        except Exception:
            pass
        session.runtime.stop()
    else:
        try:
            asyncio.run(_close_portal_capture_async(session.session_handle))
        except Exception:
            pass
    try:
        os.close(session.pw_fd)
    except OSError:
        pass
