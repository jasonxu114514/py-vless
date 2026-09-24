"""VLESS-over-WebSocket data plane.

This is the only module that touches JS interop.

The optimisation that matters here is **pass-through**: once the VLESS header has
been parsed, payload chunks travel JS-to-JS without ever being materialised as
Python objects.

    upload    ws ArrayBuffer ──────────────────────────────► socket.write()
                              (Python only inspects the header)
    download  socket chunk ──┬─ first chunk: Python prefixes the 2-byte header
                             └─ thereafter: straight to ws.send()

Each WS frame otherwise costs a JS->Python->JS round trip; with 16 KiB frames
that dominates. Measured on the Free-plan-hostile profile in the README.

Platform constraints learned by experiment (see README for the full list):
  * Return the 101 BEFORE awaiting anything TCP, or the runtime kills the
    request as "hung and would never generate a response".
  * `onmessage` is a JS callback and cannot await: push into an asyncio.Queue.
  * `JsProxy` is unhashable -- keep proxies alive in a list, never a set.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from urllib.parse import urlsplit

import js
from js import WebSocketPair
from workers import Response, import_from_javascript, wait_until

from vless import (
    CMD_TCP,
    HeaderIncomplete,
    VlessError,
    base64_to_bytes,
    parse_header,
)

# Resolved once per isolate rather than once per connection.
_SOCKETS = import_from_javascript("cloudflare:sockets")
_UINT8 = js.Uint8Array
_UNDEFINED = js.undefined

_pending: list = []


@contextmanager
def js_view(pybytes):
    """Zero-copy Uint8Array view over Python bytes, valid inside the block."""
    from pyodide.ffi import create_proxy

    px = create_proxy(pybytes)
    buf = px.getBuffer()
    px.destroy()
    try:
        yield buf.data
    finally:
        buf.release()


def to_bytes(value) -> bytes:
    """Normalise a JS binary chunk (ArrayBuffer or view) to Python bytes."""
    if value is None or value is _UNDEFINED:
        return b""
    try:
        return value.to_bytes()
    except AttributeError:
        return _UINT8.new(value).to_bytes()


def _prepend(prefix: bytes, js_chunk):
    """prefix + js_chunk as one Uint8Array, one copy. JS-side, no Python payload."""
    out = _UINT8.new(js_chunk.byteLength + len(prefix))
    out.set(_UINT8.new(prefix), 0)
    out.set(_UINT8.new(js_chunk), len(prefix))
    return out


class Session:
    """One VLESS connection: WS on one side, TCP on the other."""

    def __init__(self, server, queue, uuid: str):
        self.server = server
        self.queue = queue
        self.uuid = uuid
        self.sock = None
        self.writer = None
        # Strong refs to JS proxies/callbacks: Pyodide proxies are unhashable and
        # would otherwise be collected while the pipeTo is still live.
        self._keepalive: list = []

    async def run(self):
        try:
            await self._serve()
        except Exception as e:  # noqa: BLE001
            print(f"SESSION_ERROR {type(e).__name__}: {e}")
            try:
                self.server.close(1011, "proxy error")
            except Exception:
                pass
        finally:
            await self._shutdown()

    async def _serve(self):
        header = await self._read_header()

        if header.uuid != self.uuid:
            print(f"SESSION_REJECT bad uuid {header.uuid}")
            self.server.close(1008, "invalid uuid")
            return
        if header.command != CMD_TCP:
            print(f"SESSION_REJECT command {header.command} (only TCP is supported)")
            self.server.close(1003, "only TCP is supported")
            return

        # Everything after the header in the first frame(s) is already payload.
        leftover = self._buffer[header.raw_data_index :]
        del self._buffer

        self.sock = _SOCKETS.connect({"hostname": header.host, "port": header.port})
        await self.sock.opened
        self.writer = self.sock.writable.getWriter()

        if leftover:
            with js_view(leftover) as payload:
                await self.writer.write(payload)

        self._start_download(header.response_header)
        await self._pump_upload()

    # -- upload: ws -> socket ------------------------------------------------

    async def _pump_upload(self):
        """Forward WS frames to the socket, bypassing Python for the payload.

        Frames already sitting in the queue are merged into a single write: each
        `writer.write` is an FFI crossing, so fewer/larger writes is the main
        lever available on the upload path.
        """
        while True:
            chunk = await self.queue.get()
            if chunk is None:
                return
            if not self.queue.empty():
                chunk = self._merge_queued(chunk)
            await self.writer.write(chunk)

    def _merge_queued(self, first):
        """Merge `first` with everything already queued, in JS, one copy total."""
        chunks = [first]
        while not self.queue.empty():
            nxt = self.queue.get_nowait()
            if nxt is None:
                # Preserve the close marker for the next loop iteration.
                self.queue.put_nowait(None)
                break
            chunks.append(nxt)
        if len(chunks) == 1:
            return first
        total = sum(c.byteLength for c in chunks)
        merged = _UINT8.new(total)
        offset = 0
        for c in chunks:
            # Uint8Array.new(arrayBuffer) is a view (no copy); .set copies once.
            view = _UINT8.new(c) if isinstance(c, _UINT8) else None
            merged.set(view if view is not None else _UINT8.new(c), offset)
            offset += c.byteLength
        return merged

    # -- download: socket -> ws ---------------------------------------------

    def _start_download(self, response_header: bytes):
        """Pump socket -> ws with a JS `pipeTo` and one Python callback per chunk.

        Measured ~3x cheaper in CPU than driving the same loop from a Python
        coroutine (122 ms/MiB vs 361 ms/MiB on a 1 MiB download): the JS runtime
        handles the promise plumbing and backpressure, so Python is entered once
        per chunk instead of once per `await`.
        """
        from pyodide.ffi import create_proxy, to_js

        state = {"first": True}

        def on_chunk(chunk, controller):
            try:
                if state["first"]:
                    # Only the first frame carries the VLESS response header.
                    self.server.send(_prepend(response_header, chunk))
                    state["first"] = False
                else:
                    self.server.send(chunk)
            except Exception as e:  # noqa: BLE001
                print("download send failed: " + repr(e))
            return None

        def on_end(controller):
            try:
                self.server.close(1000, "remote closed")
            except Exception:
                pass
            return None

        p_write = create_proxy(on_chunk)
        p_close = create_proxy(on_end)
        sink = to_js(
            {"write": p_write, "close": p_close}, dict_converter=js.Object.fromEntries
        )

        def on_error(_err):
            try:
                self.server.close(1011, "remote error")
            except Exception:
                pass

        self._keepalive.extend([p_write, p_close, sink])
        try:
            self.sock.readable.pipeTo(js.WritableStream.new(sink)).catch(
                create_proxy(on_error)
            )
        except Exception as e:  # noqa: BLE001
            print("pipeTo failed: " + repr(e))

    # -- header --------------------------------------------------------------

    _buffer = b""

    async def _read_header(self):
        buf = b""
        while True:
            chunk = await self.queue.get()
            if chunk is None:
                raise VlessError("connection closed before the VLESS header arrived")
            buf += to_bytes(chunk)
            try:
                header = parse_header(buf)
            except HeaderIncomplete:
                if len(buf) > 4096:
                    raise VlessError("VLESS header absurdly large") from None
                continue
            self._buffer = buf
            return header

    async def _shutdown(self):
        for closer in (self.writer, self.sock):
            if closer is None:
                continue
            try:
                closer.releaseLock() if closer is self.writer else closer.close()
            except Exception:
                pass


async def vless_fetch(self, request, cfg):
    """Handle a WebSocket upgrade request."""
    upgrade = request.headers.get("Upgrade") or ""
    if upgrade.lower() != "websocket":
        return Response("expected Upgrade: websocket", status=426)

    if cfg.path_is_enforced:
        # Compare the pathname only; the query carries the early-data hint.
        # request.url is the full URL, so parse it rather than string-splitting.
        if urlsplit(request.url).path != urlsplit(cfg.path).path:
            return Response("not found", status=404)

    client, server = WebSocketPair.new().object_values()
    server.accept()
    # Without this, binary frames arrive as Blob under wrangler dev.
    server.binaryType = "arraybuffer"

    queue = asyncio.Queue()
    from pyodide.ffi import create_proxy

    proxies = []

    def on_message(evt):
        try:
            queue.put_nowait(evt.data)
        except Exception as e:  # noqa: BLE001
            print("onmessage error: " + repr(e))

    def on_close(evt):
        try:
            queue.put_nowait(None)
        except Exception:
            pass

    p_msg = create_proxy(on_message)
    p_close = create_proxy(on_close)
    proxies.extend([p_msg, p_close])
    server.onmessage = p_msg
    server.onclose = p_close

    # 0-RTT early data: a client may put the VLESS header in the subprotocol
    # header. Enqueue it first so it is always the first thing parsed.
    early = request.headers.get("Sec-WebSocket-Protocol") or ""
    if early:
        try:
            data = base64_to_bytes(early)
        except VlessError as e:
            print(f"bad early data: {e}")
            data = None
        if data:
            with js_view(data) as view:
                queue.put_nowait(view.slice())

    session = Session(server, queue, cfg.uuid)
    task = asyncio.ensure_future(session.run())
    task_proxy = create_proxy(task)
    # JsProxy is unhashable, so strong refs live in a list (keeps them alive).
    _pending.append((task, task_proxy, proxies))

    def _done(_f):
        for i, (t, _p, _x) in enumerate(_pending):
            if t is task:
                _pending.pop(i)
                break

    task.add_done_callback(_done)
    wait_until(task_proxy)

    return Response(None, status=101, web_socket=client)
