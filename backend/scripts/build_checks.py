"""Maschinelle Build-Prüfung für Auflage 3 und Auflage 4 (Entwurf HED-36 Abschnitt 3.3/3.4).

Auflage 3: durchsucht die Installation nach jeder Verwendung von
``app.schluessel.read_exchange_credential`` -- der einzigen Funktion, die einen
Gegenstellen-Schlüssel liest. Gefunden werden darf genau eine Stelle:
``app/trading/versandengpass.py``. Jede weitere Stelle bricht den Build ab.

Auflage 4: für das Live-Artefakt darf ``app/trading/papier`` -- der designierte
Ablageort für papier-spezifischen Code (Simulations-Fill-Logik, Demo-Orderpfad,
Testdaten-Erzeuger) -- im Quellbaum nicht vorhanden sein.

Aufruf: ``python scripts/build_checks.py --artefakt-art papier_artefakt|live_artefakt``
Exit-Code ungleich 0 bricht den Build ab (CI-Schritt, siehe .github/workflows).
"""

import argparse
import ast
import sys
from pathlib import Path

ERLAUBTE_ZUGRIFFSSTELLE = Path("app/trading/versandengpass.py")
PAPIER_NUR_PAKET = Path("app/trading/papier")
SCHLUESSELMODUL_NAME = "schluessel.py"
ZU_SUCHENDE_FUNKTION = "read_exchange_credential"


def _ruft_funktion_auf(baum: ast.AST, funktionsname: str) -> bool:
    for knoten in ast.walk(baum):
        if isinstance(knoten, ast.Call):
            ziel = knoten.func
            name = ziel.attr if isinstance(ziel, ast.Attribute) else getattr(ziel, "id", None)
            if name == funktionsname:
                return True
    return False


def finde_schluessel_zugriffe(app_root: Path) -> list[Path]:
    """Alle .py-Dateien unterhalb ``app_root``, die ``read_exchange_credential`` aufrufen.

    Die Definition selbst (``schluessel.py``) wird ausgenommen -- dort steht die
    Funktion, dort wird sie nicht aufgerufen.
    """
    treffer = []
    for pfad in sorted(app_root.rglob("*.py")):
        if pfad.name == SCHLUESSELMODUL_NAME:
            continue
        baum = ast.parse(pfad.read_text(), filename=str(pfad))
        if _ruft_funktion_auf(baum, ZU_SUCHENDE_FUNKTION):
            treffer.append(pfad.relative_to(app_root.parent))
    return treffer


def pruefe_einzige_zugriffsstelle(app_root: Path) -> str | None:
    treffer = finde_schluessel_zugriffe(app_root)
    unerlaubt = [t for t in treffer if t != ERLAUBTE_ZUGRIFFSSTELLE]
    if unerlaubt:
        namen = ", ".join(str(t) for t in unerlaubt)
        return (
            f"Unerlaubte Zugriffsstelle(n) auf {ZU_SUCHENDE_FUNKTION}: {namen}. "
            f"Erlaubt ist ausschließlich {ERLAUBTE_ZUGRIFFSSTELLE}."
        )
    if ERLAUBTE_ZUGRIFFSSTELLE not in treffer:
        return (
            f"{ERLAUBTE_ZUGRIFFSSTELLE} ruft {ZU_SUCHENDE_FUNKTION} nicht auf. "
            "Der Versand-Engpass wäre folgenlos -- Prüfung greift nicht."
        )
    return None


def pruefe_live_artefakt_ohne_papier_pakete(app_root: Path) -> str | None:
    pfad = app_root.parent / PAPIER_NUR_PAKET
    if pfad.exists():
        return f"{PAPIER_NUR_PAKET} ist im Live-Artefakt vorhanden (Auflage 4)."
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--artefakt-art", choices=("papier_artefakt", "live_artefakt"), required=True
    )
    parser.add_argument(
        "--app-root", type=Path, default=Path(__file__).resolve().parent.parent / "app"
    )
    args = parser.parse_args()

    fehler = []
    einzige_zugriffsstelle_fehler = pruefe_einzige_zugriffsstelle(args.app_root)
    if einzige_zugriffsstelle_fehler:
        fehler.append(einzige_zugriffsstelle_fehler)

    if args.artefakt_art == "live_artefakt":
        papier_fehler = pruefe_live_artefakt_ohne_papier_pakete(args.app_root)
        if papier_fehler:
            fehler.append(papier_fehler)

    if fehler:
        for eintrag in fehler:
            print(f"BUILD-PRUEFUNG FEHLGESCHLAGEN: {eintrag}", file=sys.stderr)
        return 1

    print(f"Build-Prüfung bestanden ({args.artefakt_art}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
