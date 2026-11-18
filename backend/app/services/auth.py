"""凭证管理业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from app.store import store

MODULE = "auth"
LIST_FIELDS = ["凭证编号", "凭证类型", "关联数据源", "凭证内容", "有效期至", "创建时间", "最后使用", "凭证状态"]
REQUIRED_FIELDS = ["凭证编号", "凭证类型", "关联数据源"]
STATUS_ORDER = ["有效", "即将过期", "已过期", "已吊销"]
ACTION_RULES = {"续期凭证": "有效", "吊销凭证": "已吊销"}
NEGATIVE_ACTIONS = []

# 参与轮换的凭据状态：已吊销的凭据不再进入轮换批次
ROTATABLE_STATUSES = ["有效", "即将过期", "已过期"]
# 判定“可用凭据”的状态口径：批次完成后每个源端只允许一份可用凭据
USABLE_STATUSES = ["有效", "即将过期"]
# 距到期多少天算临期，用于有效期解析
EXPIRING_SOON_DAYS = 30
# 轮换签发的新凭据默认有效期
ROTATED_CREDENTIAL_DAYS = 365


def parse_day(text: object) -> date | None:
    """把有效期记录解析成日期；解析不了返回 None，由调用方决定口径。"""
    try:
        return date.fromisoformat(str(text or "").strip())
    except ValueError:
        return None


class AuthService:
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
            rows = [row for row in rows if keyword.casefold() in str(row.get("凭证编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

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
            return None, f"认证凭证 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于凭证管理可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"认证凭证已{action}"

    # ---------- 凭证轮换支撑：按关联数据源解析有效期与最后使用记录 ----------

    def find_by_code(self, credential_code: str) -> dict[str, Any] | None:
        """按凭证编号定位凭据，轮换批次里存的是业务编号而不是内存 id。"""
        for row in store.rows(MODULE):
            if str(row.get("凭证编号", "")).strip() == credential_code:
                return row
        return None

    def credentials_for_source(self, source_code: str) -> list[dict[str, Any]]:
        """源端名下的全部凭据（含已吊销的历史结果，旧结果保留不物理删除）。"""
        return [
            row for row in store.rows(MODULE)
            if str(row.get("关联数据源", "")).strip() == source_code
        ]

    def active_credential_for_source(self, source_code: str) -> dict[str, Any] | None:
        """源端当前待轮换的旧凭据：有效/即将过期/已过期中最新登记的一条。"""
        candidates = [
            row for row in self.credentials_for_source(source_code)
            if row.get("status") in ROTATABLE_STATUSES
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda row: int(row.get("id", 0)))

    def usable_credentials_for_source(self, source_code: str) -> list[dict[str, Any]]:
        """源端当前可用凭据（有效/即将过期）；轮换完成后必须只剩一份。"""
        return [
            row for row in self.credentials_for_source(source_code)
            if row.get("status") in USABLE_STATUSES
        ]

    def validity_profile(self, credential: dict[str, Any]) -> dict[str, str]:
        """解析单条凭据的有效期与最后使用记录，给轮换批次做到期判断。"""
        expiry = parse_day(credential.get("有效期至"))
        today = date.today()
        if expiry is None:
            stage = "未登记有效期"
        elif expiry < today:
            stage = "已过期"
        elif expiry <= today + timedelta(days=EXPIRING_SOON_DAYS):
            stage = "临期"
        else:
            stage = "正常"
        return {
            "有效期至": str(credential.get("有效期至", "") or ""),
            "有效期解析": stage,
            "最后使用": str(credential.get("最后使用", "") or ""),
        }

    def next_credential_code(self) -> str:
        """按凭证编号最大序号签发新编号，避免与存量编号冲突。"""
        suffixes: list[int] = []
        for row in store.rows(MODULE):
            code = str(row.get("凭证编号", ""))
            if code.startswith("AUTH-"):
                tail = code.removeprefix("AUTH-")
                if tail.isdigit():
                    suffixes.append(int(tail))
        return f"AUTH-{max(suffixes, default=0) + 1:04d}"

    def issue_rotated_credential(
        self,
        source_code: str,
        old: dict[str, Any] | None,
        record: dict[str, Any],
        batch_code: str,
    ) -> dict[str, Any]:
        """轮换生效回执落库：为源端签发一份状态为有效的新凭据。"""
        rows = store.rows(MODULE)
        entry: dict[str, Any] = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: "" for field in LIST_FIELDS})
        entry["凭证编号"] = self.next_credential_code()
        entry["凭证类型"] = str((old or {}).get("凭证类型", "") or "")
        entry["关联数据源"] = source_code
        entry["凭证内容"] = str(record.get("新凭证内容") or "").strip() or f"轮换签发-{batch_code}"
        entry["有效期至"] = (
            str(record.get("新有效期至") or "").strip()
            or (date.today() + timedelta(days=ROTATED_CREDENTIAL_DAYS)).isoformat()
        )
        entry["创建时间"] = date.today().isoformat()
        entry["最后使用"] = "未使用"
        entry["凭证状态"] = "有效"
        entry["status"] = "有效"
        entry["pending"] = False
        entry["abnormal"] = False
        entry["轮换批次"] = batch_code
        rows.append(entry)
        return entry

    def revoke_for_rotation(self, credential_code: str, batch_code: str) -> dict[str, Any] | None:
        """轮换吊销回执落库：旧凭据置为已吊销，历史结果保留备查。"""
        entry = self.find_by_code(credential_code)
        if entry is None:
            return None
        entry["status"] = "已吊销"
        entry["凭证状态"] = "已吊销"
        entry["pending"] = False
        entry["轮换批次"] = batch_code
        return entry
