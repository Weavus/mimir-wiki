from __future__ import annotations

from mimir_wiki.config import load_config
from mimir_wiki.writers.onyx_markdown import apply_redaction


def test_redaction_patterns_cover_common_secret_shapes() -> None:
    config = load_config(
        cli_overrides={
            "redaction": {
                "enabled": True,
                "action": "redact",
                "patterns": [
                    "aws_access_key",
                    "github_token",
                    "slack_token",
                    "openai_key",
                    "azure_openai_key_assignment",
                    "generic_api_key_assignment",
                    "bearer_token",
                    "connection_string",
                    "private_key_block",
                    "jwt",
                    "password_assignment",
                ],
            }
        }
    )
    content = "\n".join(
        [
            "AKIA" + "ABCDEFGHIJKLMNOP",
            "ghp_" + "abcdefghijklmnopqrstuvwxyz123456",
            "xoxb-" + "123456789012-abcdefabcdefabcdef",
            "sk-" + "abcdefghijklmnopqrstuvwxyz123456",
            "AZURE_OPENAI_API_KEY=secret-value",
            "api_key=secret-value",
            "Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456",
            "Server=db.example;User Id=admin;Password=secret;Database=prod;",
            "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----",
            "eyJaaaaaaaaaa.bbbbbbbbbbbbb.cccccccccccc",
            "password=secret-value",
        ]
    )
    redacted, warnings = apply_redaction(content, config)
    assert "[REDACTED]" in redacted
    assert "secret-value" not in redacted
    assert len(warnings) >= 10
