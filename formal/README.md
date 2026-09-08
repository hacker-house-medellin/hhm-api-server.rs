# H/HAUS API formal verification

`intake_api.qnt` models the API-owned transition systems that sit above the
PostgreSQL capability layer:

- a request is admitted only after either an active Shared Auth subject or a
  verified public anti-abuse proof;
- the idempotency key and canonical payload digest are bound together before a
  primary write;
- Supabase mirror persistence follows the primary write;
- the primary outbox is marked mirrored before an accepted submission receipt;
- a failed mirror write records a retryable failure and does not issue a false
  acceptance receipt; and
- upload completion follows primary metadata lookup, digest comparison,
  cross-database identity equality, object-store verification, mirror
  verification, and finally primary verification.

Quint 0.32.0 samples adversarial schedules and TLC exhaustively enumerates the
finite abstraction under the `api_safety` invariant. Replay and rejected-upload
paths are explicit stuttering transitions: they cannot create accepted effects.

`check_refinement.py` checks the production handler ordering and cryptographic,
authentication, idempotency, and upload-identity anchors, then emits a
SHA-256-bound JSON receipt. This is refinement evidence for source structure;
it does not claim that a live PostgreSQL, Supabase, Shared Auth, Turnstile, or
object-storage deployment has been verified. Those remain integration and
deployment gates.
