"""Explicit demo fixtures; never reads plugin records or configuration."""

from copy import deepcopy
import time


SCENARIOS = {"mixed": "混合状态", "healthy": "全部正常", "empty": "暂无记录", "stress": "长名称 / 多页"}


def make_demo():
    now = time.time()
    specifications = [
        ("主渠道", "gpt-image-1", "正常", 247, 247, 4.82, {}, "■" * 24, 32),
        ("高速渠道", "gemini-2.5-flash-image", "降级", 186, 179, 3.16, {"timeout": 5, "server": 2}, "■■■■■▲■■■■■■▲■■■■■■■■■▲■", 148),
        ("备用渠道", "flux.1-dev", "异常", 63, 41, 12.74, {"timeout": 16, "network": 6}, "■■■■▲■■×▲■■▲×■■■▲×■▲■×××", 327),
        ("创作渠道", "dall-e-3", "验证失败", 12, 0, 1.08, {"auth": 12}, "-------------×××××××××××", 582),
        ("共享渠道", "imagen-4", "限流", 91, 76, 6.39, {"rate_limit": 15}, "■■■■■■■▲■■▲■■■■▲■■■▲■■××", 816),
        ("新建渠道", "novelai-diffusion-4", "未使用", 0, 0, 0, {}, "-" * 24, 0),
        ("离线渠道", "stable-diffusion-xl", "断流", 18, 0, 18.5, {"network": 18}, "----------××××××××××××××", 1280),
        ("专用渠道", "hunyuan-image", "正常", 57, 57, 6.27, {}, "■■■■-■■■■■■■■■■-■■■■■■■■", 95),
    ]
    models = []
    for channel, model, state, attempts, successes, elapsed, errors, history, age in specifications:
        failed = state in {"异常", "验证失败", "限流", "断流"}
        models.append({
            "channel": channel, "model": model, "label": f"{channel}/{model}",
            "status": state, "attempts": attempts, "successes": successes,
            "failures": attempts - successes, "success_rate": successes / attempts if attempts else 0,
            "average_elapsed_seconds": elapsed, "error_categories": errors,
            "last_timestamp": now - age if age else 0,
            "last_success": None if not attempts else not failed,
            "last_error_category": next(iter(errors), "") if failed else "",
            "consecutive_failures": min(attempts - successes, 3) if failed else 0,
            "history": history, "configured": True,
        })

    def payload(rows):
        alerts = [f"{row['label']}：{row['status']}" for row in rows if row["status"] not in {"正常", "未使用"}]
        return {
            "window_seconds": 604800, "updated_at": now,
            "overall_status": "异常" if alerts else "正常" if rows else "无模型",
            "model_count": len(rows), "channel_count": len({row["channel"] for row in rows}),
            "available_count": sum(row["status"] in {"正常", "降级"} and row["successes"] > 0 for row in rows),
            "alert_count": len(alerts), "unused_count": sum(row["status"] == "未使用" for row in rows),
            "models": rows, "alerts": alerts,
        }

    healthy = deepcopy(models[:4])
    for row in healthy:
        row.update(status="正常", attempts=max(row["attempts"], 37), error_categories={},
                   consecutive_failures=0, last_success=True, last_error_category="", history="■" * 24)
        row.update(successes=row["attempts"], failures=0, success_rate=1, average_elapsed_seconds=4.82)
    stress = deepcopy(models)
    for index, row in enumerate(stress):
        row["channel"] = f"多区域图像生成与备用调度渠道{index + 1}"
        row["model"] = "gemini-2.5-flash-image-preview-custom-regional-deployment-2026"
        row["label"] = f"{row['channel']}/{row['model']}"
    return {"mixed": payload(models), "healthy": payload(healthy), "empty": payload([]), "stress": payload(stress)}
