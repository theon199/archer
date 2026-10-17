"""Configuration settings for Archer Core."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # App
    env: str = "development"
    host: str = "127.0.0.1"
    port: int = 8000
    secret_key: str = "dev-secret-key-archer-dgx-spark"
    data_dir: Path = Path(__file__).resolve().parent.parent.parent / "data"

    # SQLite Database
    @property
    def database_path(self) -> Path:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return self.data_dir / "archer.db"

    # Model Provider Selection: "local-ollama" | "local-sglang" | "anthropic" | "gemini" | "openai"
    model_provider: str = "local-ollama"

    # Local Ollama on Spark
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "gemma4:26b"

    # Local SGLang Router on Spark
    sglang_base_url: str = "http://localhost:8080/v1"
    sglang_model: str = "qwen3.8-27b"

    # Cloud Providers (Optional)
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-3-7-sonnet-20250219"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o"
    deepseek_api_key: str = ""
    deepseek_model: str = "deepseek-chat"

    # Plugins
    brave_search_api_key: str = ""
    approved_files_root: Path = Path(__file__).resolve().parent.parent.parent / "data" / "approved_files"

    model_config = SettingsConfigDict(
        env_file=str(Path(__file__).resolve().parent.parent.parent / ".env"), env_prefix="ARCHER_", extra="ignore"
    )


settings = Settings()
