# B-16: Tägliche Buch-Sicherung und Wiederanlauf (Hedgeclip)

Gate-B-Bedingung B-16 ([HED-85](https://github.com/michaexel-copilot/paper-trading-platform),
Risikopolitik HED-8, Plan Revision 4). Betrifft ausschließlich Hedgeclip-Installationen
(`deploy/hedgeclip.env.example`) -- Deployments ohne Bezug zu Hedgeclip lassen
`BUCH_SICHERUNG_AKTIV` auf dem Default `false`.

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

1. rclone installieren (MIT-Lizenz, Prüfkarte siehe HED-85-Kommentar; Freigabe durch
   CEO ausstehend -- **nicht vor Freigabe installieren**).
2. Azure-AD-App-Registrierung mit Scope `Files.ReadWrite.AppFolder` anlegen (Plan
   Revision 4 Abschnitt 3a, Option A -- vom Board entschieden). Ergebnis: `client_id`,
   `client_secret`.
3. Zwei rclone-Remotes in `/root/.config/rclone/rclone.conf` (oder dem Home-Verzeichnis
   des Service-Users):
   - `onedrive`: `type = onedrive`, mit den Werten aus Schritt 2 statt rclones
     Standard-App-ID.
   - `onedrive-crypt`: `type = crypt`, `remote = onedrive:hedgeclip-backups`,
     `password` = das obscured Crypt-Passwort (`rclone obscure <Klartext>`), aus dem
     Paperclip-Secret `ONEDRIVE_RCLONE_CRYPT_PASSWORD` (vom Board angelegt) --
     **nie im Klartext in einer Datei dieses Repos**.
4. `BUCH_SICHERUNG_AKTIV=true` in `/etc/paper-trading/paper-trading.env` setzen,
   `sudo systemctl restart paper-trading`.
5. Nach dem ersten Lauf prüfen: `buch_sicherung_lauf` hat eine Zeile mit
   `ergebnis = 'erfolg'`, und in OneDrive liegt unter `Apps/<App-Name>/papier/<host>/`
   (bzw. `live/<host>/`) eine neue, verschlüsselte Datei.

## Löschschutz (fest verdrahtet, nicht konfigurierbar)

- `app/trading/buch_sicherung.py` ruft ausschließlich `rclone copy` auf, nie `sync`.
- Die Rotation (`rotiere_einmal`, täglich nach einer erfolgreichen Sicherung) löscht
  ausschließlich Dateien älter als `BUCH_SICHERUNG_ROTATION_TAGE` Tage (Default 30),
  über `rclone delete --min-age`, nie durch Abgleich gegen den Host-Zustand.
- Zusätzlich (Board prüft, keine Code-Einstellung): OneDrive-Versionsverlauf und
  Papierkorb sind für das Zielkonto aktiv (Standard).

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

Noch nicht durchgeführt. Voraussetzungen fehlen: die Prüfkarte für rclone (Anhang A,
Kommentar auf HED-85) ist beim CEO zur Freigabe eingereicht, aber noch nicht
entschieden; die Azure-App-Registrierung, das `onedrive-crypt`-Remote und das
Paperclip-Secret `ONEDRIVE_RCLONE_CRYPT_PASSWORD` sind noch nicht eingerichtet
(`GET /api/agents/me/secrets`, Stand 20.10.2026: nicht vorhanden). Der Code
(Sicherung, Rotation, Dashboard-Nachweis) ist fertig und getestet (gestubbte
`sqlite3`/`rclone`-Aufrufe, siehe `backend/tests/test_buch_sicherung.py`); der echte
Lauf und dieser Wiederanlauf-Test folgen, sobald beides vorliegt.
