"""Pure filtering and text formatting for generation task views."""

from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping
from typing import Any, Dict, List


STATUS_LABELS = {
    "queued": "排队",
    "running": "进行中",
    "succeeded": "完成",
    "partial_success": "部分完成",
    "failed": "失败",
    "cancelled": "已取消",
    "expired": "已过期",
    "delivery_failed": "生成成功但发送失败",
}

MEDIA_LABELS = {
    "image": "图片",
    "video": "视频",
}

SOURCE_LABELS = {
    "web-test": "Web 试画",
    "web-video-test": "Web 试视频",
    "studio-run": "创作画布",
    "record-retry": "记录重试",
    "record-video-retry": "记录重试",
    "llm-generate-image": "LLM 生图",
    "llm-generate-selfie": "LLM 自拍",
}


def _task_media_type(task: Mapping[str, Any]) -> str:
    request = task.get("request_data") if isinstance(task.get("request_data"), dict) else {}
    value = str(task.get("media_type") or request.get("media_type") or "").strip().lower()
    if value in {"image", "video"}:
        return value
    kind = str(request.get("kind") or "").strip().lower()
    source = str(task.get("source") or "").strip().lower()
    return "video" if kind == "video" or "视频" in kind or "video" in source else "image"


def task_source_label(task: Mapping[str, Any]) -> str:
    """Return a stable Chinese label without exposing internal source names."""
    source = str(task.get("source") or "").strip().lower()
    if source in SOURCE_LABELS:
        return SOURCE_LABELS[source]
    if source.startswith("llm-"):
        return "LLM 调用"
    if source.startswith("web-"):
        return "Web 试画"
    if "retry" in source or "重试" in source:
        return "记录重试"
    if source.startswith("command-"):
        return "指令"
    return "指令" if source else "未知来源"


def filter_image_tasks(
    tasks: Iterable[Mapping[str, Any]],
    session_key: str = "",
    *,
    include_finished: bool = False,
    limit: int = 10,
    media_type: str = "",
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
        wanted_media = str(media_type or "").strip().lower()
        if wanted_media:
            if _task_media_type(task) != wanted_media:
                continue
        source = str(task.get("source") or "")
        if session_key and source.startswith("web") and owner != session_key:
            continue
        rows.append(copy.deepcopy(dict(task)))
        if len(rows) >= max(1, limit):
            break
    return rows


def format_task_list_text(
    tasks: Iterable[Mapping[str, Any]],
    *,
    include_finished: bool = False,
    media_type: str = "",
) -> str:
    items = list(tasks)
    if not items:
        return "最近没有任务记录。" if include_finished else "现在没有进行中的出图/视频任务。"
    wanted_media = str(media_type or "").strip().lower()
    video_only = wanted_media == "video" or all(_task_media_type(item) == "video" for item in items)
    lines = [
        ("最近的视频任务：" if video_only else "最近的任务：")
        if include_finished
        else ("进行中的视频任务：" if video_only else "进行中的任务：")
    ]
    for index, task in enumerate(items, 1):
        task_id = str(task.get("task_id") or "")
        status = str(task.get("status") or "")
        status_cn = STATUS_LABELS.get(status, status)
        request = task.get("request_data") if isinstance(task.get("request_data"), dict) else {}
        media_type = _task_media_type(task)
        kind = MEDIA_LABELS.get(media_type, media_type)
        prompt = str(
            request.get("original_prompt")
            or request.get("prompt")
            or request.get("mode")
            or ""
        )[:40]
        progress = _progress_text(task)
        suffix = f" {progress}" if progress else ""
        queue = _queue_text(task)
        warning = str(task.get("timeout_warning") or "").strip()
        suffix += f" {queue}" if queue else ""
        stage = str(task.get("generation_stage_label") or "").strip()
        suffix += f" [{stage}]" if stage else ""
        suffix += f"（{warning}）" if warning else ""
        lines.append(f"{index}. {task_id} [{status_cn}/{kind}] {prompt}{suffix}")
    if include_finished:
        lines.append(
            "查看详情：/视频任务 历史 编号或任务号"
            if video_only
            else "查看详情：/生图任务 历史 编号或任务号"
        )
    else:
        lines.append(
            "查看：/视频任务 编号或任务号；取消：/视频取消 …"
            if video_only
            else "查看：/生图任务 编号或任务号；取消：/生图取消 …"
        )
    return "\n".join(lines)


def format_task_detail_text(task: Mapping[str, Any]) -> str:
    request = task.get("request_data") if isinstance(task.get("request_data"), dict) else {}
    result = task.get("result") if isinstance(task.get("result"), dict) else {}
    media_type = _task_media_type(task)
    status = str(task.get("status") or "")
    detail_labels = {**STATUS_LABELS, "running": "绘制中"}
    original_prompt = str(
        request.get("original_prompt")
        or request.get("prompt")
        or result.get("original_prompt")
        or result.get("prompt")
        or ""
    ).strip()
    final_prompt = str(
        result.get("final_prompt")
        or result.get("request_prompt")
        or request.get("request_prompt_en")
        or request.get("request_prompt")
        or ""
    ).strip()
    lines = [
        f"任务 {task.get('task_id')}",
        f"状态：{detail_labels.get(status, status)}",
        f"来源：{task_source_label(task)}",
        f"类型：{MEDIA_LABELS.get(media_type, media_type)}",
        f"说明：{original_prompt[:120]}",
    ]
    if final_prompt:
        lines.append(f"最终提示词：{final_prompt[:240]}")
    progress = _progress_text(task)
    if progress:
        lines.append(f"进度：{progress}")
    if task.get("generation_stage_label"):
        lines.append(f"生成阶段：{task.get('generation_stage_label')}")
    queue = _queue_text(task)
    if queue:
        lines.append(f"队列：{queue}")
    if task.get("timeout_warning"):
        lines.append(f"提示：{task.get('timeout_warning')}")
    if task.get("error"):
        lines.append(f"原因：{task.get('error')}")
    linked_records = task.get("record_ids")
    if not isinstance(linked_records, list):
        linked_records = []
    linked_records = [str(item).strip() for item in linked_records if str(item).strip()]
    if linked_records:
        lines.append(f"关联记录：{', '.join(linked_records[:8])}" + (" …" if len(linked_records) > 8 else ""))
    used_model = result.get("used_model") or request.get("model")
    if used_model:
        lines.append(f"模型：{used_model}")
    result_files = (
        result.get("files")
        or result.get("image_paths")
        or result.get("generated_image_paths")
        or result.get("generated_video_paths")
    )
    if result_files:
        label = "视频" if media_type == "video" else "图片"
        lines.append(f"{label}：{len(result_files)} 个")
    if task.get("status") in {"queued", "running"}:
        lines.append(f"已用时：{task.get('running_seconds', 0)} 秒")
    attempts = result.get("attempts") if isinstance(result.get("attempts"), list) else []
    if attempts:
        attempt_labels = []
        for index, item in enumerate(attempts[-6:], max(1, len(attempts) - 5)):
            if not isinstance(item, Mapping):
                continue
            label = str(item.get("label") or item.get("model") or item.get("channel") or "渠道").strip()
            outcome = "成功" if item.get("success") else "失败"
            elapsed = item.get("elapsed_seconds")
            suffix = f" {elapsed}s" if elapsed not in (None, "") else ""
            attempt_labels.append(f"#{index} {label} {outcome}{suffix}")
        if attempt_labels:
            lines.append("渠道尝试：" + "；".join(attempt_labels))
    failed_attempts = [item for item in attempts if isinstance(item, Mapping) and not item.get("success")]
    if failed_attempts:
        last = failed_attempts[-1]
        label = str(last.get("label") or last.get("model") or last.get("channel") or "上一次渠道")
        reason = str(last.get("error_user_message") or last.get("error") or "生成失败")
        lines.append(f"最近尝试：{label}，{reason[:120]}")
    retry_record_id = str(request.get("retry_record_id") or task.get("retry_record_id") or "").strip()
    if retry_record_id:
        lines.append("可在管理页任务详情中从关联记录重新执行。")
    return "\n".join(lines)


def _progress_text(task: Mapping[str, Any]) -> str:
    try:
        requested = max(0, int(task.get("requested_count") or 0))
        completed = max(0, int(task.get("completed_count") or 0))
    except (TypeError, ValueError):
        return ""
    if requested <= 0:
        return ""
    percent = task.get("progress_percent")
    try:
        percent_value = max(0, min(100, int(percent)))
    except (TypeError, ValueError):
        percent_value = int(round(completed * 100 / requested))
    return f"{completed}/{requested}（{percent_value}%）"


def _queue_text(task: Mapping[str, Any]) -> str:
    try:
        wait = max(0.0, float(task.get("queue_wait_seconds") or 0))
    except (TypeError, ValueError):
        wait = 0.0
    if task.get("queue_waiting"):
        try:
            position = max(0, int(task.get("queue_position") or 0))
        except (TypeError, ValueError):
            position = 0
        return f"排队 {wait:.1f}s" + (f" 第{position}位" if position else "")
    if wait > 0:
        return f"等待 {wait:.1f}s"
    return ""
