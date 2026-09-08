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
    /// Loads fail-closed runtime configuration from the process environment.
    ///
    /// # Errors
    ///
    /// Returns [`ConfigError`] when a required setting is absent or unsafe.
    pub fn from_env() -> Result<Self, ConfigError> {
        Self::from_resolver(|name| env::var(name).ok())
    }

    /// Loads fail-closed runtime configuration from an audited resolver.
    ///
    /// This is the command-line integration boundary: `flags-2-env` may supply
    /// typed, precedence-resolved values while secrets continue to arrive only
    /// from the environment/decrypted runtime. No configuration value is logged.
    ///
    /// # Errors
    ///
    /// Returns [`ConfigError`] when a required setting is absent or unsafe.
    pub fn from_resolver<F>(resolver: F) -> Result<Self, ConfigError>
    where
        F: FnMut(&str) -> Option<String>,
    {
        Self::from_lookup(resolver)
    }

    fn from_lookup<F>(mut lookup: F) -> Result<Self, ConfigError>
    where
        F: FnMut(&str) -> Option<String>,
    {
        let host = optional(&mut lookup, "HOST").unwrap_or_else(|| "0.0.0.0".into());
        let port = optional(&mut lookup, "PORT").unwrap_or_else(|| "8080".into());
        let bind_address = format!("{host}:{port}")
            .parse()
            .map_err(|_| ConfigError::Invalid("HOST or PORT"))?;
        let supabase_url = parse_https_url(&mut lookup, "SUPABASE_URL")?;
        let shared_auth_base_url = required(&mut lookup, "SHARED_AUTH_BASE_URL")?;
        validate_auth_url(&shared_auth_base_url)?;
        let turnstile_action =
            optional(&mut lookup, "TURNSTILE_ACTION").unwrap_or_else(|| "intake".into());
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
            primary_database_url: SecretString::from(required(&mut lookup, "DATABASE_URL")?),
            supabase_database_url: SecretString::from(required(
                &mut lookup,
                "SUPABASE_DATABASE_URL",
            )?),
            supabase_url,
            supabase_service_role_key: SecretString::from(required(
                &mut lookup,
                "SUPABASE_SERVICE_ROLE_KEY",
            )?),
            turnstile_secret_key: SecretString::from(required(
                &mut lookup,
                "TURNSTILE_SECRET_KEY",
            )?),
            turnstile_action,
            shared_auth_base_url,
            shared_auth_service_credential: SecretString::from(required(
                &mut lookup,
                "SHARED_AUTH_SERVICE_CREDENTIAL",
            )?),
            shared_auth_audience: required(&mut lookup, "SHARED_AUTH_AUDIENCE")?,
            cors_origins: required(&mut lookup, "CORS_ORIGINS")?,
        })
    }
}

fn required<F>(lookup: &mut F, name: &'static str) -> Result<String, ConfigError>
where
    F: FnMut(&str) -> Option<String>,
{
    optional(lookup, name).ok_or(ConfigError::Missing(name))
}

fn optional<F>(lookup: &mut F, name: &str) -> Option<String>
where
    F: FnMut(&str) -> Option<String>,
{
    lookup(name)
        .map(|value| value.trim().to_owned())
        .filter(|value| !value.is_empty())
}

fn parse_https_url<F>(lookup: &mut F, name: &'static str) -> Result<Url, ConfigError>
where
    F: FnMut(&str) -> Option<String>,
{
    let url = Url::parse(&required(lookup, name)?).map_err(|_| ConfigError::Invalid(name))?;
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
    use std::collections::BTreeMap;

    #[test]
    fn shared_auth_allows_https_and_in_cluster_http_only() {
        assert!(validate_auth_url("https://auth.hhaus.org").is_ok());
        assert!(validate_auth_url("http://shared-auth.auth.svc").is_ok());
        assert!(validate_auth_url("http://auth.hhaus.org").is_err());
    }

    #[test]
    fn audited_resolver_drives_non_secret_listener_values() {
        let values = BTreeMap::from([
            ("HOST".to_owned(), "127.0.0.1".to_owned()),
            ("PORT".to_owned(), "31337".to_owned()),
            ("DATABASE_URL".to_owned(), "postgres://primary".to_owned()),
            (
                "SUPABASE_DATABASE_URL".to_owned(),
                "postgres://supabase".to_owned(),
            ),
            (
                "SUPABASE_URL".to_owned(),
                "https://example.supabase.co".to_owned(),
            ),
            ("SUPABASE_SERVICE_ROLE_KEY".to_owned(), "secret".to_owned()),
            ("TURNSTILE_SECRET_KEY".to_owned(), "secret".to_owned()),
            (
                "SHARED_AUTH_BASE_URL".to_owned(),
                "https://auth.hhaus.org".to_owned(),
            ),
            (
                "SHARED_AUTH_SERVICE_CREDENTIAL".to_owned(),
                "secret".to_owned(),
            ),
            ("SHARED_AUTH_AUDIENCE".to_owned(), "hhm-api".to_owned()),
            ("CORS_ORIGINS".to_owned(), "https://hhaus.org".to_owned()),
        ]);
        let config = Config::from_resolver(|name| values.get(name).cloned()).expect("config");
        assert_eq!(config.bind_address.to_string(), "127.0.0.1:31337");
    }
}
