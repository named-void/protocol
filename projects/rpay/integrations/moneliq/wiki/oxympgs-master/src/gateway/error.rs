use std::fmt;

#[derive(Debug)]
pub enum GatewayError {
    Http(reqwest::Error),
    Json(serde_json::Error),
    Api { result: String, explanation: String },
}

impl fmt::Display for GatewayError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Http(e) => write!(f, "HTTP error: {e}"),
            Self::Json(e) => write!(f, "JSON error: {e}"),
            Self::Api {
                result,
                explanation,
            } => {
                write!(f, "MPGS API error ({result}): {explanation}")
            }
        }
    }
}

impl From<reqwest::Error> for GatewayError {
    fn from(e: reqwest::Error) -> Self {
        Self::Http(e)
    }
}

impl From<serde_json::Error> for GatewayError {
    fn from(e: serde_json::Error) -> Self {
        Self::Json(e)
    }
}
