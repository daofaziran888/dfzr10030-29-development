"""凭证轮换接口：多源凭证批量轮换，覆盖批次提交、回执登记与来源引用快照查询。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.schemas import PageResult
from app.services.rotation import BATCH_STATUSES, RotationService

router = APIRouter(prefix="/api/rotation", tags=["凭证轮换"])

service = RotationService()


@router.get("/batches", response_model=PageResult[dict])
def list_batches(
    keyword: str | None = Query(default=None, description="按批次编号检索"),
    status: str | None = Query(default=None, description="待处理、轮换中、已完成"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """分页列出凭证轮换批次，进度计数随批次实时汇总。"""
    items, total = service.list_batches(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.post("/batches", response_model=dict)
def submit_batch(payload: dict[str, object]) -> dict:
    """提交多源凭证轮换批次：逐条解析源端记录，并发冲突的源端仅确认一份。"""
    detail, results, message = service.submit_batch(payload)
    return {"ok": detail is not None, "message": message, "results": results, "batch": detail}


@router.get("/snapshot", response_model=dict)
def snapshot() -> dict:
    """全量来源引用快照：每个源端轮换后只有一份可用凭据的数据快照。"""
    return {"items": service.snapshot_rows()}


@router.get("/statuses", response_model=dict)
def statuses() -> dict:
    """批次状态序列，给前端筛选下拉用。"""
    return {"items": BATCH_STATUSES}


@router.get("/batches/{batch_id}", response_model=dict)
def get_batch(batch_id: int) -> dict:
    """批次详情：逐条明细的旧新凭据引用、回执状态与来源引用快照。"""
    detail = service.get_batch_detail(batch_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"凭证轮换批次 {batch_id} 不存在或已归档")
    return detail


@router.post("/batches/{batch_id}/receipts", response_model=dict)
def apply_receipts(batch_id: int, payload: dict[str, object]) -> dict:
    """登记吊销/生效回执；同一源端双回执同时到达时按轮换策略定序落库。"""
    if service.get_batch_detail(batch_id) is None:
        raise HTTPException(status_code=404, detail=f"凭证轮换批次 {batch_id} 不存在或已归档")
    detail, results, message = service.apply_receipts(batch_id, payload)
    return {"ok": detail is not None, "message": message, "results": results, "batch": detail}
