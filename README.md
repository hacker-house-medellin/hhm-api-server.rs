# hhm-api-server.rs

**Hacker House Medellín — Rust REST and WebSocket API server**

Operations and community software for an entrepreneur-focused coliving and coworking house in Medellín, Colombia.

This repository is an independently deployable component and a member of the `hhm-monorepo` workspace.

## Baseline

- Rust 2024 edition.
- Axum HTTP and WebSocket transport.
- SeaORM/PostgreSQL connection through `DATABASE_URL`.
- Supabase configuration through `SUPABASE_URL` and environment-only secrets.
- OpenTelemetry-compatible tracing hooks.
- Docker and GitHub Actions entry points.
- Contracts live in `hhm-interfaces`; shared behavior belongs in `hhm-libs`.

## Implemented routes

- `GET /healthz`
- `GET /v1/reservations`
- `POST /v1/reservations`
- `GET /v1/reservations/{id}`
- `GET /v1/ws`

The current reservation store is process-local. A configured database connection is reported by `/healthz`, but persistence and migrations remain a separate delivery gate.

## Reservation boundary

Creation accepts a JSON object with:

- `member_name`
- `room_type`
- `check_in`
- `check_out`
- `workspace_plan`
- `status`
- `notes`

Text fields are trimmed and bounded. `check_out` must be later than `check_in`, a stay may not exceed 366 days, and status must be one of `pending`, `confirmed`, `checked_in`, `checked_out`, or `cancelled`. Invalid input receives a typed `422` response.

Successful creation broadcasts a typed `reservation.created` envelope. Lagged WebSocket consumers skip dropped broadcast items and continue receiving subsequent events.

## CORS

`CORS_ORIGINS` is a comma-separated list of exact `http` or `https` origins. Wildcards, paths, and query strings are rejected at startup. Leaving it empty allows same-origin use while emitting no cross-origin allow header.

Example:

```dotenv
CORS_ORIGINS=http://localhost:3000,https://app.example.test
```

## Development

```bash
cp .env.example .env
cargo fmt --check
cargo clippy --all-targets --all-features -- -D warnings
cargo test --all-targets --all-features
```

## Deployment boundary

Authentication, durable reservation persistence, migrations, tenant isolation, rate limiting, and production secrets must be completed and reviewed before deployment. Do not treat the in-memory scaffold as a production booking system.

## Environment secrets

Secrets live in this repo **encrypted** with [sops](https://github.com/getsops/sops) + [age](https://github.com/FiloSottile/age):
`env/enc/<dev|prod>.env.enc` is committed; `just env-use <name>` decrypts it to
`env/dec/<name>.env` (gitignored, mode 0600) and symlinks `./.env` to it. The
Nix dev shell provides the tooling, `just env-audit` runs keyless in CI, and
containers decrypt at `docker run` — never at build. See [`env/README.md`](env/README.md).
