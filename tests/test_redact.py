"""Tests for swe_agent.redact."""
from swe_agent.redact import redact, contains_secret, _shannon_entropy


def test_redact_openai_key():
    text = "My key is sk-abcdefghijklmnopqrstuvwxyz1234"
    result = redact(text, check_entropy=False)
    assert "sk-abcdefghijklmnopqrstuvwxyz1234" not in result
    assert "[REDACTED]" in result


def test_redact_github_pat():
    text = "token: ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij"
    result = redact(text, check_entropy=False)
    assert "ghp_" not in result
    assert "[REDACTED]" in result


def test_redact_aws_access_key():
    text = "aws_access_key_id = AKIAIOSFODNN7EXAMPLE"
    result = redact(text, check_entropy=False)
    assert "AKIAIOSFODNN7EXAMPLE" not in result
    assert "[REDACTED]" in result


def test_redact_generic_api_key():
    text = 'api_key = "sk_test_1234567890abcdefgh"'
    result = redact(text, check_entropy=False)
    assert "1234567890abcdefgh" not in result
    assert "[REDACTED]" in result


def test_redact_bearer_token():
    text = "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5c"
    result = redact(text, check_entropy=False)
    assert "eyJhbGciOiJIUzI1NiIsInR5c" not in result
    assert "[REDACTED]" in result


def test_redact_preserves_normal_text():
    text = "This is a normal log message with no secrets."
    result = redact(text, check_entropy=False)
    assert result == text


def test_redact_private_key_header():
    text = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAK..."
    result = redact(text, check_entropy=False)
    assert "-----BEGIN RSA PRIVATE KEY-----" not in result
    assert "[REDACTED]" in result


def test_contains_secret_positive():
    assert contains_secret("token: ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij")
    assert contains_secret("sk-abcdefghijklmnopqrstuvwxyz1234")


def test_contains_secret_negative():
    assert not contains_secret("just a normal string")
    assert not contains_secret("")


def test_entropy_check():
    """High entropy strings should be detected."""
    # Random-looking string
    high_entropy = "aB3xK9mNpQ2rS5tU7vW0yZ1cE4fG6hJ"
    assert _shannon_entropy(high_entropy) > 4.0
    # Low entropy string
    low_entropy = "aaaaaaaaaaaaaaaaaaaaaa"
    assert _shannon_entropy(low_entropy) < 1.0


def test_redact_with_entropy():
    """Entropy-based redaction catches unknown-format secrets."""
    # A high-entropy token that doesn't match any known pattern
    text = "secret_val=Kj8mNpQ2rS5tU7vW0yZ1cE4fG6hJx9aB3"
    result = redact(text, check_entropy=True)
    # The high-entropy part should be redacted
    assert "Kj8mNpQ2rS5tU7vW0yZ1cE4fG6hJx9aB3" not in result


def test_redact_slack_token():
    text = "SLACK_TOKEN=xoxb-not-a-real-token-just-testing-pattern"
    result = redact(text, check_entropy=False)
    assert "xoxb-" not in result
    assert "[REDACTED]" in result
