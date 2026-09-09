//! Experimental same-thread rustls transport. No Tokio tasks or Python plaintext buffers.
use std::cell::RefCell;
use std::io::{Read, Write};
use std::sync::Arc;

use pyo3::exceptions::{PyConnectionError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyBytes;

use super::client::NativeClient;

struct State {
    tls: rustls::ClientConnection,
    inner: Option<Py<NativeClient>>,
    transport: Option<Py<PyAny>>,
    plaintext: Vec<u8>,
    ciphertext: Vec<u8>,
    closing: bool,
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
                ciphertext: Vec::new(),
                closing: false,
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
        let (inner, transport) = {
            let mut st = self.state.borrow_mut();
            st.closing = true;
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
        let decoded: PyResult<(Vec<u8>, bool)> = (|| {
            let mut st = self.state.borrow_mut();
            let mut input = data;
            let mut plain = std::mem::take(&mut st.plaintext);
            plain.clear();
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
                match st.tls.reader().read_to_end(&mut plain) {
                    Ok(_) => (),
                    Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => (),
                    Err(e) => return Err(io_error(e)),
                }
            }
            Ok((plain, peer_closed))
        })();
        let (mut plain, peer_closed) = match decoded {
            Ok(v) => v,
            Err(e) => return self.fail(py, e),
        };
        self.flush(py)?;
        let inner = self.state.borrow().inner.as_ref().map(|c| c.clone_ref(py));
        if let Some(c) = inner {
            let c = c.borrow(py);
            if !plain.is_empty() {
                c.data_received_inner(py, &plain)?;
                c.flush_pending_callbacks(py)?;
            }
        }
        plain.clear();
        self.state.borrow_mut().plaintext = plain;
        if peer_closed {
            self.close(py)?;
        }
        Ok(())
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
        let inner = {
            let mut st = self.state.borrow_mut();
            st.closing = true;
            st.transport = None;
            st.inner.take()
        };
        if let Some(c) = inner {
            c.bind(py).call_method1("connection_lost", (exc,))?;
        }
        Ok(())
    }
}
