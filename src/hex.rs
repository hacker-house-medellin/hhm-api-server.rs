//! Lower-case hexadecimal encoding for digest output.
//!
//! `sha2` 0.11 returns `hybrid_array::Array` digests, which do not implement
//! `LowerHex`, so the former `format!("{:x}", digest)` idiom no longer
//! compiles. Every persisted and compared digest in this crate (canonical
//! request digests and streamed upload verification) is lower-case hex, so the
//! encoding lives here once instead of pulling in another dependency.

const LOWER_HEX_DIGITS: &[u8; 16] = b"0123456789abcdef";

/// Encodes `bytes` as lower-case hexadecimal, two characters per byte.
pub(crate) fn lower_hex(bytes: &[u8]) -> String {
    let mut encoded = String::with_capacity(bytes.len() * 2);
    for &byte in bytes {
        encoded.push(char::from(LOWER_HEX_DIGITS[usize::from(byte >> 4)]));
        encoded.push(char::from(LOWER_HEX_DIGITS[usize::from(byte & 0x0f)]));
    }
    encoded
}

#[cfg(test)]
mod tests {
    use sha2::{Digest, Sha256};

    use super::lower_hex;

    #[test]
    fn empty_input_encodes_to_empty_string() {
        assert_eq!(lower_hex(&[]), "");
    }

    #[test]
    fn every_byte_value_encodes_to_two_lower_case_digits() {
        assert_eq!(lower_hex(&[0x00, 0x0f, 0xa5, 0xff]), "000fa5ff");
        let all: Vec<u8> = (0..=u8::MAX).collect();
        let encoded = lower_hex(&all);
        assert_eq!(encoded.len(), 512);
        for (byte, pair) in all.iter().zip(encoded.as_bytes().chunks(2)) {
            assert_eq!(pair, format!("{byte:02x}").as_bytes());
        }
    }

    #[test]
    fn sha256_known_vectors_keep_the_lower_hex_wire_format() {
        assert_eq!(
            lower_hex(&Sha256::digest(b"")),
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        );
        assert_eq!(
            lower_hex(&Sha256::digest(b"abc")),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
    }

    #[test]
    fn streaming_and_one_shot_digests_encode_identically() {
        let mut streaming = Sha256::new();
        streaming.update(b"a");
        streaming.update(b"bc");
        let streamed = lower_hex(&streaming.finalize());
        assert_eq!(streamed, lower_hex(&Sha256::digest(b"abc")));
        assert_eq!(streamed.len(), 64);
    }
}
