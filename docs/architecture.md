# Intake API architecture

## Ownership

- `hhm-interfaces`: TypeSpec authority, generated OpenAPI/JSON Schema, and Rust
  request/receipt validation.
- `hhm-lib-core`: declarative PostgreSQL authority, generated migration, Diesel
  schema, and generated SeaORM entities.
- `hhm-orm-core`: named least-privilege read/write capabilities and dual-target
  persistence mechanics.
- `hhm-api-server.rs`: HTTP, anti-abuse, authentication, orchestration, private
  object verification, and safe error mapping.
- `hhm-web-server.rs`: authenticated pages and prefill. It consumes read-only
  capabilities directly and submits writes through this API.

## Submission flow

1. The browser validates the versioned client contract and obtains a Turnstile
   proof. Authenticated pages also send the Shared Auth bearer token.
2. The API validates the contract, Turnstile action/hostname, optional identity,
   exact origin, and idempotency key.
3. The API commits the canonical row and outbox item in the primary database.
4. It writes the same UUID, subject, and payload digest to Supabase PostgreSQL.
5. It acknowledges the primary outbox entry and returns `201` only after both
   stores have accepted the same submission.

Uploads add a private direct-to-Supabase Storage leg. The client first reserves
an object key, uploads with the signed URL, and reports completion. The API
downloads that one object as a bounded stream and verifies kind, MIME type,
exact byte count, and SHA-256 before either database marks it verified. An
application can reference only verified uploads owned by the same optional
subject.

## Failure semantics

Validation and identity failures mutate nothing. A supplied invalid bearer
token is an authentication error, even on public endpoints. A primary database
failure is retryable and has no mirror side effect. A mirror failure after the
primary commit records a safe error code in the outbox and returns the stable
submission UUID in a retryable response. Repeating the same idempotency key and
digest is safe; changing the digest is a conflict.

The outbox provides durable reconciliation evidence; it does not turn two
independent PostgreSQL systems into a single atomic transaction.

## Network boundary

The service listens on the cluster network and is exposed through the reviewed
HHaus ingress at `api.hhaus.org`. Cloudflare must proxy the public hostname to
the cluster edge. The origin should accept only the expected ingress path, and
the application CORS allowlist remains narrower than network reachability.

Shared Auth is called through its in-cluster service address. Supabase database,
Storage, and Turnstile are explicit outbound dependencies. Startup and readiness
fail closed when required database capabilities are unavailable; liveness does
not depend on external services.
