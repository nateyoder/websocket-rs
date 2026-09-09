"""Native transport contracts needed by maintained reconnecting clients."""

import asyncio
import base64
import gc
import hashlib
import socket
import weakref

import pytest

from websocket_rs.native_client import connect as async_connect


@pytest.mark.parametrize("loop_kind", ["asyncio", "uvloop"])
@pytest.mark.parametrize("connect_timeout", [None, 10])
def test_pre_upgrade_connection_refusal_has_no_unhandled_future(loop_kind, connect_timeout):
    loop_factory = asyncio.new_event_loop
    if loop_kind == "uvloop":
        loop_factory = pytest.importorskip("uvloop").new_event_loop

    async def run():
        reports = []
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda _loop, context: reports.append(context))
        try:
            with socket.socket() as reserved:
                reserved.bind(("127.0.0.1", 0))
                port = reserved.getsockname()[1]
            with pytest.raises(ConnectionRefusedError):
                await async_connect(f"ws://127.0.0.1:{port}", connect_timeout=connect_timeout)
            await asyncio.sleep(0)
            gc.collect()
            await asyncio.sleep(0)
            assert reports == []
        finally:
            loop.set_exception_handler(None)

    with asyncio.Runner(loop_factory=loop_factory) as runner:
        runner.run(run())


@pytest.mark.parametrize("fault", ["status", "upgrade", "connection", "accept", "duplicate"])
def test_rejects_invalid_upgrade_and_preserves_http_status(fault):
    async def run():
        async def serve(reader, writer):
            request = await reader.readuntil(b"\r\n\r\n")
            key = next(
                line.split(b":", 1)[1].strip()
                for line in request.split(b"\r\n")
                if line.lower().startswith(b"sec-websocket-key:")
            )
            accept = base64.b64encode(hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest()).decode()
            status = 429 if fault == "status" else 101
            headers = [
                f"HTTP/1.1 {status} Response",
                "Upgrade: " + ("http" if fault == "upgrade" else "websocket"),
                "Connection: " + ("keep-alive" if fault == "connection" else "Upgrade"),
                "Sec-WebSocket-Accept: " + ("prefix" if fault == "accept" else "") + accept,
            ]
            if fault == "duplicate":
                headers.append("Sec-WebSocket-Accept: " + accept)
            writer.write(("\r\n".join(headers) + "\r\n\r\n").encode())
            await writer.drain()
            await asyncio.wait_for(reader.read(), 2)
            writer.close()
            await writer.wait_closed()

        async with await asyncio.start_server(serve, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            with pytest.raises(ConnectionError) as caught:
                await async_connect(f"ws://127.0.0.1:{port}", connect_timeout=1)
            assert caught.value.status_code == (429 if fault == "status" else 101)

    asyncio.run(run())


@pytest.mark.parametrize("path", ["local", "lost", "peer"])
def test_closed_client_releases_loop_and_callback_before_foreign_gc(path):
    clients = []

    class Callback:
        def __call__(self, message):
            pass

    async def run():
        import websockets

        abort = asyncio.Event()

        async def serve(ws):
            if path == "lost":
                await abort.wait()
                ws.transport.abort()
            await ws.wait_closed()

        async with websockets.serve(serve, "127.0.0.1", 0) as server:
            callback = Callback()
            ws = await async_connect(f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}", on_message=callback)
            callback_ref = weakref.ref(callback)
            del callback
            if path == "lost":
                abort.set()
                async with asyncio.timeout(2):
                    while not ws.closed:
                        await asyncio.sleep(0.001)
            elif path == "peer":
                ws.data_received(b"\x88\x05\x03\xe9bye")
                assert ws.close_received_code == 1001
                assert ws.close_received_reason == "bye"
                assert ws.close_sent_code is None
            ws.close()
            assert callback_ref() is None
            if path == "lost":
                assert ws.close_received_code is None
                assert ws.close_sent_code is None
            clients.append(ws)

    loop = asyncio.new_event_loop()
    loop_ref = weakref.ref(loop)
    try:
        loop.run_until_complete(run())
    finally:
        loop.close()
    del loop
    gc.collect()
    assert loop_ref() is None
    # NativeClient is explicitly unsendable: dispose it on its owning thread.
    clients.clear()
    import threading

    thread = threading.Thread(target=gc.collect)
    thread.start()
    thread.join()


def test_protocol_failure_records_sent_code_only():
    async def run():
        import websockets

        async def serve(ws):
            await ws.wait_closed()

        async with websockets.serve(serve, "127.0.0.1", 0) as server:
            ws = await async_connect(f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}")
            ws.data_received(b"\x80\x03bad")
            assert ws.closed
            assert ws.close_received_code is None
            assert ws.close_received_reason is None
            assert ws.close_sent_code == 1002
            assert ws.close_sent_reason == ""
            ws.close()

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["eof", "timeout", "cancel"])
def test_incomplete_handshake_releases_transport(mode):
    async def run():
        accepted = asyncio.Event()
        disconnected = asyncio.Event()

        async def serve(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            accepted.set()
            if mode == "eof":
                writer.close()
            else:
                await reader.read()
                writer.close()
            await writer.wait_closed()
            disconnected.set()

        async with await asyncio.start_server(serve, "127.0.0.1", 0) as server:
            task = asyncio.ensure_future(
                async_connect(
                    f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}",
                    connect_timeout=0.05 if mode == "timeout" else 5,
                )
            )
            await asyncio.wait_for(accepted.wait(), 1)
            if mode == "cancel":
                task.cancel()
            expected = {"eof": ConnectionError, "timeout": TimeoutError, "cancel": asyncio.CancelledError}[mode]
            with pytest.raises(expected):
                await asyncio.wait_for(task, 1)
            await asyncio.wait_for(disconnected.wait(), 1)

    asyncio.run(run())


def test_failed_handshakes_do_not_leave_native_clients_for_foreign_gc():
    import threading

    gc.collect()  # Dispose traceback cycles from preceding tests on their owning thread.

    was_enabled = gc.isenabled()
    gc.disable()
    try:
        for _ in range(10):
            test_rejects_invalid_upgrade_and_preserves_http_status("status")
        thread = threading.Thread(target=gc.collect)
        thread.start()
        thread.join()
    finally:
        if was_enabled:
            gc.enable()


@pytest.mark.parametrize("mode", ["local", "eof"])
def test_repeated_owner_thread_teardown_then_foreign_gc(mode):
    import threading

    async def run():
        import websockets

        async def serve(ws):
            if mode == "eof":
                ws.transport.abort()
            await ws.wait_closed()

        async with websockets.serve(serve, "127.0.0.1", 0) as server:
            for _ in range(20):
                ws = await async_connect(f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}")
                if mode == "eof":
                    async with asyncio.timeout(1):
                        while not ws.closed:
                            await asyncio.sleep(0)
                ws.close()
                del ws
                await asyncio.sleep(0)

    gc.collect()  # Dispose traceback cycles from preceding tests on their owning thread.

    was_enabled = gc.isenabled()
    gc.disable()
    try:
        owner = threading.Thread(target=lambda: asyncio.run(run()))
        owner.start()
        owner.join()
        # This main thread never owned any of the native connections.
        gc.collect()
    finally:
        if was_enabled:
            gc.enable()


@pytest.mark.parametrize("terminal", [b"\x88\x02\x03\xe9", b"\x80\x03bad"])
def test_final_message_callback_precedes_close_cleanup(terminal):
    async def run():
        import websockets

        async def serve(ws):
            await ws.wait_closed()

        received = []
        async with websockets.serve(serve, "127.0.0.1", 0) as server:
            ws = await async_connect(
                f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}",
                on_message=lambda message: received.append(bytes(message)),
            )
            ws.data_received(b"\x81\x04last" + terminal)
            assert received == [b"last"]
            assert ws.closed
            ws.close()

    asyncio.run(run())


def test_rejected_upgrade_traceback_can_be_disposed_on_foreign_thread():
    import threading

    failures = []

    async def run():
        async def serve(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            await reader.read()
            writer.close()
            await writer.wait_closed()

        async with await asyncio.start_server(serve, "127.0.0.1", 0) as server:
            try:
                await async_connect(f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}")
            except ConnectionError as error:
                assert error.status_code == 403
                failures.append(error)

    asyncio.run(run())
    assert len(failures) == 1
    thread = threading.Thread(target=lambda: (failures.clear(), gc.collect()))
    thread.start()
    thread.join()


@pytest.mark.parametrize("terminal", [b"\x88\x02\x03\xe9", b"\x80\x03bad"])
@pytest.mark.parametrize("path", ["bytes", "bytearray", "buffered"])
def test_final_callback_failure_reaches_loop_after_cleanup(terminal, path):
    async def run():
        import websockets

        async def serve(ws):
            await ws.wait_closed()

        class Callback:
            def __call__(self, message):
                raise ValueError("final callback failed")

        loop = asyncio.get_running_loop()
        observed = loop.create_future()

        def report(loop, context):
            if not observed.done():
                observed.set_result(context.get("exception"))

        loop.set_exception_handler(report)
        async with websockets.serve(serve, "127.0.0.1", 0) as server:
            callback = Callback()
            callback_ref = weakref.ref(callback)
            ws = await async_connect(f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}", on_message=callback)
            del callback
            data = b"\x81\x04last" + terminal
            if path == "buffered":
                buffer = ws.get_buffer(len(data))
                buffer[: len(data)] = data
                del buffer
                loop.call_soon(ws.buffer_updated, len(data))
            else:
                loop.call_soon(ws.data_received, data if path == "bytes" else bytearray(data))
            try:
                error = await asyncio.wait_for(observed, 1)
                assert isinstance(error, ValueError)
                assert str(error) == "final callback failed"
                assert ws.closed
                # The reported traceback itself legitimately retains Callback.__call__.
                error.__traceback__ = None
                assert callback_ref() is None
            finally:
                ws.close()
                loop.set_exception_handler(None)

    asyncio.run(run())
