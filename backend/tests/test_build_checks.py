"""Testpfad 9, Entwurf Abschnitt 9.6 Gruppe D: Build-Prüfung auf eine einzige
Zugriffsstelle, inklusive einer künstlich eingebauten zweiten Stelle, die den Build
abbrechen muss.
"""

from pathlib import Path

import pytest

import scripts.build_checks as build_checks

BACKEND_ROOT = Path(__file__).resolve().parent.parent
APP_ROOT = BACKEND_ROOT / "app"


def test_echter_quellbaum_hat_genau_eine_zugriffsstelle():
    assert build_checks.pruefe_einzige_zugriffsstelle(APP_ROOT) is None


def test_echter_quellbaum_papier_paket_existiert_noch():
    """Dokumentiert den aktuellen Stand: app/trading/papier existiert (Scaffold für
    B-6) und ist deshalb im Live-Artefakt noch ein Fehler, bis es wirklich leer bleibt
    oder B-6 es befüllt und das Dockerfile es für 'live' entfernt."""
    assert build_checks.pruefe_live_artefakt_ohne_papier_pakete(APP_ROOT) is not None


def test_zweite_zugriffsstelle_bricht_die_pruefung_ab(tmp_path):
    app_root = tmp_path / "app"
    (app_root / "trading").mkdir(parents=True)
    (app_root / "schluessel.py").write_text("def read_exchange_credential(exchange): ...\n")
    (app_root / "trading" / "versandengpass.py").write_text(
        "from app.schluessel import read_exchange_credential\n"
        "def send():\n    return read_exchange_credential('okx')\n"
    )
    # Die künstliche zweite Zugriffsstelle, die Testpfad 9 ausdrücklich vorschreibt.
    (app_root / "trading" / "irgendwo_sonst.py").write_text(
        "from app.schluessel import read_exchange_credential\n"
        "def heimlich():\n    return read_exchange_credential('okx')\n"
    )

    fehler = build_checks.pruefe_einzige_zugriffsstelle(app_root)
    assert fehler is not None
    assert "irgendwo_sonst.py" in fehler


def test_fehlende_zugriffsstelle_wird_auch_erkannt(tmp_path):
    """Ein Versand-Engpass, der read_exchange_credential nie aufruft, wäre folgenlos --
    auch das ist ein Build-Fehler, nicht nur ein zusätzlicher Aufrufer."""
    app_root = tmp_path / "app"
    (app_root / "trading").mkdir(parents=True)
    (app_root / "schluessel.py").write_text("def read_exchange_credential(exchange): ...\n")
    (app_root / "trading" / "versandengpass.py").write_text("def send():\n    pass\n")

    fehler = build_checks.pruefe_einzige_zugriffsstelle(app_root)
    assert fehler is not None
    assert "ruft" in fehler


def test_live_artefakt_ohne_papier_paket_besteht(tmp_path):
    app_root = tmp_path / "app"
    (app_root / "trading").mkdir(parents=True)
    assert build_checks.pruefe_live_artefakt_ohne_papier_pakete(app_root) is None


def test_live_artefakt_mit_papier_paket_schlaegt_an(tmp_path):
    app_root = tmp_path / "app"
    (app_root / "trading" / "papier").mkdir(parents=True)
    fehler = build_checks.pruefe_live_artefakt_ohne_papier_pakete(app_root)
    assert fehler is not None


@pytest.mark.parametrize("artefakt_art", ["papier_artefakt", "live_artefakt"])
def test_main_gegen_echten_quellbaum(artefakt_art, monkeypatch, capsys):
    """Gegen den echten Quellbaum: papier_artefakt besteht heute, live_artefakt noch
    nicht (app/trading/papier existiert als leeres Scaffold für B-6, s. Test oben)."""
    monkeypatch.setattr(
        "sys.argv", ["build_checks.py", "--artefakt-art", artefakt_art, "--app-root", str(APP_ROOT)]
    )
    exit_code = build_checks.main()
    if artefakt_art == "papier_artefakt":
        assert exit_code == 0
    else:
        assert exit_code == 1
