"""Offline behavior/security checks for the exact Python embedded in the workflow.

Run from the repository root:
    python3 -m unittest discover -s .github/tests -v

Only the standard library is used. No GitHub or Supabase requests are made.
"""

import contextlib
import io
from pathlib import Path
import ssl
import tempfile
import textwrap
import types
import unittest
from unittest import mock
import urllib.error
import urllib.request
import urllib.response


WORKFLOW = Path(__file__).resolve().parents[1] / "workflows/supabase-keepalive.yml"
ENDPOINT = "https://wufbvfumsrwkgxsztuea.supabase.co/rest/v1/rpc/kokuban_healthcheck"
KEY = "sb_publishable_" + "test_only_0123456789abcdef"
PRIVATE_DETAIL = "DO_NOT_LOG_RESPONSE_HEADERS_OR_EXCEPTION_DETAILS"


def load_program():
    """Extract the only run block; refuse ambiguous workflow restructuring."""
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    starts = [index for index, line in enumerate(lines) if line.strip() == "run: |"]
    if len(starts) != 1:
        raise AssertionError("Expected exactly one embedded Python run block")
    start = starts[0]
    indent = len(lines[start]) - len(lines[start].lstrip())
    block = []
    for line in lines[start + 1:]:
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        block.append(line)
    source = textwrap.dedent("\n".join(block))
    if not source.strip():
        raise AssertionError("The workflow Python block is empty")
    program = types.ModuleType("supabase_keepalive_under_test")
    exec(compile(source, str(WORKFLOW), "exec"), program.__dict__)
    return program


def response(body=b"1", status=200, content_type="application/json"):
    result = mock.MagicMock()
    result.status = status
    result.headers = {} if content_type is None else {"Content-Type": content_type}
    result.read.side_effect = lambda limit: body[:limit]
    result.__enter__.return_value = result
    return result


def http_error(status):
    return urllib.error.HTTPError(
        ENDPOINT, status, PRIVATE_DETAIL + KEY,
        {"X-Private-Detail": PRIVATE_DETAIL},
        io.BytesIO((PRIVATE_DETAIL + KEY).encode()),
    )


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.program = load_program()

    def run_main(self, effects=None, key=KEY, *, summary_target=None, extra_env=None):
        opener = mock.Mock()
        opener.open.side_effect = [response()] if effects is None else effects
        output, errors = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            summary_path = Path(temporary) / "summary.md"
            environment = {
                "SUPABASE_KEEPALIVE_PUBLISHABLE_KEY": key,
                "GITHUB_STEP_SUMMARY": str(summary_path if summary_target is None else summary_target),
            }
            environment.update(extra_env or {})
            with (
                mock.patch.dict(self.program.os.environ, environment, clear=True),
                mock.patch.object(self.program.urllib.request, "build_opener", return_value=opener) as builder,
                mock.patch.object(self.program.time, "sleep") as sleep,
                contextlib.redirect_stdout(output),
                contextlib.redirect_stderr(errors),
            ):
                code = self.program.main()
            summary = summary_path.read_text(encoding="utf-8") if summary_path.is_file() else ""
        combined = output.getvalue() + errors.getvalue() + summary
        self.assertNotIn(KEY, combined)
        self.assertNotIn(PRIVATE_DETAIL, combined)
        self.assertEqual("", errors.getvalue())
        return types.SimpleNamespace(
            code=code, output=output.getvalue(), summary=summary,
            opener=opener, builder=builder, sleep=sleep,
        )

    def assert_failure(self, result, category, calls=1):
        self.assertEqual(1, result.code)
        self.assertIn("::error::", result.output)
        self.assertIn(category, result.output)
        self.assertNotIn("DB_READ_SUCCEEDED", result.output + result.summary)
        self.assertEqual(calls, result.opener.open.call_count)

    def test_success_uses_fixed_get_without_user_authorization(self):
        reply = response(b"1\n", content_type="application/json; charset=utf-8")
        result = self.run_main([reply])
        self.assertEqual(0, result.code)
        self.assertIn("DB_READ_SUCCEEDED", result.summary)
        self.assertIn("does not prove prevention", result.summary)
        self.assertIn("Attempts: 1", result.summary)
        request = result.opener.open.call_args.args[0]
        headers = {name.lower(): value for name, value in request.header_items()}
        self.assertEqual(ENDPOINT, request.full_url)
        self.assertEqual("GET", request.get_method())
        self.assertIsNone(request.data)
        self.assertEqual(KEY, headers["apikey"])
        self.assertEqual("public", headers["accept-profile"])
        self.assertNotIn("authorization", headers)
        self.assertNotIn("cookie", headers)
        self.assertIn("no-cache", headers["cache-control"])
        self.assertEqual(15, result.opener.open.call_args.kwargs["timeout"])
        reply.read.assert_called_once_with(1025)
        reply.__exit__.assert_called_once()
        result.sleep.assert_not_called()

    def test_request_installs_redirect_refusal_and_verified_tls(self):
        result = self.run_main()
        handlers = result.builder.call_args.args
        self.assertTrue(any(isinstance(handler, self.program.NoRedirect) for handler in handlers))
        https = next(handler for handler in handlers if isinstance(handler, urllib.request.HTTPSHandler))
        self.assertEqual(ssl.CERT_REQUIRED, https._context.verify_mode)
        self.assertTrue(https._context.check_hostname)

    def test_environment_cannot_override_destination(self):
        result = self.run_main(extra_env={
            "SUPABASE_URL": "https://attacker.invalid/",
            "ENDPOINT": "https://attacker.invalid/",
        })
        self.assertEqual(ENDPOINT, result.opener.open.call_args.args[0].full_url)

    def test_missing_key_fails_before_any_request(self):
        result = self.run_main([], key="")
        self.assert_failure(result, "MISSING_PUBLISHABLE_KEY", calls=0)
        result.builder.assert_not_called()
        result.sleep.assert_not_called()

    def test_secret_legacy_and_malformed_keys_are_not_sent(self):
        invalid_keys = (
            "sb_secret_" + "test_only_0123456789abcdef",
            "eyJhbGciOiJIUzI1NiJ9.eyJyb2xlIjoic2VydmljZV9yb2xlIn0.signature",
            "eyJhbGciOiJIUzI1NiJ9.eyJyb2xlIjoiYW5vbiJ9.signature",
            KEY + "\n", KEY + "\r\nAuthorization: injected",
            " " + KEY, "sb_publishable_short", "sb_publishable_" + "x" * 129,
        )
        for key in invalid_keys:
            with self.subTest(key_type=key[:14]):
                result = self.run_main([], key=key)
                self.assert_failure(result, "INVALID_PUBLISHABLE_KEY", calls=0)
                self.assertNotIn(key, result.output + result.summary)
                result.builder.assert_not_called()

    def test_json_must_be_exact_integer_one(self):
        for body in (b"true", b"1.0", b'"1"', b"[1]", b"null", b"0", b"2", b'{"ok":1}'):
            with self.subTest(body=body):
                result = self.run_main([response(body)])
                self.assert_failure(result, "UNEXPECTED_RPC_RESULT")
                result.sleep.assert_not_called()

    def test_malformed_json_and_invalid_encoding_fail(self):
        for body in (b"", b"{invalid", b"\xff", b"1 2", PRIVATE_DETAIL.encode()):
            with self.subTest(body=body):
                result = self.run_main([response(body)])
                self.assert_failure(result, "INVALID_JSON")

    def test_non_json_content_type_fails_without_reading_body(self):
        for content_type in ("text/html", "text/plain", None, ""):
            with self.subTest(content_type=content_type):
                reply = response(PRIVATE_DETAIL.encode(), content_type=content_type)
                result = self.run_main([reply])
                self.assert_failure(result, "UNEXPECTED_CONTENT_TYPE")
                reply.read.assert_not_called()

    def test_response_size_is_bounded(self):
        reply = response(b"1" + b" " * 2048)
        result = self.run_main([reply])
        self.assert_failure(result, "RESPONSE_TOO_LARGE")
        reply.read.assert_called_once_with(1025)

    def test_non_200_success_status_is_not_accepted(self):
        for status in (201, 204, 206):
            with self.subTest(status=status):
                reply = response(status=status)
                result = self.run_main([reply])
                self.assert_failure(result, "HTTP_" + str(status))
                reply.read.assert_not_called()

    def test_permanent_http_failures_and_paused_project_do_not_retry(self):
        cases = {
            301: "REDIRECT_REFUSED", 302: "REDIRECT_REFUSED", 303: "REDIRECT_REFUSED",
            307: "REDIRECT_REFUSED", 308: "REDIRECT_REFUSED", 400: "HTTP_400",
            401: "AUTHENTICATION_FAILED", 403: "PERMISSION_DENIED",
            404: "RPC_NOT_FOUND", 405: "RPC_NOT_READ_ONLY",
            501: "HTTP_501", 540: "PROJECT_PAUSED",
        }
        for status, category in cases.items():
            with self.subTest(status=status):
                error = http_error(status)
                result = self.run_main([error])
                self.assert_failure(result, category)
                self.assertTrue(error.fp.closed)
                result.sleep.assert_not_called()

    def test_transient_http_failures_stop_after_three_attempts(self):
        for status in (408, 429, 500, 502, 503, 504):
            with self.subTest(status=status):
                result = self.run_main([http_error(status) for _ in range(3)])
                self.assert_failure(result, "HTTP_" + str(status), calls=3)
                self.assertEqual([mock.call(5), mock.call(15)], result.sleep.call_args_list)
                self.assertIn("Attempts: 3", result.summary)

    def test_transient_failure_then_recovery_succeeds(self):
        result = self.run_main([http_error(503), response()])
        self.assertEqual(0, result.code)
        self.assertEqual(2, result.opener.open.call_count)
        result.sleep.assert_called_once_with(5)
        self.assertIn("HTTP_503", result.output)
        self.assertIn("Attempts: 2", result.summary)

    def test_network_errors_retry_with_bounded_delays(self):
        factories = (
            lambda: urllib.error.URLError(PRIVATE_DETAIL + KEY),
            lambda: TimeoutError(PRIVATE_DETAIL + KEY),
            lambda: ConnectionResetError(PRIVATE_DETAIL + KEY),
            lambda: OSError(PRIVATE_DETAIL + KEY),
        )
        for factory in factories:
            with self.subTest(error_type=type(factory()).__name__):
                result = self.run_main([factory() for _ in range(3)])
                self.assert_failure(result, "NETWORK_FAILED", calls=3)
                self.assertEqual([mock.call(5), mock.call(15)], result.sleep.call_args_list)

    def test_body_read_timeout_retries_and_closes_response(self):
        reply = response()
        reply.read.side_effect = TimeoutError(PRIVATE_DETAIL + KEY)
        result = self.run_main([reply, response()])
        self.assertEqual(0, result.code)
        self.assertEqual(2, result.opener.open.call_count)
        reply.__exit__.assert_called_once()
        result.sleep.assert_called_once_with(5)

    def test_tls_errors_fail_without_retry_or_details(self):
        for error in (
            ssl.SSLCertVerificationError(PRIVATE_DETAIL + KEY),
            urllib.error.URLError(ssl.SSLCertVerificationError(PRIVATE_DETAIL + KEY)),
            ssl.SSLError(PRIVATE_DETAIL + KEY),
        ):
            with self.subTest(error_type=type(error).__name__):
                result = self.run_main([error])
                self.assert_failure(result, "TLS_FAILED")
                result.sleep.assert_not_called()

    def test_unexpected_client_errors_fail_without_exception_text(self):
        result = self.run_main([RuntimeError(PRIVATE_DETAIL + KEY)])
        self.assert_failure(result, "UNEXPECTED_CLIENT_ERROR")
        result.sleep.assert_not_called()

    def test_summary_write_failure_returns_nonzero(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_main(summary_target=directory)
        self.assertEqual(1, result.code)
        self.assertIn("::error::SUMMARY_WRITE_FAILED", result.output)
        self.assertEqual(1, result.opener.open.call_count)

    def test_local_execution_without_github_summary_path(self):
        result = self.run_main(extra_env={"GITHUB_STEP_SUMMARY": ""})
        self.assertEqual(0, result.code)
        self.assertIn("DB_READ_SUCCEEDED", result.output)

    def test_urllib_redirect_flow_never_requests_destination(self):
        # A synthetic HTTPS handler exercises urllib's real redirect machinery.
        # It supplies responses in memory and never opens a socket.
        requests = []

        class InMemoryHTTPS(urllib.request.HTTPSHandler):
            handler_order = 100

            def https_open(self, request):
                requests.append(request)
                reply = urllib.response.addinfourl(
                    io.BytesIO(PRIVATE_DETAIL.encode()),
                    {"Location": "https://attacker.invalid/steal"},
                    request.full_url, code=302,
                )
                reply.msg = "Found"
                return reply

        opener = urllib.request.build_opener(self.program.NoRedirect(), InMemoryHTTPS())
        request = urllib.request.Request(ENDPOINT, headers={"apikey": KEY})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            opener.open(request, timeout=1)
        caught.exception.close()
        self.assertEqual(302, caught.exception.code)
        self.assertEqual(1, len(requests))
        self.assertEqual(ENDPOINT, requests[0].full_url)


if __name__ == "__main__":
    unittest.main()
