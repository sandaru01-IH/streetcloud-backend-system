from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = "local"
    database_url: str = "sqlite+aiosqlite:///./data/streetcloud.db"
    redis_url: str = ""
    storage_backend: str = "local"
    storage_local_dir: str = "./data/objects"
    storage_endpoint: str = ""
    storage_bucket: str = "streetcloud-private"
    storage_access_key: str = ""
    storage_secret_key: str = ""
    storage_region: str = "us-east-1"
    jwt_secret: str = "dev-only-change-me-please-use-a-long-random-string"
    access_token_minutes: int = 30
    refresh_token_days: int = 14
    public_base_url: str = "http://127.0.0.1:8000"
    openai_api_key: str = ""
    openai_image_model: str = ""
    openai_vision_model: str = ""
    google_api_key: str = ""
    gemini_image_model: str = ""
    gemini_vision_model: str = ""
    gemini_image_api: str = "auto"
    hosted_gpu_endpoint: str = ""
    hosted_gpu_api_key: str = ""
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_vision_model: str = ""
    comfy_url: str = ""
    max_upload_mb: int = 20
    max_megapixels: int = 40
    embed_worker: bool = True
    auto_create_tables: bool = True
    cors_origins: str = "*"
    log_level: str = "INFO"
    retention_days: int = 90
    max_concurrent_jobs_per_user: int = 2
    pairing_ttl_seconds: int = 600
    lease_ttl_seconds: int = 300
    media_url_ttl_seconds: int = 900
    worker_offline_seconds: int = 45

    @property
    def openai_image_ready(self) -> bool:
        return bool(self.openai_api_key and self.openai_image_model)

    @property
    def gemini_image_ready(self) -> bool:
        return bool(self.google_api_key and self.gemini_image_model)

    @property
    def openai_vision_ready(self) -> bool:
        return bool(self.openai_api_key and self.openai_vision_model)

    @property
    def gemini_vision_ready(self) -> bool:
        return bool(self.google_api_key and self.gemini_vision_model)

    @property
    def hosted_gpu_ready(self) -> bool:
        return bool(self.hosted_gpu_endpoint)

    @property
    def ollama_vision_ready(self) -> bool:
        return bool(self.ollama_base_url and self.ollama_vision_model)


@lru_cache
def get_settings() -> Settings:
    return Settings()


def assert_runtime_settings(settings: Settings) -> None:
    if settings.environment == "production" and settings.jwt_secret.startswith("dev-only"):
        raise RuntimeError("Refusing to start production with the default JWT secret.")
