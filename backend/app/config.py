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
    # Fristwächter für Vorschläge (HED-45, Entwurf 7.1 Durchsetzung Punkt 1). Verbindlich
    # ist Stufe-2-Schritt-2.3 (HED-44), nicht dieser Sweep -- siehe
    # app.trading.proposals.verfalle_ueberfaellige_vorschlaege.
    check_proposal_expiry_on_startup: bool = True
    lock_file: str = "./data/backend.lock"
    frontend_dist: str = "../frontend/dist"
    # Auflage 1/4 Startgatter (HED-36 Abschnitt 3.1, 3.4). Default AUS: diese Plattform
    # hat auch Deployments ohne jeden Bezug zu Hedgeclip (siehe docs/hostinger.md,
    # docs/proxmox.md), die BETRIEBSART/ARTEFAKT_ART nie gesetzt haben und sonst nicht
    # mehr starten würden. Jede Hedgeclip-Installation (papier UND live) MUSS dies
    # explizit auf True setzen -- siehe Issue-Kommentar HED-42 zur offenen Rückfrage,
    # ob dieser Default so richtig ist.
    enforce_startgatter: bool = False
    # B-2 Stufe 1/2 (HED-44): welche Tranche aus Risikopolitik Abschnitt 4 (HED-8) als
    # freigegeben gilt -- eine Board-Entscheidung (HED-8 Abschnitt 9.1), keine
    # Risikozahl. Default B1: die kleinste, strengste Tranche, passend zum Start von
    # Gate B. Eine Freischaltung von B2/B3 ist ein eigener Board-Beschluss, kein
    # Konfigurations-Default.
    freigegebene_tranche: str = "B1"

    def chains(self) -> dict[str, list[str]]:
        return {
            cls: [s.strip() for s in getattr(self, f"chain_{cls}").split(",") if s.strip()]
            for cls in ASSET_CLASSES
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()
