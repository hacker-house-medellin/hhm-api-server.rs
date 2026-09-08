//! Product-owned, bounded intake persistence.
//!
//! The public API consumes the public `hhm-interfaces` contract and the public
//! `hhm-lib-core` schema. It deliberately does not depend on the private runtime
//! crate: hosted builds must be reproducible with public source
//! dependencies, while database access remains constrained to named commands.

use chrono::{DateTime, Duration, Utc};
use hhm_interfaces::intake::{
    ApplicationCreate, IntakeValidationError, PreInterestCreate, ProjectStage, ReferralCreate,
    RoommatePreference, SensitivityLevel, StayPreference, UploadIntentCreate, UploadKind,
};
use sea_orm::{
    ConnectionTrait, Database, DatabaseBackend, DatabaseConnection, DatabaseTransaction,
    QueryResult, Statement, TransactionTrait, TryGetable,
};
use uuid::Uuid;

const UPLOAD_LIFETIME_HOURS: i64 = 2;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct VerifiedSubject(String);

impl VerifiedSubject {
    pub fn from_verified_claim(value: impl Into<String>) -> Result<Self, DataError> {
        let value = value.into();
        if value.is_empty()
            || value.len() > 255
            || value.trim() != value
            || value.chars().any(char::is_control)
        {
            return Err(DataError::InvalidContext("subject"));
        }
        Ok(Self(value))
    }

    fn as_str(&self) -> &str {
        &self.0
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SubmissionContext {
    canonical_id: Uuid,
    subject: Option<VerifiedSubject>,
    idempotency_key: String,
    payload_sha256: String,
    source_host: String,
}

impl SubmissionContext {
    pub fn public(
        idempotency_key: impl Into<String>,
        payload_sha256: impl Into<String>,
        source_host: impl Into<String>,
    ) -> Result<Self, DataError> {
        Self::new(None, idempotency_key, payload_sha256, source_host)
    }

    pub fn authenticated(
        subject: VerifiedSubject,
        idempotency_key: impl Into<String>,
        payload_sha256: impl Into<String>,
        source_host: impl Into<String>,
    ) -> Result<Self, DataError> {
        Self::new(Some(subject), idempotency_key, payload_sha256, source_host)
    }

    fn new(
        subject: Option<VerifiedSubject>,
        idempotency_key: impl Into<String>,
        payload_sha256: impl Into<String>,
        source_host: impl Into<String>,
    ) -> Result<Self, DataError> {
        let idempotency_key = idempotency_key.into();
        let payload_sha256 = payload_sha256.into();
        let source_host = source_host.into().to_ascii_lowercase();
        if !(16..=128).contains(&idempotency_key.len())
            || !idempotency_key.bytes().all(|byte| {
                byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b':' | b'.')
            })
        {
            return Err(DataError::InvalidContext("idempotency_key"));
        }
        if payload_sha256.len() != 64
            || !payload_sha256
                .bytes()
                .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
        {
            return Err(DataError::InvalidContext("payload_sha256"));
        }
        if !matches!(
            source_host.as_str(),
            "hhaus.org"
                | "www.hhaus.org"
                | "user.hhaus.org"
                | "medellin.hhaus.org"
                | "localhost"
                | "127.0.0.1"
        ) {
            return Err(DataError::InvalidContext("source_host"));
        }
        Ok(Self {
            canonical_id: Uuid::new_v4(),
            subject,
            idempotency_key,
            payload_sha256,
            source_host,
        })
    }

    #[must_use]
    pub fn with_canonical_id(&self, canonical_id: Uuid) -> Self {
        Self {
            canonical_id,
            ..self.clone()
        }
    }

    #[must_use]
    pub fn payload_sha256(&self) -> &str {
        &self.payload_sha256
    }

    fn subject(&self) -> Option<&str> {
        self.subject.as_ref().map(VerifiedSubject::as_str)
    }

    fn require_subject(&self) -> Result<&str, DataError> {
        self.subject().ok_or(DataError::Unauthenticated)
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PersistenceTarget {
    Primary,
    SupabaseMirror,
}

impl PersistenceTarget {
    const fn mirror_status(self) -> &'static str {
        match self {
            Self::Primary => "pending",
            Self::SupabaseMirror => "mirrored",
        }
    }

    const fn creates_outbox(self) -> bool {
        matches!(self, Self::Primary)
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum StoredSubmissionKind {
    PreInterest,
    Upload,
    Application,
    Referral,
}

impl StoredSubmissionKind {
    const fn database_value(self) -> &'static str {
        match self {
            Self::PreInterest => "pre_interest",
            Self::Upload => "upload",
            Self::Application => "application",
            Self::Referral => "referral",
        }
    }

    const fn table_name(self) -> &'static str {
        match self {
            Self::PreInterest => "hhm_pre_interests",
            Self::Upload => "hhm_intake_uploads",
            Self::Application => "hhm_applications",
            Self::Referral => "hhm_referrals",
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct StoredSubmission {
    pub id: Uuid,
    pub accepted_at: DateTime<Utc>,
    pub replayed: bool,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct StoredUpload {
    pub id: Uuid,
    pub object_key: String,
    pub expires_at: DateTime<Utc>,
    pub replayed: bool,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PendingUpload {
    pub id: Uuid,
    pub object_key: String,
    pub expected_sha256: String,
    pub content_type: String,
    pub size_bytes: u64,
    pub expires_at: DateTime<Utc>,
}

#[derive(Debug, thiserror::Error)]
pub enum DataError {
    #[error("input failed the intake contract")]
    Validation(#[from] IntakeValidationError),
    #[error("invalid persistence context: {0}")]
    InvalidContext(&'static str),
    #[error("idempotency key conflicts with a different payload")]
    IdempotencyConflict,
    #[error("intake object is unavailable")]
    IntakeObjectUnavailable,
    #[error("authentication is required")]
    Unauthenticated,
    #[error("persistence is unavailable")]
    Database,
    #[error("stored data did not satisfy the schema projection")]
    Decode,
    #[error("persistence invariant failed")]
    Invariant,
}

impl From<sea_orm::DbErr> for DataError {
    fn from(_: sea_orm::DbErr) -> Self {
        Self::Database
    }
}

#[derive(Clone)]
pub struct WriteContext {
    database: DatabaseConnection,
}

impl WriteContext {
    pub async fn connect(database_url: &str) -> Result<Self, DataError> {
        let database = Database::connect(database_url).await?;
        Ok(Self { database })
    }

    pub async fn ping(&self) -> Result<(), DataError> {
        self.database.execute_unprepared("SELECT 1").await?;
        Ok(())
    }

    pub async fn store_pre_interest(
        &self,
        target: PersistenceTarget,
        context: &SubmissionContext,
        input: &PreInterestCreate,
    ) -> Result<StoredSubmission, DataError> {
        input.validate()?;
        let transaction = self.database.begin().await?;
        let stored = insert_or_replay(
            &transaction,
            context,
            "INSERT INTO hhm_pre_interests (
                id, idempotency_key, payload_sha256, applicant_subject, email,
                linkedin_url, entrepreneurship_idea, stay_preference,
                privacy_notice_version, source_host, mirror_status
             ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
             ON CONFLICT (idempotency_key) DO NOTHING
             RETURNING id, created_at AS accepted_at, payload_sha256",
            vec![
                context.canonical_id.into(),
                context.idempotency_key.clone().into(),
                context.payload_sha256.clone().into(),
                context.subject().map(str::to_owned).into(),
                input.email.trim().to_owned().into(),
                input.linkedin_url.trim().to_owned().into(),
                input.entrepreneurship_idea.trim().to_owned().into(),
                stay_preference(input.stay_preference).into(),
                input.privacy_notice_version.clone().into(),
                context.source_host.clone().into(),
                target.mirror_status().into(),
            ],
            "SELECT id, created_at AS accepted_at, payload_sha256
             FROM hhm_pre_interests WHERE idempotency_key = $1",
        )
        .await?;
        maybe_insert_outbox(
            &transaction,
            target,
            StoredSubmissionKind::PreInterest,
            &stored,
            context,
        )
        .await?;
        transaction.commit().await?;
        Ok(stored)
    }

    pub async fn store_upload_intent(
        &self,
        target: PersistenceTarget,
        context: &SubmissionContext,
        input: &UploadIntentCreate,
    ) -> Result<StoredUpload, DataError> {
        input.validate()?;
        let transaction = self.database.begin().await?;
        let expires_at = Utc::now() + Duration::hours(UPLOAD_LIFETIME_HOURS);
        let object_key = format!(
            "intake/{}/{}/{}",
            upload_kind(input.kind),
            context.canonical_id,
            &input.sha256[..16]
        );
        let inserted = transaction
            .query_one_raw(Statement::from_sql_and_values(
                DatabaseBackend::Postgres,
                "INSERT INTO hhm_intake_uploads (
                    id, idempotency_key, payload_sha256, applicant_subject, kind,
                    object_key, content_sha256, content_type, size_bytes, status, expires_at
                 ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'pending', $10)
                 ON CONFLICT (idempotency_key) DO NOTHING
                 RETURNING id, object_key, expires_at, payload_sha256",
                vec![
                    context.canonical_id.into(),
                    context.idempotency_key.clone().into(),
                    context.payload_sha256.clone().into(),
                    context.subject().map(str::to_owned).into(),
                    upload_kind(input.kind).into(),
                    object_key.into(),
                    input.sha256.clone().into(),
                    input.content_type.clone().into(),
                    i64::try_from(input.size_bytes)
                        .map_err(|_| DataError::Invariant)?
                        .into(),
                    expires_at.into(),
                ],
            ))
            .await?;
        let (id, object_key, expires_at, replayed) = match inserted {
            Some(row) => (
                read_uuid(&row, "id")?,
                read_string(&row, "object_key")?,
                read_timestamp(&row, "expires_at")?,
                false,
            ),
            None => {
                let row = transaction
                    .query_one_raw(Statement::from_sql_and_values(
                        DatabaseBackend::Postgres,
                        "SELECT id, object_key, expires_at, payload_sha256
                         FROM hhm_intake_uploads WHERE idempotency_key = $1",
                        [context.idempotency_key.clone().into()],
                    ))
                    .await?
                    .ok_or(DataError::Invariant)?;
                require_same_payload(&row, context.payload_sha256())?;
                (
                    read_uuid(&row, "id")?,
                    read_string(&row, "object_key")?,
                    read_timestamp(&row, "expires_at")?,
                    true,
                )
            }
        };
        let stored = StoredSubmission {
            id,
            accepted_at: expires_at,
            replayed,
        };
        maybe_insert_outbox(
            &transaction,
            target,
            StoredSubmissionKind::Upload,
            &stored,
            context,
        )
        .await?;
        transaction.commit().await?;
        Ok(StoredUpload {
            id,
            object_key,
            expires_at,
            replayed,
        })
    }

    pub async fn pending_upload(
        &self,
        upload_id: Uuid,
        subject: Option<&VerifiedSubject>,
    ) -> Result<PendingUpload, DataError> {
        let row = self
            .database
            .query_one_raw(Statement::from_sql_and_values(
                DatabaseBackend::Postgres,
                "SELECT id, object_key, content_sha256, content_type, size_bytes, expires_at
                 FROM hhm_intake_uploads
                 WHERE id = $1
                   AND applicant_subject IS NOT DISTINCT FROM $2
                   AND expires_at > transaction_timestamp()
                   AND status IN ('pending', 'uploaded', 'verified')",
                vec![
                    upload_id.into(),
                    subject
                        .map(VerifiedSubject::as_str)
                        .map(str::to_owned)
                        .into(),
                ],
            ))
            .await?
            .ok_or(DataError::IntakeObjectUnavailable)?;
        let size_bytes: i64 = row
            .try_get("", "size_bytes")
            .map_err(|_| DataError::Decode)?;
        Ok(PendingUpload {
            id: read_uuid(&row, "id")?,
            object_key: read_string(&row, "object_key")?,
            expected_sha256: read_string(&row, "content_sha256")?,
            content_type: read_string(&row, "content_type")?,
            size_bytes: u64::try_from(size_bytes).map_err(|_| DataError::Decode)?,
            expires_at: read_timestamp(&row, "expires_at")?,
        })
    }

    pub async fn verify_upload(
        &self,
        upload_id: Uuid,
        subject: Option<&VerifiedSubject>,
        observed_sha256: &str,
    ) -> Result<(), DataError> {
        if observed_sha256.len() != 64
            || !observed_sha256
                .bytes()
                .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
        {
            return Err(DataError::InvalidContext("observed_sha256"));
        }
        let result = self
            .database
            .execute_raw(Statement::from_sql_and_values(
                DatabaseBackend::Postgres,
                "UPDATE hhm_intake_uploads
                 SET status = 'verified',
                     uploaded_at = COALESCE(uploaded_at, transaction_timestamp()),
                     verified_at = COALESCE(verified_at, transaction_timestamp())
                 WHERE id = $1
                   AND applicant_subject IS NOT DISTINCT FROM $2
                   AND content_sha256 = $3
                   AND expires_at > transaction_timestamp()
                   AND status IN ('pending', 'uploaded', 'verified')",
                vec![
                    upload_id.into(),
                    subject
                        .map(VerifiedSubject::as_str)
                        .map(str::to_owned)
                        .into(),
                    observed_sha256.into(),
                ],
            ))
            .await?;
        if result.rows_affected() != 1 {
            return Err(DataError::IntakeObjectUnavailable);
        }
        Ok(())
    }

    pub async fn store_application(
        &self,
        target: PersistenceTarget,
        context: &SubmissionContext,
        input: &ApplicationCreate,
    ) -> Result<StoredSubmission, DataError> {
        input.validate()?;
        let preferred_room_occupancy =
            i16::try_from(input.preferred_room_occupancy).map_err(|_| DataError::Invariant)?;
        let transaction = self.database.begin().await?;
        require_verified_uploads(&transaction, context, input).await?;
        let stored = insert_or_replay(
            &transaction,
            context,
            "INSERT INTO hhm_applications (
                id, idempotency_key, payload_sha256, applicant_subject, email,
                linkedin_url, legal_name, date_of_birth, nationality, phone,
                current_city, github_url, portfolio_url, entrepreneurship_idea,
                project_stage, stay_preference, preferred_start_month,
                community_contribution, accessibility_or_accommodation_notes,
                allergy_notes, noise_sensitivity, light_sensitivity,
                room_preference_notes, roommate_preference, preferred_room_occupancy,
                roommate_for_lower_cost, roommate_for_social_connection,
                accommodation_data_consent, resume_upload_id, photo_id_upload_id,
                age_and_identity_attestation, privacy_notice_version, mirror_status
             ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12,
                $13, $14, $15, $16, $17, $18, $19, $20, $21, $22, $23,
                $24, $25, $26, $27, $28, $29, $30, $31, $32, $33
             ) ON CONFLICT (idempotency_key) DO NOTHING
             RETURNING id, submitted_at AS accepted_at, payload_sha256",
            vec![
                context.canonical_id.into(),
                context.idempotency_key.clone().into(),
                context.payload_sha256.clone().into(),
                context.subject().map(str::to_owned).into(),
                input.email.trim().to_owned().into(),
                input.linkedin_url.trim().to_owned().into(),
                input.legal_name.trim().to_owned().into(),
                input.date_of_birth.into(),
                input.nationality.trim().to_owned().into(),
                input.phone.trim().to_owned().into(),
                input.current_city.trim().to_owned().into(),
                input.github_url.clone().into(),
                input.portfolio_url.clone().into(),
                input.entrepreneurship_idea.trim().to_owned().into(),
                project_stage(input.project_stage).into(),
                stay_preference(input.stay_preference).into(),
                input.preferred_start_month.into(),
                input.community_contribution.trim().to_owned().into(),
                input.accessibility_or_accommodation_notes.clone().into(),
                input.allergy_notes.clone().into(),
                sensitivity_level(input.noise_sensitivity).into(),
                sensitivity_level(input.light_sensitivity).into(),
                input.room_preference_notes.clone().into(),
                roommate_preference(input.roommate_preference).into(),
                preferred_room_occupancy.into(),
                input.roommate_for_lower_cost.into(),
                input.roommate_for_social_connection.into(),
                input.accommodation_data_consent.into(),
                input.resume_upload_id.into(),
                input.photo_id_upload_id.into(),
                input.age_and_identity_attestation.into(),
                input.privacy_notice_version.clone().into(),
                target.mirror_status().into(),
            ],
            "SELECT id, submitted_at AS accepted_at, payload_sha256
             FROM hhm_applications WHERE idempotency_key = $1",
        )
        .await?;
        maybe_insert_outbox(
            &transaction,
            target,
            StoredSubmissionKind::Application,
            &stored,
            context,
        )
        .await?;
        transaction.commit().await?;
        Ok(stored)
    }

    pub async fn store_referral(
        &self,
        target: PersistenceTarget,
        context: &SubmissionContext,
        input: &ReferralCreate,
    ) -> Result<StoredSubmission, DataError> {
        input.validate()?;
        let subject = context.require_subject()?;
        let transaction = self.database.begin().await?;
        let stored = insert_or_replay(
            &transaction,
            context,
            "INSERT INTO hhm_referrals (
                id, idempotency_key, payload_sha256, referrer_subject, referee_name,
                referee_email, referee_linkedin_url, relationship, rationale,
                stay_preference, nominee_consent_confirmed, mirror_status
             ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
             ON CONFLICT (idempotency_key) DO NOTHING
             RETURNING id, created_at AS accepted_at, payload_sha256",
            vec![
                context.canonical_id.into(),
                context.idempotency_key.clone().into(),
                context.payload_sha256.clone().into(),
                subject.to_owned().into(),
                input.referee_name.trim().to_owned().into(),
                input.referee_email.trim().to_owned().into(),
                input.referee_linkedin_url.trim().to_owned().into(),
                input.relationship.trim().to_owned().into(),
                input.rationale.trim().to_owned().into(),
                input.stay_preference.map(stay_preference).into(),
                input.nominee_consent_confirmed.into(),
                target.mirror_status().into(),
            ],
            "SELECT id, created_at AS accepted_at, payload_sha256
             FROM hhm_referrals WHERE idempotency_key = $1",
        )
        .await?;
        maybe_insert_outbox(
            &transaction,
            target,
            StoredSubmissionKind::Referral,
            &stored,
            context,
        )
        .await?;
        transaction.commit().await?;
        Ok(stored)
    }

    pub async fn mark_mirrored(
        &self,
        kind: StoredSubmissionKind,
        id: Uuid,
        payload_sha256: &str,
    ) -> Result<(), DataError> {
        let transaction = self.database.begin().await?;
        if kind != StoredSubmissionKind::Upload {
            let statement = format!(
                "UPDATE {} SET mirror_status = 'mirrored' WHERE id = $1 AND payload_sha256 = $2",
                kind.table_name()
            );
            let result = transaction
                .execute_raw(Statement::from_sql_and_values(
                    DatabaseBackend::Postgres,
                    statement,
                    vec![id.into(), payload_sha256.into()],
                ))
                .await?;
            if result.rows_affected() != 1 {
                return Err(DataError::Invariant);
            }
        }
        let result = transaction
            .execute_raw(Statement::from_sql_and_values(
                DatabaseBackend::Postgres,
                "UPDATE hhm_submission_outbox
                 SET mirror_status = 'mirrored', last_error_code = NULL
                 WHERE submission_kind = $1 AND submission_id = $2 AND payload_sha256 = $3",
                vec![
                    kind.database_value().into(),
                    id.into(),
                    payload_sha256.into(),
                ],
            ))
            .await?;
        if result.rows_affected() != 1 {
            return Err(DataError::Invariant);
        }
        transaction.commit().await?;
        Ok(())
    }

    pub async fn record_mirror_failure(
        &self,
        kind: StoredSubmissionKind,
        id: Uuid,
        error_code: &str,
    ) -> Result<(), DataError> {
        if error_code.is_empty()
            || error_code.len() > 64
            || !error_code
                .bytes()
                .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'_')
        {
            return Err(DataError::InvalidContext("mirror_error_code"));
        }
        let transaction = self.database.begin().await?;
        if kind != StoredSubmissionKind::Upload {
            let statement = format!(
                "UPDATE {} SET mirror_status = 'retryable_failure' WHERE id = $1",
                kind.table_name()
            );
            let result = transaction
                .execute_raw(Statement::from_sql_and_values(
                    DatabaseBackend::Postgres,
                    statement,
                    [id.into()],
                ))
                .await?;
            if result.rows_affected() != 1 {
                return Err(DataError::Invariant);
            }
        }
        let result = transaction
            .execute_raw(Statement::from_sql_and_values(
                DatabaseBackend::Postgres,
                "UPDATE hhm_submission_outbox
                 SET attempts = attempts + 1,
                     mirror_status = CASE WHEN attempts >= 99
                       THEN 'terminal_failure' ELSE 'retryable_failure' END,
                     next_attempt_at = transaction_timestamp()
                       + make_interval(secs => LEAST(3600, (1 << LEAST(attempts, 11)))),
                     last_error_code = $3
                 WHERE submission_kind = $1 AND submission_id = $2 AND attempts < 100",
                vec![kind.database_value().into(), id.into(), error_code.into()],
            ))
            .await?;
        if result.rows_affected() != 1 {
            return Err(DataError::Invariant);
        }
        transaction.commit().await?;
        Ok(())
    }
}

async fn maybe_insert_outbox(
    transaction: &DatabaseTransaction,
    target: PersistenceTarget,
    kind: StoredSubmissionKind,
    stored: &StoredSubmission,
    context: &SubmissionContext,
) -> Result<(), DataError> {
    if !target.creates_outbox() || stored.replayed {
        return Ok(());
    }
    transaction
        .execute_raw(Statement::from_sql_and_values(
            DatabaseBackend::Postgres,
            "INSERT INTO hhm_submission_outbox (submission_kind, submission_id, payload_sha256)
             VALUES ($1, $2, $3)",
            vec![
                kind.database_value().into(),
                stored.id.into(),
                context.payload_sha256.clone().into(),
            ],
        ))
        .await?;
    Ok(())
}

async fn insert_or_replay(
    transaction: &DatabaseTransaction,
    context: &SubmissionContext,
    insert_sql: &str,
    insert_values: Vec<sea_orm::Value>,
    replay_sql: &str,
) -> Result<StoredSubmission, DataError> {
    let inserted = transaction
        .query_one_raw(Statement::from_sql_and_values(
            DatabaseBackend::Postgres,
            insert_sql,
            insert_values,
        ))
        .await?;
    if let Some(row) = inserted {
        return stored_from_row(&row, false);
    }
    let replay = transaction
        .query_one_raw(Statement::from_sql_and_values(
            DatabaseBackend::Postgres,
            replay_sql,
            [context.idempotency_key.clone().into()],
        ))
        .await?
        .ok_or(DataError::Invariant)?;
    require_same_payload(&replay, context.payload_sha256())?;
    stored_from_row(&replay, true)
}

fn stored_from_row(row: &QueryResult, replayed: bool) -> Result<StoredSubmission, DataError> {
    Ok(StoredSubmission {
        id: read_uuid(row, "id")?,
        accepted_at: read_timestamp(row, "accepted_at")?,
        replayed,
    })
}

async fn require_verified_uploads(
    transaction: &DatabaseTransaction,
    context: &SubmissionContext,
    input: &ApplicationCreate,
) -> Result<(), DataError> {
    let row = transaction
        .query_one_raw(Statement::from_sql_and_values(
            DatabaseBackend::Postgres,
            "SELECT count(*)::bigint AS matching_uploads
             FROM hhm_intake_uploads
             WHERE applicant_subject IS NOT DISTINCT FROM $1
               AND status = 'verified'
               AND expires_at > transaction_timestamp()
               AND ((id = $2 AND kind = 'resume') OR (id = $3 AND kind = 'photo_id'))",
            vec![
                context.subject().map(str::to_owned).into(),
                input.resume_upload_id.into(),
                input.photo_id_upload_id.into(),
            ],
        ))
        .await?
        .ok_or(DataError::Invariant)?;
    let matching: i64 = row
        .try_get("", "matching_uploads")
        .map_err(|_| DataError::Decode)?;
    if matching != 2 {
        return Err(DataError::IntakeObjectUnavailable);
    }
    Ok(())
}

fn require_same_payload(row: &QueryResult, expected: &str) -> Result<(), DataError> {
    if read_string(row, "payload_sha256")? == expected {
        Ok(())
    } else {
        Err(DataError::IdempotencyConflict)
    }
}

fn read_uuid(row: &QueryResult, column: &str) -> Result<Uuid, DataError> {
    row.try_get("", column).map_err(|_| DataError::Decode)
}

fn read_string(row: &QueryResult, column: &str) -> Result<String, DataError> {
    String::try_get(row, "", column).map_err(|_| DataError::Decode)
}

fn read_timestamp(row: &QueryResult, column: &str) -> Result<DateTime<Utc>, DataError> {
    row.try_get("", column).map_err(|_| DataError::Decode)
}

const fn stay_preference(value: StayPreference) -> &'static str {
    match value {
        StayPreference::ThreeMonths => "three_months",
        StayPreference::SixMonths => "six_months",
    }
}

const fn upload_kind(value: UploadKind) -> &'static str {
    match value {
        UploadKind::Resume => "resume",
        UploadKind::PhotoId => "photo_id",
    }
}

const fn project_stage(value: ProjectStage) -> &'static str {
    match value {
        ProjectStage::Idea => "idea",
        ProjectStage::Prototype => "prototype",
        ProjectStage::EarlyRevenue => "early_revenue",
        ProjectStage::Growing => "growing",
        ProjectStage::NonprofitOrOpenSource => "nonprofit_or_open_source",
    }
}

const fn sensitivity_level(value: SensitivityLevel) -> &'static str {
    match value {
        SensitivityLevel::None => "none",
        SensitivityLevel::Low => "low",
        SensitivityLevel::Moderate => "moderate",
        SensitivityLevel::High => "high",
        SensitivityLevel::PreferNotToSay => "prefer_not_to_say",
    }
}

const fn roommate_preference(value: RoommatePreference) -> &'static str {
    match value {
        RoommatePreference::PrivateRoom => "private_room",
        RoommatePreference::OpenToRoommates => "open_to_roommates",
        RoommatePreference::PreferRoommates => "prefer_roommates",
        RoommatePreference::Flexible => "flexible",
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn context_validation_is_bounded_and_redacted() {
        assert!(
            SubmissionContext::public("intake-request-0001", "a".repeat(64), "hhaus.org").is_ok()
        );
        assert!(
            SubmissionContext::public("intake-request-0001", "A".repeat(64), "attacker.example")
                .is_err()
        );
        assert!(!format!("{}", DataError::Database).contains("postgres://"));
    }

    #[test]
    fn enum_mappings_are_exhaustive_contract_values() {
        assert_eq!(upload_kind(UploadKind::PhotoId), "photo_id");
        assert_eq!(stay_preference(StayPreference::SixMonths), "six_months");
        assert_eq!(project_stage(ProjectStage::EarlyRevenue), "early_revenue");
        assert_eq!(sensitivity_level(SensitivityLevel::High), "high");
        assert_eq!(
            roommate_preference(RoommatePreference::OpenToRoommates),
            "open_to_roommates"
        );
    }
}
