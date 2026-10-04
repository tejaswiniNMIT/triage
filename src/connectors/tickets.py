"""Ticket connectors - HIGH / MEDIUM cases become work items for AML analysts."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import requests

from .base import TicketConnector, TicketRef, register

TIMEOUT = 45


@register("ticket", "mock")
class MockTicketConnector(TicketConnector):
    """Writes tickets to a JSONL file. Use for dev, dry-runs and tests."""
    name = "mock"

    def __init__(self, cfg: dict):
        self.path = Path(cfg.get("path", "./data/runs/tickets.jsonl"))
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _existing(self) -> dict:
        if not self.path.exists():
            return {}
        return {json.loads(l)["external_key"]: json.loads(l)
                for l in self.path.read_text().splitlines() if l.strip()}

    def find_existing(self, external_key: str) -> TicketRef | None:
        rec = self._existing().get(external_key)
        return TicketRef("mock", rec["ticket_id"], rec["url"], "existing") if rec else None

    def create(self, *, summary, description, priority, labels, external_key, attachment_url=None) -> TicketRef:
        tid = f"AML-{abs(hash(external_key)) % 100000:05d}"
        rec = {"ticket_id": tid, "external_key": external_key, "summary": summary,
               "priority": priority, "labels": labels, "url": f"local://tickets/{tid}",
               "attachment_url": attachment_url, "description": description,
               "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ")}
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return TicketRef("mock", tid, rec["url"])


@register("ticket", "jira")
class JiraConnector(TicketConnector):
    """
    Jira Cloud REST v3.
      Auth       : Basic (email + API token) or OAuth 2.0 3LO
      Idempotency: JQL search on the external key stored in a labels field
    """
    name = "jira"

    def __init__(self, cfg: dict):
        self.base = cfg["base_url"].rstrip("/")            # https://<site>.atlassian.net
        self.project = cfg["project_key"]
        self.issue_type = cfg.get("issue_type", "Task")
        self.priority_map = cfg.get("priority_map", {"HIGH": "Highest", "MEDIUM": "High", "LOW": "Low"})
        self.extra_fields = cfg.get("extra_fields", {})
        self.auth = (os.environ["JIRA_EMAIL"], os.environ["JIRA_API_TOKEN"])

    def find_existing(self, external_key: str) -> TicketRef | None:
        jql = f'project = "{self.project}" AND labels = "{external_key}" AND statusCategory != Done'
        r = requests.get(f"{self.base}/rest/api/3/search", auth=self.auth,
                         params={"jql": jql, "maxResults": 1}, timeout=TIMEOUT)
        if r.ok and r.json().get("issues"):
            k = r.json()["issues"][0]["key"]
            return TicketRef("jira", k, f"{self.base}/browse/{k}", "existing")
        return None

    def create(self, *, summary, description, priority, labels, external_key, attachment_url=None) -> TicketRef:
        existing = self.find_existing(external_key)
        if existing:
            return existing
        body = description + (f"\n\nTriage workbook: {attachment_url}" if attachment_url else "")
        fields = {
            "project": {"key": self.project},
            "summary": summary[:250],
            "issuetype": {"name": self.issue_type},
            "labels": list({*labels, external_key, "aml-auto"}),
            "description": {"type": "doc", "version": 1,
                            "content": [{"type": "paragraph",
                                         "content": [{"type": "text", "text": body[:30000]}]}]},
            **self.extra_fields,
        }
        if self.priority_map.get(priority):
            fields["priority"] = {"name": self.priority_map[priority]}
        r = requests.post(f"{self.base}/rest/api/3/issue", auth=self.auth,
                          headers={"Content-Type": "application/json"},
                          json={"fields": fields}, timeout=TIMEOUT)
        if not r.ok:
            return TicketRef("jira", "", "", "failed", r.text[:400])
        k = r.json()["key"]
        return TicketRef("jira", k, f"{self.base}/browse/{k}")


@register("ticket", "gitlab")
class GitLabConnector(TicketConnector):
    """GitLab issues API. Auth: PRIVATE-TOKEN (project access token, scope `api`)."""
    name = "gitlab"

    def __init__(self, cfg: dict):
        self.base = cfg.get("base_url", "https://gitlab.com").rstrip("/")
        self.project_id = cfg["project_id"]                 # numeric id or url-encoded path
        self.label_map = cfg.get("label_map", {"HIGH": "priority::critical", "MEDIUM": "priority::high", "LOW": "priority::low"})
        self.h = {"PRIVATE-TOKEN": os.environ["GITLAB_TOKEN"]}

    def find_existing(self, external_key: str) -> TicketRef | None:
        r = requests.get(f"{self.base}/api/v4/projects/{self.project_id}/issues",
                         headers=self.h, params={"labels": external_key, "state": "opened"}, timeout=TIMEOUT)
        if r.ok and r.json():
            i = r.json()[0]
            return TicketRef("gitlab", str(i["iid"]), i["web_url"], "existing")
        return None

    def create(self, *, summary, description, priority, labels, external_key, attachment_url=None) -> TicketRef:
        existing = self.find_existing(external_key)
        if existing:
            return existing
        body = description + (f"\n\n[Triage workbook]({attachment_url})" if attachment_url else "")
        r = requests.post(f"{self.base}/api/v4/projects/{self.project_id}/issues", headers=self.h,
                          data={"title": summary[:250], "description": body,
                                "labels": ",".join({*labels, external_key, "aml-auto",
                                                    self.label_map.get(priority, "")} - {""})},
                          timeout=TIMEOUT)
        if not r.ok:
            return TicketRef("gitlab", "", "", "failed", r.text[:400])
        i = r.json()
        return TicketRef("gitlab", str(i["iid"]), i["web_url"])


@register("ticket", "odoo")
class OdooConnector(TicketConnector):
    """
    Odoo Helpdesk / Project via JSON-RPC (/jsonrpc, execute_kw).
    Model default: helpdesk.ticket (fall back to project.task on Community).
    """
    name = "odoo"

    def __init__(self, cfg: dict):
        self.url = cfg["url"].rstrip("/")
        self.db = cfg["db"]
        self.model = cfg.get("model") or "helpdesk.ticket"
        team = cfg.get("team_id")
        self.team_id = int(team) if str(team).strip().isdigit() else None
        proj = cfg.get("project_id")
        self.project_id = int(proj) if str(proj).strip().isdigit() else None
        self.priority_map = cfg.get("priority_map", {"HIGH": "3", "MEDIUM": "2", "LOW": "1"})
        if self.model == "project.task" and not self.project_id:
            # a task with no project is created successfully and then appears in no view -
            # the worst kind of failure, so refuse up front
            raise KeyError("ODOO_PROJECT_ID is required for model project.task, or the tasks are "
                           "created but invisible in the UI. Open the project in Odoo and take "
                           "the id from the URL (.../odoo/project/<id> or ?active_id=<id>)")
        # ticket creation is the last step of a run - fail on unfilled config now, naming
        # the variable, rather than after the workbook has already been delivered
        for label, val in (("url", self.url), ("db", self.db)):
            if not val or "${" in str(val):
                raise KeyError(f"ODOO_{label.upper()} is not set (got {val!r}) - add it to .env")
        # credentials come from the environment only (settings.yaml holds ${ODOO_USER} /
        # ${ODOO_PASSWORD} references, interpolated in src/config.py)
        self.user = cfg.get("user") or os.environ.get("ODOO_USER", "")
        self.password = cfg.get("password") or os.environ.get("ODOO_PASSWORD", "")
        for label, val in (("user", self.user), ("password", self.password)):
            if not val or "${" in str(val):
                raise KeyError(f"ODOO_{label.upper()} is not set - add it to .env. The password "
                               f"may be an API key from Odoo > Preferences > Account Security")
        self._uid = None

    def health(self) -> dict:
        """Log in for real - a bad db name or password is otherwise silent until a run."""
        try:
            uid = self._login()
        except Exception as exc:                             # noqa: BLE001
            return {"connector": self.name, "ok": False, "error": str(exc)[:300]}
        if not uid:
            return {"connector": self.name, "ok": False,
                    "error": f"login refused for {self.user} on db '{self.db}'"}
        return {"connector": self.name, "ok": True, "uid": uid, "model": self.model}

    def _rpc(self, service: str, method: str, args: list):
        r = requests.post(f"{self.url}/jsonrpc", json={
            "jsonrpc": "2.0", "method": "call",
            "params": {"service": service, "method": method, "args": args}, "id": 1},
            timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            raise RuntimeError(str(data["error"])[:400])
        return data["result"]

    def _login(self) -> int:
        if self._uid is None:
            self._uid = self._rpc("common", "login", [self.db, self.user, self.password])
        return self._uid

    def _exec(self, method: str, params: list, kwargs: dict | None = None):
        return self._rpc("object", "execute_kw",
                         [self.db, self._login(), self.password, self.model, method, params, kwargs or {}])

    def find_existing(self, external_key: str) -> TicketRef | None:
        try:
            # Scoped to the destination: a task sitting in another project - or in no
            # project at all, as happens to anything created before project_id was set -
            # must not shadow this one, or every run reports "existing" and creates
            # nothing where you are actually looking.
            #
            # The key is matched bracketed. Odoo's `like` is a substring match, so a bare
            # key would let aml-20260828-PL-1 match the task for PL-100019.
            domain = [["name", "like", f"[{external_key}]"]]
            if self.model == "project.task" and self.project_id:
                domain.append(["project_id", "=", self.project_id])
            elif self.model == "helpdesk.ticket" and self.team_id:
                domain.append(["team_id", "=", self.team_id])
            ids = self._exec("search", [domain], {"limit": 1})
            if ids:
                return TicketRef("odoo", str(ids[0]),
                                 f"{self.url}/web#id={ids[0]}&model={self.model}&view_type=form", "existing")
        except Exception:                                    # noqa: BLE001
            return None
        return None

    def create(self, *, summary, description, priority, labels, external_key, attachment_url=None) -> TicketRef:
        existing = self.find_existing(external_key)
        if existing:
            return existing
        # the key has to survive truncation or the next run cannot find this task
        tag = f" [{external_key}]"
        vals = {"name": summary[:250 - len(tag)] + tag,
                "description": description + (f"\n\nWorkbook: {attachment_url}" if attachment_url else ""),
                "priority": self.priority_map.get(priority, "1")}
        if self.model == "helpdesk.ticket" and self.team_id:
            vals["team_id"] = self.team_id
        if self.model == "project.task" and self.project_id:
            vals["project_id"] = self.project_id
        try:
            tid = self._exec("create", [vals])
        except Exception as exc:                             # noqa: BLE001
            return TicketRef("odoo", "", "", "failed", str(exc)[:400])
        return TicketRef("odoo", str(tid),
                         f"{self.url}/web#id={tid}&model={self.model}&view_type=form")
