"""Pure filtering and text formatting for generation task views."""

from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping
from typing import Any, Dict, List


STATUS_LABELS = {
    "queued": "排队",
    "running": "进行中",
    "succeeded": "完成",
    "failed": "失败",
    "cancelled": "已取消",
}


def filter_image_tasks(
    tasks: Iterable[Mapping[str, Any]],
    session_key: str = "",
    *,
    include_finished: bool = False,
    limit: int = 10,
) -> List[Dict[str, Any]]:
    active_status = {"queued", "running"}
    rows: List[Dict[str, Any]] = []
    items = sorted(
        tasks,
        key=lambda item: float(item.get("created_ts") or 0),
        reverse=True,
    )
    for task in items:
        owner = str(task.get("owner_session") or "")
        if session_key and owner and owner != session_key:
            continue
        if not include_finished and task.get("status") not in active_status:
            continue
        source = str(task.get("source") or "")
        if session_key and source.startswith("web") and owner != session_key:
            continue
        rows.append(copy.deepcopy(dict(task)))
        if len(rows) >= max(1, limit):
            break
    return rows


def format_task_list_text(tasks: Iterable[Mapping[str, Any]]) -> str:
    items = list(tasks)
    if not items:
        return "现在没有进行中的出图/视频任务。"
    lines = ["进行中的任务："]
    for index, task in enumerate(items, 1):
        task_id = str(task.get("task_id") or "")
        status = str(task.get("status") or "")
        status_cn = STATUS_LABELS.get(status, status)
        request = task.get("request_data") if isinstance(task.get("request_data"), dict) else {}
        kind = str(request.get("kind") or "")
        if not kind:
            source = str(task.get("source") or "")
            kind = "视频" if "视频" in source or "video" in source.lower() else "出图"
        prompt = str(
            request.get("original_prompt")
            or request.get("prompt")
            or request.get("mode")
            or ""
        )[:40]
        lines.append(f"{index}. {task_id} [{status_cn}/{kind}] {prompt}")
    lines.append("查看：/生图任务 编号或任务号；取消：/生图取消 …")
    return "\n".join(lines)


def format_task_detail_text(task: Mapping[str, Any]) -> str:
    request = task.get("request_data") if isinstance(task.get("request_data"), dict) else {}
    result = task.get("result") if isinstance(task.get("result"), dict) else {}
    status = str(task.get("status") or "")
    detail_labels = {**STATUS_LABELS, "running": "绘制中"}
    lines = [
        f"任务 {task.get('task_id')}",
        f"状态：{detail_labels.get(status, status)}",
        f"说明：{str(request.get('original_prompt') or request.get('prompt') or '')[:120]}",
    ]
    if task.get("error"):
        lines.append(f"原因：{task.get('error')}")
    if result.get("used_model"):
        lines.append(f"模型：{result.get('used_model')}")
    if result.get("files"):
        lines.append(f"图片：{len(result.get('files') or [])} 张")
    if task.get("status") in {"queued", "running"}:
        lines.append(f"已用时：{task.get('running_seconds', 0)} 秒")
    return "\n".join(lines)
