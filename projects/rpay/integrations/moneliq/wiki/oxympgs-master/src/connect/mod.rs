use serde::Serialize;

use crate::gateway::mask;

pub mod api;
pub mod interaction_log;
pub mod status;

pub type Result<T> = std::result::Result<T, GwConnectErrorResponse>;

#[derive(Debug, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Status {
    Approved,
    Declined,
    Pending,
    Refunded,
}

#[derive(Debug, Serialize)]
pub struct GwConnectErrorResponse {
    result: bool,
    error: String,
    logs: Vec<interaction_log::InteractionLog>,
}

impl std::error::Error for GwConnectErrorResponse {}

impl std::fmt::Display for GwConnectErrorResponse {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.error)
    }
}

impl GwConnectErrorResponse {
    pub fn new(text: String, logs: Vec<interaction_log::InteractionLog>) -> Self {
        Self {
            result: false,
            error: text,
            logs,
        }
    }
}

impl axum::response::IntoResponse for GwConnectErrorResponse {
    fn into_response(self) -> axum::response::Response {
        tracing::debug!(data = %mask::secure_serializable(&self), "Connect API error response payload");
        (reqwest::StatusCode::OK, axum::Json(self)).into_response()
    }
}
