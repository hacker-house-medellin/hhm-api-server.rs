use axum::http::{HeaderMap, header::AUTHORIZATION};
use hhm_orm_core::VerifiedSubject;
use secrecy::ExposeSecret;
use shared_auth_service_client::{ClientError, SharedAuthClient};

use crate::config::Config;

#[derive(Clone)]
pub struct Authenticator {
    client: SharedAuthClient,
    audience: String,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, thiserror::Error)]
pub enum AuthError {
    #[error("authentication is required")]
    Missing,
    #[error("authentication is invalid")]
    Invalid,
    #[error("authentication authority is unavailable")]
    Unavailable,
}

impl Authenticator {
    /// Builds the official protected-introspection client.
    ///
    /// # Errors
    ///
    /// Returns [`ClientError`] if the configured Shared Auth URL is invalid.
    pub fn from_config(config: &Config) -> Result<Self, ClientError> {
        let client = SharedAuthClient::try_new(config.shared_auth_base_url.clone())?
            .with_service_credential(
                config
                    .shared_auth_service_credential
                    .expose_secret()
                    .to_owned(),
            );
        Ok(Self {
            client,
            audience: config.shared_auth_audience.clone(),
        })
    }

    /// Returns an authentication-derived subject when a bearer is present.
    ///
    /// # Errors
    ///
    /// Invalid or undecidable supplied credentials fail closed instead of being
    /// silently downgraded to an anonymous request.
    pub async fn optional_subject(
        &self,
        headers: &HeaderMap,
    ) -> Result<Option<VerifiedSubject>, AuthError> {
        let Some(token) = bearer_token(headers)? else {
            return Ok(None);
        };
        self.verify(token).await.map(Some)
    }

    /// Requires an active Shared Auth user subject.
    ///
    /// # Errors
    ///
    /// Returns [`AuthError`] when a bearer is missing, invalid, or undecidable.
    pub async fn required_subject(
        &self,
        headers: &HeaderMap,
    ) -> Result<VerifiedSubject, AuthError> {
        let token = bearer_token(headers)?.ok_or(AuthError::Missing)?;
        self.verify(token).await
    }

    async fn verify(&self, token: &str) -> Result<VerifiedSubject, AuthError> {
        let introspection = self
            .client
            .introspect_for_audience(token, &self.audience)
            .await
            .map_err(|error| classify_client_error(&error))?;
        if !eligible_for_intake(&introspection, &self.audience) {
            return Err(AuthError::Invalid);
        }
        VerifiedSubject::from_verified_claim(introspection.sub.ok_or(AuthError::Invalid)?)
            .map_err(|_| AuthError::Invalid)
    }
}

fn eligible_for_intake(
    introspection: &shared_auth_service_client::Introspection,
    audience: &str,
) -> bool {
    introspection.active
        && introspection.aud.as_deref() == Some(audience)
        && introspection.has_scope("hhm:intake:write")
}

fn bearer_token(headers: &HeaderMap) -> Result<Option<&str>, AuthError> {
    let Some(raw) = headers.get(AUTHORIZATION) else {
        return Ok(None);
    };
    let raw = raw.to_str().map_err(|_| AuthError::Invalid)?;
    let (scheme, token) = raw.split_once(' ').ok_or(AuthError::Invalid)?;
    if !scheme.eq_ignore_ascii_case("bearer")
        || token.is_empty()
        || token.len() > 16 * 1024
        || token.contains(char::is_whitespace)
    {
        return Err(AuthError::Invalid);
    }
    Ok(Some(token))
}

fn classify_client_error(error: &ClientError) -> AuthError {
    match error {
        ClientError::Transport(_)
        | ClientError::Status(500..=599)
        | ClientError::MissingServiceCredential
        | ClientError::InvalidBaseUrl
        | ClientError::InsecureTransport(_) => AuthError::Unavailable,
        ClientError::Unauthorized
        | ClientError::InvalidInput(_)
        | ClientError::RequestTooLarge { .. }
        | ClientError::ResponseTooLarge { .. }
        | ClientError::Encode { .. }
        | ClientError::Decode { .. }
        | ClientError::Status(_) => AuthError::Invalid,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bearer_parser_is_bounded_and_single_token() {
        let mut headers = HeaderMap::new();
        headers.insert(AUTHORIZATION, "bEaReR opaque-token".parse().unwrap());
        assert_eq!(bearer_token(&headers).unwrap(), Some("opaque-token"));
        headers.insert(AUTHORIZATION, "Bearer two tokens".parse().unwrap());
        assert_eq!(bearer_token(&headers), Err(AuthError::Invalid));
    }

    #[test]
    fn delegated_token_requires_exact_audience_and_write_scope() {
        let valid: shared_auth_service_client::Introspection =
            serde_json::from_value(serde_json::json!({
                "active": true,
                "sub": "user-1",
                "aud": "hhm-api",
                "scope": "hhm:intake:write"
            }))
            .unwrap();
        assert!(eligible_for_intake(&valid, "hhm-api"));

        let mut wrong_audience = valid.clone();
        wrong_audience.aud = Some("admin-api".into());
        assert!(!eligible_for_intake(&wrong_audience, "hhm-api"));

        let mut missing_scope = valid;
        missing_scope.scope = Some("hhm:intake:read".into());
        assert!(!eligible_for_intake(&missing_scope, "hhm-api"));
    }
}
