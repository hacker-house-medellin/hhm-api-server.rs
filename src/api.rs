use axum::{
    Json, Router,
    extract::{Path, State},
    http::{
        HeaderMap, HeaderName, HeaderValue, Method, StatusCode, Uri,
        header::{AUTHORIZATION, CONTENT_TYPE, ORIGIN},
    },
    response::{IntoResponse, Response},
    routing::{get, post},
};
use hhm_interfaces::intake::{
    ApplicationCreate, MAX_UPLOAD_BYTES, PersistenceReceipt, PreInterestCreate, ReferralCreate,
    SubmissionKind, SubmissionReceipt, UploadCompleteCreate, UploadCompletionReceipt,
    UploadCompletionStatus, UploadIntentCreate, UploadIntentReceipt, UploadKind,
};
use hhm_orm_core::{
    DataError, PersistenceTarget, StoredSubmission, StoredSubmissionKind, SubmissionContext,
    WriteContext,
};
use secrecy::ExposeSecret;
use serde::Serialize;
use sha2::{Digest, Sha256};
use tower_http::{
    cors::{AllowOrigin, CorsLayer},
    trace::TraceLayer,
};
use tracing::warn;
use uuid::Uuid;

use crate::{
    auth::{AuthError, Authenticator},
    config::Config,
    external::{ExternalError, SupabaseStorage, TurnstileVerifier},
};

const IDEMPOTENCY_KEY: HeaderName = HeaderName::from_static("idempotency-key");

#[derive(Clone)]
pub struct AppState {
    primary: WriteContext,
    supabase: WriteContext,
    auth: Authenticator,
    turnstile: TurnstileVerifier,
    storage: SupabaseStorage,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct ApiErrorBody {
    code: &'static str,
    message: &'static str,
    retryable: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    submission_id: Option<Uuid>,
}

#[derive(Debug)]
struct ApiFailure {
    status: StatusCode,
    body: ApiErrorBody,
}

#[derive(Serialize)]
struct Health {
    service: &'static str,
    status: &'static str,
}

impl AppState {
    /// Creates outbound clients and both explicit database capabilities.
    ///
    /// # Errors
    ///
    /// Fails startup unless both databases and the Shared Auth client can be
    /// initialized. It never runs migrations.
    pub async fn from_config(config: &Config) -> anyhow::Result<Self> {
        let http = reqwest::Client::builder()
            .user_agent("hhm-api/0.1")
            .redirect(reqwest::redirect::Policy::none())
            .timeout(std::time::Duration::from_secs(15))
            .build()?;
        let primary = WriteContext::connect(config.primary_database_url.expose_secret()).await?;
        let supabase = WriteContext::connect(config.supabase_database_url.expose_secret()).await?;
        let auth = Authenticator::from_config(config)?;
        Ok(Self {
            primary,
            supabase,
            auth,
            turnstile: TurnstileVerifier::from_config(config, http.clone()),
            storage: SupabaseStorage::from_config(config, http),
        })
    }
}

/// Builds the versioned intake router with exact-origin CORS.
///
/// # Errors
///
/// Returns an error for wildcard, non-HTTPS, or path-bearing origins.
pub fn router(state: AppState, cors_origins: &str) -> anyhow::Result<Router> {
    let cors = cors_layer(cors_origins)?;
    Ok(Router::new()
        .route("/healthz", get(health))
        .route("/readyz", get(readiness))
        .route("/v1/pre-interests", post(create_pre_interest))
        .route("/v1/intake/uploads", post(create_upload_intent))
        .route(
            "/v1/intake/uploads/{upload_id}/complete",
            post(complete_upload),
        )
        .route("/v1/applications", post(create_application))
        .route("/v1/referrals", post(create_referral))
        .layer(cors)
        .layer(TraceLayer::new_for_http())
        .with_state(state))
}

async fn health() -> Json<Health> {
    Json(Health {
        service: "hhm-api",
        status: "ok",
    })
}

async fn readiness(State(state): State<AppState>) -> Result<Json<Health>, ApiFailure> {
    tokio::try_join!(state.primary.ping(), state.supabase.ping()).map_err(ApiFailure::from)?;
    Ok(Json(Health {
        service: "hhm-api",
        status: "ready",
    }))
}

async fn create_pre_interest(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(input): Json<PreInterestCreate>,
) -> Result<(StatusCode, Json<SubmissionReceipt>), ApiFailure> {
    input.validate().map_err(|_| ApiFailure::invalid())?;
    state
        .turnstile
        .verify(&input.turnstile_token)
        .await
        .map_err(ApiFailure::from)?;
    let context = submission_context(
        &state,
        &headers,
        canonical_digest_without_turnstile(&input)?,
    )
    .await?;
    let primary = state
        .primary
        .store_pre_interest(PersistenceTarget::Primary, &context, &input)
        .await
        .map_err(ApiFailure::from)?;
    let mirror_context = context.with_canonical_id(primary.id);
    if state
        .supabase
        .store_pre_interest(PersistenceTarget::SupabaseMirror, &mirror_context, &input)
        .await
        .is_err()
    {
        record_failure(&state, StoredSubmissionKind::PreInterest, primary.id).await;
        return Err(ApiFailure::mirror_unavailable(primary.id));
    }
    finish_submission(
        &state,
        StoredSubmissionKind::PreInterest,
        SubmissionKind::PreInterest,
        &context,
        primary,
    )
    .await
}

async fn create_upload_intent(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(input): Json<UploadIntentCreate>,
) -> Result<(StatusCode, Json<UploadIntentReceipt>), ApiFailure> {
    input.validate().map_err(|_| ApiFailure::invalid())?;
    state
        .turnstile
        .verify(&input.turnstile_token)
        .await
        .map_err(ApiFailure::from)?;
    let context = submission_context(
        &state,
        &headers,
        canonical_digest_without_turnstile(&input)?,
    )
    .await?;
    let primary = state
        .primary
        .store_upload_intent(PersistenceTarget::Primary, &context, &input)
        .await
        .map_err(ApiFailure::from)?;
    let mirror_context = context.with_canonical_id(primary.id);
    if state
        .supabase
        .store_upload_intent(PersistenceTarget::SupabaseMirror, &mirror_context, &input)
        .await
        .is_err()
    {
        record_failure(&state, StoredSubmissionKind::Upload, primary.id).await;
        return Err(ApiFailure::mirror_unavailable(primary.id));
    }
    state
        .primary
        .mark_mirrored(
            StoredSubmissionKind::Upload,
            primary.id,
            context_payload(&context),
        )
        .await
        .map_err(|_| ApiFailure::mirror_unavailable(primary.id))?;
    let upload_url = state
        .storage
        .create_signed_upload_url(&primary.object_key)
        .await
        .map_err(ApiFailure::from)?;
    Ok((
        StatusCode::CREATED,
        Json(UploadIntentReceipt {
            upload_id: primary.id,
            kind: input.kind,
            upload_url,
            expires_at: primary.expires_at,
            maximum_bytes: MAX_UPLOAD_BYTES,
            allowed_content_types: allowed_content_types(input.kind),
        }),
    ))
}

async fn complete_upload(
    State(state): State<AppState>,
    Path(upload_id): Path<Uuid>,
    headers: HeaderMap,
    Json(input): Json<UploadCompleteCreate>,
) -> Result<Json<UploadCompletionReceipt>, ApiFailure> {
    input.validate().map_err(|_| ApiFailure::invalid())?;
    state
        .turnstile
        .verify(&input.turnstile_token)
        .await
        .map_err(ApiFailure::from)?;
    let subject = state
        .auth
        .optional_subject(&headers)
        .await
        .map_err(ApiFailure::from)?;
    let primary_upload = state
        .primary
        .pending_upload(upload_id, subject.as_ref())
        .await
        .map_err(ApiFailure::from)?;
    if input.sha256 != primary_upload.expected_sha256 {
        return Err(ApiFailure::conflict());
    }
    let mirror_upload = state
        .supabase
        .pending_upload(upload_id, subject.as_ref())
        .await
        .map_err(ApiFailure::from)?;
    // Each database computes its own bounded upload expiry, so the timestamps
    // can differ by milliseconds. Compare the immutable object identity rather
    // than treating that operational deadline as cross-database content.
    if !same_upload_identity(&primary_upload, &mirror_upload) {
        return Err(ApiFailure::conflict());
    }
    state
        .storage
        .verify_object(&primary_upload)
        .await
        .map_err(ApiFailure::from)?;
    state
        .supabase
        .verify_upload(upload_id, subject.as_ref(), &input.sha256)
        .await
        .map_err(ApiFailure::from)?;
    state
        .primary
        .verify_upload(upload_id, subject.as_ref(), &input.sha256)
        .await
        .map_err(ApiFailure::from)?;
    Ok(Json(UploadCompletionReceipt {
        upload_id,
        status: UploadCompletionStatus::Verified,
    }))
}

fn same_upload_identity(
    primary: &hhm_orm_core::PendingUpload,
    mirror: &hhm_orm_core::PendingUpload,
) -> bool {
    primary.id == mirror.id
        && primary.object_key == mirror.object_key
        && primary.expected_sha256 == mirror.expected_sha256
        && primary.content_type == mirror.content_type
        && primary.size_bytes == mirror.size_bytes
}

async fn create_application(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(input): Json<ApplicationCreate>,
) -> Result<(StatusCode, Json<SubmissionReceipt>), ApiFailure> {
    input.validate().map_err(|_| ApiFailure::invalid())?;
    state
        .turnstile
        .verify(&input.turnstile_token)
        .await
        .map_err(ApiFailure::from)?;
    let context = submission_context(
        &state,
        &headers,
        canonical_digest_without_turnstile(&input)?,
    )
    .await?;
    let primary = state
        .primary
        .store_application(PersistenceTarget::Primary, &context, &input)
        .await
        .map_err(ApiFailure::from)?;
    let mirror_context = context.with_canonical_id(primary.id);
    if state
        .supabase
        .store_application(PersistenceTarget::SupabaseMirror, &mirror_context, &input)
        .await
        .is_err()
    {
        record_failure(&state, StoredSubmissionKind::Application, primary.id).await;
        return Err(ApiFailure::mirror_unavailable(primary.id));
    }
    finish_submission(
        &state,
        StoredSubmissionKind::Application,
        SubmissionKind::Application,
        &context,
        primary,
    )
    .await
}

async fn create_referral(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(input): Json<ReferralCreate>,
) -> Result<(StatusCode, Json<SubmissionReceipt>), ApiFailure> {
    input.validate().map_err(|_| ApiFailure::invalid())?;
    let subject = state
        .auth
        .required_subject(&headers)
        .await
        .map_err(ApiFailure::from)?;
    let context = SubmissionContext::authenticated(
        subject,
        idempotency_key(&headers)?,
        canonical_digest(&input)?,
        source_host(&headers)?,
    )
    .map_err(ApiFailure::from)?;
    let primary = state
        .primary
        .store_referral(PersistenceTarget::Primary, &context, &input)
        .await
        .map_err(ApiFailure::from)?;
    let mirror_context = context.with_canonical_id(primary.id);
    if state
        .supabase
        .store_referral(PersistenceTarget::SupabaseMirror, &mirror_context, &input)
        .await
        .is_err()
    {
        record_failure(&state, StoredSubmissionKind::Referral, primary.id).await;
        return Err(ApiFailure::mirror_unavailable(primary.id));
    }
    finish_submission(
        &state,
        StoredSubmissionKind::Referral,
        SubmissionKind::Referral,
        &context,
        primary,
    )
    .await
}

async fn submission_context(
    state: &AppState,
    headers: &HeaderMap,
    payload_sha256: String,
) -> Result<SubmissionContext, ApiFailure> {
    let idempotency_key = idempotency_key(headers)?;
    let source_host = source_host(headers)?;
    match state
        .auth
        .optional_subject(headers)
        .await
        .map_err(ApiFailure::from)?
    {
        Some(subject) => {
            SubmissionContext::authenticated(subject, idempotency_key, payload_sha256, source_host)
        }
        None => SubmissionContext::public(idempotency_key, payload_sha256, source_host),
    }
    .map_err(ApiFailure::from)
}

async fn finish_submission(
    state: &AppState,
    stored_kind: StoredSubmissionKind,
    receipt_kind: SubmissionKind,
    context: &SubmissionContext,
    primary: StoredSubmission,
) -> Result<(StatusCode, Json<SubmissionReceipt>), ApiFailure> {
    state
        .primary
        .mark_mirrored(stored_kind, primary.id, context_payload(context))
        .await
        .map_err(|_| ApiFailure::mirror_unavailable(primary.id))?;
    Ok((
        StatusCode::CREATED,
        Json(SubmissionReceipt {
            submission_id: primary.id,
            kind: receipt_kind,
            accepted_at: primary.accepted_at,
            primary_persistence: PersistenceReceipt::Stored,
            supabase_persistence: PersistenceReceipt::Stored,
        }),
    ))
}

async fn record_failure(state: &AppState, kind: StoredSubmissionKind, id: Uuid) {
    if state
        .primary
        .record_mirror_failure(kind, id, "supabase_unavailable")
        .await
        .is_err()
    {
        warn!(kind = ?kind, submission_id = %id, "failed to update mirror outbox status");
    }
}

fn canonical_digest_without_turnstile<T>(input: &T) -> Result<String, ApiFailure>
where
    T: Serialize + Clone,
{
    let mut value = serde_json::to_value(input).map_err(|_| ApiFailure::invalid())?;
    let object = value.as_object_mut().ok_or_else(ApiFailure::invalid)?;
    object.remove("turnstileToken");
    canonical_digest(&value)
}

fn canonical_digest<T: Serialize>(input: &T) -> Result<String, ApiFailure> {
    let bytes = serde_json::to_vec(input).map_err(|_| ApiFailure::invalid())?;
    Ok(format!("{:x}", Sha256::digest(bytes)))
}

fn context_payload(context: &SubmissionContext) -> &str {
    // The digest has already passed the typed context constructor. Serializing
    // the context would risk including an auth subject, so this accessor remains
    // intentionally narrow inside the API-to-ORM boundary.
    context.payload_sha256()
}

fn idempotency_key(headers: &HeaderMap) -> Result<String, ApiFailure> {
    let value = headers
        .get(&IDEMPOTENCY_KEY)
        .ok_or_else(ApiFailure::invalid)?
        .to_str()
        .map_err(|_| ApiFailure::invalid())?;
    if !(16..=128).contains(&value.len())
        || value.contains(char::is_whitespace)
        || value.chars().any(char::is_control)
    {
        return Err(ApiFailure::invalid());
    }
    Ok(value.to_owned())
}

fn source_host(headers: &HeaderMap) -> Result<String, ApiFailure> {
    let origin = headers
        .get(ORIGIN)
        .ok_or_else(ApiFailure::invalid)?
        .to_str()
        .map_err(|_| ApiFailure::invalid())?;
    let url = url::Url::parse(origin).map_err(|_| ApiFailure::invalid())?;
    if url.scheme() != "https"
        || url.path() != "/"
        || url.query().is_some()
        || url.fragment().is_some()
    {
        return Err(ApiFailure::invalid());
    }
    let host = url.host_str().ok_or_else(ApiFailure::invalid)?;
    if !matches!(
        host,
        "hhaus.org" | "www.hhaus.org" | "user.hhaus.org" | "medellin.hhaus.org"
    ) {
        return Err(ApiFailure::invalid());
    }
    Ok(host.to_owned())
}

fn allowed_content_types(kind: UploadKind) -> Vec<String> {
    match kind {
        UploadKind::Resume => vec![
            "application/pdf".into(),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document".into(),
        ],
        UploadKind::PhotoId => vec![
            "application/pdf".into(),
            "image/jpeg".into(),
            "image/png".into(),
            "image/heic".into(),
        ],
    }
}

fn cors_layer(raw: &str) -> anyhow::Result<CorsLayer> {
    let mut origins = Vec::new();
    for origin in raw
        .split(',')
        .map(str::trim)
        .filter(|item| !item.is_empty())
    {
        if origin == "*" {
            anyhow::bail!("CORS_ORIGINS must not contain a wildcard");
        }
        let uri = origin.parse::<Uri>()?;
        if uri.scheme_str() != Some("https")
            || uri.authority().is_none()
            || uri.path() != "/"
            || uri.query().is_some()
        {
            anyhow::bail!("CORS origin must be an exact HTTPS origin");
        }
        let header = origin.parse::<HeaderValue>()?;
        if !origins.contains(&header) {
            origins.push(header);
        }
    }
    if origins.is_empty() {
        anyhow::bail!("CORS_ORIGINS must contain at least one exact origin");
    }
    Ok(CorsLayer::new()
        .allow_origin(AllowOrigin::list(origins))
        .allow_methods([Method::GET, Method::POST])
        .allow_headers([CONTENT_TYPE, AUTHORIZATION, IDEMPOTENCY_KEY]))
}

impl ApiFailure {
    const fn invalid() -> Self {
        Self::new(
            StatusCode::UNPROCESSABLE_ENTITY,
            "invalid_request",
            "The request did not satisfy the intake contract.",
            false,
            None,
        )
    }

    const fn conflict() -> Self {
        Self::new(
            StatusCode::CONFLICT,
            "intake_conflict",
            "The intake resource conflicts with its verified state.",
            false,
            None,
        )
    }

    const fn mirror_unavailable(id: Uuid) -> Self {
        Self::new(
            StatusCode::SERVICE_UNAVAILABLE,
            "dual_persistence_incomplete",
            "The submission is retained for retry but is not yet accepted.",
            true,
            Some(id),
        )
    }

    const fn new(
        status: StatusCode,
        code: &'static str,
        message: &'static str,
        retryable: bool,
        submission_id: Option<Uuid>,
    ) -> Self {
        Self {
            status,
            body: ApiErrorBody {
                code,
                message,
                retryable,
                submission_id,
            },
        }
    }
}

impl From<DataError> for ApiFailure {
    fn from(error: DataError) -> Self {
        match error {
            DataError::Validation(_) | DataError::InvalidContext(_) => Self::invalid(),
            DataError::IdempotencyConflict | DataError::IntakeObjectUnavailable => Self::conflict(),
            DataError::Unauthenticated => Self::from(AuthError::Missing),
            DataError::Connect(_)
            | DataError::Operation(_)
            | DataError::WritableReadContext
            | DataError::Decode
            | DataError::Invariant => Self::new(
                StatusCode::SERVICE_UNAVAILABLE,
                "persistence_unavailable",
                "The intake service could not complete persistence.",
                true,
                None,
            ),
        }
    }
}

impl From<AuthError> for ApiFailure {
    fn from(error: AuthError) -> Self {
        match error {
            AuthError::Missing | AuthError::Invalid => Self::new(
                StatusCode::UNAUTHORIZED,
                "authentication_required",
                "A valid Shared Auth session is required.",
                false,
                None,
            ),
            AuthError::Unavailable => Self::new(
                StatusCode::SERVICE_UNAVAILABLE,
                "authentication_unavailable",
                "Shared Auth could not make an authorization decision.",
                true,
                None,
            ),
        }
    }
}

impl From<ExternalError> for ApiFailure {
    fn from(error: ExternalError) -> Self {
        match error {
            ExternalError::Rejected => Self::invalid(),
            ExternalError::ObjectMismatch => Self::conflict(),
            ExternalError::Unavailable | ExternalError::InvalidResponse => Self::new(
                StatusCode::SERVICE_UNAVAILABLE,
                "external_verification_unavailable",
                "An external verification step could not be completed.",
                true,
                None,
            ),
        }
    }
}

impl IntoResponse for ApiFailure {
    fn into_response(self) -> Response {
        (self.status, Json(self.body)).into_response()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn canonical_digest_excludes_single_use_turnstile_proof() {
        let one = PreInterestCreate {
            email: "builder@example.com".into(),
            linkedin_url: "https://www.linkedin.com/in/builder".into(),
            entrepreneurship_idea:
                "A sufficiently detailed proposal that is stable across proof refreshes.".into(),
            stay_preference: hhm_interfaces::intake::StayPreference::ThreeMonths,
            privacy_notice_version: hhm_interfaces::intake::PRIVACY_NOTICE_VERSION.into(),
            turnstile_token: "proof-one".into(),
        };
        let mut two = one.clone();
        two.turnstile_token = "proof-two".into();
        assert_eq!(
            canonical_digest_without_turnstile(&one).unwrap(),
            canonical_digest_without_turnstile(&two).unwrap()
        );
    }

    #[test]
    fn cors_rejects_wildcards_and_paths() {
        assert!(cors_layer("*").is_err());
        assert!(cors_layer("https://hhaus.org/path").is_err());
        assert!(cors_layer("https://hhaus.org,https://user.hhaus.org").is_ok());
    }

    #[test]
    fn upload_identity_ignores_independent_expiry_clocks() {
        let base = serde_json::json!({
            "id": "0b46178c-c81d-4d28-8298-7f66a9467a4f",
            "objectKey": "intake/0b46178c-c81d-4d28-8298-7f66a9467a4f/resume.pdf",
            "expectedSha256": "a".repeat(64),
            "contentType": "application/pdf",
            "sizeBytes": 1024,
            "expiresAt": "2026-08-30T12:00:00Z"
        });
        let primary: hhm_orm_core::PendingUpload = serde_json::from_value(base.clone()).unwrap();
        let mut mirror_value = base;
        mirror_value["expiresAt"] = serde_json::json!("2026-08-30T12:00:00.001Z");
        let mirror: hhm_orm_core::PendingUpload = serde_json::from_value(mirror_value).unwrap();

        assert_ne!(primary, mirror);
        assert!(same_upload_identity(&primary, &mirror));
    }
}
