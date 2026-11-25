import pytest

from agent_sync.privacy import secret_values, sensitive


@pytest.mark.parametrize(("value", "kind"), [
    ("ghp_" + "A1" * 18, "GitHub token"),
    ("github_pat_" + "A1_" * 20, "GitHub personal access token"),
    ("sk-" + "A1" * 10, "OpenAI/Anthropic API key"),
    ("sk-ant-" + "A_" * 10, "OpenAI/Anthropic API key"),
    ("sk-proj-" + "A-" * 10, "OpenAI/Anthropic API key"),
    ("AKIA" + "A1" * 8, "AWS access key"),
    ("xoxb-" + "A1" * 5, "Slack token"),
    ("AIza" + "A" * 35, "Google API key"),
    ("-----BEGIN RSA PRIVATE KEY-----", "private key"),
    ("eyJ" + "a" * 10 + "." + "a" * 10 + ".sig", "JWT"),
])
def test_recognizes_credentials_without_returning_the_value(value, kind):
    assert secret_values(f"Use {value} for this account.") == [kind]
    assert value not in kind


@pytest.mark.parametrize("key", ["password", "passwd", "passphrase", "secret", "token", "api_key",
                                 "access-key", "client_secret"])
def test_recognizes_assignments_without_returning_the_value(key):
    assert secret_values(f'\"{key.upper()}\": "s3cr3tValue"') == [f"{key} assignment"]
    assert secret_values(f'SERVICE_{key.upper()}="s3cr3tValue"') == [f"{key} assignment"]


@pytest.mark.parametrize("value", [
    "<replacement-value>", "$PASSWORD", "${PASSWORD}", "~/path/to/key", "/path/to/key",
    "./path/to/key", "***", "********", "xxxx", "xxxxxxxxxx", "example", "changeme", "`$PASSWORD`",
])
def test_placeholders_and_references_are_not_credentials(value):
    assert secret_values(f"password: {value}") == []


@pytest.mark.parametrize(("text", "reason"), [
    ("password", "mentions a password"),
    ("passphrase", "mentions a passphrase"),
    ("credentials", "mentions credentials"),
    ("personal access token", "mentions a token"),
    ("PAT", "mentions a token"),
    ("API key", "mentions an API key"),
    ("secret key", "mentions a secret key"),
    ("private key", "mentions a private key"),
    ("keychain item", "mentions a keychain item"),
    ("2FA", "mentions a sign-in code"),
    ("one-time code", "mentions a sign-in code"),
    ("recovery code", "mentions a recovery code"),
    ("SSN", "mentions social security"),
    ("social security", "mentions social security"),
    ("legal name", "mentions a legal name"),
    ("home address", "mentions a home address"),
    ("phone number", "mentions a phone number"),
    ("date of birth", "mentions a date of birth"),
    ("bank", "mentions banking details"),
    ("IBAN", "mentions banking details"),
    ("credit card", "mentions banking details"),
])
def test_sensitive_topics_are_case_insensitive(text, reason):
    assert sensitive(text.upper()) == [reason]


def test_sensitive_topics_respect_boundaries_and_token_context():
    assert sensitive("Compatible with token accounting and tokenized workloads.") == []
