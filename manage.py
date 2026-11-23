"""Administration from the command line (run it with the same settings as the server: boq2rfq.toml / environment).

    python manage.py status                      schema version, counts, folders
    python manage.py migrate                     apply database migrations (also done at every start)
    python manage.py backup  backups/boq.zip     database + project files + secret key, with checksums
             [--no-files] [--with-cache]
    python manage.py verify  backups/boq.zip     check a backup's checksums without restoring it
    python manage.py restore backups/boq.zip     replace this installation's data with the backup (stop the server
             [--yes]                             and the worker first). The current data is kept next to it as
                                                 *.before-restore-<time> and put back automatically if anything fails.
    python manage.py export-json data.json       every table as JSON, with per-table checksums
    python manage.py import-json data.json       load such a file into an EMPTY database (one transaction: all or
                                                 nothing)
    python manage.py create-admin <user> <name>  a new administrator (asks for the password)
    python manage.py reset-password <user>       e.g. when the only administrator is locked out
    python manage.py reindex                     rebuild search indexes and meaning vectors of every project
"""
from __future__ import annotations

import argparse
import datetime as dt
import getpass
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import zipfile
from pathlib import Path

FORMAT = "boq2rfq-backup/1"


def runtime():
    from webapp.runtime import build_runtime
    return build_runtime()


# ---------------------------------------------------------------------- checksums
def table_checksums(db_path: Path) -> dict[str, dict]:
    """{table: {rows, sha256}} — the hash covers every row (sorted), so any change is detected."""
    c = sqlite3.connect(db_path)
    out = {}
    tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                                      "AND name NOT LIKE 'search_fts%' ORDER BY name")]
    for t in tables:
        cols = [r[1] for r in c.execute(f"PRAGMA table_info({t})")]
        rows = c.execute(f"SELECT * FROM {t}").fetchall()
        enc = sorted(json.dumps(list(r), ensure_ascii=False, default=lambda b: b.hex() if isinstance(b, bytes)
                                else str(b)) for r in rows)
        h = hashlib.sha256(json.dumps(cols).encode())
        for e in enc:
            h.update(e.encode())
        out[t] = {"rows": len(rows), "sha256": h.hexdigest()}
    c.close()
    return out


def file_sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------- backup / verify / restore
def backup(out: Path, with_files: bool = True, with_cache: bool = False, quiet: bool = False) -> dict:
    rt = runtime()
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="boq-backup-"))
    try:
        snap = tmp / "boq2rfq.db"
        src = sqlite3.connect(rt.db.path)
        dst = sqlite3.connect(snap)
        src.backup(dst)                     # consistent copy even while the app is running
        dst.close()
        src.close()
        manifest = {"format": FORMAT, "created": dt.datetime.now().isoformat(timespec="seconds"),
                    "schema_version": rt.db.version(), "tables": table_checksums(snap), "files": {}}
        ws = Path(rt.settings.workspace)
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
            z.write(snap, "instance/boq2rfq.db")
            key = rt.instance_path / "secret.key"
            if key.exists():
                z.write(key, "instance/secret.key")
            if with_files and ws.exists():
                for p in sorted(ws.rglob("*")):
                    if not p.is_file():
                        continue
                    rel = p.relative_to(ws).as_posix()
                    if rel.startswith("_cache/") and not with_cache:
                        continue
                    arc = f"workspace/{rel}"
                    z.write(p, arc)
                    manifest["files"][arc] = {"size": p.stat().st_size, "sha256": file_sha(p)}
            z.writestr("manifest.json", json.dumps(manifest, indent=1, ensure_ascii=False))
        rt.db.close()
        if not quiet:
            print(f"Backup written: {out} ({out.stat().st_size / 1048576:.1f} MB, {len(manifest['tables'])} tables, "
                  f"{len(manifest['files'])} files)")
        return manifest
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def verify(path: Path, quiet: bool = False) -> list[str]:
    """[] when the backup is intact, else the problems found."""
    problems = []
    with zipfile.ZipFile(path) as z:
        try:
            man = json.loads(z.read("manifest.json"))
        except KeyError:
            return ["no manifest.json — not a backup made by manage.py"]
        if man.get("format") != FORMAT:
            problems.append(f"unknown format {man.get('format')!r}")
        names = set(z.namelist())
        for arc, meta in man.get("files", {}).items():
            if arc not in names:
                problems.append(f"missing file {arc}")
                continue
            h = hashlib.sha256()
            with z.open(arc) as f:
                while chunk := f.read(1 << 20):
                    h.update(chunk)
            if h.hexdigest() != meta["sha256"]:
                problems.append(f"checksum mismatch {arc}")
        tmp = Path(tempfile.mkdtemp(prefix="boq-verify-"))
        try:
            z.extract("instance/boq2rfq.db", tmp)
            got = table_checksums(tmp / "instance" / "boq2rfq.db")
            for t, meta in man.get("tables", {}).items():
                if t not in got:
                    problems.append(f"missing table {t}")
                elif got[t] != meta:
                    problems.append(f"table {t}: {got[t]['rows']} rows / checksum differs from the manifest")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    if not quiet:
        print("Backup OK." if not problems else "Backup has problems:\n  " + "\n  ".join(problems[:50]))
    return problems


def restore(path: Path, yes: bool = False) -> None:
    problems = verify(path, quiet=True)
    if problems:
        raise SystemExit("Refusing to restore a damaged backup:\n  " + "\n  ".join(problems[:20]))
    from boq_agent.config import Settings
    from webapp.runtime import instance_dir
    st = Settings()
    ws, inst = Path(st.workspace).resolve(), instance_dir(st).resolve()
    if not yes:
        a = input(f"This REPLACES\n  {inst}\n  {ws}\nwith the backup. Stop the server and the worker first. "
                  "Type RESTORE to continue: ")
        if a.strip() != "RESTORE":
            raise SystemExit("Cancelled.")
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    kept = []
    for d in (inst, ws):
        if d.exists():
            k = d.with_name(f"{d.name}.before-restore-{stamp}")
            try:
                d.rename(k)
            except PermissionError:
                for d0, k0 in kept:
                    k0.rename(d0)
                raise SystemExit(f"{d} is in use — stop the server and the worker first, then restore again.")
            kept.append((d, k))
    try:
        with zipfile.ZipFile(path) as z:
            for info in z.infolist():
                name = Path(info.filename)
                if name.is_absolute() or ".." in name.parts:
                    continue
                if info.filename.startswith("instance/"):
                    target = inst / Path(*name.parts[1:])
                elif info.filename.startswith("workspace/"):
                    target = ws / Path(*name.parts[1:])
                else:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as s, open(target, "wb") as d:
                    shutil.copyfileobj(s, d)
            man = json.loads(z.read("manifest.json"))
        ws.mkdir(parents=True, exist_ok=True)
        got = table_checksums(inst / "boq2rfq.db")
        bad = [t for t, m in man["tables"].items() if got.get(t) != m]
        if bad:
            raise RuntimeError(f"restored tables differ from the backup: {', '.join(bad)}")
        from webapp.db import DB
        DB(inst / "boq2rfq.db")              # bring an older backup up to the current schema
    except BaseException as e:
        for d, k in kept:                    # roll back
            shutil.rmtree(d, ignore_errors=True)
            k.rename(d)
        raise SystemExit(f"Restore failed and was rolled back: {e}")
    print("Restored. The previous data was kept as:\n  " + "\n  ".join(str(k) for _, k in kept))


# ---------------------------------------------------------------------- JSON export / import
def export_json(out: Path) -> None:
    rt = runtime()
    c = rt.db.conn()
    data = {"format": "boq2rfq-json/1", "schema_version": rt.db.version(), "tables": {}}
    for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                          "AND name NOT LIKE 'search_fts%' AND name <> 'schema_version' ORDER BY name"):
        rows = [dict(r) for r in c.execute(f"SELECT * FROM {t}")]
        for r in rows:
            for k, v in r.items():
                if isinstance(v, bytes):
                    r[k] = {"$hex": v.hex()}
        data["tables"][t] = rows
    data["checksums"] = {t: hashlib.sha256(json.dumps(rows, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                         for t, rows in data["tables"].items()}
    Path(out).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    print(f"Exported {sum(len(v) for v in data['tables'].values())} rows from {len(data['tables'])} tables to {out}")


def import_json(src: Path) -> None:
    data = json.loads(Path(src).read_text(encoding="utf-8"))
    for t, rows in data["tables"].items():
        h = hashlib.sha256(json.dumps(rows, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if h != data["checksums"].get(t):
            raise SystemExit(f"Checksum mismatch in table {t} — the file was changed or is damaged.")
    rt = runtime()
    db = rt.db
    if db.one("SELECT 1 FROM users LIMIT 1") or db.one("SELECT 1 FROM projects LIMIT 1"):
        raise SystemExit("The database is not empty — import only into a fresh installation.")
    first = ["users", "projects"]
    order = first + [t for t in data["tables"] if t not in first]
    with db.tx() as c:                           # one transaction: everything or nothing
        for t in order:
            rows = data["tables"].get(t, [])
            have = {r[1] for r in c.execute(f"PRAGMA table_info({t})")}
            if not have:
                continue
            for r in rows:
                r = {k: (bytes.fromhex(v["$hex"]) if isinstance(v, dict) and "$hex" in v else v) for k, v in r.items()
                     if k in have}
                cols = ", ".join(r)
                c.execute(f"INSERT INTO {t} ({cols}) VALUES ({', '.join('?' * len(r))})", tuple(r.values()))
    print(f"Imported {sum(len(v) for v in data['tables'].values())} rows. Run 'manage.py reindex' to rebuild search.")


# ---------------------------------------------------------------------- accounts
def create_admin(username: str, name: str, password: str | None = None) -> None:
    from werkzeug.security import generate_password_hash

    from webapp.db import now
    rt = runtime()
    pw = password or getpass.getpass("Password (at least 8 characters): ")
    if len(pw) < 8:
        raise SystemExit("The password needs at least 8 characters.")
    if rt.db.one("SELECT 1 FROM users WHERE username=?", (username,)):
        raise SystemExit("That user name is taken.")
    rt.db.x("INSERT INTO users (username, full_name, pw_hash, role, created) VALUES (?,?,?,?,?)",
            (username, name, generate_password_hash(pw), "admin", now()))
    rt.db.audit("admin created (command line)", None, username=username)
    print(f"Administrator {username} created.")


def reset_password(username: str, password: str | None = None) -> None:
    from werkzeug.security import generate_password_hash
    rt = runtime()
    if not rt.db.one("SELECT 1 FROM users WHERE username=?", (username,)):
        raise SystemExit("No such user.")
    pw = password or getpass.getpass("New password (at least 8 characters): ")
    if len(pw) < 8:
        raise SystemExit("The password needs at least 8 characters.")
    rt.db.x("UPDATE users SET pw_hash=?, ses_ver=ses_ver+1, active=1 WHERE username=?",
            (generate_password_hash(pw), username))
    rt.db.x("DELETE FROM login_failures WHERE username=?", (username.lower(),))
    rt.db.audit("password reset (command line)", None, username=username)
    print(f"Password of {username} reset; the account is active and unlocked.")


def reindex() -> None:
    from webapp.collab import index_search
    from webapp.fleet import index_vectors
    rt = runtime()
    n = 0
    for r in rt.db.q("SELECT run_id FROM projects WHERE deleted=0"):
        if not rt.store.exists(r["run_id"]):
            continue
        s = rt.store.load(r["run_id"])
        index_search(rt.db, r["run_id"], s)
        index_vectors(rt, r["run_id"], s)
        n += 1
    print(f"Re-indexed {n} project(s).")


def status() -> None:
    rt = runtime()
    from webapp.db import LATEST
    print(f"Database : {rt.db.path} (schema {rt.db.version()}/{LATEST})")
    print(f"Workspace: {rt.settings.workspace}")
    for t in ("users", "projects", "jobs", "rfqs", "comments"):
        print(f"  {t:10} {rt.db.one(f'SELECT COUNT(*) n FROM {t}')['n']}")


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("migrate")
    b = sub.add_parser("backup")
    b.add_argument("out")
    b.add_argument("--no-files", action="store_true")
    b.add_argument("--with-cache", action="store_true")
    sub.add_parser("verify").add_argument("file")
    r = sub.add_parser("restore")
    r.add_argument("file")
    r.add_argument("--yes", action="store_true")
    sub.add_parser("export-json").add_argument("out")
    sub.add_parser("import-json").add_argument("file")
    a = sub.add_parser("create-admin")
    a.add_argument("username")
    a.add_argument("name")
    sub.add_parser("reset-password").add_argument("username")
    sub.add_parser("reindex")
    args = ap.parse_args(argv)
    if args.cmd == "status":
        status()
    elif args.cmd == "migrate":
        rt = runtime()
        print(f"Schema version {rt.db.version()} (up to date).")
    elif args.cmd == "backup":
        backup(Path(args.out), not args.no_files, args.with_cache)
    elif args.cmd == "verify":
        return 1 if verify(Path(args.file)) else 0
    elif args.cmd == "restore":
        restore(Path(args.file), args.yes)
    elif args.cmd == "export-json":
        export_json(Path(args.out))
    elif args.cmd == "import-json":
        import_json(Path(args.file))
    elif args.cmd == "create-admin":
        create_admin(args.username, args.name, os.getenv("BOQ_NEW_PASSWORD"))
    elif args.cmd == "reset-password":
        reset_password(args.username, os.getenv("BOQ_NEW_PASSWORD"))
    elif args.cmd == "reindex":
        reindex()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
