# BOQ → RFQ — Administrator manual

## Roles and access

| Role | Can |
|---|---|
| Administrator | everything: users, settings, AI models, audit log, every project |
| Manager | every project (view, edit, membership), approves and awards RFQs, sees the job queue and usage |
| Engineer | creates projects; works on own, team-shared and member projects; issues RFQs and records quotes |
| Viewer | reads team-shared and member projects, comments; changes nothing |

Per project (⋯ › Members & sharing): **Share with the team** lets every engineer edit and every viewer read the project.
**Private** limits it to the owner, the members (editor or viewer), managers and administrators.

## Users

**Admin › Users**: add a user (they change their password under Account), reset a password (signs the user out
everywhere and lifts a sign-in lock), disable an account (their projects stay), change a role.

**Locked out?** 5 wrong passwords for one user name (or 20 from one address) lock sign-in for 15 minutes. If the only
administrator is locked out: `python manage.py reset-password <username>` on the server.

**Company sign-in (Active Directory / LDAP)**: set `BOQ_AUTH = "ldap+local"` and the `BOQ_LDAP_*` values in
`boq2rfq.toml` (see the example file), install `ldap3` (`pip install -r requirements-optional.txt`) and restart.
People sign in with their company account; their role follows their AD groups (`BOQ_LDAP_ROLE_MAP`) at every sign-in.
Keep one local administrator until the directory sign-in is confirmed to work, then switch to `ldap` only.
*Not tested against a real directory yet — only with a simulated one.*

## AI models (Admin › AI models)

* One model per role (text, vision, embeddings, second opinion), with fallbacks used when one is missing or fails.
* **Staged** (one GPU): only one model in GPU memory at a time. **Resident** (several GPUs): all stay loaded.
* **Reuse answers**: identical questions are answered from the cache — re-running an unchanged BOQ costs nothing.
* **Remember corrections**: when a user moves an item to another category, the same item text gets that category in
  later analyses.
* **Remember special-case decisions**: every Approve / Deny is stored; a new project's open cases get the latest
  decision of the same kind of case (else of the same rule), marked "Remembered" for review. A revised BOQ takes its
  own project's decisions first. Users review, change or forget them on Account menu › **Remembered decisions**.
* **Second opinion**: a different model re-checks doubtful items and *flags* disagreements (it never changes an answer).
* Cloud models are never offered.

**Before changing a default, measure it**: `python bench/benchmark.py --config baseline --config <candidate>` runs the
engine on historical projects where people made the packages by hand (see BENCHMARK.md for the method and the last
results). Change the default only if the candidate is measurably better.

## Day-to-day

* **Admin › System health** — database, worker, models, GPU, disk, Excel, OCR, sign-in mode.
* **Admin › Job queue** — what runs and waits; cancel a stuck job, retry a failed one, test the worker. Jobs survive
  restarts: a job whose worker stopped is put back in the queue (up to 3 attempts); temporary AI-server outages are
  retried automatically.
* **Admin › Audit log** — sign-ins (also failed ones), user changes, every change to a project, RFQ status changes.
* **Admin › Usage** — analyses, job time and AI time per user; calls, cache hits and failures per model. The queue is
  fair: the next job is taken from the person who has waited longest.
* **Admin › Settings** — the effective settings and where each comes from (file, environment, default).
* **Trash** — deleted projects are removed for good after `BOQ_TRASH_DAYS` (default 30) days.

## Backups

```sh
python manage.py backup /backups/boq-$(date +%F).zip     # database + project files + secret key, with checksums
python manage.py verify /backups/boq-2026-11-23.zip      # check a backup without restoring
```

Schedule the backup daily (cron / Task Scheduler) and copy the file off the server. Restore: see MIGRATION_RUNBOOK.md.
The AI cache (`workspace/_cache`) is left out unless `--with-cache` is given — it is rebuilt automatically.

## Updating the app

1. `python manage.py backup …`
2. Stop the web and worker services, replace the app files (keep `boq2rfq.toml`, `instance/`, `workspace/`).
3. `pip install -r requirements.txt`, start the services. Database changes are applied automatically at start
   (`python manage.py status` shows the schema version).

## Troubleshooting

| Symptom | Check |
|---|---|
| "AI offline — keyword mode" | Ollama running? `OLLAMA_URL` uses the IP (`127.0.0.1`), not `localhost`? Models pulled? |
| Jobs stay "Waiting" | Admin › Job queue: is a worker running? With `BOQ_WORKER = "external"` the worker service must run. |
| Users signed out often | `BOQ_IDLE_MINUTES`; behind a proxy without HTTPS, `BOQ_HTTPS` must be 0 (Secure cookies need HTTPS). |
| "Request from another site refused" | the proxy must pass the original `Host` header (`proxy_set_header Host $host`). |
| Upload refused | content does not match the file type, or above `BOQ_MAX_UPLOAD_MB` / pages / megapixels limits. |
