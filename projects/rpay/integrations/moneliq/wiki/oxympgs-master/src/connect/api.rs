use axum::{extract::State, response::IntoResponse, routing::post};
use serde::{Deserialize, Serialize};
use tracing::instrument;

use crate::{
    connect::{
        GwConnectErrorResponse, Result, Status,
        interaction_log::{InteractionLog, InteractionSpan},
        status,
    },
    gateway::{
        self, mask,
        pay::{CardData, GatewayCode, GatewayRecommendation, MpgsOpResult, TransactionType},
    },
    state::AppState,
    threeds_storage::Pending3dsData,
};

#[instrument(skip_all, fields(token = %req.payment.token))]
pub async fn pay(
    State(state): State<AppState>,
    Json(req): Json<payment::GwConnectH2HPaymentRequest>,
) -> Result<GwConnectResponse<GwConnectH2HPaymentResponse>> {
    let card_params = req.params.card_params.as_ref().ok_or_else(|| {
        GwConnectErrorResponse::new("H2H payment requires card details".into(), vec![])
    })?;

    let order_id = req.payment.token.clone();
    let auth_tx_id = gateway::gen_id();

    let (expiry_month, year_full) = card_params
        .expires
        .split_once('/')
        .expect("reactivepay card format should be 11/2077");
    let expiry_year = &year_full[year_full.len().saturating_sub(2)..];

    let amount_str = format!("{:.2}", req.payment.gateway_amount as f64 / 100.0);
    let currency = req.payment.gateway_currency;

    let mpgs = gateway::MpgsClient::new(state.client, &req.settings);

    let card = CardData {
        number: card_params.pan.clone(),
        expiry: gateway::pay::CardExpiry {
            month: expiry_month.to_string(),
            year: expiry_year.to_string(),
        },
        security_code: card_params.cvv.clone(),
        name_on_card: card_params.holder.clone(),
    };

    let ip = req.payment.ip.as_deref().unwrap_or("127.0.0.1");
    let mut logs: Vec<InteractionLog> = Vec::new();
    let mut frictionless_auth_tx_id: Option<String> = None;

    // try 3ds flow
    let mut init_span = InteractionSpan::enter();
    match mpgs
        .initiate_auth(
            &order_id,
            &auth_tx_id,
            &currency,
            &card.number,
            &mut init_span,
        )
        .await
    {
        Ok(init_resp) => {
            logs.push(init_span.interaction_log("initiate_authentication"));

            if init_resp.result != MpgsOpResult::Success {
                tracing::debug!(
                    result = ?init_resp.result,
                    "INITIATE_AUTHENTICATION not successful, falling back to direct PAY"
                );
            } else {
                let mut auth_span = InteractionSpan::enter();
                match mpgs
                    .authenticate_payer(
                        &order_id,
                        &auth_tx_id,
                        &req.callback_3ds_url,
                        &amount_str,
                        &currency,
                        &card,
                        ip,
                        &mut auth_span,
                    )
                    .await
                {
                    Ok(auth_resp) => {
                        logs.push(auth_span.interaction_log("authenticate_payer"));

                        let trans_status = auth_resp.authentication.as_ref().and_then(|a| {
                            a.three_ds2
                                .as_ref()
                                .and_then(|t| t.transaction_status.as_deref())
                                .or_else(|| {
                                    a.three_ds
                                        .as_ref()
                                        .and_then(|t| t.transaction_status.as_deref())
                                })
                        });

                        tracing::debug!(?trans_status, "3DS authentication status");

                        match trans_status {
                            Some("Y") | Some("A") => {
                                // Frictionless success
                                frictionless_auth_tx_id = Some(auth_tx_id.clone());
                            }
                            Some("C") => {
                                let challenge = auth_resp
                                    .authentication
                                    .as_ref()
                                    .and_then(|a| a.redirect.as_ref())
                                    .and_then(|r| r.customized_html.as_ref())
                                    .and_then(|h| h.three_ds2.as_ref());
                                let acs_url = challenge
                                    .and_then(|d| d.acs_url.clone())
                                    .unwrap_or_default();
                                let creq =
                                    challenge.and_then(|d| d.c_req.clone()).unwrap_or_default();

                                tracing::info!("3DS challenge required");

                                state.pending_3ds.store(
                                    order_id.clone(),
                                    Pending3dsData {
                                        auth_tx_id,
                                        amount_str,
                                        amount: req.payment.gateway_amount,
                                        currency: currency.clone(),
                                        pan: card.number,
                                        expiry_month: card.expiry.month,
                                        expiry_year: card.expiry.year,
                                        security_code: card.security_code,
                                        name_on_card: card.name_on_card,
                                    },
                                );

                                return Ok(GwConnectResponse::new(
                                    GwConnectH2HPaymentResponse {
                                        status: Status::Pending,
                                        amount: req.payment.gateway_amount,
                                        currency: currency.clone(),
                                        details: None,
                                        card_enrolled: true,
                                        gateway_token: Some(order_id),
                                        redirect_request: Some(RedirectRequest {
                                            url: req.processing_url,
                                            kind: RedirectRequestType::PostIframes,
                                            iframes: vec![IframeSpec {
                                                url: acs_url,
                                                data: IframeData { creq },
                                            }],
                                        }),
                                    },
                                    logs,
                                ));
                            }
                            _ => {
                                tracing::debug!(
                                    ?trans_status,
                                    "3DS not applicable, falling back to direct PAY"
                                );
                            }
                        }
                    }
                    Err(e) => {
                        tracing::warn!(
                            "AUTHENTICATE_PAYER failed: {e}, falling back to direct PAY"
                        );
                        logs.push(auth_span.interaction_log("authenticate_payer"));
                    }
                }
            }
        }
        Err(e) => {
            tracing::warn!("INITIATE_AUTHENTICATION failed: {e}, falling back to direct PAY");
            logs.push(init_span.interaction_log("initiate_authentication"));
        }
    }

    // make pay request
    let pay_tx_id = gateway::gen_id();
    let mut pay_span = InteractionSpan::enter();
    match mpgs
        .pay(
            &order_id,
            &pay_tx_id,
            frictionless_auth_tx_id.as_deref(),
            &amount_str,
            &currency,
            &card,
            &mut pay_span,
        )
        .await
    {
        Ok(pay_resp) => {
            logs.push(pay_span.interaction_log("pay"));
            let provider_code = pay_resp.response.as_ref().and_then(|r| r.gateway_code);
            let (status, details) = match (pay_resp.result, provider_code) {
                (MpgsOpResult::Failure, _) => (Status::Declined, Some(pay_resp.details_message())),
                (MpgsOpResult::Success, Some(GatewayCode::Approved)) => (Status::Approved, None),
                (MpgsOpResult::Success, Some(GatewayCode::Declined)) => (Status::Declined, None),
                _ => (Status::Pending, None),
            };
            tracing::info!(
                result = ?pay_resp.result,
                "PAY completed"
            );
            Ok(GwConnectResponse::new(
                GwConnectH2HPaymentResponse {
                    status,
                    amount: req.payment.gateway_amount,
                    currency,
                    details,
                    card_enrolled: false,
                    gateway_token: Some(order_id),
                    redirect_request: None,
                },
                logs,
            ))
        }
        Err(e) => {
            logs.push(pay_span.interaction_log("pay"));
            tracing::error!(token = %req.payment.token, order_id, "PAY failed: {e}");
            Err(GwConnectErrorResponse::new(e.to_string(), logs))
        }
    }
}

#[instrument(skip_all, fields(token = %req.params.checkout_result_token))]
pub async fn confirm_secure_code(
    State(state): State<AppState>,
    Json(req): Json<confirm::Request>,
) -> Result<GwConnectResponse<()>> {
    let order_id = req.params.order_id;

    let Some(pending) = state.pending_3ds.remove(&order_id) else {
        tracing::warn!("Pending 3DS state not found");
        return Err(GwConnectErrorResponse::new(
            format!("pending 3DS state not found for order {order_id}"),
            vec![],
        ));
    };

    let mut logs = Vec::new();

    if req.params.result == "FAILURE" {
        tracing::info!("3DS authentication failed");
        return Ok(GwConnectResponse::new((), logs));
    }

    let mpgs = gateway::MpgsClient::new(state.client, &req.settings);

    let card = CardData {
        number: pending.pan,
        expiry: gateway::pay::CardExpiry {
            month: pending.expiry_month,
            year: pending.expiry_year,
        },
        security_code: pending.security_code,
        name_on_card: pending.name_on_card,
    };

    let pay_tx_id = gateway::gen_id();
    let mut pay_span = InteractionSpan::enter();
    let pay_result = mpgs
        .pay(
            &order_id,
            &pay_tx_id,
            Some(&pending.auth_tx_id),
            &pending.amount_str,
            &pending.currency,
            &card,
            &mut pay_span,
        )
        .await;

    match pay_result {
        Ok(pay_resp) => {
            logs.push(pay_span.interaction_log("pay_3ds"));
            tracing::info!(
                result = ?pay_resp.result,
                "3DS PAY completed"
            );
            Ok(GwConnectResponse::new((), logs))
        }
        Err(e) => {
            logs.push(pay_span.interaction_log("pay_3ds"));
            tracing::error!("3DS PAY failed: {e}");
            Err(GwConnectErrorResponse::new(e.to_string(), logs))
        }
    }
}

#[derive(Debug)]
struct StatusResHelper {
    pub amount: f64,
    pub currency: String,
    pub log: InteractionLog,
}

impl StatusResHelper {
    pub fn status_res(
        self,
        status: Status,
        details: String,
    ) -> Result<GwConnectResponse<status::res::Status>> {
        Ok(GwConnectResponse::new(
            status::res::Status {
                status,
                details,
                amount: (self.amount * 100.) as usize,
                currency: self.currency,
            },
            vec![self.log],
        ))
    }
}

#[instrument(skip_all, fields(token = %req.payment.token))]
pub async fn status(
    State(state): State<AppState>,
    Json(req): Json<status::req::Request>,
) -> Result<GwConnectResponse<status::res::Status>> {
    let mpgs = gateway::MpgsClient::new(state.client, &req.settings);

    let mut span = InteractionSpan::enter();
    let order = mpgs
        .get_order_status(&req.payment.gateway_token, &mut span)
        .await
        .map_err(|e| {
            let log = span.clone().interaction_log("status");
            tracing::error!("Failed to fetch order status: {e}");
            GwConnectErrorResponse::new(e.to_string(), vec![log])
        })?;
    tracing::info!(
        mpgs_status = %order.status,
        "Status polled"
    );

    let helper = StatusResHelper {
        amount: req
            .refund
            .as_ref()
            .map(|r| r.gateway_amount as f64 / 100.)
            .unwrap_or(order.amount),
        currency: order.currency.clone(),
        log: span.interaction_log("status"),
    };
    let transactions = order.transaction;

    // If we have refund field in status request that means that we are polling refund status.
    if let Some(_) = req.refund {
        let mut refunds = transactions
            .iter()
            .filter(|t| t.transaction.t_type == TransactionType::Refund);
        if let Some(refund_tx) = refunds.next_back() {
            let status = match (refund_tx.result, refund_tx.response.gateway_code) {
                (MpgsOpResult::Success, GatewayCode::Approved) => Status::Refunded,
                (MpgsOpResult::Failure, _) => Status::Declined,
                _ => Status::Pending,
            };
            return helper.status_res(status, order.status);
        }
        return helper.status_res(Status::Pending, order.status);
    }

    let mut payments = transactions.iter().filter(|t| {
        t.transaction.t_type == TransactionType::Payment
            || t.transaction.t_type == TransactionType::Authorization
    });
    let mut authentications = transactions
        .iter()
        .filter(|t| t.transaction.t_type == TransactionType::Authentication);

    if let Some(payment) = payments.next_back() {
        match payment.result {
            MpgsOpResult::Failure => {
                return helper.status_res(Status::Declined, order.status);
            }
            MpgsOpResult::Success if payment.response.gateway_code == GatewayCode::Approved => {
                return helper.status_res(Status::Approved, order.status);
            }
            MpgsOpResult::Success if payment.response.gateway_code == GatewayCode::Declined => {
                return helper.status_res(Status::Declined, order.status);
            }
            _ => return helper.status_res(Status::Pending, order.status),
        }
    }
    if let Some(auth) = authentications.next_back() {
        match auth.transaction.authentication_status {
            Some(
                gateway::pay::AuthenticationStatus::AuthenticationFailed
                | gateway::pay::AuthenticationStatus::AuthenticationRejected,
            ) if auth
                .response
                .gateway_recommendation
                .as_ref()
                .is_some_and(GatewayRecommendation::do_not_proceed) =>
            {
                return helper.status_res(Status::Declined, order.status);
            }
            _ => (),
        }
    }
    helper.status_res(Status::Pending, order.status)
}

#[instrument(skip_all, fields(refund_token = %req.refund.token, gateway_token = %req.payment.gateway_token))]
pub async fn refund(
    State(state): State<AppState>,
    Json(req): Json<refund::Request>,
) -> Result<GwConnectResponse<GwConnectH2HPaymentResponse>> {
    let order_id = &req.payment.gateway_token;
    let amount_str = format!(
        "{:.2}",
        req.refund
            .gateway_amount
            .unwrap_or(req.payment.gateway_amount) as f64
            / 100.
    );
    let currency = &req.payment.gateway_currency;

    let mpgs = gateway::MpgsClient::new(state.client, &req.settings);

    let refund_tx_id = &req.refund.token;
    let mut span = InteractionSpan::enter();

    match mpgs
        .refund(order_id, refund_tx_id, &amount_str, currency, &mut span)
        .await
    {
        Ok(resp) => {
            let log = span.interaction_log("refund");
            let (status, details) = match &resp.result {
                MpgsOpResult::Success
                    if resp
                        .response
                        .as_ref()
                        .and_then(|r| r.gateway_code)
                        .is_some_and(|c| c == GatewayCode::Approved) =>
                {
                    (Status::Approved, None)
                }
                MpgsOpResult::Success
                    if resp
                        .response
                        .as_ref()
                        .and_then(|r| r.gateway_code)
                        .is_some_and(|c| c == GatewayCode::Declined) =>
                {
                    (Status::Declined, None)
                }
                MpgsOpResult::Failure => (Status::Declined, Some(resp.details_message())),
                other => (
                    Status::Pending,
                    Some(format!("unexpected result: {other:?}")),
                ),
            };
            tracing::info!(
                result = ?resp.result,
                "REFUND completed"
            );
            Ok(GwConnectResponse::new(
                GwConnectH2HPaymentResponse {
                    status,
                    amount: req.payment.gateway_amount,
                    currency: currency.clone(),
                    details,
                    card_enrolled: false,
                    gateway_token: Some(order_id.clone()),
                    redirect_request: None,
                },
                vec![log],
            ))
        }
        Err(e) => {
            let log = span.interaction_log("refund");
            tracing::error!("REFUND failed: {e}");
            Err(GwConnectErrorResponse::new(e.to_string(), vec![log]))
        }
    }
}

#[derive(Debug, Serialize)]
pub struct GwConnectResponse<T> {
    result: bool,
    logs: Vec<InteractionLog>,
    #[serde(flatten)]
    data: T,
}

impl<T> GwConnectResponse<T> {
    pub fn new(data: T, logs: Vec<InteractionLog>) -> Self {
        Self {
            result: true,
            logs,
            data,
        }
    }
}

impl<T: Serialize> IntoResponse for GwConnectResponse<T> {
    fn into_response(self) -> axum::response::Response {
        let value = serde_json::to_value(&self).unwrap();
        tracing::debug!(data = %mask::secure_value(&value), "Connect API response payload");
        axum::Json(value).into_response()
    }
}

#[derive(Debug, Serialize)]
pub struct GwConnectH2HPaymentResponse {
    pub status: Status,
    pub amount: usize,
    pub currency: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub details: Option<String>,
    pub card_enrolled: bool,
    pub gateway_token: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub redirect_request: Option<RedirectRequest>,
}

#[derive(Debug, Serialize)]
pub struct RedirectRequest {
    pub url: String,
    #[serde(rename = "type")]
    pub kind: RedirectRequestType,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub iframes: Vec<IframeSpec>,
}

#[derive(Debug, Serialize)]
pub struct IframeSpec {
    pub url: String,
    pub data: IframeData,
}

#[derive(Debug, Serialize)]
pub struct IframeData {
    pub creq: String,
}

#[derive(Debug, Serialize, Default)]
#[serde(rename_all = "snake_case")]
#[allow(unused)]
pub enum RedirectRequestType {
    PostIframes,
    #[default]
    GetWithProcessing,
    Get,
    Post,
    RedirectHtml,
}

#[derive(Debug, Deserialize, Serialize, Clone)]
pub struct Settings {
    pub merchant_id: String,
    pub api_password: String,
}

pub mod payment {
    use serde::{Deserialize, Serialize};

    use crate::connect::api::Settings;

    #[derive(Debug, Deserialize, Serialize, Clone)]
    pub struct GwConnectH2HPaymentRequest {
        pub processing_url: String,
        pub callback_3ds_url: String,
        pub payment: Payment,
        pub params: H2HParams,
        pub settings: Settings,
    }

    #[derive(Debug, Deserialize, Serialize, Clone)]
    pub struct H2HParams {
        #[serde(flatten)]
        pub card_params: Option<H2HCardParams>,
    }

    #[derive(Debug, Clone, Serialize)]
    pub struct H2HCardParams {
        pub cvv: String,
        pub expires: String,
        pub pan: String,
        pub holder: String,
    }

    // Implement deserialize manually to conceal any "helpful" error messages that can leak
    // sensitive data
    impl<'de> serde::de::Deserialize<'de> for H2HCardParams {
        fn deserialize<D>(deserializer: D) -> std::result::Result<H2HCardParams, D::Error>
        where
            D: serde::de::Deserializer<'de>,
        {
            #[derive(Debug, Clone, Deserialize)]
            struct H2HCardParamsShadow {
                cvv: String,
                expires: String,
                pan: String,
                holder: String,
            }

            impl From<H2HCardParamsShadow> for H2HCardParams {
                fn from(
                    H2HCardParamsShadow {
                        cvv,
                        expires,
                        pan,
                        holder,
                    }: H2HCardParamsShadow,
                ) -> Self {
                    let pan = pan.replace('-', "");
                    Self {
                        cvv,
                        expires,
                        pan,
                        holder,
                    }
                }
            }

            H2HCardParamsShadow::deserialize(deserializer)
                .map(Into::into)
                .map_err(|_| serde::de::Error::custom("failed to deserialize card data"))
        }
    }

    #[derive(Debug, Deserialize, Serialize, Clone)]
    pub struct Payment {
        pub gateway_amount: usize,
        pub gateway_currency: String,
        pub ip: Option<String>,
        pub token: String,
    }
}

pub mod confirm {
    use serde::Deserialize;

    use crate::connect::api::Settings;

    #[derive(Debug, Deserialize)]
    pub struct Request {
        pub settings: Settings,
        pub params: Params,
    }

    #[derive(Debug, Deserialize)]
    pub struct Params {
        pub checkout_result_token: String,
        pub result: String,
        #[serde(rename = "order.id")]
        pub order_id: String,
    }
}

pub mod refund {
    use serde::Deserialize;

    use crate::connect::api::Settings;

    #[derive(Debug, Deserialize)]
    pub struct Request {
        pub settings: Settings,
        pub payment: Payment,
        pub refund: Refund,
    }

    #[derive(Debug, Deserialize)]
    pub struct Payment {
        pub gateway_amount: usize,
        pub gateway_currency: String,
        pub gateway_token: String,
    }

    #[derive(Debug, Deserialize)]
    pub struct Refund {
        pub gateway_amount: Option<usize>,
        pub token: String,
    }
}

pub fn router() -> axum::Router<crate::state::AppState> {
    axum::Router::new()
        .route("/pay", post(pay))
        .route("/confirm_secure_code", post(confirm_secure_code))
        .route("/status", post(status))
        .route("/refund", post(refund))
}

/// `Json` extractor wrapper that customizes the error from `axum::extract::Json`
pub struct Json<T>(pub T);

impl<S, T> axum::extract::FromRequest<S> for Json<T>
where
    T: serde::de::DeserializeOwned + Send,
    S: Send + Sync,
{
    type Rejection = axum::Json<GwConnectErrorResponse>;

    async fn from_request(
        req: axum::http::Request<axum::body::Body>,
        state: &S,
    ) -> std::result::Result<Self, Self::Rejection> {
        let value = match axum::Json::<serde_json::Value>::from_request(req, state).await {
            Ok(axum::Json(value)) => value,
            Err(e) => {
                tracing::error!(error = %e, "Failed to deserialize gateway connect request");
                return Err(axum::Json(GwConnectErrorResponse::new(
                    e.to_string(),
                    vec![],
                )));
            }
        };
        tracing::debug!(payload = %mask::secure_value(&value), "Gateway connect request");
        match serde_json::from_value::<T>(value) {
            Ok(value) => Ok(Self(value)),
            Err(e) => {
                tracing::error!(error = %e, "Failed to deserialize value into target type");
                Err(axum::Json(GwConnectErrorResponse::new(
                    e.to_string(),
                    vec![],
                )))
            }
        }
    }
}
