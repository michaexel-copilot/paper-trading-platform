from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

ASSET_CLASSES = ("crypto", "stocks", "etfs", "commodities", "forex")
BASE_CURRENCIES = ("EUR", "USD")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite+aiosqlite:///./data/app.db"

    # Ordered source chains per asset class, comma separated.
    chain_crypto: str = "okx"
    chain_stocks: str = "yahoo"
    chain_etfs: str = "yahoo"
    chain_commodities: str = "yahoo"
    chain_forex: str = "yahoo"

    cookie_secure: bool = False
    auto_migrate: bool = True
    run_matcher: bool = True
    check_assets_on_startup: bool = True
    lock_file: str = "./data/backend.lock"
    frontend_dist: str = "../frontend/dist"

    def chains(self) -> dict[str, list[str]]:
        return {
            cls: [s.strip() for s in getattr(self, f"chain_{cls}").split(",") if s.strip()]
            for cls in ASSET_CLASSES
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()
