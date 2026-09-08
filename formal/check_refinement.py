#!/usr/bin/env python3
"""Bind the Quint API model to the production handler ordering."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API = ROOT / "src" / "api.rs"
AUTH = ROOT / "src" / "auth.rs"
MODEL = ROOT / "formal" / "intake_api.qnt"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require(text: str, fragment: str, label: str) -> None:
    if fragment not in text:
        raise SystemExit(f"missing API refinement anchor: {label}")


def require_order(text: str, fragments: list[tuple[str, str]], label: str) -> None:
    positions: list[int] = []
    for name, fragment in fragments:
        position = text.find(fragment)
        if position < 0:
            raise SystemExit(f"missing API refinement anchor in {label}: {name}")
        positions.append(position)
    if positions != sorted(positions):
        raise SystemExit(f"production ordering no longer refines {label}")


def main() -> None:
    api = API.read_text(encoding="utf-8")
    auth = AUTH.read_text(encoding="utf-8")
    model = MODEL.read_text(encoding="utf-8")

    require_order(
        api,
        [
            ("validate", "input.validate().map_err"),
            ("authenticate", "authenticate_or_verify_public"),
            ("context", "submission_context("),
            ("primary", ".store_pre_interest(PersistenceTarget::Primary"),
            ("mirror", ".store_pre_interest(PersistenceTarget::SupabaseMirror"),
            ("finish", "finish_submission("),
        ],
        "pre-interest dual persistence",
    )
    require_order(
        api,
        [
            ("primary pending metadata", ".primary\n        .pending_upload"),
            ("expected digest comparison", "input.sha256 != primary_upload.expected_sha256"),
            ("mirror pending metadata", ".supabase\n        .pending_upload"),
            ("cross-database identity", "same_upload_identity(&primary_upload, &mirror_upload)"),
            ("object-store verification", ".storage\n        .verify_object"),
            ("mirror verification", ".supabase\n        .verify_upload"),
            ("primary verification", ".primary\n        .verify_upload"),
            ("verified receipt", "status: UploadCompletionStatus::Verified"),
        ],
        "upload completion",
    )
    require_order(
        api,
        [
            ("mark mirrored", ".mark_mirrored(stored_kind, primary.id"),
            ("accepted receipt", "Json(SubmissionReceipt"),
        ],
        "receipt admission",
    )

    api_anchors = {
        "canonical mirror id": "context.with_canonical_id(primary.id)",
        "bounded mirror failure": "record_mirror_failure(kind, id, \"supabase_unavailable\")",
        "turnstile removed from digest": "object.remove(\"turnstileToken\")",
        "exact idempotency header": "HeaderName::from_static(\"idempotency-key\")",
        "public-or-subject decision": "optional_subject(headers)",
        "fail-closed public proof": "proof.ok_or_else(ApiFailure::invalid)?",
        "upload identity id": "primary.id == mirror.id",
        "upload identity key": "primary.object_key == mirror.object_key",
        "upload identity digest": "primary.expected_sha256 == mirror.expected_sha256",
        "upload identity media type": "primary.content_type == mirror.content_type",
        "upload identity size": "primary.size_bytes == mirror.size_bytes",
    }
    for label, fragment in api_anchors.items():
        require(api, fragment, label)

    auth_anchors = {
        "redirect refusal": ".redirect(Policy::none())",
        "bounded response": "MAX_RESPONSE_BYTES",
        "exact audience": "introspection.aud.as_deref() == Some(audience)",
        "write scope": "hhm:intake:write",
        "active claim": "introspection.active",
    }
    for label, fragment in auth_anchors.items():
        require(auth, fragment, label)

    model_anchors = {
        "auth order": "accepted_auth_precedes_validation",
        "request binding": "request_identity_is_atomic",
        "dual persistence": "dual_persistence_order",
        "upload completion": "upload_completion_order",
        "aggregate invariant": "api_safety",
    }
    for label, fragment in model_anchors.items():
        require(model, fragment, label)

    print(
        json.dumps(
            {
                "schema": "hhaus.api-formal-refinement.v1",
                "model": "intake_api",
                "invariant": "api_safety",
                "apiSha256": sha256(API),
                "authSha256": sha256(AUTH),
                "modelSha256": sha256(MODEL),
                "claims": [
                    "authentication or public proof precedes mutation",
                    "receipt follows canonical dual persistence",
                    "upload completion follows identity and object verification",
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )


if __name__ == "__main__":
    main()
