//! Native asyncio.Protocol WebSocket client.
//!
//! Runs entirely on the asyncio event loop thread — no tokio runtime involvement
//! post-handshake, no cross-thread wakeup (call_soon_threadsafe). Frame codec is
//! in Rust with AVX2-friendly masking.
//!
//! Current scope:
//! - ws:// plain TCP; wss:// TLS through aiofastnet when installed, else the
//!   stdlib ssl module, with an experimental same-thread rustls backend behind
//!   the `rustls-transport` cargo feature; SOCKS5 via the embedded connect helper
//! - Binary + Text messages, fragmented messages, permessage-deflate when
//!   negotiated
//! - Control frames: close, client ping, and server pings answered with a
//!   masked pong on every receive path (fast paths and ProtocolCore alike)
//! - Client-side handshake (RFC 6455 §4.1) with subprotocol negotiation
//! - Fire-and-forget send(), async recv() with optional receive_timeout
//! - BufferedProtocol variant (NativeClientBuffered) sharing the same codec

mod client;
mod codec;
mod protocol;
#[cfg(feature = "rustls-transport")]
mod rustls_transport;

use self::client::{NativeClient, NativeClientBuffered, State, WSMessage};
use self::protocol::{build_handshake, DeflateCtx};
use std::collections::VecDeque;
use std::sync::Arc;

use bytes::BytesMut;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyModule;
use std::cell::RefCell;
use url::Url;

use crate::DEFAULT_CONNECT_TIMEOUT;

/// Connect to a ws:// or wss:// URI and return a NativeClient once the handshake completes.
///
/// TLS is delegated to the event loop — we pass an ``ssl.SSLContext`` through to
/// ``create_connection``, so the protocol sees decrypted bytes. If a custom
/// context is needed (self-signed, client cert), pass it via ``ssl_context``.
///
/// ``tls_backend`` selects which transport carries wss:// traffic and is ignored
/// for ws://:
/// - ``"auto"`` (default): aiofastnet when importable, otherwise asyncio.
/// - ``"asyncio"``: pin the stdlib ``loop.create_connection`` + SSLProtocol path.
/// - ``"aiofastnet"``: require aiofastnet; error if it is not installed.
/// - ``"rustls"``: experimental, requires the ``rustls-transport`` cargo feature.
///   Keeps TLS on this thread in Rust and takes ``rustls_ca_file`` in place of
///   ``ssl_context``; client-certificate auth is not implemented.
#[pyfunction]
#[pyo3(signature = (uri, *, headers=None, subprotocols=None, ssl_context=None, connect_timeout=None, receive_timeout=None, proxy=None, compression=false, on_message=None, tls_backend="auto", rustls_ca_file=None))]
#[allow(clippy::too_many_arguments)]
fn connect<'py>(
    py: Python<'py>,
    uri: String,
    headers: Option<Vec<(String, String)>>,
    subprotocols: Option<Vec<String>>,
    ssl_context: Option<Py<PyAny>>,
    connect_timeout: Option<f64>,
    receive_timeout: Option<f64>,
    proxy: Option<String>,
    compression: bool,
    on_message: Option<Py<PyAny>>,
    tls_backend: &str,
    rustls_ca_file: Option<String>,
) -> PyResult<Bound<'py, PyAny>> {
    let (scheme, host, port, path) = parse_ws_uri(&uri)?;
    let is_tls = scheme == "wss";
    let use_rustls = resolve_tls_backend(tls_backend, is_tls, ssl_context.is_some())?;
    // Keyed on the name, not on use_rustls: `tls_backend="rustls"` over ws://
    // resolves to false, and rejecting the trust file there would blame the
    // wrong argument.
    if rustls_ca_file.is_some() && tls_backend != "rustls" {
        return Err(PyValueError::new_err(
            "rustls_ca_file requires tls_backend=\"rustls\"",
        ));
    }
    let headers = headers.unwrap_or_default();
    let subprotocols = subprotocols.unwrap_or_default();
    let (req_bytes, expected_accept) =
        build_handshake(&host, port, &path, &headers, &subprotocols, compression);
    let client = NativeClient {
        #[allow(clippy::arc_with_non_send_sync)]
        state: Arc::new(RefCell::new(State {
            transport: None,
            // Buffers start empty — first receive or fragment triggers growth.
            // This avoids reserving receive memory for idle connections.
            buf: BytesMut::new(),
            handshake_done: false,
            handshake_fut: None,
            expected_accept,
            pending_pings: Default::default(),
            pending_recv: VecDeque::new(),
            backlog: VecDeque::new(),
            on_message,
            pending_callback_msgs: VecDeque::new(),
            closed: false,
            paused: false,
            buf_known_empty: false,
            mask_pool: Vec::new(),
            send_buf: Vec::new(),
            recv_buf: Vec::new(),
            recv_pos: 0,
            next_frame_needed: None,
            write_queue: VecDeque::new(),
            loop_ref: None,
            transport_write: None,
            transport_get_buf_size: None,
            raw_fd: None,
            create_future: None,
            wait_for: None,
            subprotocol: None,
            close_received_code: None,
            close_received_reason: None,
            close_sent_code: None,
            close_sent_reason: None,
            close_code: None,
            close_reason: None,
            receive_timeout,
            fragment_buf: None,
            fragment_opcode: 0,
            fragment_rsv1: false,
            deflate: if compression { Some(DeflateCtx) } else { None },
        })),
    };
    let state_arc = client.state.clone();
    let client_obj: Py<PyAny> = if is_tls {
        Py::new(py, client)?.into_any()
    } else {
        Py::new(py, (NativeClientBuffered, client))?.into_any()
    };

    // With rustls the object asyncio drives is the TLS shim; it decrypts and
    // forwards plaintext into the NativeClient, which never sees the raw socket.
    #[cfg(feature = "rustls-transport")]
    let protocol_obj: Py<PyAny> = if use_rustls {
        Py::new(
            py,
            rustls_transport::RustlsTransport::new(
                client_obj.extract::<Py<NativeClient>>(py)?,
                host.clone(),
                rustls_ca_file,
            )?,
        )?
        .into_any()
    } else {
        client_obj.clone_ref(py)
    };
    #[cfg(not(feature = "rustls-transport"))]
    let protocol_obj: Py<PyAny> = client_obj.clone_ref(py);

    // Create the handshake future. Cache `loop.create_future` and
    // `asyncio.wait_for` bound methods so the recv/anext hot paths don't
    // need to re-resolve them.
    let asyncio = py.import("asyncio")?;
    let loop_ = asyncio.call_method0("get_running_loop")?;
    let create_future = loop_.getattr(pyo3::intern!(py, "create_future"))?;
    let wait_for = asyncio.getattr(pyo3::intern!(py, "wait_for"))?;
    let handshake_fut = create_future.call0()?;
    {
        let mut st = state_arc.borrow_mut();
        st.handshake_fut = Some(handshake_fut.clone().unbind());
        st.loop_ref = Some(loop_.clone().unbind());
        st.create_future = Some(create_future.unbind());
        st.wait_for = Some(wait_for.unbind());
    }

    // Launch the low-level create_connection + post-connection handshake send as a task
    let protocol_factory = {
        let client_clone = protocol_obj.clone_ref(py);
        pyo3::types::PyCFunction::new_closure(
            py,
            None,
            None,
            move |_args, _kwargs| -> PyResult<Py<PyAny>> {
                Python::attach(|py| Ok(client_clone.clone_ref(py).into_any()))
            },
        )?
    };

    // Resolve SSL context if wss:// (user-supplied overrides default).
    let ssl_arg: Py<PyAny> = if is_tls && !use_rustls {
        match ssl_context {
            Some(ctx) => ctx,
            None => py
                .import("ssl")?
                .call_method0("create_default_context")?
                .unbind(),
        }
    } else {
        py.None()
    };

    // If a proxy is configured, SOCKS5 negotiation happens Python-side inside
    // run_in_executor (see _connect_helper). Otherwise create_connection takes
    // host/port directly.
    let helper = get_connect_helper(py)?;
    let timeout_obj = connect_timeout
        .unwrap_or(DEFAULT_CONNECT_TIMEOUT)
        .into_pyobject(py)?
        .into_any();
    let proxy_obj = match proxy {
        Some(p) => p.into_pyobject(py)?.into_any().unbind(),
        None => py.None(),
    };
    // What the helper should act on, which is not always what the caller asked
    // for: over ws:// the rustls shim is never built, so telling the helper
    // "rustls" there would make it write the handshake to a NativeClient that
    // has no write(). Collapsing to "auto" is safe because the helper ignores
    // the backend entirely for plain TCP.
    let helper_backend = if tls_backend == "rustls" && !use_rustls {
        "auto"
    } else {
        tls_backend
    };
    let ssl_obj: Py<PyAny> = if is_tls && !use_rustls {
        ssl_arg
    } else {
        py.None()
    };
    helper.call1((
        loop_,
        protocol_factory,
        host.clone(),
        port,
        // rustls drives the handshake itself over a plain TCP transport.
        is_tls && !use_rustls,
        ssl_obj,
        proxy_obj,
        req_bytes,
        handshake_fut,
        client_obj,
        timeout_obj,
        helper_backend,
    ))
}

/// Validate ``tls_backend`` and report whether the same-thread rustls transport
/// was requested. Backend selection between asyncio and aiofastnet happens in
/// the Python connect helper, which is where ``create_connection`` lives; this
/// only rejects unknown names and combinations that cannot work.
fn resolve_tls_backend(tls_backend: &str, is_tls: bool, has_ssl_context: bool) -> PyResult<bool> {
    match tls_backend {
        "auto" | "asyncio" | "aiofastnet" => Ok(false),
        "rustls" => {
            if cfg!(not(feature = "rustls-transport")) {
                return Err(PyValueError::new_err(
                    "tls_backend=\"rustls\" needs a build with the `rustls-transport` \
                     cargo feature enabled",
                ));
            }
            // Silently ignoring an SSLContext would hide a misconfigured trust
            // store, so reject the combination rather than pick one of them.
            if is_tls && has_ssl_context {
                return Err(PyValueError::new_err(
                    "tls_backend=\"rustls\" does not accept ssl_context; use rustls_ca_file",
                ));
            }
            // ws:// has no TLS layer to swap out, so the backend is inert there
            // — the same leniency the other backends get.
            Ok(is_tls)
        }
        other => Err(PyValueError::new_err(format!(
            "tls_backend must be one of auto, asyncio, aiofastnet, rustls (got {other:?})"
        ))),
    }
}

fn parse_ws_uri(uri: &str) -> PyResult<(&'static str, String, u16, String)> {
    let parsed = Url::parse(uri).map_err(|_| PyValueError::new_err("Invalid WebSocket URI"))?;
    let (scheme, default_port) = match parsed.scheme() {
        "wss" => ("wss", 443),
        "ws" => ("ws", 80),
        _ => return Err(PyValueError::new_err("URI must start with ws:// or wss://")),
    };
    // url keeps IPv6 literals bracketed ([::1]); getaddrinfo and the SOCKS5
    // helper need the bare literal. build_handshake re-brackets it for the
    // Host header.
    let raw_host = parsed
        .host_str()
        .filter(|h| !h.is_empty())
        .ok_or_else(|| PyValueError::new_err("URI must include a host"))?;
    let host = raw_host
        .strip_prefix('[')
        .and_then(|h| h.strip_suffix(']'))
        .map(|h| h.to_string())
        .unwrap_or_else(|| raw_host.to_string());
    let port = parsed.port().unwrap_or(default_port);
    let mut path = parsed.path().to_string();
    if path.is_empty() {
        path.push('/');
    }
    if let Some(query) = parsed.query() {
        path.push('?');
        path.push_str(query);
    }
    Ok((scheme, host, port, path))
}

/// Cached Python helper that orchestrates create_connection -> send handshake -> await accept -> return client.
fn get_connect_helper(py: Python<'_>) -> PyResult<Bound<'_, PyAny>> {
    use std::sync::OnceLock;
    static CACHE: OnceLock<Py<PyAny>> = OnceLock::new();
    if let Some(h) = CACHE.get() {
        return Ok(h.bind(py).clone());
    }
    let code = include_str!("connect_helper.py");
    let module = PyModule::from_code(
        py,
        std::ffi::CString::new(code)?.as_c_str(),
        c"helper.py",
        c"helper",
    )?;
    // Publish the module so the TLS backend selection it owns is reachable for
    // tests and debugging. The name is private; nothing in the public API
    // depends on it.
    py.import("sys")?
        .getattr("modules")?
        .set_item("websocket_rs._native_connect_helper", &module)?;
    let helper = module.getattr("_connect_helper")?;
    let _ = CACHE.set(helper.clone().unbind());
    Ok(helper)
}

pub fn register_native_client(py: Python<'_>, parent: &Bound<'_, PyModule>) -> PyResult<()> {
    let m = PyModule::new(py, "native_client")?;
    m.add_class::<NativeClient>()?;
    m.add_class::<NativeClientBuffered>()?;
    m.add_class::<WSMessage>()?;
    m.add_function(wrap_pyfunction!(connect, &m)?)?;
    parent.add_submodule(&m)?;
    // Also register in sys.modules so `from websocket_rs.native_client import ...` works.
    let sys_modules = py.import("sys")?.getattr("modules")?;
    sys_modules.set_item("websocket_rs.native_client", &m)?;
    // Compile the connect helper now rather than on the first connect: it is a
    // fixed include_str! of our own source, so nothing about it can fail later
    // that would not fail here, and this keeps it off the connect path.
    get_connect_helper(py)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use std::cell::RefCell;

    use bytes::BytesMut;

    use super::codec::{
        parse_header, walk_frames, ScanOutcome, VisitOutcome, MAX_FRAME_SIZE, OP_BINARY, OP_PING,
    };
    use super::protocol::{
        decompress_message, emit_protocol_events, EventFlow, HandshakeOutcome, ProtocolCore,
        ProtocolEvent,
    };
    use super::{parse_ws_uri, resolve_tls_backend};

    struct CoreState {
        buf: BytesMut,
        handshake_done: bool,
        expected_accept: String,
        compression_enabled: bool,
        fragment_buf: Option<BytesMut>,
        fragment_opcode: u8,
        fragment_rsv1: bool,
    }

    impl CoreState {
        fn core(&mut self) -> ProtocolCore<'_> {
            ProtocolCore {
                buf: &mut self.buf,
                handshake_done: &mut self.handshake_done,
                expected_accept: &self.expected_accept,
                compression_enabled: self.compression_enabled,
                fragment_buf: &mut self.fragment_buf,
                fragment_opcode: &mut self.fragment_opcode,
                fragment_rsv1: &mut self.fragment_rsv1,
            }
        }
    }

    fn frame_core(data: &[u8]) -> RefCell<CoreState> {
        RefCell::new(CoreState {
            buf: BytesMut::from(data),
            handshake_done: true,
            expected_accept: String::new(),
            compression_enabled: false,
            fragment_buf: None,
            fragment_opcode: 0,
            fragment_rsv1: false,
        })
    }

    #[test]
    fn test_parse_header_masked_server_frame_keeps_unmasked_header_size() {
        let frame = [0x82, 0x81, 1, 2, 3, 4, b'x'];

        assert_eq!(parse_header(&frame), Some((true, false, 0x2, 1, 2)));
    }

    #[test]
    fn test_parse_header_rsv2_rsv3_and_reserved_opcode_returns_header() {
        let frame = [0xB3, 0x00];

        assert_eq!(parse_header(&frame), Some((true, false, 0x3, 0, 2)));
    }

    #[test]
    fn test_parse_header_fragmented_extended_ping_returns_header() {
        let frame = [OP_PING, 126, 0, 126];

        assert_eq!(parse_header(&frame), Some((false, false, OP_PING, 126, 4)));
    }

    #[test]
    fn test_parse_header_incomplete_extended_lengths_returns_none() {
        assert_eq!(parse_header(&[0x82, 126, 0]), None);
        assert_eq!(parse_header(&[0x82, 127, 0, 0, 0, 0, 0, 0, 0]), None);
    }

    #[test]
    fn test_walk_frames_complete_frames_visits_payloads() {
        let data = [0x82, 0x01, b'a', 0x82, 0x02, b'b', b'c'];
        let mut payloads = Vec::new();

        let outcome = walk_frames(&data, |frame| {
            assert_eq!(frame.opcode, OP_BINARY);
            payloads.push(frame.payload);
            Ok::<_, ()>(VisitOutcome::Continue)
        })
        .unwrap();

        assert_eq!(payloads, [b"a".as_slice(), b"bc".as_slice()]);
        assert_eq!(outcome, ScanOutcome::Exhausted { consumed: 7 });
    }

    #[test]
    fn test_walk_frames_partial_payload_reports_post_compaction_threshold() {
        let data = [0x82, 0x01, b'a', 0x82, 0x03, b'b'];

        let outcome = walk_frames(&data, |_| Ok::<_, ()>(VisitOutcome::Continue)).unwrap();

        assert_eq!(
            outcome,
            ScanOutcome::Partial {
                consumed: 3,
                needed: 5,
            }
        );
    }

    #[test]
    fn test_walk_frames_fragmented_frame_falls_back_without_visiting() {
        let data = [0x02, 0x01, b'a'];
        let mut visited = false;

        let outcome = walk_frames(&data, |_| {
            visited = true;
            Ok::<_, ()>(VisitOutcome::Continue)
        })
        .unwrap();

        assert!(!visited);
        assert_eq!(outcome, ScanOutcome::Fallback { consumed: 0 });
    }

    /// Binary frame header declaring a payload length of `u64::MAX`: with
    /// overflow checks off, `hdr + plen` used to wrap and index past the
    /// buffer, aborting the process under `panic = "abort"`.
    const OVERSIZED_FRAME: [u8; 12] = [
        0x82, 0x7F, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0x01, 0x02,
    ];

    #[test]
    fn test_walk_frames_oversized_length_falls_back_without_visiting() {
        let mut visited = false;

        let outcome = walk_frames(&OVERSIZED_FRAME, |_| {
            visited = true;
            Ok::<_, ()>(VisitOutcome::Continue)
        })
        .unwrap();

        assert!(!visited);
        assert_eq!(outcome, ScanOutcome::Fallback { consumed: 0 });
    }

    #[test]
    fn test_protocol_core_oversized_frame_emits_1009_before_buffering() {
        let core = frame_core(&OVERSIZED_FRAME);

        let event = core.borrow_mut().core().next_event().unwrap();

        assert!(matches!(
            event,
            ProtocolEvent::ProtocolError { code: 1009, .. }
        ));
    }

    #[test]
    fn test_protocol_core_fragments_over_message_cap_emit_1009() {
        // First fragment: 1 byte, then a continuation declaring 64 MiB; the
        // sum crosses MAX_MESSAGE_SIZE before any payload bytes arrive.
        let mut data = vec![0x02, 0x01, b'a', 0x80, 0x7F];
        data.extend_from_slice(&(64u64 << 20).to_be_bytes());
        let core = frame_core(&data);

        let event = core.borrow_mut().core().next_event().unwrap();

        assert!(matches!(
            event,
            ProtocolEvent::ProtocolError { code: 1009, .. }
        ));
    }

    #[test]
    fn test_decompress_message_inflating_past_message_cap_emits_1009() {
        // 64 MiB + 1 of zeros deflates to a few KiB; the inflated size is what
        // must trip the cap, not the on-wire frame length.
        let mut encoder =
            flate2::write::DeflateEncoder::new(Vec::new(), flate2::Compression::best());
        let zeros = vec![0u8; 1 << 20];
        for _ in 0..64 {
            std::io::Write::write_all(&mut encoder, &zeros).unwrap();
        }
        std::io::Write::write_all(&mut encoder, &[0u8]).unwrap();
        let compressed = encoder.finish().unwrap();
        assert!(compressed.len() < MAX_FRAME_SIZE);

        let event = decompress_message(true, &compressed).unwrap_err();

        assert!(matches!(
            event,
            ProtocolEvent::ProtocolError { code: 1009, .. }
        ));
    }

    #[test]
    fn test_protocol_handshake_accepts_subprotocol_and_compression() {
        let mut state = CoreState {
            buf: BytesMut::from(
                &b"HTTP/1.1 101 Switching Protocols\r\n\
                   Upgrade: websocket\r\n\
                   Connection: Upgrade\r\n\
                   Sec-WebSocket-Accept: expected\r\n\
                   Sec-WebSocket-Protocol: chat\r\n\
                   Sec-WebSocket-Extensions: permessage-deflate\r\n\r\n"[..],
            ),
            handshake_done: false,
            expected_accept: "expected".to_string(),
            compression_enabled: true,
            fragment_buf: None,
            fragment_opcode: 0,
            fragment_rsv1: false,
        };

        let outcome = state.core().process_handshake();

        assert!(matches!(
            outcome,
            HandshakeOutcome::Accepted {
                subprotocol: Some(ref protocol),
                compression_enabled: true,
            } if protocol == "chat"
        ));
        assert!(state.handshake_done);
        assert!(state.buf.is_empty());
    }

    #[test]
    fn test_emit_protocol_events_fragment_ping_message_order_and_releases_borrow() {
        let core = frame_core(
            b"\x02\x05hello\
              \x89\x04ping\
              \x80\x05world",
        );
        let mut index = 0;

        emit_protocol_events(
            || core.borrow_mut().core().next_event(),
            |event| {
                assert!(core.try_borrow_mut().is_ok());
                match (index, event) {
                    (0, ProtocolEvent::SendPong(payload)) => assert_eq!(payload, b"ping"[..]),
                    (1, ProtocolEvent::Message(payload)) => assert_eq!(payload, b"helloworld"[..]),
                    _ => panic!("unexpected protocol event"),
                }
                index += 1;
                Ok::<_, ()>(EventFlow::Continue)
            },
        )
        .unwrap();

        assert_eq!(index, 2);
    }

    #[test]
    fn test_protocol_core_continuation_without_fragment_emits_protocol_error() {
        let core = frame_core(b"\x80\x01x");

        let event = core.borrow_mut().core().next_event().unwrap();

        assert!(matches!(
            event,
            ProtocolEvent::ProtocolError {
                code: 1002,
                reason: "continuation frame without fragmented message"
            }
        ));
    }

    #[test]
    fn test_protocol_core_close_emits_code_and_reason() {
        let core = frame_core(b"\x88\x05\x03\xe9bye");

        let event = core.borrow_mut().core().next_event().unwrap();

        assert!(matches!(
            event,
            ProtocolEvent::Close {
                code: Some(1001),
                reason: Some(ref reason),
            } if reason == "bye"
        ));
    }

    #[test]
    fn test_resolve_tls_backend_accepts_the_python_side_backends() {
        // These three are all resolved in the connect helper, so none of them
        // asks the Rust side for the rustls shim.
        for name in ["auto", "asyncio", "aiofastnet"] {
            assert!(!resolve_tls_backend(name, true, false).unwrap());
            assert!(!resolve_tls_backend(name, false, false).unwrap());
        }
        // A user-supplied SSLContext is only meaningful to those backends, and
        // must keep working with them.
        assert!(!resolve_tls_backend("auto", true, true).unwrap());
    }

    #[test]
    fn test_resolve_tls_backend_rejects_unknown_names() {
        // The message itself is asserted from tests/test_tls_backends.py; these
        // unit tests run without an initialised interpreter to read it.
        assert!(resolve_tls_backend("openssl", true, false).is_err());
        assert!(resolve_tls_backend("", true, false).is_err());
        assert!(resolve_tls_backend("Auto", true, false).is_err());
    }

    #[test]
    #[cfg(feature = "rustls-transport")]
    fn test_resolve_tls_backend_rustls_needs_tls_and_no_ssl_context() {
        assert!(resolve_tls_backend("rustls", true, false).unwrap());
        // ws:// has no TLS layer to replace, so the backend is simply inert
        // rather than an error - same leniency the other backends get.
        assert!(!resolve_tls_backend("rustls", false, false).unwrap());
        // Silently ignoring an SSLContext would hide a misconfigured trust store.
        assert!(resolve_tls_backend("rustls", true, true).is_err());
    }

    #[test]
    #[cfg(not(feature = "rustls-transport"))]
    fn test_resolve_tls_backend_rustls_needs_the_feature() {
        // Not compiled in: asking for it must fail loudly rather than quietly
        // falling back to a different TLS stack than the caller asked for.
        assert!(resolve_tls_backend("rustls", true, false).is_err());
        assert!(resolve_tls_backend("rustls", false, false).is_err());
    }

    #[test]
    fn test_parse_ws_uri_ipv6_with_port_and_query() {
        let (scheme, host, port, path) = parse_ws_uri("ws://[::1]:8860/ws?token=a").unwrap();
        assert_eq!(scheme, "ws");
        assert_eq!(host, "::1");
        assert_eq!(port, 8860);
        assert_eq!(path, "/ws?token=a");
    }

    #[test]
    fn test_parse_ws_uri_uses_default_ports() {
        let (scheme, host, port, path) = parse_ws_uri("wss://example.com/feed").unwrap();
        assert_eq!(scheme, "wss");
        assert_eq!(host, "example.com");
        assert_eq!(port, 443);
        assert_eq!(path, "/feed");
    }
}
