use std::time::Duration;

use axum::http::{HeaderMap, header::AUTHORIZATION};
use futures_util::StreamExt;
use hhm_orm_core::VerifiedSubject;
use reqwest::{StatusCode, header::ACCEPT, redirect::Policy};
use secrecy::{ExposeSecret, SecretString};
use serde::{Deserialize, Serialize};
use url::Url;

use crate::config::Config;

const MAX_CREDENTIAL_BYTES: usize = 16 * 1024;
const MAX_RESPONSE_BYTES: usize = 1024 * 1024;
const MAX_AUDIENCE_BYTES: usize = 128;

#[derive(Clone)]
pub struct Authenticator {
    http: reqwest::Client,
    endpoint: Url,
    service_credential: SecretString,
    audience: String,
}

#[derive(Debug, thiserror::Error)]
pub enum AuthBuildError {
    #[error("shared-auth base URL is invalid")]
    InvalidBaseUrl,
    #[error("shared-auth service credential is invalid")]
    InvalidServiceCredential,
    #[error("shared-auth audience is invalid")]
    InvalidAudience,
    #[error("shared-auth HTTP transport could not be initialized")]
    Transport(#[from] reqwest::Error),
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

#[derive(Debug, Serialize)]
struct IntrospectionEnvelope<'a> {
    contract: &'static str,
    payload: IntrospectionRequest<'a>,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct IntrospectionRequest<'a> {
    token: &'a str,
    audience: &'a str,
    required_scopes: &'a [&'a str],
}

#[derive(Clone, Debug, Deserialize)]
struct Introspection {
    active: bool,
    #[serde(default)]
    sub: Option<String>,
    #[serde(default)]
    aud: Option<String>,
    #[serde(default)]
    scope: Option<String>,
}

impl Introspection {
    fn has_scope(&self, required: &str) -> bool {
        self.active
            && self.scope.as_deref().is_some_and(|scope| {
                scope
                    .split_ascii_whitespace()
                    .any(|candidate| candidate == required)
            })
    }
}

impl Authenticator {
    /// Builds a bounded, redirect-free adapter for the canonical Shared Auth
    /// `IntrospectionRequest` protocol.
    ///
    /// The adapter deliberately owns only transport and response validation;
    /// token authority and claim semantics remain in Shared Auth.
    ///
    /// # Errors
    ///
    /// Returns [`AuthBuildError`] when the configured endpoint, service
    /// credential, audience, or HTTP client policy is invalid.
    pub fn from_config(config: &Config) -> Result<Self, AuthBuildError> {
        let credential = config.shared_auth_service_credential.expose_secret();
        if credential.is_empty()
            || credential.len() > MAX_CREDENTIAL_BYTES
            || credential.contains(char::is_whitespace)
        {
            return Err(AuthBuildError::InvalidServiceCredential);
        }
        if config.shared_auth_audience.is_empty()
            || config.shared_auth_audience.len() > MAX_AUDIENCE_BYTES
            || !config.shared_auth_audience.bytes().all(|byte| {
                byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b':' | b'.')
            })
        {
            return Err(AuthBuildError::InvalidAudience);
        }

        let endpoint = introspection_endpoint(&config.shared_auth_base_url)?;
        let http = reqwest::Client::builder()
            .connect_timeout(Duration::from_secs(2))
            .timeout(Duration::from_secs(5))
            .redirect(Policy::none())
            .user_agent("hhm-api-shared-auth-adapter/0.1")
            .build()?;

        Ok(Self {
            http,
            endpoint,
            service_credential: config.shared_auth_service_credential.clone(),
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
        let response = self
            .http
            .post(self.endpoint.clone())
            .header(ACCEPT, "application/json")
            .bearer_auth(self.service_credential.expose_secret())
            .json(&IntrospectionEnvelope {
                contract: "IntrospectionRequest",
                payload: IntrospectionRequest {
                    token,
                    audience: &self.audience,
                    required_scopes: &[],
                },
            })
            .send()
            .await
            .map_err(|_| AuthError::Unavailable)?;

        let status = response.status();
        if status.is_server_error() {
            return Err(AuthError::Unavailable);
        }
        if status == StatusCode::UNAUTHORIZED || !status.is_success() {
            return Err(AuthError::Invalid);
        }
        if response
            .content_length()
            .is_some_and(|length| length > MAX_RESPONSE_BYTES as u64)
        {
            return Err(AuthError::Invalid);
        }

        let bytes = bounded_response(response).await?;
        let introspection: Introspection =
            serde_json::from_slice(&bytes).map_err(|_| AuthError::Invalid)?;
        if !eligible_for_intake(&introspection, &self.audience) {
            return Err(AuthError::Invalid);
        }
        VerifiedSubject::from_verified_claim(introspection.sub.ok_or(AuthError::Invalid)?)
            .map_err(|_| AuthError::Invalid)
    }
}

async fn bounded_response(response: reqwest::Response) -> Result<Vec<u8>, AuthError> {
    let mut body = Vec::new();
    let mut stream = response.bytes_stream();
    while let Some(chunk) = stream.next().await {
        let chunk = chunk.map_err(|_| AuthError::Unavailable)?;
        let next_len = body
            .len()
            .checked_add(chunk.len())
            .ok_or(AuthError::Invalid)?;
        if next_len > MAX_RESPONSE_BYTES {
            return Err(AuthError::Invalid);
        }
        body.extend_from_slice(&chunk);
    }
    Ok(body)
}

fn introspection_endpoint(raw: &str) -> Result<Url, AuthBuildError> {
    let mut url = Url::parse(raw).map_err(|_| AuthBuildError::InvalidBaseUrl)?;
    if url.username() != ""
        || url.password().is_some()
        || url.query().is_some()
        || url.fragment().is_some()
    {
        return Err(AuthBuildError::InvalidBaseUrl);
    }
    let mut segments = url
        .path_segments_mut()
        .map_err(|()| AuthBuildError::InvalidBaseUrl)?;
    segments.pop_if_empty();
    segments.extend(["auth", "introspect"]);
    drop(segments);
    Ok(url)
}

fn eligible_for_intake(introspection: &Introspection, audience: &str) -> bool {
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
        || token.len() > MAX_CREDENTIAL_BYTES
        || token.contains(char::is_whitespace)
    {
        return Err(AuthError::Invalid);
    }
    Ok(Some(token))
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
        let valid = Introspection {
            active: true,
            sub: Some("user-1".into()),
            aud: Some("hhm-api".into()),
            scope: Some("hhm:intake:write".into()),
        };
        assert!(eligible_for_intake(&valid, "hhm-api"));

        let mut wrong_audience = valid.clone();
        wrong_audience.aud = Some("admin-api".into());
        assert!(!eligible_for_intake(&wrong_audience, "hhm-api"));

        let mut missing_scope = valid;
        missing_scope.scope = Some("hhm:intake:read".into());
        assert!(!eligible_for_intake(&missing_scope, "hhm-api"));
    }

    #[test]
    fn mounted_prefix_is_preserved_when_building_introspection_endpoint() {
        let endpoint = introspection_endpoint("https://gateway.example/shared-auth/").unwrap();
        assert_eq!(
            endpoint.as_str(),
            "https://gateway.example/shared-auth/auth/introspect"
        );
    }
}
