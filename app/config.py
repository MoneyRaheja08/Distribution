import os

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    mongo_uri: str = "mongodb://localhost:27017"
    db_name: str = "ashoka_distribution"
    jwt_secret: str = "dev-secret-change-me"
    jwt_expire_minutes: int = 60 * 24 * 7  # one week
    cors_origins: str = "*"  # comma-separated, or "*"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


DEFAULT_SECRET = "dev-secret-change-me"
settings = Settings()


def check_production():
    """On Render: never run with the built-in secret (anyone could forge a login with it)."""
    if not os.environ.get("RENDER"):
        return
    if settings.jwt_secret == DEFAULT_SECRET:
        raise RuntimeError("JWT_SECRET is not set on Render — add a long random value in Environment and redeploy")
    if len(settings.jwt_secret) < 32:
        print("WARNING: JWT_SECRET is shorter than 32 characters — use a longer random value")
    if settings.cors_origins.strip() == "*":
        print("WARNING: CORS_ORIGINS is * — set it to your Vercel address")
