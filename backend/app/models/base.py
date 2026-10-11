from sqlalchemy import MetaData, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, validates

from app.betriebsart import BetriebsartFehler, get_betriebsart

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)


class BetriebsartMixin:
    """Pflichtfeld 'betriebsart' auf jedem Datensatz der Ausführungsschicht (Auflage 1).

    Jeder neue Datensatz wird beim Anlegen automatisch mit der Betriebsart des
    laufenden Prozesses markiert. Ein Datensatz, dessen Betriebsart-Feld nicht zur
    Prozess-Betriebsart passt, wird nicht geschrieben -- Abweichung ist ein
    Sofort-Stopp-Fall (Entwurf HED-36 Abschnitt 3.1, letzter Satz).
    """

    betriebsart: Mapped[str] = mapped_column(String(5), default=lambda: get_betriebsart().value)

    @validates("betriebsart")
    def _betriebsart_muss_passen(self, _key: str, value: str) -> str:
        erwartet = get_betriebsart().value
        if value != erwartet:
            raise BetriebsartFehler(
                f"Datensatz trägt Betriebsart {value!r}, Prozess läuft als {erwartet!r}. "
                "Schreibvorgang abgebrochen, Sofort-Stopp-Fall (Auflage 1)."
            )
        return value
