use serde::{Deserialize, Serialize};

#[derive(Debug, Deserialize, Serialize)]
pub struct Masked;

pub trait MaskPolicy {
    fn mask(card: &str) -> String;
}

impl MaskPolicy for Masked {
    fn mask(card: &str) -> String {
        let len = card.len();
        if len > 10 {
            let masked_middle = "*".repeat(len - 10);
            format!("{}{}{}", &card[..6], masked_middle, &card[len - 4..])
        } else {
            card.to_string()
        }
    }
}

/// Return true if a key name likely holds a PAN/card number.
fn is_pan_key(key: &str) -> bool {
    let k = key.to_lowercase();
    matches!(k.as_str(), "pan")
        || k.contains("card") && (k.contains("number") || k.contains("num"))
        || k.contains("card_number")
        || k.contains("cardnumber")
        || k == "number"
        || k.contains("pan")
}

/// Return true if a key name likely holds a CVV/CVC.
fn is_cvv_key(key: &str) -> bool {
    let k = key.to_lowercase();
    k.contains("cvv")
        || k.contains("cvc")
        || k.contains("card_verification")
        || k.contains("cvn")
        || k == "securitycode"
        || k == "security_code"
}

pub fn secure_serializable(v: impl Serialize) -> serde_json::Value {
    let value = serde_json::to_value(v).expect("serialization is infallible");
    secure_value(&value)
}

pub fn secure_value(v: &serde_json::Value) -> serde_json::Value {
    use serde_json::Value;

    match v {
        Value::Object(map) => {
            let mut new = serde_json::Map::with_capacity(map.len());
            for (k, val) in map {
                let is_pan = is_pan_key(k);
                let is_cvv = is_cvv_key(k);
                let new_val = match val {
                    Value::String(s) if is_pan => Value::String(Masked::mask(s)),
                    Value::String(_) if is_cvv => Value::String("***".to_string()),
                    Value::Number(n) if is_pan => {
                        let s = n.to_string();
                        Value::String(Masked::mask(&s))
                    }
                    Value::Number(_) if is_cvv => Value::String("***".to_string()),
                    _ => secure_value(val),
                };
                new.insert(k.clone(), new_val);
            }
            Value::Object(new)
        }
        Value::Array(arr) => Value::Array(arr.iter().map(secure_value).collect()),
        // primitives that are not objects: leave them as-is
        other => other.clone(),
    }
}
