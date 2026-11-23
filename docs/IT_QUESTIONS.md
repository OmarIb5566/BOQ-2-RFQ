# Questions for IT

The app is ready for each of these; the answers only change settings in `boq2rfq.toml`.

## Server
1. Operating system and version of the server (we reach it through MobaXterm/SSH — Linux? which distribution?).
2. Python 3.12 available, or may we install it?
3. Can we run two services (web + worker) — systemd on Linux / NSSM on Windows?
4. Disk: where may we keep the database (local disk, ~1 GB) and the project files (grows with uploads; plan 200 GB+)?
5. Who runs backups, where are they copied, and how long are they kept?

## GPU and AI
6. Which GPUs, how many, how much memory each? (One 24 GB GPU → "staged"; several → "resident".)
7. May Ollama run on this server (or another server on the internal network — which address)?
8. May we download the models once (≈ 20 GB) through a proxy, or must they be copied in by hand?

## Network and access
9. The internal host name for the app (e.g. `boq2rfq.company.local`) and an HTTPS certificate for it.
10. Reverse proxy: nginx or IIS — managed by IT or by us? Upload limit 4 GB, timeouts 10 minutes.
11. Which networks / VPN may reach it? Any firewall rules to request?

## Sign-in
12. Should people sign in with their company (Active Directory) accounts? If yes:
    the LDAPS address, the user name format (UPN `name@company.local` or `DOMAIN\name`), the search base,
    and four AD groups for the roles (administrators, managers, engineers, viewers).
13. Must the server hold a service account for directory searches, or is binding as the user enough?

## Office files
14. Will Microsoft Excel be on the server? (Only Windows; needed for `.xlsb` files and exact-look snapshots. Without it
    the app uses its own renderer.)
15. Is outbound e-mail (SMTP) allowed for sending RFQs later, or do engineers keep sending them by hand?

## Operations
16. Who is the first administrator in the app, and who is the backup administrator?
17. Maintenance window for updates, and who is called when the app is down?
18. Data retention: how long must projects and the audit log be kept? (Trash is emptied after 30 days by default.)
