"""Чистка текста от секретов и личного до того, как он попадёт в базу.

Это первая линия. Вторая: библиотекарь, он ещё обобщает имена и пути.
Но на модель одну не надеемся: регулярки срабатывают всегда, даже когда
библиотекарь выключен.
"""

import re

# (вид, шаблон, на что заменить)
_RULES: list[tuple[str, re.Pattern, str]] = [
    ("private_key", re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
     "<private-key>"),
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}"), "<anthropic-key>"),
    ("openai_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{20,}"), "<api-key>"),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"), "<github-token>"),
    ("aws_key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), "<aws-key>"),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"), "<slack-token>"),
    ("google_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), "<google-key>"),
    ("stripe_key", re.compile(r"\b[rs]k_(?:live|test)_[0-9A-Za-z]{16,}"), "<stripe-key>"),
    ("telegram_bot_token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_\-]{35}\b"), "<telegram-bot-token>"),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"), "<jwt>"),
    ("bearer", re.compile(r"(?i)(authorization:\s*bearer\s+)[A-Za-z0-9._\-~+/=]{12,}"), r"\1<token>"),
    ("url_credentials", re.compile(r"([a-z][a-z0-9+.\-]*://)[^/\s:@]+:[^/\s@]+@", re.I), r"\1<user>:<password>@"),
    ("assigned_secret", re.compile(
        r"(?i)\b([A-Z0-9_]*(?:PASSWORD|PASSWD|SECRET|TOKEN|API_?KEY|PRIVATE_KEY)[A-Z0-9_]*\s*[=:]\s*)"
        r"([\"']?)(?!<)[^\s\"']{6,}\2"),
     r"\1<secret>"),
    ("email", re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"), "<email>"),
    ("home_path", re.compile(r"(?:/home/|/Users/)[^/\s]+"), "~"),
    ("home_path", re.compile(r"(?i)C:\\Users\\[^\\\s]+"), "~"),
    ("ip", re.compile(
        r"(?<![\w.])(?!127\.0\.0\.1\b)(?!0\.0\.0\.0\b)"
        r"(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)(?![\w.])"),
     "<ip>"),
]


def scrub(text: str) -> tuple[str, list[str]]:
    """Вернёт чистый текст и список того, что вырезали (без самих значений)."""
    if not text:
        return text, []
    found: list[str] = []
    for kind, pattern, repl in _RULES:
        text, n = pattern.subn(repl, text)
        if n and kind not in found:
            found.append(kind)
    return text, found


def scrub_many(*parts: str) -> tuple[list[str], list[str]]:
    out, found = [], []
    for p in parts:
        clean, f = scrub(p or "")
        out.append(clean)
        found += [k for k in f if k not in found]
    return out, found
