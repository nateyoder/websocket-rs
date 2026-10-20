//! Experimental same-thread rustls transport. No Tokio tasks or Python plaintext buffers.
use std::cell::RefCell;
use std::io::{Read, Write};
use std::sync::Arc;

use pyo3::exceptions::{PyConnectionError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyBytes;

use super::client::NativeClient;
use super::NativeReceiveCapacityError;

struct State {
    tls: rustls::ClientConnection,
    inner: Option<Py<NativeClient>>,
    transport: Option<Py<PyAny>>,
    plaintext: Vec<u8>,
    plaintext_used: usize,
    websocket_mode: bool,
    frame_remaining: Option<usize>,
    ciphertext: Vec<u8>,
    closing: bool,
    read_paused: bool,
    peer_closed: bool,
    terminal_error: Option<Py<PyAny>>,
}

const MAX_PENDING_PLAINTEXT: usize = 1024 * 1024;
const MAX_HANDSHAKE_BYTES: usize = 64 * 1024;

fn ensure_pending_plaintext_capacity(pending: usize, additional: usize) -> PyResult<()> {
    if pending
        .checked_add(additional)
        .is_none_or(|total| total > MAX_PENDING_PLAINTEXT)
    {
        return Err(NativeReceiveCapacityError::new_err(
            "Rust TLS pending plaintext exceeds 1 MiB read-flow bound",
        ));
    }
    Ok(())
}

#[pyclass(unsendable)]
pub(super) struct RustlsTransport {
    state: RefCell<State>,
}

impl RustlsTransport {
    pub(super) fn new(
        inner: Py<NativeClient>,
        host: String,
        ca_file: Option<String>,
    ) -> PyResult<Self> {
        let config = if let Some(path) = ca_file {
            let pem = std::fs::read(path).map_err(io_error)?;
            let mut roots = rustls::RootCertStore::empty();
            for cert in rustls_pemfile::certs(&mut pem.as_slice()) {
                roots
                    .add(cert.map_err(io_error)?)
                    .map_err(|e| PyValueError::new_err(e.to_string()))?;
            }
            if roots.is_empty() {
                return Err(PyValueError::new_err(
                    "rustls_ca_file contains no certificates",
                ));
            }
            Arc::new(
                rustls::ClientConfig::builder()
                    .with_root_certificates(roots)
                    .with_no_client_auth(),
            )
        } else {
            crate::sync_client::build_rustls_client_config()?
        };
        let name = rustls::pki_types::ServerName::try_from(host)
            .map_err(|e| PyValueError::new_err(e.to_string()))?;
        let mut tls = rustls::ClientConnection::new(config, name)
            .map_err(|e| PyValueError::new_err(e.to_string()))?;
        // write() is synchronous and must consume a complete WebSocket frame.
        // The raw transport + NativeClient apply their existing write watermarks.
        tls.set_buffer_limit(None);
        Ok(Self {
            state: RefCell::new(State {
                tls,
                inner: Some(inner),
                transport: None,
                plaintext: Vec::new(),
                plaintext_used: 0,
                websocket_mode: false,
                frame_remaining: None,
                ciphertext: Vec::new(),
                closing: false,
                read_paused: false,
                peer_closed: false,
                terminal_error: None,
            }),
        })
    }

    fn flush(&self, py: Python<'_>) -> PyResult<()> {
        let (mut out, transport) = {
            let mut st = self.state.borrow_mut();
            let mut out = std::mem::take(&mut st.ciphertext);
            out.clear();
            while st.tls.wants_write() {
                st.tls.write_tls(&mut out).map_err(io_error)?;
            }
            (out, st.transport.as_ref().map(|t| t.clone_ref(py)))
        };
        // Do not hold TLS state across transport calls: pause_writing can re-enter.
        let result = if let Some(t) = transport {
            if out.is_empty() {
                Ok(())
            } else {
                t.bind(py)
                    .call_method1("write", (PyBytes::new(py, &out),))
                    .map(|_| ())
            }
        } else {
            Ok(())
        };
        out.clear();
        self.state.borrow_mut().ciphertext = out;
        result
    }

    fn fail(&self, py: Python<'_>, err: PyErr) -> PyResult<()> {
        let error = err.value(py).clone().unbind();
        let (inner, transport) = {
            let mut st = self.state.borrow_mut();
            st.closing = true;
            st.terminal_error = Some(error.clone_ref(py).into_any());
            (
                st.inner.as_ref().map(|c| c.clone_ref(py)),
                st.transport.as_ref().map(|t| t.clone_ref(py)),
            )
        };
        if let Some(c) = inner {
            let fut = c.borrow(py).state.borrow_mut().handshake_fut.take();
            if let Some(f) = fut {
                if !f.bind(py).call_method0("done")?.is_truthy()? {
                    f.bind(py).call_method1("set_exception", (err.value(py),))?;
                }
            }
        }
        if let Some(t) = transport {
            t.bind(py).call_method0("abort")?;
        }
        Ok(())
    }

    fn drain_plaintext(&self, py: Python<'_>) -> PyResult<()> {
        loop {
            let next = {
                let mut st = self.state.borrow_mut();
                if st.closing || st.read_paused || st.plaintext_used == st.plaintext.len() {
                    if st.plaintext_used == st.plaintext.len() {
                        st.plaintext.clear();
                        st.plaintext_used = 0;
                    }
                    return Ok(());
                }

                let available_len = st.plaintext.len() - st.plaintext_used;
                let take = if !st.websocket_mode {
                    match st.plaintext[st.plaintext_used..]
                        .windows(4)
                        .position(|w| w == b"\r\n\r\n")
                    {
                        Some(pos) => {
                            st.websocket_mode = true;
                            pos + 4
                        }
                        None if available_len > MAX_HANDSHAKE_BYTES => {
                            return Err(PyConnectionError::new_err(
                                "Rust TLS WebSocket handshake exceeds 64 KiB",
                            ));
                        }
                        None => return Ok(()),
                    }
                } else if let Some(remaining) = st.frame_remaining {
                    remaining.min(available_len)
                } else {
                    let frame_len = {
                        let available = &st.plaintext[st.plaintext_used..];
                        websocket_frame_len(available)?
                    };
                    let Some(frame_len) = frame_len else {
                        return Ok(());
                    };
                    st.frame_remaining = Some(frame_len);
                    frame_len.min(available_len)
                };

                let start = st.plaintext_used;
                let end = start + take;
                let chunk = st.plaintext[start..end].to_vec();
                st.plaintext_used = end;
                if st.websocket_mode {
                    if let Some(remaining) = st.frame_remaining.as_mut() {
                        *remaining -= take;
                        if *remaining == 0 {
                            st.frame_remaining = None;
                        }
                    }
                }
                (chunk, st.inner.as_ref().map(|c| c.clone_ref(py)))
            };

            if let (chunk, Some(inner)) = next {
                inner.borrow(py).data_received_plaintext(py, &chunk)?;
            }
        }
    }

    fn finish_peer_close(&self, py: Python<'_>) -> PyResult<()> {
        let terminal = {
            let st = self.state.borrow();
            if st.closing || !st.peer_closed || st.read_paused {
                return Ok(());
            }
            st.plaintext_used == st.plaintext.len()
        };
        if terminal {
            self.close(py)
        } else {
            self.fail(
                py,
                PyConnectionError::new_err(
                    "Rust TLS peer closed with an incomplete WebSocket frame",
                ),
            )
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pending_plaintext_bound_has_a_stable_public_error_type() {
        Python::initialize();
        Python::attach(|py| {
            assert!(ensure_pending_plaintext_capacity(MAX_PENDING_PLAINTEXT, 0).is_ok());
            let error = ensure_pending_plaintext_capacity(MAX_PENDING_PLAINTEXT, 1).unwrap_err();
            assert!(error.is_instance_of::<NativeReceiveCapacityError>(py));
            assert_eq!(
                error.to_string(),
                "NativeReceiveCapacityError: Rust TLS pending plaintext exceeds 1 MiB read-flow bound"
            );
        });
    }
}

fn websocket_frame_len(data: &[u8]) -> PyResult<Option<usize>> {
    if data.len() < 2 {
        return Ok(None);
    }
    let masked = data[1] & 0x80 != 0;
    let short = (data[1] & 0x7f) as usize;
    let (header, payload): (usize, usize) = match short {
        126 => {
            if data.len() < 4 {
                return Ok(None);
            }
            (4, u16::from_be_bytes([data[2], data[3]]) as usize)
        }
        127 => {
            if data.len() < 10 {
                return Ok(None);
            }
            let n = u64::from_be_bytes(data[2..10].try_into().unwrap());
            let payload = usize::try_from(n).map_err(|_| {
                PyConnectionError::new_err("WebSocket frame length exceeds platform limits")
            })?;
            (10, payload)
        }
        n => (2, n),
    };
    header
        .checked_add(if masked { 4 } else { 0 })
        .and_then(|n| n.checked_add(payload))
        .map(Some)
        .ok_or_else(|| PyConnectionError::new_err("WebSocket frame length overflow"))
}

fn io_error(e: std::io::Error) -> PyErr {
    PyConnectionError::new_err(e.to_string())
}

#[pymethods]
impl RustlsTransport {
    fn connection_made(slf: PyRef<'_, Self>, py: Python<'_>, transport: Py<PyAny>) -> PyResult<()> {
        let inner = {
            let mut st = slf.state.borrow_mut();
            st.transport = Some(transport);
            st.inner.as_ref().unwrap().clone_ref(py)
        };
        inner
            .bind(py)
            .call_method1("connection_made", (slf.into_pyobject(py)?,))?;
        // ClientHello is emitted by the first write() from the connect helper.
        Ok(())
    }

    fn data_received(&self, py: Python<'_>, data: &[u8]) -> PyResult<()> {
        if self.state.borrow().closing {
            return Ok(());
        }
        let decoded: PyResult<()> = (|| {
            let mut st = self.state.borrow_mut();
            let mut input = data;
            // Kept at its high-water length, never cleared: `read_to_end` treats
            // spare capacity as uninitialised and zeroes it on every call, which
            // profiling showed as a full-size memset per message. Reading into
            // an already-initialised slice with a separate used-length skips it.
            let mut peer_closed = false;
            while !input.is_empty() {
                let n = st.tls.read_tls(&mut input).map_err(io_error)?;
                if n == 0 {
                    break;
                }
                let io = st.tls.process_new_packets().map_err(|e| {
                    if matches!(e, rustls::Error::InvalidCertificate(_)) {
                        match py
                            .import("ssl")
                            .and_then(|m| m.getattr("SSLCertVerificationError"))
                            .and_then(|t| t.call1((e.to_string(),)))
                        {
                            Ok(value) => PyErr::from_value(value),
                            Err(error) => error,
                        }
                    } else {
                        PyConnectionError::new_err(e.to_string())
                    }
                })?;
                peer_closed |= io.peer_has_closed();
                // rustls reports exactly how much plaintext is ready, so the
                // destination can be sized once and filled with a plain read
                // rather than grown-and-zeroed by read_to_end.
                let want = io.plaintext_bytes_to_read();
                if want > 0 {
                    let pending = st.plaintext.len() - st.plaintext_used;
                    ensure_pending_plaintext_capacity(pending, want)?;
                    if st.plaintext_used > 0 {
                        let used = st.plaintext_used;
                        st.plaintext.copy_within(used.., 0);
                        st.plaintext.truncate(pending);
                        st.plaintext_used = 0;
                    }
                    let start = st.plaintext.len();
                    st.plaintext.resize(start + want, 0);
                    // `read` rather than `read_exact`: a short read advances
                    // `used` by exactly what was copied, and the remainder is
                    // re-reported by the next `plaintext_bytes_to_read()`, so
                    // no already-consumed plaintext can be dropped.
                    let read = {
                        let State { tls, plaintext, .. } = &mut *st;
                        tls.reader().read(&mut plaintext[start..start + want])
                    };
                    match read {
                        Ok(n) => st.plaintext.truncate(start + n),
                        Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => (),
                        Err(e) => return Err(io_error(e)),
                    }
                }
            }
            if peer_closed {
                st.peer_closed = true;
            }
            Ok(())
        })();
        match decoded {
            Ok(()) => (),
            Err(e) => return self.fail(py, e),
        }
        self.flush(py)?;
        self.drain_plaintext(py)?;
        self.finish_peer_close(py)
    }

    fn write(&self, py: Python<'_>, data: &[u8]) -> PyResult<()> {
        {
            let mut st = self.state.borrow_mut();
            if st.closing {
                return Err(PyConnectionError::new_err("TLS transport is closing"));
            }
            st.tls.writer().write_all(data).map_err(io_error)?;
        }
        self.flush(py)
    }

    #[pyo3(signature = (name, default=None))]
    fn get_extra_info(
        &self,
        py: Python<'_>,
        name: &str,
        default: Option<Py<PyAny>>,
    ) -> PyResult<Py<PyAny>> {
        // NativeClient must never bypass TLS by writing directly to the socket fd.
        if name == "ssl_object" {
            return Ok(true.into_pyobject(py)?.to_owned().into_any().unbind());
        }
        let t = self
            .state
            .borrow()
            .transport
            .as_ref()
            .map(|t| t.clone_ref(py));
        match t {
            Some(t) => Ok(t
                .bind(py)
                .call_method1("get_extra_info", (name, default))?
                .unbind()),
            None => Ok(default.unwrap_or_else(|| py.None())),
        }
    }

    fn get_write_buffer_size(&self, py: Python<'_>) -> PyResult<usize> {
        let t = self
            .state
            .borrow()
            .transport
            .as_ref()
            .map(|t| t.clone_ref(py));
        match t {
            Some(t) => t.bind(py).call_method0("get_write_buffer_size")?.extract(),
            None => Ok(0),
        }
    }

    fn pause_writing(&self, py: Python<'_>) -> PyResult<()> {
        let c = self.state.borrow().inner.as_ref().map(|c| c.clone_ref(py));
        if let Some(c) = c {
            c.bind(py).call_method0("pause_writing")?;
        }
        Ok(())
    }

    fn resume_writing(&self, py: Python<'_>) -> PyResult<()> {
        let c = self.state.borrow().inner.as_ref().map(|c| c.clone_ref(py));
        if let Some(c) = c {
            c.bind(py).call_method0("resume_writing")?;
        }
        Ok(())
    }

    fn pause_reading(&self, py: Python<'_>) -> PyResult<()> {
        let transport = {
            let mut st = self.state.borrow_mut();
            if st.closing || st.read_paused {
                return Ok(());
            }
            st.read_paused = true;
            st.transport.as_ref().map(|t| t.clone_ref(py))
        };
        if let Some(t) = transport {
            t.bind(py).call_method0("pause_reading")?;
        }
        Ok(())
    }

    fn resume_reading(&self, py: Python<'_>) -> PyResult<()> {
        {
            let mut st = self.state.borrow_mut();
            if st.closing || !st.read_paused {
                return Ok(());
            }
            st.read_paused = false;
        }
        self.drain_plaintext(py)?;
        self.finish_peer_close(py)?;
        let transport = {
            let st = self.state.borrow();
            if st.closing || st.read_paused || st.plaintext_used != st.plaintext.len() {
                None
            } else {
                st.transport.as_ref().map(|t| t.clone_ref(py))
            }
        };
        if let Some(t) = transport {
            t.bind(py).call_method0("resume_reading")?;
        }
        Ok(())
    }

    fn is_reading(&self) -> bool {
        let st = self.state.borrow();
        !st.closing && !st.read_paused
    }

    fn eof_received(&self) -> bool {
        false
    }

    fn close(&self, py: Python<'_>) -> PyResult<()> {
        let transport = {
            let mut st = self.state.borrow_mut();
            if st.closing {
                return Ok(());
            }
            st.closing = true;
            st.tls.send_close_notify();
            st.transport.as_ref().map(|t| t.clone_ref(py))
        };
        self.flush(py)?;
        if let Some(t) = transport {
            t.bind(py).call_method0("close")?;
        }
        Ok(())
    }

    fn connection_lost(&self, py: Python<'_>, exc: Py<PyAny>) -> PyResult<()> {
        let (inner, terminal_error) = {
            let mut st = self.state.borrow_mut();
            st.closing = true;
            st.plaintext.clear();
            st.plaintext_used = 0;
            st.transport = None;
            (st.inner.take(), st.terminal_error.take())
        };
        if let Some(c) = inner {
            c.bind(py)
                .call_method1("connection_lost", (terminal_error.unwrap_or(exc),))?;
        }
        Ok(())
    }
}
