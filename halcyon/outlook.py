"""Outlook / Microsoft 365 mailbox through Microsoft Graph with a delegated sign-in.

Works for personal Outlook.com / Hotmail accounts and for work or school accounts.
Outlook.com no longer accepts passwords or app passwords for IMAP (OAuth2 only since
16 Sept 2024), so the agent signs in once with a device code and keeps a refresh token.
The password is never given to the agent.

One-time setup (about 5 minutes, free):
  1. https://portal.azure.com > App registrations > New registration
       Supported account types: "Personal Microsoft accounts only"
       (or "Any organizational directory and personal Microsoft accounts")
  2. Authentication > Advanced settings > Allow public client flows: Yes
  3. API permissions > Add > Microsoft Graph > Delegated: Mail.Read  (offline_access is added automatically)
  4. Copy the Application (client) ID into .env as OUTLOOK_CLIENT_ID
  5. python -m halcyon.outlook_login   -> open the link, enter the code, sign in with the test account
Then set MAIL_MODE=outlook and start the server.

Only messages received today whose subject contains ORDER_SUBJECT (default "New order arrived",
case-insensitive) are downloaded. Nothing is marked as read, moved or deleted.
"""
from __future__ import annotations
import json, os, time, urllib.parse, urllib.request, urllib.error
from datetime import datetime, timezone
from .common import OUT

TOKEN_FILE = OUT / "outlook_token.json"
SCOPE = "offline_access Mail.Read"


def _tenant(): return os.environ.get("OUTLOOK_TENANT", "consumers")   # "consumers" for Outlook.com, "common" or a tenant id for work accounts
def _cid():
    cid = os.environ.get("OUTLOOK_CLIENT_ID", "")
    if not cid: raise RuntimeError("OUTLOOK_CLIENT_ID is not set in .env (see halcyon/outlook.py for the 5-minute setup)")
    return cid


def _post(url, data):
    req = urllib.request.Request(url, urllib.parse.urlencode(data).encode(), {"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r: return json.load(r)
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode() or "{}")


def start_device_flow(client_id=None, tenant=None):
    """Step 1 of sign-in: ask Microsoft for a one-time code the user enters at microsoft.com/devicelogin."""
    cid = client_id or _cid(); ten = tenant or _tenant()
    dc = _post(f"https://login.microsoftonline.com/{ten}/oauth2/v2.0/devicecode", dict(client_id=cid, scope=SCOPE))
    if "device_code" not in dc:
        raise RuntimeError(dc.get("error_description") or dc.get("error") or str(dc))
    dc.update(client_id=cid, tenant=ten, started=time.time())
    return dc


def poll_device_flow(dc):
    """Step 2: wait until the user has signed in; stores the refresh token. Returns the token response."""
    interval = int(dc.get("interval", 5)); deadline = dc["started"] + int(dc.get("expires_in", 900))
    while time.time() < deadline:
        time.sleep(interval)
        tok = _post(f"https://login.microsoftonline.com/{dc['tenant']}/oauth2/v2.0/token",
                    dict(grant_type="urn:ietf:params:oauth:grant-type:device_code", client_id=dc["client_id"], device_code=dc["device_code"]))
        if "access_token" in tok:
            _save(tok); return tok
        if tok.get("error") == "authorization_pending": continue
        if tok.get("error") == "slow_down": interval += 5; continue
        raise RuntimeError(tok.get("error_description") or tok.get("error") or str(tok))
    raise RuntimeError("The sign-in code expired before it was used. Start again.")


def device_login():
    """Command-line sign-in: python -m halcyon.outlook_login"""
    dc = start_device_flow()
    print("\n" + dc["message"] + "\n")
    poll_device_flow(dc)
    print("Signed in. The agent can now read the mailbox.")


def sign_out():
    if TOKEN_FILE.exists(): TOKEN_FILE.unlink()


def _save(tok):
    OUT.mkdir(exist_ok=True)
    TOKEN_FILE.write_text(json.dumps(dict(refresh_token=tok["refresh_token"], access_token=tok["access_token"],
                                          expires_at=time.time() + int(tok.get("expires_in", 3600)))))
    try: os.chmod(TOKEN_FILE, 0o600)
    except Exception: pass


class OutlookMailbox:
    name = "outlook"

    def __init__(self):
        self.subject = os.environ.get("ORDER_SUBJECT", "New order arrived").strip().lower()
        self.account = None

    def _token(self):
        if not TOKEN_FILE.exists():
            raise RuntimeError("Outlook is not signed in yet. Run: python -m halcyon.outlook_login")
        t = json.loads(TOKEN_FILE.read_text())
        if time.time() < t.get("expires_at", 0) - 120: return t["access_token"]
        tok = _post(f"https://login.microsoftonline.com/{_tenant()}/oauth2/v2.0/token",
                    dict(grant_type="refresh_token", client_id=_cid(), refresh_token=t["refresh_token"], scope=SCOPE))
        if "access_token" not in tok:
            raise RuntimeError("Outlook sign-in has expired. Run: python -m halcyon.outlook_login  (" + tok.get("error", "") + ")")
        if "refresh_token" not in tok: tok["refresh_token"] = t["refresh_token"]
        _save(tok); return tok["access_token"]

    def _get(self, url, raw=False):
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + self._token()})
        with urllib.request.urlopen(req, timeout=30) as r: return r.read() if raw else json.load(r)

    def matches(self, subject: str) -> bool:
        return self.subject in (subject or "").lower()

    def fetch(self):
        if self.account is None:
            try: me = self._get("https://graph.microsoft.com/v1.0/me?$select=mail,userPrincipalName"); self.account = me.get("mail") or me.get("userPrincipalName")
            except Exception: self.account = ""
        start = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
        q = urllib.parse.urlencode({"$filter": f"receivedDateTime ge {start}", "$orderby": "receivedDateTime desc",
                                    "$select": "id,subject,internetMessageId,receivedDateTime", "$top": "100"})
        url = f"https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages?{q}"
        out = []; scanned = 0; recent = []
        while url:
            page = self._get(url)
            for m in page.get("value", []):
                scanned += 1; hit = self.matches(m.get("subject"))
                if len(recent) < 15: recent.append(dict(subject=m.get("subject") or "(no subject)", received=m.get("receivedDateTime"), matched=hit))
                if not hit: continue          # only "New order arrived" emails
                raw = self._get(f"https://graph.microsoft.com/v1.0/me/messages/{m['id']}/$value", raw=True)
                out.append((m.get("internetMessageId") or m["id"], raw))
            url = page.get("@odata.nextLink")
        self.stats = dict(scanned_today=scanned, matched_today=len(out), subject=self.subject, recent_inbox=recent)
        return out
