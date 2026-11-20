from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    openrouter_api_key: str = ""
    openrouter_model: str = "deepseek/deepseek-v4.1-flash-1017"
    openrouter_metadata_model: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    llm_tools: bool = True
    max_tool_rounds: int = 6

    app_password: str = ""
    auth_disabled: bool = False
    secret_key: str = ""
    app_url: str = "http://localhost:8080"

    data_dir: Path = Path("/data")
    max_upload_mb: int = 100
    ocr_langs: str = "eng"
    preferred_langs: str = "en"
    strip_other_langs: bool = True
    backup_keep: int = 7
    catalog_inline_limit: int = 300
    start_worker: bool = True

    @property
    def metadata_model(self) -> str:
        return self.openrouter_metadata_model or self.openrouter_model

    @property
    def db_path(self) -> Path:
        return self.data_dir / "manualagerie.db"

    @property
    def files_dir(self) -> Path:
        return self.data_dir / "files"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def preferred_lang_list(self) -> list[str]:
        return [x.strip() for x in self.preferred_langs.split(",") if x.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
