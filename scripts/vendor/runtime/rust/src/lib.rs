//! Compiles the reference opto-sync transport against a real generated client.
//!
//! This mirrors the layout a consumer uses: the generated module at the crate
//! root, and `opto_sync.rs` vendored beside it so its `crate::` paths resolve
//! to that service's own operations. See `../README.md`.

#[path = "../../../examples/generated/demo/rust/ridl_generated.rs"]
mod generated;

pub use generated::*;

#[path = "../opto_sync.rs"]
pub mod opto_sync;

pub use opto_sync::{
    DirectTransport, LocalReadback, MutationQueue, OptoSyncTransport, OptoTransportError,
};
