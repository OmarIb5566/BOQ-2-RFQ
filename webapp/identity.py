"""Sign-in back ends: local accounts (default) and Active Directory / LDAP.

    BOQ_AUTH = local          user names + passwords kept by this app (the administrator creates the accounts)
    BOQ_AUTH = ldap           company accounts; nobody can sign in with a local password
    BOQ_AUTH = ldap+local     company accounts first, local accounts as a fallback (e.g. the first administrator)

LDAP settings (all in boq2rfq.toml, [sign_in] section — values come from IT):
    BOQ_LDAP_URL           ldaps://dc01.company.local:636
    BOQ_LDAP_USER_DN       "{username}@company.local"   (UPN)  or  "uid={username},ou=people,dc=company,dc=local"
    BOQ_LDAP_BASE_DN       dc=company,dc=local          (where users are searched)
    BOQ_LDAP_USER_FILTER   (sAMAccountName={username})
    BOQ_LDAP_ROLE_MAP      "CN=BOQ-Admins,OU=Groups,DC=company,DC=local=admin; CN=BOQ-Managers,…=manager;
                            CN=BOQ-Engineers,…=engineer; CN=BOQ-Viewers,…=viewer"
    BOQ_LDAP_DEFAULT_ROLE  role for a company user in none of the mapped groups ("" = may not sign in)

A company user is created in this app on first sign-in (no password stored); the role follows the AD groups at
every sign-in. Needs the `ldap3` package on the server (pip install ldap3) — not needed for local accounts.
NOT TESTED against a real directory here (no directory server available); covered by tests with a fake one.
"""
from __future__ import annotations

import os
import re
from typing import Optional

from werkzeug.security import check_password_hash

from .perms import ROLES

LDAP_MARK = "!ldap"        # pw_hash of accounts that sign in through the directory


def mode() -> str:
    m = os.getenv("BOQ_AUTH", "local").strip().lower()
    return m if m in ("local", "ldap", "ldap+local") else "local"


def authenticate(db, username: str, password: str, dummy_hash: str) -> tuple[Optional[dict], str]:
    """(user row, '') when the password is right, else (None, reason for the log)."""
    m = mode()
    if m in ("ldap", "ldap+local") and password:
        try:
            info = ldap_check(username, password)
        except LdapUnavailable as e:
            info, err = None, str(e)
        else:
            err = ""
        if info is not None:
            return provision(db, username, info), ""
        if m == "ldap":
            check_password_hash(dummy_hash, password)
            return None, err or "directory refused"
    u = db.one("SELECT * FROM users WHERE username=? AND active=1", (username,))
    if u and u["pw_hash"] == LDAP_MARK:
        u = None                                      # directory accounts have no local password
    ok = check_password_hash(u["pw_hash"] if u else dummy_hash, password or "")
    return (dict(u), "") if u and ok else (None, "wrong password")


class LdapUnavailable(RuntimeError):
    pass


def role_map() -> list[tuple[str, str]]:
    out = []
    for part in os.getenv("BOQ_LDAP_ROLE_MAP", "").split(";"):
        if "=" not in part:
            continue
        dn, role = part.strip().rsplit("=", 1)
        if role.strip() in ROLES:
            out.append((dn.strip().lower(), role.strip()))
    return out


def role_for(groups: list[str]) -> Optional[str]:
    gs = {g.lower() for g in groups}
    best = None
    for dn, role in role_map():
        if dn in gs and (best is None or ROLES.index(role) < ROLES.index(best)):
            best = role                                # the most powerful mapped group wins
    if best:
        return best
    d = os.getenv("BOQ_LDAP_DEFAULT_ROLE", "").strip()
    return d if d in ROLES else None


_CONNECT = None          # tests replace this with a fake directory


def ldap_check(username: str, password: str) -> Optional[dict]:
    """Bind as the user; return {'full_name', 'groups', 'role'} or None when the directory says no."""
    if not re.fullmatch(r"[A-Za-z0-9._@-]{1,80}", username or ""):
        return None
    url = os.getenv("BOQ_LDAP_URL", "")
    if not url:
        raise LdapUnavailable("BOQ_LDAP_URL is not set")
    connect = _CONNECT or _ldap3_connect
    conn = connect(url, os.getenv("BOQ_LDAP_USER_DN", "{username}").format(username=username), password)
    if conn is None:
        return None
    base = os.getenv("BOQ_LDAP_BASE_DN", "")
    flt = os.getenv("BOQ_LDAP_USER_FILTER", "(sAMAccountName={username})").format(username=_escape(username))
    entry = conn.find_user(base, flt) or {}
    groups = [str(x) for x in entry.get("memberOf", [])]
    role = role_for(groups)
    if role is None:
        return None
    return {"full_name": str(entry.get("displayName") or username), "groups": groups, "role": role}


def _escape(v: str) -> str:
    return "".join(f"\\{ord(c):02x}" if c in "*()\\\0" else c for c in v)


def _ldap3_connect(url: str, user_dn: str, password: str):
    try:
        import ldap3
    except ImportError as e:
        raise LdapUnavailable("the ldap3 package is not installed on the server") from e
    server = ldap3.Server(url, get_info=ldap3.NONE, connect_timeout=5)
    try:
        conn = ldap3.Connection(server, user=user_dn, password=password, auto_bind=True, receive_timeout=10)
    except ldap3.core.exceptions.LDAPBindError:
        return None
    except ldap3.core.exceptions.LDAPException as e:
        raise LdapUnavailable(f"directory not reachable: {e}") from e

    class _Conn:
        def find_user(self, base, flt):
            conn.search(base, flt, attributes=["displayName", "memberOf"])
            if not conn.entries:
                return None
            e = conn.entries[0]
            return {"displayName": str(e.displayName) if "displayName" in e else "",
                    "memberOf": list(e.memberOf.values) if "memberOf" in e else []}
    return _Conn()


def provision(db, username: str, info: dict) -> dict:
    from .db import now
    u = db.one("SELECT * FROM users WHERE username=?", (username,))
    if u is None:
        db.x("INSERT INTO users (username, full_name, pw_hash, role, created) VALUES (?,?,?,?,?)",
             (username, info["full_name"][:80], LDAP_MARK, info["role"], now()))
    else:
        db.x("UPDATE users SET role=?, full_name=? WHERE id=?", (info["role"], info["full_name"][:80] or u["full_name"],
                                                               u["id"]))
    u = db.one("SELECT * FROM users WHERE username=?", (username,))
    if not u["active"]:
        return None
    return dict(u)
