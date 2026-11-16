"""In-memory taskboard delegate listener for tests.

`FakeTaskboard(token).app` serves the approval routes the connector calls, behind a bearer
check. `approvals` maps decision id to its approval JSON (`status` open, answered, expired or
withdrawn); `answers` records every posted answer body.
"""

from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request


class FakeTaskboard:
    def __init__(self, token: str) -> None:
        self.token = token
        self.approvals: dict[int, dict[str, Any]] = {}
        self.answers: list[dict[str, Any]] = []
        self.app = FastAPI()
        self.app.add_api_route("/taskboard/api/v1/approvals", self._list, methods=["GET"])
        self.app.add_api_route("/taskboard/api/v1/approvals/answer", self._answer, methods=["POST"])

    def _check(self, authorization: str | None) -> None:
        if authorization != f"Bearer {self.token}":
            raise HTTPException(401, "unauthorized")

    async def _list(
        self,
        status: str = "open",
        limit: int = 100,
        authorization: str | None = Header(None),
    ) -> dict[str, Any]:
        self._check(authorization)
        rows = [a for a in self.approvals.values() if status == "all" or a["status"] == status]
        return {"items": rows[:limit]}

    async def _answer(
        self, request: Request, authorization: str | None = Header(None)
    ) -> dict[str, Any]:
        self._check(authorization)
        body = await request.json()
        self.answers.append(body)
        out = []
        for item in body["items"]:
            approval = self.approvals.get(item["id"])
            if approval is None:
                result, key = "not_found", None
            else:
                key = approval.get("task_key")
                if approval["status"] == "open":
                    approval["status"] = "answered"
                    approval["verdict"] = item["verdict"]
                    result = "answered"
                elif approval["status"] == "answered":
                    result = "already_answered"
                else:
                    result = approval["status"]
            out.append({"id": item["id"], "result": result, "task_key": key})
        return {"items": out}
