"""Stable image codes and transport-independent prompt lookup."""

from __future__ import annotations

import copy
import hashlib
import os
import re
import secrets
import time
from io import BytesIO
from typing import Any, Mapping

from PIL import Image, ImageOps, ImageStat

from ..core.utils import event_group_id, event_user_id, extract_quote_text, load_json_file, save_json_file


CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_PATTERN = re.compile(r"(?=[A-Z0-9]*[A-Z])(?=[A-Z0-9]*[0-9])[A-Z0-9]{6}")


def new_image_code(used: set[str]) -> str:
    while True:
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))
        if code not in used and any(c.isalpha() for c in code) and any(c.isdigit() for c in code):
            return code


def image_fingerprint(data: bytes) -> dict[str, Any]:
    try:
        with Image.open(BytesIO(data)) as opened:
            image = ImageOps.exif_transpose(opened).convert("RGB")
            gray = image.convert("L")
            horizontal = list(gray.resize((9, 8), Image.Resampling.LANCZOS).tobytes())
            small = list(gray.resize((8, 8), Image.Resampling.LANCZOS).tobytes())
            average = sum(small) / 64
            dhash = sum((horizontal[y * 9 + x] > horizontal[y * 9 + x + 1]) << (y * 8 + x) for y in range(8) for x in range(8))
            ahash = sum((pixel > average) << i for i, pixel in enumerate(small))
            return {
                "version": 1, "width": image.width, "height": image.height,
                "dh": f"{dhash:016x}", "ah": f"{ahash:016x}", "gray": small,
                "rgb": [round(v) for v in ImageStat.Stat(image.resize((1, 1))).mean],
            }
    except Exception:
        return {}


def fingerprint_distance(left: Mapping[str, Any], right: Mapping[str, Any]) -> float | None:
    try:
        if any(len(fp["gray"]) != 64 or len(fp["rgb"]) != 3 for fp in (left, right)):
            return None
        ratio = (left["width"] / left["height"]) / (right["width"] / right["height"])
        if not 0.97 <= ratio <= 1.03:
            return None
        dh = (int(left["dh"], 16) ^ int(right["dh"], 16)).bit_count()
        ah = (int(left["ah"], 16) ^ int(right["ah"], 16)).bit_count()
        gray = sum(abs(a - b) for a, b in zip(left["gray"], right["gray"])) / 64
        color = max(abs(a - b) for a, b in zip(left["rgb"], right["rgb"]))
        if dh > 12 or ah > 12 or gray > 20 or color > 24:
            return None
        return dh + ah + gray / 2 + color / 4
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None


def prompt_selection_snapshot(text: str) -> dict[str, str]:
    return dict(re.findall(r"【(cos(?:_[a-z_]+)?|legs(?:_[a-z_]+)?|pose|scene|cam|frame|outfit|wear|angle|support|shot):([a-zA-Z0-9_-]+)】", str(text or "")))


def record_final_prompt(record: Mapping[str, Any]) -> str:
    request = record.get("request_data") or {}
    if not isinstance(request, Mapping):
        request = {}
    return str(record.get("final_prompt") or request.get("request_prompt_en") or record.get("request_prompt") or record.get("prompt") or record.get("original_prompt") or "").strip()


class PromptLookupMixin:
    def _reverse_prompt_path(self) -> str:
        root = str(getattr(self, "data_dir", "") or "")
        return os.path.join(root, "prompt_reverse_records.json") if root else ""

    def _reverse_prompt_rows(self) -> list[dict[str, Any]]:
        if not hasattr(self, "_prompt_reverse_records"):
            path = self._reverse_prompt_path()
            data = load_json_file(path) if path else {}
            self._prompt_reverse_records = [r for r in data.get("records", []) if isinstance(r, dict)][:100]
        return self._prompt_reverse_records

    def _new_prompt_code_locked(self) -> str:
        rows = [*getattr(self, "_records", []), *self._reverse_prompt_rows()]
        return new_image_code({str(r.get("image_code") or "") for r in rows})

    def _backfill_prompt_codes(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen = {str(r.get("image_code") or "").upper() for r in self._reverse_prompt_rows()}
        used = seen | {str(r.get("image_code") or "").upper() for r in records}
        changed = []
        for record in records:
            code = str(record.get("image_code") or "").upper()
            if not CODE_PATTERN.fullmatch(code) or code in seen:
                code = new_image_code(used)
            if record.get("image_code") != code:
                record["image_code"] = code
                changed.append(record)
            used.add(code)
            seen.add(code)
        if changed:
            database = self._record_database()
            if database is not None:
                for record in changed:
                    database.upsert_record(record)
            elif getattr(self, "records_path", ""):
                save_json_file(self.records_path, {"records": records})
        return records

    def _prompt_record_patch(self, record_id: str, changes: Mapping[str, Any]) -> dict[str, Any] | None:
        with self._records_lock:
            record = next((r for r in self._records if str(r.get("id")) == str(record_id)), None)
            if record is None:
                return None
            record.update(changes)
            database = self._record_database()
            if database is not None:
                database.upsert_record(record)
            elif getattr(self, "records_path", ""):
                save_json_file(self.records_path, {"records": self._records})
            return copy.deepcopy(record)

    def _prompt_record_allowed(self, event: Any, record: Mapping[str, Any]) -> bool:
        if self._is_admin_event(event):
            return True
        user = event_user_id(event)
        if not user or user != str(record.get("user_id") or ""):
            return False
        scope = str(record.get("lookup_scope") or "")
        if scope:
            return scope == self._context_session_key(event)
        return str(record.get("group_id") or "") == event_group_id(event)

    def _prompt_records_for_event(self, event: Any) -> list[dict[str, Any]]:
        with self._records_lock:
            return [copy.deepcopy(r) for r in self._records if self._prompt_record_allowed(event, r)]

    def _prompt_record_by_code(self, event: Any, code: str) -> dict[str, Any] | None:
        return next((r for r in self._prompt_records_for_event(event) if r.get("image_code") == code.upper()), None)

    def _prompt_records_from_quote_text(self, event: Any, text: str) -> list[dict[str, Any]]:
        codes = set(re.findall(r"生图编号[：:]\s*([A-Z0-9]{6})(?![A-Z0-9])", text, re.I))
        return [row for code in codes if (row := self._prompt_record_by_code(event, code))]

    def _recent_prompt_record(self, event: Any) -> dict[str, Any] | None:
        user = event_user_id(event)
        scope = self._context_session_key(event)
        return next((r for r in self._prompt_records_for_event(event)
                     if r.get("generated_image_paths") and str(r.get("user_id") or "") == user
                     and (r.get("lookup_scope") == scope if r.get("lookup_scope")
                          else str(r.get("group_id") or "") == event_group_id(event))), None)

    def _prompt_record_for_file(self, file_path: str) -> dict[str, Any] | None:
        relative = self._cache_relative_path(file_path)
        with self._records_lock:
            return next((copy.deepcopy(r) for r in self._records if relative in (r.get("generated_image_paths") or [])), None)

    def _redelivery_record_for_file(self, event: Any, file_path: str) -> dict[str, Any] | None:
        relative = self._cache_relative_path(file_path)
        scope = self._context_session_key(event)
        with self._records_lock:
            matches = [r for r in self._records if relative in (r.get("generated_image_paths") or [])]
            scoped = [r for r in matches if r.get("lookup_scope") == scope or any(
                item.get("scope") == scope for item in r.get("delivery_messages", [])
                if isinstance(item, Mapping)
            )]
            # Legacy queues contain paths, not record IDs. Never guess between
            # records sharing a file or attribute a retry to another session.
            if scoped:
                matches = scoped
            else:
                matches = [r for r in matches if not r.get("lookup_scope") and not r.get("delivery_messages")]
            return copy.deepcopy(matches[0]) if len(matches) == 1 else None

    def _remember_prompt_delivery(self, event: Any, record: Mapping[str, Any], receipt: Any) -> None:
        def message_id(value: Any) -> str:
            if isinstance(value, Mapping):
                found = value.get("message_id") or value.get("msg_id")
                return str(found) if found is not None else message_id(value.get("data"))
            if isinstance(value, bool):
                return ""
            if isinstance(value, (str, int)):
                return str(value)
            return str(getattr(value, "message_id", "") or "")

        mid = message_id(receipt)
        if not mid:
            return
        with self._records_lock:
            current = next((r for r in self._records if r.get("id") == record.get("id")), None)
            if current is None:
                return
            messages = list(current.get("delivery_messages") or [])
            item = {"scope": self._context_session_key(event), "message_id": mid}
            if item not in messages:
                self._prompt_record_patch(str(current["id"]), {"delivery_messages": [*messages, item][-32:]})

    def _lookup_prompt_image(self, event: Any, refs: list[Any], reply_ids: list[str]) -> tuple[dict[str, Any] | None, str, list[dict[str, Any]]]:
        records = [r for r in self._prompt_records_for_event(event) if r.get("generated_image_paths")]
        scope = self._context_session_key(event)
        messages = [r for r in records if any(m.get("scope") == scope and str(m.get("message_id")) in reply_ids for m in r.get("delivery_messages", []))]
        if len(messages) == 1:
            self._remember_prompt_aliases(messages[0], refs)
            return messages[0], "引用消息", []
        if len(messages) > 1:
            return None, "引用消息包含多张图片，请确认编号", messages[:3]
        quoted = self._prompt_records_from_quote_text(event, extract_quote_text(event))
        if len(quoted) == 1:
            self._remember_prompt_aliases(quoted[0], refs)
            return quoted[0], "引用消息编号", []
        if len(quoted) > 1:
            return None, "引用消息包含多个编号，请确认", quoted[:3]
        if not refs:
            return None, "", []
        digests = {hashlib.md5(ref.data).hexdigest() for ref in refs}
        exact = [r for r in records if digests.intersection([r.get("md5"), *(r.get("image_alias_md5s") or [])])]
        if len(exact) == 1:
            return exact[0], "图片校验", []
        if len(exact) > 1:
            return None, "多个记录使用同一图片", exact[:3]
        wanted = [image_fingerprint(ref.data) for ref in refs]
        candidates = []
        for record in records:
            fingerprint = record.get("image_fingerprint") or {}
            if not fingerprint:
                loaded = self._load_cache_image_bytes(record["generated_image_paths"][0])
                if loaded:
                    fingerprint = image_fingerprint(loaded[0])
                    variants = self._image_md5_variants(loaded[0])
                    record = self._prompt_record_patch(str(record["id"]), {
                        "image_fingerprint": fingerprint,
                        "image_alias_md5s": list(dict.fromkeys([*(record.get("image_alias_md5s") or []), *variants]))[-24:],
                    }) or record
                    if digests.intersection(variants):
                        candidates.append((0.0, record, True))
                        continue
            distances = [d for fp in wanted if fp for d in [fingerprint_distance(fp, fingerprint)] if d is not None]
            if distances:
                candidates.append((min(distances), record, False))
        candidates.sort(key=lambda item: item[0])
        if candidates:
            distance, record, exact_variant = candidates[0]
            gap = candidates[1][0] - distance if len(candidates) > 1 else 100
            gray = (record.get("image_fingerprint") or {}).get("gray") or [0]
            textured = max(gray) - min(gray) >= 30
            if (exact_variant and not any(c[2] for c in candidates[1:])) or (textured and distance <= 8 and gap >= 6):
                return record, "图片校验" if exact_variant else "图片相似匹配", []
            return None, "图片相似，请确认编号", [item[1] for item in candidates[:3]]
        return None, "", []

    def _remember_prompt_aliases(self, record: Mapping[str, Any], refs: list[Any]) -> None:
        with self._records_lock:
            current = next((r for r in self._records if r.get("id") == record.get("id")), None)
            if current is not None:
                fingerprint = current.get("image_fingerprint") or {}
                # A quote can also contain an unrelated attachment. Only learn
                # aliases whose visual content agrees with the selected record.
                digests = [hashlib.md5(ref.data).hexdigest() for ref in refs
                           if fingerprint_distance(image_fingerprint(ref.data), fingerprint) is not None]
                if not digests:
                    return
                values = [*(current.get("image_alias_md5s") or []), *digests]
                self._prompt_record_patch(str(current["id"]), {"image_alias_md5s": list(dict.fromkeys(values))[-24:]})

    def _remember_prompt_candidates(self, event: Any, refs: list[Any], candidates: list[dict[str, Any]]) -> None:
        if not refs:
            return
        with self._records_lock:
            now = time.monotonic()
            pending = getattr(self, "_prompt_confirmations", {})
            pending = {k: v for k, v in pending.items() if now - v["created"] < 600}
            key = (self._context_session_key(event), event_user_id(event))
            pending.pop(key, None)
            fingerprints = [(hashlib.md5(ref.data).hexdigest(), image_fingerprint(ref.data)) for ref in refs]
            aliases = {
                str(row["id"]): [digest for digest, fp in fingerprints
                                 if fingerprint_distance(fp, row.get("image_fingerprint") or {}) is not None]
                for row in candidates
            }
            pending[key] = {"created": now, "aliases": aliases}
            self._prompt_confirmations = dict(list(pending.items())[-100:])

    def _confirm_prompt_candidate(self, event: Any, record: Mapping[str, Any]) -> None:
        with self._records_lock:
            key = (self._context_session_key(event), event_user_id(event))
            pending = getattr(self, "_prompt_confirmations", {}).pop(key, None)
            if not pending or time.monotonic() - pending["created"] >= 600:
                return
            aliases = pending["aliases"].get(str(record["id"])) or []
            if aliases:
                current = next((r for r in self._records if r.get("id") == record["id"]), None)
                if current:
                    values = [*(current.get("image_alias_md5s") or []), *aliases]
                    self._prompt_record_patch(str(record["id"]), {"image_alias_md5s": list(dict.fromkeys(values))[-24:]})

    def _save_reverse_prompt(self, event: Any, image: bytes, prompt: str) -> dict[str, Any]:
        with self._records_lock:
            row = {
                "image_code": self._new_prompt_code_locked(), "prompt": prompt,
                "md5": hashlib.md5(image).hexdigest(), "kind": "reverse_prompt",
                "user_id": event_user_id(event), "group_id": event_group_id(event),
                "lookup_scope": self._context_session_key(event),
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            self._prompt_reverse_records = [row, *self._reverse_prompt_rows()][:100]
            path = self._reverse_prompt_path()
            if path:
                save_json_file(path, {"records": self._prompt_reverse_records})
            return copy.deepcopy(row)

    def _reverse_prompt_by_code(self, event: Any, code: str) -> dict[str, Any] | None:
        with self._records_lock:
            return next((copy.deepcopy(r) for r in self._reverse_prompt_rows() if r.get("image_code") == code.upper() and self._prompt_record_allowed(event, r)), None)
