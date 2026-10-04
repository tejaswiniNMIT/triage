"""
Exchange a Zoho grant code for a refresh token, and check the scopes came back.

    python scripts/zoho_token.py                # prompts for the code, prints the result
    python scripts/zoho_token.py --write-env    # also writes ZOHO_REFRESH_TOKEN into .env

The grant code is typed at a prompt rather than passed on the command line, so
there is nothing for the shell to quote, glob or truncate - the failure mode that
eats most first attempts - and the code never lands in your shell history.

Needs ZOHO_CLIENT_ID and ZOHO_CLIENT_SECRET in the environment:
    set -a; source .env; set +a
"""
import argparse
import json
import os
import re
import sys
import time
from getpass import getpass
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.config import load_dotenv  # noqa: E402

load_dotenv()

CODE_SHAPE = re.compile(r"^1000\.[0-9a-fA-F]{16,}\.[0-9a-fA-F]{16,}$")
REQUIRED_SCOPES = {
    "ZohoMail.accounts.READ", "ZohoMail.folders.READ", "ZohoMail.messages.READ",
    "ZohoMail.messages.UPDATE", "ZohoMail.messages.CREATE",
    "WorkDrive.files.CREATE", "WorkDrive.files.READ", "WorkDrive.links.CREATE",
}
# What each Zoho error actually means in this flow.
HINTS = {
    "invalid_client": "ZOHO_CLIENT_ID / ZOHO_CLIENT_SECRET are wrong, empty, or from another "
                      "datacenter. Check them against the Self Client page.",
    "invalid_code": "The code is well-formed but expired or already used. Codes last ~10 minutes "
                    "and are single-use - generate a fresh one.",
    "general_error": "The code was rejected. Most often the client was registered in a different "
                     "datacenter than the one being posted to - the sweep below settles that. "
                     "Otherwise: code already spent (single-use), expired (3 minutes by DEFAULT, "
                     "not 10 - set the duration explicitly), or truncated on paste.",
    "invalid_redirect_uri": "The --redirect-uri must match the client's registered value exactly, "
                            "including scheme, any trailing slash, and the one used to get the code.",
}


def auth_url(dc: str, redirect_uri: str) -> str:
    """The browser approval URL, for clients that are not Self Clients.

    access_type=offline is what makes Zoho return a refresh_token at all; without
    it the exchange succeeds and hands back only a one-hour access token.
    """
    from urllib.parse import urlencode
    q = urlencode({"response_type": "code", "client_id": os.environ["ZOHO_CLIENT_ID"],
                   "scope": ",".join(sorted(REQUIRED_SCOPES)), "redirect_uri": redirect_uri,
                   "access_type": "offline", "prompt": "consent"})
    return f"https://accounts.zoho.{dc}/oauth/v2/auth?{q}"


def exchange(dc: str, code: str, redirect_uri: str | None) -> tuple[int, dict | str]:
    data = {"grant_type": "authorization_code", "code": code,
            "client_id": os.environ["ZOHO_CLIENT_ID"],
            "client_secret": os.environ["ZOHO_CLIENT_SECRET"]}
    if redirect_uri:
        data["redirect_uri"] = redirect_uri
    r = requests.post(f"https://accounts.zoho.{dc}/oauth/v2/token", data=data, timeout=45)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, r.text or "<empty body>"


def verify(dc: str, access_token: str) -> None:
    """Spend the access token on the calls the agent actually makes.

    Also prints the two ids step 4 asks for, so the mail scopes are proved and the
    config values are collected in one pass.
    """
    h = {"Authorization": f"Zoho-oauthtoken {access_token}"}
    print("\nverifying against the live APIs:")
    try:
        r = requests.get(f"https://mail.zoho.{dc}/api/accounts", headers=h, timeout=45)
    except requests.RequestException as exc:
        print(f"  mail       unreachable ({type(exc).__name__})")
        return
    if not r.ok:
        print(f"  mail       HTTP {r.status_code} - {r.text[:200]}")
        print("  ^ the Mail scopes did not come through, or the plan does not expose the API")
        return
    accounts = r.json().get("data", [])
    print(f"  mail       OK - {len(accounts)} account(s)")
    for acc in accounts:
        aid = acc.get("accountId")
        print(f"    ZOHO_ACCOUNT_ID={aid}    {acc.get('primaryEmailAddress','')}")
        fr = requests.get(f"https://mail.zoho.{dc}/api/accounts/{aid}/folders", headers=h, timeout=45)
        if not fr.ok:
            print(f"    folders  HTTP {fr.status_code} - ZohoMail.folders.READ missing?")
            continue
        for f in fr.json().get("data", []):
            print(f"    ZOHO_FOLDER_ID={f.get('folderId'):<22} {f.get('folderName')}")

    wd = os.environ.get("ZOHO_WORKDRIVE_FOLDER_ID")
    if not wd:
        print("  workdrive  skipped - set ZOHO_WORKDRIVE_FOLDER_ID to check it too")
        return
    try:
        r = requests.get(f"https://www.zohoapis.{dc}/workdrive/api/v1/files/{wd}",
                         headers={**h, "Accept": "application/vnd.api+json"}, timeout=45)
        print(f"  workdrive  {'OK - folder readable' if r.ok else f'HTTP {r.status_code} - {r.text[:200]}'}")
    except requests.RequestException as exc:
        print(f"  workdrive  unreachable ({type(exc).__name__})")


def peek(dc: str, access_token: str, limit: int = 15, everyone: bool = False) -> None:
    """Show the mail the agent would consider, read or unread.

    Scoped to ZOHO_SENDER_FILTER via server-side search by default, so debugging does
    not pull anybody else's mail either. --peek-all widens it to the whole folder,
    which is the only way to discover an unknown sender.
    """
    h = {"Authorization": f"Zoho-oauthtoken {access_token}"}
    acc = os.environ.get("ZOHO_ACCOUNT_ID")
    folder = os.environ.get("ZOHO_FOLDER_ID")
    want = (os.environ.get("ZOHO_SENDER_FILTER") or "").lower()
    if not acc:
        print("! ZOHO_ACCOUNT_ID not set - run --check first to find it")
        return
    if want and not everyone:
        print(f"scoped to sender:{want} (server-side). --peek-all to list the whole folder.")
        params = {"searchKey": f"sender:{want}", "limit": limit, "start": 1,
                  "receivedTime": int(time.time() * 1000)}
        url = f"https://mail.zoho.{dc}/api/accounts/{acc}/messages/search"
    else:
        print("listing the WHOLE folder - every sender in it" if want else
              "ZOHO_SENDER_FILTER not set - listing the whole folder to find it")
        params = {"limit": limit, "sortorder": "false"}
        if folder:
            params["folderId"] = folder
        url = f"https://mail.zoho.{dc}/api/accounts/{acc}/messages/view"
    r = requests.get(url, headers=h, params=params, timeout=45)
    if not r.ok:
        print(f"! HTTP {r.status_code}: {r.text[:300]}")
        return
    msgs = r.json().get("data") or []
    print(f"\n{len(msgs)} message(s) in folder {folder or '(all folders)'}"
          f"{'' if everyone or not want else f' from {want}'}:")
    if msgs:
        # the read/unread field name varies by account; show what this one returns so
        # the poll's unread check can be confirmed against real data
        st = {k: v for k, v in msgs[0].items() if "status" in k.lower()}
        print(f"  (read-state fields on the newest message: {st})")
    if not msgs:
        print("  (empty - wrong folder id, or the mail never arrived)")
    for m in msgs:
        sender = (m.get("fromAddress") or "").strip("<>")
        unread = str(m.get("status2", m.get("status", ""))) in ("0", "unread")
        flag = "MATCHES" if want and want in sender.lower() else "       "
        print(f"  {flag} {'unread' if unread else 'READ  '} {sender:38s} "
              f"{(m.get('subject') or '')[:44]:44s} {'attach' if m.get('hasAttachment') else '-'}")
    if want:
        print(f"\ncurrent ZOHO_SENDER_FILTER={want!r} - it must be a substring of the sender "
              f"address above.\nThe agent also needs the mail UNREAD and carrying a "
              f".csv/.xlsx/.xls/.xlsm/.txt attachment.")
    else:
        print("\nZOHO_SENDER_FILTER is not set - copy the sender you want into .env.")


def write_env(env_path: Path, token: str) -> None:
    line = f"ZOHO_REFRESH_TOKEN={token}"
    text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    if re.search(r"^ZOHO_REFRESH_TOKEN=.*$", text, flags=re.M):
        text = re.sub(r"^ZOHO_REFRESH_TOKEN=.*$", line, text, flags=re.M)
    else:
        text = text.rstrip("\n") + ("\n" if text else "") + line + "\n"
    env_path.write_text(text, encoding="utf-8")
    env_path.chmod(0o600)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dc", default="in", help="datacenter: in | eu | com | com.au (default: in)")
    ap.add_argument("--redirect-uri", default=None,
                    help="the client's registered redirect URI (Web-based clients only)")
    ap.add_argument("--auth-url", action="store_true",
                    help="print the browser approval URL first - for Web-based clients, which "
                         "have no Generate Code tab. Requires --redirect-uri.")
    ap.add_argument("--peek", action="store_true",
                    help="show the mail the agent would consider - scoped to ZOHO_SENDER_FILTER "
                         "server-side. Use when a run says 'no matching mail'")
    ap.add_argument("--peek-all", action="store_true",
                    help="widen --peek to every sender in the folder, to discover an unknown "
                         "sender address. Reads mail the agent itself never touches")
    ap.add_argument("--check", action="store_true",
                    help="verify the ZOHO_REFRESH_TOKEN already in the environment against the "
                         "live APIs - no grant code needed")
    ap.add_argument("--ask-creds", action="store_true",
                    help="type the client id and secret at a prompt instead of reading the "
                         "environment - use when a stale export may not match the client the "
                         "code was generated from")
    ap.add_argument("--no-dc-sweep", action="store_true",
                    help="on failure, do not retry the same code against the other datacenters")
    ap.add_argument("--write-env", action="store_true",
                    help="write ZOHO_REFRESH_TOKEN into .env (replaces any existing line)")
    a = ap.parse_args()

    if a.auth_url and not a.redirect_uri:
        print("! --auth-url needs --redirect-uri (copy it from the client's page in the console)")
        return 2

    if a.ask_creds:
        # Read them off the client's page in the console, in the same sitting as the code,
        # so a stale shell export cannot be the thing that is wrong.
        print("Copy these from the SAME client page you will click Generate Code on.")
        os.environ["ZOHO_CLIENT_ID"] = input("  Client ID     : ").strip().strip("\"'")
        os.environ["ZOHO_CLIENT_SECRET"] = getpass("  Client Secret : ").strip().strip("\"'")

    missing = [k for k in ("ZOHO_CLIENT_ID", "ZOHO_CLIENT_SECRET") if not os.environ.get(k)]
    if missing:
        print(f"! {' and '.join(missing)} not set in this shell.\n"
              f"  Run:  set -a; source .env; set +a   (or export them by hand)")
        return 2

    if a.check or a.peek or a.peek_all:
        if not os.environ.get("ZOHO_REFRESH_TOKEN"):
            print("! ZOHO_REFRESH_TOKEN not set. Run:  set -a; source .env; set +a")
            return 2
        r = requests.post(f"https://accounts.zoho.{a.dc}/oauth/v2/token",
                          data={"grant_type": "refresh_token",
                                "refresh_token": os.environ["ZOHO_REFRESH_TOKEN"],
                                "client_id": os.environ["ZOHO_CLIENT_ID"],
                                "client_secret": os.environ["ZOHO_CLIENT_SECRET"]}, timeout=45)
        d = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if "access_token" not in d:
            print(f"! refresh failed: {json.dumps(d)[:300] or r.text[:300]}")
            return 1
        print(f"refresh token OK - new access token, expires_in {d.get('expires_in')}s")
        if a.check:
            verify(a.dc, d["access_token"])
        if a.peek or a.peek_all:
            peek(a.dc, d["access_token"], everyone=a.peek_all)
        return 0

    # The client is validated before the code, so a general_error with valid creds means
    # the code belongs to a DIFFERENT client than the one whose id is exported here.
    # Print enough to compare against the console page without spilling the secret.
    cid, sec = os.environ["ZOHO_CLIENT_ID"], os.environ["ZOHO_CLIENT_SECRET"]
    print(f"  client_id: {cid}")
    print(f"  secret   : {len(sec)} chars, ends {sec[-4:]}"
          + ("   ! has surrounding whitespace" if sec != sec.strip() else ""))
    print("  ^ this must be the SAME client whose Generate Code tab you used\n")

    if a.auth_url:
        print("\n1. Open this URL in the browser where you are signed into Zoho:\n")
        print(f"   {auth_url(a.dc, a.redirect_uri)}\n")
        print("2. Approve the scopes. The browser lands on your redirect URI - the page itself")
        print("   does not matter, it may even 404.")
        print("3. Copy the value of code= from the address bar (up to the next &).\n")

    raw = input("Paste the 10-minute grant code, then Enter: ")
    # copying a wrapped code out of the console brings line breaks with it; a real
    # code has no internal whitespace, so stripping it can only help.
    code = re.sub(r"\s+", "", raw).strip("<>\"'")
    if "code=" in code:                    # whole redirect URL pasted - dig the code out
        from urllib.parse import parse_qs, urlparse
        code = parse_qs(urlparse(code).query).get("code", [code])[0]
        print(f"  (extracted the code from the pasted URL)")
    if not code or code.isupper():
        print(f"! '{code}' is not a grant code - looks like a placeholder. "
              f"Generate one at https://api-console.zoho.{a.dc}")
        return 2
    print(f"  code: {len(code)} chars, {code[:10]}...{code[-6:]}")
    if not CODE_SHAPE.match(code):
        print("  ! unusual shape - a Zoho grant code is 1000.<32 hex>.<32 hex>, ~70 chars.\n"
              "    A short one means the paste was truncated: the console wraps the code, and\n"
              "    copying wrapped text can drop everything after the first line.")

    status, body = exchange(a.dc, code, a.redirect_uri)
    print(f"\nHTTP {status} from accounts.zoho.{a.dc}")

    # A code minted in one datacenter is worthless in another, and the console will
    # happily hand you a .com code while you post to .in. A rejected code is not
    # consumed, so sweeping the other DCs with the same code costs nothing and settles
    # the question outright.
    if isinstance(body, dict) and "refresh_token" not in body and not a.no_dc_sweep:
        print(f"  ({body.get('error')}) - trying the other datacenters with the same code")
        # only the DCs that fit the accounts.zoho.<dc> pattern the connectors build
        for dc in [d for d in ("in", "com", "eu", "com.au", "jp", "sa") if d != a.dc]:
            try:
                st2, b2 = exchange(dc, code, a.redirect_uri)
            except requests.RequestException as exc:
                print(f"    accounts.zoho.{dc:7s} unreachable ({type(exc).__name__})")
                continue
            err = b2.get("error", "?") if isinstance(b2, dict) else "non-JSON"
            ok = isinstance(b2, dict) and "refresh_token" in b2
            print(f"    accounts.zoho.{dc:7s} {'REFRESH TOKEN' if ok else err}")
            if ok:
                print(f"\n>>> Your account lives in the '{dc}' datacenter, not '{a.dc}'.")
                print(f">>> Set datacenter: {dc} under BOTH source_config and sink_config in "
                      f"config/settings.yaml,")
                print(f">>> and use --dc {dc} here from now on.")
                a.dc, status, body = dc, st2, b2
                break
    if not isinstance(body, dict):
        print(f"! Non-JSON response: {body[:400]}")
        return 1

    if "refresh_token" not in body:
        err = body.get("error", "unknown")
        print(f"! Zoho said: {err}")
        if err in HINTS:
            print(f"  {HINTS[err]}")
        print(f"  full response: {json.dumps(body)[:400]}")
        return 1

    print("\nrefresh_token (does not expire - copy it now):\n")
    print(f"  {body['refresh_token']}\n")
    print(f"access_token ok, expires_in {body.get('expires_in')}s")

    # Zoho's token response for this flow carries no `scope` field, so the grant cannot
    # be inspected - the only honest check is to spend the access token on a real call.
    if body.get("scope"):
        gaps = REQUIRED_SCOPES - set(body["scope"].split(","))
        print(f"! missing scopes: {', '.join(sorted(gaps))}" if gaps else "all 8 scopes granted")
    else:
        verify(a.dc, body["access_token"])

    if a.write_env:
        env = ROOT / ".env"
        write_env(env, body["refresh_token"])
        print(f"\nwritten to {env} (mode 600). Reload it:  set -a; source .env; set +a")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
