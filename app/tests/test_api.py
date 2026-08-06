from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.src.config import Settings
from app.src.main import create_app

HEX_UUID = re.compile(r"^[0-9a-f]{32}$")
PREFIX = "/api/v1/douyin"


class DouyinApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        data_dir = Path(self.temporary.name)
        settings = Settings(
            data_dir=data_dir,
            database_url=f"sqlite+aiosqlite:///{data_dir / 'test.db'}",
            worker_enabled=False,
        )
        self.settings = settings
        self.client_context = TestClient(create_app(settings))
        self.client = self.client_context.__enter__()

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        self.temporary.cleanup()

    def _upload(self, account: str, filename: str, content: bytes, mime_type: str) -> dict:
        response = self.client.post(
            f"{PREFIX}/materials",
            data={"account": account},
            files=[("files", (filename, content, mime_type))],
        )
        self.assertEqual(response.status_code, 200, response.text)
        item = response.json()["data"]["items"][0]
        self.assertTrue(item["success"])
        return item["material"]

    def _mark_logged_in(self, account: str) -> None:
        path = self.settings.cookies_dir / f"douyin_{account}.json"
        path.write_text('{"cookies": [], "origins": []}', encoding="utf-8")

    def test_health_and_request_ids_are_hex_uuids(self) -> None:
        response = self.client.get(f"{PREFIX}/health/ready")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertRegex(payload["request_id"], HEX_UUID)
        self.assertEqual(response.headers["x-request-id"], payload["request_id"])

    def test_openapi_uses_only_douyin_tag_and_chinese_documentation(self) -> None:
        response = self.client.get("/openapi.json")
        self.assertEqual(response.status_code, 200, response.text)
        specification = response.json()
        self.assertEqual(specification["info"]["title"], "抖音自动发布 API")
        self.assertEqual([tag["name"] for tag in specification["tags"]], ["douyin"])

        operations = [
            operation
            for path_item in specification["paths"].values()
            for operation in path_item.values()
            if isinstance(operation, dict) and "tags" in operation
        ]
        self.assertEqual(len(operations), 11)
        self.assertTrue(all(operation["tags"] == ["douyin"] for operation in operations))
        self.assertTrue(all(operation.get("summary") for operation in operations))
        self.assertTrue(all(operation.get("description") for operation in operations))
        self.assertIn("ApiSuccessEnvelope", specification["components"]["schemas"])
        self.assertIn("ApiErrorEnvelope", specification["components"]["schemas"])
        self.assertNotIn("HTTPValidationError", specification["components"]["schemas"])

        upload_operation = specification["paths"][f"{PREFIX}/materials"]["post"]
        multipart = upload_operation["requestBody"]["content"]["multipart/form-data"]
        upload_schema = multipart["schema"]
        self.assertEqual(upload_schema["properties"]["files"]["items"]["format"], "binary")
        referenced_name = upload_schema["$ref"].rsplit("/", 1)[-1]
        referenced_files = specification["components"]["schemas"][referenced_name]["properties"]["files"]
        self.assertEqual(referenced_files["items"]["format"], "binary")

    def test_login_callback_mode_returns_queued_hex_task_id(self) -> None:
        cookie = json.dumps(
            [{"name": "sessionid", "value": "demo", "domain": ".douyin.com", "path": "/"}]
        )
        response = self.client.post(
            f"{PREFIX}/accounts/login",
            json={
                "account": "alice",
                "cookie": cookie,
                "callback_url": "https://callback.example.com/douyin",
            },
        )
        self.assertEqual(response.status_code, 202, response.text)
        task = response.json()["data"]
        self.assertRegex(task["task_id"], HEX_UUID)
        self.assertEqual(task["status"], "queued")

        query = self.client.get(f"{PREFIX}/tasks/{task['task_id']}", params={"account": "alice"})
        self.assertEqual(query.status_code, 200, query.text)
        self.assertEqual(query.json()["data"]["id"], task["task_id"])

        cancel = self.client.post(
            f"{PREFIX}/tasks/{task['task_id']}/cancel",
            json={"account": "alice"},
        )
        self.assertEqual(cancel.status_code, 200, cancel.text)
        cancelled = cancel.json()["data"]
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["callbacks"][0]["event"], "cancelled")
        self.assertEqual(cancelled["callbacks"][0]["status"], "pending")
        self.assertRegex(cancelled["callbacks"][0]["event_id"], HEX_UUID)
        self.assertFalse((self.settings.temporary_dir / f"{task['task_id']}.cookie.json").exists())

    def test_login_accepts_raw_browser_cookie_header(self) -> None:
        response = self.client.post(
            f"{PREFIX}/accounts/login",
            json={
                "account": "raw_cookie_user",
                "cookie": "sessionid=example-value; sid_tt=value-with-equals==",
                "callback_url": "https://callback.example.com/douyin",
            },
        )
        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["data"]["task_id"]
        temporary_cookie = self.settings.temporary_dir / f"{task_id}.cookie.json"
        storage_state = json.loads(temporary_cookie.read_text(encoding="utf-8"))
        cookies = {item["name"]: item for item in storage_state["cookies"]}
        self.assertEqual(cookies["sessionid"]["value"], "example-value")
        self.assertEqual(cookies["sid_tt"]["value"], "value-with-equals==")
        self.assertEqual(cookies["sessionid"]["domain"], ".douyin.com")
        self.assertTrue(cookies["sessionid"]["secure"])

        cancel = self.client.post(
            f"{PREFIX}/tasks/{task_id}/cancel",
            json={"account": "raw_cookie_user"},
        )
        self.assertEqual(cancel.status_code, 200, cancel.text)
        self.assertFalse(temporary_cookie.exists())

    def test_login_rejects_malformed_raw_cookie_header(self) -> None:
        response = self.client.post(
            f"{PREFIX}/accounts/login",
            json={"account": "alice", "cookie": "missing-equals-sign"},
        )
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["error"]["code"], "INVALID_COOKIE")

    def test_material_upload_is_account_scoped_and_deduplicated(self) -> None:
        first = self._upload("alice", "cover.jpg", b"same-image", "image/jpeg")
        second = self._upload("alice", "renamed.jpg", b"same-image", "image/jpeg")
        other = self._upload("bob", "cover.jpg", b"same-image", "image/jpeg")

        self.assertRegex(first["id"], HEX_UUID)
        self.assertEqual(second["id"], first["id"])
        self.assertTrue(second["deduplicated"])
        self.assertNotEqual(other["id"], first["id"])

    def test_material_callback_mode_stages_files_and_returns_task_id(self) -> None:
        response = self.client.post(
            f"{PREFIX}/materials",
            data={
                "account": "alice",
                "callback_url": "https://callback.example.com/materials",
            },
            files=[("files", ("clip.mp4", b"fake-video", "video/mp4"))],
        )
        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["data"]["task_id"]
        self.assertRegex(task_id, HEX_UUID)
        staging_dir = self.settings.task_staging_dir / task_id
        self.assertTrue(staging_dir.is_dir())
        self.assertEqual(len(list(staging_dir.iterdir())), 1)

        query = self.client.get(f"{PREFIX}/tasks/{task_id}", params={"account": "alice"})
        self.assertEqual(query.status_code, 200, query.text)
        self.assertEqual(query.json()["data"]["operation"], "upload_materials")

        cancel = self.client.post(
            f"{PREFIX}/tasks/{task_id}/cancel",
            json={"account": "alice"},
        )
        self.assertEqual(cancel.status_code, 200, cancel.text)
        self.assertFalse(staging_dir.exists())

    def test_callback_url_requires_http_or_https(self) -> None:
        response = self.client.post(
            f"{PREFIX}/accounts/login",
            json={
                "account": "alice",
                "cookie": "sessionid=example",
                "callback_url": "ftp://callback.example.com/result",
            },
        )
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["error"]["code"], "VALIDATION_ERROR")

    def test_video_publish_idempotency_and_account_isolation(self) -> None:
        self._mark_logged_in("alice")
        video = self._upload("alice", "clip.mp4", b"fake-video", "video/mp4")
        body = {
            "account": "alice",
            "video_material_id": video["id"],
            "title": "first title",
            "description": "description",
            "tags": ["demo"],
        }
        headers = {"Idempotency-Key": "video-001"}

        body["callback_url"] = "https://callback.example.com/douyin"
        first = self.client.post(f"{PREFIX}/video", json=body, headers=headers)
        replay = self.client.post(f"{PREFIX}/video", json=body, headers=headers)
        self.assertEqual(first.status_code, 202, first.text)
        self.assertEqual(replay.status_code, 202, replay.text)
        first_task = first.json()["data"]
        replay_task = replay.json()["data"]
        self.assertRegex(first_task["task_id"], HEX_UUID)
        self.assertEqual(first_task["task_id"], replay_task["task_id"])
        self.assertTrue(replay_task["idempotent_replay"])

        changed = dict(body, title="changed title")
        conflict = self.client.post(f"{PREFIX}/video", json=changed, headers=headers)
        self.assertEqual(conflict.status_code, 409, conflict.text)
        self.assertEqual(conflict.json()["error"]["code"], "IDEMPOTENCY_CONFLICT")

        hidden = self.client.get(
            f"{PREFIX}/tasks/{first_task['task_id']}",
            params={"account": "bob"},
        )
        self.assertEqual(hidden.status_code, 404, hidden.text)

        in_use = self.client.delete(
            f"{PREFIX}/materials/{video['id']}",
            params={"account": "alice"},
        )
        self.assertEqual(in_use.status_code, 409, in_use.text)

    def test_note_publish_and_required_idempotency_key(self) -> None:
        self._mark_logged_in("alice")
        image = self._upload("alice", "note.png", b"fake-image", "image/png")
        response = self.client.post(
            f"{PREFIX}/note",
            json={
                "account": "alice",
                "image_material_ids": [image["id"]],
                "title": "note title",
                "note": "note body",
            },
        )
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["error"]["code"], "VALIDATION_ERROR")

        accepted = self.client.post(
            f"{PREFIX}/note",
            json={
                "account": "alice",
                "image_material_ids": [image["id"]],
                "title": "note title",
                "note": "note body",
                "callback_url": "https://callback.example.com/douyin",
            },
            headers={"Idempotency-Key": "note-001"},
        )
        self.assertEqual(accepted.status_code, 202, accepted.text)
        self.assertRegex(accepted.json()["data"]["task_id"], HEX_UUID)

        self.assertEqual(
            self.client.post(f"{PREFIX}/publish/note", json={}).status_code,
            404,
        )

    def test_missing_account_check_does_not_launch_browser(self) -> None:
        response = self.client.post(f"{PREFIX}/accounts/check", json={"account": "missing"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json()["data"],
            {"account": "missing", "valid": False, "status": "missing"},
        )


if __name__ == "__main__":
    unittest.main()
