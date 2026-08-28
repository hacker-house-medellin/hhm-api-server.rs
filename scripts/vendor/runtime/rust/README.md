# ridl runtime — Rust

`opto_sync.rs` is a **vendorable module**, not a crate. It refers to
`Delivery`, `RpcRequest`, `RpcTransport`, `RecordIdSource` and
`OptoSyncBinding` through `crate::`, because those are defined by the file
`ridl generate` emits for your service. Dropping it into a crate that also has
the generated module is what makes those paths resolve — and it means the
transport is compiled against *your* route map's operations, not against a
lowest-common-denominator copy.

```
src/
  lib.rs            pub use generated::*;  mod opto_sync;
  generated.rs      <- ridl generate --lang rust
  opto_sync.rs      <- this file, copied verbatim
```

`cargo test` in the host crate runs the module's own tests, including the one
that pins the mid-path record-id case (`/v1/matters/{id}/walk`, where the last
path segment is a literal and taking it would file every matter under `walk`).

## What it does

Reads go straight to the server. Mutations the route map marked
`delivery: opto_sync_queued` are written into opto-sync's durable queue and the
call returns the caller's own write, read back through the local view.

Wire it up by implementing three small traits over whichever opto-sync surface
you already use:

| Trait | Back it with |
| --- | --- |
| `DirectTransport` | `reqwest` / `ureq` / your existing HTTP client |
| `MutationQueue` | `ProtocolQueue::queue_upsert` / `queue_delete`, or `SqliteProtocolStore` |
| `LocalReadback` | `SqliteProtocolStore::local_record` or `OptoSyncClient::local_view` |

The traits exist so this module compiles and tests without opto-sync present,
and so the choice between the in-memory queue and the SQLite store stays yours.

## Direction of the dependency

Your crate depends on `opto-sync-client`. opto-sync depends on nothing here.
That is the constraint `opto-sync-interfaces/README.md` states — *"Interfaces
must never depend on a sync engine or client implementation"* — and it is
satisfied structurally: `ProtocolTransport` is generic over `type Error` and
taken by `&mut T`, so nothing has to be taught about RPC.
