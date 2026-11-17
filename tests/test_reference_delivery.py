"""Generated-image delivery outcomes, persistence and cancellation regressions."""

from __future__ import annotations

import asyncio
import threading
import unittest
from pathlib import Path
from unittest.mock import patch


class ReferenceMediaDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_delivery_results_are_persisted_in_one_bounded_batch(self) -> None:
        from astrbot_plugin_selfie_image.features.reference_media import ReferenceMediaMixin

        plugin = object.__new__(ReferenceMediaMixin)
        plugin._send_failures = {}
        plugin._send_failures_lock = threading.RLock()
        plugin._session_key = lambda _event: "group:test"
        plugin._cache_relative_path = lambda path: Path(path).name
        plugin._create_image_component = lambda path: path
        plugin._record_bot_image_context = lambda *_args: None
        record = {"id": "record-batch", "task_id": "task-batch"}
        updates = []
        prompt_deliveries = []

        async def wait_for_record(_path):
            return None

        plugin._wait_for_image_record = wait_for_record
        plugin._prompt_record_for_file = lambda _path: record
        plugin._remember_prompt_deliveries = lambda _event, items: prompt_deliveries.extend(items)
        plugin._update_generation_records_delivery = lambda *args, **kwargs: updates.append((args, kwargs))

        class Event:
            def chain_result(self, components):
                return components

            async def send(self, message):
                message = message[0]
                if message == "/tmp/image-2.png":
                    raise RuntimeError("permission denied")
                return {"message_id": message.rsplit("-", 1)[-1]}

        async def no_sleep(_delay):
            return None

        with patch("asyncio.sleep", new=no_sleep):
            outcome = await plugin._send_generated_images(
                Event(), [f"/tmp/image-{index}.png" for index in range(4)],
            )

        self.assertEqual((int(outcome), outcome.failed_count, outcome.unknown_count), (3, 1, 0))
        self.assertEqual(len(updates), 1)
        self.assertEqual(
            updates[0][1]["image_outcomes"],
            {
                "/tmp/image-0.png": (True, ""),
                "/tmp/image-1.png": (True, ""),
                "/tmp/image-2.png": (False, "permission denied"),
                "/tmp/image-3.png": (True, ""),
            },
        )
        self.assertEqual(len(prompt_deliveries), 3)

    async def test_pending_delivery_results_flush_when_send_loop_is_cancelled(self) -> None:
        from astrbot_plugin_selfie_image.features.reference_media import ReferenceMediaMixin

        plugin = object.__new__(ReferenceMediaMixin)
        plugin._send_failures = {}
        plugin._send_failures_lock = threading.RLock()
        plugin._session_key = lambda _event: "group:test"
        plugin._cache_relative_path = lambda path: Path(path).name
        plugin._create_image_component = lambda path: path
        plugin._record_bot_image_context = lambda *_args: None
        record = {"id": "record-cancel", "task_id": "task-cancel"}
        updates = []

        async def wait_for_record(_path):
            return None

        plugin._wait_for_image_record = wait_for_record
        plugin._prompt_record_for_file = lambda _path: record
        plugin._remember_prompt_deliveries = lambda *_args: None
        plugin._update_generation_records_delivery = lambda *args, **kwargs: updates.append(kwargs)

        class Event:
            def chain_result(self, components):
                return components

            async def send(self, _message):
                return {"message_id": "sent"}

        async def cancel_between_messages(_delay):
            raise asyncio.CancelledError()

        with patch("asyncio.sleep", new=cancel_between_messages):
            with self.assertRaises(asyncio.CancelledError):
                await plugin._send_generated_images(
                    Event(), ["/tmp/image-0.png", "/tmp/image-1.png"],
                )

        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]["image_outcomes"], {"/tmp/image-0.png": (True, "")})

    async def test_transport_timeout_is_unknown_and_not_queued_as_failure(self) -> None:
        from astrbot_plugin_selfie_image.features.reference_media import ReferenceMediaMixin

        class Event:
            def chain_result(self, components):
                return components

            async def send(self, _message):
                raise RuntimeError(
                    "ActionFailed retcode=1200 Timeout: NodeIKernelMsgService/sendMsg"
                )

        plugin = object.__new__(ReferenceMediaMixin)
        plugin._send_failures = {}
        plugin._send_failures_lock = threading.RLock()
        plugin._session_key = lambda _event: "group:test"
        plugin._cache_relative_path = lambda path: "generated/" + Path(path).name
        plugin._create_image_component = lambda path: path
        plugin._record_bot_image_context = lambda *_args: None

        outcome = await plugin._send_generated_images(Event(), ["/tmp/generated.png"])

        self.assertEqual(int(outcome), 0)
        self.assertEqual(outcome.failed_count, 0)
        self.assertEqual(outcome.unknown_count, 1)
        self.assertEqual(plugin._send_failures, {})

    async def test_explicit_send_error_is_failed_and_retryable(self) -> None:
        from astrbot_plugin_selfie_image.features.reference_media import ReferenceMediaMixin

        class Event:
            def chain_result(self, components):
                return components

            async def send(self, _message):
                raise RuntimeError("permission denied by adapter")

        plugin = object.__new__(ReferenceMediaMixin)
        plugin._send_failures = {}
        plugin._send_failures_lock = threading.RLock()
        plugin._session_key = lambda _event: "group:test"
        plugin._cache_relative_path = lambda path: "generated/" + Path(path).name
        plugin._create_image_component = lambda path: path
        plugin._record_bot_image_context = lambda *_args: None

        outcome = await plugin._send_generated_images(Event(), ["/tmp/generated.png"])

        self.assertEqual(int(outcome), 0)
        self.assertEqual(outcome.failed_count, 1)
        self.assertEqual(outcome.unknown_count, 0)
        self.assertEqual(plugin._send_failures, {"group:test": ["generated/generated.png"]})

    async def test_empty_timeout_exception_is_also_unknown(self) -> None:
        from astrbot_plugin_selfie_image.features.reference_media import ReferenceMediaMixin

        class Event:
            def chain_result(self, components):
                return components

            async def send(self, _message):
                raise TimeoutError()

        plugin = object.__new__(ReferenceMediaMixin)
        plugin._send_failures = {}
        plugin._send_failures_lock = threading.RLock()
        plugin._session_key = lambda _event: "group:test"
        plugin._cache_relative_path = lambda path: "generated/" + Path(path).name
        plugin._create_image_component = lambda path: path
        plugin._record_bot_image_context = lambda *_args: None

        outcome = await plugin._send_generated_images(Event(), ["/tmp/generated.png"])

        self.assertEqual(outcome.unknown_count, 1)
        self.assertEqual(outcome.failed_count, 0)
        self.assertEqual(plugin._send_failures, {})
