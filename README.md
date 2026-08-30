# hhm-api-server.rs

Rust/Axum intake API for HHaus. The service accepts public pre-interest and
application submissions, authenticated referrals, and private upload intents.
It writes every accepted submission to the cluster PostgreSQL database and the
HHaus Supabase PostgreSQL project before returning success.

## Routes

| Method | Route | Authentication | Purpose |
| --- | --- | --- | --- |
| `GET` | `/healthz` | none | Process liveness only |
| `GET` | `/readyz` | none | Both database connections are available |
| `POST` | `/v1/pre-interests` | optional | Public or recognized pre-interest submission |
| `POST` | `/v1/intake/uploads` | optional | Create a private, bounded resume/photo-ID upload intent |
| `POST` | `/v1/intake/uploads/{id}/complete` | optional | Verify uploaded bytes, MIME type, size, and SHA-256 |
| `POST` | `/v1/applications` | optional | Submit an application that references verified uploads |
| `POST` | `/v1/referrals` | required | Submit a referral for another person |

Wire contracts and validation rules are owned by `hhm-interfaces`. Database
capabilities are owned by `hhm-orm-core`; this runtime cannot execute arbitrary
SQL and never runs migrations at startup.

## Security and persistence boundary

- Cloudflare Turnstile is verified server-side before a public write.
- A supplied bearer token is introspected with the official Shared Auth service
  client. An invalid token fails closed and is never downgraded to anonymous.
- Browser CORS uses an exact allowlist; wildcard origins are rejected.
- Uploaded identity documents remain in the private Supabase Storage bucket.
  The API issues a short-lived signed upload URL, then streams and verifies the
  resulting object without retaining its bytes in application memory.
- The primary PostgreSQL write and outbox record are atomic. The same canonical
  UUID and payload digest are then written to Supabase. A success response is
  sent only after the mirror and outbox acknowledgement succeed.
- Secrets, bearer tokens, Turnstile tokens, signed URLs, resumes, and photo-ID
  bytes must never be logged.

Dual persistence is deliberately not a distributed transaction. If the mirror
is unavailable after the primary commit, the API returns a retryable `503` with
the stable submission UUID and preserves a bounded outbox item for repair.
Idempotency keys and payload digests fence conflicting replays.

## Runtime configuration

Copy `.env.example` for the complete variable list. All database credentials,
the Supabase service-role key, Turnstile secret, and Shared Auth service
credential are required. Production secrets are supplied through the existing
SOPS/age runtime entrypoint; they are never embedded in the container image.

`CORS_ORIGINS` is a comma-separated list of exact origins. Remote origins must
use HTTPS and cannot include paths, queries, fragments, or wildcards.

## Development

The API requires Rust 1.94 or later.

```bash
cp .env.example .env
cargo fmt --all --check
cargo check --locked --all-targets --all-features
cargo clippy --all-targets --all-features -- -D warnings
cargo test --locked --all-targets --all-features
```

The unit suite does not need live secrets. Production startup is intentionally
fail-closed unless every required integration is configured.

## Schema and deployment

Apply reviewed migrations from `hhm-lib-core` to the primary cluster database
and the dedicated Supabase project before deploying this server. The service
does not create or repair tables. See [`docs/architecture.md`](docs/architecture.md)
for the ownership and request flow.

Encrypted environment workflow details remain in [`env/README.md`](env/README.md).
