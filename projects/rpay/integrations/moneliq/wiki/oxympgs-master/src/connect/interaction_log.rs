use std::time::Instant;

use serde::Serialize;
use time::OffsetDateTime;

#[derive(Debug, Clone, Serialize)]
struct Request {
    url: String,
    params: serde_json::Value,
}

#[derive(Debug, Serialize)]
pub struct InteractionLog {
    gateway: &'static str,
    request: Option<Request>,
    status: Option<u16>,
    response: Option<serde_json::Value>,
    kind: String,
    #[serde(with = "time::serde::rfc3339")]
    created_at: time::OffsetDateTime,
    duration: f32,
}

#[derive(Debug, Clone)]
pub struct InteractionSpan {
    created: Instant,
    request: Option<Request>,
    response: Option<serde_json::Value>,
    response_status: Option<u16>,
}

impl InteractionSpan {
    pub fn enter() -> Self {
        Self {
            created: Instant::now(),
            request: None,
            response: None,
            response_status: None,
        }
    }

    pub fn set_request(&mut self, url: String, params: &impl Serialize) {
        let params = serde_json::to_value(params).expect("json serialization not fail");
        self.request = Some(Request { url, params });
    }

    pub fn set_response(&mut self, res: &impl Serialize) {
        self.response = serde_json::to_value(res).ok();
    }

    pub fn set_response_status(&mut self, status: u16) {
        self.response_status = Some(status);
    }

    pub fn interaction_log(self, kind: &str) -> InteractionLog {
        let created_at = OffsetDateTime::now_utc();
        InteractionLog {
            gateway: "oxympgs",
            request: self.request,
            status: self.response_status,
            response: self.response,
            kind: kind.into(),
            created_at,
            duration: self.created.elapsed().as_secs_f32(),
        }
    }
}
