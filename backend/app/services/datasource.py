"""数据源配置业务规则：状态流转、字段校验与筛选口径都收在这里。

跨模块协作：数据源服务同步消费凭证轮换批次及来源引用快照，
按凭据引用状态决定源端是否启用，并把轮换待办回写到待办工作台（pending）。
"""
from __future__ import annotations

from typing import Any

from app.services.auth import AuthService
from app.store import store

MODULE = "datasource"
LIST_FIELDS = ["源端编号", "连接类型", "接入地址", "认证方式", "采样周期", "超时秒数", "重试上限", "负责人"]
REQUIRED_FIELDS = ["源端编号", "连接类型", "接入地址"]
STATUS_ORDER = ["草稿", "待校验", "已启用", "已停用"]
ACTION_RULES = {"提交校验": "待校验", "启用配置": "已启用", "停用配置": "已停用"}
NEGATIVE_ACTIONS = ["停用配置"]

# 来源引用快照 / 数据源侧可回读记录所在的辅助表
SNAPSHOT_MODULE = "rotation_snapshot"
ROTATION_RECORD_MODULE = "datasource_rotation"

# 凭据引用状态 -> 源端可用状态：轮换期间按这个口径决定是否启用源端
REFERENCE_AVAILABILITY = {
    "待轮换": "可用",
    "暂停引用": "暂停引用",
    "凭据重叠": "凭据重叠",
    "已切换": "可用",
}


class DatasourceService:
    def __init__(self) -> None:
        self._auth = AuthService()

    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            rows = [row for row in rows if keyword.casefold() in str(row.get("源端编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        page_rows = rows[start:start + size]
        for row in page_rows:
            row.update(self.availability(row))
        return page_rows, total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is not None:
            entry.update(self.availability(entry))
        return entry

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in LIST_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"数据源 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于数据源配置可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"数据源已{action}"

    # ---------- 凭证轮换协作：消费批次与快照、回写待办工作台 ----------

    def find_by_code(self, source_code: str) -> dict[str, Any] | None:
        """按源端编号定位数据源，轮换批次按源端编号关联。"""
        for row in store.rows(MODULE):
            if str(row.get("源端编号", "")).strip() == source_code:
                return row
        return None

    def consume_rotation(self, batch: dict[str, Any], items: list[dict[str, Any]]) -> None:
        """同步消费凭证轮换批次及来源引用快照。

        - 回写待办工作台：轮换进行中的源端挂起 pending 并写明批次，批次完成后清除；
        - 围绕批次与快照登记一份可回读记录（datasource_rotation），供数据源页回查。
        """
        in_flight = batch.get("status") != "已完成"
        for item in items:
            if item.get("状态") == "已拒绝":
                continue
            entry = self.find_by_code(str(item.get("源端编号", "")))
            if entry is None:
                continue
            entry["pending"] = in_flight
            entry["待办说明"] = f"凭证轮换中：{batch.get('批次编号', '')}" if in_flight else ""
            self._upsert_rotation_record(batch, item)

    def _upsert_rotation_record(self, batch: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
        """围绕凭证轮换批次及来源引用快照的可回读记录，按批次+源端幂等更新。"""
        rows = store.rows(ROTATION_RECORD_MODULE)
        key = (str(batch.get("批次编号", "")), str(item.get("源端编号", "")))
        for row in rows:
            if (row.get("批次编号"), row.get("源端编号")) == key:
                record = row
                break
        else:
            record = {
                "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
                "批次编号": key[0],
                "源端编号": key[1],
                "登记时间": item.get("登记时间", ""),
            }
            rows.append(record)
        record["策略"] = str(batch.get("策略", ""))
        record["批次状态"] = str(batch.get("status", ""))
        record["旧凭据引用"] = str(item.get("旧凭据引用", ""))
        record["新凭据引用"] = str(item.get("新凭据引用", ""))
        record["凭据引用状态"] = str(item.get("状态", ""))
        record["可用状态"] = REFERENCE_AVAILABILITY.get(str(item.get("状态", "")), "可用")
        record["完成时间"] = str(item.get("完成时间", ""))
        return record

    def list_rotations(self, entry_id: int) -> list[dict[str, Any]] | None:
        """数据源侧的轮换可回读记录；源端不存在时返回 None 由路由层报 404。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        source_code = str(entry.get("源端编号", ""))
        records = [
            row for row in store.rows(ROTATION_RECORD_MODULE)
            if row.get("源端编号") == source_code
        ]
        return sorted(records, key=lambda row: int(row.get("id", 0)), reverse=True)

    def availability(self, entry: dict[str, Any]) -> dict[str, str]:
        """读取来源引用快照，按凭据引用状态决定源端可用状态与启用决策。"""
        source_code = str(entry.get("源端编号", ""))
        snapshot = self._latest_snapshot(source_code)
        if snapshot is not None:
            reference_state = str(snapshot.get("凭据引用状态", ""))
            usable = [str(code) for code in snapshot.get("可用凭据", [])]
            availability = REFERENCE_AVAILABILITY.get(reference_state, "可用")
            current_ref = "、".join(usable) if usable else "—"
            batch_code = str(snapshot.get("批次编号", ""))
        else:
            usable_rows = self._auth.usable_credentials_for_source(source_code)
            availability = "可用" if usable_rows else "无可用凭据"
            current_ref = "、".join(str(row.get("凭证编号", "")) for row in usable_rows) or "—"
            batch_code = ""
        if entry.get("status") == "已启用":
            decision = "暂停启用" if availability == "暂停引用" else "保持启用"
        else:
            decision = "未启用"
        return {
            "可用状态": availability,
            "当前凭据引用": current_ref,
            "轮换批次": batch_code,
            "启用决策": decision,
        }

    def _latest_snapshot(self, source_code: str) -> dict[str, Any] | None:
        """来源引用快照按源端编号保留最新一份，轮换后快照里只剩一份可用凭据。"""
        snapshots = [
            row for row in store.rows(SNAPSHOT_MODULE)
            if row.get("源端编号") == source_code
        ]
        if not snapshots:
            return None
        return max(snapshots, key=lambda row: int(row.get("id", 0)))
