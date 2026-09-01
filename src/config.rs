use std::{env, net::SocketAddr};

use secrecy::SecretString;
use url::Url;

#[derive(Clone)]
pub struct Config {
    pub bind_address: SocketAddr,
    pub primary_database_url: SecretString,
    pub supabase_database_url: SecretString,
    pub supabase_url: Url,
    pub supabase_service_role_key: SecretString,
    pub turnstile_secret_key: SecretString,
    pub turnstile_action: String,
    pub shared_auth_base_url: String,
    pub shared_auth_service_credential: SecretString,
    pub shared_auth_audience: String,
    pub cors_origins: String,
}

#[derive(Debug, thiserror::Error)]
pub enum ConfigError {
    #[error("required configuration is missing: {0}")]
    Missing(&'static str),
    #[error("configuration is invalid: {0}")]
    Invalid(&'static str),
}

impl Config {
    /// Loads fail-closed runtime configuration without applying schema changes.
    ///
    /// # Errors
    ///
    /// Returns [`ConfigError`] when a required setting is absent or unsafe.
    pub fn from_env() -> Result<Self, ConfigError> {
        let host = optional("HOST").unwrap_or_else(|| "0.0.0.0".into());
        let port = optional("PORT").unwrap_or_else(|| "8080".into());
        let bind_address = format!("{host}:{port}")
            .parse()
            .map_err(|_| ConfigError::Invalid("HOST or PORT"))?;
        let supabase_url = parse_https_url("SUPABASE_URL")?;
        let shared_auth_base_url = required("SHARED_AUTH_BASE_URL")?;
        validate_auth_url(&shared_auth_base_url)?;
        let turnstile_action = optional("TURNSTILE_ACTION").unwrap_or_else(|| "intake".into());
        if turnstile_action.len() > 64
            || turnstile_action.is_empty()
            || !turnstile_action
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_'))
        {
            return Err(ConfigError::Invalid("TURNSTILE_ACTION"));
        }

        Ok(Self {
            bind_address,
            primary_database_url: SecretString::from(required("DATABASE_URL")?),
            supabase_database_url: SecretString::from(required("SUPABASE_DATABASE_URL")?),
            supabase_url,
            supabase_service_role_key: SecretString::from(required("SUPABASE_SERVICE_ROLE_KEY")?),
            turnstile_secret_key: SecretString::from(required("TURNSTILE_SECRET_KEY")?),
            turnstile_action,
            shared_auth_base_url,
            shared_auth_service_credential: SecretString::from(required(
                "SHARED_AUTH_SERVICE_CREDENTIAL",
            )?),
            shared_auth_audience: required("SHARED_AUTH_AUDIENCE")?,
            cors_origins: required("CORS_ORIGINS")?,
        })
    }
}

fn required(name: &'static str) -> Result<String, ConfigError> {
    optional(name).ok_or(ConfigError::Missing(name))
}

fn optional(name: &str) -> Option<String> {
    env::var(name)
        .ok()
        .map(|value| value.trim().to_owned())
        .filter(|value| !value.is_empty())
}

fn parse_https_url(name: &'static str) -> Result<Url, ConfigError> {
    let url = Url::parse(&required(name)?).map_err(|_| ConfigError::Invalid(name))?;
    if url.scheme() != "https" || url.host_str().is_none() || url.query().is_some() {
        return Err(ConfigError::Invalid(name));
    }
    Ok(url)
}

fn validate_auth_url(value: &str) -> Result<(), ConfigError> {
    let url = Url::parse(value).map_err(|_| ConfigError::Invalid("SHARED_AUTH_BASE_URL"))?;
    let host = url.host_str().unwrap_or_default();
    let labels = host.split('.').collect::<Vec<_>>();
    let in_cluster = labels
        .last()
        .is_some_and(|label| label.eq_ignore_ascii_case("svc"))
        || labels
            .get(labels.len().saturating_sub(3)..)
            .is_some_and(|suffix| {
                suffix.len() == 3
                    && suffix[0].eq_ignore_ascii_case("svc")
                    && suffix[1].eq_ignore_ascii_case("cluster")
                    && suffix[2].eq_ignore_ascii_case("local")
            });
    let local = host == "localhost" || host == "127.0.0.1" || in_cluster;
    if url.host_str().is_none()
        || url.query().is_some()
        || !(url.scheme() == "https" || (url.scheme() == "http" && local))
    {
        return Err(ConfigError::Invalid("SHARED_AUTH_BASE_URL"));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn shared_auth_allows_https_and_in_cluster_http_only() {
        assert!(validate_auth_url("https://auth.hhaus.org").is_ok());
        assert!(validate_auth_url("http://shared-auth.auth.svc").is_ok());
        assert!(validate_auth_url("http://auth.hhaus.org").is_err());
    }
}
