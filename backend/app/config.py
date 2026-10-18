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

    # HED-55 (Entwurf HED-36 Abschnitt 3.2, "Laufende Prüfung, nicht nur beim
    # Start"): derselbe Selbsttest wie beim Start läuft zusätzlich in festem
    # Intervall während des Betriebs. Der Entwurf legt keine Zahl fest ("festes
    # Intervall"); 5 Minuten ist ein Kompromiss zwischen Erkennungsgeschwindigkeit
    # und Last auf dem Kontostand-Endpunkt (reiner Lesevorgang, kein Handel). Nur
    # wirksam, wenn enforce_startgatter gesetzt ist -- ohne Betriebsart ist nichts
    # zu prüfen.
    schluesseltrennung_laufend_pruefen: bool = True
    schluesseltrennung_intervall_sekunden: int = 300

    # OKX-Demo-Hostname (HED-87, Nachtrag zu B-6/HED-76): der OKX-Demo-Schlüssel des
    # Boards gehört zu einem Konto der OKX-EWR-Einheit und existiert ausschließlich auf
    # eea.okx.com (Fehlercode 50119 "API key doesn't exist" auf www.okx.com/app.okx.com,
    # Handtest Board 2026-10-13). Konfigurierbar statt fest verdrahtet, falls künftige
    # Demo-Schlüssel zu einer anderen OKX-Einheit gehören.
    okx_demo_hostname: str = "eea.okx.com"

    # HED-119: on-disk cache for historical OHLCV used by backtests. Separate
    # from database_url on purpose -- a backtest data path has no business
    # near the trading database (live or paper).
    backtest_cache_dir: str = "./data/backtest_cache"

    # Board-Benachrichtigung (HED-43, Entwurf 5.3): E-Mail an die Board-Verteilerliste je
    # Vorschlag in Z3. Nur Python-Standardbibliothek (smtplib) -- bewusst kein
    # Drittanbieter-Versanddienst, um die Prüfkarte aus Anhang A zu vermeiden (Entwurf
    # 5.3.1). Leere board_notify_emails deaktiviert den Versand nicht; ein Versuch ohne
    # Empfänger gilt als Fehler und zählt in den Wiederholungs-/Fehlerpfad.
    smtp_host: str = "localhost"
    smtp_port: int = 25
    smtp_starttls: bool = False
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str = "dashboard@hedgeclip.local"
    board_notify_emails: str = ""
    cro_alert_email: str = ""
    dashboard_base_url: str = ""
    notify_poll_seconds: int = 15

    # B-12 (HED-84, CRO-Abnahme HED-104 Fassung 1.0 "angenommen mit Nachforderungen für
    # Fassung 1.1"): Mechanismus und Eskalationslogik sind freigegeben, die konkreten
    # Zahlen sind Risikoparameter und noch kein Board-Beschluss (E-3/E-8/E-9 bleiben
    # offen). Die drei Werte unten sind die im Entwurf benannten Standardannahmen --
    # NICHT fest verdrahtet, sondern hier als Settings, damit sie ohne Code-Änderung
    # durch den anstehenden CRO-Vorschlag ersetzt werden können, sobald er vorliegt.
    versand_treiber_intervall_sekunden: int = 15
    stop_pflicht_intervall_sekunden: int = 15
    # E-3: Pflicht-Stop-Nachfrist, danach Kill-Switch Stufe 2
    # (`pflicht_stop_nicht_platziert`, Entwurf Abschnitt 7.5).
    pflicht_stop_nachfrist_sekunden: int = 120
    # E-8: Mengen-Rundungstoleranz beim täglichen Abgleich, als Anteil (0.001 = 0,1 %)
    # der Fill-Größe (Entwurf Abschnitt 10.3).
    stop_mengen_rundungstoleranz: str = "0.001"
    stop_orderbuch_abgleich_intervall_sekunden: int = 24 * 3600
    # E-9: Totmannfrist des täglichen Abgleichs (Entwurf Abschnitt 10.6).
    stop_orderbuch_totmannfrist_sekunden: int = 26 * 3600
    # HED-147 (Entwurf Abschnitt 4.6): gleiches Intervall wie die Einstiegs-
    # Fill-Erkennung -- ein Exit ist nicht weniger dringend zu erkennen als ein
    # Einstieg.
    exit_fill_intervall_sekunden: int = 15

    def board_notify_email_list(self) -> list[str]:
        return [addr.strip() for addr in self.board_notify_emails.split(",") if addr.strip()]

    def chains(self) -> dict[str, list[str]]:
        return {
            cls: [s.strip() for s in getattr(self, f"chain_{cls}").split(",") if s.strip()]
            for cls in ASSET_CLASSES
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()
