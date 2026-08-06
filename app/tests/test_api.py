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
BASE = "/api/v1"
DOUYIN = f"{BASE}/douyin"
SHIPIN = f"{BASE}/shipin"


class PublishingApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        data_dir = Path(self.temporary.name)
        self.settings = Settings(
            data_dir=data_dir,
            database_url=f"sqlite+aiosqlite:///{data_dir / 'test.db'}",
            worker_enabled=False,
        )
        self.client_context = TestClient(create_app(self.settings))
        self.client = self.client_context.__enter__()

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        self.temporary.cleanup()

    @staticmethod
    def headers(user_id: str = "user_a", **extra: str) -> dict[str, str]:
        return {"X-User-ID": user_id, **extra}

    def _upload(
        self,
        user_id: str,
        filename: str,
        content: bytes,
        mime_type: str,
    ) -> dict:
        response = self.client.post(
            f"{BASE}/materials",
            headers=self.headers(user_id),
            files=[("files", (filename, content, mime_type))],
        )
        self.assertEqual(response.status_code, 200, response.text)
        item = response.json()["data"]["items"][0]
        self.assertTrue(item["success"])
        return item["material"]

    def _mark_logged_in(self, user_id: str, platform: str, account: str) -> None:
        path = self.settings.cookies_dir / user_id / f"{platform}_{account}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"cookies": [], "origins": []}', encoding="utf-8")

    def test_health_is_common_and_requires_no_user_header(self) -> None:
        response = self.client.get(f"{BASE}/health/ready")
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertRegex(payload["request_id"], HEX_UUID)
        self.assertEqual(response.headers["x-request-id"], payload["request_id"])
        self.assertEqual(
            set(payload["data"]),
            {
                "status",
                "database",
                "browser_worker",
                "material_worker",
                "callback_worker",
            },
        )
        self.assertEqual(self.client.get(f"{DOUYIN}/health/ready").status_code, 404)

    def test_openapi_exposes_v2_platform_and_common_routes(self) -> None:
        specification = self.client.get("/openapi.json").json()
        self.assertEqual(specification["info"]["title"], "自媒体自动发布 API")
        self.assertEqual(specification["info"]["version"], "2.0.0")
        self.assertEqual(
            [tag["name"] for tag in specification["tags"]],
            ["douyin", "shipin", "materials", "tasks", "health"],
        )
        expected = {
            f"{BASE}/materials",
            f"{BASE}/materials/{{material_id}}",
            f"{BASE}/tasks/{{task_id}}",
            f"{BASE}/tasks/{{task_id}}/cancel",
            f"{BASE}/health/live",
            f"{BASE}/health/ready",
            f"{DOUYIN}/accounts/login",
            f"{DOUYIN}/accounts/check",
            f"{DOUYIN}/video",
            f"{DOUYIN}/note",
            f"{DOUYIN}/tasks/{{task_id}}/verification-code",
            f"{SHIPIN}/accounts/login",
            f"{SHIPIN}/accounts/check",
            f"{SHIPIN}/video",
        }
        self.assertEqual(set(specification["paths"]), expected)
        multipart = specification["paths"][f"{BASE}/materials"]["post"]["requestBody"][
            "content"
        ]["multipart/form-data"]
        self.assertNotIn("account", json.dumps(multipart))

    def test_business_routes_require_valid_user_id(self) -> None:
        missing = self.client.post(f"{DOUYIN}/accounts/check", json={"account": "alice"})
        invalid = self.client.post(
            f"{DOUYIN}/accounts/check",
            headers={"X-User-ID": "bad user"},
            json={"account": "alice"},
        )
        self.assertEqual(missing.status_code, 422)
        self.assertEqual(invalid.status_code, 422)

    def test_douyin_login_task_is_user_scoped_and_cancel_has_no_body(self) -> None:
        cookie = json.dumps(
            [{"name": "sessionid", "value": "demo", "domain": ".douyin.com", "path": "/"}]
        )
        response = self.client.post(
            f"{DOUYIN}/accounts/login",
            headers=self.headers(),
            json={
                "account": "alice",
                "cookie": cookie,
                "callback_url": "https://callback.example.com/douyin",
            },
        )
        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["data"]["task_id"]
        self.assertRegex(task_id, HEX_UUID)

        hidden = self.client.get(f"{BASE}/tasks/{task_id}", headers=self.headers("user_b"))
        self.assertEqual(hidden.status_code, 404)
        visible = self.client.get(f"{BASE}/tasks/{task_id}", headers=self.headers())
        self.assertEqual(visible.status_code, 200, visible.text)
        self.assertEqual(visible.json()["data"]["platform"], "douyin")

        cancelled = self.client.post(
            f"{BASE}/tasks/{task_id}/cancel", headers=self.headers()
        )
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertEqual(cancelled.json()["data"]["status"], "cancelled")
        self.assertFalse((self.settings.temporary_dir / f"{task_id}.cookie.json").exists())

    def test_shipin_login_accepts_required_raw_cookie_fields(self) -> None:
        response = self.client.post(
            f"{SHIPIN}/accounts/login",
            headers=self.headers(),
            json={
                "account": "channel_a",
                "cookie": "wxuin=123456;sessionid=example",
                "callback_url": "https://callback.example.com/shipin",
            },
        )
        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["data"]["task_id"]
        storage_state = json.loads(
            (self.settings.temporary_dir / f"{task_id}.cookie.json").read_text(encoding="utf-8")
        )
        cookies = {item["name"]: item for item in storage_state["cookies"]}
        self.assertEqual(set(cookies), {"wxuin", "sessionid"})
        self.assertEqual(cookies["wxuin"]["domain"], "channels.weixin.qq.com")
        self.assertEqual(cookies["sessionid"]["sameSite"], "None")
        self.client.post(f"{BASE}/tasks/{task_id}/cancel", headers=self.headers())

        invalid = self.client.post(
            f"{SHIPIN}/accounts/login",
            headers=self.headers(),
            json={"account": "channel_a", "cookie": "wxuin=123456"},
        )
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(invalid.json()["error"]["code"], "INVALID_COOKIE")

    def test_materials_are_user_scoped_and_shared_across_accounts(self) -> None:
        first = self._upload("user_a", "cover.jpg", b"same-image", "image/jpeg")
        second = self._upload("user_a", "renamed.jpg", b"same-image", "image/jpeg")
        other = self._upload("user_b", "cover.jpg", b"same-image", "image/jpeg")
        self.assertEqual(second["id"], first["id"])
        self.assertTrue(second["deduplicated"])
        self.assertNotEqual(other["id"], first["id"])
        self.assertEqual(first["user_id"], "user_a")

    def test_material_callback_task_has_null_platform(self) -> None:
        response = self.client.post(
            f"{BASE}/materials",
            headers=self.headers(),
            data={"callback_url": "https://callback.example.com/materials"},
            files=[("files", ("clip.mp4", b"fake-video", "video/mp4"))],
        )
        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["data"]["task_id"]
        queried = self.client.get(f"{BASE}/tasks/{task_id}", headers=self.headers())
        self.assertIsNone(queried.json()["data"]["platform"])
        self.client.post(f"{BASE}/tasks/{task_id}/cancel", headers=self.headers())

    def test_douyin_video_idempotency_is_user_scoped(self) -> None:
        self._mark_logged_in("user_a", "douyin", "alice")
        video = self._upload("user_a", "clip.mp4", b"fake-video", "video/mp4")
        body = {
            "account": "alice",
            "video_material_id": video["id"],
            "title": "first title",
            "callback_url": "https://callback.example.com/douyin",
        }
        headers = self.headers(**{"Idempotency-Key": "video-001"})
        first = self.client.post(f"{DOUYIN}/video", json=body, headers=headers)
        replay = self.client.post(f"{DOUYIN}/video", json=body, headers=headers)
        self.assertEqual(first.status_code, 202, first.text)
        self.assertEqual(first.json()["data"]["task_id"], replay.json()["data"]["task_id"])
        self.assertTrue(replay.json()["data"]["idempotent_replay"])
        conflict = self.client.post(
            f"{DOUYIN}/video", json={**body, "title": "changed"}, headers=headers
        )
        self.assertEqual(conflict.status_code, 409)
        in_use = self.client.delete(
            f"{BASE}/materials/{video['id']}", headers=self.headers()
        )
        self.assertEqual(in_use.status_code, 409)

    def test_shipin_video_uses_common_material_and_has_no_draft_field(self) -> None:
        self._mark_logged_in("user_a", "shipin", "channel_a")
        video = self._upload("user_a", "clip.mp4", b"shipin-video", "video/mp4")
        body = {
            "account": "channel_a",
            "video_material_id": video["id"],
            "title": "视频号标题",
            "short_title": "视频号短标题",
            "category": "科技",
            "callback_url": "https://callback.example.com/shipin",
        }
        response = self.client.post(
            f"{SHIPIN}/video",
            headers=self.headers(**{"Idempotency-Key": "shipin-001"}),
            json=body,
        )
        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["data"]["task_id"]
        task = self.client.get(f"{BASE}/tasks/{task_id}", headers=self.headers()).json()["data"]
        self.assertEqual(task["platform"], "shipin")
        self.assertNotIn("is_draft", task)

        invalid_short_title = self.client.post(
            f"{SHIPIN}/video",
            headers=self.headers(**{"Idempotency-Key": "shipin-short-title"}),
            json={**body, "short_title": "海鸥"},
        )
        self.assertEqual(invalid_short_title.status_code, 422)

        rejected_draft = self.client.post(
            f"{SHIPIN}/video",
            headers=self.headers(**{"Idempotency-Key": "shipin-draft"}),
            json={**body, "draft": True},
        )
        self.assertEqual(rejected_draft.status_code, 422)

    def test_missing_account_check_does_not_launch_browser(self) -> None:
        response = self.client.post(
            f"{SHIPIN}/accounts/check",
            headers=self.headers(),
            json={"account": "missing"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["data"]["status"], "missing")
        self.assertEqual(response.json()["data"]["platform"], "shipin")


if __name__ == "__main__":
    unittest.main()
