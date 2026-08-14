use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize)]
pub struct CardData {
    pub number: String,
    pub expiry: CardExpiry,
    #[serde(rename = "securityCode")]
    pub security_code: String,
    #[serde(rename = "nameOnCard")]
    pub name_on_card: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct CardExpiry {
    pub month: String,
    pub year: String,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct PayOrder<'a> {
    amount: &'a str,
    currency: &'a str,
    reference: &'a str,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct OrderCurrencyRef<'a> {
    currency: &'a str,
    reference: &'a str,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct OrderAmountCurrency<'a> {
    amount: &'a str,
    currency: &'a str,
}

#[derive(Debug, Serialize)]
struct SourceOfFunds<'a> {
    #[serde(rename = "type")]
    kind: &'static str,
    provided: ProvidedCard<'a>,
}

#[derive(Debug, Serialize)]
struct SourceOfFundsCardNumber<'a> {
    #[serde(rename = "type")]
    kind: &'static str,
    provided: ProvidedCardNumber<'a>,
}

#[derive(Debug, Serialize)]
struct ProvidedCard<'a> {
    card: &'a CardData,
}

#[derive(Debug, Serialize)]
struct ProvidedCardNumber<'a> {
    card: CardNumberOnly<'a>,
}

#[derive(Debug, Serialize)]
struct CardNumberOnly<'a> {
    number: &'a str,
}

#[derive(Debug, Serialize)]
struct TxRef<'a> {
    reference: &'a str,
}

// REFUND request

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct RefundRequest<'a> {
    api_operation: &'static str,
    transaction: RefundTransaction<'a>,
}

#[derive(Debug, Serialize)]
struct RefundTransaction<'a> {
    amount: &'a str,
    currency: &'a str,
}

impl<'a> RefundRequest<'a> {
    pub fn new(amount: &'a str, currency: &'a str) -> Self {
        Self {
            api_operation: "REFUND",
            transaction: RefundTransaction { amount, currency },
        }
    }
}

// PAY request

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct PayRequest<'a> {
    api_operation: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    authentication: Option<PayAuthentication<'a>>,
    order: PayOrder<'a>,
    source_of_funds: SourceOfFunds<'a>,
    transaction: TxRef<'a>,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct PayAuthentication<'a> {
    transaction_id: &'a str,
}

impl<'a> PayRequest<'a> {
    pub fn new(
        order_id: &'a str,
        pay_tx_id: &'a str,
        auth_tx_id: Option<&'a str>,
        amount: &'a str,
        currency: &'a str,
        card: &'a CardData,
    ) -> Self {
        Self {
            api_operation: "PAY",
            authentication: auth_tx_id.map(|id| PayAuthentication { transaction_id: id }),
            order: PayOrder {
                amount,
                currency,
                reference: order_id,
            },
            source_of_funds: SourceOfFunds {
                kind: "CARD",
                provided: ProvidedCard { card },
            },
            transaction: TxRef {
                reference: pay_tx_id,
            },
        }
    }
}

// INITIATE_AUTHENTICATION request

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct InitiateAuthRequest<'a> {
    api_operation: &'static str,
    authentication: AuthChannel,
    order: OrderCurrencyRef<'a>,
    source_of_funds: SourceOfFundsCardNumber<'a>,
}

/// authentication.purpose If you do not provide a value, the gateway will use PAYMENT_TRANSACTION as the default.
#[derive(Debug, Serialize)]
struct AuthChannel {
    /// Indicates the channel in which the authentication request is being initiated.
    channel: &'static str,
}

impl<'a> InitiateAuthRequest<'a> {
    pub fn new(order_id: &'a str, currency: &'a str, card_number: &'a str) -> Self {
        Self {
            api_operation: "INITIATE_AUTHENTICATION",
            authentication: AuthChannel {
                channel: "PAYER_BROWSER",
            },
            order: OrderCurrencyRef {
                currency,
                reference: order_id,
            },
            source_of_funds: SourceOfFundsCardNumber {
                kind: "CARD",
                provided: ProvidedCardNumber {
                    card: CardNumberOnly {
                        number: card_number,
                    },
                },
            },
        }
    }
}

// AUTHENTICATE_PAYER request

#[derive(Debug, Serialize)]
struct SourceOfFundsProvided<'a> {
    provided: ProvidedCard<'a>,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct AuthenticatePayerRequest<'a> {
    api_operation: &'static str,
    authentication: AuthRedirect<'a>,
    order: OrderAmountCurrency<'a>,
    source_of_funds: SourceOfFundsProvided<'a>,
    device: DeviceInfo<'a>,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct AuthRedirect<'a> {
    redirect_response_url: &'a str,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct DeviceInfo<'a> {
    browser: &'a str,
    browser_details: BrowserDetails<'a>,
    ip_address: &'a str,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct BrowserDetails<'a> {
    #[serde(rename = "3DSecureChallengeWindowSize")]
    challenge_window_size: &'static str,
    accept_headers: &'a str,
    color_depth: u32,
    java_enabled: bool,
    language: &'a str,
    screen_height: u32,
    screen_width: u32,
    time_zone: i32,
}

impl Default for BrowserDetails<'_> {
    fn default() -> Self {
        Self {
            challenge_window_size: "FULL_SCREEN",
            accept_headers: "application/json,text/html,*/*",
            color_depth: 24,
            java_enabled: false,
            language: "en-US",
            screen_height: 900,
            screen_width: 1440,
            time_zone: 0,
        }
    }
}

impl<'a> AuthenticatePayerRequest<'a> {
    pub fn new(
        redirect_url: &'a str,
        amount: &'a str,
        currency: &'a str,
        card: &'a CardData,
        ip_address: &'a str,
    ) -> Self {
        Self {
            api_operation: "AUTHENTICATE_PAYER",
            authentication: AuthRedirect {
                redirect_response_url: redirect_url,
            },
            order: OrderAmountCurrency { amount, currency },
            source_of_funds: SourceOfFundsProvided {
                provided: ProvidedCard { card },
            },
            device: DeviceInfo {
                browser: "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
                browser_details: BrowserDetails::default(),
                ip_address,
            },
        }
    }
}

// Responses

#[derive(Debug, Deserialize)]
pub struct MpgsTransactionResponse {
    pub result: MpgsOpResult,
    pub authentication: Option<AuthenticationResp>,
    pub order: Option<MpgsOrderInTx>,
    pub response: Option<MpgsResponseDetails>,
    pub error: Option<MpgsApiError>,
}

impl MpgsTransactionResponse {
    pub fn details_message(&self) -> String {
        let msg = match &self.response {
            Some(MpgsResponseDetails {
                acquirer_message: Some(message),
                ..
            }) => message.to_string(),
            Some(MpgsResponseDetails {
                gateway_code: Some(message),
                ..
            }) => format!("{message:?}"),
            _ => "no message".to_string(),
        };
        format!("{:?} ({msg})", self.result)
    }
}

#[derive(Debug, Deserialize)]
pub struct AuthenticationResp {
    #[serde(rename = "3ds2")]
    pub three_ds2: Option<ThreeDs2>,
    #[serde(rename = "3ds")]
    pub three_ds: Option<ThreeDs1>,
    /// Challenge URL and creq live under redirect.customizedHtml["3ds2"]
    pub redirect: Option<AuthRedirectResp>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct AuthRedirectResp {
    pub customized_html: Option<CustomizedHtmlResp>,
}

#[derive(Debug, Deserialize)]
pub struct CustomizedHtmlResp {
    #[serde(rename = "3ds2")]
    pub three_ds2: Option<ChallengeData>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ChallengeData {
    pub acs_url: Option<String>,
    /// base64url-encoded challenge request
    pub c_req: Option<String>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ThreeDs2 {
    pub transaction_status: Option<String>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ThreeDs1 {
    pub transaction_status: Option<String>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct MpgsOrderInTx {
    pub status: Option<String>,
    pub amount: Option<f64>,
    pub currency: Option<String>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct MpgsResponseDetails {
    #[serde(default)]
    pub gateway_code: Option<GatewayCode>,
    pub acquirer_message: Option<String>,
}

#[derive(Debug, Deserialize)]
pub struct MpgsApiError {
    pub cause: String,
    pub explanation: String,
}

#[derive(Debug, Deserialize)]
pub struct MpgsOrderResponse {
    pub status: String,
    pub amount: f64,
    #[serde(default)]
    pub transaction: Vec<MpgsOrderTransaction>,
    pub currency: String,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct MpgsOrderTransaction {
    pub result: MpgsOpResult,
    pub transaction: MpgsOrderTransactionInnerTransaction,
    pub response: MpgsOrderTransactionInnerTransactionResponse,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct MpgsOrderTransactionInnerTransaction {
    #[serde(rename = "type", default)]
    pub t_type: TransactionType,
    pub authentication_status: Option<AuthenticationStatus>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct MpgsOrderTransactionInnerTransactionResponse {
    #[serde(default)]
    pub gateway_code: GatewayCode,
    pub gateway_recommendation: Option<GatewayRecommendation>,
}

/// Summary of the success or otherwise of the operation.
#[derive(Debug, Deserialize, Clone, Copy, Default, Eq, PartialEq)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum GatewayCode {
    /// Transaction aborted by payer
    Aborted,
    /// Acquirer system error occurred processing the transaction
    AcquirerSystemError,
    /// Transaction Approved
    Approved,
    /// The transaction was automatically approved by the gateway. It was not submitted to the acquirer.
    ApprovedAuto,
    /// Transaction Approved - pending batch settlement
    ApprovedPendingSettlement,
    /// Payer authentication failed
    AuthenticationFailed,
    /// The operation determined that payer authentication is possible for the given card, but this has not been completed, and requires further action by the merchant to proceed.
    AuthenticationInProgress,
    /// A balance amount is available for the card, and the payer can redeem points.
    BalanceAvailable,
    /// A balance amount might be available for the card. Points redemption should be offered to the payer.
    BalanceUnknown,
    /// Transaction blocked due to Risk or 3D Secure blocking rules
    Blocked,
    /// Transaction cancelled by payer
    Cancelled,
    /// The requested operation was not successful. For example, a payment was declined by issuer or payer authentication was not able to be successfully completed.
    Declined,
    /// Transaction declined due to address verification
    DeclinedAvs,
    /// Transaction declined due to address verification and card security code
    DeclinedAvsCsc,
    /// Transaction declined due to card security code
    DeclinedCsc,
    /// Transaction declined - do not contact issuer
    DeclinedDoNotContact,
    /// Transaction declined due to invalid PIN
    DeclinedInvalidPin,
    /// Transaction declined due to payment plan
    DeclinedPaymentPlan,
    /// Transaction declined due to PIN required
    DeclinedPinRequired,
    /// Deferred transaction received and awaiting processing
    DeferredTransactionReceived,
    /// Transaction declined due to duplicate batch
    DuplicateBatch,
    /// Transaction retry limit exceeded
    ExceededRetryLimit,
    /// Transaction declined due to expired card
    ExpiredCard,
    /// Transaction declined due to insufficient funds
    InsufficientFunds,
    /// Invalid card security code
    InvalidCsc,
    /// Order locked - another transaction is in progress for this order
    LockFailure,
    /// Card holder is not enrolled in 3D Secure
    #[serde(rename = "NOT_ENROLLED_3D_SECURE")]
    NotEnrolled3dSecure,
    /// Transaction type not supported
    NotSupported,
    /// A balance amount is not available for the card. The payer cannot redeem points.
    NoBalance,
    /// The transaction was approved for a lesser amount than requested. The approved amount is returned in order.totalAuthorizedAmount.
    PartiallyApproved,
    /// Transaction is pending
    Pending,
    /// Transaction declined - refer to issuer
    Referred,
    /// The transaction has successfully been created in the gateway. It is either awaiting submission to the acquirer or has been submitted to the acquirer but the gateway has not yet received a response about the success or otherwise of the payment.
    Submitted,
    /// Internal system error occurred processing the transaction
    SystemError,
    /// The gateway has timed out the request to the acquirer because it did not receive a response. Points redemption should not be offered to the payer.
    TimedOut,
    /// The transaction has been submitted to the acquirer but the gateway was not able to find out about the success or otherwise of the payment. If the gateway subsequently finds out about the success of the payment it will update the response code.
    #[default]
    Unknown,
    /// Unspecified failure
    UnspecifiedFailure,
}

/// A system-generated high level overall result of the operation.
#[derive(Debug, Deserialize, Clone, Copy, Default, Eq, PartialEq)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum MpgsOpResult {
    /// The operation was declined or rejected by the gateway, acquirer or issuer
    Failure,
    /// The operation is currently in progress or pending processing
    Pending,
    /// The operation was successfully processed
    Success,
    /// The result of the operation is unknown
    #[default]
    Unknown,
}

/// Indicates the type of action performed on the order.
#[derive(Debug, Deserialize, Default, PartialEq, Eq)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum TransactionType {
    /// Authentication
    Authentication,
    /// Authorization
    Authorization,
    /// Authorization Update
    AuthorizationUpdate,
    /// Capture
    Capture,
    /// Chargeback
    Chargeback,
    /// Disbursement
    Disbursement,
    /// The transaction transfers money to or from the merchant, without the involvement of a payer. For example, recording monthly merchant service fees from your payment service provider.
    Funding,
    /// Payment (Purchase)
    Payment,
    /// Refund
    Refund,
    /// Refund Request
    RefundRequest,
    /// Verification
    Verification,
    /// Void Authorization
    VoidAuthorization,
    /// Void Capture
    VoidCapture,
    /// Void Payment
    VoidPayment,
    /// Void Refund
    VoidRefund,
    /// Fallback for default implementation
    #[default]
    Undocumented,
}

/// Provides a recommendation for your next action based on the outcome of this transaction.
///
/// The recommendation may advise you that you:
/// - can proceed as planned.
/// - must not proceed. For example, because there is suspected fraud.
/// - can take action to obtain a successful Authorization. For example, by authenticating the payer, or asking the payer for updated or new payment details.
/// - must make a review decision.
#[derive(Debug, Deserialize, PartialEq, Eq, Default)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum GatewayRecommendation {
    /// Check the status of the transaction later. This will be presented if the gateway is in the process of retrieving the response from the acquirer.
    CheckTransactionStatusLater,
    /// The status of transaction processing is unknown. Do not submit the same request again and do not attempt to process other transactions under the same agreement. Please contact your payment services provider to find out how to proceed with this payment.
    ContactPaymentProvider,
    /// Do not proceed using this card. This will be presented if the gateway fails the request, but there is no apparent way for this transaction to succeed.
    DoNotProceed,
    /// Do not submit the same request again. The payment service provider, scheme or issuer require you to abandon the order.
    DoNotProceedAbandonOrder,
    /// Do not submit the same request again and do not attempt to process other transactions under the same agreement. The issuer has indicated that the payer has cancelled the payment agreement. Contact the payer.
    DoNotProceedContactPayer,
    /// No action is required because the transaction was either successful or is in progress.
    NoAction,
    /// Proceed with the next step in processing this payment. If the Initiate Authentication response indicated that payer authentication is available, proceed with authenticating the payer using the Authenticate Payer operation. If the Authenticate Payer operation indicates that the payer is sufficiently authenticated, proceed with submitting an Authorize or Pay request. If the Authorization for the payment was successful proceed with capturing the funds and if applicable, ship the goods.
    Proceed,
    /// Resubmit the same request later. The acquirer or issuer has indicated that retrying the same transaction is allowed and may be successful. Some acquirers and networks provide additional information on when to retry. Please check the field 'authorizationResponse.merchantAdviceCode' for more details.
    ResubmitLater,
    /// Attempt payer authentication and resubmit the request with the payer authentication details. Do not resubmit the same request.
    #[serde(rename = "RESUBMIT_WITH_3DS")]
    ResubmitWith3ds,
    /// Ask the payer for alternative payment details (e.g. a new card or another payment method) and resubmit the request with the new details. Do not resubmit the same request.
    ResubmitWithAlternativePaymentDetails,
    /// Attempt payer authentication and resubmit the request with the payer authentication details. Do not resubmit the same request.
    ResubmitWithPayerAuthentication,
    /// Ask the payer for a valid PIN. There is no need for the payer to tap on the terminal again. Please submit a new transaction request, providing the valid PIN and the original transaction Id in the "transaction.targetTransactionID" field.
    ResubmitWithPin,
    /// Ask the payer for the updated payment details and resubmit the request with the updated details. The acquirer, scheme or issuer has indicated that the provided payment details have changed, for example, the expiry date of the card has changed. For a merchant-initiated transaction you may want to request account update information using the functionality provided by the gateway. Do not resubmit the same request.
    ResubmitWithUpdatedPaymentDetails,
    /// The issuer has downgraded the payer authentication status for the transaction. You need to decide if you want to proceed with an unauthenticated transaction.
    ReviewAuthenticationResult,
    /// You must review the transaction and risk assessment details and make a decision about how to proceed with the transaction.
    ReviewRiskStatus,
    #[default]
    Undocumented,
}

impl GatewayRecommendation {
    pub fn do_not_proceed(&self) -> bool {
        match self {
            GatewayRecommendation::DoNotProceed
            | GatewayRecommendation::DoNotProceedAbandonOrder
            | GatewayRecommendation::DoNotProceedContactPayer => true,
            _ => false,
        }
    }
}

/// Indicates the result of payer authentication.
#[derive(Debug, Deserialize, Default, PartialEq, Eq)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum AuthenticationStatus {
    /// Payer authentication was attempted and a proof of authentication attempt was obtained.
    AuthenticationAttempted,
    /// Payer authentication is available for the payment method provided.
    AuthenticationAvailable,
    /// Exemption from the Regulatory Technical Standards (RTS) requirements for Strong Customer Authentication (SCA) under the Payment Services Directive 2 (PSD2) regulations in the European Economic Area has been claimed or granted.
    AuthenticationExempt,
    /// The payer was not authenticated. You should not proceed with this transaction.
    AuthenticationFailed,
    /// There is no authentication information associated with this transaction.
    AuthenticationNotInEffect,
    /// The requested authentication method is not supported for this payment method.
    AuthenticationNotSupported,
    /// Payer authentication is pending completion of a challenge process.
    AuthenticationPending,
    /// The issuer rejected the authentication request and requested that you do not attempt authorization of a payment.
    AuthenticationRejected,
    /// Payer authentication is required for this payment, but was not provided.
    AuthenticationRequired,
    /// The payer was successfully authenticated.
    AuthenticationSuccessful,
    /// The payer was not able to be authenticated due to a technical or other issue.
    AuthenticationUnavailable,
    #[default]
    Undocumented,
}
