"""Identify credential values and sensitive topics without returning matched text."""
from __future__ import annotations

import re

PRIVATE_KEY = r"-----BEGIN [A-Z ]*PRIVATE KEY-----"
PRIVATE_KEY_END = re.compile(r"-----END [A-Z ]*PRIVATE KEY-----")
SECRET_PATTERNS = tuple((kind, re.compile(pattern)) for kind, pattern in (
    ("GitHub token", r"gh[pousr]_[A-Za-z0-9]{36,}"),
    ("GitHub personal access token", r"github_pat_[A-Za-z0-9_]{60,}"),
    ("OpenAI/Anthropic API key", r"sk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}"),
    ("AWS access key", r"AKIA[0-9A-Z]{16}"),
    ("Slack token", r"xox[abprs]-[A-Za-z0-9-]{10,}"),
    ("Google API key", r"AIza[0-9A-Za-z_-]{35}"),
    ("private key", PRIVATE_KEY),
    ("JWT", r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\."),
))
ASSIGNMENT = re.compile(
    r"""(?<![A-Z0-9])(password|passwd|passphrase|secret|token|api[_-]?key|access[_-]?key|client[_-]?secret)"""
    r"""["']?\s*[:=]\s*["']?([^\s"'<>]{8,})""",
    re.I,
)
PLACEHOLDER = re.compile(r"\*+|x+|example|changeme", re.I)

SENSITIVE_PATTERNS = tuple((reason, re.compile(rf"\b(?:{pattern})\b", re.I)) for reason, pattern in (
    ("mentions a password", r"passwords?|passwd"),
    ("mentions a passphrase", r"passphrases?"),
    ("mentions credentials", r"credentials?"),
    ("mentions a token", r"(?:access|api|auth(?:entication|orization)?|personal|pat)[ _-]+tokens?|PATs?"),
    ("mentions an API key", r"api[ _-]+keys?"),
    ("mentions a secret key", r"secret[ _-]+keys?|client[ _-]+secrets?"),
    ("mentions a private key", r"private[ _-]+keys?"),
    ("mentions a keychain item", r"keychain[ _-]+items?"),
    ("mentions a sign-in code", r"2fa|one[ _-]+time[ _-]+codes?"),
    ("mentions a recovery code", r"recovery[ _-]+codes?"),
    ("mentions social security", r"ssn|social[ _-]+security"),
    ("mentions a legal name", r"legal[ _-]+names?"),
    ("mentions a home address", r"home[ _-]+address(?:es)?"),
    ("mentions a phone number", r"phone[ _-]+numbers?"),
    ("mentions a date of birth", r"date[ _-]+of[ _-]+birth"),
    ("mentions banking details", r"banks?|iban|credit[ _-]+cards?"),
))


def secret_values(text: str) -> list[str]:
    kinds = [kind for kind, pattern in SECRET_PATTERNS if pattern.search(text)]
    for key, value in ASSIGNMENT.findall(text):
        value = value.lstrip("`")
        if value.startswith(("$", "~/", "/", "./", "../")) or PLACEHOLDER.fullmatch(value.rstrip("`.,;)]}")):
            continue
        kinds.append(f"{key.lower()} assignment")
    return list(dict.fromkeys(kinds))


def secret_rows(lines: list[str]) -> dict[int, list[str]]:
    rows = {}
    in_key = False
    for index, line in enumerate(lines):
        kinds = secret_values(line)
        in_key = in_key or "private key" in kinds
        if in_key and "private key" not in kinds:
            kinds.append("private key")
        if kinds:
            rows[index] = kinds
        if in_key and PRIVATE_KEY_END.search(line):
            in_key = False
    return rows


def sensitive(text: str) -> list[str]:
    return [reason for reason, pattern in SENSITIVE_PATTERNS if pattern.search(text)]
