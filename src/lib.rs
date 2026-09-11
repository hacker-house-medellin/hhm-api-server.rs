mod api;
mod auth;
pub mod config;
mod external;
pub mod four_transports;
pub mod middleware;
mod persistence;
pub mod web_api_plane;

use std::net::SocketAddr;

use tracing::info;

use crate::{api::AppState, config::Config};

/// Serves the `HHaus` intake API until shutdown.
///
/// # Errors
///
/// Returns an error when startup dependencies, binding, or serving fails.
pub async fn serve(config: Config) -> anyhow::Result<()> {
    let state = AppState::from_config(&config).await?;
    let lifecycle = middleware::stack(&config)?;
    let app = middleware::install(api::router(state, &config.cors_origins)?, lifecycle);
    let listener = tokio::net::TcpListener::bind(config.bind_address).await?;
    info!(address = %listener.local_addr()?, "HHaus API listening");
    axum::serve(
        listener,
        app.into_make_service_with_connect_info::<SocketAddr>(),
    )
    .with_graceful_shutdown(shutdown_signal())
    .await?;
    Ok(())
}

async fn shutdown_signal() {
    let _ = tokio::signal::ctrl_c().await;
}
