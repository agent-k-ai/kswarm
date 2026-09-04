//! The deterministic JSON encoding the branch canonical hash is taken over.
//!
//! The Python producer is `canonical_json_bytes` in
//! `backend/app/protocol/canonical_hash.py`:
//!
//! ```python
//! json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
//! ```
//!
//! `serde_json` reproduces every one of those choices on a `Value` whose objects are
//! `BTreeMap`s: keys come out in byte order (which is code-point order for UTF-8, the
//! order Python's `sort_keys` uses), there is no whitespace, non-ASCII characters are
//! written raw, and the escape set is the same -- the quote, the backslash, and the
//! C0 controls, with short forms for backspace, tab, newline, form feed and carriage
//! return and a four-digit escape for the rest.
//!
//! One rule is added here rather than inherited: a floating-point number is refused.
//! Python writes a float with `repr` and Rust writes it with Ryu, and the two agree
//! for every value they both round-trip, but nothing in the branch schema is a float
//! and a hash rule should not rest on that agreement. Every number in a branch output
//! is an integer, so a float in the document means the document is not the one this
//! rule was written for.

use alloc::vec::Vec;
use serde_json::Value;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CanonicalJsonError {
    /// A floating-point number reached the canonicalizer.
    FloatNotCanonical,
    /// `serde_json` could not serialize the value.
    Serialize,
}

impl core::fmt::Display for CanonicalJsonError {
    fn fmt(&self, formatter: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        write!(formatter, "{self:?}")
    }
}

#[cfg(feature = "std")]
impl std::error::Error for CanonicalJsonError {}

/// Canonical JSON bytes, or an error when the document is not one this rule covers.
pub fn canonical_json_bytes(value: &Value) -> Result<Vec<u8>, CanonicalJsonError> {
    reject_floats(value)?;
    serde_json::to_vec(value).map_err(|_| CanonicalJsonError::Serialize)
}

fn reject_floats(value: &Value) -> Result<(), CanonicalJsonError> {
    match value {
        Value::Number(number) => {
            if number.is_f64() {
                Err(CanonicalJsonError::FloatNotCanonical)
            } else {
                Ok(())
            }
        }
        Value::Array(items) => items.iter().try_for_each(reject_floats),
        Value::Object(entries) => entries.values().try_for_each(reject_floats),
        _ => Ok(()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use alloc::string::String;

    fn canonical(text: &str) -> String {
        let value: Value = serde_json::from_str(text).unwrap();
        String::from_utf8(canonical_json_bytes(&value).unwrap()).unwrap()
    }

    #[test]
    fn sorts_keys_and_drops_whitespace() {
        assert_eq!(
            canonical(r#"{ "b": 1, "a": {"d": 2, "c": 3} }"#),
            r#"{"a":{"c":3,"d":2},"b":1}"#
        );
    }

    #[test]
    fn keeps_null_and_writes_non_ascii_raw() {
        assert_eq!(canonical(r#"{"a": null}"#), r#"{"a":null}"#);
        let accented = "\u{e9}";
        let input = alloc::format!("{{\"a\": \"{accented}\"}}");
        assert_eq!(canonical(&input), alloc::format!("{{\"a\":\"{accented}\"}}"));
    }

    #[test]
    fn escapes_exactly_what_python_escapes() {
        assert_eq!(canonical(r#"{"a":"x\"y\\z"}"#), r#"{"a":"x\"y\\z"}"#);
        assert_eq!(canonical(r#"{"a":"\n\t\r\b\f"}"#), r#"{"a":"\n\t\r\b\f"}"#);
        // A C0 control with no short form: both producers write a four-digit escape
        // with lowercase hex digits.
        assert_eq!(canonical(r#"{"a":"\u001f"}"#), r#"{"a":"\u001f"}"#);
        assert_eq!(canonical(r#"{"a":"\u0000"}"#), r#"{"a":"\u0000"}"#);
        // Escaped by neither producer.
        assert_eq!(canonical(r#"{"a":"/"}"#), r#"{"a":"/"}"#);
        assert_eq!(canonical(r#"{"a":""}"#), r#"{"a":""}"#);
    }

    #[test]
    fn refuses_a_float() {
        let value: Value = serde_json::from_str(r#"{"a": 1.5}"#).unwrap();
        assert_eq!(
            canonical_json_bytes(&value),
            Err(CanonicalJsonError::FloatNotCanonical)
        );
    }
}
