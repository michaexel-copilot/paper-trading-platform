"""Papier-spezifischer Code der Ausführungsschicht (Auflage 4, Entwurf Abschnitt 3.4).

Alles, was hier oder in Unterpaketen liegt, ist im Live-Artefakt **nicht enthalten**
(Dockerfile-Stufe ``live``, ``scripts/build_checks.py --artefakt-art live``). Das Build
schließt dieses Paket aus, statt es zur Laufzeit abzuschalten -- ein Laufzeitschalter
wäre genau der Konfigurationsfehler, den das Board als Hauptrisiko benannt hat.

``okx_demo.py`` (B-6, HED-76) ist der erste konkrete Baustein: der echte OKX-Demo-
Orderpfad plus reiner Kontostand-Lesevorgang, nur Integrationstest, nie
Performancemessung (Handbuch Abschnitt 7).
"""
