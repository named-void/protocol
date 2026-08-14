use std::{
    collections::HashMap,
    sync::{Arc, Mutex},
};

/// In-memory state for a payment awaiting 3DS challenge completion.
#[derive(Debug, Clone)]
pub struct Pending3dsData {
    pub auth_tx_id: String,
    /// Amount formatted for MPGS ("1.00")
    pub amount_str: String,
    pub amount: usize,
    pub currency: String,
    pub pan: String,
    pub expiry_month: String,
    pub expiry_year: String,
    pub security_code: String,
    pub name_on_card: String,
}

#[derive(Debug)]
struct Cell<T> {
    data: T,
    stored_at: time::OffsetDateTime,
}

const MAX_SIZE_BEFORE_CLEANUP: usize = 300;

#[derive(Debug, Clone, Default)]
pub struct ThreedsStorage {
    pending_3ds: Arc<Mutex<HashMap<String, Cell<Pending3dsData>>>>,
}

impl ThreedsStorage {
    pub fn remove(&self, order_id: &str) -> Option<Pending3dsData> {
        let mut store = self.pending_3ds.lock().unwrap();
        let removed = store.remove(order_id).map(|cell| cell.data);
        tracing::trace!(new_length = %store.len(), "Removed pending 3ds from storage");
        removed
    }

    pub fn store(&self, order: String, pending_3ds: Pending3dsData) {
        let mut store = self.pending_3ds.lock().unwrap();
        let now = time::OffsetDateTime::now_utc();

        if store.len() > MAX_SIZE_BEFORE_CLEANUP {
            tracing::warn!(%MAX_SIZE_BEFORE_CLEANUP, length = %store.len(), "Threeds storage exceeds size limit, running cleanup");
            let delete_threshold = now - time::Duration::DAY;
            store.retain(|_, Cell { stored_at, .. }| *stored_at >= delete_threshold);
        }

        store.insert(
            order,
            Cell {
                data: pending_3ds,
                stored_at: now,
            },
        );
        tracing::trace!(new_length = %store.len(), "Inserted pending 3ds in storage");
    }
}
