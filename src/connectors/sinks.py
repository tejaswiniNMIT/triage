"""Sink connectors - where the triaged workbook is delivered."""
from __future__ import annotations

import base64
import html
import mimetypes
import os
import time
from pathlib import Path

import requests

from .base import SinkConnector, register

TIMEOUT = 90
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@register("sink", "local")
class LocalSink(SinkConnector):
    """Writes to a local/mounted folder. Default for dev and for air-gapped runs."""
    name = "local"

    def __init__(self, cfg: dict):
        self.root = Path(cfg.get("root", "./data/outbox"))

    def upload(self, local_path: str, remote_folder: str, metadata: dict | None = None) -> str:
        dest = self.root / remote_folder
        dest.mkdir(parents=True, exist_ok=True)
        target = dest / Path(local_path).name
        target.write_bytes(Path(local_path).read_bytes())
        return str(target.resolve())


@register("sink", "zoho_workdrive")
class ZohoWorkDriveSink(SinkConnector):
    """
    Zoho WorkDrive upload.
      Scope    : WorkDrive.files.CREATE, WorkDrive.files.READ
      Endpoint : POST https://www.zohoapis.<dc>/workdrive/api/v1/upload
                 multipart: content=<file>, parent_id=<folder id>, filename=<name>
      Sharing  : POST /workdrive/api/v1/links   (optional, returns a share URL)
    """
    name = "zoho_workdrive"

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.dc = cfg.get("datacenter", "eu")
        self.parent_id = cfg["folder_id"]
        self.create_share_link = cfg.get("create_share_link", True)
        self._token, self._exp = None, 0.0

    def _access_token(self) -> str:
        if self._token and time.time() < self._exp - 60:
            return self._token
        r = requests.post(f"https://accounts.zoho.{self.dc}/oauth/v2/token",
                          data={"refresh_token": os.environ["ZOHO_REFRESH_TOKEN"],
                                "client_id": os.environ["ZOHO_CLIENT_ID"],
                                "client_secret": os.environ["ZOHO_CLIENT_SECRET"],
                                "grant_type": "refresh_token"}, timeout=TIMEOUT)
        r.raise_for_status()
        d = r.json()
        self._token, self._exp = d["access_token"], time.time() + d.get("expires_in", 3600)
        return self._token

    def upload(self, local_path: str, remote_folder: str, metadata: dict | None = None) -> str:
        h = {"Authorization": f"Zoho-oauthtoken {self._access_token()}"}
        name = Path(local_path).name
        with open(local_path, "rb") as fh:
            r = requests.post(f"https://www.zohoapis.{self.dc}/workdrive/api/v1/upload",
                              headers=h,
                              data={"parent_id": self.parent_id, "filename": name, "override-name-exist": "true"},
                              files={"content": (name, fh, XLSX_MIME)}, timeout=TIMEOUT)
        r.raise_for_status()
        payload = r.json()
        try:
            attrs = payload["data"][0]["attributes"]
            rid = attrs.get("resource_id") or attrs.get("Permalink", "")
        except (KeyError, IndexError):
            return str(payload)
        if self.create_share_link and rid:
            try:
                lr = requests.post(f"https://www.zohoapis.{self.dc}/workdrive/api/v1/links",
                                   headers={**h, "Content-Type": "application/json"},
                                   json={"data": {"attributes": {"resource_id": rid, "link_name": name,
                                                                 "role_id": "34"},  # view-only
                                                  "type": "links"}}, timeout=TIMEOUT)
                if lr.ok:
                    return lr.json()["data"]["attributes"]["link"]
            except requests.RequestException:
                pass
        return f"https://workdrive.zoho.{self.dc}/file/{rid}"


@register("sink", "zoho_workdrive_mail")
class ZohoWorkDriveMailSink(ZohoWorkDriveSink):
    """
    WorkDrive upload, then a Zoho Mail alert naming the high-risk players.
      Scopes   : WorkDrive.files.CREATE, WorkDrive.files.READ, WorkDrive.links.CREATE,
                 ZohoMail.messages.CREATE  (+ ZohoMail.attachments.CREATE if attach_workbook)
      Endpoint : POST https://mail.zoho.<dc>/api/accounts/<accountId>/messages

    One digest mail per run rather than one per player: the workbook stays the
    record of the batch, the mail is only the "look at these now" prompt. The
    upload happens first and its URL is returned even if the mail fails - a run
    is not un-delivered because SMTP had a bad day - but the failure is reported
    on `alert_status` so the orchestrator can surface it instead of swallowing it.
    """
    name = "zoho_workdrive_mail"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.account_id = cfg["account_id"]
        self.from_address = cfg["from_address"]
        rec = cfg["recipients"]
        self.recipients = [r.strip() for r in (rec if isinstance(rec, list) else rec.split(","))
                           if r.strip()]
        # delivery is the last step of a run - catch an unfilled .env now, with the name of
        # the variable, rather than after several minutes of scoring
        for label, val in (("ZOHO_ALERT_FROM", self.from_address),
                           ("ZOHO_ALERT_TO", ",".join(self.recipients))):
            if "${" in val or "@" not in val:
                raise KeyError(f"{label} is not set to an email address (got {val!r}) - fill it "
                               f"in .env; it must be an address this mailbox may send as")
        self.alert_tiers = {t.upper() for t in cfg.get("alert_tiers", ["HIGH"])}
        self.attach_workbook = cfg.get("attach_workbook", False)
        self.send_when_empty = cfg.get("send_when_empty", False)
        self.max_rows = cfg.get("max_rows_in_mail", 50)
        self.alert_status: dict | None = None

    def upload(self, local_path: str, remote_folder: str, metadata: dict | None = None) -> str:
        url = super().upload(local_path, remote_folder, metadata)
        m = metadata or {}
        rows = [r for r in m.get("players", [])
                if str(r.get("RiskTier", "")).upper() in self.alert_tiers]
        rows.sort(key=lambda r: r.get("FinalScore") or 0, reverse=True)
        if not rows and not self.send_when_empty:
            self.alert_status = {"sent": False, "detail": "no players in alert tiers - no mail sent"}
            return url
        try:
            self._send(rows, url, local_path, m)
            self.alert_status = {"sent": True, "players": len(rows),
                                 "detail": f"alert mail for {len(rows)} player(s) -> "
                                           f"{', '.join(self.recipients)}"}
        except (requests.RequestException, KeyError, ValueError) as exc:   # noqa: BLE001
            self.alert_status = {"sent": False, "players": len(rows),
                                 "detail": f"ALERT MAIL FAILED ({type(exc).__name__}: {exc}) - "
                                           f"{len(rows)} high-risk player(s) not notified"}
        return url

    # -- mail ---------------------------------------------------------------
    def _send(self, rows: list[dict], workbook_url: str, local_path: str, m: dict) -> None:
        h = {"Authorization": f"Zoho-oauthtoken {self._access_token()}",
             "Content-Type": "application/json"}
        body = {"fromAddress": self.from_address,
                "toAddress": ",".join(self.recipients),
                "subject": f"[AML] {len(rows)} high-risk player(s) - batch {m.get('run_id','')}",
                "content": self._html(rows, workbook_url, m),
                "mailFormat": "html", "askReceipt": "no"}
        if self.attach_workbook:
            att = self._upload_attachment(local_path, h["Authorization"])
            if att:
                body["attachments"] = [att]
        r = requests.post(f"https://mail.zoho.{self.dc}/api/accounts/{self.account_id}/messages",
                          headers=h, json=body, timeout=TIMEOUT)
        r.raise_for_status()

    def _upload_attachment(self, local_path: str, auth: str) -> dict | None:
        """Two-call flow: stage the file, then reference it in the send payload."""
        name = Path(local_path).name
        with open(local_path, "rb") as fh:
            r = requests.post(f"https://mail.zoho.{self.dc}/api/accounts/{self.account_id}"
                              f"/messages/attachments",
                              headers={"Authorization": auth},
                              params={"fileName": name, "uploadType": "multipart"},
                              files={"attach": (name, fh, XLSX_MIME)}, timeout=TIMEOUT)
        if not r.ok:
            return None
        data = r.json().get("data")
        a = data[0] if isinstance(data, list) and data else data
        if not isinstance(a, dict) or "storeName" not in a:
            return None
        return {"storeName": a["storeName"], "attachmentPath": a.get("attachmentPath", ""),
                "attachmentName": a.get("attachmentName", name)}

    def _html(self, rows: list[dict], workbook_url: str, m: dict) -> str:
        e = html.escape
        head = ("PlayerID", "Shop", "Score", "Critical", "Typology", "Top signals", "SLA")
        cells = []
        for r in rows[:self.max_rows]:
            sigs = ", ".join(str(r.get(f"TopSignal{i}") or "") for i in (1, 2, 3)).strip(", ")
            cells.append("<tr>" + "".join(
                f'<td style="padding:6px 10px;border-bottom:1px solid #e4e4e7">{e(str(v))}</td>'
                for v in (r.get("PlayerID", ""), r.get("Shop", ""),
                          f'{r.get("FinalScore") or 0:.0f}',
                          r.get("CriticalCodes") or "-", r.get("LLM_Typology", ""),
                          sigs, f'{r.get("SLA_Hours", "")}h')) + "</tr>")
        more = (f"<p>+ {len(rows) - self.max_rows} further player(s) - see the workbook.</p>"
                if len(rows) > self.max_rows else "")
        link = (f'<p><a href="{e(workbook_url)}">Open the full triage workbook</a></p>'
                if workbook_url else "")
        return (
            f'<div style="font-family:system-ui,Segoe UI,Arial,sans-serif;font-size:14px;color:#18181b">'
            f'<p><b>AML triage batch {e(str(m.get("run_id","")))}</b><br>'
            f'Population {m.get("total",0)} &middot; HIGH {m.get("high",0)} &middot; '
            f'MEDIUM {m.get("medium",0)} &middot; LOW {m.get("low",0)} &middot; '
            f'critical-signal cases {m.get("critical",0)}</p>'
            f'<table style="border-collapse:collapse;font-size:13px">'
            f'<tr>' + "".join(f'<th style="text-align:left;padding:6px 10px;'
                              f'border-bottom:2px solid #18181b">{c}</th>' for c in head) + '</tr>'
            + "".join(cells) + '</table>' + more + link +
            '<p style="color:#71717a;font-size:12px">Scores are produced by the deterministic AML '
            'framework; narratives are generated and must be verified by the analyst.</p></div>')


@register("sink", "sharepoint")
class SharePointSink(SinkConnector):
    """
    SharePoint / OneDrive via Microsoft Graph.
      Permission : Sites.ReadWrite.All
      Small file : PUT /sites/{siteId}/drives/{driveId}/root:/{path}:/content
      >4 MB      : createUploadSession (implemented below)
    """
    name = "sharepoint"

    def __init__(self, cfg: dict):
        self.site_id = cfg["site_id"]
        self.drive_id = cfg.get("drive_id")
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

    def _drive(self) -> str:
        if self.drive_id:
            return f"{self.base}/drives/{self.drive_id}"
        return f"{self.base}/sites/{self.site_id}/drive"

    def upload(self, local_path: str, remote_folder: str, metadata: dict | None = None) -> str:
        h = {"Authorization": f"Bearer {self._access_token()}"}
        name = Path(local_path).name
        data = Path(local_path).read_bytes()
        path = f"{remote_folder.strip('/')}/{name}"
        if len(data) < 4 * 1024 * 1024:
            r = requests.put(f"{self._drive()}/root:/{path}:/content",
                             headers={**h, "Content-Type": XLSX_MIME}, data=data, timeout=TIMEOUT)
            r.raise_for_status()
            return r.json().get("webUrl", "")
        s = requests.post(f"{self._drive()}/root:/{path}:/createUploadSession",
                          headers=h, json={"item": {"@microsoft.graph.conflictBehavior": "replace"}},
                          timeout=TIMEOUT)
        s.raise_for_status()
        url, size, chunk = s.json()["uploadUrl"], len(data), 3276800
        for start in range(0, size, chunk):
            end = min(start + chunk, size) - 1
            rr = requests.put(url, headers={"Content-Length": str(end - start + 1),
                                            "Content-Range": f"bytes {start}-{end}/{size}"},
                              data=data[start:end + 1], timeout=TIMEOUT)
            rr.raise_for_status()
        return rr.json().get("webUrl", "")


@register("sink", "teams")
class TeamsSink(SinkConnector):
    """
    Teams delivery = upload to the channel's SharePoint folder, then post an
    adaptive-card style message with the link into the channel.
      Permissions: Sites.ReadWrite.All + ChannelMessage.Send
    """
    name = "teams"

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.team_id = cfg["team_id"]
        self.channel_id = cfg["channel_id"]
        self.sp = SharePointSink({"site_id": cfg["site_id"], "drive_id": cfg.get("drive_id")})
        self.base = "https://graph.microsoft.com/v1.0"

    def upload(self, local_path: str, remote_folder: str, metadata: dict | None = None) -> str:
        url = self.sp.upload(local_path, remote_folder, metadata)
        m = metadata or {}
        body = (f"<b>AML triage batch {m.get('run_id','')}</b><br>"
                f"HIGH: {m.get('high',0)} &middot; MEDIUM: {m.get('medium',0)} &middot; LOW: {m.get('low',0)}<br>"
                f"Critical signal cases: {m.get('critical',0)}<br>"
                f'<a href="{url}">Open triage workbook</a>')
        h = {"Authorization": f"Bearer {self.sp._access_token()}", "Content-Type": "application/json"}
        requests.post(f"{self.base}/teams/{self.team_id}/channels/{self.channel_id}/messages",
                      headers=h, json={"body": {"contentType": "html", "content": body}}, timeout=TIMEOUT)
        return url


@register("sink", "outlook_mail")
class OutlookMailSink(SinkConnector):
    """Mails the workbook to the AML analyst distribution list (Graph sendMail)."""
    name = "outlook_mail"

    def __init__(self, cfg: dict):
        self.sender = cfg["from_mailbox"]
        self.recipients = cfg["recipients"]
        self.base = "https://graph.microsoft.com/v1.0"
        self._sp = SharePointSink({"site_id": "unused"})   # token helper reuse

    def upload(self, local_path: str, remote_folder: str, metadata: dict | None = None) -> str:
        m = metadata or {}
        data = base64.b64encode(Path(local_path).read_bytes()).decode()
        msg = {"message": {
            "subject": f"[AML] Triage batch {m.get('run_id','')} - {m.get('high',0)} HIGH / {m.get('medium',0)} MEDIUM",
            "body": {"contentType": "HTML",
                     "content": f"Automated AML triage. Population {m.get('total',0)} players. "
                                f"Critical-signal cases: {m.get('critical',0)}. Workbook attached."},
            "toRecipients": [{"emailAddress": {"address": r}} for r in self.recipients],
            "attachments": [{"@odata.type": "#microsoft.graph.fileAttachment",
                             "name": Path(local_path).name,
                             "contentType": mimetypes.guess_type(local_path)[0] or XLSX_MIME,
                             "contentBytes": data}]},
            "saveToSentItems": True}
        r = requests.post(f"{self.base}/users/{self.sender}/sendMail",
                          headers={"Authorization": f"Bearer {self._sp._access_token()}",
                                   "Content-Type": "application/json"},
                          json=msg, timeout=TIMEOUT)
        r.raise_for_status()
        return f"mailto:{','.join(self.recipients)}"
