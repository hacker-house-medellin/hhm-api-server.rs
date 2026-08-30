use futures_util::StreamExt;
use hhm_orm_core::PendingUpload;
use secrecy::{ExposeSecret, SecretString};
use serde::Deserialize;
use sha2::{Digest, Sha256};
use url::Url;

use crate::config::Config;

const INTAKE_BUCKET: &str = "hhm-intake-private";
const MAX_UPLOAD_BYTES: u64 = 10 * 1024 * 1024;

#[derive(Clone)]
pub struct TurnstileVerifier {
    http: reqwest::Client,
    secret: SecretString,
    expected_action: String,
}

#[derive(Clone)]
pub struct SupabaseStorage {
    http: reqwest::Client,
    project_url: Url,
    service_role_key: SecretString,
}

#[derive(Debug, thiserror::Error)]
pub enum ExternalError {
    #[error("external verification rejected the request")]
    Rejected,
    #[error("external verification is unavailable")]
    Unavailable,
    #[error("stored object does not match its intake declaration")]
    ObjectMismatch,
    #[error("external service returned an invalid response")]
    InvalidResponse,
}

#[derive(Deserialize)]
struct TurnstileResponse {
    success: bool,
    #[serde(default)]
    hostname: String,
    #[serde(default)]
    action: String,
}

#[derive(Deserialize)]
struct SignedUploadResponse {
    url: String,
}

impl TurnstileVerifier {
    #[must_use]
    pub fn from_config(config: &Config, http: reqwest::Client) -> Self {
        Self {
            http,
            secret: config.turnstile_secret_key.clone(),
            expected_action: config.turnstile_action.clone(),
        }
    }

    /// Verifies a single-use Cloudflare Turnstile proof server-side.
    ///
    /// # Errors
    ///
    /// Rejects invalid action/hostname bindings and treats transport ambiguity
    /// as unavailable, never as a successful proof.
    pub async fn verify(&self, token: &str) -> Result<(), ExternalError> {
        if token.is_empty() || token.len() > 4_096 || token.contains(char::is_control) {
            return Err(ExternalError::Rejected);
        }
        let response = self
            .http
            .post("https://challenges.cloudflare.com/turnstile/v0/siteverify")
            .form(&[("secret", self.secret.expose_secret()), ("response", token)])
            .send()
            .await
            .map_err(|_| ExternalError::Unavailable)?;
        if !response.status().is_success() {
            return Err(ExternalError::Unavailable);
        }
        let result: TurnstileResponse = response
            .json()
            .await
            .map_err(|_| ExternalError::InvalidResponse)?;
        if !result.success
            || result.action != self.expected_action
            || !matches!(
                result.hostname.as_str(),
                "hhaus.org" | "www.hhaus.org" | "user.hhaus.org" | "medellin.hhaus.org"
            )
        {
            return Err(ExternalError::Rejected);
        }
        Ok(())
    }
}

impl SupabaseStorage {
    #[must_use]
    pub fn from_config(config: &Config, http: reqwest::Client) -> Self {
        Self {
            http,
            project_url: config.supabase_url.clone(),
            service_role_key: config.supabase_service_role_key.clone(),
        }
    }

    /// Creates a signed direct-to-Supabase upload URL for a private object.
    ///
    /// # Errors
    ///
    /// Returns [`ExternalError`] for transport, status, or origin-confusion
    /// failures.
    pub async fn create_signed_upload_url(&self, object_key: &str) -> Result<Url, ExternalError> {
        validate_object_key(object_key)?;
        let storage_base = self
            .project_url
            .join("storage/v1/")
            .map_err(|_| ExternalError::InvalidResponse)?;
        let endpoint = storage_base
            .join(&format!("object/upload/sign/{INTAKE_BUCKET}/{object_key}"))
            .map_err(|_| ExternalError::InvalidResponse)?;
        let response = self
            .http
            .post(endpoint)
            .bearer_auth(self.service_role_key.expose_secret())
            .header("apikey", self.service_role_key.expose_secret())
            .json(&serde_json::json!({}))
            .send()
            .await
            .map_err(|_| ExternalError::Unavailable)?;
        if !response.status().is_success() {
            return Err(ExternalError::Unavailable);
        }
        let signed: SignedUploadResponse = response
            .json()
            .await
            .map_err(|_| ExternalError::InvalidResponse)?;
        let signed_url = if signed.url.starts_with("https://") {
            Url::parse(&signed.url).map_err(|_| ExternalError::InvalidResponse)?
        } else {
            let suffix = signed.url.strip_prefix('/').unwrap_or(&signed.url);
            let suffix = suffix.strip_prefix("object/").unwrap_or(suffix);
            storage_base
                .join(&format!("object/{suffix}"))
                .map_err(|_| ExternalError::InvalidResponse)?
        };
        if signed_url.scheme() != "https"
            || signed_url.host_str() != self.project_url.host_str()
            || !signed_url
                .path()
                .starts_with("/storage/v1/object/upload/sign/")
            || signed_url.query_pairs().all(|(key, _)| key != "token")
        {
            return Err(ExternalError::InvalidResponse);
        }
        Ok(signed_url)
    }

    /// Streams a private object through a bounded verifier and checks its exact
    /// MIME type, byte length, and SHA-256 digest without retaining the bytes.
    ///
    /// # Errors
    ///
    /// Returns [`ExternalError::ObjectMismatch`] for any metadata or digest
    /// mismatch.
    pub async fn verify_object(&self, expected: &PendingUpload) -> Result<(), ExternalError> {
        validate_object_key(&expected.object_key)?;
        if expected.size_bytes > MAX_UPLOAD_BYTES {
            return Err(ExternalError::ObjectMismatch);
        }
        let storage_base = self
            .project_url
            .join("storage/v1/")
            .map_err(|_| ExternalError::InvalidResponse)?;
        let endpoint = storage_base
            .join(&format!(
                "object/authenticated/{INTAKE_BUCKET}/{}",
                expected.object_key
            ))
            .map_err(|_| ExternalError::InvalidResponse)?;
        let response = self
            .http
            .get(endpoint)
            .bearer_auth(self.service_role_key.expose_secret())
            .header("apikey", self.service_role_key.expose_secret())
            .send()
            .await
            .map_err(|_| ExternalError::Unavailable)?;
        if !response.status().is_success() {
            return Err(ExternalError::ObjectMismatch);
        }
        let content_type = response
            .headers()
            .get(reqwest::header::CONTENT_TYPE)
            .and_then(|value| value.to_str().ok())
            .and_then(|value| value.split(';').next())
            .map(str::trim)
            .ok_or(ExternalError::ObjectMismatch)?;
        if content_type != expected.content_type {
            return Err(ExternalError::ObjectMismatch);
        }
        if response
            .content_length()
            .is_some_and(|length| length != expected.size_bytes)
        {
            return Err(ExternalError::ObjectMismatch);
        }

        let mut stream = response.bytes_stream();
        let mut observed_size = 0_u64;
        let mut digest = Sha256::new();
        while let Some(chunk) = stream.next().await {
            let chunk = chunk.map_err(|_| ExternalError::Unavailable)?;
            observed_size = observed_size
                .checked_add(u64::try_from(chunk.len()).map_err(|_| ExternalError::ObjectMismatch)?)
                .ok_or(ExternalError::ObjectMismatch)?;
            if observed_size > expected.size_bytes || observed_size > MAX_UPLOAD_BYTES {
                return Err(ExternalError::ObjectMismatch);
            }
            digest.update(&chunk);
        }
        let observed_digest = format!("{:x}", digest.finalize());
        if observed_size != expected.size_bytes || observed_digest != expected.expected_sha256 {
            return Err(ExternalError::ObjectMismatch);
        }
        Ok(())
    }
}

fn validate_object_key(value: &str) -> Result<(), ExternalError> {
    if value.is_empty()
        || value.len() > 1_024
        || value.starts_with('/')
        || value.split('/').any(|segment| {
            segment.is_empty()
                || segment == "."
                || segment == ".."
                || !segment
                    .bytes()
                    .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_'))
        })
    {
        return Err(ExternalError::InvalidResponse);
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn object_keys_are_path_bounded() {
        assert!(validate_object_key("intake/photo_id/id/digest").is_ok());
        assert!(validate_object_key("../identity.jpg").is_err());
        assert!(validate_object_key("intake//identity").is_err());
    }
}
