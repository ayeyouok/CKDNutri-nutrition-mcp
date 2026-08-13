# -*- coding: utf-8 -*-
"""P2 营养域数据访问层（DAO）：饮食日记 + PEW 历史。

v0.5（2026-08-13）：统一后端语义 = **默认 Tablestore（生产）+ A207_STORAGE_BACKEND=json
可选开发模式**。此前日记/PEW 直接写本地 JSON 文件（无 DAO 层），魔搭临时文件系统
重启即丢数据；现经本层访问：
- 生产（缺省 / A207_STORAGE_BACKEND=tablestore）：Tablestore 两表，按 patient_id 分片；
- 本地开发/测试（显式 A207_STORAGE_BACKEND=json）：LocalJson（行为与旧版完全一致）。
缺 OTS 连接参数时 fail-fast（不静默回退，避免数据写错地方）。

Tablestore 表结构：
- food_diary   主键 patient_id；属性列 entries(JSON 数组)/updated_at
- pew_history  主键 patient_id；属性列 points(JSON 数组)/updated_at
日记在业务层为"全量 entries"语义（跨患者），本层按 patient_id 分片存储，
load_diary 时全表 GetRange 合并回 {"entries": [...]}，业务层零感知。
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from a207_policy import atomic_write_json, resolve_state_path

logger = logging.getLogger("CKDNutri-nutrition-mcp.repository")

# 后端选择（与 care/clinical-data 同语义）：缺省 = tablestore（生产），
# 显式 "json" = 本地 JSON 开发模式。设 json 时不需 OTS 参数。
STORAGE_BACKEND_ENV = "A207_STORAGE_BACKEND"

# Tablestore 连接参数（部署注入，不入代码）
OTS_ENDPOINT_ENV = "A207_OTS_ENDPOINT"
OTS_INSTANCE_ENV = "A207_OTS_INSTANCE_NAME"
OTS_AK_ID_ENV = "A207_OTS_ACCESS_KEY_ID"
OTS_AK_SECRET_ENV = "A207_OTS_ACCESS_KEY_SECRET"

# Tablestore 表名（单一事实源）
TABLE_FOOD_DIARY = "food_diary"
TABLE_PEW_HISTORY = "pew_history"

# JSON 文件名（LocalJson 后端；core 测试仍引用 DIARY_STORE_FILENAME 常量）
DIARY_STORE_FILENAME = "diary_store.json"
PEW_STORE_FILENAME = "pew_history_store.json"

# Tablestore 乐观锁版本列与重试次数（对齐 care repository）
_REV_COL = "_rev"
_MAX_RETRY = 3

# 本地开发数据目录 override（与旧 core._state_path 语义一致）
_DATA_DIR_ENV = "A207_NUTRITION_ASSESSMENT_DATA_DIR"


@runtime_checkable
class NutritionRepository(Protocol):
    """P2 存储契约：饮食日记（全量 entries 语义）+ PEW 历史（按患者分键）。"""

    # ---- 饮食日记 ----
    def load_diary(self) -> dict[str, Any]:
        """读取全部日记条目，返回 {"entries": [...]}；损坏 fail-closed 抛 RuntimeError。"""
        ...

    def save_diary(self, store: dict[str, Any]) -> None:
        """原子持久化全部日记条目。"""
        ...

    # ---- PEW 历史 ----
    def load_pew(self) -> dict[str, Any]:
        """读取全部 PEW 历史，返回 {patient_id: [points]}；损坏 fail-closed。"""
        ...

    def save_pew(self, store: dict[str, Any]) -> None:
        """原子持久化全部 PEW 历史。"""
        ...


def _state_path(filename: str) -> str:
    """本地 JSON 状态文件路径（A207_NUTRITION_ASSESSMENT_DATA_DIR override 优先）。"""
    override = os.environ.get(_DATA_DIR_ENV)
    if override:
        root = Path(override)
        root.mkdir(parents=True, exist_ok=True)
        return str(root / filename)
    return str(resolve_state_path(filename))


def _read_json_fail_closed(path: str, label: str, default: Any) -> Any:
    """读取 JSON 文件（fail-closed：损坏/非 dict 抛 RuntimeError，防 RMW 静默清空）。"""
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"{label} {Path(path).name} JSON 损坏，拒绝加载（防止静默清空），"
            f"请检查磁盘/恢复备份: {exc}") from exc
    except OSError as exc:
        raise RuntimeError(
            f"{label} {Path(path).name} 读取失败，拒绝加载（防止静默清空）: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(
            f"{label} {Path(path).name} 数据类型错误：期望 dict，实际为 {type(data).__name__}，"
            f"拒绝加载（防止静默清空）")
    return data


class LocalJsonRepository:
    """本地 JSON 文件后端（v0.5 起仅本地开发/测试，显式 A207_STORAGE_BACKEND=json）。"""

    # ---- 饮食日记 ----
    def load_diary(self) -> dict[str, Any]:
        return _read_json_fail_closed(_state_path(DIARY_STORE_FILENAME),
                                      "日记库", {"entries": []})

    def save_diary(self, store: dict[str, Any]) -> None:
        atomic_write_json(_state_path(DIARY_STORE_FILENAME), store)

    # ---- PEW 历史 ----
    def load_pew(self) -> dict[str, Any]:
        return _read_json_fail_closed(_state_path(PEW_STORE_FILENAME),
                                      "PEW 历史库", {})

    def save_pew(self, store: dict[str, Any]) -> None:
        atomic_write_json(_state_path(PEW_STORE_FILENAME), store)


class TablestoreRepository:
    """阿里云表格存储后端（生产默认，缺 OTS 参数 fail-fast）。"""

    def __init__(self, client: Any | None = None) -> None:
        """client 仅供测试注入内存 Fake（生产不传，走 A207_OTS_* 环境变量）。"""
        if client is not None:
            self._client = client
            return
        self.endpoint = os.environ.get(OTS_ENDPOINT_ENV)
        self.instance = os.environ.get(OTS_INSTANCE_ENV)
        self.ak_id = os.environ.get(OTS_AK_ID_ENV)
        self.ak_secret = os.environ.get(OTS_AK_SECRET_ENV)
        missing = [name for name, val in (
            (OTS_ENDPOINT_ENV, self.endpoint),
            (OTS_INSTANCE_ENV, self.instance),
            (OTS_AK_ID_ENV, self.ak_id),
            (OTS_AK_SECRET_ENV, self.ak_secret),
        ) if not val]
        if missing:
            raise RuntimeError(
                f"Tablestore 后端缺少连接参数：{', '.join(missing)}。"
                f"请注入 A207_OTS_* 环境变量（生产默认后端，勿静默回退 JSON）。")
        self._client = None  # 惰性建连

    def _get_client(self):
        if self._client is None:
            import tablestore  # 延迟导入：LocalJson 后端无需 SDK

            self._client = tablestore.OTSClient(
                self.endpoint, self.ak_id, self.ak_secret, self.instance)
        return self._client

    # ---- 基础读写 ----

    @staticmethod
    def _pk_patient(patient_id: str) -> list[tuple[str, str]]:
        return [("patient_id", patient_id)]

    def _get_row(self, table: str, pk: list[tuple[str, str]]) -> dict[str, Any] | None:
        try:
            _, row, _ = self._get_client().get_row(table, pk)
        except Exception as exc:
            # fail-closed（对齐 P1 五审）：存储故障抛 RuntimeError（→ INTERNAL_ERROR），
            # 不得静默当"行不存在"处理——否则网络抖动时日记被误判为空。
            logger.error("Tablestore get_row 失败: table=%s pk=%s exc=%s", table, pk, exc)
            raise RuntimeError(
                f"Tablestore 读取失败（table={table}），详情见服务端日志") from exc
        if row is None:
            return None
        return {name: value for name, value, _ in row.attribute_columns}

    def _put_row_conditioned(self, table: str, pk: list[tuple[str, str]],
                             attrs: dict[str, Any], rev: int,
                             expect_exists: bool) -> None:
        """条件写：_rev 必须等于 rev（乐观锁）。条件不满足抛 OTSClientError。"""
        from tablestore import (ComparatorType, Condition, Row,
                                RowExistenceExpectation, SingleColumnCondition)

        expectation = (RowExistenceExpectation.EXPECT_EXIST if expect_exists
                       else RowExistenceExpectation.EXPECT_NOT_EXIST)
        col_cond = None
        if expect_exists:
            col_cond = SingleColumnCondition(
                _REV_COL, ComparatorType.EQUAL, rev)
        condition = Condition(expectation, col_cond)
        clean = {k: v for k, v in attrs.items() if v is not None}
        row = Row(pk, list(clean.items()))
        self._get_client().put_row(table, row, condition)

    def _save_row_locked(self, table: str, pk: list[tuple[str, str]],
                         attrs: dict[str, Any]) -> None:
        """乐观锁写入：读 _rev → 条件写 _rev+1 → 冲突重试（对齐 care）。

        五审（2026-08-13）：此前 save_diary/save_pew 无条件 PutRow 覆盖——多 worker
        并发 upsert 时后写者覆盖先写者（丢更新）。行级 _rev 条件写 + 重试，
        冲突仍失败抛 RuntimeError（fail-closed，不静默丢数据）。
        """
        from tablestore import OTSClientError

        last_err: Exception | None = None
        for _ in range(_MAX_RETRY):
            current = self._get_row(table, pk)
            rev = int(current.get(_REV_COL, 0)) if current else 0
            next_attrs = dict(attrs)
            next_attrs[_REV_COL] = rev + 1
            try:
                self._put_row_conditioned(
                    table, pk, next_attrs, rev, expect_exists=current is not None)
                return
            except OTSClientError as exc:
                last_err = exc  # 条件不满足 → 并发写冲突，重试
        raise RuntimeError(
            f"存储并发写冲突（{table} pk={pk}），重试 {_MAX_RETRY} 次仍失败，"
            f"拒绝静默覆盖: {last_err}")

    def _range_all(self, table: str) -> list[dict[str, Any]]:
        """全表 GetRange（主键升序）。返回 [{pk_dict, attrs_dict}]。"""
        from tablestore import INF_MAX, INF_MIN

        start = [("patient_id", INF_MIN)]
        end = [("patient_id", INF_MAX)]
        rows: list[dict[str, Any]] = []
        next_start = start
        while next_start is not None:
            consumed, next_start, row_list, _ = self._get_client().get_range(
                table, "FORWARD", next_start, end, limit=200)
            for row in row_list:
                pk_dict = {}
                for k, v in row.primary_key:
                    pk_dict[k] = v.decode() if isinstance(v, bytes) else v
                attrs_dict = {name: value for name, value, _ in row.attribute_columns}
                rows.append({"pk": pk_dict, "attrs": attrs_dict})
        return rows

    @staticmethod
    def _json_col(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False)

    # ---- 饮食日记（全量 entries ↔ 按患者分片）----

    def load_diary(self) -> dict[str, Any]:
        entries: list[dict[str, Any]] = []
        for item in self._range_all(TABLE_FOOD_DIARY):
            raw = item["attrs"].get("entries")
            if isinstance(raw, str):
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    data = []
                if isinstance(data, list):
                    entries.extend(data)
        return {"entries": entries}

    def save_diary(self, store: dict[str, Any]) -> None:
        entries = store.get("entries", [])
        by_patient: dict[str, list[dict[str, Any]]] = {}
        for e in entries:
            pid = e.get("patient_id")
            if not pid:
                continue
            by_patient.setdefault(pid, []).append(e)
        # 按患者分片覆盖写（行级 _rev 乐观锁，防并发丢更新）。
        # 业务为 append-only（upsert 只追加不清除），故不做"删除表内多余行"——
        # 如需删除语义需增加行级删除（当前无消费方）。
        for pid, patient_entries in by_patient.items():
            self._save_row_locked(TABLE_FOOD_DIARY, self._pk_patient(pid),
                                  {"entries": self._json_col(patient_entries),
                                   "updated_at": _now_iso()})

    # ---- PEW 历史（{patient_id: [points]} ↔ 按患者分片）----

    def load_pew(self) -> dict[str, Any]:
        store: dict[str, Any] = {}
        for item in self._range_all(TABLE_PEW_HISTORY):
            pid = item["pk"].get("patient_id")
            raw = item["attrs"].get("points")
            if pid is None:
                continue
            if isinstance(raw, str):
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    data = []
                if isinstance(data, list):
                    store[pid] = data
        return store

    def save_pew(self, store: dict[str, Any]) -> None:
        for pid, points in store.items():
            if not isinstance(points, list):
                continue
            self._save_row_locked(TABLE_PEW_HISTORY, self._pk_patient(pid),
                                  {"points": self._json_col(points),
                                   "updated_at": _now_iso()})


def _now_iso() -> str:
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")


def ensure_tablestore_tables() -> None:
    """创建/校验 Tablestore 表（幂等，仅建缺失表）。"""
    from tablestore import (CapacityUnit, Condition, OTSClient,
                            ReservedThroughput, RowExistenceExpectation,
                            TableMeta, TableOptions)

    endpoint = os.environ[OTS_ENDPOINT_ENV]
    instance = os.environ[OTS_INSTANCE_ENV]
    ak = os.environ[OTS_AK_ID_ENV]
    sk = os.environ[OTS_AK_SECRET_ENV]
    client = OTSClient(endpoint, ak, sk, instance)
    existing = set(client.list_table())

    def _create(table_name: str, pk_schema: list[tuple[str, str]]) -> None:
        if table_name in existing:
            return
        meta = TableMeta(table_name, pk_schema)
        options = TableOptions(time_to_live=-1, max_version=1)
        throughput = ReservedThroughput(capacity_unit=CapacityUnit(0, 0))
        client.create_table(meta, options, throughput)
        print(f"[ensure] 已创建表 {table_name}")

    _create(TABLE_FOOD_DIARY, [("patient_id", "STRING")])
    _create(TABLE_PEW_HISTORY, [("patient_id", "STRING")])
    print(f"[ensure] Tablestore 表就绪：{sorted(existing | {TABLE_FOOD_DIARY, TABLE_PEW_HISTORY})}")


def get_repository() -> NutritionRepository:
    """按环境变量选择后端：缺省 tablestore（生产）；显式 json（本地开发/测试）。"""
    backend = os.environ.get(STORAGE_BACKEND_ENV, "tablestore").strip().lower()
    if backend == "json":
        return LocalJsonRepository()
    return TablestoreRepository()


__all__ = [
    "NutritionRepository",
    "LocalJsonRepository",
    "TablestoreRepository",
    "ensure_tablestore_tables",
    "get_repository",
    "TABLE_FOOD_DIARY",
    "TABLE_PEW_HISTORY",
    "DIARY_STORE_FILENAME",
    "PEW_STORE_FILENAME",
]
