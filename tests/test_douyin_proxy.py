# Language: 中文
import asyncio
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import sau_cli
from uploader.douyin_uploader import main as douyin_main
from uploader.douyin_uploader.main import DouYinNote, DouYinVideo


PROXY = {
    "server": "http://proxy.example:12345",
    "username": "proxy-user",
    "password": "proxy-password-secret",
}


class _AsyncPlaywrightContext:
    def __init__(self, playwright):
        self.playwright = playwright

    async def __aenter__(self):
        return self.playwright

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _PageLocator:
    def __init__(self, *, count=0, visible=False, selector=""):
        self._count = count
        self._visible = visible
        self.selector = selector

    @property
    def first(self):
        return self

    async def count(self):
        return self._count

    async def is_visible(self):
        return self._visible


class _PhoneLoginPage:
    url = "https://creator.douyin.com/creator-micro/content/upload"

    async def goto(self, *_args, **_kwargs):
        return None

    async def wait_for_timeout(self, _milliseconds):
        return None

    def get_by_text(self, _text, **_kwargs):
        return _PageLocator()

    def locator(self, selector):
        is_phone_login = (
            "normal-input" in selector
            or "button-input" in selector
            or "请输入手机号" in selector
            or "请输入验证码" in selector
        )
        return _PageLocator(
            count=1 if is_phone_login else 0,
            visible=is_phone_login,
            selector=selector,
        )


class _UploadPage:
    url = "https://creator.douyin.com/creator-micro/content/upload"

    async def wait_for_timeout(self, _milliseconds):
        return None

    def get_by_text(self, _text, **_kwargs):
        return _PageLocator()

    def locator(self, selector):
        is_file_input = "input[type='file']" in selector
        return _PageLocator(
            count=1 if is_file_input else 0,
            visible=is_file_input,
            selector=selector,
        )


class DouyinProxyLaunchTests(unittest.TestCase):
    def test_launch_helper_passes_playwright_proxy_copy(self):
        playwright = MagicMock()
        browser = object()
        playwright.chromium.launch = AsyncMock(return_value=browser)

        result = asyncio.run(
            douyin_main._launch_douyin_browser(
                playwright,
                headless=True,
                proxy=PROXY,
            )
        )

        self.assertIs(result, browser)
        launch_proxy = playwright.chromium.launch.await_args.kwargs["proxy"]
        self.assertEqual(launch_proxy, PROXY)
        self.assertIsNot(launch_proxy, PROXY)

    def test_proxy_launch_error_hides_credentials(self):
        playwright = MagicMock()
        playwright.chromium.launch = AsyncMock(
            side_effect=RuntimeError(f"failed with {PROXY!r}")
        )

        with self.assertRaises(RuntimeError) as raised:
            asyncio.run(
                douyin_main._launch_douyin_browser(
                    playwright,
                    headless=True,
                    proxy=PROXY,
                )
            )

        self.assertNotIn(PROXY["username"], str(raised.exception))
        self.assertNotIn(PROXY["password"], str(raised.exception))

    def test_cdp_and_proxy_are_rejected_without_connecting(self):
        playwright = MagicMock()
        playwright.chromium.connect_over_cdp = AsyncMock()

        with self.assertRaisesRegex(ValueError, "不能同时使用") as raised:
            asyncio.run(
                douyin_main._launch_douyin_browser(
                    playwright,
                    headless=False,
                    cdp_url="http://browser.example:9222",
                    proxy=PROXY,
                )
            )

        self.assertNotIn(PROXY["password"], str(raised.exception))
        playwright.chromium.connect_over_cdp.assert_not_awaited()

    def test_cookie_auth_launches_with_proxy(self):
        playwright = MagicMock()
        launch = AsyncMock(side_effect=RuntimeError("stop after launch assertion"))

        with (
            patch.object(
                douyin_main,
                "async_playwright",
                return_value=_AsyncPlaywrightContext(playwright),
            ),
            patch.object(douyin_main, "_launch_douyin_browser", launch),
            self.assertRaisesRegex(RuntimeError, "stop after launch assertion"),
        ):
            asyncio.run(
                douyin_main.cookie_auth(
                    "/tmp/cookie.json",
                    headless=True,
                    proxy=PROXY,
                )
            )

        self.assertEqual(launch.await_args.kwargs["proxy"], PROXY)

    def test_cookie_auth_rejects_phone_login_form_on_upload_url(self):
        playwright = MagicMock()
        page = _PhoneLoginPage()
        context = MagicMock()
        context.new_page = AsyncMock(return_value=page)
        browser = MagicMock()
        browser.new_context = AsyncMock(return_value=context)
        browser.close = AsyncMock()

        with (
            patch.object(
                douyin_main,
                "async_playwright",
                return_value=_AsyncPlaywrightContext(playwright),
            ),
            patch.object(
                douyin_main,
                "_launch_douyin_browser",
                AsyncMock(return_value=browser),
            ),
            patch.object(
                douyin_main,
                "set_init_script",
                AsyncMock(return_value=context),
            ),
        ):
            valid = asyncio.run(
                douyin_main.cookie_auth(
                    "/tmp/cookie.json",
                    headless=True,
                    proxy=PROXY,
                )
            )

        self.assertFalse(valid)

    def test_upload_input_guard_rejects_phone_login_form(self):
        page = _PhoneLoginPage()

        with self.assertRaisesRegex(RuntimeError, "Cookie 已失效"):
            asyncio.run(
                douyin_main._wait_for_douyin_upload_input(
                    page,
                    kind="video",
                    timeout_ms=100,
                )
            )

    def test_upload_input_guard_selects_only_file_input(self):
        upload_input = asyncio.run(
            douyin_main._wait_for_douyin_upload_input(
                _UploadPage(),
                kind="video",
                timeout_ms=100,
            )
        )

        self.assertIn("input[type='file']", upload_input.selector)

    def test_cookie_generation_launches_with_proxy(self):
        playwright = MagicMock()
        launch = AsyncMock(side_effect=RuntimeError("stop after launch assertion"))

        with (
            patch.object(
                douyin_main,
                "async_playwright",
                return_value=_AsyncPlaywrightContext(playwright),
            ),
            patch.object(douyin_main, "_launch_douyin_browser", launch),
            self.assertRaisesRegex(RuntimeError, "stop after launch assertion"),
        ):
            asyncio.run(
                douyin_main.douyin_cookie_gen(
                    "/tmp/cookie.json",
                    headless=True,
                    proxy=PROXY,
                )
            )

        self.assertEqual(launch.await_args.kwargs["proxy"], PROXY)

    def test_setup_reuses_proxy_for_check_and_login(self):
        login_result = {"success": True, "status": "success"}
        with (
            patch.object(douyin_main.os.path, "exists", return_value=True),
            patch.object(
                douyin_main,
                "cookie_auth",
                AsyncMock(return_value=False),
            ) as auth,
            patch.object(
                douyin_main,
                "douyin_cookie_gen",
                AsyncMock(return_value=login_result),
            ) as login,
        ):
            result = asyncio.run(
                douyin_main.douyin_setup(
                    "/tmp/cookie.json",
                    handle=True,
                    return_detail=True,
                    proxy=PROXY,
                )
            )

        self.assertIs(result, login_result)
        self.assertEqual(auth.await_args.kwargs["proxy"], PROXY)
        self.assertEqual(login.await_args.kwargs["proxy"], PROXY)

    def test_video_upload_launches_with_proxy(self):
        uploader = DouYinVideo(
            "标题",
            "/tmp/video.mp4",
            [],
            0,
            "/tmp/cookie.json",
            proxy=PROXY,
        )
        uploader.validate_upload_args = AsyncMock()
        launch = AsyncMock(side_effect=RuntimeError("stop after launch assertion"))

        with (
            patch.object(douyin_main, "_launch_douyin_browser", launch),
            self.assertRaisesRegex(RuntimeError, "stop after launch assertion"),
        ):
            asyncio.run(uploader.upload(MagicMock()))

        self.assertEqual(launch.await_args.kwargs["proxy"], PROXY)

    def test_note_upload_launches_with_proxy(self):
        uploader = DouYinNote(
            ["/tmp/image.jpg"],
            "正文",
            [],
            0,
            "/tmp/cookie.json",
            title="标题",
            proxy=PROXY,
        )
        uploader.validate_upload_args = AsyncMock()
        launch = AsyncMock(side_effect=RuntimeError("stop after launch assertion"))

        with (
            patch.object(douyin_main, "_launch_douyin_browser", launch),
            self.assertRaisesRegex(RuntimeError, "stop after launch assertion"),
        ):
            asyncio.run(uploader.upload(MagicMock()))

        self.assertEqual(launch.await_args.kwargs["proxy"], PROXY)


class DouyinProxyCliTests(unittest.TestCase):
    def test_request_repr_hides_proxy_credentials(self):
        request = sau_cli.DouyinVideoUploadRequest(
            account_name="creator",
            video_file=Path("video.mp4"),
            title="标题",
            description="正文",
            tags=[],
            publish_date=0,
            proxy=PROXY,
        )

        representation = repr(request)
        self.assertNotIn(PROXY["username"], representation)
        self.assertNotIn(PROXY["password"], representation)

    def test_upload_video_passes_same_proxy_to_check_and_uploader(self):
        request = sau_cli.DouyinVideoUploadRequest(
            account_name="creator",
            account_file=Path("/tmp/cookie.json"),
            video_file=Path("/tmp/video.mp4"),
            title="标题",
            description="正文",
            tags=[],
            publish_date=0,
            proxy=PROXY,
        )
        uploader = MagicMock()
        uploader.douyin_upload_video = AsyncMock()

        with (
            patch.object(sau_cli, "douyin_setup", AsyncMock(return_value=True)) as setup,
            patch.object(sau_cli, "DouYinVideo", return_value=uploader) as uploader_class,
        ):
            asyncio.run(sau_cli.upload_video(request))

        self.assertEqual(setup.await_args.kwargs["proxy"], PROXY)
        self.assertEqual(uploader_class.call_args.kwargs["proxy"], PROXY)

    def test_login_and_check_pass_proxy_to_uploader_layer(self):
        with (
            patch.object(
                sau_cli,
                "resolve_account_file",
                return_value=Path("/tmp/cookie.json"),
            ),
            patch.object(Path, "exists", return_value=True),
            patch.object(
                sau_cli,
                "douyin_setup",
                AsyncMock(return_value={"success": True}),
            ) as setup,
            patch.object(
                sau_cli,
                "douyin_cookie_auth",
                AsyncMock(return_value=True),
            ) as auth,
        ):
            login_result = asyncio.run(
                sau_cli.login_douyin_account("creator", proxy=PROXY)
            )
            check_result = asyncio.run(
                sau_cli.check_douyin_account("creator", proxy=PROXY)
            )

        self.assertTrue(login_result["success"])
        self.assertTrue(check_result)
        self.assertEqual(setup.await_args.kwargs["proxy"], PROXY)
        self.assertEqual(auth.await_args.kwargs["proxy"], PROXY)

    def test_upload_note_passes_same_proxy_to_check_and_uploader(self):
        request = sau_cli.DouyinNoteUploadRequest(
            account_name="creator",
            account_file=Path("/tmp/cookie.json"),
            image_files=[Path("/tmp/image.jpg")],
            title="标题",
            note="正文",
            tags=[],
            publish_date=0,
            proxy=PROXY,
        )
        uploader = MagicMock()
        uploader.douyin_upload_note = AsyncMock()

        with (
            patch.object(sau_cli, "douyin_setup", AsyncMock(return_value=True)) as setup,
            patch.object(sau_cli, "DouYinNote", return_value=uploader) as uploader_class,
        ):
            asyncio.run(sau_cli.upload_note(request))

        self.assertEqual(setup.await_args.kwargs["proxy"], PROXY)
        self.assertEqual(uploader_class.call_args.kwargs["proxy"], PROXY)


if __name__ == "__main__":
    unittest.main()
