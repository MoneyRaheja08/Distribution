from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    mongo_uri: str = "mongodb://localhost:27017"
    db_name: str = "ashoka_distribution"
    jwt_secret: str = "dev-secret-change-me"
    jwt_expire_minutes: int = 60 * 24 * 7  # one week
    cors_origins: str = "*"  # comma-separated, or "*"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
