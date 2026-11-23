# BOQ → RFQ — Installation guide

For the person installing the app on a company server (Linux or Windows). Allow about an hour.

## 1. What the server needs

| Need | Why | Notes |
|---|---|---|
| Python 3.12 | runs the app | the same Python for the web server and the worker |
| Ollama + the models | the AI engine | `gemma4:e4b` (text), `qwen2.5vl:7b` (scanned drawings), `bge-m3` (search by meaning). Optional: `qwen3:14b` (second opinion). ~20 GB of disk. |
| An NVIDIA GPU (≥ 16 GB) | speed | one RTX 4090 (24 GB) runs everything in the "staged" profile |
| Tesseract OCR with Arabic | scanned BOQs only | optional |
| A local disk for the database | SQLite | never put `instance/` on a network share |
| An HTTPS certificate + nginx / IIS | encrypted access | from IT |
| Microsoft Excel | optional, Windows only | exact-look snapshots and `.xlsb` files; not available on Linux and not supported inside a Windows service |

No Docker, no cloud services, no outgoing internet at runtime: everything stays on the server.

## 2. Linux (systemd + nginx)

```sh
sudo useradd --system --home /opt/boq2rfq boq2rfq
sudo mkdir -p /opt/boq2rfq /srv/boq2rfq /etc/boq2rfq
sudo chown boq2rfq: /opt/boq2rfq /srv/boq2rfq
# copy the app folder to /opt/boq2rfq, then:
cd /opt/boq2rfq
sudo -u boq2rfq python3.12 -m venv .venv
sudo -u boq2rfq .venv/bin/pip install -r requirements.txt   # + requirements-optional.txt for LDAP
sudo cp boq2rfq.example.toml /etc/boq2rfq/boq2rfq.toml           # then edit it (paths, HTTPS, sign-in)
sudo cp deploy/linux/boq2rfq-web.service deploy/linux/boq2rfq-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now boq2rfq-web boq2rfq-worker
sudo cp deploy/linux/nginx-boq2rfq.conf /etc/nginx/sites-available/boq2rfq   # set server_name + certificate
sudo ln -s /etc/nginx/sites-available/boq2rfq /etc/nginx/sites-enabled/ && sudo nginx -t && sudo systemctl reload nginx
```

In `boq2rfq.toml` set at least: `BOQ_WORKSPACE`, `BOQ_WORKER = "external"`, `BOQ_HTTPS = 1`, `BOQ_BEHIND_PROXY = 1`.

## 3. Windows Server (NSSM services + IIS / nginx)

1. Install Python 3.12 and Ollama; `ollama pull gemma4:e4b`, `ollama pull qwen2.5vl:7b`, `ollama pull bge-m3`.
2. Copy the app folder (e.g. `D:\BOQ2RFQ`), then in PowerShell: `python -m venv .venv; .venv\Scripts\pip install -r requirements.txt`.
3. Copy `boq2rfq.example.toml` to `boq2rfq.toml` and edit it (on Windows the default Tesseract path is used).
4. Download `nssm.exe` and run, as administrator: `.\deploy\windows\install-services.ps1 -Nssm C:\tools\nssm.exe`.
5. Put IIS (URL Rewrite + ARR) or nginx with the certificate in front of `http://127.0.0.1:8080`
   (upload size 4 GB, timeouts 600 s, pass `X-Forwarded-For/-Proto/-Host`).

## 4. First start

The first time, the server writes a **one-time set-up link** into its log (`journalctl -u boq2rfq-web` on Linux,
`logs\BOQ2RFQ-Web.log` on Windows) and into `instance/setup.token`. Open it in a browser and create the first
administrator. Nobody else can create an administrator. Then:

* **Admin › System health**: everything green (grey = optional and not used).
* **Admin › Job queue › Test the worker**: the test job finishes within seconds.
* **Admin › Users**: add the team (or switch sign-in to the company directory, see the admin manual).

## 5. Single PC (no server)

Double-click `run_app.bat` (this PC only) or `run_for_team.bat` (colleagues on the office network). Jobs run inside the
same program; nothing else to install apart from Python and Ollama.

## 6. Check after installing

```sh
.venv/bin/python manage.py status          # schema version, counts, folders
.venv/bin/python manage.py backup /srv/backups/first.zip && .venv/bin/python manage.py verify /srv/backups/first.zip
```
