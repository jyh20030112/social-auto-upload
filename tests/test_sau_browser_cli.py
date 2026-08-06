import asyncio
import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import sau_cli


class BrowserCliParserTests(unittest.TestCase):
    def test_build_parser_accepts_xiaohongshu_login(self):
        parser = sau_cli.build_parser()
        args = parser.parse_args(["xiaohongshu", "login", "--account", "creator"])
        self.assertEqual(args.platform, "xiaohongshu")
        self.assertEqual(args.action, "login")

    def test_douyin_login_accepts_cookie_file(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cookie_path = Path(tmp_dir) / "cookies.json"
            cookie_path.write_text("[]", encoding="utf-8")
            parser = sau_cli.build_parser()
            args = parser.parse_args(
                ["douyin", "login", "--account", "creator", "--cookie-file", str(cookie_path)]
            )
        self.assertEqual(args.cookie_file, cookie_path)

    def test_douyin_login_cookie_file_defaults_none(self):
        args = sau_cli.build_parser().parse_args(["douyin", "login", "--account", "creator"])
        self.assertIsNone(args.cookie_file)

    def test_douyin_check_has_no_cookie_file_flag(self):
        args = sau_cli.build_parser().parse_args(["douyin", "check", "--account", "creator"])
        self.assertFalse(hasattr(args, "cookie_file"))

    def test_douyin_upload_video_accepts_desc(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            video_path = Path(tmp_dir) / "demo.mp4"
            video_path.write_bytes(b"video")

            parser = sau_cli.build_parser()
            args = parser.parse_args(
                [
                    "douyin",
                    "upload-video",
                    "--account",
                    "creator",
                    "--file",
                    str(video_path),
                    "--title",
                    "标题",
                    "--desc",
                    "视频简介",
                ]
            )

        self.assertEqual(args.desc, "视频简介")

    def test_douyin_upload_video_accepts_dual_thumbnail_aspects(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            video_path = Path(tmp_dir) / "demo.mp4"
            landscape_path = Path(tmp_dir) / "landscape.png"
            portrait_path = Path(tmp_dir) / "portrait.png"
            video_path.write_bytes(b"video")
            landscape_path.write_bytes(b"image")
            portrait_path.write_bytes(b"image")

            parser = sau_cli.build_parser()
            args = parser.parse_args(
                [
                    "douyin",
                    "upload-video",
                    "--account",
                    "creator",
                    "--file",
                    str(video_path),
                    "--title",
                    "标题",
                    "--thumbnail-landscape",
                    str(landscape_path),
                    "--thumbnail-portrait",
                    str(portrait_path),
                ]
            )

        self.assertEqual(args.thumbnail_landscape, landscape_path)
        self.assertEqual(args.thumbnail_portrait, portrait_path)

    def test_douyin_upload_video_accepts_explicit_declaration(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            video_path = Path(tmp_dir) / "demo.mp4"
            video_path.write_bytes(b"video")
            parser = sau_cli.build_parser()
            args = parser.parse_args([
                "douyin", "upload-video", "--account", "creator",
                "--file", str(video_path), "--title", "标题",
                "--declaration", "已确认声明原文",
            ])
        self.assertEqual(args.declaration, "已确认声明原文")

    def test_douyin_upload_video_has_no_implicit_declaration(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            video_path = Path(tmp_dir) / "demo.mp4"
            video_path.write_bytes(b"video")
            args = sau_cli.build_parser().parse_args([
                "douyin", "upload-video", "--account", "creator",
                "--file", str(video_path), "--title", "标题",
            ])
        self.assertIsNone(args.declaration)

    def test_douyin_request_legacy_positional_runtime_flags_keep_their_meaning(self):
        request = sau_cli.DouyinVideoUploadRequest(
            "creator", Path("demo.mp4"), "标题", "简介", [], 0,
            None, None, None, "", "", "scheduled", False, False,
        )
        self.assertEqual(request.publish_strategy, "scheduled")
        self.assertFalse(request.debug)
        self.assertFalse(request.headless)
        self.assertIsNone(request.declaration)

    def test_tencent_upload_video_accepts_dual_thumbnail_aspects(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            video_path = Path(tmp_dir) / "demo.mp4"
            landscape_path = Path(tmp_dir) / "landscape.png"
            portrait_path = Path(tmp_dir) / "portrait.png"
            video_path.write_bytes(b"video")
            landscape_path.write_bytes(b"image")
            portrait_path.write_bytes(b"image")

            parser = sau_cli.build_parser()
            args = parser.parse_args(
                [
                    "tencent",
                    "upload-video",
                    "--account",
                    "creator",
                    "--file",
                    str(video_path),
                    "--title",
                    "标题",
                    "--thumbnail-landscape",
                    str(landscape_path),
                    "--thumbnail-portrait",
                    str(portrait_path),
                ]
            )

        self.assertEqual(args.thumbnail_landscape, landscape_path)
        self.assertEqual(args.thumbnail_portrait, portrait_path)

    def test_kuaishou_upload_note_accepts_title_and_note(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            image_path = Path(tmp_dir) / "1.png"
            image_path.write_bytes(b"image")

            parser = sau_cli.build_parser()
            args = parser.parse_args(
                [
                    "kuaishou",
                    "upload-note",
                    "--account",
                    "creator",
                    "--images",
                    str(image_path),
                    "--title",
                    "图文标题",
                    "--note",
                    "图文正文",
                ]
            )

        self.assertEqual(args.title, "图文标题")
        self.assertEqual(args.note, "图文正文")

    def test_xiaohongshu_upload_video_defaults_to_headless(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            video_path = Path(tmp_dir) / "demo.mp4"
            video_path.write_bytes(b"video")

            parser = sau_cli.build_parser()
            args = parser.parse_args(
                [
                    "xiaohongshu",
                    "upload-video",
                    "--account",
                    "creator",
                    "--file",
                    str(video_path),
                    "--title",
                    "视频标题",
                ]
            )

        self.assertTrue(args.headless)

    def test_xiaohongshu_upload_note_accepts_headed(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            image_path = Path(tmp_dir) / "1.png"
            image_path.write_bytes(b"image")

            parser = sau_cli.build_parser()
            args = parser.parse_args(
                [
                    "xiaohongshu",
                    "upload-note",
                    "--account",
                    "creator",
                    "--images",
                    str(image_path),
                    "--title",
                    "图文标题",
                    "--note",
                    "图文正文",
                    "--headed",
                ]
            )

        self.assertFalse(args.headless)


class DouyinCookieImportTests(unittest.TestCase):
    def test_convert_extension_cookies_basic_mapping(self):
        raw = [
            {
                "domain": "creator.douyin.com",
                "expirationDate": 1786244253,
                "hostOnly": True,
                "httpOnly": False,
                "name": "gfkadpd",
                "path": "/",
                "sameSite": "no_restriction",
                "secure": True,
                "session": False,
                "storeId": None,
                "value": "2906,33638",
            }
        ]
        result = sau_cli.convert_extension_cookies_to_storage_state(raw)
        self.assertEqual(result["origins"], [])
        self.assertEqual(len(result["cookies"]), 1)
        c = result["cookies"][0]
        self.assertEqual(c["name"], "gfkadpd")
        self.assertEqual(c["value"], "2906,33638")
        self.assertEqual(c["domain"], "creator.douyin.com")
        self.assertEqual(c["path"], "/")
        self.assertEqual(c["expires"], 1786244253)
        self.assertFalse(c["httpOnly"])
        self.assertTrue(c["secure"])
        self.assertEqual(c["sameSite"], "None")
        self.assertNotIn("hostOnly", c)
        self.assertNotIn("session", c)
        self.assertNotIn("storeId", c)

    def test_convert_session_cookie_expires_minus_one(self):
        raw = [
            {
                "domain": ".douyin.com",
                "name": "a",
                "value": "1",
                "path": "/",
                "expirationDate": 1786244253,
                "session": True,
                "secure": False,
                "sameSite": "lax",
            },
            {
                "domain": ".douyin.com",
                "name": "b",
                "value": "2",
                "path": "/",
                "session": True,
                "secure": False,
                "sameSite": "lax",
            },  # no expirationDate
        ]
        result = sau_cli.convert_extension_cookies_to_storage_state(raw)
        self.assertEqual([c["expires"] for c in result["cookies"]], [-1, -1])

    def test_convert_samesite_mapping(self):
        raw = [
            {
                "domain": "a.douyin.com",
                "name": f"c{i}",
                "value": "v",
                "path": "/",
                "expirationDate": 1,
                "session": False,
                "secure": True,
                "sameSite": ss,
            }
            for i, ss in enumerate(["no_restriction", "lax", "strict", None, "unspecified"])
        ]
        result = sau_cli.convert_extension_cookies_to_storage_state(raw)
        self.assertEqual([c["sameSite"] for c in result["cookies"]], ["None", "Lax", "Strict", "Lax", "Lax"])

    def test_convert_none_samesite_without_secure_downgraded_to_lax(self):
        raw = [
            {
                "domain": "a.douyin.com",
                "name": "c",
                "value": "1",
                "path": "/",
                "expirationDate": 1,
                "session": False,
                "secure": False,
                "sameSite": "no_restriction",
            }
        ]
        result = sau_cli.convert_extension_cookies_to_storage_state(raw)
        self.assertEqual(result["cookies"][0]["sameSite"], "Lax")
        self.assertFalse(result["cookies"][0]["secure"])

    def test_convert_filters_non_douyin_domains(self):
        raw = [
            {
                "domain": "creator.douyin.com",
                "name": "a",
                "value": "1",
                "path": "/",
                "session": True,
                "secure": False,
                "sameSite": "lax",
            },
            {
                "domain": "example.com",
                "name": "b",
                "value": "2",
                "path": "/",
                "session": True,
                "secure": False,
                "sameSite": "lax",
            },
            {
                "domain": ".bytedance.com",
                "name": "c",
                "value": "3",
                "path": "/",
                "session": True,
                "secure": False,
                "sameSite": "lax",
            },
        ]
        result = sau_cli.convert_extension_cookies_to_storage_state(raw)
        self.assertEqual([c["domain"] for c in result["cookies"]], ["creator.douyin.com"])

    def test_convert_empty_after_filter_raises(self):
        raw = [
            {
                "domain": "example.com",
                "name": "a",
                "value": "1",
                "path": "/",
                "session": True,
                "secure": False,
                "sameSite": "lax",
            }
        ]
        with self.assertRaises(ValueError):
            sau_cli.convert_extension_cookies_to_storage_state(raw)

    def test_convert_rejects_unknown_shape(self):
        with self.assertRaises(ValueError):
            sau_cli.convert_extension_cookies_to_storage_state({"foo": "bar"})
        with self.assertRaises(ValueError):
            sau_cli.convert_extension_cookies_to_storage_state("nope")

    def test_convert_storage_state_passthrough(self):
        raw = {
            "cookies": [
                {
                    "name": "a",
                    "value": "1",
                    "domain": ".douyin.com",
                    "path": "/",
                    "expires": 1786244253.5,
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "None",
                },
                {
                    "name": "b",
                    "value": "2",
                    "domain": "other.com",
                    "path": "/",
                    "expires": -1,
                    "httpOnly": False,
                    "secure": False,
                    "sameSite": "Lax",
                },
            ],
            "origins": [{"origin": "https://creator.douyin.com", "localStorage": []}],
        }
        result = sau_cli.convert_extension_cookies_to_storage_state(raw)
        self.assertEqual(len(result["cookies"]), 1)
        self.assertEqual(result["cookies"][0]["sameSite"], "None")
        self.assertEqual(result["cookies"][0]["expires"], 1786244253.5)
        self.assertEqual(result["origins"], raw["origins"])


class BrowserCliDispatchTests(unittest.TestCase):
    def test_dispatch_douyin_login_import_cookie_success(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cookie_file = Path(tmp_dir) / "export.json"
            cookie_file.write_text(
                json.dumps(
                    [
                        {
                            "domain": ".douyin.com",
                            "name": "sessionid",
                            "value": "abc",
                            "path": "/",
                            "expirationDate": 1786244253,
                            "session": False,
                            "secure": True,
                            "sameSite": "no_restriction",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            account_file = Path(tmp_dir) / "douyin_creator.json"
            args = Namespace(
                platform="douyin", action="login", account="creator", headless=True, cookie_file=cookie_file
            )
            with patch("sau_cli.resolve_account_file", return_value=account_file), patch(
                "sau_cli.douyin_cookie_auth", new=AsyncMock(return_value=True)
            ):
                code = asyncio.run(sau_cli.dispatch(args))
            self.assertEqual(code, 0)
            written = json.loads(account_file.read_text(encoding="utf-8"))
            self.assertEqual(written["origins"], [])
            self.assertEqual(written["cookies"][0]["name"], "sessionid")
            self.assertEqual(written["cookies"][0]["sameSite"], "None")

    def test_dispatch_douyin_login_import_cookie_invalid_raises(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cookie_file = Path(tmp_dir) / "export.json"
            cookie_file.write_text(
                json.dumps(
                    [
                        {
                            "domain": ".douyin.com",
                            "name": "sid",
                            "value": "1",
                            "path": "/",
                            "session": True,
                            "secure": False,
                            "sameSite": "lax",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            account_file = Path(tmp_dir) / "douyin_creator.json"
            args = Namespace(
                platform="douyin", action="login", account="creator", headless=True, cookie_file=cookie_file
            )
            with patch("sau_cli.resolve_account_file", return_value=account_file), patch(
                "sau_cli.douyin_cookie_auth", new=AsyncMock(return_value=False)
            ):
                with self.assertRaises(RuntimeError):
                    asyncio.run(sau_cli.dispatch(args))

    def test_dispatch_douyin_login_import_invalid_json_raises(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cookie_file = Path(tmp_dir) / "export.json"
            cookie_file.write_text("{not json", encoding="utf-8")
            account_file = Path(tmp_dir) / "douyin_creator.json"
            args = Namespace(
                platform="douyin", action="login", account="creator", headless=True, cookie_file=cookie_file
            )
            with patch("sau_cli.resolve_account_file", return_value=account_file):
                with self.assertRaises(RuntimeError) as ctx:
                    asyncio.run(sau_cli.dispatch(args))
            self.assertIn("JSON", str(ctx.exception))

    def test_dispatch_xiaohongshu_check_prints_valid(self):
        args = Namespace(platform="xiaohongshu", action="check", account="creator")
        with patch("sau_cli.check_xiaohongshu_account", new=AsyncMock(return_value=True)):
            code = asyncio.run(sau_cli.dispatch(args))
        self.assertEqual(code, 0)

    def test_dispatch_douyin_upload_note_uses_new_request_fields(self):
        args = Namespace(
            platform="douyin",
            action="upload-note",
            account="creator",
            images=[Path("1.png")],
            title="图文标题",
            note="图文正文",
            tags="测试,图文",
            schedule=0,
            debug=False,
            headless=True,
        )
        with patch("sau_cli.upload_note", new=AsyncMock()) as mock_upload:
            asyncio.run(sau_cli.dispatch(args))

        request = mock_upload.await_args.args[0]
        self.assertEqual(request.title, "图文标题")
        self.assertEqual(request.note, "图文正文")

    def test_dispatch_douyin_upload_video_uses_dual_thumbnail_request_fields(self):
        args = Namespace(
            platform="douyin",
            action="upload-video",
            account="creator",
            file=Path("demo.mp4"),
            title="视频标题",
            desc="视频简介",
            tags="测试,视频",
            schedule=0,
            thumbnail=None,
            thumbnail_landscape=Path("landscape.png"),
            thumbnail_portrait=Path("portrait.png"),
            product_link="",
            product_title="",
            declaration="已确认声明原文",
            debug=False,
            headless=True,
        )
        with patch("sau_cli.upload_video", new=AsyncMock()) as mock_upload:
            asyncio.run(sau_cli.dispatch(args))

        request = mock_upload.await_args.args[0]
        self.assertEqual(request.thumbnail_landscape_file, Path("landscape.png"))
        self.assertEqual(request.thumbnail_portrait_file, Path("portrait.png"))
        self.assertEqual(request.declaration, "已确认声明原文")

    def test_dispatch_tencent_upload_video_uses_dual_thumbnail_request_fields(self):
        args = Namespace(
            platform="tencent",
            action="upload-video",
            account="creator",
            file=Path("demo.mp4"),
            title="视频标题",
            desc="视频简介",
            tags="测试,视频",
            schedule=0,
            thumbnail=None,
            thumbnail_landscape=Path("landscape.png"),
            thumbnail_portrait=Path("portrait.png"),
            short_title=None,
            category=None,
            draft=False,
            debug=False,
            headless=True,
        )
        with patch("sau_cli.upload_tencent_video", new=AsyncMock()) as mock_upload:
            asyncio.run(sau_cli.dispatch(args))

        request = mock_upload.await_args.args[0]
        self.assertEqual(request.thumbnail_landscape_file, Path("landscape.png"))
        self.assertEqual(request.thumbnail_portrait_file, Path("portrait.png"))

    def test_dispatch_xiaohongshu_upload_video_uses_headed_request(self):
        args = Namespace(
            platform="xiaohongshu",
            action="upload-video",
            account="creator",
            file=Path("demo.mp4"),
            title="视频标题",
            desc="视频简介",
            tags="测试,视频",
            schedule=0,
            thumbnail=None,
            debug=False,
            headless=False,
        )
        with patch("sau_cli.upload_xiaohongshu_video", new=AsyncMock()) as mock_upload:
            asyncio.run(sau_cli.dispatch(args))

        request = mock_upload.await_args.args[0]
        self.assertEqual(request.title, "视频标题")
        self.assertEqual(request.description, "视频简介")
        self.assertFalse(request.headless)

    def test_dispatch_xiaohongshu_upload_note_uses_headless_request(self):
        args = Namespace(
            platform="xiaohongshu",
            action="upload-note",
            account="creator",
            images=[Path("1.png"), Path("2.png")],
            title="图文标题",
            note="图文正文",
            tags="测试,图文",
            schedule=0,
            debug=False,
            headless=True,
        )
        with patch("sau_cli.upload_xiaohongshu_note", new=AsyncMock()) as mock_upload:
            asyncio.run(sau_cli.dispatch(args))

        request = mock_upload.await_args.args[0]
        self.assertEqual(request.title, "图文标题")
        self.assertEqual(request.note, "图文正文")
        self.assertTrue(request.headless)
        self.assertEqual(len(request.image_files), 2)


if __name__ == "__main__":
    unittest.main()
