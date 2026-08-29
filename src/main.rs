mod four_transports;
mod web_api_plane;
use std::{collections::HashMap, env, sync::Arc};

use anyhow::{Context, bail};
use axum::{
    Json, Router,
    extract::{
        Path, State,
        ws::{Message, WebSocket, WebSocketUpgrade},
    },
    http::{HeaderValue, Method, StatusCode, Uri, header::CONTENT_TYPE},
    response::IntoResponse,
    routing::get,
};
use chrono::{DateTime, Duration, Utc};
use futures_util::{SinkExt, StreamExt};
use sea_orm::{Database, DatabaseConnection};
use serde::{Deserialize, Serialize};
use tokio::sync::{RwLock, broadcast};
use tower_http::{
    cors::{AllowOrigin, CorsLayer},
    trace::TraceLayer,
};
use tracing::{error, info};
use uuid::Uuid;

const MAX_MEMBER_NAME_CHARS: usize = 200;
const MAX_ROOM_TYPE_CHARS: usize = 100;
const MAX_WORKSPACE_PLAN_CHARS: usize = 100;
const MAX_NOTES_CHARS: usize = 4_000;
const MAX_STAY_DAYS: i64 = 366;
const RESERVATION_STATUSES: [&str; 5] = [
    "pending",
    "confirmed",
    "checked_in",
    "checked_out",
    "cancelled",
];

#[derive(Clone)]
struct AppState {
    db: Option<DatabaseConnection>,
    records: Arc<RwLock<HashMap<Uuid, Reservation>>>,
    events: broadcast::Sender<String>,
    supabase_url: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct Reservation {
    pub id: Uuid,
    pub created_at: DateTime<Utc>,
    pub updated_at: DateTime<Utc>,
    pub member_name: String,
    pub room_type: String,
    pub check_in: DateTime<Utc>,
    pub check_out: DateTime<Utc>,
    pub workspace_plan: String,
    pub status: String,
    pub notes: String,
}

#[derive(Debug, Deserialize)]
struct CreateReservation {
    pub member_name: String,
    pub room_type: String,
    pub check_in: DateTime<Utc>,
    pub check_out: DateTime<Utc>,
    pub workspace_plan: String,
    pub status: String,
    pub notes: String,
}

#[derive(Debug, Serialize)]
struct ReservationEvent<'a> {
    event: &'static str,
    reservation: &'a Reservation,
}

#[derive(Debug, Serialize)]
struct ApiError {
    code: &'static str,
    message: String,
}

#[derive(Debug, Serialize)]
struct Health {
    service: &'static str,
    status: &'static str,
    database_configured: bool,
    supabase_configured: bool,
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    dotenvy::dotenv().ok();
    tracing_subscriber::fmt()
        .with_env_filter(tracing_subscriber::EnvFilter::from_default_env())
        .init();

    let db = match env::var("DATABASE_URL") {
        Ok(url) if !url.trim().is_empty() => Some(
            Database::connect(url)
                .await
                .context("connect database")?,
        ),
        _ => None,
    };
    let (events, _) = broadcast::channel(512);
    let state = AppState {
        db,
        records: Arc::new(RwLock::new(HashMap::new())),
        events,
        supabase_url: non_empty_env("SUPABASE_URL"),
    };

    let app = Router::new()
        .route("/healthz", get(health))
            .route("/v1/data-plane/capabilities", axum::routing::get(|| async { axum::Json(crate::web_api_plane::capabilities()) }))
        .route("/v1/reservations", get(list_records).post(create_record))
        .route("/v1/reservations/{id}", get(get_record))
        .route("/v1/ws", get(ws_upgrade))
        .layer(cors_layer()?)
        .layer(TraceLayer::new_for_http())
        .with_state(state);

    let host = env::var("HOST").unwrap_or_else(|_| "0.0.0.0".into());
    let port = env::var("PORT").unwrap_or_else(|_| "8080".into());
    let listener = tokio::net::TcpListener::bind(format!("{host}:{port}")).await?;
    info!(address = %listener.local_addr()?, "Hacker House Medellín API listening");
    axum::serve(listener, app)
        .with_graceful_shutdown(shutdown_signal())
        .await?;
    Ok(())
}

fn non_empty_env(name: &str) -> Option<String> {
    env::var(name)
        .ok()
        .map(|value| value.trim().to_owned())
        .filter(|value| !value.is_empty())
}

fn cors_layer() -> anyhow::Result<CorsLayer> {
    let origins = parse_cors_origins(&env::var("CORS_ORIGINS").unwrap_or_default())?;
    let layer = CorsLayer::new()
        .allow_methods([Method::GET, Method::POST])
        .allow_headers([CONTENT_TYPE]);
    Ok(if origins.is_empty() {
        layer
    } else {
        layer.allow_origin(AllowOrigin::list(origins))
    })
}

fn parse_cors_origins(raw: &str) -> anyhow::Result<Vec<HeaderValue>> {
    let mut origins = Vec::new();
    for origin in raw.split(',').map(str::trim).filter(|item| !item.is_empty()) {
        if origin == "*" {
            bail!("CORS_ORIGINS must not contain a wildcard");
        }
        let uri = origin
            .parse::<Uri>()
            .with_context(|| format!("invalid CORS origin: {origin}"))?;
        if !matches!(uri.scheme_str(), Some("http" | "https"))
            || uri.authority().is_none()
            || uri.path() != "/"
            || uri.query().is_some()
        {
            bail!("CORS origin must be an exact http(s) origin without a path: {origin}");
        }
        let header = origin
            .parse::<HeaderValue>()
            .with_context(|| format!("invalid CORS origin header: {origin}"))?;
        if !origins.contains(&header) {
            origins.push(header);
        }
    }
    Ok(origins)
}

async fn health(State(state): State<AppState>) -> Json<Health> {
    Json(Health {
        service: "hhm-api",
        status: "ok",
        database_configured: state.db.is_some(),
        supabase_configured: state.supabase_url.is_some(),
    })
}

async fn list_records(State(state): State<AppState>) -> Json<Vec<Reservation>> {
    let mut records = state
        .records
        .read()
        .await
        .values()
        .cloned()
        .collect::<Vec<_>>();
    records.sort_by_key(|record| record.created_at);
    Json(records)
}

async fn get_record(
    Path(id): Path<Uuid>,
    State(state): State<AppState>,
) -> Result<Json<Reservation>, (StatusCode, Json<ApiError>)> {
    state
        .records
        .read()
        .await
        .get(&id)
        .cloned()
        .map(Json)
        .ok_or_else(|| api_error(StatusCode::NOT_FOUND, "not_found", "reservation not found"))
}

async fn create_record(
    State(state): State<AppState>,
    Json(input): Json<CreateReservation>,
) -> Result<(StatusCode, Json<Reservation>), (StatusCode, Json<ApiError>)> {
    let input = normalize_and_validate(input).map_err(|message| {
        api_error(
            StatusCode::UNPROCESSABLE_ENTITY,
            "invalid_reservation",
            message,
        )
    })?;
    let now = Utc::now();
    let record = Reservation {
        id: Uuid::new_v4(),
        created_at: now,
        updated_at: now,
        member_name: input.member_name,
        room_type: input.room_type,
        check_in: input.check_in,
        check_out: input.check_out,
        workspace_plan: input.workspace_plan,
        status: input.status,
        notes: input.notes,
    };
    let event = serde_json::to_string(&ReservationEvent {
        event: "reservation.created",
        reservation: &record,
    })
    .map_err(|source| {
        error!(error = %source, "failed to serialize reservation event");
        api_error(
            StatusCode::INTERNAL_SERVER_ERROR,
            "event_serialization_failed",
            "reservation could not be created",
        )
    })?;

    state.records.write().await.insert(record.id, record.clone());
    let _ = state.events.send(event);
    Ok((StatusCode::CREATED, Json(record)))
}

fn normalize_and_validate(mut input: CreateReservation) -> Result<CreateReservation, String> {
    input.member_name = input.member_name.trim().to_owned();
    input.room_type = input.room_type.trim().to_owned();
    input.workspace_plan = input.workspace_plan.trim().to_owned();
    input.status = input.status.trim().to_ascii_lowercase();
    input.notes = input.notes.trim().to_owned();

    validate_required_text("member_name", &input.member_name, MAX_MEMBER_NAME_CHARS)?;
    validate_required_text("room_type", &input.room_type, MAX_ROOM_TYPE_CHARS)?;
    validate_required_text(
        "workspace_plan",
        &input.workspace_plan,
        MAX_WORKSPACE_PLAN_CHARS,
    )?;
    if input.notes.chars().count() > MAX_NOTES_CHARS {
        return Err(format!("notes must be at most {MAX_NOTES_CHARS} characters"));
    }
    if input.check_out <= input.check_in {
        return Err("check_out must be later than check_in".to_owned());
    }
    if input.check_out - input.check_in > Duration::days(MAX_STAY_DAYS) {
        return Err(format!("stay must not exceed {MAX_STAY_DAYS} days"));
    }
    if !RESERVATION_STATUSES.contains(&input.status.as_str()) {
        return Err(format!(
            "status must be one of {}",
            RESERVATION_STATUSES.join(", ")
        ));
    }
    Ok(input)
}

fn validate_required_text(label: &str, value: &str, maximum: usize) -> Result<(), String> {
    if value.is_empty() {
        return Err(format!("{label} must not be blank"));
    }
    if value.chars().count() > maximum {
        return Err(format!("{label} must be at most {maximum} characters"));
    }
    Ok(())
}

fn api_error(
    status: StatusCode,
    code: &'static str,
    message: impl Into<String>,
) -> (StatusCode, Json<ApiError>) {
    (
        status,
        Json(ApiError {
            code,
            message: message.into(),
        }),
    )
}

async fn ws_upgrade(ws: WebSocketUpgrade, State(state): State<AppState>) -> impl IntoResponse {
    ws.on_upgrade(move |socket| websocket(socket, state))
}

async fn websocket(socket: WebSocket, state: AppState) {
    let (mut sender, mut receiver) = socket.split();
    let mut events = state.events.subscribe();

    loop {
        tokio::select! {
            event = events.recv() => match event {
                Ok(event) => {
                    if sender.send(Message::Text(event.into())).await.is_err() {
                        break;
                    }
                }
                Err(broadcast::error::RecvError::Lagged(skipped)) => {
                    info!(skipped, "WebSocket client lagged behind reservation events");
                }
                Err(broadcast::error::RecvError::Closed) => break,
            },
            message = receiver.next() => match message {
                Some(Ok(Message::Ping(payload))) => {
                    if sender.send(Message::Pong(payload)).await.is_err() {
                        break;
                    }
                }
                Some(Ok(Message::Close(_))) | Some(Err(_)) | None => break,
                Some(Ok(_)) => {}
            },
        }
    }
}

async fn shutdown_signal() {
    let _ = tokio::signal::ctrl_c().await;
}

#[cfg(test)]
mod tests {
    use super::*;

    fn timestamp(value: &str) -> DateTime<Utc> {
        value.parse().expect("valid test timestamp")
    }

    fn valid_input() -> CreateReservation {
        CreateReservation {
            member_name: "  Ada Lovelace  ".to_owned(),
            room_type: " private ".to_owned(),
            check_in: timestamp("2026-09-01T15:00:00Z"),
            check_out: timestamp("2026-09-14T11:00:00Z"),
            workspace_plan: " dedicated-desk ".to_owned(),
            status: " CONFIRMED ".to_owned(),
            notes: "  Arriving after the community dinner.  ".to_owned(),
        }
    }

    #[test]
    fn normalizes_a_valid_reservation() {
        let normalized = normalize_and_validate(valid_input()).expect("reservation is valid");
        assert_eq!(normalized.member_name, "Ada Lovelace");
        assert_eq!(normalized.room_type, "private");
        assert_eq!(normalized.workspace_plan, "dedicated-desk");
        assert_eq!(normalized.status, "confirmed");
        assert_eq!(normalized.notes, "Arriving after the community dinner.");
    }

    #[test]
    fn rejects_blank_names_and_invalid_dates() {
        let mut blank = valid_input();
        blank.member_name = " \t ".to_owned();
        assert_eq!(
            normalize_and_validate(blank).expect_err("blank name must fail"),
            "member_name must not be blank"
        );

        let mut dates = valid_input();
        dates.check_out = dates.check_in;
        assert_eq!(
            normalize_and_validate(dates).expect_err("invalid dates must fail"),
            "check_out must be later than check_in"
        );
    }

    #[test]
    fn rejects_unknown_statuses_and_unbounded_stays() {
        let mut status = valid_input();
        status.status = "approved-ish".to_owned();
        assert!(
            normalize_and_validate(status)
                .expect_err("unknown status must fail")
                .starts_with("status must be one of")
        );

        let mut stay = valid_input();
        stay.check_out = stay.check_in + Duration::days(MAX_STAY_DAYS + 1);
        assert_eq!(
            normalize_and_validate(stay).expect_err("long stay must fail"),
            format!("stay must not exceed {MAX_STAY_DAYS} days")
        );
    }

    #[test]
    fn parses_exact_cors_origins_and_deduplicates_them() {
        let origins = parse_cors_origins(
            "https://app.example.test, https://admin.example.test,https://app.example.test",
        )
        .expect("origins are valid");
        assert_eq!(origins.len(), 2);
        assert_eq!(origins[0], HeaderValue::from_static("https://app.example.test"));
        assert_eq!(origins[1], HeaderValue::from_static("https://admin.example.test"));
    }

    #[test]
    fn rejects_wildcard_and_path_cors_configuration() {
        assert!(parse_cors_origins("*").is_err());
        assert!(parse_cors_origins("https://app.example.test/path").is_err());
        assert!(parse_cors_origins("javascript:alert(1)").is_err());
    }
}
