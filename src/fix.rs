//! Native FIXT.1.1 frame decoder exposed as `websocket_rs.fix`.
//!
//! The parser validates and walks the frame once in Rust, then crosses into
//! Python once with a complete immutable batch. Values use Latin-1 so every
//! wire byte maps losslessly to one Python code point without a UTF-8 failure
//! path or a downstream per-field decode.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::ffi::c_char;
use std::ptr;

use pyo3::exceptions::{PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyAny, PyBool, PyBytes, PyModule, PyString, PyTuple};
use serde::Deserialize;
use serde_json::value::RawValue;

const SOH: u8 = 1;
const MAX_FIX_FRAME_LEN: usize = 1 << 20;
const MAX_GROUP_ENTRIES: usize = 4096;
const MAX_FIELDS: usize = 16_384;

#[derive(Debug, Deserialize)]
struct KalshiWsEnvelope<'a> {
    #[serde(borrow, rename = "type")]
    message_type: Option<&'a str>,
    #[serde(borrow)]
    sid: Option<&'a RawValue>,
    #[serde(borrow)]
    seq: Option<&'a RawValue>,
    #[serde(borrow)]
    msg: Option<&'a RawValue>,
}

#[derive(Debug, Deserialize)]
struct KalshiWsMessage<'a> {
    #[serde(borrow)]
    market_ticker: Option<&'a str>,
    #[serde(borrow)]
    market_id: Option<&'a str>,
    #[serde(borrow)]
    yes_dollars_fp: Option<Vec<[&'a str; 2]>>,
    #[serde(borrow)]
    no_dollars_fp: Option<Vec<[&'a str; 2]>>,
    #[serde(borrow)]
    price_dollars: Option<&'a str>,
    #[serde(borrow)]
    delta_fp: Option<&'a str>,
    #[serde(borrow)]
    side: Option<&'a str>,
    #[serde(borrow)]
    ts_ms: Option<&'a RawValue>,
    #[serde(borrow)]
    ts: Option<&'a RawValue>,
}

#[derive(Debug, PartialEq, Eq)]
enum KalshiWsBook<'a> {
    Snapshot {
        sid: u64,
        sequence: u64,
        ticker: &'a str,
        market_id: &'a str,
        venue_timestamp: Option<String>,
        yes: Vec<(i64, i64)>,
        no: Vec<(i64, i64)>,
    },
    Delta {
        sid: u64,
        sequence: u64,
        ticker: &'a str,
        market_id: &'a str,
        venue_timestamp: Option<String>,
        side: &'a str,
        price: i64,
        delta: i64,
    },
}

#[derive(Debug)]
enum KalshiWsBookUpdate {
    Snapshot {
        yes: Vec<(i64, i64)>,
        no: Vec<(i64, i64)>,
    },
    Delta {
        side: String,
        price: i64,
        delta: i64,
    },
}

/// One Rust-owned Kalshi book frame decoded once at the socket boundary.
#[pyclass(module = "websocket_rs.fix", frozen)]
struct KalshiWsBookFrame {
    message_type: &'static str,
    sid: u64,
    sequence: u64,
    ticker: String,
    market_id: String,
    venue_timestamp: Option<String>,
    update: KalshiWsBookUpdate,
}

#[pymethods]
impl KalshiWsBookFrame {
    #[getter]
    fn message_type(&self) -> &'static str {
        self.message_type
    }

    #[getter]
    fn sid(&self) -> u64 {
        self.sid
    }

    #[getter]
    fn sequence(&self) -> u64 {
        self.sequence
    }

    #[getter]
    fn ticker(&self) -> &str {
        &self.ticker
    }

    #[getter]
    fn market_id(&self) -> &str {
        &self.market_id
    }

    #[getter]
    fn venue_timestamp(&self) -> Option<&str> {
        self.venue_timestamp.as_deref()
    }
}

#[derive(Debug)]
struct NativeKalshiBook {
    ticker: String,
    bids: BTreeMap<i64, i64>,
    asks: BTreeMap<i64, i64>,
}

/// Stateful fixed-point Kalshi book that retains full depth and publishes only N rows.
#[pyclass(module = "websocket_rs.fix")]
struct KalshiWsBookState {
    publication_depth: usize,
    use_yes_price: bool,
    enforce_sequence: bool,
    books: HashMap<String, NativeKalshiBook>,
    sequences: HashMap<u64, u64>,
}

#[pymethods]
impl KalshiWsBookState {
    #[new]
    #[pyo3(signature = (*, publication_depth, use_yes_price, enforce_sequence=false))]
    fn new(
        publication_depth: &Bound<'_, PyAny>,
        use_yes_price: bool,
        enforce_sequence: bool,
    ) -> PyResult<Self> {
        if publication_depth.is_instance_of::<PyBool>() {
            return Err(PyTypeError::new_err(
                "publication_depth must be a positive integer",
            ));
        }
        let publication_depth = publication_depth
            .extract::<usize>()
            .map_err(|_| PyTypeError::new_err("publication_depth must be a positive integer"))?;
        if publication_depth == 0 {
            return Err(PyValueError::new_err(
                "publication_depth must be a positive integer",
            ));
        }
        Ok(Self {
            publication_depth,
            use_yes_price,
            enforce_sequence,
            books: HashMap::new(),
            sequences: HashMap::new(),
        })
    }

    fn apply<'py>(
        &mut self,
        py: Python<'py>,
        frame: &Bound<'py, PyBytes>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let decoded = match decode_kalshi_ws_book(frame.as_bytes()) {
            Ok(decoded) => decoded,
            Err(error) => {
                self.reset();
                return Err(PyValueError::new_err(error));
            }
        };
        let Some(decoded) = decoded else {
            return Ok(py.None().into_bound(py));
        };
        let (sid, sequence, ticker, market_id) = match &decoded {
            KalshiWsBook::Snapshot {
                sid,
                sequence,
                ticker,
                market_id,
                ..
            }
            | KalshiWsBook::Delta {
                sid,
                sequence,
                ticker,
                market_id,
                ..
            } => (*sid, *sequence, *ticker, *market_id),
        };
        self.validate_sequence(sid, sequence)?;

        match decoded {
            KalshiWsBook::Snapshot { yes, no, .. } => {
                self.apply_snapshot(ticker, market_id, &yes, &no)?;
            }
            KalshiWsBook::Delta {
                side, price, delta, ..
            } => {
                self.apply_delta(ticker, market_id, side, price, delta)?;
            }
        }
        self.publication(py, market_id)
    }

    fn apply_decoded<'py>(
        &mut self,
        py: Python<'py>,
        frame: &KalshiWsBookFrame,
    ) -> PyResult<Bound<'py, PyAny>> {
        self.validate_sequence(frame.sid, frame.sequence)?;
        match &frame.update {
            KalshiWsBookUpdate::Snapshot { yes, no } => {
                self.apply_snapshot(&frame.ticker, &frame.market_id, yes, no)?;
            }
            KalshiWsBookUpdate::Delta { side, price, delta } => {
                self.apply_delta(&frame.ticker, &frame.market_id, side, *price, *delta)?;
            }
        }
        self.publication(py, &frame.market_id)
    }

    fn publication<'py>(&self, py: Python<'py>, market_id: &str) -> PyResult<Bound<'py, PyAny>> {
        let book = self
            .books
            .get(market_id)
            .expect("an accepted Kalshi update must leave a resident book");
        let bids = book
            .bids
            .iter()
            .rev()
            .take(self.publication_depth)
            .map(|(&price, &size)| (price, size))
            .collect::<Vec<_>>();
        let asks = book
            .asks
            .iter()
            .take(self.publication_depth)
            .map(|(&price, &size)| (price, size))
            .collect::<Vec<_>>();
        let values = [
            kalshi_ws_levels(py, &bids)?.into_any(),
            kalshi_ws_levels(py, &asks)?.into_any(),
        ];
        Ok(PyTuple::new(py, values)?.into_any())
    }

    fn reset(&mut self) {
        self.books.clear();
        self.sequences.clear();
    }

    fn baseline_ready(&self, market_id: &str) -> bool {
        self.books.contains_key(market_id)
            || self.books.values().any(|book| book.ticker == market_id)
    }

    fn invalidate_tickers(&mut self, tickers: HashSet<String>) {
        self.books
            .retain(|_, book| !tickers.contains(book.ticker.as_str()));
    }
}

impl KalshiWsBookState {
    fn validate_sequence(&mut self, sid: u64, sequence: u64) -> PyResult<()> {
        if !self.enforce_sequence {
            return Ok(());
        }
        if let Some(previous) = self.sequences.get(&sid) {
            if previous.checked_add(1) != Some(sequence) {
                let error = format!("Kalshi book sequence gap after {previous}");
                self.reset();
                return Err(PyValueError::new_err(error));
            }
        }
        self.sequences.insert(sid, sequence);
        Ok(())
    }

    fn apply_snapshot(
        &mut self,
        ticker: &str,
        market_id: &str,
        yes: &[(i64, i64)],
        no: &[(i64, i64)],
    ) -> PyResult<()> {
        let bids = yes
            .iter()
            .copied()
            .filter(|(_, size)| *size > 0)
            .collect::<BTreeMap<_, _>>();
        let asks = no
            .iter()
            .copied()
            .filter(|(_, size)| *size > 0)
            .map(|(price, size)| (self.ask_price(price), size))
            .collect::<BTreeMap<_, _>>();
        let book = NativeKalshiBook {
            ticker: ticker.to_owned(),
            bids,
            asks,
        };
        if Self::crossed(&book) {
            self.reset();
            return Err(PyValueError::new_err("Kalshi crossed book snapshot"));
        }
        self.books.insert(market_id.to_owned(), book);
        Ok(())
    }

    fn apply_delta(
        &mut self,
        ticker: &str,
        market_id: &str,
        side: &str,
        wire_price: i64,
        delta: i64,
    ) -> PyResult<()> {
        let ask_price = self.ask_price(wire_price);
        let Some(book) = self.books.get_mut(market_id) else {
            return Err(PyValueError::new_err(
                "Kalshi book delta requires a fresh snapshot",
            ));
        };
        if book.ticker != ticker {
            self.books.remove(market_id);
            return Err(PyValueError::new_err(
                "Kalshi book delta identity does not match its snapshot",
            ));
        }
        let (ladder, price) = if side == "yes" {
            (&mut book.bids, wire_price)
        } else {
            (&mut book.asks, ask_price)
        };
        let previous = ladder.get(&price).copied().unwrap_or_default();
        let Some(size) = previous.checked_add(delta) else {
            self.books.remove(market_id);
            return Err(PyValueError::new_err(
                "Kalshi book size is outside the supported range",
            ));
        };
        if size < 0 {
            self.books.remove(market_id);
            return Err(PyValueError::new_err("Kalshi book level became negative"));
        }
        if size == 0 {
            ladder.remove(&price);
        } else {
            ladder.insert(price, size);
        }
        if Self::crossed(book) {
            self.books.remove(market_id);
            return Err(PyValueError::new_err("Kalshi crossed book update"));
        }
        Ok(())
    }

    fn ask_price(&self, wire_price: i64) -> i64 {
        if self.use_yes_price {
            wire_price
        } else {
            10_000 - wire_price
        }
    }

    fn crossed(book: &NativeKalshiBook) -> bool {
        match (book.bids.last_key_value(), book.asks.first_key_value()) {
            (Some((&bid, _)), Some((&ask, _))) => bid > ask,
            _ => false,
        }
    }
}

fn parse_fixed(raw: &str, scale: u32, signed: bool, label: &str) -> Result<i64, String> {
    if raw.is_empty() {
        return Err(format!("{label} must be a fixed-point decimal string"));
    }
    let (negative, unsigned) = match raw.strip_prefix('-') {
        Some(value) if signed && !value.is_empty() => (true, value),
        Some(_) => return Err(format!("{label} must be non-negative")),
        None => (false, raw),
    };
    let mut parts = unsigned.split('.');
    let whole = parts.next().unwrap_or_default();
    let fraction = parts.next().unwrap_or_default();
    if raw.eq_ignore_ascii_case("infinity")
        || raw.eq_ignore_ascii_case("inf")
        || raw.eq_ignore_ascii_case("nan")
    {
        return Err(format!("{label} must be finite"));
    }
    if parts.next().is_some()
        || whole.is_empty()
        || !whole.bytes().all(|byte| byte.is_ascii_digit())
        || !fraction.bytes().all(|byte| byte.is_ascii_digit())
    {
        return Err(format!("{label} must be numeric"));
    }
    if fraction.len() > scale as usize {
        return Err(format!("{label} must have at most {scale} decimal places"));
    }
    let factor = 10_i64.pow(scale);
    let whole = whole
        .parse::<i64>()
        .map_err(|_| format!("{label} is outside the supported range"))?;
    let mut fractional = if fraction.is_empty() {
        0
    } else {
        fraction
            .parse::<i64>()
            .map_err(|_| format!("{label} is outside the supported range"))?
    };
    fractional *= 10_i64.pow(scale - fraction.len() as u32);
    let magnitude = whole
        .checked_mul(factor)
        .and_then(|value| value.checked_add(fractional))
        .ok_or_else(|| format!("{label} is outside the supported range"))?;
    if negative {
        magnitude
            .checked_neg()
            .ok_or_else(|| format!("{label} is outside the supported range"))
    } else {
        Ok(magnitude)
    }
}

fn required<T>(value: Option<T>, label: &str) -> Result<T, String> {
    value.ok_or_else(|| format!("Kalshi WebSocket book frame requires {label}"))
}

fn parse_ws_raw<'a, T>(value: Option<&'a RawValue>, label: &str) -> Result<T, String>
where
    T: Deserialize<'a>,
{
    let raw = required(value, label)?;
    serde_json::from_str(raw.get())
        .map_err(|error| format!("Kalshi WebSocket book frame has invalid {label}: {error}"))
}

fn kalshi_ws_timestamp(value: Option<&RawValue>) -> Option<String> {
    let raw = value?.get();
    if raw == "null" {
        return None;
    }
    serde_json::from_str::<String>(raw)
        .ok()
        .or_else(|| Some(raw.to_owned()))
}

fn decode_kalshi_ws_book(frame: &[u8]) -> Result<Option<KalshiWsBook<'_>>, String> {
    if frame.iter().find(|byte| !byte.is_ascii_whitespace()) != Some(&b'{') {
        serde_json::from_slice::<serde::de::IgnoredAny>(frame)
            .map_err(|error| format!("invalid Kalshi WebSocket JSON: {error}"))?;
        return Ok(None);
    }
    let envelope: KalshiWsEnvelope<'_> = serde_json::from_slice(frame)
        .map_err(|error| format!("invalid Kalshi WebSocket JSON: {error}"))?;
    let Some(message_type) = envelope.message_type else {
        return Ok(None);
    };
    if message_type != "orderbook_snapshot" && message_type != "orderbook_delta" {
        return Ok(None);
    }

    let sid = parse_ws_raw(envelope.sid, "sid")?;
    let sequence = parse_ws_raw(envelope.seq, "seq")?;
    let message: KalshiWsMessage<'_> = parse_ws_raw(envelope.msg, "msg")?;
    let ticker = required(message.market_ticker, "msg.market_ticker")?;
    let market_id = required(message.market_id, "msg.market_id")?;
    let venue_timestamp =
        kalshi_ws_timestamp(message.ts_ms).or_else(|| kalshi_ws_timestamp(message.ts));
    if sid == 0 || sequence == 0 {
        return Err("Kalshi WebSocket book sid and seq must be positive".into());
    }
    if ticker.is_empty() || market_id.is_empty() {
        return Err("Kalshi WebSocket book identity must be non-empty".into());
    }

    if message_type == "orderbook_snapshot" {
        let parse_levels = |levels: Vec<[&str; 2]>, side: &str| {
            levels
                .into_iter()
                .map(|[price, size]| {
                    let price = parse_fixed(price, 4, false, &format!("{side} price"))?;
                    if price > 10_000 {
                        return Err(format!("{side} price must be between 0 and 1"));
                    }
                    let size = parse_fixed(size, 2, false, &format!("{side} size"))?;
                    Ok((price, size))
                })
                .collect::<Result<Vec<_>, String>>()
        };
        return Ok(Some(KalshiWsBook::Snapshot {
            sid,
            sequence,
            ticker,
            market_id,
            venue_timestamp,
            yes: parse_levels(message.yes_dollars_fp.unwrap_or_default(), "yes")?,
            no: parse_levels(message.no_dollars_fp.unwrap_or_default(), "no")?,
        }));
    }

    let side = required(message.side, "msg.side")?;
    if side != "yes" && side != "no" {
        return Err("msg.side must be 'yes' or 'no'".into());
    }
    let price = parse_fixed(
        required(message.price_dollars, "msg.price_dollars")?,
        4,
        false,
        "msg.price_dollars",
    )?;
    if price > 10_000 {
        return Err("msg.price_dollars must be between 0 and 1".into());
    }
    let delta = parse_fixed(
        required(message.delta_fp, "msg.delta_fp")?,
        2,
        true,
        "msg.delta_fp",
    )?;
    Ok(Some(KalshiWsBook::Delta {
        sid,
        sequence,
        ticker,
        market_id,
        venue_timestamp,
        side,
        price,
        delta,
    }))
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct FieldRef<'a> {
    tag: u32,
    value: &'a [u8],
}

#[derive(Debug, PartialEq, Eq)]
struct Decoded<'a> {
    fields: Vec<FieldRef<'a>>,
    entry_fields: Vec<FieldRef<'a>>,
    entry_starts: Vec<usize>,
}

#[derive(Debug, PartialEq)]
struct KalshiBookEntry<'a> {
    action: Option<&'a [u8]>,
    symbol: Option<&'a [u8]>,
    entry_type: &'a [u8],
    price: f64,
    size: f64,
    date: &'a [u8],
    time: &'a [u8],
}

#[derive(Debug, PartialEq)]
struct KalshiBook<'a> {
    msg_type: &'a [u8],
    sequence: &'a [u8],
    snapshot_symbol: Option<&'a [u8]>,
    entries: Vec<KalshiBookEntry<'a>>,
}

impl<'a> Decoded<'a> {
    fn entry(&self, index: usize) -> &[FieldRef<'a>] {
        let start = self.entry_starts[index];
        let end = self
            .entry_starts
            .get(index + 1)
            .copied()
            .unwrap_or(self.entry_fields.len());
        &self.entry_fields[start..end]
    }
}

fn parse_tag(raw: &[u8]) -> Result<u32, String> {
    if raw.is_empty() || raw[0] == b'0' {
        return Err("malformed FIX tag: expected a positive canonical integer".into());
    }
    let mut value = 0u32;
    for byte in raw {
        if !byte.is_ascii_digit() {
            return Err("malformed FIX tag: expected decimal digits".into());
        }
        value = value
            .checked_mul(10)
            .and_then(|current| current.checked_add(u32::from(*byte - b'0')))
            .ok_or_else(|| "malformed FIX tag: integer overflow".to_owned())?;
    }
    Ok(value)
}

fn parse_decimal(raw: &[u8], label: &str) -> Result<usize, String> {
    if raw.is_empty() || (raw.len() > 1 && raw[0] == b'0') {
        return Err(format!(
            "malformed {label}: expected a canonical decimal integer"
        ));
    }
    let mut value = 0usize;
    for byte in raw {
        if !byte.is_ascii_digit() {
            return Err(format!("malformed {label}: expected decimal digits"));
        }
        value = value
            .checked_mul(10)
            .and_then(|current| current.checked_add(usize::from(*byte - b'0')))
            .ok_or_else(|| format!("malformed {label}: integer overflow"))?;
    }
    Ok(value)
}

fn parse_field(frame: &[u8], start: usize, limit: usize) -> Result<(FieldRef<'_>, usize), String> {
    let mut equals = None;
    let mut end = None;
    for (relative, byte) in frame[start..limit].iter().enumerate() {
        match *byte {
            b'=' if equals.is_none() => equals = Some(start + relative),
            SOH => {
                end = Some(start + relative);
                break;
            }
            _ => {}
        }
    }
    let end = end.ok_or_else(|| "incomplete FIX field: missing SOH terminator".to_owned())?;
    let equals = equals
        .filter(|position| *position < end)
        .ok_or_else(|| "malformed FIX field: missing '=' between tag and value".to_owned())?;
    let tag = parse_tag(&frame[start..equals])?;
    Ok((
        FieldRef {
            tag,
            value: &frame[equals + 1..end],
        },
        end + 1,
    ))
}

fn reject_duplicate(fields: &[FieldRef<'_>], tag: u32) -> Result<(), String> {
    if fields.iter().any(|field| field.tag == tag) {
        Err(format!("duplicate tag {tag}"))
    } else {
        Ok(())
    }
}

fn group_lead_tag(fields: &[FieldRef<'_>]) -> Result<u32, String> {
    let msg_type = fields
        .iter()
        .find(|field| field.tag == 35)
        .map(|field| field.value);
    match msg_type {
        Some(b"W") => Ok(269),
        Some(b"X") => Ok(279),
        _ => Err("tag 268 requires tag 35 MsgType W or X".into()),
    }
}

fn require_standard_header_field(
    fields: &[FieldRef<'_>],
    cursor: &mut usize,
    tag: u32,
    name: &str,
    optional_before: &[u32],
) -> Result<(), String> {
    let mut optional_cursor = 0;
    loop {
        let field = fields
            .get(*cursor)
            .ok_or_else(|| format!("FIX standard header requires tag {tag} {name}"))?;
        if field.tag == tag {
            if field.value.is_empty() {
                return Err(format!(
                    "FIX standard header requires non-empty tag {tag} {name}"
                ));
            }
            *cursor += 1;
            return Ok(());
        }

        let relative = optional_before[optional_cursor..]
            .iter()
            .position(|candidate| *candidate == field.tag)
            .ok_or_else(|| {
                format!(
                    "FIX standard header requires tag {tag} {name} before tag {}",
                    field.tag
                )
            })?;
        optional_cursor += relative + 1;
        *cursor += 1;
    }
}

fn validate_standard_header(fields: &[FieldRef<'_>]) -> Result<(), String> {
    let msg_type = fields
        .get(2)
        .ok_or_else(|| "FIX standard header requires tag 35 MsgType third".to_owned())?;
    if msg_type.tag != 35 {
        return Err("FIX standard header requires tag 35 MsgType third".into());
    }
    if msg_type.value.is_empty() {
        return Err("FIX standard header requires non-empty tag 35 MsgType".into());
    }

    let mut cursor = 3;
    require_standard_header_field(fields, &mut cursor, 49, "SenderCompID", &[1128, 1156, 1129])?;
    require_standard_header_field(fields, &mut cursor, 56, "TargetCompID", &[])?;
    require_standard_header_field(fields, &mut cursor, 34, "MsgSeqNum", &[115, 128, 90, 91])?;
    require_standard_header_field(
        fields,
        &mut cursor,
        52,
        "SendingTime",
        &[50, 142, 57, 143, 116, 144, 129, 145, 43, 97],
    )?;
    Ok(())
}

fn decode_frame(frame: &[u8]) -> Result<Decoded<'_>, String> {
    if frame.len() > MAX_FIX_FRAME_LEN {
        return Err(format!("FIX frame exceeds {MAX_FIX_FRAME_LEN} bytes"));
    }

    let (begin_string, after_begin) = parse_field(frame, 0, frame.len())
        .map_err(|error| format!("invalid tag 8 BeginString: {error}"))?;
    if begin_string.tag != 8 {
        return Err("FIX frame must start with tag 8 BeginString".into());
    }
    if begin_string.value != b"FIXT.1.1" {
        return Err("tag 8 BeginString must be FIXT.1.1".into());
    }

    let (body_length, body_start) = parse_field(frame, after_begin, frame.len())
        .map_err(|error| format!("invalid tag 9 BodyLength: {error}"))?;
    if body_length.tag != 9 {
        return Err("tag 9 BodyLength must immediately follow tag 8".into());
    }
    let body_len = parse_decimal(body_length.value, "tag 9 BodyLength")?;
    let trailer_start = body_start
        .checked_add(body_len)
        .ok_or_else(|| "tag 9 BodyLength overflows frame size".to_owned())?;
    if trailer_start.checked_add(7) != Some(frame.len()) {
        return Err("tag 9 BodyLength does not match the complete FIX frame".into());
    }
    if &frame[trailer_start..trailer_start + 3] != b"10=" || frame[trailer_start + 6] != SOH {
        return Err("invalid checksum trailer: expected final 10=NNN<SOH>".into());
    }
    let checksum_raw = &frame[trailer_start + 3..trailer_start + 6];
    if !checksum_raw.iter().all(u8::is_ascii_digit) {
        return Err("invalid checksum trailer: expected exactly three digits".into());
    }
    let expected_checksum = u16::from(checksum_raw[0] - b'0') * 100
        + u16::from(checksum_raw[1] - b'0') * 10
        + u16::from(checksum_raw[2] - b'0');
    if expected_checksum > 255 {
        return Err("invalid checksum trailer: value exceeds 255".into());
    }
    let actual_checksum = frame[..trailer_start]
        .iter()
        .fold(0u8, |sum, byte| sum.wrapping_add(*byte));
    if actual_checksum != expected_checksum as u8 {
        return Err(format!(
            "checksum mismatch: expected {expected_checksum:03}, calculated {actual_checksum:03}"
        ));
    }

    let mut fields = Vec::with_capacity(16);
    fields.push(begin_string);
    fields.push(body_length);
    let mut entry_fields = Vec::new();
    let mut entry_starts = Vec::new();
    let mut group = None;
    let mut position = body_start;
    let mut parsed_fields = 2usize;

    while position < trailer_start {
        parsed_fields += 1;
        if parsed_fields > MAX_FIELDS {
            return Err(format!("FIX frame exceeds {MAX_FIELDS} fields"));
        }
        let (field, next) = parse_field(frame, position, trailer_start)?;
        position = next;

        if matches!(field.tag, 8..=10) {
            return Err(format!(
                "framing tag {} is not allowed in the FIX body",
                field.tag
            ));
        }

        match group {
            None => {
                if matches!(field.tag, 269 | 279) {
                    return Err(format!(
                        "repeating entry lead tag {} appears without tag 268",
                        field.tag
                    ));
                }
                reject_duplicate(&fields, field.tag)?;
                fields.push(field);
                if field.tag == 268 {
                    let count = parse_decimal(field.value, "tag 268 count")?;
                    if count > MAX_GROUP_ENTRIES {
                        return Err(format!(
                            "tag 268 count {count} exceeds maximum {MAX_GROUP_ENTRIES}"
                        ));
                    }
                    let lead_tag = group_lead_tag(&fields)?;
                    entry_starts.reserve(count);
                    group = Some((count, lead_tag));
                }
            }
            Some((expected, lead_tag)) => {
                if field.tag == 268 {
                    return Err(
                        "tag 268 may appear only once and before all repeating entries".into(),
                    );
                }
                if field.tag == lead_tag {
                    if entry_starts.len() == expected {
                        return Err(format!(
                            "tag 268 expected {expected} entries but found more"
                        ));
                    }
                    entry_starts.push(entry_fields.len());
                    entry_fields.push(field);
                } else {
                    let start = entry_starts.last().copied().ok_or_else(|| {
                        format!("tag 268 entries for this MsgType must begin with tag {lead_tag}")
                    })?;
                    reject_duplicate(&entry_fields[start..], field.tag)?;
                    entry_fields.push(field);
                }
            }
        }
    }

    if position != trailer_start {
        return Err("tag 9 BodyLength ends in the middle of a FIX field".into());
    }
    if let Some((expected, _)) = group {
        if entry_starts.len() != expected {
            return Err(format!(
                "tag 268 expected {expected} entries but found {}",
                entry_starts.len()
            ));
        }
    }
    validate_standard_header(&fields)?;
    fields.push(FieldRef {
        tag: 10,
        value: checksum_raw,
    });
    Ok(Decoded {
        fields,
        entry_fields,
        entry_starts,
    })
}

fn required_value<'a>(fields: &'a [FieldRef<'a>], tag: u32) -> Result<&'a [u8], String> {
    let value = fields
        .iter()
        .find(|field| field.tag == tag)
        .map(|field| field.value)
        .ok_or_else(|| format!("Kalshi FIX requires tag {tag}"))?;
    if value.is_empty() {
        return Err(format!("Kalshi FIX requires non-empty tag {tag}"));
    }
    Ok(value)
}

fn fix_number(raw: &[u8], tag: u32) -> Result<f64, String> {
    let unsigned = match raw.first() {
        Some(b'+' | b'-') => &raw[1..],
        _ => raw,
    };
    let mut saw_digit = false;
    let mut saw_decimal = false;
    for byte in unsigned {
        match *byte {
            b'0'..=b'9' => saw_digit = true,
            b'.' if !saw_decimal => saw_decimal = true,
            _ => {
                return Err(format!(
                    "Kalshi FIX tag {tag} must use numeric FIX decimal syntax"
                ));
            }
        }
    }
    if !saw_digit {
        return Err(format!(
            "Kalshi FIX tag {tag} must use numeric FIX decimal syntax"
        ));
    }
    let text = std::str::from_utf8(raw)
        .map_err(|_| format!("Kalshi FIX tag {tag} must be numeric ASCII"))?;
    let value = text
        .parse::<f64>()
        .map_err(|_| format!("Kalshi FIX tag {tag} must be numeric"))?;
    if !value.is_finite() {
        return Err(format!("Kalshi FIX tag {tag} must be finite"));
    }
    Ok(value)
}

fn parse_kalshi_book<'a>(decoded: &'a Decoded<'a>) -> Result<KalshiBook<'a>, String> {
    let msg_type = required_value(&decoded.fields, 35)?;
    if !matches!(msg_type, b"W" | b"X") {
        return Err("Kalshi book decoder requires MsgType W or X".into());
    }
    let sequence = required_value(&decoded.fields, 34)?;
    required_value(&decoded.fields, 268)?;
    let incremental = msg_type == b"X";
    let snapshot_symbol = if incremental {
        None
    } else {
        Some(required_value(&decoded.fields, 55)?)
    };

    let mut entries = Vec::with_capacity(decoded.entry_starts.len());
    for index in 0..decoded.entry_starts.len() {
        let fields = decoded.entry(index);
        let entry_type = required_value(fields, 269)?;
        let allowed_type = matches!(entry_type, b"0" | b"1") || incremental && entry_type == b"2";
        if !allowed_type {
            return Err(format!(
                "unsupported Kalshi FIX MDEntryType<269> {:?}",
                String::from_utf8_lossy(entry_type)
            ));
        }
        let (action, symbol) = if incremental {
            let action = required_value(fields, 279)?;
            if !matches!(action, b"0" | b"1" | b"2") {
                return Err(format!(
                    "unsupported Kalshi FIX MDUpdateAction<279> {:?}",
                    String::from_utf8_lossy(action)
                ));
            }
            (Some(action), Some(required_value(fields, 55)?))
        } else {
            (None, None)
        };
        let price = fix_number(required_value(fields, 270)?, 270)?;
        let size = fix_number(required_value(fields, 271)?, 271)?;
        if !(0.0..=1.0).contains(&price) {
            return Err("Kalshi FIX MDEntryPx<270> must be in [0, 1]".into());
        }
        if size < 0.0 {
            return Err("Kalshi FIX MDEntrySize<271> must be non-negative".into());
        }
        if matches!(entry_type, b"0" | b"1") && action != Some(b"2") && size == 0.0 {
            return Err("Kalshi FIX active book levels require positive size".into());
        }
        entries.push(KalshiBookEntry {
            action,
            symbol,
            entry_type,
            price,
            size,
            date: required_value(fields, 272)?,
            time: required_value(fields, 273)?,
        });
    }

    Ok(KalshiBook {
        msg_type,
        sequence,
        snapshot_symbol,
        entries,
    })
}

fn latin1<'py>(py: Python<'py>, value: &[u8]) -> PyResult<Bound<'py, PyString>> {
    // SAFETY: `value` is valid for `value.len()` bytes during the call; CPython
    // copies it into a new Unicode object. Null is converted into the active
    // Python exception, and the returned owned reference is adopted by Bound.
    let raw = unsafe {
        pyo3::ffi::PyUnicode_DecodeLatin1(
            value.as_ptr().cast::<c_char>(),
            value.len() as pyo3::ffi::Py_ssize_t,
            ptr::null(),
        )
    };
    let object = unsafe { Bound::from_owned_ptr_or_err(py, raw)? };
    Ok(object.cast_into::<PyString>()?)
}

fn field_batch<'py>(py: Python<'py>, fields: &[FieldRef<'_>]) -> PyResult<Bound<'py, PyTuple>> {
    let mut result = Vec::with_capacity(fields.len());
    for field in fields {
        let tag = field.tag.into_pyobject(py)?.into_any();
        let value = latin1(py, field.value)?.into_any();
        result.push(PyTuple::new(py, [tag, value])?);
    }
    PyTuple::new(py, result)
}

fn optional_latin1<'py>(py: Python<'py>, value: Option<&[u8]>) -> PyResult<Bound<'py, PyAny>> {
    match value {
        Some(value) => Ok(latin1(py, value)?.into_any()),
        None => Ok(py.None().into_bound(py)),
    }
}

/// Decode one complete FIXT.1.1 frame into `(fields, entries)`.
///
/// `fields` and every entry are ordered tuples of `(int, str)` pairs. Values
/// use Latin-1's one-byte mapping. Tag 268 remains in `fields`; its entry fields
/// are returned in `entries`. Malformed framing, counts, tags, and duplicates
/// raise `ValueError`.
#[pyfunction(name = "decode")]
#[pyo3(signature = (frame, /))]
fn decode_py<'py>(py: Python<'py>, frame: &Bound<'py, PyBytes>) -> PyResult<Bound<'py, PyTuple>> {
    let decoded = decode_frame(frame.as_bytes()).map_err(PyValueError::new_err)?;
    let fields = field_batch(py, &decoded.fields)?.into_any();
    let mut entry_batches = Vec::with_capacity(decoded.entry_starts.len());
    for index in 0..decoded.entry_starts.len() {
        entry_batches.push(field_batch(py, decoded.entry(index))?);
    }
    let entries = PyTuple::new(py, entry_batches)?.into_any();
    PyTuple::new(py, [fields, entries])
}

/// Decode a validated Kalshi W/X frame into book-ready typed values.
///
/// Returns `(msg_type, sequence, snapshot_symbol, entries)`. Each immutable
/// entry is `(action, symbol, type, price, size, date, time)`, with price and
/// size converted to native Python floats in Rust. W entries use `None` for
/// action and symbol; X entries require both values.
#[pyfunction(name = "decode_kalshi_book")]
#[pyo3(signature = (frame, /))]
fn decode_kalshi_book_py<'py>(
    py: Python<'py>,
    frame: &Bound<'py, PyBytes>,
) -> PyResult<Bound<'py, PyTuple>> {
    let decoded = decode_frame(frame.as_bytes()).map_err(PyValueError::new_err)?;
    let book = parse_kalshi_book(&decoded).map_err(PyValueError::new_err)?;
    let mut entries = Vec::with_capacity(book.entries.len());
    for entry in &book.entries {
        entries.push(PyTuple::new(
            py,
            [
                optional_latin1(py, entry.action)?,
                optional_latin1(py, entry.symbol)?,
                latin1(py, entry.entry_type)?.into_any(),
                entry.price.into_pyobject(py)?.into_any(),
                entry.size.into_pyobject(py)?.into_any(),
                latin1(py, entry.date)?.into_any(),
                latin1(py, entry.time)?.into_any(),
            ],
        )?);
    }
    let entries = PyTuple::new(py, entries)?.into_any();
    PyTuple::new(
        py,
        [
            latin1(py, book.msg_type)?.into_any(),
            latin1(py, book.sequence)?.into_any(),
            optional_latin1(py, book.snapshot_symbol)?,
            entries,
        ],
    )
}

fn kalshi_ws_levels<'py>(py: Python<'py>, levels: &[(i64, i64)]) -> PyResult<Bound<'py, PyTuple>> {
    let mut rows = Vec::with_capacity(levels.len());
    for (price, size) in levels {
        rows.push(PyTuple::new(
            py,
            [
                price.into_pyobject(py)?.into_any(),
                size.into_pyobject(py)?.into_any(),
            ],
        )?);
    }
    PyTuple::new(py, rows)
}

/// Decode a Kalshi WebSocket fixed-point order-book frame.
///
/// Valid non-book JSON returns `None`. Book frames are projected into one
/// immutable tuple with prices scaled by 10,000 and sizes scaled by 100, so
/// callers can update ladders without Python JSON traversal or Decimal parsing.
#[pyfunction(name = "decode_kalshi_ws_book")]
#[pyo3(signature = (frame, /))]
fn decode_kalshi_ws_book_py<'py>(
    py: Python<'py>,
    frame: &Bound<'py, PyBytes>,
) -> PyResult<Bound<'py, PyAny>> {
    let Some(book) = decode_kalshi_ws_book(frame.as_bytes()).map_err(PyValueError::new_err)? else {
        return Ok(py.None().into_bound(py));
    };
    let empty = || PyTuple::empty(py).into_any();
    let none = || py.None().into_bound(py);
    let values = match book {
        KalshiWsBook::Snapshot {
            sid,
            sequence,
            ticker,
            market_id,
            yes,
            no,
            venue_timestamp: _,
        } => [
            PyString::new(py, "orderbook_snapshot").into_any(),
            sid.into_pyobject(py)?.into_any(),
            sequence.into_pyobject(py)?.into_any(),
            PyString::new(py, ticker).into_any(),
            PyString::new(py, market_id).into_any(),
            kalshi_ws_levels(py, &yes)?.into_any(),
            kalshi_ws_levels(py, &no)?.into_any(),
            none(),
            none(),
            none(),
        ],
        KalshiWsBook::Delta {
            sid,
            sequence,
            ticker,
            market_id,
            side,
            price,
            delta,
            venue_timestamp: _,
        } => [
            PyString::new(py, "orderbook_delta").into_any(),
            sid.into_pyobject(py)?.into_any(),
            sequence.into_pyobject(py)?.into_any(),
            PyString::new(py, ticker).into_any(),
            PyString::new(py, market_id).into_any(),
            empty(),
            empty(),
            PyString::new(py, side).into_any(),
            price.into_pyobject(py)?.into_any(),
            delta.into_pyobject(py)?.into_any(),
        ],
    };
    Ok(PyTuple::new(py, values)?.into_any())
}

/// Decode one Kalshi book frame into a Rust-owned object reusable by book state.
#[pyfunction]
#[pyo3(signature = (frame, /))]
fn decode_kalshi_ws_book_frame(frame: &Bound<'_, PyBytes>) -> PyResult<Option<KalshiWsBookFrame>> {
    let bytes = frame.as_bytes();
    if !bytes
        .windows(b"orderbook_snapshot".len())
        .any(|window| window == b"orderbook_snapshot")
        && !bytes
            .windows(b"orderbook_delta".len())
            .any(|window| window == b"orderbook_delta")
    {
        return Ok(None);
    }
    let Some(book) = decode_kalshi_ws_book(bytes).map_err(PyValueError::new_err)? else {
        return Ok(None);
    };
    Ok(Some(match book {
        KalshiWsBook::Snapshot {
            sid,
            sequence,
            ticker,
            market_id,
            venue_timestamp,
            yes,
            no,
        } => KalshiWsBookFrame {
            message_type: "orderbook_snapshot",
            sid,
            sequence,
            ticker: ticker.to_owned(),
            market_id: market_id.to_owned(),
            venue_timestamp,
            update: KalshiWsBookUpdate::Snapshot { yes, no },
        },
        KalshiWsBook::Delta {
            sid,
            sequence,
            ticker,
            market_id,
            venue_timestamp,
            side,
            price,
            delta,
        } => KalshiWsBookFrame {
            message_type: "orderbook_delta",
            sid,
            sequence,
            ticker: ticker.to_owned(),
            market_id: market_id.to_owned(),
            venue_timestamp,
            update: KalshiWsBookUpdate::Delta {
                side: side.to_owned(),
                price,
                delta,
            },
        },
    }))
}

pub fn register_fix(py: Python<'_>, parent: &Bound<'_, PyModule>) -> PyResult<()> {
    let module = PyModule::new(py, "fix")?;
    module.add_class::<KalshiWsBookFrame>()?;
    module.add_class::<KalshiWsBookState>()?;
    module.add_function(wrap_pyfunction!(decode_py, &module)?)?;
    module.add_function(wrap_pyfunction!(decode_kalshi_book_py, &module)?)?;
    module.add_function(wrap_pyfunction!(decode_kalshi_ws_book_py, &module)?)?;
    module.add_function(wrap_pyfunction!(decode_kalshi_ws_book_frame, &module)?)?;
    parent.add_submodule(&module)?;
    py.import("sys")?
        .getattr("modules")?
        .set_item("websocket_rs.fix", &module)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::{decode_frame, parse_kalshi_book, FieldRef, SOH};

    fn raw_frame(body: &[u8]) -> Vec<u8> {
        let mut message = format!("8=FIXT.1.1\x019={}\x01", body.len()).into_bytes();
        message.extend_from_slice(body);
        let checksum = message
            .iter()
            .fold(0u8, |sum, byte| sum.wrapping_add(*byte));
        message.extend_from_slice(format!("10={checksum:03}\x01").as_bytes());
        message
    }

    fn frame(body: &[u8]) -> Vec<u8> {
        let msg_type_end = body
            .iter()
            .position(|byte| *byte == SOH)
            .expect("test body must contain MsgType")
            + 1;
        assert!(body.starts_with(b"35="));
        let mut standard_body = body[..msg_type_end].to_vec();
        standard_body
            .extend_from_slice(b"49=KALSHI\x0156=CLIENT\x0134=1\x0152=20260926-21:00:00.000\x01");
        standard_body.extend_from_slice(&body[msg_type_end..]);
        raw_frame(&standard_body)
    }

    fn values(fields: &[FieldRef<'_>]) -> Vec<(u32, Vec<u8>)> {
        fields
            .iter()
            .map(|field| (field.tag, field.value.to_vec()))
            .collect()
    }

    #[test]
    fn decodes_scalars_and_repeating_entries() {
        let message =
            frame(b"35=W\x0155=FED-26\x01268=2\x01269=0\x01270=.41\x01269=1\x01270=.59\x01");
        let decoded = decode_frame(&message).unwrap();
        assert_eq!(
            values(&decoded.fields[2..decoded.fields.len() - 1]),
            vec![
                (35, b"W".to_vec()),
                (49, b"KALSHI".to_vec()),
                (56, b"CLIENT".to_vec()),
                (34, b"1".to_vec()),
                (52, b"20260926-21:00:00.000".to_vec()),
                (55, b"FED-26".to_vec()),
                (268, b"2".to_vec())
            ]
        );
        assert_eq!(
            values(decoded.entry(0)),
            vec![(269, b"0".to_vec()), (270, b".41".to_vec())]
        );
        assert_eq!(
            values(decoded.entry(1)),
            vec![(269, b"1".to_vec()), (270, b".59".to_vec())]
        );
    }

    #[test]
    fn decodes_incremental_entries_led_by_279() {
        let message = frame(
            b"35=X\x01268=2\x01279=0\x0155=FED-26\x01269=0\x01270=.41\x01\
              279=1\x0155=FED-26\x01269=1\x01270=.59\x01",
        );
        let decoded = decode_frame(&message).unwrap();
        assert_eq!(
            values(decoded.entry(0)),
            vec![
                (279, b"0".to_vec()),
                (55, b"FED-26".to_vec()),
                (269, b"0".to_vec()),
                (270, b".41".to_vec())
            ]
        );
        assert_eq!(
            values(decoded.entry(1)),
            vec![
                (279, b"1".to_vec()),
                (55, b"FED-26".to_vec()),
                (269, b"1".to_vec()),
                (270, b".59".to_vec())
            ]
        );
    }

    #[test]
    fn projects_book_ready_snapshot_and_incremental_values() {
        let snapshot = frame(
            b"35=W\x0155=FED-26\x01268=1\x01269=0\x01270=.41\x01271=17.5\x01\
              272=20260926\x01273=21:00:00.123\x01",
        );
        let decoded = decode_frame(&snapshot).unwrap();
        let book = parse_kalshi_book(&decoded).unwrap();
        assert_eq!(book.msg_type, b"W");
        assert_eq!(book.sequence, b"1");
        assert_eq!(book.snapshot_symbol, Some(&b"FED-26"[..]));
        assert_eq!(book.entries[0].price, 0.41);
        assert_eq!(book.entries[0].size, 17.5);

        let incremental = frame(
            b"35=X\x01268=2\x01279=1\x0155=FED-26\x01269=0\x01270=.41\x01271=18\x01\
              272=20260926\x01273=21:00:01.123\x01279=2\x0155=OTHER-26\x01269=1\x01270=.59\x01\
              271=0\x01272=20260926\x01273=21:00:01.124\x01",
        );
        let decoded = decode_frame(&incremental).unwrap();
        let book = parse_kalshi_book(&decoded).unwrap();
        assert_eq!(book.msg_type, b"X");
        assert_eq!(book.snapshot_symbol, None);
        assert_eq!(book.entries.len(), 2);
        assert_eq!(book.entries[0].action, Some(&b"1"[..]));
        assert_eq!(book.entries[0].symbol, Some(&b"FED-26"[..]));
        assert_eq!(book.entries[1].action, Some(&b"2"[..]));
        assert_eq!(book.entries[1].size, 0.0);
    }

    #[test]
    fn book_projection_rejects_invalid_numeric_values() {
        for (value, needle) in [
            (&b"nan"[..], "numeric"),
            (&b"5e-1"[..], "numeric"),
            (&b"1.01"[..], "[0, 1]"),
            (&b"not-a-number"[..], "numeric"),
        ] {
            let mut body = b"35=W\x0155=FED\x01268=1\x01269=0\x01270=".to_vec();
            body.extend_from_slice(value);
            body.extend_from_slice(b"\x01271=1\x01272=20260926\x01273=00:00:00.000\x01");
            let message = frame(&body);
            let decoded = decode_frame(&message).unwrap();
            assert!(parse_kalshi_book(&decoded).unwrap_err().contains(needle));
        }
    }

    #[test]
    fn requires_ordered_standard_header_fields() {
        for body in [
            &b"49=KALSHI\x0135=W\x0156=CLIENT\x0134=1\x0152=20260926-21:00:00.000\x01"[..],
            &b"35=W\x0156=CLIENT\x0134=1\x0152=20260926-21:00:00.000\x01"[..],
            &b"35=W\x0149=KALSHI\x0134=1\x0152=20260926-21:00:00.000\x01"[..],
            &b"35=W\x0156=CLIENT\x0149=KALSHI\x0134=1\x0152=20260926-21:00:00.000\x01"[..],
        ] {
            assert!(decode_frame(&raw_frame(body))
                .unwrap_err()
                .contains("standard header"));
        }
    }

    #[test]
    fn rejects_body_fields_before_standard_header_completion() {
        for body in [
            &b"35=W\x0155=FED\x0149=KALSHI\x0156=CLIENT\x0134=1\x0152=20260926-21:00:00.000\x01"[..],
            &b"35=W\x0149=KALSHI\x0155=FED\x0156=CLIENT\x0134=1\x0152=20260926-21:00:00.000\x01"[..],
            &b"35=W\x0149=KALSHI\x0156=CLIENT\x0134=1\x0155=FED\x0152=20260926-21:00:00.000\x01"[..],
        ] {
            assert!(decode_frame(&raw_frame(body))
                .unwrap_err()
                .contains("standard header"));
        }
    }

    #[test]
    fn validates_body_length_and_checksum() {
        let mut wrong_length = frame(b"35=0\x01");
        let length = wrong_length
            .windows(2)
            .position(|window| window == b"9=")
            .unwrap()
            + 2;
        wrong_length[length] = b'4';
        assert!(decode_frame(&wrong_length)
            .unwrap_err()
            .contains("BodyLength"));

        let mut wrong_checksum = frame(b"35=0\x01");
        let index = wrong_checksum.len() - 2;
        wrong_checksum[index] = if wrong_checksum[index] == b'9' {
            b'0'
        } else {
            wrong_checksum[index] + 1
        };
        assert!(decode_frame(&wrong_checksum)
            .unwrap_err()
            .contains("checksum"));
    }

    #[test]
    fn rejects_duplicate_scalar_and_entry_tags() {
        let scalar = frame(b"35=W\x0135=X\x01");
        assert!(decode_frame(&scalar)
            .unwrap_err()
            .contains("duplicate tag 35"));

        let entry = frame(b"35=W\x01268=1\x01269=0\x01270=.4\x01270=.5\x01");
        assert!(decode_frame(&entry)
            .unwrap_err()
            .contains("duplicate tag 270"));

        let incremental = frame(b"35=X\x01268=1\x01279=0\x0155=A\x0155=B\x01");
        assert!(decode_frame(&incremental)
            .unwrap_err()
            .contains("duplicate tag 55"));
    }

    #[test]
    fn rejects_malformed_group_count_and_boundaries() {
        for (body, needle) in [
            (&b"35=W\x01268=2\x01269=0\x01"[..], "expected 2"),
            (&b"35=X\x01268=2\x01279=0\x01269=0\x01"[..], "expected 2"),
            (&b"35=W\x01268=1\x01270=.4\x01"[..], "begin with tag 269"),
            (&b"35=X\x01268=1\x01269=0\x01"[..], "begin with tag 279"),
            (&b"35=W\x01268=1\x01279=0\x01"[..], "begin with tag 269"),
            (&b"35=0\x01268=0\x01"[..], "MsgType W or X"),
            (&b"35=W\x01269=0\x01"[..], "without tag 268"),
        ] {
            assert!(decode_frame(&frame(body)).unwrap_err().contains(needle));
        }
    }

    #[test]
    fn rejects_noncanonical_tags() {
        for body in [
            &b"35=W\x0101=x\x01"[..],
            &b"35=W\x01A=x\x01"[..],
            &b"35=W\x010=x\x01"[..],
        ] {
            assert!(decode_frame(&frame(body)).unwrap_err().contains("tag"));
        }
    }

    #[test]
    fn checksum_covers_every_byte_before_trailer() {
        let mut message = frame(b"35=0\x01112=request=42\x01");
        let value = message
            .windows(3)
            .position(|window| window == b"=42")
            .unwrap()
            + 1;
        message[value] = b'5';
        assert!(decode_frame(&message).unwrap_err().contains("checksum"));
    }

    #[test]
    fn soh_constant_matches_wire_format() {
        assert_eq!(SOH, b'\x01');
    }
}
