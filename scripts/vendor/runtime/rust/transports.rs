//! Transport selection and telemetry for ridl-generated clients.
//!
//! # Four avenues, one contract
//!
//! A web server here can reach an api server four ways: plain HTTP, a stateful
//! TCP connection, a websocket, and NATS. They are not interchangeable per
//! operation -- NATS has no path or query string, and only a framed transport
//! can stream -- so the route map states which apply and this module routes by
//! that declaration instead of every caller assuming HTTP.
//!
//! # Decoupling
//!
//! Two seams, both defined here, both implemented elsewhere:
//!
//! * [`Wire`] is one concrete transport. An HTTP client, a TCP connection pool,
//!   a websocket, or a NATS handle each implement it. This module never depends
//!   on any of them.
//! * [`TelemetrySink`] is where ores-otel attaches. It is a trait with a
//!   no-op default impl, so a build that does not want telemetry links nothing
//!   and pays nothing, and ores-otel never learns what RPC is.
//!
//! The same direction holds as for opto-sync: your crate depends on theirs,
//! never the reverse.

use std::fmt;

use crate::{OperationInfo, RpcRequest, RpcTransport, OPERATIONS};

/// One wire an operation can travel.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum TransportKind {
    Http,
    WebSocket,
    Tcp,
    Nats,
}

impl TransportKind {
    /// The spelling used in a route map, so a manifest entry can be matched
    /// without re-deriving the vocabulary in every caller.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Http => "http",
            Self::WebSocket => "websocket",
            Self::Tcp => "tcp",
            Self::Nats => "nats",
        }
    }

    #[must_use]
    pub fn parse(value: &str) -> Option<Self> {
        match value {
            "http" => Some(Self::Http),
            "websocket" => Some(Self::WebSocket),
            "tcp" => Some(Self::Tcp),
            "nats" => Some(Self::Nats),
            _ => None,
        }
    }

    /// Websocket and TCP carry the ridl frame envelope, so they can stream.
    #[must_use]
    pub const fn is_framed(self) -> bool {
        matches!(self, Self::WebSocket | Self::Tcp)
    }
}

impl fmt::Display for TransportKind {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// One concrete transport.
///
/// Implement it over `reqwest`, a pooled TCP connection, a websocket, or a NATS
/// client. `send` receives a request whose path, query and body the generated
/// code has already resolved, so a transport never re-derives a URL.
pub trait Wire {
    type Error;

    /// Which wire this is, so [`MultiTransport`] can match it against the
    /// operation's declared transports.
    fn kind(&self) -> TransportKind;

    /// Perform `request` and return the raw JSON response body.
    fn send(&self, request: &RpcRequest) -> Result<String, Self::Error>;

    /// Whether this wire is usable right now. A pooled TCP or websocket
    /// connection that has dropped answers `false`, and [`MultiTransport`]
    /// falls through to the next declared transport instead of erroring.
    fn is_ready(&self) -> bool {
        true
    }
}

/// Where ores-otel attaches.
///
/// Every method has a no-op default, so telemetry is genuinely optional: a
/// build with no sink links nothing. ores-otel implements this trait in its own
/// crate; nothing here knows about spans, exporters, or Supabase.
pub trait TelemetrySink {
    /// A call is about to go out. The returned value is handed back to
    /// [`Self::on_finish`], so an implementation can carry a span or a start
    /// instant without this module defining what either is.
    fn on_start(&self, _request: &RpcRequest, _transport: TransportKind) -> u64 {
        0
    }

    fn on_finish(&self, _request: &RpcRequest, _token: u64, _outcome: Outcome<'_>) {}

    /// A declared transport was skipped because it was not ready. Useful for
    /// noticing that a websocket has been flapping and every call is silently
    /// falling back to HTTP.
    fn on_fallback(&self, _request: &RpcRequest, _skipped: TransportKind) {}
}

/// What happened to a call, in terms this module can describe without knowing
/// the transport's error type.
#[derive(Clone, Copy, Debug)]
pub enum Outcome<'a> {
    Ok { bytes: usize },
    Failed { message: &'a str },
}

/// A sink that does nothing, for builds without telemetry.
#[derive(Clone, Copy, Debug, Default)]
pub struct NoTelemetry;

impl TelemetrySink for NoTelemetry {}

#[derive(Debug)]
pub enum TransportError<E> {
    Wire(E),
    /// Every transport the operation declares was unavailable.
    NoneAvailable {
        key: &'static str,
        declared: Vec<TransportKind>,
    },
    /// The operation is not in the generated manifest -- the generated code and
    /// the runtime are from different versions of the route map.
    UnknownOperation(&'static str),
}

impl<E: fmt::Debug> fmt::Display for TransportError<E> {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Wire(e) => write!(f, "transport: {e:?}"),
            Self::NoneAvailable { key, declared } => write!(
                f,
                "{key}: no declared transport is available (declared: {})",
                declared
                    .iter()
                    .map(ToString::to_string)
                    .collect::<Vec<_>>()
                    .join(", ")
            ),
            Self::UnknownOperation(key) => write!(
                f,
                "{key} is not in OPERATIONS -- generated client and runtime disagree"
            ),
        }
    }
}

/// Routes each call to the first declared transport that is ready.
///
/// Preference order is the caller's, not the route map's: the map says which
/// transports are *permitted*, the deployment says which is *preferred*. A web
/// server holding a warm TCP connection to the api server puts `Tcp` first and
/// keeps `Http` as the fallback, and neither choice is baked into the contract.
pub struct MultiTransport<W, T = NoTelemetry> {
    wires: Vec<W>,
    preference: Vec<TransportKind>,
    telemetry: T,
}

impl<W: Wire> MultiTransport<W, NoTelemetry> {
    pub fn new(wires: Vec<W>) -> Self {
        let preference = wires.iter().map(Wire::kind).collect();
        Self {
            wires,
            preference,
            telemetry: NoTelemetry,
        }
    }
}

impl<W: Wire, T: TelemetrySink> MultiTransport<W, T> {
    pub fn with_telemetry(wires: Vec<W>, telemetry: T) -> Self {
        let preference = wires.iter().map(Wire::kind).collect();
        Self {
            wires,
            preference,
            telemetry,
        }
    }

    /// Try transports in this order. Anything not listed is tried afterwards in
    /// registration order, so a partial preference is still complete.
    #[must_use]
    pub fn prefer(mut self, order: &[TransportKind]) -> Self {
        let mut preference: Vec<TransportKind> = order.to_vec();
        for wire in &self.wires {
            if !preference.contains(&wire.kind()) {
                preference.push(wire.kind());
            }
        }
        self.preference = preference;
        self
    }

    fn operation(key: &str) -> Option<&'static OperationInfo> {
        OPERATIONS.iter().find(|op| op.key == key)
    }

    fn declared(op: &OperationInfo) -> Vec<TransportKind> {
        op.transports
            .iter()
            .filter_map(|name| TransportKind::parse(name))
            .collect()
    }
}

impl<W: Wire, T: TelemetrySink> RpcTransport for MultiTransport<W, T> {
    type Error = TransportError<W::Error>;

    fn call(&self, request: RpcRequest) -> Result<String, Self::Error> {
        let op = Self::operation(request.key)
            .ok_or(TransportError::UnknownOperation(request.key))?;
        let declared = Self::declared(op);

        for kind in &self.preference {
            if !declared.contains(kind) {
                continue;
            }
            let Some(wire) = self.wires.iter().find(|w| w.kind() == *kind) else {
                continue;
            };
            if !wire.is_ready() {
                self.telemetry.on_fallback(&request, *kind);
                continue;
            }

            let token = self.telemetry.on_start(&request, *kind);
            return match wire.send(&request) {
                Ok(body) => {
                    self.telemetry
                        .on_finish(&request, token, Outcome::Ok { bytes: body.len() });
                    Ok(body)
                }
                Err(err) => {
                    let message = format!("{kind} wire failed");
                    self.telemetry.on_finish(
                        &request,
                        token,
                        Outcome::Failed { message: &message },
                    );
                    Err(TransportError::Wire(err))
                }
            };
        }

        Err(TransportError::NoneAvailable {
            key: request.key,
            declared,
        })
    }
}

/// The envelope framed transports put on the wire.
///
/// HTTP needs none -- method, path and body are already the envelope. TCP and
/// websocket do: several calls share one connection, so each frame carries the
/// operation key and a correlation id. This mirrors the shape opto-sync's own
/// realtime transport already uses (`{v, type, requestId, ...}`), so a
/// deployment that multiplexes both over one socket sees one convention.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Frame {
    pub version: u8,
    pub key: &'static str,
    pub request_id: String,
    pub method: &'static str,
    pub path: String,
    pub body: Option<String>,
}

impl Frame {
    pub const VERSION: u8 = 1;

    #[must_use]
    pub fn new(request: &RpcRequest, request_id: impl Into<String>) -> Self {
        Self {
            version: Self::VERSION,
            key: request.key,
            request_id: request_id.into(),
            method: request.method,
            path: request.path.clone(),
            body: request.body.clone(),
        }
    }

    /// Serialise without pulling in a JSON library: the shape is fixed and
    /// every field is either a known-safe literal or already-valid JSON.
    #[must_use]
    pub fn to_json(&self) -> String {
        let body = self.body.as_deref().unwrap_or("null");
        format!(
            r#"{{"v":{},"type":"call","requestId":{},"key":{},"method":{},"path":{},"body":{}}}"#,
            self.version,
            quote(&self.request_id),
            quote(self.key),
            quote(self.method),
            quote(&self.path),
            body,
        )
    }
}

fn quote(value: &str) -> String {
    let mut out = String::with_capacity(value.len() + 2);
    out.push('"');
    for ch in value.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::RefCell;

    struct Fake {
        kind: TransportKind,
        ready: bool,
    }

    impl Wire for Fake {
        type Error = String;
        fn kind(&self) -> TransportKind {
            self.kind
        }
        fn send(&self, _request: &RpcRequest) -> Result<String, String> {
            Ok(format!("\"{}\"", self.kind))
        }
        fn is_ready(&self) -> bool {
            self.ready
        }
    }

    #[derive(Default)]
    struct Recorder {
        fallbacks: RefCell<Vec<TransportKind>>,
        finished: RefCell<usize>,
    }

    impl TelemetrySink for Recorder {
        fn on_finish(&self, _r: &RpcRequest, _t: u64, _o: Outcome<'_>) {
            *self.finished.borrow_mut() += 1;
        }
        fn on_fallback(&self, _r: &RpcRequest, skipped: TransportKind) {
            self.fallbacks.borrow_mut().push(skipped);
        }
    }

    fn request(key: &'static str) -> RpcRequest {
        let op = OPERATIONS.iter().find(|o| o.key == key).expect("known op");
        RpcRequest {
            key: op.key,
            method: op.methods[0],
            path: op.path.to_string(),
            path_template: op.path,
            query: Vec::new(),
            body: None,
            delivery: op.delivery,
            opto_sync: None,
        }
    }

    #[test]
    fn a_transport_the_operation_does_not_declare_is_never_used() {
        // `get_matter` declares http only; a websocket is present but must be
        // skipped rather than silently used.
        let transport = MultiTransport::new(vec![
            Fake { kind: TransportKind::WebSocket, ready: true },
            Fake { kind: TransportKind::Http, ready: true },
        ]);
        assert_eq!(transport.call(request("get_matter")).unwrap(), "\"http\"");
    }

    #[test]
    fn an_unready_wire_falls_through_and_is_reported() {
        let recorder = Recorder::default();
        let transport = MultiTransport::with_telemetry(
            vec![
                Fake { kind: TransportKind::Tcp, ready: false },
                Fake { kind: TransportKind::Http, ready: true },
            ],
            recorder,
        )
        .prefer(&[TransportKind::Tcp, TransportKind::Http]);
        // `healthz` declares http, websocket and tcp.
        assert_eq!(transport.call(request("healthz")).unwrap(), "\"http\"");
        assert_eq!(
            *transport.telemetry.fallbacks.borrow(),
            vec![TransportKind::Tcp],
            "a skipped transport must be observable, not silent"
        );
        assert_eq!(*transport.telemetry.finished.borrow(), 1);
    }

    #[test]
    fn preference_picks_among_transports_the_map_permits() {
        let transport = MultiTransport::new(vec![
            Fake { kind: TransportKind::Http, ready: true },
            Fake { kind: TransportKind::Tcp, ready: true },
        ])
        .prefer(&[TransportKind::Tcp]);
        assert_eq!(transport.call(request("healthz")).unwrap(), "\"tcp\"");
    }

    #[test]
    fn no_available_transport_is_an_error_not_a_hang() {
        let transport = MultiTransport::new(vec![Fake {
            kind: TransportKind::Http,
            ready: false,
        }]);
        assert!(matches!(
            transport.call(request("get_matter")),
            Err(TransportError::NoneAvailable { .. })
        ));
    }

    #[test]
    fn a_frame_escapes_what_it_interpolates() {
        let mut req = request("get_matter");
        req.path = "/v1/matters/a\"b".into();
        let frame = Frame::new(&req, "r-1");
        assert!(frame.to_json().contains(r#""path":"/v1/matters/a\"b""#));
        assert!(frame.to_json().contains(r#""requestId":"r-1""#));
    }
}
