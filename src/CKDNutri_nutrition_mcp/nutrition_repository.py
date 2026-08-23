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
日记业务主路径按**患者行**读写（N-MEM-2，2026-08-14）：load_patient_diary /
save_patient_diary 只 GetRow/写该患者行——此前写路径已按患者分片、读路径仍全表
GetRange 合并回 {"entries": [...]}，单次"记一顿饭"= 全表扫描 + 回写每个患者行，
医院级（数千患儿 × 多年日记）OOM + O(患者数) 写放大。全量 load_diary/save_diary
保留给跨患者聚合场景（当前无消费方），业务主路径不得再走全表。
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from a207_policy import atomic_write_json, resolve_state_path
from a207_policy.storage import (  # 2026-08-15：共享 Tablestore 基础设施
    TablestoreBase,
    ensure_json_backend_allowed,
)

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
# 2026-08-21：孩子自报饮食记录表（child_foodlog）——仅 child_assistant 可写；
# 参考数据（不作医疗结论），按 patient_id 分片，行 = {entries, total_points,
# daily_points, last_points_date}。
TABLE_CHILD_FOODLOG = "child_foodlog"

# JSON 文件名（LocalJson 后端；core 测试仍引用 DIARY_STORE_FILENAME 常量）
DIARY_STORE_FILENAME = "diary_store.json"
PEW_STORE_FILENAME = "pew_history_store.json"
CHILD_FOODLOG_STORE_FILENAME = "child_foodlog_store.json"

# Tablestore 乐观锁版本列与重试次数（对齐 care repository）
_REV_COL = "_rev"
_MAX_RETRY = 3

# 本地开发数据目录 override（与旧 core._state_path 语义一致）
_DATA_DIR_ENV = "A207_NUTRITION_ASSESSMENT_DATA_DIR"

# S5 修复（2026-08-13）：乐观锁冲突重试时**合并业务字段**而非整行覆盖——
# 并发 upsert 日记/PEW 时，后写者不得覆盖先写者追加的 entries/points。
# 合并规则：JSON 数组列按元素 id 去重合并，标量 new 优先。


@runtime_checkable
class NutritionRepository(Protocol):
    """P2 存储契约：饮食日记（全量 entries 语义）+ PEW 历史（按患者分键）。"""

    # ---- 饮食日记 ----
    def load_diary(self) -> dict[str, Any]:
        """读取全部日记条目，返回 {"entries": [...]}；损坏 fail-closed 抛 RuntimeError。

        N-MEM-2（2026-08-14）：仅限跨患者聚合场景；业务单患者主路径必须用
        load_patient_diary（行级读，勿全表扫描）。
        """
        ...

    def save_diary(self, store: dict[str, Any]) -> None:
        """原子持久化全部日记条目（按患者分片覆盖写）。"""
        ...

    def load_patient_diary(self, patient_id: str) -> dict[str, Any]:
        """读取**单个患者**的日记条目，返回 {"entries": [...]}（行级读，N-MEM-2）。

        无记录 → {"entries": []}；损坏 JSON fail-closed 抛 RuntimeError（同 load_diary）。
        """
        ...

    def save_patient_diary(self, patient_id: str,
                           entries: list[dict[str, Any]]) -> None:
        """原子持久化**单个患者**的日记条目（行级写，N-MEM-2）。

        覆盖写该患者行（行级 _rev 乐观锁防并发丢更新），不触碰其他患者。
        """
        ...

    # ---- PEW 历史 ----
    def load_pew(self) -> dict[str, Any]:
        """读取全部 PEW 历史，返回 {patient_id: [points]}；损坏 fail-closed。

        N-MEM-3（2026-08-14）：仅限跨患者聚合/迁移场景；业务单患者主路径必须用
        load_patient_pew（行级读，勿全表扫描）。
        """
        ...

    def save_pew(self, store: dict[str, Any]) -> None:
        """原子持久化全部 PEW 历史（按患者分片覆盖写）。"""
        ...

    def load_patient_pew(self, patient_id: str) -> dict[str, Any]:
        """读取**单个患者**的 PEW 历史，返回 {patient_id: [points]}（行级读，N-MEM-3）。

        无记录 → {patient_id: []}；损坏 JSON fail-closed 抛 RuntimeError（同 load_pew）。
        """
        ...

    def save_patient_pew(self, patient_id: str,
                         points: list[dict[str, Any]]) -> None:
        """原子持久化**单个患者**的 PEW 历史（行级写，N-MEM-3）。

        覆盖写该患者行（行级 _rev 乐观锁防并发丢更新），不触碰其他患者。
        """
        ...

    # ---- 孩子自报饮食（child_foodlog，2026-08-21）----
    def load_patient_child_foodlog(self, patient_id: str) -> dict[str, Any]:
        """读取**单个患者**的孩子自报饮食行（行级读，N-MEM-2 同口径）。

        返回 {entries: [...], total_points, daily_points, last_points_date}；
        无记录返回 {"entries": [], "total_points": 0, "daily_points": 0,
        "last_points_date": ""}；损坏 JSON fail-closed 抛 RuntimeError。
        """
        ...

    def save_patient_child_foodlog(self, patient_id: str,
                                   row: dict[str, Any]) -> None:
        """原子持久化**单个患者**的孩子自报饮食行（行级写）。

        覆盖写该患者行（行级 _rev 乐观锁防并发丢更新），不触碰其他患者。
        """
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
        with open(path, encoding="utf-8") as f:
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
    """本地 JSON 文件后端（v0.5 起仅本地开发/测试，显式 A207_STORAGE_BACKEND=json）。

    N4（2026-08-16，九审）：进程内写锁 _LOCAL_JSON_LOCK——save_patient_diary /
    save_patient_pew 是 load→过滤→整文件原子写（RMW），无锁时两线程/进程同患者
    并发 append，后写者基于旧快照覆盖 → 先写条目丢失（经典 TOCTOU）。Tablestore
    后端有 _rev 乐观锁重试，本地 json 后端（dev/A207_ACCEPT_DEV_STORAGE=1）此前
    无等价保护。threading.Lock 仅保进程内；跨进程仍建议单实例部署（与 care 同口径）。
    """

    # N4：本地 JSON 后端 RMW 写锁（进程内串行化 load→filter→atomic write）
    # P3（2026-08-23 审查）：threading.Lock → RLock——读方法（load_diary/load_pew/
    # _load_child_foodlog_file）此前全程无锁，多线程下与写线程的 atomic_write_json
    # （写临时+os.replace）在 Windows/共享卷可能产生瞬时读空或 PermissionError。
    # 升级 RLock 并在读流程加锁；RLock 可重入，save_* 内部调用 load_* 不会自死锁。
    _LOCAL_JSON_LOCK = threading.RLock()

    # ---- 饮食日记 ----
    def load_diary(self) -> dict[str, Any]:
        # P3（2026-08-23 审查）：读加锁，与写线程 atomic_write_json 互斥（防 rename
        # 瞬间读空/PermissionError）。RLock 可重入，save_patient_diary 内调本方法安全。
        with self._LOCAL_JSON_LOCK:
            return _read_json_fail_closed(_state_path(DIARY_STORE_FILENAME),
                                          "日记库", {"entries": []})

    def save_diary(self, store: dict[str, Any]) -> None:
        atomic_write_json(_state_path(DIARY_STORE_FILENAME), store)

    def load_patient_diary(self, patient_id: str) -> dict[str, Any]:
        # 本地 JSON 开发模式：文件即全量，过滤该患者（无分片需求）
        store = self.load_diary()
        return {"entries": [e for e in store.get("entries", [])
                            if e.get("patient_id") == patient_id]}

    def save_patient_diary(self, patient_id: str,
                           entries: list[dict[str, Any]]) -> None:
        # P3（2026-08-23 复审）：防御性规约——强制补全每条 entry 的 patient_id，
        # 防止上层漏传/传 None 导致该条在 load_patient_diary 时被 `!= patient_id`
        # 过滤成不可见的「幽灵数据」（LocalJson）或被跨患者保存静默丢弃（Tablestore）。
        norm_entries = [{**e, "patient_id": patient_id} for e in entries]
        # N4：RMW 全程持锁——防并发 append 后写者覆盖先写条目（TOCTOU）
        with self._LOCAL_JSON_LOCK:
            store = self.load_diary()
            others = [e for e in store.get("entries", [])
                      if e.get("patient_id") != patient_id]
            store["entries"] = others + norm_entries
            self.save_diary(store)

    # ---- PEW 历史 ----
    def load_pew(self) -> dict[str, Any]:
        # P3（2026-08-23 审查）：读加锁（同 load_diary）
        with self._LOCAL_JSON_LOCK:
            return _read_json_fail_closed(_state_path(PEW_STORE_FILENAME),
                                          "PEW 历史库", {})

    def save_pew(self, store: dict[str, Any]) -> None:
        atomic_write_json(_state_path(PEW_STORE_FILENAME), store)

    def load_patient_pew(self, patient_id: str) -> dict[str, Any]:
        # 本地 JSON 开发模式：文件即全量，过滤该患者（无分片需求）
        store = self.load_pew()
        pts = store.get(patient_id)
        return {patient_id: list(pts) if isinstance(pts, list) else []}

    def save_patient_pew(self, patient_id: str,
                         points: list[dict[str, Any]]) -> None:
        # N4：RMW 全程持锁（同 save_patient_diary，防 TOCTOU 覆盖）
        with self._LOCAL_JSON_LOCK:
            store = self.load_pew()
            store[patient_id] = list(points)
            self.save_pew(store)

    # ---- 孩子自报饮食（child_foodlog，2026-08-21）----
    def _load_child_foodlog_file(self) -> dict[str, Any]:
        # P3（2026-08-23 审查）：读加锁（同 load_diary）
        with self._LOCAL_JSON_LOCK:
            return _read_json_fail_closed(_state_path(CHILD_FOODLOG_STORE_FILENAME),
                                          "孩子自报饮食库", {})

    def load_patient_child_foodlog(self, patient_id: str) -> dict[str, Any]:
        store = self._load_child_foodlog_file()
        row = store.get(patient_id)
        if not isinstance(row, dict):
            return {"entries": [], "total_points": 0,
                    "daily_points": 0, "last_points_date": ""}
        # 缺键行（旧数据/手工写入）→ 安全默认；entries 非 list 视为损坏 fail-closed
        entries = row.get("entries")
        if not isinstance(entries, list):
            raise RuntimeError(
                f"孩子自报饮食行 entries 类型错误（patient_id={patient_id}）："
                f"期望 list，实际 {type(entries).__name__}——拒绝静默清空")
        # P2-9（2026-08-23 审查）：返回时对 entries 做隔离（list(entries)），避免调用方
        # 原地修改内部列表引用而污染尚未持久化的内存结构（defense-in-depth）。
        return {
            "entries": list(entries),
            "total_points": int(row.get("total_points", 0) or 0),
            "daily_points": int(row.get("daily_points", 0) or 0),
            "last_points_date": str(row.get("last_points_date", "") or ""),
        }

    def save_patient_child_foodlog(self, patient_id: str,
                                   row: dict[str, Any]) -> None:
        # P3（2026-08-23 复审）：白名单清洗——整行写入前只提取标量键并过滤 None /
        # 未序列化容器，防止后续切到 Tablestore 后端时 PutRow 因 None 值或非标类型
        # （如 list/dict 扩展字段）抛 OTSClientError / TypeError 崩溃。LocalJson 下
        # 此清洗亦消除幽灵 None 列、保证 round-trip 稳定（entries 在 LocalJson 直接存
        # list，不需 _json_col 序列化；Tablestore 后端同名方法走 _json_col）。
        attrs: dict[str, Any] = {
            "entries": list(row.get("entries", [])),
            "total_points": int(row.get("total_points", 0) or 0),
            "daily_points": int(row.get("daily_points", 0) or 0),
            "last_points_date": str(row.get("last_points_date", "") or ""),
            "updated_at": _now_iso(),
        }
        # N4：RMW 全程持锁（同 save_patient_diary，防 TOCTOU 覆盖）
        with self._LOCAL_JSON_LOCK:
            store = self._load_child_foodlog_file()
            store[patient_id] = attrs
            atomic_write_json(_state_path(CHILD_FOODLOG_STORE_FILENAME), store)


class TablestoreRepository(TablestoreBase):
    """阿里云表格存储后端（生产默认，缺 OTS 参数 fail-fast）。

    2026-08-15：连接/乐观锁/GetRange/建表等基础设施收敛到
    a207_policy.storage.TablestoreBase（消除三包 ~750 行复制），本类仅留业务方法。
    """

    @staticmethod
    def _pk_patient(patient_id: str) -> list[tuple[str, str]]:
        return [("patient_id", patient_id)]


    # ---- 饮食日记（全量 entries ↔ 按患者分片）----

    def load_diary(self) -> dict[str, Any]:
        """全量读取全部患者日记（N-MEM-2：仅跨患者聚合场景；业务单患者主路径
        必须用 load_patient_diary 行级读，勿全表扫描）。"""
        entries: list[dict[str, Any]] = []
        for item in self._range_all(TABLE_FOOD_DIARY, ["patient_id"]):
            raw = item["attrs"].get("entries")
            if raw is None:
                continue
            # P1-9（2026-08-18 四审）：非字符串 entries 类型 fail-closed——此前仅
            # `isinstance(raw, str)` 走解析，dict/list 等错误类型被静默跳过（读到的
            # 空列表经 save 覆盖写回 → 日记**永久丢失**且无告警，同 X1 原则）。
            if not isinstance(raw, str):
                raise RuntimeError(
                    f"饮食日记列 entries 类型错误：期望 JSON 字符串，实际为 "
                    f"{type(raw).__name__}——拒绝静默清空，请人工修复 Tablestore 该行数据")
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                # X1（2026-08-14）：损坏 JSON 一律抛错（fail-closed，与 JSON 端
                # _read_json_file 同口径）——此前 except 静默置 []，读到的空列表经
                # save_diary 全量覆盖写回 → 患儿饮食日记**永久丢失**且无任何告警。
                # 损坏即显式失败，交由上层定位（人工修复或降级），绝不静默。
                raise RuntimeError(
                    f"饮食日记列 entries 损坏（非法 JSON）：{exc}——拒绝静默清空，"
                    "请人工修复 Tablestore 该行数据") from exc
            if not isinstance(data, list):
                raise RuntimeError(
                    f"饮食日记列 entries 解析结果类型错误：期望 list，实际为 "
                    f"{type(data).__name__}——拒绝静默清空，请人工修复 Tablestore 该行数据")
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

    def load_patient_diary(self, patient_id: str) -> dict[str, Any]:
        """行级读**单个患者**日记（N-MEM-2：GetRow(pk=patient_id)，不扫全表）。

        无该患者行 → {"entries": []}；损坏 JSON/类型错误 fail-closed 抛 RuntimeError
        （X1 口径：拒绝静默清空后经 save 覆盖写回丢数据）。
        """
        row = self._get_row(TABLE_FOOD_DIARY, self._pk_patient(patient_id))
        if row is None:
            return {"entries": []}
        raw = row.get("entries")
        # X1 对齐（2026-08-23 审查）：无 entries 列（raw=None）视为无数据返回空；
        # 但 raw 存在却非 str（脏类型/字段篡改）必须 fail-closed 抛错，否则下游
        # 读-改-写会把历史日记经 save 覆盖清空。与 load_diary / load_patient_child_foodlog 口径一致。
        if raw is None:
            return {"entries": []}
        if not isinstance(raw, str):
            raise RuntimeError(
                f"饮食日记列 entries 类型错误（patient_id={patient_id}）："
                f"期望 JSON 字符串，实际 {type(raw).__name__}——拒绝静默清空")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"饮食日记列 entries 损坏（非法 JSON，patient_id={patient_id}）：{exc}"
                "——拒绝静默清空，请人工修复 Tablestore 该行数据") from exc
        if not isinstance(data, list):
            raise RuntimeError(
                f"饮食日记列 entries 类型错误（patient_id={patient_id}）："
                f"期望 list，实际 {type(data).__name__}——拒绝静默清空")
        return {"entries": data}

    def save_patient_diary(self, patient_id: str,
                           entries: list[dict[str, Any]]) -> None:
        """行级写**单个患者**日记（N-MEM-2：只写该患者行，行级 _rev 乐观锁）。"""
        # P3（2026-08-23 复审）：防御性规约——强制补全 patient_id（同 LocalJson 后端），
        # 防止上层漏传条目变幽灵数据。
        norm_entries = [{**e, "patient_id": patient_id} for e in entries]
        self._save_row_locked(TABLE_FOOD_DIARY, self._pk_patient(patient_id),
                              {"entries": self._json_col(norm_entries),
                               "updated_at": _now_iso()})

    # ---- PEW 历史（{patient_id: [points]} ↔ 按患者分片）----

    def load_pew(self) -> dict[str, Any]:
        """全量读取（N-MEM-3：仅跨患者聚合/迁移场景；业务单患者主路径用
        load_patient_pew 行级读，勿全表扫描）。"""
        store: dict[str, Any] = {}
        for item in self._range_all(TABLE_PEW_HISTORY, ["patient_id"]):
            pid = item["pk"].get("patient_id")
            raw = item["attrs"].get("points")
            if pid is None:
                continue
            if isinstance(raw, str):
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError as exc:
                    # X1（2026-08-14）：同 load_diary——损坏 JSON 抛错 fail-closed，
                    # 防止空列表经 save 全量覆盖写回导致 PEW 历史永久丢失。
                    raise RuntimeError(
                        f"PEW 历史列 points 损坏（非法 JSON）：{exc}——拒绝静默清空，"
                        "请人工修复 Tablestore 该行数据") from exc
                if isinstance(data, list):
                    store[pid] = data
        return store

    def save_pew(self, store: dict[str, Any]) -> None:
        """全量按患者分片覆盖写（N-MEM-3：仅跨患者聚合/迁移场景）。"""
        for pid, points in store.items():
            if not isinstance(points, list):
                continue
            self._save_row_locked(TABLE_PEW_HISTORY, self._pk_patient(pid),
                                  {"points": self._json_col(points),
                                   "updated_at": _now_iso()})

    def load_patient_pew(self, patient_id: str) -> dict[str, Any]:
        """行级读**单个患者** PEW 历史（N-MEM-3：GetRow(pk=patient_id)，不扫全表）。

        无该患者行 → {patient_id: []}；损坏 JSON/类型错误 fail-closed 抛 RuntimeError
        （X1 口径：拒绝静默清空后经 save 覆盖写回丢数据）。
        """
        row = self._get_row(TABLE_PEW_HISTORY, self._pk_patient(patient_id))
        if row is None:
            return {patient_id: []}
        raw = row.get("points")
        # X1 对齐（2026-08-23 审查）：无 points 列视为无数据；非 None 且非 str 必须
        # fail-closed 抛错，防止读-改-写清空 PEW 历史。
        if raw is None:
            return {patient_id: []}
        if not isinstance(raw, str):
            raise RuntimeError(
                f"PEW 历史列 points 类型错误（patient_id={patient_id}）："
                f"期望 JSON 字符串，实际 {type(raw).__name__}——拒绝静默清空")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"PEW 历史列 points 损坏（非法 JSON，patient_id={patient_id}）：{exc}"
                "——拒绝静默清空，请人工修复 Tablestore 该行数据") from exc
        if not isinstance(data, list):
            raise RuntimeError(
                f"PEW 历史列 points 类型错误（patient_id={patient_id}）："
                f"期望 list，实际 {type(data).__name__}——拒绝静默清空")
        return {patient_id: data}

    def save_patient_pew(self, patient_id: str,
                         points: list[dict[str, Any]]) -> None:
        """行级写**单个患者** PEW 历史（N-MEM-3：只写该患者行，行级 _rev 乐观锁）。"""
        self._save_row_locked(TABLE_PEW_HISTORY, self._pk_patient(patient_id),
                              {"points": self._json_col(list(points)),
                               "updated_at": _now_iso()})

    # ---- 孩子自报饮食（child_foodlog，2026-08-21）----

    def load_patient_child_foodlog(self, patient_id: str) -> dict[str, Any]:
        """行级读**单个患者**孩子自报饮食（GetRow(pk=patient_id)，不扫全表）。

        无该患者行 → {"entries": [], "total_points": 0, "daily_points": 0,
        "last_points_date": ""}；损坏 JSON/类型错误 fail-closed 抛 RuntimeError
        （X1 口径：拒绝静默清空后经 save 覆盖写回丢数据）。
        """
        row = self._get_row(TABLE_CHILD_FOODLOG, self._pk_patient(patient_id))
        if row is None:
            return {"entries": [], "total_points": 0,
                    "daily_points": 0, "last_points_date": ""}
        entries: list[dict[str, Any]] = []
        raw = row.get("entries")
        if isinstance(raw, str):
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"孩子自报饮食列 entries 损坏（非法 JSON，patient_id={patient_id}）："
                    f"{exc}——拒绝静默清空，请人工修复 Tablestore 该行数据") from exc
            if not isinstance(data, list):
                raise RuntimeError(
                    f"孩子自报饮食列 entries 类型错误（patient_id={patient_id}）："
                    f"期望 list，实际 {type(data).__name__}——拒绝静默清空")
            entries = data
        elif raw is not None:
            raise RuntimeError(
                f"孩子自报饮食列 entries 类型错误（patient_id={patient_id}）："
                f"期望 JSON 字符串，实际 {type(raw).__name__}——拒绝静默清空")
        # P2-9（2026-08-23 审查）：返回时对 entries 做隔离（list(entries)），避免调用方
        # 原地修改内部列表引用而污染尚未持久化的内存结构（defense-in-depth）。
        return {
            "entries": list(entries),
            "total_points": int(row.get("total_points", 0) or 0),
            "daily_points": int(row.get("daily_points", 0) or 0),
            "last_points_date": str(row.get("last_points_date", "") or ""),
        }

    def save_patient_child_foodlog(self, patient_id: str,
                                   row: dict[str, Any]) -> None:
        """行级写**单个患者**孩子自报饮食（行级 _rev 乐观锁，只写该患者行）。"""
        # P3（2026-08-23 复审）：白名单清洗——Tablestore 属性列仅支持标量类型，
        # None 值或非序列化容器（如 list/dict 扩展字段）直接 PutRow 会抛
        # OTSClientError / TypeError。只提取已知标量键 + 序列化 entries。
        attrs: dict[str, Any] = {
            "entries": self._json_col(list(row.get("entries", []))),
            "total_points": int(row.get("total_points", 0) or 0),
            "daily_points": int(row.get("daily_points", 0) or 0),
            "last_points_date": str(row.get("last_points_date", "") or ""),
            "updated_at": _now_iso(),
        }
        self._save_row_locked(TABLE_CHILD_FOODLOG, self._pk_patient(patient_id), attrs)


def _now_iso() -> str:
    # C2 修复（2026-08-14）：aware UTC——此前 naive datetime.now() 与 care/P1 的
    # UTC 口径混存（同进程多包 updated_at 两种口径，审计/时间线错位）。
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_tablestore_tables() -> None:
    """创建/校验 Tablestore 表（幂等，仅建缺失表；2026-08-15 收敛到 storage.ensure_tables）。"""
    TablestoreBase().ensure_tables({
        TABLE_FOOD_DIARY: [("patient_id", "STRING")],
        TABLE_PEW_HISTORY: [("patient_id", "STRING")],
        TABLE_CHILD_FOODLOG: [("patient_id", "STRING")],
    })


# 后端实例缓存（C3 修复，2026-08-14）：按 backend 缓存 Repository 实例——此前每请求
# 新建 OTSClient 连接池（与注释自称"单例"不符）。缓存后首请求建连、后续复用。
_REPO_CACHE: dict[str, Any] = {}
_REPO_CACHE_LOCK = threading.Lock()


def get_repository() -> NutritionRepository:
    """按环境变量选择后端：缺省 tablestore（生产）；显式 json（本地开发/测试）。
    实例按 backend 缓存（P2：防每请求重复建 OTSClient）。"""
    backend = os.environ.get(STORAGE_BACKEND_ENV, "tablestore").strip().lower()
    if backend == "json":
        # 生产护栏（2026-08-15）：json 后端仅限显式确认（A207_ACCEPT_DEV_STORAGE=1）
        ensure_json_backend_allowed()  # 未确认即抛 RuntimeError（fail-closed）
    repo = _REPO_CACHE.get(backend)
    if repo is None:
        with _REPO_CACHE_LOCK:  # double-check 防并发首调重复构建
            repo = _REPO_CACHE.get(backend)
            if repo is None:
                repo = (LocalJsonRepository() if backend == "json"
                        else TablestoreRepository())
                _REPO_CACHE[backend] = repo
    return repo


__all__ = [
    "CHILD_FOODLOG_STORE_FILENAME",
    "DIARY_STORE_FILENAME",
    "PEW_STORE_FILENAME",
    "TABLE_CHILD_FOODLOG",
    "TABLE_FOOD_DIARY",
    "TABLE_PEW_HISTORY",
    "LocalJsonRepository",
    "NutritionRepository",
    "TablestoreRepository",
    "ensure_tablestore_tables",
    "get_repository",
]
