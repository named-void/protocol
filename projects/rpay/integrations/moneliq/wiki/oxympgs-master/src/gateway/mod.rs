use std::sync::LazyLock;

use base64::{Engine as _, engine::general_purpose::STANDARD};
use reqwest::header::{AUTHORIZATION, HeaderValue};
use serde::{Serialize, de::DeserializeOwned};

use crate::connect::{api::Settings, interaction_log::InteractionSpan};

pub mod error;
pub mod mask;
pub(crate) mod pay;

pub type Result<T> = std::result::Result<T, error::GatewayError>;

static BASE_URL: LazyLock<String> = LazyLock::new(|| {
    std::env::var("BASE_URL")
        .unwrap_or_else(|_| "https://eu-gateway.mastercard.com/api/rest/version/100".to_string())
});

pub fn gen_id() -> String {
    use rand::RngExt;
    let bytes: [u8; 16] = rand::rng().random();
    hex::encode(bytes)
}

pub struct MpgsClient {
    /// Base URL including merchant path: `{BASE_URL}/merchant/{merchant_id}`
    base_url: String,
    client: reqwest::Client,
    auth_header: HeaderValue,
}

impl MpgsClient {
    pub fn new(
        client: reqwest::Client,
        Settings {
            merchant_id,
            api_password,
        }: &Settings,
    ) -> Self {
        let encoded = STANDARD.encode(format!("merchant.{merchant_id}:{api_password}"));
        let auth_value = format!("Basic {encoded}");
        Self {
            base_url: format!("{}/merchant/{merchant_id}", *BASE_URL),
            client: client.clone(),
            auth_header: HeaderValue::from_str(&auth_value).expect("auth header is valid ASCII"),
        }
    }

    async fn put_transaction<R, T>(
        &self,
        order_id: &str,
        tx_id: &str,
        body: &R,
        span: &mut InteractionSpan,
    ) -> Result<T>
    where
        R: Serialize,
        T: DeserializeOwned,
    {
        let url = format!("{}/order/{order_id}/transaction/{tx_id}", self.base_url);
        let masked_req = mask::secure_serializable(body);
        tracing::debug!(%url, data = %masked_req, "MPGS PUT transaction request");
        span.set_request(url.clone(), &masked_req);

        let resp = self
            .client
            .put(&url)
            .header(AUTHORIZATION, self.auth_header.clone())
            .json(body)
            .send()
            .await?;

        let status = resp.status().as_u16();
        span.set_response_status(status);

        let raw: serde_json::Value = resp.json().await?;
        let masked_resp = mask::secure_value(&raw);
        span.set_response(&masked_resp);
        tracing::debug!(%url, %status, response = %masked_resp, "MPGS PUT transaction response");

        if let Some(result) = raw.get("result").and_then(|v| v.as_str()) {
            if result == "ERROR" {
                let explanation = raw
                    .get("error")
                    .and_then(|e| e.get("explanation"))
                    .and_then(|v| v.as_str())
                    .unwrap_or("unknown error")
                    .to_string();
                return Err(error::GatewayError::Api {
                    result: result.to_string(),
                    explanation,
                });
            }
        }

        Ok(serde_json::from_value(raw)?)
    }

    async fn get_order<T: DeserializeOwned>(
        &self,
        order_id: &str,
        span: &mut InteractionSpan,
    ) -> Result<T> {
        let url = format!("{}/order/{order_id}", self.base_url);
        tracing::debug!(%url, "MPGS GET order request");
        span.set_request(url.clone(), &serde_json::Value::Null);

        let resp = self
            .client
            .get(&url)
            .header(AUTHORIZATION, self.auth_header.clone())
            .send()
            .await?;

        let status = resp.status().as_u16();
        span.set_response_status(status);

        let raw: serde_json::Value = resp.json().await?;
        let masked_resp = mask::secure_value(&raw);
        span.set_response(&masked_resp);
        tracing::debug!(%url, %status, response = %masked_resp, "MPGS GET order response");

        Ok(serde_json::from_value(raw)?)
    }

    pub async fn initiate_auth(
        &self,
        order_id: &str,
        auth_tx_id: &str,
        currency: &str,
        card_number: &str,
        span: &mut InteractionSpan,
    ) -> Result<pay::MpgsTransactionResponse> {
        let req = pay::InitiateAuthRequest::new(order_id, currency, card_number);
        self.put_transaction(order_id, auth_tx_id, &req, span).await
    }

    pub async fn authenticate_payer(
        &self,
        order_id: &str,
        auth_tx_id: &str,
        redirect_url: &str,
        amount: &str,
        currency: &str,
        card: &pay::CardData,
        ip_address: &str,
        span: &mut InteractionSpan,
    ) -> Result<pay::MpgsTransactionResponse> {
        let req =
            pay::AuthenticatePayerRequest::new(redirect_url, amount, currency, card, ip_address);
        self.put_transaction(order_id, auth_tx_id, &req, span).await
    }

    pub async fn pay(
        &self,
        order_id: &str,
        pay_tx_id: &str,
        auth_tx_id: Option<&str>,
        amount: &str,
        currency: &str,
        card: &pay::CardData,
        span: &mut InteractionSpan,
    ) -> Result<pay::MpgsTransactionResponse> {
        let req = pay::PayRequest::new(order_id, pay_tx_id, auth_tx_id, amount, currency, card);
        self.put_transaction(order_id, pay_tx_id, &req, span).await
    }

    pub async fn refund(
        &self,
        order_id: &str,
        refund_tx_id: &str,
        amount: &str,
        currency: &str,
        span: &mut InteractionSpan,
    ) -> Result<pay::MpgsTransactionResponse> {
        let req = pay::RefundRequest::new(amount, currency);
        self.put_transaction(order_id, refund_tx_id, &req, span)
            .await
    }

    pub async fn get_order_status(
        &self,
        order_id: &str,
        span: &mut InteractionSpan,
    ) -> Result<pay::MpgsOrderResponse> {
        self.get_order(order_id, span).await
    }
}
