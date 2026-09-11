//! `RpcTransport` over HTTP, WebSocket and TCP.
//!
//! Generated code produces an `RpcRequest` that already carries everything any
//! of the three needs -- key, method, substituted path, template, query, body,
//! delivery. So the transport choice is genuinely a choice at the edge: the
//! same generated call works over all three without regeneration.
//!
//! * **HTTP** needs no envelope. [`HttpCall`] is one method wide; back it with
//!   `reqwest`, `ureq`, or whatever the app already has.
//! * **WebSocket and TCP** carry the [`Frame`] envelope. [`FramedConnection`]
//!   is also one method wide -- send a call frame, return the frames that
//!   answer it -- so multiplexing, reconnect, and auth stay the app's, and
//!   this module stays testable without a socket.
//!
//! Streaming (`server_stream`, `client_stream`, `bidi` in the route map) is
//! declared in the contract and validated, but generated clients do not yet
//! expose it: that needs the emitters to produce a stream-returning signature.
//! [`FramedStream`] is the seam it will land on. Unary works today.

use std::sync::Arc;
use std::time::Instant;

use crate::frame::{Correlator, Frame, FrameKind};
use crate::telemetry::{emit, Carrier, Outcome, RpcEvent, RpcTelemetrySink};
use crate::{Delivery, RpcRequest, RpcTransport};

/// A plain online request. The app owns URLs, auth, retries and TLS.
pub trait HttpCall {
    type Error;
    /// Perform `request` and return the raw JSON response body.
    fn call(&self, request: &RpcRequest) -> Result<String, Self::Error>;
}

/// One framed exchange: send the call, return the frames that answer it.
///
/// A unary answer is one `data` then `end`, or a single `error`. The
/// implementation owns the socket, correlation-id demultiplexing, reconnect
/// and backoff -- none of which belongs in generated-code-adjacent glue.
pub trait FramedConnection {
    type Error;
    fn exchange(&self, call: Frame) -> Result<Vec<Frame>, Self::Error>;
    /// Which of the two framed transports this is. Telemetry only.
    fn carrier(&self) -> Carrier;
}

/// The seam a streaming client will use once the emitters produce one.
pub trait FramedStream {
    type Error;
    fn open(&self, call: Frame) -> Result<Box<dyn Iterator<Item = Result<Frame, Self::Error>>>, Self::Error>;
}

#[derive(Debug)]
pub enum TransportError<E> {
    /// The call never reached a peer.
    Carrier(E),
    /// The peer answered, and the answer was a failure.
    Remote { code: String, message: Option<String> },
    /// The peer answered in a shape the contract does not allow.
    Protocol(String),
}

impl<E: std::fmt::Debug> std::fmt::Display for TransportError<E> {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Carrier(e) => write!(f, "carrier: {e:?}"),
            Self::Remote { code, message } => match message {
                Some(m) => write!(f, "remote {code}: {m}"),
                None => write!(f, "remote {code}"),
            },
            Self::Protocol(why) => write!(f, "protocol: {why}"),
        }
    }
}

fn observe<E>(
    sink: Option<&dyn RpcTelemetrySink>,
    request: &RpcRequest,
    service: &str,
    carrier: Carrier,
    started: Instant,
    correlation_id: Option<&str>,
    result: &Result<String, TransportError<E>>,
) {
    let (outcome, code) = match result {
        Ok(_) => (Outcome::Ok, None),
        Err(TransportError::Remote { code, .. }) => (Outcome::Failed, Some(code.as_str())),
        Err(TransportError::Carrier(_)) => (Outcome::TransportError, None),
        Err(TransportError::Protocol(_)) => (Outcome::Failed, Some("protocol")),
    };
    emit(
        sink,
        RpcEvent {
            key: request.key,
            service,
            method: request.method,
            // The template, never `request.path`: the template carries no ids.
            path_template: request.path_template,
            carrier,
            outcome,
            duration_micros: started.elapsed().as_micros() as u64,
            code,
            correlation_id,
            trace_id: None,
            span_id: None,
        },
    );
}

/// `RpcTransport` over plain HTTP.
pub struct HttpTransport<C> {
    http: C,
    service: &'static str,
    telemetry: Option<Arc<dyn RpcTelemetrySink>>,
}

impl<C> HttpTransport<C> {
    pub fn new(http: C, service: &'static str) -> Self {
        Self { http, service, telemetry: None }
    }

    /// Attach an application-owned sink. Without one, nothing is emitted and
    /// no telemetry library is linked.
    pub fn with_telemetry(mut self, sink: Arc<dyn RpcTelemetrySink>) -> Self {
        self.telemetry = Some(sink);
        self
    }
}

impl<C: HttpCall> RpcTransport for HttpTransport<C> {
    type Error = TransportError<C::Error>;

    fn call(&self, request: RpcRequest) -> Result<String, Self::Error> {
        let started = Instant::now();
        let result = self.http.call(&request).map_err(TransportError::Carrier);
        observe(
            self.telemetry.as_deref(),
            &request,
            self.service,
            Carrier::Http,
            started,
            None,
            &result,
        );
        result
    }
}

/// `RpcTransport` over a framed connection (WebSocket or TCP).
pub struct FramedTransport<C> {
    conn: C,
    service: &'static str,
    correlator: std::sync::Mutex<Correlator>,
    telemetry: Option<Arc<dyn RpcTelemetrySink>>,
}

impl<C> FramedTransport<C> {
    pub fn new(conn: C, service: &'static str, id_prefix: impl Into<String>) -> Self {
        Self {
            conn,
            service,
            correlator: std::sync::Mutex::new(Correlator::new(id_prefix)),
            telemetry: None,
        }
    }

    pub fn with_telemetry(mut self, sink: Arc<dyn RpcTelemetrySink>) -> Self {
        self.telemetry = Some(sink);
        self
    }

    fn next_id(&self) -> String {
        // A poisoned correlator is still usable: the counter is just a number.
        match self.correlator.lock() {
            Ok(mut c) => c.take(),
            Err(poisoned) => poisoned.into_inner().take(),
        }
    }
}

impl<C: FramedConnection> RpcTransport for FramedTransport<C> {
    type Error = TransportError<C::Error>;

    fn call(&self, request: RpcRequest) -> Result<String, Self::Error> {
        let started = Instant::now();
        let id = self.next_id();
        let body = match request.body.as_deref() {
            Some(raw) => Some(
                serde_json::from_str(raw)
                    .map_err(|e| TransportError::Protocol(format!("request body is not JSON: {e}")))?,
            ),
            None => None,
        };
        let call = Frame::call(
            id.clone(),
            request.key,
            request.method,
            request.path.clone(),
            request.query.clone(),
            body,
        );

        let result = self
            .conn
            .exchange(call)
            .map_err(TransportError::Carrier)
            .and_then(|frames| unary_answer(&id, frames));

        observe(
            self.telemetry.as_deref(),
            &request,
            self.service,
            self.conn.carrier(),
            started,
            Some(&id),
            &result,
        );
        result
    }
}

/// Reduce the frames answering a unary call to its response body.
///
/// Strict on shape: a stream of data frames arriving for a unary operation is
/// a contract violation on the server's side, and saying so beats quietly
/// keeping the first one.
fn unary_answer<E>(id: &str, frames: Vec<Frame>) -> Result<String, TransportError<E>> {
    let mut body: Option<String> = None;
    let mut ended = false;
    for frame in frames {
        if frame.id != id {
            return Err(TransportError::Protocol(format!(
                "frame for correlation id {} arrived on the exchange for {id}",
                frame.id
            )));
        }
        match frame.kind {
            FrameKind::Data => {
                if body.is_some() {
                    return Err(TransportError::Protocol(
                        "a unary operation answered with more than one data frame".into(),
                    ));
                }
                let value = frame.body.ok_or_else(|| {
                    TransportError::Protocol("a data frame arrived without a body".into())
                })?;
                body = Some(value.to_string());
            }
            FrameKind::End => {
                ended = true;
            }
            FrameKind::Error => {
                return Err(TransportError::Remote {
                    code: frame.code.unwrap_or_else(|| "unknown".into()),
                    message: frame.message,
                });
            }
            FrameKind::Cancel => {
                return Err(TransportError::Protocol("the peer cancelled the exchange".into()));
            }
            FrameKind::Call => {
                return Err(TransportError::Protocol(
                    "a call frame cannot answer a call".into(),
                ));
            }
        }
    }
    match (body, ended) {
        (Some(body), true) => Ok(body),
        (Some(_), false) => Err(TransportError::Protocol(
            "the exchange delivered a body but never ended".into(),
        )),
        (None, _) => Err(TransportError::Protocol(
            "the exchange ended without a response body".into(),
        )),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    struct Canned(Vec<Frame>);
    impl FramedConnection for Canned {
        type Error = String;
        fn exchange(&self, _call: Frame) -> Result<Vec<Frame>, String> {
            Ok(self.0.clone())
        }
        fn carrier(&self) -> Carrier {
            Carrier::WebSocket
        }
    }

    fn request() -> RpcRequest {
        RpcRequest {
            key: "healthz",
            method: "GET",
            path: "/healthz".into(),
            path_template: "/healthz",
            query: Vec::new(),
            body: None,
            delivery: Delivery::Direct,
            opto_sync: None,
        }
    }

    fn transport(frames: Vec<Frame>) -> FramedTransport<Canned> {
        FramedTransport::new(Canned(frames), "demo", "t-")
    }

    #[test]
    fn a_unary_answer_is_one_data_frame_then_end() {
        let t = transport(vec![Frame::data("t-1", json!({"ok": true})), Frame::end("t-1")]);
        assert_eq!(t.call(request()).unwrap(), r#"{"ok":true}"#);
    }

    #[test]
    fn an_error_frame_becomes_a_remote_error_not_a_body() {
        let t = transport(vec![Frame::error("t-1", "503", Some("draining".into()))]);
        match t.call(request()) {
            Err(TransportError::Remote { code, message }) => {
                assert_eq!(code, "503");
                assert_eq!(message.as_deref(), Some("draining"));
            }
            other => panic!("expected a remote error, got {other:?}"),
        }
    }

    #[test]
    fn a_second_data_frame_on_a_unary_call_is_refused() {
        let t = transport(vec![
            Frame::data("t-1", json!(1)),
            Frame::data("t-1", json!(2)),
            Frame::end("t-1"),
        ]);
        assert!(matches!(t.call(request()), Err(TransportError::Protocol(_))));
    }

    #[test]
    fn a_body_without_an_end_frame_is_refused() {
        let t = transport(vec![Frame::data("t-1", json!({"ok": true}))]);
        assert!(matches!(t.call(request()), Err(TransportError::Protocol(_))));
    }

    #[test]
    fn a_frame_for_another_exchange_is_refused() {
        let t = transport(vec![Frame::data("t-99", json!({"ok": true})), Frame::end("t-99")]);
        assert!(matches!(t.call(request()), Err(TransportError::Protocol(_))));
    }
}
