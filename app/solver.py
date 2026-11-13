"""核心求解逻辑。

把 Europe/Berlin 的本地时刻翻译成 UTC 候选，并在整份日志上施加
“真实时刻（UTC）严格递增”约束，用前向-后向传播求出每一行
在全文件约束下仍然可行的全部候选。

设计原则：
- 不默认选择夏令时或冬令时；
- 不重排记录，序号顺序即时间顺序；
- 每个本地时刻的所有合法解释都保留，由全局约束或用户选择来消歧。
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo("Europe/Berlin")
UTC = timezone.utc
MAX_ROWS = 5000

SEQ_NAMES = {"seq", "id", "no", "nr", "index", "序号", "编号"}
TIME_NAMES = {"local_time", "localtime", "time", "timestamp", "datetime",
              "本地时刻", "本地时间", "时刻", "时间"}
LEVEL_NAMES = {"water_level", "waterlevel", "level", "value", "水位", "水位值"}


class CsvStructureError(Exception):
    """CSV 结构错误（表头/列/序号/数值格式），整份拒绝上传。"""


# ---------------------------------------------------------------- 时刻候选

def wall_to_utc_candidates(local: datetime) -> list[datetime]:
    """本地（naive）时刻在 Europe/Berlin 下对应的所有 UTC 时刻。

    - 普通时刻：1 个；
    - 秋季回拨重叠时刻：2 个（CEST 与 CET 各一次）；
    - 春季跳跃不存在时刻：0 个。
    """
    out: list[datetime] = []
    for fold in (0, 1):
        aware = local.replace(tzinfo=BERLIN, fold=fold)
        utc = aware.astimezone(UTC)
        # 往返校验：该 UTC 在柏林的墙上读数必须等于原本地时刻
        if utc.astimezone(BERLIN).replace(tzinfo=None) == local and utc not in out:
            out.append(utc)
    return sorted(out)


def fmt_offset(off: timedelta) -> str:
    total = int(off.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"UTC{sign}{total // 3600:02d}:{(total % 3600) // 60:02d}"


def candidate_label(utc: datetime) -> str:
    b = utc.astimezone(BERLIN)
    return (f"{utc:%Y-%m-%d %H:%M:%S}Z"
            f"（柏林 {b.tzname()} {fmt_offset(b.utcoffset())}）")


def parse_timestamp(text: str):
    """解析 '2023-10-29 02:30[:ss][+02:00|Z]'。

    返回 (naive_local, offset_timedelta|None, anchored_utc|None)。
    格式错误抛 ValueError。
    """
    t = text.strip()
    if not t:
        raise ValueError("时刻为空")
    try:
        dt = datetime.fromisoformat(t)
    except ValueError:
        raise ValueError(
            f"无法解析时刻 {text!r}，期望形如 "
            f"“2023-10-29 02:30” 或 “2023-10-29 02:30+02:00”")
    if dt.tzinfo is None:
        return dt, None, None
    return dt.replace(tzinfo=None), dt.utcoffset(), dt.astimezone(UTC)


def analyze_timestamp(local_text: str):
    """返回 (candidates: list[datetime], error: str|None)。

    - 带偏移的时刻是锚点：瞬间唯一，但偏移必须与柏林规则一致，
      且本地墙上时刻必须真实存在，否则视为矛盾。
    - 不存在的本地时刻（春季跳跃）：0 候选 + 错误说明。
    """
    naive, offset, anchor = parse_timestamp(local_text)
    valid = wall_to_utc_candidates(naive)
    if anchor is not None:
        if not valid:
            return [], (f"本地时刻 {naive} 在 Europe/Berlin 不存在"
                        f"（夏令时春季跳跃），即使带偏移 {fmt_offset(offset)} 也无有效解释")
        if anchor not in valid:
            allowed = "、".join(
                fmt_offset(u.astimezone(BERLIN).utcoffset()) for u in valid)
            return [], (f"锚点偏移 {fmt_offset(offset)} 与 Europe/Berlin 规则矛盾："
                        f"本地时刻 {naive} 当时只可能是 {allowed}")
        return [anchor], None
    if not valid:
        return [], (f"本地时刻 {naive} 在 Europe/Berlin 不存在"
                    f"（夏令时春季跳跃），该行无可行解释")
    return valid, None


# ---------------------------------------------------------------- CSV 结构解析

@dataclass
class ParsedRow:
    line_no: int          # 数据行号（从 1 开始，不含表头）
    seq: int
    local_text: str
    water_text: str
    candidates: list      # list[datetime]，语义错误时为空
    error: str | None


def _map_columns(header: list[str]) -> dict:
    idx = {"seq": None, "time": None, "level": None}
    for i, name in enumerate(header):
        n = name.strip().lower()
        if n in SEQ_NAMES and idx["seq"] is None:
            idx["seq"] = i
        elif n in TIME_NAMES and idx["time"] is None:
            idx["time"] = i
        elif n in LEVEL_NAMES and idx["level"] is None:
            idx["level"] = i
    remaining = [i for i in range(len(header)) if i not in idx.values()]
    for role in ("seq", "time", "level"):
        if idx[role] is None:
            if remaining:
                idx[role] = remaining.pop(0)
            else:
                raise CsvStructureError(
                    f"表头缺少“{role}”列，且剩余列不足；"
                    f"需要 序号,本地时刻,水位 三列")
    return idx


def parse_csv_text(text: str) -> list[ParsedRow]:
    """解析整份 CSV。结构错误抛 CsvStructureError（整份拒绝）；
    语义问题（时刻不存在、锚点矛盾）记录在行内，由导出环节阻止。"""
    try:
        raw = list(csv.reader(io.StringIO(text)))
    except csv.Error as e:
        raise CsvStructureError(f"CSV 解析失败：{e}")
    raw = [r for r in raw if any(c.strip() for c in r)]
    if not raw:
        raise CsvStructureError("文件为空")
    header, data = raw[0], raw[1:]
    if not data:
        raise CsvStructureError("只有表头，没有数据行")
    if len(data) > MAX_ROWS:
        raise CsvStructureError(f"数据行 {len(data)} 超过上限 {MAX_ROWS} 行")
    cols = _map_columns(header)

    out: list[ParsedRow] = []
    prev_seq = None
    for line_no, r in enumerate(data, start=1):
        def cell(role: str) -> str:
            i = cols[role]
            return r[i].strip() if i < len(r) else ""

        seq_s, time_s, level_s = cell("seq"), cell("time"), cell("level")
        try:
            seq = int(seq_s)
        except ValueError:
            raise CsvStructureError(f"行 {line_no}：序号 {seq_s!r} 不是整数")
        if prev_seq is not None and seq <= prev_seq:
            raise CsvStructureError(
                f"行 {line_no}：序号 {seq} 未严格递增（上一行为 {prev_seq}）")
        prev_seq = seq
        try:
            float(level_s)
        except ValueError:
            raise CsvStructureError(f"行 {line_no}：水位 {level_s!r} 不是数值")
        try:
            cands, err = analyze_timestamp(time_s)
        except ValueError as e:
            raise CsvStructureError(f"行 {line_no}：{e}")
        out.append(ParsedRow(line_no, seq, time_s, level_s, cands, err))
    return out


# ---------------------------------------------------------------- 全局约束传播

@dataclass
class RowIn:
    id: int
    line_no: int
    seq: int
    local_text: str
    water_text: str
    candidates: list      # list[datetime]
    error: str | None


def propagate(chains: list[list[datetime]]):
    """前向-后向传播。

    返回 (feasible, fwd, bwd)：
    - fwd[i]：能作为前 i 行严格递增序列收尾的候选；
    - bwd[i]：能作为 i..n 行严格递增序列开头的候选；
    - feasible[i] = fwd[i] ∩ bwd[i]，即全链约束下仍可行的候选。
    """
    n = len(chains)
    fwd: list[list[datetime]] = [[] for _ in range(n)]
    for i, cands in enumerate(chains):
        prev = fwd[i - 1] if i else None
        for c in cands:
            if prev is None or any(p < c for p in prev):
                fwd[i].append(c)
    bwd: list[list[datetime]] = [[] for _ in range(n)]
    for i in range(n - 1, -1, -1):
        nxt = bwd[i + 1] if i < n - 1 else None
        for c in chains[i]:
            if nxt is None or any(c < s for s in nxt):
                bwd[i].append(c)
    feasible = [sorted(set(fwd[i]) & set(bwd[i])) for i in range(n)]
    return feasible, fwd, bwd


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def validate(rows: list[RowIn], selections: dict[int, datetime]) -> dict:
    """整份校验。selections: row_id -> 用户选定的 UTC。"""
    pinned: list[list[datetime]] = []
    for r in rows:
        cands = list(r.candidates)
        sel = selections.get(r.id)
        if sel is not None and sel in cands:
            cands = [sel]
        pinned.append(cands)

    # 语义错误行不参与递增链（文件已被阻止，避免错误级联淹没其它行）
    idxs = [i for i, r in enumerate(rows) if r.error is None]
    feas_sub, fwd_sub, bwd_sub = propagate([pinned[i] for i in idxs])
    feasible: list[list[datetime]] = [[] for _ in rows]
    fwd_ok = [False] * len(rows)
    bwd_ok = [False] * len(rows)
    for k, i in enumerate(idxs):
        feasible[i] = feas_sub[k]
        fwd_ok[i] = bool(fwd_sub[k])
        bwd_ok[i] = bool(bwd_sub[k])

    out_rows = []
    problems = []
    n_unique = n_ambiguous = 0
    for i, r in enumerate(rows):
        sel = selections.get(r.id)
        if r.error is not None:
            state = "error"
            problems.append({"line_no": r.line_no, "seq": r.seq, "reason": r.error})
        elif not feasible[i]:
            state = "conflict"
            if fwd_ok[i] and not bwd_ok[i]:
                hint = "本行及其之前可以成立，但其后方存在矛盾"
            elif bwd_ok[i] and not fwd_ok[i]:
                hint = "本行及其之后可以成立，但其前方存在矛盾"
            else:
                hint = "本行前后均无法满足"
            problems.append({
                "line_no": r.line_no, "seq": r.seq,
                "reason": f"与全文件严格递增约束矛盾（{hint}）："
                          f"没有任何候选能同时满足前后行"})
        elif len(feasible[i]) == 1:
            state = "unique"
            n_unique += 1
        else:
            state = "ambiguous"
            n_ambiguous += 1
        out_rows.append({
            "row_id": r.id,
            "line_no": r.line_no,
            "seq": r.seq,
            "local_text": r.local_text,
            "water_text": r.water_text,
            "state": state,
            "error": r.error,
            "selected_utc": iso(sel) if sel is not None else None,
            "candidates": [{
                "utc": iso(c),
                "label": candidate_label(c),
                "feasible": c in feasible[i],
                "selected": sel == c,
            } for c in r.candidates],
        })

    can_export = not problems and all(len(f) == 1 for f in feasible) and bool(rows)
    status = "resolved" if can_export else ("blocked" if problems else "ambiguous")
    return {
        "status": status,
        "can_export": can_export,
        "total": len(rows),
        "n_unique": n_unique,
        "n_ambiguous": n_ambiguous,
        "n_problems": len(problems),
        "problems": problems,
        "rows": out_rows,
        "feasible": [[iso(c) for c in f] for f in feasible],
    }
