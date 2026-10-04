"""
Source connectors - where the watchlist attachments come from.

Every connector implements the same poll() contract, so the orchestrator is
identical whether the files arrive from Zoho Mail, Outlook, Teams or a manual
drag-and-drop in the UI.
"""
from __future__ import annotations

import base64
import os
import time
from pathlib import Path

import requests

from .base import Attachment, InboundMessage, SourceConnector, register

TIMEOUT = 45


# --------------------------------------------------------------------------- #
@register("source", "manual_upload")
class ManualUploadSource(SourceConnector):
    """Files handed in by the UI. Also the offline demo path."""
    name = "manual_upload"

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.paths: list[str] = cfg.get("paths", [])

    def poll(self, since: str | None = None) -> list[InboundMessage]:
        atts = []
        for p in self.paths:
            p = Path(p)
            atts.append(Attachment(p.name, p.read_bytes()))
        if not atts:
            return []
        return [InboundMessage(f"manual-{int(time.time())}", self.cfg.get("sender", "manual@local"),
                               "Manual upload", time.strftime("%Y-%m-%dT%H:%M:%SZ"), atts)]

    def mark_processed(self, message_id: str) -> None:
        return None


# --------------------------------------------------------------------------- #
@register("source", "zoho_mail")
class ZohoMailSource(SourceConnector):
    """
    Zoho Mail REST API.
      OAuth scopes : ZohoMail.messages.READ, ZohoMail.accounts.READ, ZohoMail.folders.READ
      Base URL     : https://mail.zoho.<dc>/api   (dc = com | eu | in | com.au)
    Flow: search folder -> list messages from sender -> fetch attachment info -> download.
    """
    name = "zoho_mail"

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.dc = cfg.get("datacenter", "eu")
        self.base = f"https://mail.zoho.{self.dc}/api"
        self.account_id = cfg["account_id"]
        self.folder_id = cfg.get("folder_id")
        self.sender_filter = cfg["sender_filter"].lower()
        if "${" in self.sender_filter:
            # an uninterpolated placeholder would match nothing and look like an empty
            # mailbox - name the missing variable instead
            raise KeyError(f"sender_filter is still the placeholder {cfg['sender_filter']!r} - "
                           f"set ZOHO_SENDER_FILTER in .env")
        self.subject_filter = cfg.get("subject_filter", "")
        # "search" asks Zoho for this sender's mail only, so nobody else's messages are
        # ever transferred. "list" is the old behaviour: fetch the folder, filter locally.
        self.poll_mode = cfg.get("poll_mode", "search")
        self._token = None
        self._token_exp = 0.0

    # -- auth ---------------------------------------------------------------
    def _access_token(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        r = requests.post(
            f"https://accounts.zoho.{self.dc}/oauth/v2/token",
            data={"refresh_token": os.environ["ZOHO_REFRESH_TOKEN"],
                  "client_id": os.environ["ZOHO_CLIENT_ID"],
                  "client_secret": os.environ["ZOHO_CLIENT_SECRET"],
                  "grant_type": "refresh_token"}, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        self._token, self._token_exp = data["access_token"], time.time() + data.get("expires_in", 3600)
        return self._token

    def _headers(self) -> dict:
        return {"Authorization": f"Zoho-oauthtoken {self._access_token()}"}

    # -- poll ---------------------------------------------------------------
    def _search_key(self) -> str:
        """Zoho search syntax: `param:value`, ANDed with `::`.

        Pushing the sender down to the server is the point: the agent is a control on
        one reporting feed, and it has no business receiving anybody else's mail from
        the mailbox it is pointed at.
        """
        parts = [f"sender:{self.sender_filter}"]
        if self.subject_filter:
            parts.append(f"subject:{self.subject_filter}")
        parts.append("has:attachment")
        return "::".join(parts)

    def _fetch_candidates(self) -> list[dict]:
        limit = self.cfg.get("limit", 25)
        if self.poll_mode == "list":                     # legacy: whole folder, filter here
            params = {"limit": limit, "sortorder": "false", "status": "unread"}
            if self.folder_id:
                params["folderId"] = self.folder_id
            url = f"{self.base}/accounts/{self.account_id}/messages/view"
        else:
            # receivedTime is an upper bound and defaults to 2 minutes ago, which would
            # hide the mail a webhook run was triggered by - ask up to "now" instead.
            params = {"searchKey": self._search_key(), "limit": limit, "start": 1,
                      "includeto": "false", "receivedTime": int(time.time() * 1000)}
            url = f"{self.base}/accounts/{self.account_id}/messages/search"
        r = requests.get(url, headers=self._headers(), params=params, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json().get("data") or []

    @staticmethod
    def _is_unread(msg: dict) -> bool:
        # Zoho marks read state in status2 ("0" unread / "1" read); treat anything
        # unrecognised as unread so a batch is never silently skipped.
        return str(msg.get("status2", msg.get("status", "0"))).lower() in ("0", "unread", "none")

    def poll(self, since: str | None = None) -> list[InboundMessage]:
        out = []
        for msg in self._fetch_candidates():
            sender = (msg.get("fromAddress") or "").lower()
            subject = msg.get("subject", "")
            # re-check server-side filters locally: never act on a message the search
            # returned but the configured filters do not actually cover
            if self.sender_filter not in sender:
                continue
            if self.subject_filter and self.subject_filter.lower() not in subject.lower():
                continue
            if self.folder_id and str(msg.get("folderId", self.folder_id)) != str(self.folder_id):
                continue
            if self.poll_mode != "list" and not self._is_unread(msg):
                continue
            atts = self._attachments(msg["messageId"], msg.get("folderId", self.folder_id))
            if not any(a.is_tabular for a in atts):
                continue
            out.append(InboundMessage(msg["messageId"], sender, subject,
                                      msg.get("receivedTime", ""), atts, raw=msg))
        return out

    def _attachments(self, message_id: str, folder_id: str) -> list[Attachment]:
        info = requests.get(
            f"{self.base}/accounts/{self.account_id}/folders/{folder_id}/messages/{message_id}/attachmentinfo",
            headers=self._headers(), timeout=TIMEOUT)
        info.raise_for_status()
        out = []
        for a in info.json().get("data", {}).get("attachments", []):
            blob = requests.get(
                f"{self.base}/accounts/{self.account_id}/folders/{folder_id}/messages/{message_id}"
                f"/attachments/{a['attachmentId']}",
                headers=self._headers(), timeout=TIMEOUT)
            blob.raise_for_status()
            out.append(Attachment(a["attachmentName"], blob.content,
                                  a.get("attachmentType", "application/octet-stream")))
        return out

    def has_new(self) -> bool:
        """Metadata only - no attachment downloads. Called on every worker tick."""
        for msg in self._fetch_candidates():
            if self.sender_filter not in (msg.get("fromAddress") or "").lower():
                continue
            if self.poll_mode != "list" and not self._is_unread(msg):
                continue
            return True
        return False

    def mark_processed(self, message_id: str) -> None:
        requests.put(f"{self.base}/accounts/{self.account_id}/updatemessage",
                     headers=self._headers(),
                     json={"mode": "markAsRead", "messageId": [message_id]}, timeout=TIMEOUT)


# --------------------------------------------------------------------------- #
@register("source", "outlook_graph")
class OutlookGraphSource(SourceConnector):
    """
    Microsoft Graph mail.
      Permission : Mail.Read (application) + Mail.ReadWrite to mark read
      Token      : client-credentials against login.microsoftonline.com
    """
    name = "outlook_graph"

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.user = cfg["mailbox"]                       # UPN or object id
        self.folder = cfg.get("folder", "Inbox")
        self.sender_filter = cfg["sender_filter"].lower()
        self.base = "https://graph.microsoft.com/v1.0"
        self._token, self._exp = None, 0.0

    def _access_token(self) -> str:
        if self._token and time.time() < self._exp - 60:
            return self._token
        tenant = os.environ["MS_TENANT_ID"]
        r = requests.post(f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
                          data={"client_id": os.environ["MS_CLIENT_ID"],
                                "client_secret": os.environ["MS_CLIENT_SECRET"],
                                "scope": "https://graph.microsoft.com/.default",
                                "grant_type": "client_credentials"}, timeout=TIMEOUT)
        r.raise_for_status()
        d = r.json()
        self._token, self._exp = d["access_token"], time.time() + d.get("expires_in", 3600)
        return self._token

    def _h(self) -> dict:
        return {"Authorization": f"Bearer {self._access_token()}"}

    def poll(self, since: str | None = None) -> list[InboundMessage]:
        flt = [f"from/emailAddress/address eq '{self.sender_filter}'", "hasAttachments eq true", "isRead eq false"]
        if since:
            flt.append(f"receivedDateTime ge {since}")
        r = requests.get(f"{self.base}/users/{self.user}/mailFolders/{self.folder}/messages",
                         headers=self._h(),
                         params={"$filter": " and ".join(flt), "$top": self.cfg.get("limit", 25),
                                 "$select": "id,subject,from,receivedDateTime"}, timeout=TIMEOUT)
        r.raise_for_status()
        out = []
        for m in r.json().get("value", []):
            ar = requests.get(f"{self.base}/users/{self.user}/messages/{m['id']}/attachments",
                              headers=self._h(), timeout=TIMEOUT)
            ar.raise_for_status()
            atts = [Attachment(a["name"], base64.b64decode(a["contentBytes"]),
                               a.get("contentType", "application/octet-stream"))
                    for a in ar.json().get("value", []) if a.get("contentBytes")]
            if not any(a.is_tabular for a in atts):
                continue
            out.append(InboundMessage(m["id"], m["from"]["emailAddress"]["address"],
                                      m.get("subject", ""), m.get("receivedDateTime", ""), atts, raw=m))
        return out

    def mark_processed(self, message_id: str) -> None:
        requests.patch(f"{self.base}/users/{self.user}/messages/{message_id}",
                       headers={**self._h(), "Content-Type": "application/json"},
                       json={"isRead": True}, timeout=TIMEOUT)


# --------------------------------------------------------------------------- #
@register("source", "imap")
class ImapSource(SourceConnector):
    """Vendor-neutral fallback. Works with Zoho, M365, Google, anything."""
    name = "imap"

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def poll(self, since: str | None = None) -> list[InboundMessage]:
        import email
        import imaplib
        from email.header import decode_header

        host, port = self.cfg.get("host", "imap.zoho.eu"), self.cfg.get("port", 993)
        box = imaplib.IMAP4_SSL(host, port)
        box.login(os.environ["IMAP_USER"], os.environ["IMAP_PASSWORD"])
        box.select(self.cfg.get("folder", "INBOX"))
        typ, data = box.search(None, f'(UNSEEN FROM "{self.cfg["sender_filter"]}")')
        out = []
        for num in data[0].split():
            typ, raw = box.fetch(num, "(RFC822)")
            msg = email.message_from_bytes(raw[0][1])
            atts = []
            for part in msg.walk():
                fn = part.get_filename()
                if fn:
                    fn = decode_header(fn)[0][0]
                    fn = fn.decode(errors="ignore") if isinstance(fn, bytes) else fn
                    atts.append(Attachment(fn, part.get_payload(decode=True),
                                           part.get_content_type()))
            if any(a.is_tabular for a in atts):
                out.append(InboundMessage(num.decode(), msg.get("From", ""), msg.get("Subject", ""),
                                          msg.get("Date", ""), atts))
        box.logout()
        return out

    def mark_processed(self, message_id: str) -> None:
        return None      # IMAP marks \Seen on fetch
