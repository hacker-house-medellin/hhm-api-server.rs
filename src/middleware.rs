//! `HHaus` request-lifecycle policy built on the public ORES middleware contract.

use std::{future::Future, sync::Arc, time::Duration};

use axum::Router;
use ores_middleware::{
    MiddlewareStack, OperationDescriptor, OperationOutcome, OperationScope, OperationTransport,
    RateLimitFailureMode, RateLimitKeyDerivationMode, RateLimitSignal, RequestContext,
    RuntimeEnvironment, default_config, run_operation_boundary_with_timeout,
};
use secrecy::{ExposeSecret, SecretString};

use crate::config::Config;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum TlsMode {
    Disabled,
    InProcess,
    TrustedProxy,
}

impl TlsMode {
    fn as_contract_value(self) -> &'static str {
        match self {
            Self::Disabled => "disabled",
            Self::InProcess => "in-process",
            Self::TrustedProxy => "trusted-proxy",
        }
    }
}

#[derive(Clone)]
pub struct RequestPolicy {
    pub environment: RuntimeEnvironment,
    pub tls_mode: TlsMode,
    pub trusted_proxy_cidrs: Vec<String>,
    pub timeout_ms: u64,
    pub max_body_bytes: usize,
    pub rate_limit_capacity: u32,
    pub rate_limit_refill_per_second: f64,
    pub rate_limit_hmac_secret: Option<SecretString>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, thiserror::Error)]
pub enum RequestPolicyError {
    #[error("runtime environment is invalid")]
    InvalidEnvironment,
    #[error("TLS mode is invalid")]
    InvalidTlsMode,
    #[error("TLS may be disabled only outside production")]
    ProductionTlsDisabled,
    #[error("trusted-proxy TLS requires an explicit peer allowlist")]
    MissingTrustedProxy,
    #[error("request timeout is invalid")]
    InvalidTimeout,
    #[error("request body limit is invalid")]
    InvalidBodyLimit,
    #[error("rate-limit capacity is invalid")]
    InvalidRateLimitCapacity,
    #[error("rate-limit refill is invalid")]
    InvalidRateLimitRefill,
    #[error("production rate limiting requires a secret-store HMAC key")]
    MissingRateLimitSecret,
}

impl RequestPolicy {
    pub(crate) fn from_lookup<F>(lookup: &mut F) -> Result<Self, RequestPolicyError>
    where
        F: FnMut(&str) -> Option<String>,
    {
        let environment = match optional(lookup, "APP_ENV")
            .unwrap_or_else(|| "development".into())
            .to_ascii_lowercase()
            .as_str()
        {
            "development" | "dev" | "local" => RuntimeEnvironment::Development,
            "test" | "testing" => RuntimeEnvironment::Test,
            "staging" | "stage" => RuntimeEnvironment::Staging,
            "production" | "prod" => RuntimeEnvironment::Production,
            _ => return Err(RequestPolicyError::InvalidEnvironment),
        };
        let tls_mode = match optional(lookup, "ORES_MIDDLEWARE_TLS_MODE")
            .unwrap_or_else(|| "disabled".into())
            .to_ascii_lowercase()
            .as_str()
        {
            "disabled" => TlsMode::Disabled,
            "in-process" | "in_process" => TlsMode::InProcess,
            "trusted-proxy" | "trusted_proxy" => TlsMode::TrustedProxy,
            _ => return Err(RequestPolicyError::InvalidTlsMode),
        };
        if matches!(environment, RuntimeEnvironment::Production)
            && matches!(tls_mode, TlsMode::Disabled)
        {
            return Err(RequestPolicyError::ProductionTlsDisabled);
        }
        let trusted_proxy_cidrs = optional(lookup, "ORES_MIDDLEWARE_TRUSTED_PROXY_CIDRS")
            .map(|value| split_csv(&value))
            .unwrap_or_default();
        if matches!(tls_mode, TlsMode::TrustedProxy) && trusted_proxy_cidrs.is_empty() {
            return Err(RequestPolicyError::MissingTrustedProxy);
        }

        let timeout_ms = parse_number(
            optional(lookup, "ORES_MIDDLEWARE_TIMEOUT_MS").as_deref(),
            10_000_u64,
        )
        .filter(|value| *value > 0)
        .ok_or(RequestPolicyError::InvalidTimeout)?;
        let max_body_bytes = parse_number(
            optional(lookup, "ORES_MIDDLEWARE_MAX_BODY_BYTES").as_deref(),
            256 * 1024_usize,
        )
        .filter(|value| *value > 0 && *value <= 2 * 1024 * 1024)
        .ok_or(RequestPolicyError::InvalidBodyLimit)?;
        let rate_limit_capacity = parse_number(
            optional(lookup, "ORES_MIDDLEWARE_RATE_LIMIT_CAPACITY").as_deref(),
            60_u32,
        )
        .filter(|value| *value > 0)
        .ok_or(RequestPolicyError::InvalidRateLimitCapacity)?;
        let rate_limit_refill_per_second = parse_number(
            optional(lookup, "ORES_MIDDLEWARE_RATE_LIMIT_REFILL_PER_SECOND").as_deref(),
            1.0_f64,
        )
        .filter(|value| value.is_finite() && *value > 0.0)
        .ok_or(RequestPolicyError::InvalidRateLimitRefill)?;
        let rate_limit_hmac_secret =
            optional(lookup, "ORES_MIDDLEWARE_RATE_LIMIT_HMAC_SECRET").map(SecretString::from);
        if matches!(environment, RuntimeEnvironment::Production) && rate_limit_hmac_secret.is_none()
        {
            return Err(RequestPolicyError::MissingRateLimitSecret);
        }

        Ok(Self {
            environment,
            tls_mode,
            trusted_proxy_cidrs,
            timeout_ms,
            max_body_bytes,
            rate_limit_capacity,
            rate_limit_refill_per_second,
            rate_limit_hmac_secret,
        })
    }
}

fn optional<F>(lookup: &mut F, name: &str) -> Option<String>
where
    F: FnMut(&str) -> Option<String>,
{
    lookup(name)
        .map(|value| value.trim().to_owned())
        .filter(|value| !value.is_empty())
}

fn parse_number<T>(value: Option<&str>, default: T) -> Option<T>
where
    T: std::str::FromStr,
{
    match value {
        Some(value) => value.parse().ok(),
        None => Some(default),
    }
}

fn split_csv(value: &str) -> Vec<String> {
    value
        .split(',')
        .map(str::trim)
        .filter(|item| !item.is_empty())
        .map(str::to_owned)
        .collect()
}

/// Builds the reviewed request lifecycle from flags-resolved configuration.
///
/// Authentication remains route-aware because public intake accepts Turnstile
/// while referrals require Shared Auth. The middleware still supplies bounded
/// payloads, trusted-peer handling, correlation, deadlines, rate limiting,
/// security headers, and the `ores-otel`-compatible telemetry port.
///
/// # Errors
///
/// Returns an error when the resolved middleware policy violates the upstream
/// lifecycle contract.
pub fn stack(config: &Config) -> anyhow::Result<MiddlewareStack> {
    stack_from_policy(&config.request_policy)
}

/// Builds a middleware stack from an already validated request policy.
///
/// # Errors
///
/// Returns an error when upstream contract validation or HMAC-key installation
/// fails.
pub fn stack_from_policy(policy: &RequestPolicy) -> anyhow::Result<MiddlewareStack> {
    let mut config = default_config("hhm-api");
    config.environment = policy.environment.clone();
    config.settings.timeout_ms = policy.timeout_ms;
    config.settings.max_body_bytes = policy.max_body_bytes;
    config.settings.tls.mode = policy.tls_mode.as_contract_value().into();
    config.settings.tls.require_https = !matches!(policy.tls_mode, TlsMode::Disabled);
    config.settings.tls.strict_forwarded_headers = true;
    config
        .settings
        .tls
        .trusted_proxy_cidrs
        .clone_from(&policy.trusted_proxy_cidrs);
    config.settings.rate_limit.capacity = policy.rate_limit_capacity;
    config.settings.rate_limit.refill_per_second = policy.rate_limit_refill_per_second;
    config.settings.rate_limit.key_by = vec![
        RateLimitSignal::Ip,
        RateLimitSignal::Route,
        RateLimitSignal::Method,
    ];
    config.settings.rate_limit.failure_mode =
        if matches!(policy.environment, RuntimeEnvironment::Production) {
            RateLimitFailureMode::FailClosed
        } else {
            RateLimitFailureMode::LocalOnly
        };
    config.settings.rate_limit.key_derivation =
        if matches!(policy.environment, RuntimeEnvironment::Production) {
            RateLimitKeyDerivationMode::ExternalHmacSha256
        } else {
            RateLimitKeyDerivationMode::EphemeralHmacSha256
        };
    config.integrations.ores_otel.enabled = true;
    config.integrations.ores_otel.service_name = "hhm-api".into();
    config.integrations.ores_otel.propagators = vec!["tracecontext".into(), "baggage".into()];

    let middleware = MiddlewareStack::new(config).map_err(|issues| {
        let codes = issues
            .iter()
            .map(|issue| format!("{}:{}", issue.path, issue.code))
            .collect::<Vec<_>>()
            .join(", ");
        anyhow::anyhow!("request middleware policy is invalid: {codes}")
    })?;
    match &policy.rate_limit_hmac_secret {
        Some(secret) => middleware
            .with_rate_limit_hmac_key(secret.expose_secret().as_bytes())
            .map_err(|_| anyhow::anyhow!("request rate-limit key is invalid")),
        None => Ok(middleware),
    }
}

pub fn install(router: Router, stack: MiddlewareStack) -> Router {
    ores_middleware::frameworks::axum::install(router, Arc::new(stack))
}

/// Applies the same panic/deadline isolation contract to one WebSocket message.
pub async fn run_websocket_message<F, T, E>(
    context: RequestContext,
    name: impl Into<String>,
    timeout: Duration,
    future: F,
) -> OperationOutcome<T>
where
    F: Future<Output = Result<T, E>>,
    E: std::fmt::Display,
{
    run_operation_boundary_with_timeout(
        context,
        OperationDescriptor {
            transport: OperationTransport::WebSocket,
            scope: OperationScope::Message,
            name: name.into(),
        },
        timeout,
        future,
    )
    .await
}
