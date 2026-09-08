// Finite abstraction of the H/HAUS API authentication, dual-persistence, and
// private-upload completion state machines. Failure and replay actions stutter
// with respect to accepted effects.
module intake_api {
  type AuthDecision = AuthPending | PublicProofVerified | SubjectVerified | AuthRejected
  type UploadStatus = UploadAbsent | UploadPending | UploadVerified | UploadRejected

  type State = {
    auth: AuthDecision,
    request_validated: bool,
    idempotency_bound: bool,
    digest_bound: bool,

    primary_stored: bool,
    mirror_stored: bool,
    mirror_failure_recorded: bool,
    primary_marked_mirrored: bool,
    receipt_issued: bool,

    upload_status: UploadStatus,
    upload_primary_intent: bool,
    upload_mirror_intent: bool,
    upload_identity_matched: bool,
    upload_object_verified: bool,
    upload_mirror_verified: bool,
    upload_primary_verified: bool,
    upload_receipt_issued: bool,
  }

  var s: State

  action init = {
    s' = {
      auth: AuthPending,
      request_validated: false,
      idempotency_bound: false,
      digest_bound: false,

      primary_stored: false,
      mirror_stored: false,
      mirror_failure_recorded: false,
      primary_marked_mirrored: false,
      receipt_issued: false,

      upload_status: UploadAbsent,
      upload_primary_intent: false,
      upload_mirror_intent: false,
      upload_identity_matched: false,
      upload_object_verified: false,
      upload_mirror_verified: false,
      upload_primary_verified: false,
      upload_receipt_issued: false,
    }
  }

  action verify_public_proof = all {
    s.auth == AuthPending,
    s' = { ...s, auth: PublicProofVerified },
  }

  action verify_subject = all {
    s.auth == AuthPending,
    s' = { ...s, auth: SubjectVerified },
  }

  action reject_auth = all {
    s.auth == AuthPending,
    s' = { ...s, auth: AuthRejected },
  }

  action validate_request = all {
    or { s.auth == PublicProofVerified, s.auth == SubjectVerified },
    not(s.request_validated),
    s' = {
      ...s,
      request_validated: true,
      idempotency_bound: true,
      digest_bound: true,
    },
  }

  action store_primary = all {
    s.request_validated,
    not(s.primary_stored),
    s' = { ...s, primary_stored: true },
  }

  action store_mirror = all {
    s.primary_stored,
    not(s.mirror_stored),
    not(s.mirror_failure_recorded),
    s' = { ...s, mirror_stored: true },
  }

  action record_mirror_failure = all {
    s.primary_stored,
    not(s.mirror_stored),
    not(s.receipt_issued),
    s' = { ...s, mirror_failure_recorded: true },
  }

  action mark_primary_mirrored = all {
    s.primary_stored,
    s.mirror_stored,
    not(s.primary_marked_mirrored),
    s' = { ...s, primary_marked_mirrored: true },
  }

  action issue_submission_receipt = all {
    s.primary_marked_mirrored,
    not(s.receipt_issued),
    s' = { ...s, receipt_issued: true },
  }

  action exact_submission_replay = all {
    s.primary_stored,
    s' = s,
  }

  action conflicting_submission_replay = all {
    s.primary_stored,
    s' = s,
  }

  action create_upload_primary_intent = all {
    s.request_validated,
    s.upload_status == UploadAbsent,
    s' = {
      ...s,
      upload_status: UploadPending,
      upload_primary_intent: true,
    },
  }

  action create_upload_mirror_intent = all {
    s.upload_primary_intent,
    not(s.upload_mirror_intent),
    s' = { ...s, upload_mirror_intent: true },
  }

  action match_upload_identity = all {
    s.upload_primary_intent,
    s.upload_mirror_intent,
    not(s.upload_identity_matched),
    s' = { ...s, upload_identity_matched: true },
  }

  action reject_upload_identity = all {
    s.upload_primary_intent,
    s.upload_mirror_intent,
    not(s.upload_identity_matched),
    s' = { ...s, upload_status: UploadRejected },
  }

  action verify_storage_object = all {
    s.upload_status == UploadPending,
    s.upload_identity_matched,
    not(s.upload_object_verified),
    s' = { ...s, upload_object_verified: true },
  }

  action verify_upload_mirror = all {
    s.upload_object_verified,
    not(s.upload_mirror_verified),
    s' = { ...s, upload_mirror_verified: true },
  }

  action verify_upload_primary = all {
    s.upload_mirror_verified,
    not(s.upload_primary_verified),
    s' = {
      ...s,
      upload_primary_verified: true,
      upload_status: UploadVerified,
    },
  }

  action issue_upload_receipt = all {
    s.upload_primary_verified,
    not(s.upload_receipt_issued),
    s' = { ...s, upload_receipt_issued: true },
  }

  action rejected_upload_retry = all {
    s.upload_status == UploadRejected,
    s' = s,
  }

  action idle = s' = s

  action step = any {
    verify_public_proof,
    verify_subject,
    reject_auth,
    validate_request,
    store_primary,
    store_mirror,
    record_mirror_failure,
    mark_primary_mirrored,
    issue_submission_receipt,
    exact_submission_replay,
    conflicting_submission_replay,
    create_upload_primary_intent,
    create_upload_mirror_intent,
    match_upload_identity,
    reject_upload_identity,
    verify_storage_object,
    verify_upload_mirror,
    verify_upload_primary,
    issue_upload_receipt,
    rejected_upload_retry,
    idle,
  }

  val accepted_auth_precedes_validation =
    not(s.request_validated) or or {
      s.auth == PublicProofVerified,
      s.auth == SubjectVerified,
    }

  val request_identity_is_atomic =
    s.request_validated == and { s.idempotency_bound, s.digest_bound }

  val dual_persistence_order = and {
    not(s.primary_stored) or s.request_validated,
    not(s.mirror_stored) or s.primary_stored,
    not(s.primary_marked_mirrored) or and { s.primary_stored, s.mirror_stored },
    not(s.receipt_issued) or s.primary_marked_mirrored,
    not(s.mirror_failure_recorded) or and {
      s.primary_stored,
      not(s.mirror_stored),
      not(s.receipt_issued),
    },
  }

  val upload_completion_order = and {
    not(s.upload_mirror_intent) or s.upload_primary_intent,
    not(s.upload_identity_matched) or and {
      s.upload_primary_intent,
      s.upload_mirror_intent,
    },
    not(s.upload_object_verified) or and {
      or { s.upload_status == UploadPending, s.upload_status == UploadVerified },
      s.upload_identity_matched,
    },
    not(s.upload_mirror_verified) or s.upload_object_verified,
    not(s.upload_primary_verified) or s.upload_mirror_verified,
    not(s.upload_receipt_issued) or and {
      s.upload_status == UploadVerified,
      s.upload_primary_verified,
    },
    (s.upload_status == UploadRejected).implies(and {
      not(s.upload_object_verified),
      not(s.upload_mirror_verified),
      not(s.upload_primary_verified),
      not(s.upload_receipt_issued),
    }),
  }

  val api_safety = and {
    accepted_auth_precedes_validation,
    request_identity_is_atomic,
    dual_persistence_order,
    upload_completion_order,
  }
}
