import contextlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from scripts import diagnose_douyin_proxy as diagnostic


class FakeLocator:
    def __init__(self, *, count: int = 0, visible: bool = False):
        self._count = count
        self._visible = visible

    @property
    def first(self):
        return self

    async def count(self):
        return self._count

    async def is_visible(self):
        return self._visible


class FakePage:
    def __init__(self, url: str, markers: dict[tuple[str, str], FakeLocator] | None = None):
        self.url = url
        self.markers = markers or {}

    def get_by_text(self, text: str, **_kwargs):
        return self.markers.get(("text", text), FakeLocator())

    def locator(self, selector: str):
        return self.markers.get(("locator", selector), FakeLocator())


def mode_probe(
    mode: str,
    ip: str,
    page_status: diagnostic.PageStatus,
    *,
    samples: int = 3,
    ip_values: tuple[str, ...] | None = None,
    errors: tuple[str, ...] = (),
) -> diagnostic.ModeProbe:
    return diagnostic.ModeProbe(
        mode=mode,
        requested_samples=samples,
        ip_samples=ip_values if ip_values is not None else (ip,) * samples,
        ip_errors=errors,
        page_status=page_status.value,
    )


class ProbeClassifierTests(unittest.TestCase):
    def setUp(self):
        self.direct_login = mode_probe(
            "direct", "203.0.113.10", diagnostic.PageStatus.LOGIN_REQUIRED
        )

    def test_proxy_failed_when_no_proxy_ip_is_observed(self):
        proxy = diagnostic.ModeProbe(
            mode="proxy",
            requested_samples=3,
            ip_errors=("timeout",),
            page_status=diagnostic.PageStatus.UNREACHABLE.value,
        )
        self.assertEqual(
            diagnostic.classify_probe(self.direct_login, proxy),
            diagnostic.Verdict.PROXY_FAILED,
        )

    def test_not_applied_when_proxy_exit_matches_direct_exit(self):
        proxy = mode_probe(
            "proxy", "203.0.113.10", diagnostic.PageStatus.LOGIN_REQUIRED
        )
        self.assertEqual(
            diagnostic.classify_probe(self.direct_login, proxy),
            diagnostic.Verdict.NOT_APPLIED,
        )

    def test_unstable_when_proxy_exit_changes_between_samples(self):
        proxy = mode_probe(
            "proxy",
            "198.51.100.20",
            diagnostic.PageStatus.LOGIN_REQUIRED,
            ip_values=("198.51.100.20", "198.51.100.21", "198.51.100.20"),
        )
        self.assertEqual(
            diagnostic.classify_probe(self.direct_login, proxy),
            diagnostic.Verdict.UNSTABLE,
        )

    def test_unstable_when_a_proxy_sample_fails(self):
        proxy = mode_probe(
            "proxy",
            "198.51.100.20",
            diagnostic.PageStatus.LOGIN_REQUIRED,
            ip_values=("198.51.100.20", "198.51.100.20"),
            errors=("timeout",),
        )
        self.assertEqual(
            diagnostic.classify_probe(self.direct_login, proxy),
            diagnostic.Verdict.UNSTABLE,
        )

    def test_changed_ip_with_login_card_is_reported(self):
        proxy = mode_probe(
            "proxy", "198.51.100.20", diagnostic.PageStatus.LOGIN_REQUIRED
        )
        self.assertEqual(
            diagnostic.classify_probe(self.direct_login, proxy),
            diagnostic.Verdict.CHANGES_IP_BUT_LOGIN_STILL_REQUIRED,
        )

    def test_changed_ip_with_verification_is_still_blocked(self):
        proxy = mode_probe(
            "proxy", "198.51.100.20", diagnostic.PageStatus.VERIFICATION_REQUIRED
        )
        self.assertEqual(
            diagnostic.classify_probe(self.direct_login, proxy),
            diagnostic.Verdict.CHANGES_IP_BUT_LOGIN_STILL_REQUIRED,
        )

    def test_authenticated_proxy_after_direct_login_card_may_help(self):
        proxy = mode_probe(
            "proxy", "198.51.100.20", diagnostic.PageStatus.AUTHENTICATED
        )
        self.assertEqual(
            diagnostic.classify_probe(self.direct_login, proxy),
            diagnostic.Verdict.PROXY_MAY_HELP,
        )

    def test_authenticated_direct_and_proxy_is_inconclusive(self):
        direct = mode_probe(
            "direct", "203.0.113.10", diagnostic.PageStatus.AUTHENTICATED
        )
        proxy = mode_probe(
            "proxy", "198.51.100.20", diagnostic.PageStatus.AUTHENTICATED
        )
        self.assertEqual(
            diagnostic.classify_probe(direct, proxy),
            diagnostic.Verdict.INCONCLUSIVE,
        )


class DiagnosticSafetyTests(unittest.TestCase):
    def test_storage_summary_never_contains_cookie_values(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "account.json"
            path.write_text(
                json.dumps(
                    {
                        "cookies": [
                            {
                                "name": "sessionid",
                                "value": "super-secret-cookie",
                                "domain": ".douyin.com",
                            },
                            {
                                "name": "other",
                                "value": "also-secret",
                                "domain": ".example.com",
                            },
                        ],
                        "origins": [{"origin": "https://creator.douyin.com"}],
                    }
                ),
                encoding="utf-8",
            )

            summary = diagnostic.summarize_storage_state(path)

        self.assertEqual(summary.cookie_count, 2)
        self.assertEqual(summary.origin_count, 1)
        self.assertEqual(summary.douyin_cookie_count, 1)
        self.assertNotIn("super-secret-cookie", repr(summary))
        self.assertNotIn("sessionid", repr(summary))

    def test_storage_summary_rejects_malformed_cookie_members(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "account.json"
            path.write_text('{"cookies":["not-an-object"],"origins":[]}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "cookies"):
                diagnostic.summarize_storage_state(path)

    def test_redaction_removes_known_secret_and_url_credentials(self):
        original = (
            "request https://proxy-user:proxy-password@proxy.example:8080 "
            "failed signature=token-value pwd=plain-password"
        )
        redacted = diagnostic.redact_text(original, secrets=("token-value",))
        self.assertNotIn("proxy-user", redacted)
        self.assertNotIn("proxy-password", redacted)
        self.assertNotIn("token-value", redacted)
        self.assertNotIn("plain-password", redacted)
        self.assertIn("<redacted>", redacted)

    def test_url_sanitizer_removes_query_fragment_and_userinfo(self):
        result = diagnostic.sanitize_url(
            "https://user:password@creator.douyin.com/creator-micro/content/upload?token=secret#x"
        )
        self.assertEqual(
            result,
            "https://creator.douyin.com/creator-micro/content/upload",
        )

    def test_terminal_output_masks_both_exit_ips(self):
        direct = mode_probe(
            "direct", "203.0.113.10", diagnostic.PageStatus.LOGIN_REQUIRED
        )
        proxy = mode_probe(
            "proxy", "198.51.100.20", diagnostic.PageStatus.AUTHENTICATED
        )
        report = diagnostic.build_report(
            summary=diagnostic.StorageStateSummary(1, 0, 1),
            direct=direct,
            proxy=proxy,
            tunnel_endpoint_acquired=True,
        )

        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            diagnostic.print_terminal_report(report, None)
        output = stream.getvalue()

        self.assertNotIn("203.0.113.10", output)
        self.assertNotIn("198.51.100.20", output)
        self.assertIn("203.0.*.*", output)
        self.assertIn("198.51.*.*", output)
        self.assertIn("TPS 隧道端点: acquired", output)

    def test_json_output_is_always_mode_0600(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "diagnostic.json"
            path.write_text("old", encoding="utf-8")
            path.chmod(0o644)

            diagnostic.write_private_json(path, {"verdict": "inconclusive"})

            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")),
                {"verdict": "inconclusive"},
            )

    def test_env_file_does_not_override_process_environment(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / ".env"
            path.write_text(
                "DIAGNOSTIC_EXISTING=file\nDIAGNOSTIC_NEW='from-file'\n",
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"DIAGNOSTIC_EXISTING": "process"},
                clear=False,
            ):
                os.environ.pop("DIAGNOSTIC_NEW", None)
                diagnostic.load_env_file(path)
                self.assertEqual(os.environ["DIAGNOSTIC_EXISTING"], "process")
                self.assertEqual(os.environ["DIAGNOSTIC_NEW"], "from-file")

    def test_ip_parser_accepts_ipv4_and_rejects_non_ip_response(self):
        self.assertEqual(
            diagnostic._parse_ip_payload('{"ip":"203.0.113.10"}'),
            "203.0.113.10",
        )
        with self.assertRaisesRegex(RuntimeError, "IP 回显服务"):
            diagnostic._parse_ip_payload('{"ip":"not-an-ip"}')

    def test_upload_url_without_attached_file_input_is_not_authenticated(self):
        page = FakePage(diagnostic.CREATOR_UPLOAD_URL)
        status = diagnostic.asyncio.run(diagnostic.detect_page_status(page))
        self.assertEqual(status, diagnostic.PageStatus.UNKNOWN)

    def test_upload_url_with_attached_file_input_is_authenticated(self):
        page = FakePage(
            diagnostic.CREATOR_UPLOAD_URL,
            {
                (
                    "locator",
                    'input[type="file"], div[class^="container"] input',
                ): FakeLocator(count=1),
            },
        )
        status = diagnostic.asyncio.run(diagnostic.detect_page_status(page))
        self.assertEqual(status, diagnostic.PageStatus.AUTHENTICATED)

    def test_captcha_iframe_overrides_attached_upload_input(self):
        page = FakePage(
            diagnostic.CREATOR_UPLOAD_URL,
            {
                (
                    "locator",
                    'input[type="file"], div[class^="container"] input',
                ): FakeLocator(count=1),
                ("locator", 'iframe[src*="captcha" i]'): FakeLocator(
                    count=1, visible=True
                ),
            },
        )
        status = diagnostic.asyncio.run(diagnostic.detect_page_status(page))
        self.assertEqual(status, diagnostic.PageStatus.VERIFICATION_REQUIRED)

    def test_login_card_overrides_attached_upload_input(self):
        page = FakePage(
            diagnostic.CREATOR_UPLOAD_URL,
            {
                (
                    "locator",
                    'input[type="file"], div[class^="container"] input',
                ): FakeLocator(count=1),
                ("text", "扫码登录"): FakeLocator(count=1, visible=True),
            },
        )
        status = diagnostic.asyncio.run(diagnostic.detect_page_status(page))
        self.assertEqual(status, diagnostic.PageStatus.LOGIN_REQUIRED)


class DiagnosticParserTests(unittest.TestCase):
    def test_parser_accepts_account_storage_alias_and_tuning_flags(self):
        args = diagnostic.build_parser().parse_args(
            [
                "--account-storage-state",
                "/tmp/account.json",
                "--account",
                "creator",
                "--user-id",
                "operator",
                "--samples",
                "5",
                "--interval",
                "0.25",
                "--json-output",
                "/tmp/result.json",
                "--headful",
            ]
        )
        self.assertEqual(args.storage_state, Path("/tmp/account.json"))
        self.assertEqual(args.account, "creator")
        self.assertEqual(args.user_id, "operator")
        self.assertEqual(args.samples, 5)
        self.assertEqual(args.interval, 0.25)
        self.assertEqual(args.json_output, Path("/tmp/result.json"))
        self.assertTrue(args.headful)

    def test_exit_code_is_zero_only_when_proxy_may_help(self):
        for verdict, expected in (
            (diagnostic.Verdict.PROXY_MAY_HELP.value, 0),
            (diagnostic.Verdict.PROXY_FAILED.value, 2),
            (diagnostic.Verdict.NOT_APPLIED.value, 2),
            (diagnostic.Verdict.UNSTABLE.value, 2),
            (diagnostic.Verdict.CHANGES_IP_BUT_LOGIN_STILL_REQUIRED.value, 2),
            (diagnostic.Verdict.INCONCLUSIVE.value, 2),
        ):
            with self.subTest(verdict=verdict):
                self.assertEqual(
                    diagnostic.exit_code_for_verdict(verdict),
                    expected,
                )


if __name__ == "__main__":
    unittest.main()
