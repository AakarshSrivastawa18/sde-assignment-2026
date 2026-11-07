"""Thin HTTP client for the simulator. Retries transport errors and 5xx; surfaces 4xx as SimError.

4xx are *answers* (stale base, duplicate id, out of credits, not permitted) and callers handle each one
explicitly. Only transport failures and 5xx are retried, with capped exponential backoff, because every
mutating call we make carries an idempotency key (proposal_id) or is naturally idempotent (GET, apply of an
already-applied approval returns 409 not_approved which governance reconciles).
"""
from __future__ import annotations

import logging
import time

import httpx

log = logging.getLogger("controller.client")


class SimError(Exception):
    def __init__(self, status: int, code: str, detail):
        super().__init__(f"{status} {code}: {detail}")
        self.status, self.code, self.detail = status, code, detail


class SimClient:
    def __init__(self, base_url: str, timeout: float = 300.0, max_attempts: int = 8):
        self.http = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout)
        self.max_attempts = max_attempts

    def _req(self, method: str, path: str, **kw) -> httpx.Response:
        delay = 0.5
        for attempt in range(1, self.max_attempts + 1):
            try:
                r = self.http.request(method, path, **kw)
            except httpx.TransportError as e:
                if attempt == self.max_attempts:
                    raise
                log.warning("transport error on %s %s (%s); retry %d in %.1fs", method, path, e, attempt, delay)
            else:
                if r.status_code < 500:
                    if r.status_code >= 400:
                        try:
                            body = r.json()
                        except ValueError:
                            body = {"error": "http_%d" % r.status_code, "detail": r.text[:300]}
                        raise SimError(r.status_code, body.get("error") or str(body.get("detail")), body.get("detail"))
                    return r
                if attempt == self.max_attempts:
                    raise SimError(r.status_code, "server_error", r.text[:300])
                log.warning("%s on %s %s; retry %d in %.1fs", r.status_code, method, path, attempt, delay)
            time.sleep(delay)
            delay = min(delay * 2, 10.0)
        raise RuntimeError("unreachable")

    # ---- reads
    def state(self) -> dict:
        return self._req("GET", "/sim/state").json()

    def workflows(self) -> list[dict]:
        return self._req("GET", "/workflows").json()

    def workflow(self, wid: str) -> dict:
        return self._req("GET", f"/workflows/{wid}").json()

    def config(self, wid: str) -> dict:
        return self._req("GET", f"/workflows/{wid}/config").json()

    def config_history(self, wid: str) -> list[dict]:
        return self._req("GET", f"/workflows/{wid}/config/history").json()

    def chunks(self, kind: str, after: int) -> list[dict]:
        return self._req("GET", "/events/chunks", params={"kind": kind, "after": after}).json()

    def chunk_text(self, n: int) -> str:
        return self._req("GET", f"/events/chunks/{n}").text

    def approval(self, aid: str) -> dict:
        return self._req("GET", f"/approvals/{aid}").json()

    def approvals(self, workflow_id: str | None = None, status: str | None = None) -> list[dict]:
        params = {k: v for k, v in (("workflow_id", workflow_id), ("status", status)) if v}
        return self._req("GET", "/approvals", params=params).json()

    # ---- writes
    def advance(self, minutes: int) -> dict:
        return self._req("POST", "/sim/advance", json={"minutes": minutes}).json()

    def request_approval(self, body: dict) -> dict:
        return self._req("POST", "/approvals", json=body).json()

    def apply(self, wid: str, approval_id: str) -> dict:
        return self._req("POST", f"/workflows/{wid}/apply", json={"approval_id": approval_id}).json()

    def auto_apply(self, wid: str, body: dict) -> dict:
        return self._req("PUT", f"/workflows/{wid}/config", json=body).json()

    def suggest_fixes(self, wid: str, problem: str) -> dict:
        return self._req("POST", "/llm/suggest-fixes", json={"workflow_id": wid, "problem": problem}).json()

    def validate(self, wid: str, prompt_version: str, n: int) -> dict:
        return self._req("POST", "/validation/runs",
                         json={"workflow_id": wid, "prompt_version": prompt_version, "n": n}).json()
