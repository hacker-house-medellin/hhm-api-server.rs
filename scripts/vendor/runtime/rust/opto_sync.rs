//! `RpcTransport` implemented over opto-sync's durable queue.
//!
//! # Direction of the dependency
//!
//! This module calls opto-sync. opto-sync never calls it. That is deliberate
//! and it is why the seam is here rather than in `opto-sync-clients`:
//! `ProtocolTransport` is generic over `type Error` and taken by `&mut T`, so
//! an RPC crate can depend on `opto-sync-client` without opto-sync acquiring
//! any knowledge of RPC. The reverse -- teaching opto-sync to speak RPC --
//! would invert the arrow that `opto-sync-interfaces/README.md` pins down:
//! "Interfaces must never depend on a sync engine or client implementation."
//!
//! # Why only some calls are queued
//!
//! opto-sync's queue is record-shaped, not a general message bus:
//!
//! * a payload must be a JSON **object** (`ProtocolError::InvalidPayload`),
//! * a table must be a SQL-safe scope id,
//! * a queued `delete` is a tombstone and carries no data,
//! * and there is **no per-mutation response channel** -- `MutationResult`
//!   carries `status`, `revision`, `code` and `message`, not a return value.
//!
//! So reads go straight to the server and only mutations are queued. Which is
//! which is decided in the route map (`delivery`) and checked at generation
//! time, not guessed here.
//!
//! # What a queued call returns
//!
//! It returns the caller's own write, read back through opto-sync's local
//! view. That is the honest answer while offline: the mutation is durable, the
//! local projection reflects it, and the authoritative value arrives later
//! through the ordinary pull/reconcile path. `LocalReadback` is the seam for
//! that projection, because only the application knows how a queued mutation
//! projects into the response the route map declares.

use std::fmt;

use crate::{Delivery, RecordIdSource, RpcRequest, RpcTransport};

/// Anything that can perform a plain, online request. Usually a `reqwest` or
/// `ureq` wrapper; it is what non-queued operations use, and what a queued
/// operation falls back to when the caller asks for confirmed semantics.
pub trait DirectTransport {
    type Error;
    fn call(&self, request: &RpcRequest) -> Result<String, Self::Error>;
}

/// The subset of `opto_sync_client::ProtocolQueue` this transport needs.
///
/// Declared as a trait rather than used concretely so the RPC layer builds and
/// tests without opto-sync present, and so a caller can supply the in-memory
/// `ProtocolQueue` or the `SqliteProtocolStore` without this module choosing
/// for them.
pub trait MutationQueue {
    type Error;

    /// Mirrors `ProtocolQueue::queue_upsert`; returns the mutation id.
    fn queue_upsert(
        &mut self,
        table: &str,
        record_id: &str,
        payload: &str,
        base_revision: Option<&str>,
    ) -> Result<String, Self::Error>;

    /// Mirrors `ProtocolQueue::queue_delete`.
    fn queue_delete(
        &mut self,
        table: &str,
        record_id: &str,
        base_revision: Option<&str>,
    ) -> Result<String, Self::Error>;
}

/// Projects a queued mutation into the response the route map declares.
///
/// Back this with `SqliteProtocolStore::local_record` or
/// `OptoSyncClient::local_view` -- both return the caller's own write layered
/// over the last authoritative row, which is exactly the optimistic value the
/// UI should render.
pub trait LocalReadback {
    type Error;
    fn local_json(&self, table: &str, record_id: &str) -> Result<Option<String>, Self::Error>;
}

/// What went wrong, kept separate so a caller can tell a dead network from a
/// rejected payload from a contract bug.
#[derive(Debug)]
pub enum OptoTransportError<D, Q, R> {
    Direct(D),
    Queue(Q),
    Readback(R),
    /// The route map said queue this, but the request cannot be represented as
    /// an opto-sync record. Generation should have caught it; if you see this,
    /// the route map and the generated code are out of step.
    NotQueueable(&'static str),
    /// A queued mutation was accepted but no local projection came back, so
    /// there is nothing truthful to return to the caller.
    NoLocalProjection { table: String, record_id: String },
}

impl<D: fmt::Debug, Q: fmt::Debug, R: fmt::Debug> fmt::Display for OptoTransportError<D, Q, R> {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Direct(e) => write!(f, "direct transport: {e:?}"),
            Self::Queue(e) => write!(f, "opto-sync queue: {e:?}"),
            Self::Readback(e) => write!(f, "local readback: {e:?}"),
            Self::NotQueueable(why) => write!(f, "not queueable: {why}"),
            Self::NoLocalProjection { table, record_id } => write!(
                f,
                "queued {table}/{record_id} but no local projection was available"
            ),
        }
    }
}

/// Routes each call by the `delivery` the route map declared.
pub struct OptoSyncTransport<D, Q, R> {
    direct: D,
    queue: std::cell::RefCell<Q>,
    readback: R,
}

impl<D, Q, R> OptoSyncTransport<D, Q, R> {
    pub fn new(direct: D, queue: Q, readback: R) -> Self {
        Self {
            direct,
            queue: std::cell::RefCell::new(queue),
            readback,
        }
    }
}

impl<D, Q, R> OptoSyncTransport<D, Q, R>
where
    D: DirectTransport,
    Q: MutationQueue,
    R: LocalReadback,
{
    /// Resolve the opto-sync record id for this call.
    ///
    /// `RecordIdSource` comes from the route map, so the id is derived the same
    /// way in every language rather than being invented per client.
    fn record_id(
        &self,
        request: &RpcRequest,
        source: RecordIdSource,
    ) -> Result<String, OptoTransportError<D::Error, Q::Error, R::Error>> {
        match source {
            // Locate the parameter by name in the template, then read the
            // matching segment out of the substituted path. Taking the last
            // segment would be wrong for `/v1/matters/{id}/walk`, where the
            // final segment is a literal.
            RecordIdSource::PathParam(name) => {
                segment_for_param(request.path_template, &request.path, name)
                    .map(|raw| percent_decode(&raw))
                    .ok_or(OptoTransportError::NotQueueable(
                        "record_id_from names a path parameter that is not in the path template",
                    ))
            }
            RecordIdSource::RequestField(field) => {
                let body = request.body.as_deref().ok_or(OptoTransportError::NotQueueable(
                    "record_id_from names a request field but there is no body",
                ))?;
                extract_string_field(body, field).ok_or(OptoTransportError::NotQueueable(
                    "record_id_from names a request field that is absent or not a string",
                ))
            }
            RecordIdSource::Minted => Ok(mint_id(request)),
        }
    }
}

impl<D, Q, R> RpcTransport for OptoSyncTransport<D, Q, R>
where
    D: DirectTransport,
    Q: MutationQueue,
    R: LocalReadback,
{
    type Error = OptoTransportError<D::Error, Q::Error, R::Error>;

    fn call(&self, request: RpcRequest) -> Result<String, Self::Error> {
        let binding = match (request.delivery, request.opto_sync) {
            (Delivery::Direct, _) | (_, None) => {
                return self.direct.call(&request).map_err(OptoTransportError::Direct);
            }
            (Delivery::OptoSyncQueued, Some(binding)) => binding,
        };

        let record_id = self.record_id(&request, binding.record_id)?;

        {
            let mut queue = self.queue.borrow_mut();
            if binding.operation == "delete" {
                queue
                    .queue_delete(binding.table, &record_id, None)
                    .map_err(OptoTransportError::Queue)?;
            } else {
                let payload = request.body.as_deref().ok_or(
                    // Validation rejects this shape, so reaching it means the
                    // generated code is older than the route map.
                    OptoTransportError::NotQueueable("a queued upsert needs a JSON body"),
                )?;
                queue
                    .queue_upsert(binding.table, &record_id, payload, None)
                    .map_err(OptoTransportError::Queue)?;
            }
        }

        self.readback
            .local_json(binding.table, &record_id)
            .map_err(OptoTransportError::Readback)?
            .ok_or(OptoTransportError::NoLocalProjection {
                table: binding.table.to_string(),
                record_id,
            })
    }
}

/// Find the substituted value of `{name}` by walking the template and the real
/// path in lockstep. Both were produced by the same generated builder, so they
/// always have the same segment count.
fn segment_for_param(template: &str, path: &str, name: &str) -> Option<String> {
    let wanted = format!("{{{name}}}");
    let mut actual = path.split('/');
    for expected in template.split('/') {
        let got = actual.next()?;
        if expected == wanted {
            return Some(got.to_string());
        }
    }
    None
}

/// Reverse `encode_segment`. Only `%XX` needs undoing; the generated encoder
/// never emits `+` for a space.
fn percent_decode(input: &str) -> String {
    let bytes = input.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%' && i + 2 < bytes.len() {
            if let Some(byte) = hex_pair(bytes[i + 1], bytes[i + 2]) {
                out.push(byte);
                i += 3;
                continue;
            }
        }
        out.push(bytes[i]);
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

fn hex_pair(hi: u8, lo: u8) -> Option<u8> {
    Some(hex_digit(hi)? << 4 | hex_digit(lo)?)
}

fn hex_digit(byte: u8) -> Option<u8> {
    match byte {
        b'0'..=b'9' => Some(byte - b'0'),
        b'a'..=b'f' => Some(byte - b'a' + 10),
        b'A'..=b'F' => Some(byte - b'A' + 10),
        _ => None,
    }
}

/// Pull one top-level string field out of a JSON object without pulling in a
/// parser. Deliberately shallow: `record_id_from` may only name a top-level
/// field, and validation enforces that.
fn extract_string_field(json: &str, field: &str) -> Option<String> {
    let needle = format!("\"{field}\"");
    let start = json.find(&needle)? + needle.len();
    let rest = json[start..].trim_start();
    let rest = rest.strip_prefix(':')?.trim_start();
    let rest = rest.strip_prefix('"')?;
    let mut out = String::new();
    let mut chars = rest.chars();
    while let Some(ch) = chars.next() {
        match ch {
            '"' => return Some(out),
            '\\' => out.push(chars.next()?),
            other => out.push(other),
        }
    }
    None
}

/// A deterministic id for `record_id_from: "uuid"`, derived from the request so
/// a retry of the same call reuses it. opto-sync dedupes on
/// `(clientId, mutationId)`, so a stable id keeps a retry idempotent.
fn mint_id(request: &RpcRequest) -> String {
    let mut hash: u64 = 0xcbf2_9ce4_8422_2325;
    for byte in request
        .key
        .as_bytes()
        .iter()
        .chain(request.path.as_bytes())
        .chain(request.body.as_deref().unwrap_or("").as_bytes())
    {
        hash ^= u64::from(*byte);
        hash = hash.wrapping_mul(0x1000_0000_01b3);
    }
    format!("{}-{:016x}", request.key, hash)
}

#[cfg(test)]
mod tests {
    use super::*;

    struct NoDirect;
    impl DirectTransport for NoDirect {
        type Error = String;
        fn call(&self, _r: &RpcRequest) -> Result<String, String> {
            Err("should not have gone direct".into())
        }
    }

    #[derive(Default)]
    struct Recorder {
        upserts: Vec<(String, String, String)>,
        deletes: Vec<(String, String)>,
    }
    impl MutationQueue for Recorder {
        type Error = String;
        fn queue_upsert(
            &mut self,
            table: &str,
            record_id: &str,
            payload: &str,
            _base: Option<&str>,
        ) -> Result<String, String> {
            self.upserts
                .push((table.into(), record_id.into(), payload.into()));
            Ok("1".into())
        }
        fn queue_delete(
            &mut self,
            table: &str,
            record_id: &str,
            _base: Option<&str>,
        ) -> Result<String, String> {
            self.deletes.push((table.into(), record_id.into()));
            Ok("2".into())
        }
    }

    struct Echo;
    impl LocalReadback for Echo {
        type Error = String;
        fn local_json(&self, _t: &str, record_id: &str) -> Result<Option<String>, String> {
            Ok(Some(format!("{{\"id\":\"{record_id}\"}}")))
        }
    }

    fn queued(path: &str, body: Option<&str>, source: RecordIdSource) -> RpcRequest {
        queued_with("/v1/matters/{id}/walk", path, body, source)
    }

    fn queued_with(
        template: &'static str,
        path: &str,
        body: Option<&str>,
        source: RecordIdSource,
    ) -> RpcRequest {
        RpcRequest {
            key: "walk_matter",
            method: "POST",
            path: path.into(),
            path_template: template,
            query: Vec::new(),
            body: body.map(str::to_string),
            delivery: Delivery::OptoSyncQueued,
            opto_sync: Some(crate::OptoSyncBinding {
                table: "pmap_matter_walk",
                operation: "upsert",
                record_id: source,
            }),
        }
    }

    #[test]
    fn a_mid_path_parameter_is_located_by_name_not_by_position() {
        let transport = OptoSyncTransport::new(NoDirect, Recorder::default(), Echo);
        // The final segment here is the literal `walk`. Taking the last segment
        // -- the obvious shortcut -- would queue every matter under "walk".
        let request = queued(
            "/v1/matters/a%2Fb/walk",
            Some("{\"choice_id\":\"c\"}"),
            RecordIdSource::PathParam("id"),
        );
        assert_eq!(transport.call(request).expect("queued"), "{\"id\":\"a/b\"}");
    }

    #[test]
    fn request_field_record_id_is_read_from_the_body() {
        let transport = OptoSyncTransport::new(NoDirect, Recorder::default(), Echo);
        let request = queued_with(
            "/v1/matters",
            "/v1/matters",
            Some("{\"choice_id\":\"c\",\"matter_id\":\"m-7\"}"),
            RecordIdSource::RequestField("matter_id"),
        );
        assert_eq!(
            transport.call(request).expect("queued"),
            "{\"id\":\"m-7\"}"
        );
    }

    #[test]
    fn a_minted_id_is_stable_across_identical_retries() {
        let a = mint_id(&queued_with("/v1/x", "/v1/x", Some("{\"a\":1}"), RecordIdSource::Minted));
        let b = mint_id(&queued_with("/v1/x", "/v1/x", Some("{\"a\":1}"), RecordIdSource::Minted));
        let c = mint_id(&queued_with("/v1/x", "/v1/x", Some("{\"a\":2}"), RecordIdSource::Minted));
        assert_eq!(a, b, "a retry of the same call must reuse its record id");
        assert_ne!(a, c, "a different payload is a different record");
    }

    #[test]
    fn a_queued_upsert_without_a_body_is_reported_not_silently_dropped() {
        let transport = OptoSyncTransport::new(NoDirect, Recorder::default(), Echo);
        let request = queued_with("/v1/x/{id}", "/v1/x/1", None, RecordIdSource::PathParam("id"));
        assert!(matches!(
            transport.call(request),
            Err(OptoTransportError::NotQueueable(_))
        ));
    }
}
