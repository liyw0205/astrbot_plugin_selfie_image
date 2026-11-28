"""Image preset management."""

from __future__ import annotations

import os
import random
import threading
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from astrbot.api import logger

from ..core.utils import load_json_file_result, save_json_file
from ..core.persistent_store import (
    PersistentStoreConflictError,
    cross_process_file_lock,
    file_fingerprint,
    read_revision_state,
    write_revision_state,
)

def _preset_group_for_name(name: object) -> str:
    try:
        from ..studio.studio import prompt_preset_group

        return str(prompt_preset_group(str(name or "")) or "")
    except Exception:
        return ""


_PRESET_GROUP_ORDER = {"action": 0, "upper": 1, "lower": 2}
_PRESET_GROUP_LABELS = {
    "action": "动作预设",
    "upper": "上身预设",
    "lower": "下身预设",
}


def preset_group_label(name: object) -> str:
    """Return the display label for a special preset group."""
    return _PRESET_GROUP_LABELS.get(_preset_group_for_name(name), "")


def preset_display_sort_key(name: object) -> tuple[int, int, str]:
    """Keep ordinary presets first, then action/upper/lower special groups."""
    value = str(name or "").strip()
    group = _preset_group_for_name(value)
    return (1 if group else 0, _PRESET_GROUP_ORDER.get(group, len(_PRESET_GROUP_ORDER)), value)


def preset_row_display_sort_key(row: object) -> tuple[int, int, str]:
    """Sort a public preset row while honoring its already-resolved group."""
    if isinstance(row, dict):
        name = str(row.get("name") or row.get("title") or "").strip()
        group = str(row.get("preset_group") or row.get("special_group") or "").strip()
        if group:
            return (1, _PRESET_GROUP_ORDER.get(group, len(_PRESET_GROUP_ORDER)), name)
        return (0, -1, name)
    return preset_display_sort_key(row)


# Upgrade only the exact old built-in value. User-customized presets remain untouched.
LEGACY_BUILTIN_PROMPT_REPLACEMENTS = {
    "废图修复": (
        "以输入图片为底图进行纠错修复。先检查实际存在的生成异常，仅针对异常区域及必要的衔接区域进行修改，正常部分尽量保持原样；不存在的问题不主动改动。\n\n"
        "若存在多余肢体、错误缺失、关节错位、连接断裂、肢体粘连、手脚畸形或手指异常，依据原图人物姿势、遮挡关系和透视修复，使人体结构连贯自然。保持原有动作方向、屈伸关系和身体支撑关系，不擅自改换姿势。多人画面保持原有人数，明确各自肢体归属。\n\n"
        "若存在面部模糊、五官错位、重影、眼睛或瞳孔异常，修复可见面部细节，保持人物身份、神态、视线方向和自然皮肤纹理，不过度美颜。若提供清晰身份参考，仅用于校正人物身份，不移植参考图的姿势、服装或场景。\n\n"
        "若存在身体穿模、手与道具脱离、脚与地面接触错误、桌椅支撑不合理，修复局部连接与接触，使动作和承重合理，尽量保留原有姿态及物体位置。\n\n"
        "若存在衣物与皮肤粘连、布料结构断裂、配饰悬浮或重复，修复结构、边缘及褶皱，保持原服装款式、材质、颜色、覆盖范围和配饰设计。\n\n"
        "若存在局部糊块、异常重影、重复纹理、拼接痕迹或不合理的阴影倒影，进行协调修复，保留正常景深、运动模糊、水面折射及自然纹理，避免全图过度锐化或磨皮。\n\n"
        "保持原图构图、机位、视角、透视、人物比例、场景、光线氛围、画幅比例及画面风格。尊重合理裁切、遮挡、透视缩短和闭眼状态，不补出原本位于画外或被遮挡的身体部位，不强求所有手脚和手指可见。缺乏细节依据时采用与原图一致的保守补全，不额外添加人物、物体或装饰。"
    ),
    "遮脸": (
        "自然遮住部分脸部，随机选择一种遮挡方式（二选一）：用一只手轻轻挡住脸颊或嘴部，"
        "或者用手机从脸侧或下方自然遮住半张脸，不要同时出现两种遮挡。"
        "至少保留一只眼睛和部分面部轮廓可见；手、手指、手臂或手机与人物连接自然，"
        "透视、光影和遮挡关系正确，手指数量正常，不要手掌变形、手臂穿过脸部、整张脸完全被盖住、"
        "道具贴脸或僵硬摆拍。保持人物身份、服装、姿势和场景不变，像自然随手拍。"
    ),
}

# Remove only these exact retired defaults from existing preset stores. A
# same-named preset with user-edited content is treated as user data.
LEGACY_BUILTIN_PROMPT_REMOVALS = {
    "咬唇回眸": "成年女性坐在沙发扶手上，身体侧向一边，轻轻咬住下唇后回头看向镜头，一只手搭在膝上，另一只手拨开发丝，眼神带着试探和深情。室内闷热，她大汗淋漓，汗湿的皮肤在紫色氛围灯反衬下泛起清晰反光，柔和侧光映出脸部轮廓。真实人类女孩，竖屏近距离人像，暧昧生活感手机摄影",
    "轻拉衣领": "成年女性站在镜前，指尖轻轻触到颈侧与衣领边缘，身体微微前倾，眼神透过镜面深情地停留在镜头上，发丝垂落肩前。闷热的夜晚卧室里，她大汗淋漓，汗湿的皮肤在柔暗灯光反衬下泛起清晰反光。真实人类女孩，镜前半身自拍，iPhone原生暧昧氛围",
    "俯身靠近": "成年女性坐在书桌前，身体微微俯身靠近手机镜头，一只手撑在桌面，另一只手轻抚耳侧发丝，眼神从睫毛下方深情地望向镜头，台灯照亮脸部和颈部轮廓，真实人类女孩，竖屏近景，带有呼吸感的生活化抓拍",
    "腰侧轻抚": "成年女性坐在床沿，一只手沿着腰侧轻轻抚过，身体向镜头方向侧转，肩膀自然放松，眼神安静而深情地看向镜头。房间闷热，她大汗淋漓，汗湿的皮肤在暖黄色床头灯反衬下泛起清晰反光，灯光落在腰线和发丝上。真实人类女孩，竖屏三分之四身构图，私密感手机摄影",
    "耳边靠近": "成年女性坐在沙发上，成年伴侣只以靠近耳边的嘴角、下颌和一侧肩膀局部入镜，她侧过脸看向镜头，眼神迷离，带着深情与温柔，一只手轻轻抓住画面边缘伸来的衣袖。昏暗客厅闷热，她大汗淋漓，汗湿的皮肤在落地灯与窗外城市灯光反衬下泛起清晰反光。真实人类女孩，竖屏半身构图，iPhone随手拍出的亲密瞬间",
    "牵手拉近": "成年女性站在房间门边，成年伴侣只以从画面外伸入的一只手和前臂出现，轻轻牵住她的手腕，她顺着力道向镜头靠近，另一只手扶住门框，眼神深情地直视镜头。室内闷热，她大汗淋漓，汗湿的皮肤在暖色灯光反衬下泛起清晰反光。真实人类女孩，竖屏中近景，暧昧抓拍质感",
    "侧躺抬眼": "成年女性侧躺在柔软沙发上，一只手支着脸颊，另一只手搭在腰侧，膝盖自然弯曲，脸颊贴近抱枕边缘，眼神从下方深情地抬起看向镜头。室内闷热，她大汗淋漓，汗湿的皮肤在昏暗暖灯反衬下泛起清晰反光，灯光照亮凌乱发丝和柔软布料。真实人类女孩，竖屏近景，慵懒亲密的iPhone摄影",
    "背对回眸": "成年女性站在窗边背对镜头，身体轻轻转向一侧，回眸深情地望向镜头，长发沿着背部自然垂落，手指轻搭在窗帘边缘，清晨柔光勾勒出肩背和衣料轮廓，真实人类女孩，竖屏全身构图，安静自然的手机摄影质感",
    "低头抬眼": "成年女性坐在昏暗房间的椅子上，双手轻轻整理衣袖，先低头看向自己的手指，随后抬眼望向镜头，眼神从克制变得深情而柔软，发丝遮住一侧脸颊。闷热的室内让她大汗淋漓，汗湿的皮肤在暖光反衬下泛起清晰反光，光线勾勒出脸部和颈部轮廓。真实人类女孩，竖屏胸像构图，安静而暧昧的原生摄影质感",
}


@dataclass
class ImagePreset:
    prompt: str
    aspect_ratio: str = ""
    resolution: str = ""
    description: str = ""
    extra_prompt: str = ""


def _default_seed() -> Dict[str, Dict[str, str]]:
    """Return image defaults while preserving a diagnostic for callers.

    ImportError/AttributeError are compatibility cases for older plugin
    layouts. Other exceptions indicate a broken built-in seed and are logged
    with a traceback instead of looking like an empty preset installation.
    """
    try:
        from ..studio.studio import default_image_preset_seed

        seed = default_image_preset_seed()
        if not isinstance(seed, dict):
            raise TypeError("default image preset seed must be a mapping")
        return seed
    except (ImportError, AttributeError):
        return {}
    except Exception:
        logger.exception("[SelfieImage] failed to load built-in image presets")
        raise


class ImagePresetManager:
    _DELETED_BUILTINS_KEY = "__deleted_builtin_presets__"
    PRESET_FILENAME = "image_presets.json"

    @staticmethod
    def _builtin_seed() -> Dict[str, Dict[str, str]]:
        return _default_seed()

    def __init__(self, data_dir: str):
        self.file_path = os.path.join(data_dir, self.PRESET_FILENAME)
        self._lock_path = f"{self.file_path}.lock"
        self._revision_path = f"{self.file_path}.revision.json"
        self._lock = threading.RLock()
        self.presets: Dict[str, ImagePreset] = {}
        self._deleted_builtin_names: set[str] = set()
        self._storage_blocked = False
        self._storage_revision = 1
        self._storage_fingerprint: Optional[str] = None
        self.load_status: Dict[str, object] = {
            "ok": True,
            "source": "builtin",
            "error": "",
        }
        self.load()

    def load(self) -> None:
        with self._lock:
            with cross_process_file_lock(self._lock_path):
                self._load_locked()

    def _load_locked(self) -> None:
        # Atomic replacement keeps each read coherent, but an older writer may
        # not use our sidecar lock.  Refuse to publish a mixed snapshot if the
        # file changes while it is being decoded.
        result = None
        revision = 1
        fingerprint = ""
        changed = False
        for _ in range(2):
            revision, fingerprint, changed = read_revision_state(
                self.file_path,
                self._revision_path,
                current_revision=self._storage_revision,
                current_fingerprint=self._storage_fingerprint,
            )
            result = load_json_file_result(self.file_path)
            if file_fingerprint(self.file_path) == fingerprint:
                break
        if result is None or file_fingerprint(self.file_path) != fingerprint:
            self._storage_blocked = True
            self.load_status = {
                "ok": False,
                "source": self.file_path,
                "status": "changed_during_read",
                "error": "预设文件读取期间发生变化，已保留当前内存数据",
            }
            return
        if not result.ok and result.status != "missing":
            self._storage_blocked = True
            self.load_status = {
                "ok": False,
                "source": self.file_path,
                "status": result.status,
                "error": f"预设文件读取失败：{result.error or result.status}",
            }
            return
        self._storage_blocked = False
        self._storage_revision = revision
        self._storage_fingerprint = fingerprint
        if changed:
            try:
                write_revision_state(self._revision_path, revision, fingerprint)
            except Exception:
                # The JSON remains readable on read-only installations.  The
                # next successful save will reconcile the sidecar again.
                logger.warning("[SelfieImage] 预设 revision 元数据写入失败", exc_info=True)
        raw = result.data
        deleted = raw.get(self._DELETED_BUILTINS_KEY)
        if isinstance(deleted, dict):
            deleted = deleted.get("names")
        self._deleted_builtin_names = {
            str(item or "").strip() for item in (deleted or []) if str(item or "").strip()
        } if isinstance(deleted, (list, tuple, set)) else set()

        presets: Dict[str, ImagePreset] = {}
        for name, value in raw.items():
            key = str(name or "").strip()
            if not key or not isinstance(value, dict):
                continue
            prompt = str(value.get("prompt") or "").strip()
            if not prompt:
                continue
            presets[key] = ImagePreset(
                prompt=prompt,
                aspect_ratio=str(value.get("aspect_ratio") or "").strip(),
                resolution=str(value.get("resolution") or "").strip(),
                description=str(value.get("description") or "").strip(),
                extra_prompt=str(value.get("extra_prompt") or value.get("extraPrompt") or "").strip(),
            )

        # Seed missing built-in defaults and upgrade only exact legacy defaults.
        seed = self._load_builtin_seed()
        dirty = False
        for name, retired_prompt in LEGACY_BUILTIN_PROMPT_REMOVALS.items():
            existing = presets.get(name)
            if existing is not None and existing.prompt == retired_prompt:
                presets.pop(name)
                dirty = True
        for name, value in seed.items():
            key = str(name or "").strip()
            prompt = str((value or {}).get("prompt") or "").strip()
            if not key or not prompt:
                continue
            existing = presets.get(key)
            if key in self._deleted_builtin_names:
                continue
            if existing is None:
                presets[key] = ImagePreset(
                    prompt=prompt,
                    description=str((value or {}).get("description") or key).strip(),
                )
                dirty = True
            elif existing.prompt == LEGACY_BUILTIN_PROMPT_REPLACEMENTS.get(key):
                existing.prompt = prompt
                dirty = True

        self.presets = presets
        if dirty or (not raw and presets):
            self._save_locked()
        if self.load_status.get("ok", True):
            self.load_status = {
                "ok": True,
                "source": "builtin" if result.status == "missing" else self.file_path,
                "error": "",
            }

    def _load_builtin_seed(self) -> Dict[str, Dict[str, str]]:
        try:
            seed = self._builtin_seed()
            if not isinstance(seed, dict):
                raise TypeError("built-in preset seed must be a mapping")
            self.load_status = {"ok": True, "source": "builtin", "error": ""}
            return seed
        except (ImportError, AttributeError) as exc:
            self.load_status = {
                "ok": False,
                "source": "builtin",
                "error": f"兼容性缺少内置预设：{type(exc).__name__}",
            }
            logger.warning("[SelfieImage] built-in %s presets unavailable: %s", self.PRESET_FILENAME, exc)
            return {}
        except Exception as exc:
            self.load_status = {
                "ok": False,
                "source": "builtin",
                "error": f"内置预设加载失败：{type(exc).__name__}: {exc}",
            }
            logger.exception("[SelfieImage] built-in %s presets failed", self.PRESET_FILENAME)
            return {}

    def get_load_status(self) -> Dict[str, object]:
        """Return a safe status object for management/health APIs."""
        return dict(self.load_status)

    def save(self, *, expected_revision: Optional[int] = None) -> None:
        with self._lock:
            with cross_process_file_lock(self._lock_path):
                self._save_locked(expected_revision=expected_revision)

    def _save_locked(self, *, expected_revision: Optional[int] = None) -> None:
        if self._storage_blocked:
            raise RuntimeError("预设文件读取失败，已停止覆盖原文件")
        disk_revision, disk_fingerprint, changed = read_revision_state(
            self.file_path,
            self._revision_path,
            current_revision=self._storage_revision,
            current_fingerprint=self._storage_fingerprint,
        )
        if changed:
            try:
                write_revision_state(self._revision_path, disk_revision, disk_fingerprint)
            except Exception:
                pass
        if expected_revision is not None and int(expected_revision) != self._storage_revision:
            raise PersistentStoreConflictError(expected_revision, self._storage_revision, "预设")
        if self._storage_fingerprint is not None and disk_fingerprint != self._storage_fingerprint:
            raise PersistentStoreConflictError(self._storage_revision, disk_revision, "预设")
        if self._storage_fingerprint is not None and self._storage_revision != disk_revision:
            raise PersistentStoreConflictError(self._storage_revision, disk_revision, "预设")
        payload = {
            name: {
                "prompt": preset.prompt,
                "aspect_ratio": preset.aspect_ratio,
                "resolution": preset.resolution,
                "description": preset.description,
                "extra_prompt": preset.extra_prompt,
            }
            for name, preset in self.presets.items()
        }
        if self._deleted_builtin_names:
            payload[self._DELETED_BUILTINS_KEY] = {"names": sorted(self._deleted_builtin_names)}
        save_json_file(self.file_path, payload)
        new_fingerprint = file_fingerprint(self.file_path)
        new_revision = max(self._storage_revision, disk_revision, 1) + 1
        try:
            write_revision_state(self._revision_path, new_revision, new_fingerprint)
        except Exception:
            logger.warning("[SelfieImage] 预设 revision 元数据写入失败，已保留预设文件", exc_info=True)
        self._storage_revision = new_revision
        self._storage_fingerprint = new_fingerprint
        self.load_status = {"ok": True, "source": self.file_path, "error": ""}

    def get_revision(self) -> int:
        with self._lock:
            with cross_process_file_lock(self._lock_path):
                revision, fingerprint, changed = read_revision_state(
                    self.file_path,
                    self._revision_path,
                    current_revision=self._storage_revision,
                    current_fingerprint=self._storage_fingerprint,
                )
                if changed:
                    try:
                        write_revision_state(self._revision_path, revision, fingerprint)
                    except Exception:
                        pass
                return revision

    def list(self) -> List[Tuple[str, ImagePreset]]:
        with self._lock:
            return list(self.presets.items())

    def is_builtin_deleted(self, name: str) -> bool:
        """Whether a seeded preset was explicitly removed by the user."""
        return str(name or "").strip() in self._deleted_builtin_names

    def list_public(self) -> List[Dict[str, str]]:
        rows: List[Dict[str, str]] = []
        for name, preset in self.list():
            rows.append(
                {
                    "name": name,
                    "title": name,
                    "prompt": preset.prompt,
                    "description": preset.description or "",
                    "aspect_ratio": preset.aspect_ratio or "",
                    "resolution": preset.resolution or "",
                    "extra_prompt": preset.extra_prompt or "",
                    "source": "user",
                    "preset_group": _preset_group_for_name(name),
                    "special_group": _preset_group_for_name(name),
                }
            )
        rows.sort(key=preset_row_display_sort_key)
        return rows

    def add(
        self, name: str, raw_value: str, *, expected_revision: Optional[int] = None
    ) -> Tuple[bool, str]:
        key = str(name or "").strip()
        value = str(raw_value or "").strip()
        if not key:
            return False, "预设名不能为空"
        if not value:
            return False, "预设内容不能为空"

        preset = self._parse_value(value)
        if not preset.prompt:
            return False, "预设内容不能为空"

        with self._lock:
            previous_presets = self.presets.copy()
            previous_deleted = set(self._deleted_builtin_names)
            self.presets[key] = preset
            self._deleted_builtin_names.discard(key)
            try:
                self.save(expected_revision=expected_revision)
            except Exception:
                self.presets = previous_presets
                self._deleted_builtin_names = previous_deleted
                raise
        return True, f"已添加预设 {key}"

    def remove(self, name: str, *, expected_revision: Optional[int] = None) -> Tuple[bool, str]:
        key = str(name or "").strip()
        if not key:
            return False, "预设名不能为空"
        if key not in self.presets:
            return False, f"预设不存在: {name}"

        with self._lock:
            previous_presets = self.presets.copy()
            previous_deleted = set(self._deleted_builtin_names)
            self.presets.pop(key, None)
            if key in self._load_builtin_seed():
                self._deleted_builtin_names.add(key)
            try:
                self.save(expected_revision=expected_revision)
            except Exception:
                self.presets = previous_presets
                self._deleted_builtin_names = previous_deleted
                raise
        return True, f"已删除预设 {name}"

    def list_management(self) -> List[Dict[str, str]]:
        """Return editable presets, including fields hidden by the picker."""
        builtin_names = set(self._load_builtin_seed())
        rows: List[Dict[str, str]] = []
        for name, preset in self.list():
            rows.append(
                {
                    "name": name,
                    "title": name,
                    "prompt": preset.prompt,
                    "description": preset.description or "",
                    "aspect_ratio": preset.aspect_ratio or "",
                    "resolution": preset.resolution or "",
                    "extra_prompt": preset.extra_prompt or "",
                    "source": "builtin" if name in builtin_names else "user",
                    "preset_group": _preset_group_for_name(name),
                    "special_group": _preset_group_for_name(name),
                }
            )
        rows.sort(key=preset_row_display_sort_key)
        return rows

    def save_management(
        self,
        payload: Dict[str, object],
        *,
        expected_revision: Optional[int] = None,
    ) -> Tuple[bool, str]:
        prepared, error = self._prepare_management_payload(payload)
        if prepared is None:
            return False, error
        with self._lock:
            name, original_name, preset = prepared
            previous_presets = dict(self.presets)
            previous_deleted = set(self._deleted_builtin_names)
            if original_name and original_name != name:
                self.presets.pop(original_name, None)
                if original_name in self._load_builtin_seed():
                    self._deleted_builtin_names.add(original_name)
            self.presets[name] = preset
            self._deleted_builtin_names.discard(name)
            try:
                self.save(expected_revision=expected_revision)
            except Exception:
                self.presets = previous_presets
                self._deleted_builtin_names = previous_deleted
                raise
        return True, f"已保存预设 {name}"

    def _prepare_management_payload(
        self,
        payload: Dict[str, object],
    ) -> Tuple[Optional[Tuple[str, str, ImagePreset]], str]:
        if not isinstance(payload, dict):
            return None, "预设参数必须是对象"
        name = str(payload.get("name") or "").strip()
        original_name = str(payload.get("original_name") or payload.get("originalName") or "").strip()
        prompt = str(payload.get("prompt") or "").strip()
        if not name:
            return None, "预设名不能为空"
        if len(name) > 100:
            return None, "预设名不能超过 100 个字符"
        if not prompt:
            return None, "预设内容不能为空"
        if len(prompt) > 20000:
            return None, "预设内容不能超过 20000 个字符"
        return (
            (
                name,
                original_name,
                ImagePreset(
                    prompt=prompt,
                    aspect_ratio=str(payload.get("aspect_ratio") or payload.get("aspectRatio") or "").strip(),
                    resolution=str(payload.get("resolution") or "").strip(),
                    description=str(payload.get("description") or "").strip(),
                    extra_prompt=str(payload.get("extra_prompt") or payload.get("extraPrompt") or "").strip(),
                ),
            ),
            "",
        )

    def import_management(
        self,
        items: object,
        *,
        expected_revision: Optional[int] = None,
    ) -> Tuple[int, str]:
        if isinstance(items, dict):
            normalized = []
            for name, value in items.items():
                if isinstance(value, dict):
                    item = dict(value)
                    item.setdefault("name", name)
                    normalized.append(item)
                elif isinstance(value, str):
                    normalized.append({"name": name, "prompt": value})
                else:
                    normalized.append(value)
            items = normalized
        if not isinstance(items, (list, tuple)):
            raise ValueError("导入内容必须包含 presets 数组或名称对象")
        errors: List[str] = []
        prepared_items: List[Tuple[str, str, ImagePreset]] = []
        for index, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                errors.append(f"第 {index} 项不是对象")
                continue
            prepared, message = self._prepare_management_payload(item)
            if prepared is None:
                errors.append(f"第 {index} 项：{message}")
                continue
            prepared_items.append(prepared)
        if errors:
            raise ValueError("；".join(errors[:5]))
        with self._lock:
            candidate_presets = dict(self.presets)
            candidate_deleted = set(self._deleted_builtin_names)
            for name, original_name, preset in prepared_items:
                if original_name and original_name != name:
                    candidate_presets.pop(original_name, None)
                    if original_name in self._load_builtin_seed():
                        candidate_deleted.add(original_name)
                candidate_presets[name] = preset
                candidate_deleted.discard(name)
            previous_presets = self.presets
            previous_deleted = self._deleted_builtin_names
            self.presets = candidate_presets
            self._deleted_builtin_names = candidate_deleted
            try:
                self.save(expected_revision=expected_revision)
            except Exception:
                self.presets = previous_presets
                self._deleted_builtin_names = previous_deleted
                raise
        imported = len(prepared_items)
        return imported, f"已导入 {imported} 个预设"

    def resolve(self, raw_prompt: str) -> Dict[str, str]:
        text = self._normalize_text(raw_prompt)
        if not text:
            return {"prompt": ""}

        preset_name, preset, rest = self._match_preset(text)
        if not preset:
            return {"prompt": text}

        prompt_parts = [preset.prompt]
        if preset.extra_prompt:
            prompt_parts.append(preset.extra_prompt)
        if rest:
            prompt_parts.append(rest)

        return {
            "prompt": self._join_prompt(prompt_parts),
            "aspect_ratio": preset.aspect_ratio,
            "resolution": preset.resolution,
            "preset_name": preset_name,
            "description": preset.description,
            "extra_prompt": preset.extra_prompt,
        }

    def has_preset(self, name: str) -> bool:
        return str(name or "").strip() in self.presets

    def _parse_value(self, raw_value: str) -> ImagePreset:
        if raw_value.startswith("{"):
            try:
                import json

                obj = json.loads(raw_value)
                if isinstance(obj, dict):
                    return ImagePreset(
                        prompt=str(obj.get("prompt") or "").strip(),
                        aspect_ratio=str(obj.get("aspect_ratio") or "").strip(),
                        resolution=str(obj.get("resolution") or "").strip(),
                        description=str(obj.get("description") or "").strip(),
                        extra_prompt=str(obj.get("extra_prompt") or obj.get("extraPrompt") or "").strip(),
                    )
            except Exception:
                pass
        return ImagePreset(prompt=raw_value)

    def _normalize_text(self, text: str) -> str:
        return str(text or "").strip().replace("\t", " ").replace("\n", " ").replace("\r", " ").replace("  ", " ")

    def _match_preset(self, text: str) -> Tuple[str, Optional[ImagePreset], str]:
        lowered = text.lower()
        # Structure aliases expand to one concrete built-in prompt at run time.
        try:
            from ..studio.studio import dynamic_prompt_preset_groups

            groups = sorted(
                dynamic_prompt_preset_groups(),
                key=lambda item: len(str(item[0] or "")),
                reverse=True,
            )
            for alias, choices in groups:
                alias = str(alias or "").strip()
                alias_lower = alias.lower()
                if not alias or not (lowered == alias_lower or lowered.startswith(alias_lower + " ")):
                    continue
                if choices:
                    selected = random.choice(choices)
                    title = str(selected.get("title") or "").strip()
                    prompt = str(selected.get("prompt") or "").strip()
                    rest = "" if lowered == alias_lower else text[len(alias) :].strip()
                    if prompt:
                        return alias, ImagePreset(
                            prompt=prompt,
                            description=f"随机选中：{title}" if title else alias,
                        ), rest
        except Exception:
            pass

        # Common aliases for built-in titles (user often types near-homophones).
        alias_map = {
            "露腰": "漏腰",
            "漏腰杀": "漏腰",
            "小蛮腰": "漏腰",
        }
        for alias, target in alias_map.items():
            alias_l = alias.lower()
            if lowered == alias_l or lowered.startswith(alias_l + " "):
                if target in self.presets:
                    rest = "" if lowered == alias_l else text[len(alias):].strip()
                    return target, self.presets[target], rest
        items = sorted(self.presets.items(), key=lambda item: len(item[0]), reverse=True)
        for name, preset in items:
            key = self._normalize_text(name)
            key_lower = key.lower()
            if lowered == key_lower:
                return name, preset, ""
            if lowered.startswith(key_lower + " "):
                return name, preset, text[len(key) :].strip()
        return "", None, ""

    def _join_prompt(self, parts: List[str]) -> str:
        return " ".join(part for part in parts if str(part or "").strip()).strip()
