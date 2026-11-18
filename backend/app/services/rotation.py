"""凭证轮换批次：按关联数据源解析有效期与最后使用记录，批量轮换认证凭证。

业务边界（按源端取舍，不看处理先后）：
- 每个源端同一时刻只允许一份在途轮换批次，并发提交两次仅确认一份，另一份拒绝；
- 旧凭据吊销回执与新凭据生效回执同时到达同一源端时，按轮换策略定序而不是按到达先后：
  先吊销后激活 = 安全边界前置，轮换期间暂停引用；先激活后吊销 = 可用性前置，轮换期间记录凭据重叠；
- 无论哪种策略，批次完成后每个源端只允许一份可用凭据，旧凭据吊销后结果保留。

调用关系：轮换服务（认证凭证域）产出凭证轮换批次及来源引用快照，
数据源服务同步消费并回写待办工作台，轮换服务再按快照编排认证凭证的后续处理。
"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

from app.services.auth import AuthService
from app.services.datasource import DatasourceService
from app.store import store

BATCH_MODULE = "rotation_batch"
ITEM_MODULE = "rotation_item"
SNAPSHOT_MODULE = "rotation_snapshot"

STRATEGIES = ["先吊销后激活", "先激活后吊销"]
# 同一数据链上的回执冲突按策略定序：安全边界前置 vs 可用性前置
STRATEGY_ORDER = {
    "先吊销后激活": ["吊销回执", "生效回执"],
    "先激活后吊销": ["生效回执", "吊销回执"],
}
STRATEGY_BOUNDARY = {
    "先吊销后激活": "安全边界前置：须先吊销旧凭据再激活新凭据，轮换期间暂停引用",
    "先激活后吊销": "可用性前置：须先激活新凭据再吊销旧凭据，轮换期间记录凭据重叠",
}
INTERMEDIATE_STATE = {"先吊销后激活": "暂停引用", "先激活后吊销": "凭据重叠"}
RECEIPT_TYPES = ["吊销回执", "生效回执"]
RECEIPT_FIELD = {"吊销回执": "旧凭据吊销回执", "生效回执": "新凭据生效回执"}
ITEM_OPEN_STATES = {"待轮换", "暂停引用", "凭据重叠"}
BATCH_STATUSES = ["待处理", "轮换中", "已完成"]


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class RotationService:
    """凭证轮换批次编排：批次、明细、来源引用快照三张表的唯一写入口。"""

    def __init__(self) -> None:
        # 提交批次与登记回执都要“先查在途再落库”，必须用同一把锁保证并发下只确认一份
        self._lock = threading.Lock()
        self._auth = AuthService()
        self._datasource = DatasourceService()

    # ---------- 批次提交 ----------

    def submit_batch(
        self, payload: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]], str]:
        """一次提交多条源端记录进入批量处理，逐条返回轮换批次及快照状态。"""
        strategy = str(payload.get("策略", "") or "").strip()
        records = payload.get("records") or payload.get("记录") or []
        if strategy not in STRATEGIES:
            return None, [], f"轮换策略「{strategy}」不支持，可选：{'、'.join(STRATEGIES)}"
        if not isinstance(records, list) or not records:
            return None, [], "轮换批次至少需要一条源端记录"

        with self._lock:
            in_flight = self._in_flight_sources()
            checks: list[tuple[dict[str, Any], str, str]] = []
            seen: set[str] = set()
            for raw in records:
                record = raw if isinstance(raw, dict) else {}
                source = str(record.get("源端编号", "") or "").strip()
                if not source:
                    checks.append((record, "", "缺少源端编号，无法关联数据源"))
                    continue
                if source in seen:
                    checks.append((record, source, "同一批次内源端重复提交"))
                    continue
                seen.add(source)
                holder = in_flight.get(source)
                if holder:
                    checks.append((record, source, f"源端仍在批次 {holder} 中轮换，并发提交仅确认一份"))
                    continue
                if self._datasource.find_by_code(source) is None:
                    checks.append((record, source, "关联数据源不存在"))
                    continue
                if self._auth.active_credential_for_source(source) is None:
                    checks.append((record, source, "源端没有可轮换的认证凭证（均已吊销或未登记）"))
                    continue
                checks.append((record, source, ""))

            accepted = [(record, source) for record, source, reason in checks if not reason]
            if not accepted:
                results = [
                    {"源端编号": source, "状态": "已拒绝", "说明": reason}
                    for _, source, reason in checks
                ]
                return None, results, "没有可进入轮换的源端记录，批次未确认"

            batch = self._create_batch(strategy)
            items: list[dict[str, Any]] = []
            results: list[dict[str, Any]] = []
            for record, source, reason in checks:
                if reason:
                    items.append(self._create_item(batch, source, record, status="已拒绝", note=reason))
                    results.append({"源端编号": source, "状态": "已拒绝", "说明": reason})
                    continue
                item = self._create_item(batch, source, record)
                items.append(item)
                self._write_snapshot(batch, item)
                results.append({
                    "源端编号": source,
                    "状态": "待轮换",
                    "说明": f"已解析旧凭据 {item['旧凭据引用']} 的有效期（{item['有效期解析']}）与最后使用记录",
                })
            # 数据源服务同步消费批次与快照：回写待办工作台、登记可回读记录
            self._datasource.consume_rotation(self._serialize_batch(batch), items)

            rejected = sum(1 for result in results if result["状态"] == "已拒绝")
            message = f"凭证轮换批次 {batch['批次编号']} 已确认：{len(accepted)} 条进入轮换"
            if rejected:
                message += f"，{rejected} 条被拒绝"
            return self.get_batch_detail(int(batch["id"])), results, message

    # ---------- 回执登记 ----------

    def apply_receipts(
        self, batch_id: int, payload: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]], str]:
        """登记吊销/生效回执；同一源端双回执同时到达时按策略定序落库。"""
        batch = store.find(BATCH_MODULE, batch_id)
        if batch is None:
            return None, [], f"凭证轮换批次 {batch_id} 不存在或已归档"
        receipts = payload.get("receipts") or payload.get("回执") or []
        if not isinstance(receipts, list) or not receipts:
            return None, [], "至少提交一条回执记录"
        if batch.get("status") == "已完成":
            return None, [], f"批次 {batch.get('批次编号')} 已完成，回执不再受理"

        with self._lock:
            items = self._items_of(batch)
            by_source = {str(item.get("源端编号")): item for item in items}
            results: list[dict[str, Any]] = []
            grouped: dict[str, list[str]] = {}
            group_order: list[str] = []
            for raw in receipts:
                record = raw if isinstance(raw, dict) else {}
                source = str(record.get("源端编号", "") or "").strip()
                receipt_type = str(record.get("回执类型", "") or "").strip()
                if receipt_type not in RECEIPT_TYPES:
                    results.append({
                        "源端编号": source, "回执类型": receipt_type, "状态": "已拒绝",
                        "说明": f"回执类型「{receipt_type}」不支持，可选：{'、'.join(RECEIPT_TYPES)}",
                    })
                    continue
                if source not in by_source:
                    results.append({
                        "源端编号": source, "回执类型": receipt_type, "状态": "已拒绝",
                        "说明": f"源端 {source or '（空）'} 不在本批次内",
                    })
                    continue
                if source not in grouped:
                    grouped[source] = []
                    group_order.append(source)
                grouped[source].append(receipt_type)

            strategy = str(batch.get("策略", ""))
            for source in group_order:
                item = by_source[source]
                # 吊销与生效回执同时到达构成同一数据链上的冲突：按策略边界排序，不按提交先后
                wanted = STRATEGY_ORDER[strategy]
                ordered = sorted(grouped[source], key=lambda kind: wanted.index(kind))
                seen_types: set[str] = set()
                for receipt_type in ordered:
                    if receipt_type in seen_types:
                        results.append({
                            "源端编号": source, "回执类型": receipt_type, "状态": "已拒绝",
                            "说明": "同一回执重复提交，仅确认一份",
                        })
                        continue
                    seen_types.add(receipt_type)
                    results.append(self._apply_one(batch, item, receipt_type))

            self._maybe_complete(batch)
            # 每个回执落库后都同步消费一次，数据源页可用状态与待办工作台保持实时
            self._datasource.consume_rotation(self._serialize_batch(batch), items)
            accepted = sum(1 for result in results if result["状态"] == "已受理")
            message = f"批次 {batch['批次编号']} 回执处理完成：{accepted} 条已受理"
            rejected = len(results) - accepted
            if rejected:
                message += f"，{rejected} 条被拒绝"
            if batch.get("status") == "已完成":
                message += "；批次已完成，每个源端仅保留一份可用凭据"
            return self.get_batch_detail(batch_id), results, message

    def _apply_one(
        self, batch: dict[str, Any], item: dict[str, Any], receipt_type: str
    ) -> dict[str, Any]:
        """落一条回执：校验策略顺序，改凭据状态，刷新来源引用快照。"""
        source = str(item.get("源端编号", ""))
        base = {"源端编号": source, "回执类型": receipt_type}
        strategy = str(batch.get("策略", ""))
        if item.get("状态") == "已拒绝":
            return {**base, "状态": "已拒绝", "说明": "该记录提交时已被拒绝，不参与轮换"}
        if item.get("状态") == "已切换":
            return {**base, "状态": "已拒绝", "说明": "轮换已完成，回执不再受理"}
        if item.get(RECEIPT_FIELD[receipt_type]) == "已受理":
            return {**base, "状态": "已拒绝", "说明": "回执已受理过，重复提交仅确认一份"}

        expected = STRATEGY_ORDER[strategy][0] if item.get("状态") == "待轮换" else STRATEGY_ORDER[strategy][1]
        if receipt_type != expected:
            return {
                **base, "状态": "已拒绝",
                "说明": f"{STRATEGY_BOUNDARY[strategy]}，当前应先受理{expected}",
            }

        batch_code = str(batch.get("批次编号", ""))
        if receipt_type == "吊销回执":
            self._auth.revoke_for_rotation(str(item.get("旧凭据引用", "")), batch_code)
            item["旧凭据吊销回执"] = "已受理"
        else:
            old = self._auth.find_by_code(str(item.get("旧凭据引用", "")))
            issued = self._auth.issue_rotated_credential(
                source,
                old,
                {"新有效期至": item.get("新有效期至", ""), "新凭证内容": item.get("新凭证内容", "")},
                batch_code,
            )
            item["新凭据引用"] = str(issued.get("凭证编号", ""))
            item["新凭据生效回执"] = "已受理"

        if item.get("状态") == "待轮换":
            item["状态"] = INTERMEDIATE_STATE[strategy]
            item["说明"] = STRATEGY_BOUNDARY[strategy]
        else:
            item["状态"] = "已切换"
            item["完成时间"] = now_text()
            item["说明"] = "轮换完成，源端仅保留一份可用凭据"
        batch["status"] = "轮换中"
        self._write_snapshot(batch, item)
        return {**base, "状态": "已受理", "说明": str(item["说明"])}

    # ---------- 查询 ----------

    def list_batches(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = [self._serialize_batch(batch) for batch in store.rows(BATCH_MODULE)]
        rows.sort(key=lambda row: int(row.get("id", 0)), reverse=True)
        if keyword:
            rows = [row for row in rows if keyword.casefold() in str(row.get("批次编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_batch_detail(self, batch_id: int) -> dict[str, Any] | None:
        """批次详情：批次本身、逐条明细，以及每个源端只有一份可用凭据的来源引用快照。"""
        batch = store.find(BATCH_MODULE, batch_id)
        if batch is None:
            return None
        items = self._items_of(batch)
        sources = {str(item.get("源端编号")) for item in items}
        snapshots = [
            self._snapshot_view(row) for row in store.rows(SNAPSHOT_MODULE)
            if str(row.get("源端编号")) in sources
        ]
        snapshots.sort(key=lambda row: str(row.get("源端编号", "")))
        return {
            "batch": self._serialize_batch(batch),
            "items": sorted(items, key=lambda row: int(row.get("id", 0))),
            "来源引用快照": snapshots,
        }

    def snapshot_rows(self) -> list[dict[str, Any]]:
        """全量来源引用快照：数据源服务之外的协作方也可以直接读。"""
        rows = [self._snapshot_view(row) for row in store.rows(SNAPSHOT_MODULE)]
        return sorted(rows, key=lambda row: str(row.get("源端编号", "")))

    # ---------- 内部 ----------

    def _create_batch(self, strategy: str) -> dict[str, Any]:
        rows = store.rows(BATCH_MODULE)
        batch = {
            "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
            "批次编号": "",
            "策略": strategy,
            "status": BATCH_STATUSES[0],
            "pending": True,
            "abnormal": False,
            "提交时间": now_text(),
            "完成时间": "",
        }
        batch["批次编号"] = f"ROTB-{int(batch['id']):04d}"
        rows.append(batch)
        return batch

    def _create_item(
        self,
        batch: dict[str, Any],
        source: str,
        record: dict[str, Any],
        *,
        status: str = "待轮换",
        note: str = "",
    ) -> dict[str, Any]:
        rows = store.rows(ITEM_MODULE)
        item: dict[str, Any] = {
            "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
            "批次编号": str(batch.get("批次编号", "")),
            "源端编号": source,
            "关联数据源": source,
            "旧凭据引用": "",
            "新凭据引用": "",
            "旧有效期至": "",
            "有效期解析": "",
            "最后使用": "",
            "新有效期至": str(record.get("新有效期至", "") or "").strip(),
            "新凭证内容": str(record.get("新凭证内容", "") or "").strip(),
            "状态": status,
            "说明": note,
            "旧凭据吊销回执": "未受理",
            "新凭据生效回执": "未受理",
            "登记时间": now_text(),
            "完成时间": "",
        }
        if status != "已拒绝":
            old = self._auth.active_credential_for_source(source)
            if old is not None:
                profile = self._auth.validity_profile(old)
                item["旧凭据引用"] = str(old.get("凭证编号", ""))
                item["旧有效期至"] = profile["有效期至"]
                item["有效期解析"] = profile["有效期解析"]
                item["最后使用"] = profile["最后使用"]
        rows.append(item)
        return item

    def _write_snapshot(self, batch: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
        """刷新来源引用快照：每个源端只留最新一份，可用凭据列表与凭据台账实时对齐。"""
        source = str(item.get("源端编号", ""))
        usable = [
            str(row.get("凭证编号", ""))
            for row in self._auth.usable_credentials_for_source(source)
        ]
        rows = store.rows(SNAPSHOT_MODULE)
        for row in rows:
            if row.get("源端编号") == source:
                snapshot = row
                break
        else:
            snapshot = {
                "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
                "源端编号": source,
            }
            rows.append(snapshot)
        snapshot["批次编号"] = str(batch.get("批次编号", ""))
        snapshot["旧凭据引用"] = str(item.get("旧凭据引用", ""))
        snapshot["新凭据引用"] = str(item.get("新凭据引用", ""))
        snapshot["凭据引用状态"] = str(item.get("状态", ""))
        snapshot["可用凭据"] = usable
        snapshot["可用凭据数"] = len(usable)
        snapshot["可用状态"] = {
            "待轮换": "可用",
            "暂停引用": "暂停引用",
            "凭据重叠": "凭据重叠",
            "已切换": "可用",
        }.get(str(item.get("状态", "")), "可用")
        snapshot["更新时间"] = now_text()
        return snapshot

    def _maybe_complete(self, batch: dict[str, Any]) -> None:
        """全部有效记录都切换完成才收尾；收尾时每个源端必须只剩一份可用凭据。"""
        items = self._items_of(batch)
        open_items = [item for item in items if item.get("状态") != "已拒绝"]
        if not open_items or any(item.get("状态") != "已切换" for item in open_items):
            return
        batch["status"] = "已完成"
        batch["pending"] = False
        batch["完成时间"] = now_text()

    def _in_flight_sources(self) -> dict[str, str]:
        """在途批次占用的源端：并发提交按源端边界互斥，而不是按提交先后。"""
        holders: dict[str, str] = {}
        for batch in store.rows(BATCH_MODULE):
            if batch.get("status") == "已完成":
                continue
            for item in self._items_of(batch):
                if item.get("状态") in ITEM_OPEN_STATES:
                    holders[str(item.get("源端编号"))] = str(batch.get("批次编号", ""))
        return holders

    def _items_of(self, batch: dict[str, Any]) -> list[dict[str, Any]]:
        batch_code = str(batch.get("批次编号", ""))
        return [row for row in store.rows(ITEM_MODULE) if row.get("批次编号") == batch_code]

    def _serialize_batch(self, batch: dict[str, Any]) -> dict[str, Any]:
        items = self._items_of(batch)
        data = dict(batch)
        data["记录总数"] = len(items)
        data["已切换数"] = sum(1 for item in items if item.get("状态") == "已切换")
        data["已拒绝数"] = sum(1 for item in items if item.get("状态") == "已拒绝")
        data["进行中数"] = len(items) - data["已切换数"] - data["已拒绝数"]
        return data

    def _snapshot_view(self, row: dict[str, Any]) -> dict[str, Any]:
        data = dict(row)
        data["可用凭据"] = "、".join(str(code) for code in row.get("可用凭据", [])) or "—"
        return data
