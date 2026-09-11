use std::{
    collections::BTreeMap,
    convert::Infallible,
    future::Future,
    net::{IpAddr, Ipv4Addr, SocketAddr},
    pin::Pin,
    sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    },
    time::Duration,
};

use axum::{
    Router,
    body::{Body, to_bytes},
    extract::ConnectInfo,
    http::{Request, StatusCode},
    routing::{get, post},
};
use hhm_api::middleware::{
    RequestPolicy, TlsMode, install, run_websocket_message, stack_from_policy,
};
use ores_middleware::{
    AuthDecision, AuthVerifier, IntegrationError, MiddlewareStack, OperationFailureKind,
    OperationOutcome, RequestContext, RequestMetadata, ResponseMetadata, RuntimeEnvironment,
    TelemetrySink,
};
use tower::ServiceExt;

fn policy() -> RequestPolicy {
    RequestPolicy {
        environment: RuntimeEnvironment::Test,
        tls_mode: TlsMode::Disabled,
        trusted_proxy_cidrs: Vec::new(),
        timeout_ms: 1_000,
        max_body_bytes: 1_024,
        rate_limit_capacity: 100,
        rate_limit_refill_per_second: 100.0,
        rate_limit_hmac_secret: None,
    }
}

fn request(method: &str, uri: &str, peer: [u8; 4]) -> Request<Body> {
    let mut request = Request::builder()
        .method(method)
        .uri(uri)
        .body(Body::empty())
        .expect("request");
    request.extensions_mut().insert(ConnectInfo(SocketAddr::new(
        IpAddr::V4(Ipv4Addr::from(peer)),
        31_337,
    )));
    request
}

fn installed(policy: &RequestPolicy, router: Router) -> Router {
    install(
        router,
        stack_from_policy(policy).expect("valid test policy"),
    )
}

#[tokio::test]
async fn oversized_requests_fail_with_413_and_correlation() {
    let app = installed(
        &RequestPolicy {
            max_body_bytes: 4,
            ..policy()
        },
        Router::new().route("/body", post(|| async { StatusCode::NO_CONTENT })),
    );
    let mut request = request("POST", "/body", [192, 0, 2, 10]);
    request
        .headers_mut()
        .insert("content-length", "5".parse().expect("header"));
    let response = app.oneshot(request).await.expect("response");
    assert_eq!(response.status(), StatusCode::PAYLOAD_TOO_LARGE);
    assert!(response.headers().contains_key("x-request-id"));
}

#[tokio::test]
async fn local_rate_limit_returns_429_without_identity_leakage() {
    let app = installed(
        &RequestPolicy {
            rate_limit_capacity: 1,
            rate_limit_refill_per_second: 0.000_001,
            ..policy()
        },
        Router::new().route("/limited", get(|| async { StatusCode::NO_CONTENT })),
    );
    let first = app
        .clone()
        .oneshot(request("GET", "/limited", [192, 0, 2, 11]))
        .await
        .expect("first response");
    assert_eq!(first.status(), StatusCode::NO_CONTENT);
    let second = app
        .oneshot(request("GET", "/limited", [192, 0, 2, 11]))
        .await
        .expect("second response");
    assert_eq!(second.status(), StatusCode::TOO_MANY_REQUESTS);
    let body = to_bytes(second.into_body(), 16 * 1024)
        .await
        .expect("bounded response");
    let body = String::from_utf8(body.to_vec()).expect("utf8 response");
    assert!(body.contains("rate_limited"));
    assert!(!body.contains("192.0.2.11"));
}

#[tokio::test]
async fn forwarded_headers_are_accepted_only_from_trusted_peers() {
    let policy = RequestPolicy {
        tls_mode: TlsMode::TrustedProxy,
        trusted_proxy_cidrs: vec!["10.0.0.0/8".into()],
        ..policy()
    };
    let app = installed(
        &policy,
        Router::new().route("/health", get(|| async { StatusCode::NO_CONTENT })),
    );
    let mut forged = request("GET", "/health", [192, 0, 2, 12]);
    forged
        .headers_mut()
        .insert("x-forwarded-proto", "https".parse().expect("header"));
    forged
        .headers_mut()
        .insert("x-forwarded-for", "203.0.113.9".parse().expect("header"));
    let response = app.clone().oneshot(forged).await.expect("response");
    assert_eq!(response.status(), StatusCode::BAD_REQUEST);

    let mut trusted = request("GET", "/health", [10, 20, 30, 40]);
    trusted
        .headers_mut()
        .insert("x-forwarded-proto", "https".parse().expect("header"));
    trusted
        .headers_mut()
        .insert("x-forwarded-for", "203.0.113.9".parse().expect("header"));
    let response = app.oneshot(trusted).await.expect("response");
    assert_eq!(response.status(), StatusCode::NO_CONTENT);
}

struct DenyAuth;

impl AuthVerifier for DenyAuth {
    fn verify<'a>(
        &'a self,
        _request: &'a RequestMetadata,
    ) -> Pin<Box<dyn Future<Output = Result<AuthDecision, IntegrationError>> + Send + 'a>> {
        Box::pin(async {
            Err(IntegrationError {
                code: "authentication_required",
                message: "authentication is required".into(),
            })
        })
    }
}

#[tokio::test]
async fn authentication_denial_is_401_and_handler_forbidden_is_403() {
    let stack = stack_from_policy(&policy())
        .expect("stack")
        .with_auth_verifier(Arc::new(DenyAuth));
    let denied = install(
        Router::new().route("/private", get(|| async { StatusCode::NO_CONTENT })),
        stack,
    )
    .oneshot(request("GET", "/private", [192, 0, 2, 13]))
    .await
    .expect("denied response");
    assert_eq!(denied.status(), StatusCode::UNAUTHORIZED);

    let forbidden = installed(
        &policy(),
        Router::new().route("/private", get(|| async { StatusCode::FORBIDDEN })),
    )
    .oneshot(request("GET", "/private", [192, 0, 2, 13]))
    .await
    .expect("forbidden response");
    assert_eq!(forbidden.status(), StatusCode::FORBIDDEN);
    assert!(forbidden.headers().contains_key("x-request-id"));
}

#[derive(Default)]
struct CaptureTelemetry {
    starts: AtomicUsize,
    finishes: AtomicUsize,
}

impl TelemetrySink for CaptureTelemetry {
    fn request_started<'a>(
        &'a self,
        _context: &'a RequestContext,
        _request: &'a RequestMetadata,
    ) -> Pin<Box<dyn Future<Output = ()> + Send + 'a>> {
        Box::pin(async move {
            self.starts.fetch_add(1, Ordering::SeqCst);
        })
    }

    fn request_finished<'a>(
        &'a self,
        _context: &'a RequestContext,
        _request: &'a RequestMetadata,
        _response: &'a ResponseMetadata,
    ) -> Pin<Box<dyn Future<Output = ()> + Send + 'a>> {
        Box::pin(async move {
            self.finishes.fetch_add(1, Ordering::SeqCst);
        })
    }
}

#[tokio::test]
async fn telemetry_boundary_observes_one_complete_request_lifecycle() {
    let telemetry = Arc::new(CaptureTelemetry::default());
    let stack: MiddlewareStack = stack_from_policy(&policy())
        .expect("stack")
        .with_telemetry(telemetry.clone());
    let response = install(
        Router::new().route("/ok", get(|| async { StatusCode::NO_CONTENT })),
        stack,
    )
    .oneshot(request(
        "GET",
        "/ok?secret=synthetic-do-not-log",
        [192, 0, 2, 14],
    ))
    .await
    .expect("response");
    assert_eq!(response.status(), StatusCode::NO_CONTENT);
    assert_eq!(telemetry.starts.load(Ordering::SeqCst), 1);
    assert_eq!(telemetry.finishes.load(Ordering::SeqCst), 1);
    assert!(!format!("{response:?}").contains("synthetic-do-not-log"));
}

fn ws_context(request_id: &str) -> RequestContext {
    RequestContext {
        request_id: request_id.into(),
        trace_id: "0123456789abcdef0123456789abcdef".into(),
        span_id: None,
        tenant_id: None,
        user_id: Some("user-test".into()),
        locale: None,
        started_at_unix_ms: 0,
        deadline_unix_ms: None,
        baggage: BTreeMap::new(),
    }
}

#[tokio::test]
async fn websocket_message_panic_isolated_from_the_next_message() {
    let failed = run_websocket_message(
        ws_context("ws-1"),
        "hhm.ws.message",
        Duration::from_millis(100),
        async move {
            panic!("synthetic private websocket content");
            #[allow(unreachable_code)]
            Ok::<_, Infallible>(())
        },
    )
    .await;
    assert!(matches!(
        failed,
        OperationOutcome::Failed(ref failure)
            if failure.kind == OperationFailureKind::Panic
                && !format!("{failure:?}").contains("synthetic private websocket content")
    ));

    let next = run_websocket_message(
        ws_context("ws-2"),
        "hhm.ws.message",
        Duration::from_millis(100),
        async { Ok::<_, Infallible>("accepted") },
    )
    .await;
    assert!(matches!(next, OperationOutcome::Completed("accepted")));
}
