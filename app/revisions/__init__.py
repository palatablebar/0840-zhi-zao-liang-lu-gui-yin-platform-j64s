"""命名兼容包：修订/重算服务。"""
from app.revision.service import (  # noqa: F401
    list_versions_view,
    present_revision,
    request_revision,
    resume_revision,
)


def revise(store, run_id, body=None, **kwargs):
    payload = dict(body or {})
    payload.update(kwargs)
    return request_revision(store, run_id, payload)
