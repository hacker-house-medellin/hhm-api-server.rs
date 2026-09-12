//! The telemetry seam. ores-otel plugs in here; this module never imports it.
//!
//! # Direction of the dependency
//!
//! Same arrow as the opto-sync seam, and for the same reason: an application
//! depends on `ores-otel`, and hands this module something that satisfies
//! [`RpcTelemetrySink`]. Nothing here links an OTel SDK, installs a global
//! provider, owns exporter shutdown, or decides sampling. That mirrors what
//! `opto-sync-clients/clients/rust/src/telemetry.rs` already does with
//! `ProtocolSyncTelemetrySink` -- one seam shape across the stack, so an
//! application writes one adapter and points both at it.
//!
//! # Fail-open, always
//!
//! Telemetry that can break a call is worse than no telemetry. Every emit is
//! wrapped: a sink that returns an error or panics changes nothing about the
//! RPC, and the failure is swallowed at this boundary.
//!
//! # What is deliberately absent
//!
//! No request body, no response body, no path parameter values, no `meta`
//! contents. An RPC payload is the caller's data and a route map cannot know
//! which fields are sensitive, so none of it crosses this boundary. The
//! operation key, the transport, and the outcome are enough to build latency
//! and error-rate signals; anything richer belongs to the application, which
//! knows what it is allowed to record.

use std::panic::{catch_unwind, AssertUnwindSafe};

/// How the call was carried.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Carrier {
    Http,
    WebSocket,
    Tcp,
    /// Written to the opto-sync queue rather than sent.
    Queue,
}

impl Carrier {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Http => "http",
            Self::WebSocket => "websocket",
            Self::Tcp => "tcp",
            Self::Queue => "queue",
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Outcome {
    Ok,
    /// The peer answered, and the answer was a failure.
    Failed,
    /// The call never reached a peer.
    TransportError,
    /// Queued locally; the authoritative result arrives later through sync.
    Queued,
}

impl Outcome {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Ok => "ok",
            Self::Failed => "failed",
            Self::TransportError => "transport_error",
            Self::Queued => "queued",
        }
    }
}

/// One completed call, reduced to what is safe to record everywhere.
#[derive(Clone, Debug)]
pub struct RpcEvent<'a> {
    /// Operation key from the route map. Low cardinality by construction --
    /// safe as a metric label, which a path with an id in it is not.
    pub key: &'a str,
    pub service: &'a str,
    pub method: &'a str,
    /// The route map's path *template*, never the substituted path: the
    /// template has no customer identifiers in it.
    pub path_template: &'a str,
    pub carrier: Carrier,
    pub outcome: Outcome,
    pub duration_micros: u64,
    /// Failure code when `outcome` is not `Ok`. An HTTP status or a slug.
    pub code: Option<&'a str>,
    /// Frame correlation id, for stitching a client call to a server span on
    /// a framed transport. Absent over HTTP.
    pub correlation_id: Option<&'a str>,
    /// W3C trace context, if the caller is already inside a trace. This module
    /// neither creates nor propagates it -- it passes through what it is given.
    pub trace_id: Option<&'a str>,
    pub span_id: Option<&'a str>,
}

/// Application-owned adapter seam. Back it with `ores-otel` / `next-loggers`.
pub trait RpcTelemetrySink: Send + Sync {
    fn emit(&self, event: &RpcEvent<'_>) -> Result<(), String>;
}

impl<F> RpcTelemetrySink for F
where
    F: Fn(&RpcEvent<'_>) -> Result<(), String> + Send + Sync,
{
    fn emit(&self, event: &RpcEvent<'_>) -> Result<(), String> {
        self(event)
    }
}

/// Deliver one event without letting it affect the call.
///
/// A missing sink, a sink error, and a sink panic are all the same outcome
/// here: nothing happens and the RPC is unaffected.
pub fn emit(sink: Option<&dyn RpcTelemetrySink>, event: RpcEvent<'_>) {
    let Some(sink) = sink else { return };
    let _ = catch_unwind(AssertUnwindSafe(|| sink.emit(&event)));
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};

    fn event<'a>(key: &'a str) -> RpcEvent<'a> {
        RpcEvent {
            key,
            service: "demo",
            method: "POST",
            path_template: "/v1/matters/{id}/walk",
            carrier: Carrier::WebSocket,
            outcome: Outcome::Ok,
            duration_micros: 1234,
            code: None,
            correlation_id: Some("c7-1"),
            trace_id: None,
            span_id: None,
        }
    }

    #[test]
    fn no_sink_is_not_an_error() {
        emit(None, event("walk_matter"));
    }

    #[test]
    fn a_panicking_sink_cannot_break_the_call() {
        struct Boom;
        impl RpcTelemetrySink for Boom {
            fn emit(&self, _: &RpcEvent<'_>) -> Result<(), String> {
                panic!("exporter is down");
            }
        }
        emit(Some(&Boom), event("walk_matter"));
    }

    #[test]
    fn a_failing_sink_is_swallowed() {
        emit(Some(&(|_: &RpcEvent<'_>| Err("queue full".to_string()))), event("healthz"));
    }

    #[test]
    fn a_closure_is_a_sink() {
        static SEEN: AtomicUsize = AtomicUsize::new(0);
        let sink = |e: &RpcEvent<'_>| {
            assert_eq!(e.key, "healthz");
            SEEN.fetch_add(1, Ordering::SeqCst);
            Ok(())
        };
        emit(Some(&sink), event("healthz"));
        assert_eq!(SEEN.load(Ordering::SeqCst), 1);
    }
}
