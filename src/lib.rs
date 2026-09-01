pub mod api;
pub mod auth;
pub mod config;
pub mod external;

use tracing::info;

use crate::{api::AppState, config::Config};

/// Serves the `HHaus` intake API until shutdown.
///
/// # Errors
///
/// Returns an error when startup dependencies, binding, or serving fails.
pub async fn serve(config: Config) -> anyhow::Result<()> {
    let state = AppState::from_config(&config).await?;
    let app = api::router(state, &config.cors_origins)?;
    let listener = tokio::net::TcpListener::bind(config.bind_address).await?;
    info!(address = %listener.local_addr()?, "HHaus API listening");
    axum::serve(listener, app)
        .with_graceful_shutdown(shutdown_signal())
        .await?;
    Ok(())
}

async fn shutdown_signal() {
    let _ = tokio::signal::ctrl_c().await;
}
