# BOQ → RFQ — Data migration runbook

Moving the data from one installation to another (e.g. from the pilot PC to the company server), or restoring a backup.
All data lives in two folders: `instance/` (database, secret key) and `workspace/` (projects).

## A. Move the pilot PC to the server

1. **On the PC**: stop the app (close the window). Run `.venv\Scripts\python manage.py backup D:\move\boq.zip`
   and `… manage.py verify D:\move\boq.zip` → "Backup OK."
2. Copy `boq.zip` to the server.
3. **On the server**: install the app (INSTALL.md) but do **not** create the administrator. Stop both services.
4. `python manage.py verify /tmp/boq.zip`, then `python manage.py restore /tmp/boq.zip` (asks you to type RESTORE).
   * The empty data of the fresh installation is kept as `*.before-restore-<time>` next to it.
   * The database is upgraded to the current schema automatically.
   * Paths inside the projects point to the old PC's folders; they are re-pointed to the new folder automatically when
     a project is opened — nothing to convert by hand.
5. Start both services. Sign in with the existing accounts. Check: Projects (all there), open two projects, download an
   export, Admin › System health.
6. `python manage.py reindex` (search index and meaning vectors of every project).

## B. Restore a backup after a problem

1. Stop the web and worker services (restore refuses while the database is in use).
2. `python manage.py verify <backup.zip>` — a damaged or edited backup is refused (per-table and per-file checksums).
3. `python manage.py restore <backup.zip>`.
   If anything fails half-way, the previous data is put back automatically ("Restore failed and was rolled back").
4. Start the services; check as in A.5. Remove the `*.before-restore-*` folders once everything is confirmed.

## C. Data only (database rows), e.g. for a DBA review

`python manage.py export-json data.json` writes every table as JSON with a checksum per table.
`python manage.py import-json data.json` loads it into an **empty** installation, in one transaction (all or nothing),
and refuses a file whose checksums do not match. Project files are not included — use backup / restore for a move.

## Rules

* Never copy `instance/boq2rfq.db` while the app runs — use `manage.py backup` (it takes a consistent snapshot).
* Keep `instance/` on a local disk; `workspace/` may be on a network share.
* `instance/secret.key` signs the sessions: after a restore with a different key, users simply sign in again.
