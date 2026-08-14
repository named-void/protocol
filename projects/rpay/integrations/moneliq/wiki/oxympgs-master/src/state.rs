use std::time::Duration;

use crate::threeds_storage::ThreedsStorage;

#[derive(Debug, Clone)]
pub struct AppState {
    pub client: reqwest::Client,
    pub pending_3ds: ThreedsStorage,
}

impl AppState {
    pub fn new() -> Self {
        let client = reqwest::ClientBuilder::new()
            .timeout(Duration::from_secs(30))
            .build()
            .expect("http client to build");
        Self {
            client,
            pending_3ds: ThreedsStorage::default(),
        }
    }
}

impl Default for AppState {
    fn default() -> Self {
        Self::new()
    }
}
