"""Explicit loopback lab endpoints, with independent node bearer credentials."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

from server import lab_backend as backend

class SanitizedLabRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request):
            try:
                return await original(request)
            except RequestValidationError:
                # Pydantic's default errors include input values, which may be secrets.
                return JSONResponse(status_code=422, content={"detail": {
                    "code": "invalid_request", "message": "请求字段或格式无效；未回显输入内容"}})
        return handler


router = APIRouter(prefix="/api/lab", tags=["local-lab"], route_class=SanitizedLabRoute)
init = backend.init


def call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except backend.LabError as exc:
        raise HTTPException(exc.status, {"code": exc.code, "message": exc.message}) from exc


def node(authorization: str = Header(default=""), x_rdos_env_id: str = Header(default=""),
         x_rdos_generation: str = Header(default="")) -> str:
    return call(backend.authenticate, authorization, x_rdos_env_id, x_rdos_generation)


class FeedbackInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    operation_id: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1, max_length=100000)
    node_id: Optional[str] = None
    member_id: Optional[str] = None
    project_id: Optional[str] = None
    task_id: Optional[str] = None


@router.get("/context")
def context(node_id: str = Depends(node)):
    return call(backend.context, node_id)


@router.get("/tasks")
def tasks(node_id: str = Depends(node)):
    return call(backend.tasks, node_id)


@router.get("/documents/{document_id}")
def document(document_id: str, node_id: str = Depends(node)):
    return call(backend.document, node_id, document_id)


@router.post("/feedback")
def submit(payload: FeedbackInput, node_id: str = Depends(node)):
    fields = payload.model_dump(exclude_none=True)
    operation_id, content = fields.pop("operation_id"), fields.pop("content")
    # Avoid passing the claimed node as the actual authenticated positional argument.
    if fields.get("node_id", node_id) != node_id:
        raise HTTPException(403, {"code": "ownership_mismatch", "message": "反馈归属与当前节点绑定不一致"})
    fields.pop("node_id", None)
    return call(backend.submit, node_id, operation_id, content, **fields)


@router.get("/feedback/{feedback_id}")
def feedback(feedback_id: str, node_id: str = Depends(node)):
    return call(backend.feedback, node_id, feedback_id)
