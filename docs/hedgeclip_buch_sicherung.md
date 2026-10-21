# B-16: Tägliche Buch-Sicherung und Wiederanlauf (Hedgeclip)

Gate-B-Bedingung B-16 ([HED-85](https://github.com/michaexel-copilot/paper-trading-platform),
Risikopolitik HED-8, Plan Revision 6). Betrifft ausschließlich Hedgeclip-Installationen
(`deploy/hedgeclip.env.example`) -- Deployments ohne Bezug zu Hedgeclip lassen
`BUCH_SICHERUNG_AKTIV` auf dem Default `false`.

Board-Bescheid 21.10.2026 (HED-85, Kommentar f40c53f3): Skriptbau freigegeben, Probelauf
Teil 2 akzeptiert. Dieses Dokument trägt die sechs Auflagen aus diesem Bescheid nach --
insbesondere die Ergänzung der rclone-Konfiguration um `drive_id`/`drive_type`/
`root_folder_id` (Schritt 3 unten) und die Token-/Secret-Lebensdauer (Abschnitt
"Token-Lebensdauer und Ablauf").

## Was gesichert wird

Positionen, Orders und das Freigabe-Protokoll liegen alle in der einen SQLite-Datei
hinter `DATABASE_URL`. Der Prozess erstellt täglich (`BUCH_SICHERUNG_INTERVALL_SEKUNDEN`,
Default 24 h) einen konsistenten `sqlite3 .backup`-Snapshot dieser Datei und lädt ihn
über `rclone copy` nach OneDrive hoch -- verschlüsselt (rclone crypt), bevor irgendetwas
den Host verlässt. Der Code (`app/trading/buch_sicherung.py`) liest nie ein Token oder
Crypt-Passwort selbst; beides steckt ausschließlich in der rclone-Konfiguration des
Hosts.

Jeder Lauf -- erfolgreich oder nicht -- hinterlässt eine Zeile in
`buch_sicherung_lauf`. Ein Fehlschlag löst eine Mail an Board + CRO aus
(`app.notify.notify_backup_failure`); er setzt **kein** Sperrkennzeichen, B-16 ist eine
Aufbewahrungspflicht, keine Handelsvoraussetzung wie B-5/B-12.

## Einmalige Einrichtung auf dem Host (Board, vor `BUCH_SICHERUNG_AKTIV=true`)

1. rclone installieren (MIT-Lizenz, Klasse A, Interaktion 614f9116, freigegeben durch
   den CEO am 20.10.2026).
2. Azure-AD-App-Registrierung mit Scope `Files.ReadWrite.AppFolder` anlegen (Plan
   Revision 6 Abschnitt 3a, Option A -- vom Board entschieden, Probelauf bestanden).
   Ergebnis: `client_id`, `client_secret`.
   **Ablauf des Client-Secrets:** Azure erzwingt eine Höchstlaufzeit von 24 Monaten.
   Tatsächliches Ablaufdatum (aus dem Azure-Portal, bei der Registrierung sichtbar):
   **noch nicht vom Board mitgeteilt** -- hier eintragen, sobald bekannt (Plan
   Abschnitt 7, Punkt 7). Empfehlung: 60 Tage vor diesem Datum ein neues Secret
   erzeugen und `ONEDRIVE_CLIENT_SECRET` aktualisieren, sonst reißt die tägliche
   Sicherung mit dem OAuth-Fehlerbild aus "Token-Lebensdauer und Ablauf" unten ab.
3. `drive_id`, `drive_type`, `root_folder_id` ermitteln (Probelauf-Befund, Kommentar
   962fd859, 20.10.2026 -- eine rclone-Config mit nur `client_id`/`client_secret`/
   `token`/`access_scopes` scheitert mit `unable to get drive_id and drive_type`, weil
   rclone diese Werte normalerweise über `/me/drive` ermittelt, was ein App-Ordner-Token
   nicht erlaubt):
   ```sh
   curl -s -H "Authorization: Bearer <access_token aus dem rclone-Token-Blob>" \
     https://graph.microsoft.com/v1.0/me/drive/special/approot
   ```
   Aus der Antwort: `parentReference.driveId` → `drive_id`, `parentReference.driveType`
   → `drive_type`, die eigene `id` des App-Ordners → `root_folder_id`.
4. Zwei rclone-Remotes in `/root/.config/rclone/rclone.conf` (oder dem Home-Verzeichnis
   des Service-Users), Datei **mit Rechten `600`**, Eigentümer = Service-User:
   - `onedrive`: `type = onedrive`, `client_id`/`client_secret` aus Schritt 2, `token`
     aus dem Ergebnis von `rclone authorize` (Plan Abschnitt 3c), `access_scopes =
     Files.ReadWrite.AppFolder offline_access`, zusätzlich `drive_id`, `drive_type`,
     `root_folder_id` aus Schritt 3.
   - `onedrive-crypt`: `type = crypt`, `remote = onedrive:hedgeclip-backups`,
     `password` = das obscured Crypt-Passwort (`rclone obscure <Klartext>`), aus dem
     Paperclip-Secret `ONEDRIVE_RCLONE_CRYPT_PASSWORD` (vom Board angelegt) --
     **nie im Klartext in einer Datei dieses Repos**.
5. `BUCH_SICHERUNG_AKTIV=true` in `/etc/paper-trading/paper-trading.env` setzen --
   **erst nach CRO-Abnahme der Nachtrags-Unteraufgabe** (HED-51-Verfahren, Plan
   Abschnitt 8) --, `sudo systemctl restart paper-trading`.
6. Nach dem ersten Lauf prüfen: `buch_sicherung_lauf` hat eine Zeile mit
   `ergebnis = 'erfolg'`, und in OneDrive liegt unter `Apps/<App-Name>/papier/<host>/`
   (bzw. `live/<host>/`) eine neue, verschlüsselte Datei.

## Token-Lebensdauer und Ablauf (Auflagen 2+3, Board-Bescheid 21.10.2026)

- **`ONEDRIVE_RCLONE_TOKEN` ist nur der Startwert.** Der Token-Blob aus diesem Secret
  wird einmalig in `rclone.conf` (Rechte `600`, siehe Schritt 4 oben) übertragen. Danach
  liest kein Code dieses Secret erneut -- die laufende Sicherung kennt nur den
  Remote-Namen (`app/trading/buch_sicherung.py`), nie Token oder Passwort direkt.
- **Refresh läuft automatisch.** rclone erneuert das Zugriffstoken bei Bedarf selbst und
  schreibt das Ergebnis (inkl. neuem Refresh-Token, falls Microsoft eines ausstellt)
  zurück in dieselbe `rclone.conf`. Das setzt voraus, dass die Datei für den
  Service-User beschreibbar bleibt (Rechte `600`, derselbe Eigentümer wie der Prozess).
  Keine eigene Erneuerungslogik in diesem Repo nötig.
- **Abgelaufenes Refresh-Token (z. B. durch Widerruf im Azure-Portal oder
  Microsofts Inaktivitätsfrist):** `rclone copy`/`rclone delete` schlägt mit einem
  OAuth-Fehler fehl (typisch: `oauth2: cannot fetch token: ... invalid_grant`),
  Exit-Code ungleich 0. `app/trading/buch_sicherung.py` behandelt das **wie jeden
  anderen rclone-Fehlschlag**: Protokollzeile mit `ergebnis = "fehlgeschlagen"` und dem
  Fehlertext, Mail an Board + CRO (`notify_backup_failure`) -- kein stilles Scheitern.
  Siehe `backend/tests/test_buch_sicherung.py::test_abgelaufenes_refresh_token_wird_gemeldet_nicht_stumm`
  für den Nachweis mit einem realistischen `invalid_grant`-Fehlertext. Behebung: Schritt
  "rclone-Login" (Plan Abschnitt 3c) wiederholen, neuen Token-Blob in `rclone.conf`
  eintragen.
- **Client-Secret-Ablauf:** siehe Schritt 2 oben -- maximal 24 Monate, tatsächliches
  Datum noch vom Board zu benennen.

## Löschschutz (fest verdrahtet, nicht konfigurierbar)

- `app/trading/buch_sicherung.py` ruft ausschließlich `rclone copy` auf, nie `sync`.
- Die Rotation (`rotiere_einmal`, täglich nach einer erfolgreichen Sicherung) löscht
  ausschließlich Dateien älter als `BUCH_SICHERUNG_ROTATION_TAGE` Tage (Default 30),
  über `rclone delete --min-age`, nie durch Abgleich gegen den Host-Zustand.
- Zusätzlich (Board prüft, keine Code-Einstellung): OneDrive-Versionsverlauf und
  Papierkorb sind für das Zielkonto aktiv (Standard).

## Keine Secrets in Kommandozeilenargumenten (Auflage 5, Board-Bescheid 21.10.2026)

`app/trading/buch_sicherung.py` übergibt `rclone` ausschließlich den Remote-Namen
(z. B. `onedrive-crypt:papier/<hostname>`) als Kommandozeilenargument -- sichtbar in der
Prozessliste (`ps aux`), aber harmlos, da es kein Geheimnis ist. Token, Client-Secret und
Crypt-Passwort stehen ausschließlich in `rclone.conf` (Rechte `600`); rclone liest sie von
dort, nie aus Argumenten oder Umgebungsvariablen dieses Prozesses. Unverändert seit
Revision 4/5, hier durch die Auflage bestätigt.

## Wiederanlauf-Runbook (auf einem getrennten Ziel, NIE auf dem Produktionshost)

Pflicht vor Gate B: mindestens einmal tatsächlich durchgespielt, danach quartalsweise.

1. Eine separate VM oder Container vorbereiten, ohne Zugriff auf die laufende
   Installation -- kein gemeinsames Dateisystem, kein gemeinsamer Lock.
2. rclone mit **demselben** `onedrive-crypt`-Remote konfigurieren (gleiche
   Azure-App-Credentials und Crypt-Passwort, aus den Paperclip-Secrets -- nicht aus
   diesem Repo).
3. Jüngste Datei laden und entschlüsseln:
   ```sh
   rclone copy "onedrive-crypt:<papier|live>/<hostname>/" ./restore/ \
     --include "app.db-*.db" --max-age 2d
   ```
   (`rclone copy` von einem Crypt-Remote entschlüsselt automatisch -- das Ziel
   `./restore/` enthält eine normale SQLite-Datei.)
4. Integrität prüfen:
   ```sh
   sqlite3 ./restore/app.db-<zeitstempel>.db "PRAGMA integrity_check;"
   ```
   Erwartet: genau `ok`.
5. Stichprobe gegen den bekannten Stand der Quelle: Zeilenzahl `orders`, `trades`,
   `proposals` sowie der letzte Eintrag in `board_notifications` bzw.
   `proposal_action_attempt` vergleichen (grober Plausibilitätscheck, keine
   Bit-für-Bit-Prüfung -- die übernimmt `integrity_check`).
6. Ergebnis dokumentieren: Datum, geprüfte Datei, `integrity_check`-Ausgabe,
   Stichprobenvergleich, Dauer des gesamten Durchlaufs. Als Kommentar auf HED-85 oder
   als eigenes Dokument, CRO-sichtbar.
7. Temporäre VM/Container und die entschlüsselte Kopie danach löschen -- sie hat
   dieselbe Sensitivität wie das Produktionsbuch.

### Stand dieses Durchlaufs

Noch nicht durchgeführt. Stand 21.10.2026: rclone ist freigegeben (Klasse A, Interaktion
614f9116), alle fünf Secrets sind angelegt (`ONEDRIVE_CLIENT_ID`, `ONEDRIVE_CLIENT_SECRET`,
`ONEDRIVE_RCLONE_CRYPT_PASSWORD`, `ONEDRIVE_RCLONE_TOKEN`), der technische Probelauf
(Dateiroundtrip über Basis- und Crypt-Remote) ist erfolgreich abgeschlossen (Kommentar
962fd859) und vom Board akzeptiert (Kommentar f40c53f3). Der Code (Sicherung, Rotation,
Dashboard-Nachweis, inkl. Testfall für abgelaufenes Refresh-Token) ist fertig und getestet
(gestubbte `sqlite3`/`rclone`-Aufrufe, siehe `backend/tests/test_buch_sicherung.py`).

Noch offen vor dem ersten echten Lauf: CRO-Abnahme der Nachtrags-Auflagen (HED-51-
Verfahren, eigene Unteraufgabe), danach Board-Merge, danach `BUCH_SICHERUNG_AKTIV=true` auf
einem echten Host. Erst danach kann der Pflicht-Wiederanlauf-Test tatsächlich durchgespielt
werden. `drive_id`/`drive_type`/`root_folder_id` (Schritt 3 oben) sind aus dem Probelauf
bekannt, aber noch nicht in einer produktiven Host-`rclone.conf` eingetragen -- das ist
Teil der Host-Einrichtung, nicht des Codes in diesem Repo.
