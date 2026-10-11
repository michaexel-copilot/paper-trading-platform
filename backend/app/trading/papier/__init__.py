"""Papier-spezifischer Code der Ausführungsschicht (Auflage 4, Entwurf Abschnitt 3.4).

Alles, was hier oder in Unterpaketen liegt, ist im Live-Artefakt **nicht enthalten**
(Dockerfile-Stufe ``live``, ``scripts/build_checks.py --artefakt-art live``). Das Build
schließt dieses Paket aus, statt es zur Laufzeit abzuschalten -- ein Laufzeitschalter
wäre genau der Konfigurationsfehler, den das Board als Hauptrisiko benannt hat.

Noch leer: B-6 (OKX-Orderpfad auf Demo) ist der erste konkrete Baustein, der hier
landet, sobald das OKX-Demo-Secret vorliegt. Bis dahin ist dieses Paket die Build-
Prüfung wert, nicht die Logik.
"""
