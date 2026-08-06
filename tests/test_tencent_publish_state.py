from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sau_cli import TencentVideoUploadRequest, upload_tencent_video
from uploader.tencent_uploader.main import TencentVideo, normalize_tencent_short_title


class FakeLocator:
    def __init__(
        self,
        *,
        count: int = 0,
        text: str = "",
        on_click=None,
        class_name: str = "weui-desktop-btn",
    ) -> None:
        self._count = count
        self._text = text
        self._on_click = on_click
        self._class_name = class_name

    @property
    def first(self):
        return self

    def nth(self, _index: int):
        return self

    async def count(self) -> int:
        return self._count

    async def click(self, **_kwargs) -> None:
        if self._on_click is not None:
            self._on_click()

    async def is_visible(self) -> bool:
        return self._count > 0

    async def inner_text(self) -> str:
        return self._text

    async def get_attribute(self, _name: str):
        return self._class_name


class FakePublishPage:
    def __init__(self, *, error_text: str = "", redirect_on_click: bool = False) -> None:
        self.url = "https://channels.weixin.qq.com/platform/post/create"
        self.clicks = 0
        self.error_text = error_text
        self.redirect_on_click = redirect_on_click

    def _click(self) -> None:
        self.clicks += 1
        if self.redirect_on_click:
            self.url = "https://channels.weixin.qq.com/platform/post/list?source=publish"

    def locator(self, selector: str) -> FakeLocator:
        if "div.form-btns button" in selector:
            return FakeLocator(count=1, on_click=self._click)
        if "role=\"alert\"" in selector and self.error_text:
            return FakeLocator(count=1, text=self.error_text)
        if selector == "body":
            return FakeLocator(count=1, text=self.error_text or "发布页仍在处理中")
        return FakeLocator()

    def get_by_text(self, _text: str, exact: bool = False) -> FakeLocator:
        return FakeLocator()

    async def wait_for_url(self, _url: str, timeout: int) -> None:
        if "/platform/post/list" not in self.url:
            raise TimeoutError(f"URL did not change in {timeout}ms")

    async def screenshot(self, **_kwargs) -> None:
        return None


class FakeUploadRacePublishPage(FakePublishPage):
    def __init__(self) -> None:
        super().__init__()
        self.upload_warning = False

    def _click(self) -> None:
        self.clicks += 1
        if self.clicks == 1:
            self.upload_warning = True
        else:
            self.url = "https://channels.weixin.qq.com/platform/post/list"

    def get_by_text(self, text: str, exact: bool = False) -> FakeLocator:
        del exact
        if self.upload_warning and text in {"文件上传中", "请等待完成后再发表"}:
            return FakeLocator(count=1, text="文件上传中，请等待完成后再发表。")
        return FakeLocator()


class FakeUploadingPage(FakePublishPage):
    def get_by_role(self, _role: str, name: str) -> FakeLocator:
        return FakeLocator(count=1, class_name="weui-desktop-btn_disabled")


class FakeUploadedPage(FakePublishPage):
    def get_by_role(self, _role: str, name: str) -> FakeLocator:
        return FakeLocator(count=1, class_name="weui-desktop-btn_primary")


class FakePrematurelyEnabledUploadPage(FakeUploadedPage):
    def __init__(self) -> None:
        super().__init__()
        self.pending_upload_scans = 0

    def locator(self, selector: str) -> FakeLocator:
        if "upload-progress" in selector or "取消上传" in selector:
            self.pending_upload_scans += 1
            return FakeLocator(count=int(self.pending_upload_scans <= 2), text="50%")
        return super().locator(selector)


class DynamicLocator(FakeLocator):
    def __init__(self, *, count, on_click=None, on_set_files=None) -> None:
        super().__init__(on_click=on_click)
        self._count_provider = count
        self._on_set_files = on_set_files

    async def count(self) -> int:
        return self._count_provider()

    async def is_visible(self) -> bool:
        return bool(self._count_provider())

    async def set_input_files(self, file_path: str) -> None:
        if self._on_set_files is not None:
            self._on_set_files(file_path)


class LateUploadEntryFrame:
    def __init__(self, page) -> None:
        self.page = page
        self.url = "https://channels.weixin.qq.com/platform"

    def locator(self, selector: str) -> DynamicLocator:
        if selector == 'input[type="file"]':
            self.page.file_input_checks += 1
            return DynamicLocator(
                count=lambda: int(self.page.entry_clicked),
                on_set_files=self.page.uploaded_files.append,
            )
        return DynamicLocator(count=lambda: 0)


class LateUploadEntryPage:
    def __init__(self) -> None:
        self.url = "https://channels.weixin.qq.com/platform"
        self.file_input_checks = 0
        self.entry_clicked = False
        self.uploaded_files: list[str] = []
        self.frames = [LateUploadEntryFrame(self)]

    def get_by_text(self, text: str, exact: bool = False) -> DynamicLocator:
        del exact
        if text == "发表视频":
            return DynamicLocator(
                count=lambda: int(self.file_input_checks >= 3),
                on_click=self._click_entry,
            )
        return DynamicLocator(count=lambda: 0)

    def _click_entry(self) -> None:
        self.entry_clicked = True

    async def screenshot(self, **_kwargs) -> None:
        return None

    def locator(self, selector: str) -> FakeLocator:
        if selector == "body":
            return FakeLocator(count=1, text="视频号助手首页")
        return FakeLocator()


class TencentPublishStateTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def uploader(timeout: float = 0.03) -> TencentVideo:
        uploader = TencentVideo("测试标题", __file__, [], 0, "unused.json")
        uploader.publish_timeout_seconds = timeout
        return uploader

    async def test_publish_timeout_clicks_only_once_and_returns_diagnostic_error(self) -> None:
        page = FakePublishPage()
        with self.assertRaisesRegex(TimeoutError, "视频发布确认超时"):
            await asyncio.wait_for(self.uploader().submit_publish(page), timeout=0.3)
        self.assertEqual(page.clicks, 1)

    async def test_publish_accepts_manage_url_with_query_string(self) -> None:
        page = FakePublishPage(redirect_on_click=True)
        await self.uploader().submit_publish(page)
        self.assertEqual(page.clicks, 1)

    async def test_publish_surfaces_visible_platform_error_without_retrying(self) -> None:
        page = FakePublishPage(error_text="标题包含不支持内容")
        with self.assertRaisesRegex(RuntimeError, "标题包含不支持内容"):
            await asyncio.wait_for(self.uploader().submit_publish(page), timeout=0.3)
        self.assertEqual(page.clicks, 0)

    async def test_upload_wait_has_a_deadline(self) -> None:
        uploader = self.uploader()
        uploader.upload_timeout_seconds = 0.03
        with self.assertRaisesRegex(TimeoutError, "等待视频上传完成超时"):
            await asyncio.wait_for(
                uploader.wait_for_upload_complete(FakeUploadingPage()), timeout=0.3
            )

    async def test_upload_wait_returns_when_publish_button_is_ready(self) -> None:
        with patch(
            "uploader.tencent_uploader.main.asyncio.sleep", new_callable=AsyncMock
        ):
            await self.uploader().wait_for_upload_complete(FakeUploadedPage())

    async def test_upload_wait_ignores_enabled_button_while_upload_is_pending(self) -> None:
        page = FakePrematurelyEnabledUploadPage()
        with patch(
            "uploader.tencent_uploader.main.asyncio.sleep", new_callable=AsyncMock
        ):
            await self.uploader().wait_for_upload_complete(page)

        self.assertGreaterEqual(page.pending_upload_scans, 3)

    async def test_upload_rechecks_entry_until_late_button_appears(self) -> None:
        page = LateUploadEntryPage()
        uploader = self.uploader()
        uploader.upload_entry_timeout_seconds = 0.2
        with patch(
            "uploader.tencent_uploader.main.asyncio.sleep", new_callable=AsyncMock
        ):
            await uploader.upload_video_file(page, "video.mp4")

        self.assertTrue(page.entry_clicked)
        self.assertEqual(page.uploaded_files, ["video.mp4"])

    async def test_publishing_progress_is_emitted_after_publish_click(self) -> None:
        progress = AsyncMock()
        uploader = TencentVideo(
            "测试标题",
            __file__,
            [],
            0,
            "unused.json",
            progress_callback=progress,
        )
        page = FakePublishPage(redirect_on_click=True)

        await uploader.submit_publish(page)

        self.assertEqual(page.clicks, 1)
        progress.assert_awaited_once_with(
            "publishing", "已点击发表，正在等待视频号确认结果"
        )

    async def test_publish_retries_only_when_platform_says_file_is_still_uploading(self) -> None:
        page = FakeUploadRacePublishPage()
        uploader = self.uploader()

        async def finish_upload(_page) -> None:
            page.upload_warning = False

        uploader.wait_for_upload_complete = AsyncMock(side_effect=finish_upload)
        await uploader.submit_publish(page)

        self.assertEqual(page.clicks, 2)
        uploader.wait_for_upload_complete.assert_awaited_once_with(page)

    async def test_cli_request_passes_progress_callback_to_uploader(self) -> None:
        progress = AsyncMock()
        request = TencentVideoUploadRequest(
            account_name="channel_a",
            video_file=Path(__file__),
            title="测试标题",
            description="",
            tags=[],
            publish_date=0,
            account_file=Path("unused.json"),
            progress_callback=progress,
        )
        with (
            patch("sau_cli.tencent_setup", new=AsyncMock(return_value=True)),
            patch("sau_cli.TencentVideo") as uploader_class,
        ):
            uploader_class.return_value.tencent_upload_video = AsyncMock()
            await upload_tencent_video(request)

        self.assertIs(uploader_class.call_args.kwargs["progress_callback"], progress)

    def test_short_title_requires_six_to_sixteen_characters(self) -> None:
        with self.assertRaisesRegex(ValueError, "6-16"):
            normalize_tencent_short_title("海鸥")
        self.assertEqual(
            normalize_tencent_short_title("  海鸥视频短标题  "), "海鸥视频短标题"
        )


if __name__ == "__main__":
    unittest.main()
