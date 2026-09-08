mod flags;

use hhm_api::config::Config;
use tracing_subscriber::EnvFilter;

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    if let Some(output) = flags::process_control().map_err(anyhow::Error::msg)? {
        print!("{output}");
        return Ok(());
    }

    let filter_value = flags::var("RUST_LOG").unwrap_or_else(|_| "error".to_owned());
    let filter = EnvFilter::try_new(filter_value)
        .map_err(|_| anyhow::anyhow!("RUST_LOG contains an invalid tracing directive"))?;
    tracing_subscriber::fmt().with_env_filter(filter).init();

    let config = Config::from_resolver(|name| flags::var(name).ok())?;
    hhm_api::serve(config).await
}
