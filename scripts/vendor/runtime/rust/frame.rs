//! The ridl frame envelope: HTTP-free addressing for WebSocket and TCP.
//!
//! This is a straight port of `ridl/framing.py`, and the fixtures under
//! `examples/frames/` are the contract between them. Nothing here does I/O --
//! it is the byte-level half of a framed transport, which is precisely the half
//! where two languages drift apart when each is written from prose.
//!
//! Canonical rules, all reproduced below: UTF-8 JSON, compact separators, a
//! fixed member order (not alphabetical, not map order), an absent value is an
//! omitted member rather than `null`, and non-ASCII is emitted literally.

use serde_json::Value;

pub const FRAME_VERSION: u8 = 1;
pub const MAX_FRAME_BYTES: usize = 8 * 1024 * 1024;
pub const LENGTH_PREFIX_BYTES: usize = 4;

/// Member order on the wire. Fixed so two ports produce identical bytes.
const FIELD_ORDER: [&str; 11] = [
    "v", "id", "t", "key", "method", "path", "query", "body", "code", "message", "meta",
];

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum FrameKind {
    Call,
    Data,
    End,
    Error,
    Cancel,
}

impl FrameKind {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Call => "call",
            Self::Data => "data",
            Self::End => "end",
            Self::Error => "error",
            Self::Cancel => "cancel",
        }
    }

    pub fn parse(raw: &str) -> Option<Self> {
        Some(match raw {
            "call" => Self::Call,
            "data" => Self::Data,
            "end" => Self::End,
            "error" => Self::Error,
            "cancel" => Self::Cancel,
            _ => return None,
        })
    }
}

#[derive(Debug)]
pub struct FrameError(pub String);

impl std::fmt::Display for FrameError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}
impl std::error::Error for FrameError {}

fn err<T>(msg: impl Into<String>) -> Result<T, FrameError> {
    Err(FrameError(msg.into()))
}

/// One message on a framed transport.
///
/// `body` is `Option<Value>` where `None` means the member is absent and
/// `Some(Value::Null)` means the payload is JSON `null`. Collapsing those two
/// would make "no body" and "a null body" indistinguishable on the wire.
#[derive(Clone, Debug, PartialEq)]
pub struct Frame {
    pub v: u8,
    pub id: String,
    pub kind: FrameKind,
    pub key: Option<String>,
    pub method: Option<String>,
    pub path: Option<String>,
    pub query: Vec<(String, String)>,
    pub body: Option<Value>,
    pub code: Option<String>,
    pub message: Option<String>,
    pub meta: Vec<(String, String)>,
}

impl Frame {
    fn bare(kind: FrameKind, id: impl Into<String>) -> Self {
        Self {
            v: FRAME_VERSION,
            id: id.into(),
            kind,
            key: None,
            method: None,
            path: None,
            query: Vec::new(),
            body: None,
            code: None,
            message: None,
            meta: Vec::new(),
        }
    }

    pub fn call(
        id: impl Into<String>,
        key: impl Into<String>,
        method: impl Into<String>,
        path: impl Into<String>,
        query: Vec<(String, String)>,
        body: Option<Value>,
    ) -> Self {
        let mut f = Self::bare(FrameKind::Call, id);
        f.key = Some(key.into());
        f.method = Some(method.into());
        f.path = Some(path.into());
        f.query = query;
        f.body = body;
        f
    }

    pub fn data(id: impl Into<String>, body: Value) -> Self {
        let mut f = Self::bare(FrameKind::Data, id);
        f.body = Some(body);
        f
    }

    pub fn end(id: impl Into<String>) -> Self {
        Self::bare(FrameKind::End, id)
    }

    pub fn cancel(id: impl Into<String>) -> Self {
        Self::bare(FrameKind::Cancel, id)
    }

    pub fn error(id: impl Into<String>, code: impl Into<String>, message: Option<String>) -> Self {
        let mut f = Self::bare(FrameKind::Error, id);
        f.code = Some(code.into());
        f.message = message;
        f
    }

    /// Out-of-band string values: auth token, trace context, deadline. Never
    /// operation data -- generated code does not read this.
    pub fn with_meta(mut self, name: impl Into<String>, value: impl Into<String>) -> Self {
        self.meta.push((name.into(), value.into()));
        self
    }

    pub fn meta_get(&self, name: &str) -> Option<&str> {
        self.meta.iter().find(|(k, _)| k == name).map(|(_, v)| v.as_str())
    }

    // -- validation ------------------------------------------------------

    pub fn validate(&self) -> Result<(), FrameError> {
        if self.v != FRAME_VERSION {
            return err(format!("unsupported frame version {}", self.v));
        }
        if self.id.is_empty() || self.id.chars().count() > 128 {
            return err("id must be 1..128 characters");
        }
        match self.kind {
            FrameKind::Call => {
                if self.key.as_deref().unwrap_or("").is_empty() {
                    return err("a call frame needs an operation key");
                }
                if self.method.as_deref().unwrap_or("").is_empty() {
                    return err("a call frame needs a method");
                }
                match self.path.as_deref() {
                    Some(p) if p.starts_with('/') => {}
                    _ => return err("a call frame needs a path starting with /"),
                }
            }
            _ => {
                if self.key.is_some() || self.method.is_some() || self.path.is_some()
                    || !self.query.is_empty()
                {
                    return err(format!(
                        "a {} frame carries no addressing fields",
                        self.kind.as_str()
                    ));
                }
            }
        }
        if self.kind == FrameKind::Data && self.body.is_none() {
            return err("a data frame needs a body");
        }
        if self.kind == FrameKind::Error {
            if self.code.as_deref().unwrap_or("").is_empty() {
                return err("an error frame needs a code");
            }
        } else if self.code.is_some() || self.message.is_some() {
            return err(format!(
                "a {} frame carries no code or message",
                self.kind.as_str()
            ));
        }
        Ok(())
    }

    // -- encoding --------------------------------------------------------

    /// Serialise in the canonical member order.
    ///
    /// Written out by hand rather than through `serde_json::Map`: without the
    /// `preserve_order` feature that map is a `BTreeMap`, so building one in
    /// the right order and serialising it would still emit members sorted
    /// alphabetically -- `body` first, `v` last -- and every frame would
    /// disagree with the fixtures and with every other port. Enabling
    /// `preserve_order` instead would change map behaviour for the whole
    /// consuming crate, which is not this module's call to make.
    fn write_json(&self, out: &mut String) -> Result<(), FrameError> {
        fn push_str_value(out: &mut String, value: &str) -> Result<(), FrameError> {
            // serde_json escapes exactly as the canonical form requires and
            // leaves non-ASCII literal.
            let encoded = serde_json::to_string(value).map_err(|e| FrameError(format!("encode: {e}")))?;
            out.push_str(&encoded);
            Ok(())
        }
        fn push_value(out: &mut String, value: &Value) -> Result<(), FrameError> {
            let encoded = serde_json::to_string(value).map_err(|e| FrameError(format!("encode: {e}")))?;
            out.push_str(&encoded);
            Ok(())
        }

        out.push('{');
        out.push_str("\"v\":");
        out.push_str(&self.v.to_string());
        out.push_str(",\"id\":");
        push_str_value(out, &self.id)?;
        out.push_str(",\"t\":");
        push_str_value(out, self.kind.as_str())?;

        if self.kind == FrameKind::Call {
            out.push_str(",\"key\":");
            push_str_value(out, self.key.as_deref().unwrap_or(""))?;
            out.push_str(",\"method\":");
            push_str_value(out, self.method.as_deref().unwrap_or(""))?;
            out.push_str(",\"path\":");
            push_str_value(out, self.path.as_deref().unwrap_or(""))?;
            if !self.query.is_empty() {
                out.push_str(",\"query\":[");
                for (i, (name, value)) in self.query.iter().enumerate() {
                    if i > 0 {
                        out.push(',');
                    }
                    out.push('[');
                    push_str_value(out, name)?;
                    out.push(',');
                    push_str_value(out, value)?;
                    out.push(']');
                }
                out.push(']');
            }
        }

        if let Some(body) = &self.body {
            out.push_str(",\"body\":");
            push_value(out, body)?;
        }

        if self.kind == FrameKind::Error {
            out.push_str(",\"code\":");
            push_str_value(out, self.code.as_deref().unwrap_or(""))?;
            if let Some(message) = &self.message {
                out.push_str(",\"message\":");
                push_str_value(out, message)?;
            }
        }

        if !self.meta.is_empty() {
            // Meta is a JSON object, so its members are compared by name, not
            // by position -- but emit them sorted anyway so two ports building
            // the same frame produce the same bytes.
            let mut meta: Vec<&(String, String)> = self.meta.iter().collect();
            meta.sort_by(|a, b| a.0.cmp(&b.0));
            out.push_str(",\"meta\":{");
            for (i, (name, value)) in meta.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                push_str_value(out, name)?;
                out.push(':');
                push_str_value(out, value)?;
            }
            out.push('}');
        }

        out.push('}');
        Ok(())
    }

    /// The canonical bytes. Byte-identical to `ridl.framing.Frame.encode`.
    pub fn encode(&self) -> Result<Vec<u8>, FrameError> {
        self.validate()?;
        let mut text = String::new();
        self.write_json(&mut text)?;
        let bytes = text.into_bytes();
        if bytes.len() > MAX_FRAME_BYTES {
            return err(format!(
                "frame is {} bytes, over the {MAX_FRAME_BYTES} limit",
                bytes.len()
            ));
        }
        Ok(bytes)
    }

    /// Length-prefixed bytes for a byte-stream transport.
    pub fn encode_tcp(&self) -> Result<Vec<u8>, FrameError> {
        let payload = self.encode()?;
        let mut out = Vec::with_capacity(LENGTH_PREFIX_BYTES + payload.len());
        out.extend_from_slice(&(payload.len() as u32).to_be_bytes());
        out.extend_from_slice(&payload);
        Ok(out)
    }

    // -- decoding --------------------------------------------------------

    pub fn decode(payload: &[u8]) -> Result<Self, FrameError> {
        if payload.len() > MAX_FRAME_BYTES {
            return err(format!(
                "frame is {} bytes, over the {MAX_FRAME_BYTES} limit",
                payload.len()
            ));
        }
        let value: Value =
            serde_json::from_slice(payload).map_err(|e| FrameError(format!("frame is not JSON: {e}")))?;
        let Value::Object(obj) = value else {
            return err("a frame must be a JSON object");
        };

        // Strict on unknown members: silently dropping one is how a peer ends
        // up believing a field was honoured when it was ignored.
        let unknown: Vec<&str> = obj
            .keys()
            .map(String::as_str)
            .filter(|k| !FIELD_ORDER.contains(k))
            .collect();
        if !unknown.is_empty() {
            return err(format!("unknown frame member(s): {}", unknown.join(", ")));
        }

        let kind = obj
            .get("t")
            .and_then(Value::as_str)
            .and_then(FrameKind::parse)
            .ok_or_else(|| FrameError("unknown frame type".into()))?;

        let mut query = Vec::new();
        if let Some(raw) = obj.get("query") {
            let Some(pairs) = raw.as_array() else {
                return err("query must be an array of [name, value] pairs");
            };
            for pair in pairs {
                match pair.as_array().map(|p| (p.len(), p)) {
                    Some((2, p)) => match (p[0].as_str(), p[1].as_str()) {
                        (Some(k), Some(v)) => query.push((k.to_string(), v.to_string())),
                        _ => return err("each query entry must be a pair of strings"),
                    },
                    _ => return err("each query entry must be a [name, value] pair"),
                }
            }
        }

        let mut meta = Vec::new();
        if let Some(raw) = obj.get("meta") {
            let Some(map) = raw.as_object() else {
                return err("meta must be an object");
            };
            for (k, v) in map {
                let Some(v) = v.as_str() else {
                    return err(format!("meta.{k} must be a string"));
                };
                meta.push((k.clone(), v.to_string()));
            }
            meta.sort_by(|a, b| a.0.cmp(&b.0));
        }

        let frame = Frame {
            v: obj.get("v").and_then(Value::as_u64).unwrap_or(0) as u8,
            id: obj.get("id").and_then(Value::as_str).unwrap_or_default().to_string(),
            kind,
            key: obj.get("key").and_then(Value::as_str).map(str::to_string),
            method: obj.get("method").and_then(Value::as_str).map(str::to_string),
            path: obj.get("path").and_then(Value::as_str).map(str::to_string),
            query,
            body: obj.get("body").cloned(),
            code: obj.get("code").and_then(Value::as_str).map(str::to_string),
            message: obj.get("message").and_then(Value::as_str).map(str::to_string),
            meta,
        };
        frame.validate()?;
        Ok(frame)
    }
}

/// Pull every whole length-prefixed frame out of a read buffer, returning the
/// frames and how many bytes were consumed. The caller keeps the tail.
pub fn decode_stream(buffer: &[u8]) -> Result<(Vec<Frame>, usize), FrameError> {
    let mut frames = Vec::new();
    let mut offset = 0usize;
    while buffer.len() - offset >= LENGTH_PREFIX_BYTES {
        let mut len_bytes = [0u8; LENGTH_PREFIX_BYTES];
        len_bytes.copy_from_slice(&buffer[offset..offset + LENGTH_PREFIX_BYTES]);
        let length = u32::from_be_bytes(len_bytes) as usize;
        if length > MAX_FRAME_BYTES {
            // Refuse before allocating: a corrupt length must never make the
            // reader reserve gigabytes.
            return err(format!(
                "declared frame length {length} is over the {MAX_FRAME_BYTES} limit"
            ));
        }
        let start = offset + LENGTH_PREFIX_BYTES;
        if buffer.len() - start < length {
            break;
        }
        frames.push(Frame::decode(&buffer[start..start + length])?);
        offset = start + length;
    }
    Ok((frames, offset))
}

/// Per-connection correlation ids.
///
/// Monotonic, never derived from the request. A content-hashed id would make
/// two genuinely separate calls with identical payloads collide.
#[derive(Debug, Default)]
pub struct Correlator {
    prefix: String,
    next: u64,
}

impl Correlator {
    pub fn new(prefix: impl Into<String>) -> Self {
        Self { prefix: prefix.into(), next: 0 }
    }

    pub fn take(&mut self) -> String {
        self.next += 1;
        if self.prefix.is_empty() {
            self.next.to_string()
        } else {
            format!("{}{}", self.prefix, self.next)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn call_frame_matches_the_canonical_bytes() {
        let f = Frame::call(
            "1",
            "walk_matter",
            "POST",
            "/v1/matters/abc/walk",
            vec![("include".into(), "1".into())],
            Some(json!({"choice_id": "c"})),
        );
        assert_eq!(
            String::from_utf8(f.encode().unwrap()).unwrap(),
            r#"{"v":1,"id":"1","t":"call","key":"walk_matter","method":"POST","path":"/v1/matters/abc/walk","query":[["include","1"]],"body":{"choice_id":"c"}}"#
        );
    }

    #[test]
    fn absent_body_and_null_body_stay_distinguishable() {
        let absent = Frame::end("1");
        let null = Frame::data("1", Value::Null);
        assert_eq!(String::from_utf8(absent.encode().unwrap()).unwrap(), r#"{"v":1,"id":"1","t":"end"}"#);
        assert_eq!(String::from_utf8(null.encode().unwrap()).unwrap(), r#"{"v":1,"id":"1","t":"data","body":null}"#);
        assert!(Frame::decode(br#"{"v":1,"id":"1","t":"end"}"#).unwrap().body.is_none());
        assert_eq!(
            Frame::decode(br#"{"v":1,"id":"1","t":"data","body":null}"#).unwrap().body,
            Some(Value::Null)
        );
    }

    #[test]
    fn unknown_members_are_refused_not_ignored() {
        let e = Frame::decode(br#"{"v":1,"id":"1","t":"end","deadline":"5s"}"#).unwrap_err();
        assert!(e.0.contains("unknown frame member"), "{}", e.0);
    }

    #[test]
    fn a_corrupt_length_prefix_cannot_force_a_huge_allocation() {
        let mut buf = u32::MAX.to_be_bytes().to_vec();
        buf.extend_from_slice(b"{}");
        assert!(decode_stream(&buf).is_err());
    }

    #[test]
    fn a_partial_tail_is_left_for_the_next_read() {
        let a = Frame::call("1", "healthz", "GET", "/healthz", vec![], None).encode_tcp().unwrap();
        let b = Frame::end("1").encode_tcp().unwrap();
        let mut buf = a.clone();
        buf.extend_from_slice(&b[..3]);
        let (frames, consumed) = decode_stream(&buf).unwrap();
        assert_eq!(frames.len(), 1);
        assert_eq!(buf.len() - consumed, 3);
    }

    #[test]
    fn every_conformance_fixture_round_trips() {
        // The same file `scripts/test_framing.py` and the TypeScript port
        // assert against. If this fails, the ports have drifted.
        let raw = include_str!("../../examples/frames/conformance.json");
        let doc: Value = serde_json::from_str(raw).expect("fixtures parse");
        let cases = doc["cases"].as_array().expect("cases array");
        assert!(!cases.is_empty());
        for case in cases {
            let name = case["name"].as_str().unwrap();
            let encoded = case["encoded"].as_str().unwrap();
            let frame = Frame::decode(encoded.as_bytes())
                .unwrap_or_else(|e| panic!("{name}: decode failed: {e}"));
            assert_eq!(
                String::from_utf8(frame.encode().unwrap()).unwrap(),
                encoded,
                "{name}: re-encoding did not reproduce the canonical bytes"
            );
            assert_eq!(
                hex(&frame.encode_tcp().unwrap()[..4]),
                case["tcp_prefix_hex"].as_str().unwrap(),
                "{name}: length prefix"
            );
        }
    }

    fn hex(bytes: &[u8]) -> String {
        bytes.iter().map(|b| format!("{b:02x}")).collect()
    }

    #[test]
    fn correlation_ids_do_not_collide_for_identical_calls() {
        let mut c = Correlator::new("c7-");
        assert_ne!(c.take(), c.take());
    }
}
