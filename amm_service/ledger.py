"""交易流水查询：过滤与分页（纯函数，不持有状态）。

口径
----
- 流水按 seq 升序存储，查询默认按 seq 倒序（最新在前）返回。
- 过滤条件均可选：pool_id / user / op / 时间范围 [since_ms, until_ms]（闭区间）。
- 分页参数非法（page < 1、page_size 越界）抛出 InvalidParameter，
  由服务层统一转成可区分失败原因；total 始终是过滤后的全集大小，
  与当前页无关，保证「每页返回的总数和实际过滤结果一致」。
"""
from __future__ import annotations

from collections.abc import Iterable

from .errors import InvalidParameter
from .models import LedgerEntry

MAX_PAGE_SIZE = 500


def filter_entries(
    entries: Iterable[LedgerEntry],
    *,
    pool_id: str | None = None,
    user: str | None = None,
    op: str | None = None,
    since_ms: int | None = None,
    until_ms: int | None = None,
) -> list[LedgerEntry]:
    """按可选条件过滤，返回按 seq 升序的列表。"""
    if op is not None and op not in ("add", "remove", "swap"):
        raise InvalidParameter(f"op must be one of add/remove/swap, got {op!r}")
    if since_ms is not None and until_ms is not None and since_ms > until_ms:
        raise InvalidParameter("since_ms must not exceed until_ms")
    out: list[LedgerEntry] = []
    for e in entries:
        if pool_id is not None and e.pool_id != pool_id:
            continue
        if user is not None and e.user != user:
            continue
        if op is not None and e.op != op:
            continue
        if since_ms is not None and e.ts_ms < since_ms:
            continue
        if until_ms is not None and e.ts_ms > until_ms:
            continue
        out.append(e)
    return out


def paginate(
    entries: list[LedgerEntry],
    *,
    page: int,
    page_size: int,
) -> dict:
    """对过滤结果分页；total 为过滤后全集大小，items 为当前页（最新在前）。"""
    if page < 1:
        raise InvalidParameter(f"page must be >= 1, got {page}")
    if not (1 <= page_size <= MAX_PAGE_SIZE):
        raise InvalidParameter(
            f"page_size must be within [1, {MAX_PAGE_SIZE}], got {page_size}"
        )
    ordered = sorted(entries, key=lambda e: e.seq, reverse=True)
    start = (page - 1) * page_size
    items = ordered[start:start + page_size]
    return {
        "total": len(entries),
        "page": page,
        "page_size": page_size,
        "items": [entry_to_dict(e) for e in items],
    }


def entry_to_dict(e: LedgerEntry) -> dict:
    return {
        "seq": e.seq,
        "ts_ms": e.ts_ms,
        "pool_id": e.pool_id,
        "user": e.user,
        "op": e.op,
        "request_id": e.request_id,
        "status": e.status,
        "error_code": e.error_code,
        "detail": dict(e.detail),
    }
