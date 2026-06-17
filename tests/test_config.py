from __future__ import annotations

from pathlib import Path

from mimir_wiki.config import apply_runtime_overrides, load_config
from mimir_wiki.llm.base import ChatCompletionProvider, provider_for_config


def test_config_precedence_and_secret_redaction(tmp_path: Path, monkeypatch) -> None:
    config_file = tmp_path / "mimir-wiki.yaml"
    config_file.write_text(
        """
paths:
  knowledge: from-file
llm:
  provider: azure-openai
  openai:
    api_key_env: SHOULD_NOT_LEAK
profiles:
  test:
    paths:
      knowledge: from-profile
""",
        encoding="utf-8",
    )
    env_file = tmp_path / ".env"
    env_file.write_text("MIMIR_WIKI_KNOWLEDGE_PATH=from-dotenv\n", encoding="utf-8")
    monkeypatch.setenv("MIMIR_WIKI_KNOWLEDGE_PATH", "from-env")
    overrides = apply_runtime_overrides(out=Path("from-cli"), provider="none")
    config = load_config(
        config_path=config_file, profile="test", cli_overrides=overrides, env_file=env_file
    )
    assert config.paths.knowledge == "from-cli"
    assert config.llm.provider == "none"
    assert config.features.llm.enabled is False
    resolved = config.non_secret_dict()
    assert resolved["llm"]["openai"]["api_key_env"] == "[REDACTED]"


def test_example_config_loads() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    config = load_config(config_path=repo_root / "mimir-wiki.yaml.example")
    assert config.project_name == "mimir-wiki"
    assert config.llm.provider == "none"
    assert config.features.llm.enabled is False
    assert config.paths.knowledge == "./knowledge"


def test_example_azure_profile_loads() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    config = load_config(config_path=repo_root / "mimir-wiki.yaml.example", profile="azure-openai")
    assert config.llm.provider == "azure-openai"
    assert config.features.llm.enabled is True
    assert config.llm.route_for("summary").provider == "azure-openai"


def test_dotenv_sources_provider_credentials(tmp_path: Path, monkeypatch) -> None:
    config_file = tmp_path / "mimir-wiki.yaml"
    config_file.write_text(
        """
llm:
  provider: azure-openai
  model: test-deployment
  azure_openai:
    endpoint_env: TEST_AZURE_OPENAI_ENDPOINT
    api_key_env: TEST_AZURE_OPENAI_API_KEY
    deployment_env: TEST_AZURE_OPENAI_DEPLOYMENT
    api_version_env: TEST_AZURE_OPENAI_API_VERSION
features:
  llm:
    enabled: true
""",
        encoding="utf-8",
    )
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "TEST_AZURE_OPENAI_ENDPOINT=https://example.openai.azure.com",
                "TEST_AZURE_OPENAI_API_KEY=from-dotenv",
                "TEST_AZURE_OPENAI_DEPLOYMENT=dotenv-deployment",
                "TEST_AZURE_OPENAI_API_VERSION=2025-01-01-preview",
            ]
        ),
        encoding="utf-8",
    )
    for name in (
        "TEST_AZURE_OPENAI_ENDPOINT",
        "TEST_AZURE_OPENAI_API_KEY",
        "TEST_AZURE_OPENAI_DEPLOYMENT",
        "TEST_AZURE_OPENAI_API_VERSION",
    ):
        monkeypatch.delenv(name, raising=False)

    config = load_config(config_path=config_file, env_file=env_file)
    provider = provider_for_config(config)

    assert isinstance(provider, ChatCompletionProvider)
    assert "dotenv-deployment" in provider.url
    assert provider.headers["api-key"] == "from-dotenv"


def test_real_environment_overrides_dotenv_for_provider_credentials(
    tmp_path: Path, monkeypatch
) -> None:
    config_file = tmp_path / "mimir-wiki.yaml"
    config_file.write_text(
        """
llm:
  provider: openai-compatible
  model: file-model
  openai_compatible:
    base_url_env: TEST_COMPAT_BASE_URL
    api_key_env: TEST_COMPAT_API_KEY
    model_env: TEST_COMPAT_MODEL
features:
  llm:
    enabled: true
""",
        encoding="utf-8",
    )
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "TEST_COMPAT_BASE_URL=https://dotenv.example/v1",
                "TEST_COMPAT_API_KEY=dotenv-key",
                "TEST_COMPAT_MODEL=dotenv-model",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TEST_COMPAT_API_KEY", "real-env-key")

    config = load_config(config_path=config_file, env_file=env_file)
    provider = provider_for_config(config)

    assert isinstance(provider, ChatCompletionProvider)
    assert provider.headers["Authorization"] == "Bearer real-env-key"
    assert provider.default_model == "dotenv-model"


def test_azure_ai_foundry_provider_accepts_full_chat_completions_endpoint(
    tmp_path: Path, monkeypatch
) -> None:
    config_file = tmp_path / "mimir-wiki.yaml"
    config_file.write_text(
        """
llm:
  provider: azure-ai-foundry
  model: foundry-model
  azure_ai_foundry:
    endpoint_env: TEST_FOUNDRY_ENDPOINT
    api_key_env: TEST_FOUNDRY_API_KEY
    deployment_env: TEST_FOUNDRY_DEPLOYMENT
features:
  llm:
    enabled: true
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("TEST_FOUNDRY_ENDPOINT", "https://foundry.example/models/chat/completions")
    monkeypatch.setenv("TEST_FOUNDRY_API_KEY", "foundry-key")
    monkeypatch.setenv("TEST_FOUNDRY_DEPLOYMENT", "foundry-deployment")
    config = load_config(config_path=config_file)
    provider = provider_for_config(config)
    assert isinstance(provider, ChatCompletionProvider)
    assert provider.url == "https://foundry.example/models/chat/completions"
    assert provider.headers["api-key"] == "foundry-key"
    assert provider.default_model == "foundry-deployment"
