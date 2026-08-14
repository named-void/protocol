use std::net::{Ipv4Addr, SocketAddrV4};

use axum::Router;
use oxympgs::{connect, state};
use tracing_subscriber::EnvFilter;

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt()
        .with_env_filter(EnvFilter::from_default_env())
        .with_ansi(false)
        .init();

    match dotenvy::dotenv() {
        Ok(p) => tracing::info!(path = %p.display(), "Loaded environment variables from .env file"),
        Err(e) => tracing::warn!("Failed to environment variables from .env: {e}"),
    };

    let state = state::AppState::new();

    let app = Router::new()
        .merge(connect::api::router())
        .layer(tower_http::trace::TraceLayer::new_for_http())
        .with_state(state);

    let port: u16 = std::env::var("PORT")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(4313);

    let listener = tokio::net::TcpListener::bind(SocketAddrV4::new(Ipv4Addr::UNSPECIFIED, port))
        .await
        .unwrap();

    tracing::info!("Serving on port {port}");
    axum::serve(listener, app)
        .with_graceful_shutdown(async {
            let _ = tokio::signal::ctrl_c().await;
        })
        .await
        .unwrap();
}
