"""Tests for deploy.py -- mocks all network and filesystem I/O."""

import io
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from deploy import (
    API_BASE,
    build_multipart,
    deploy,
    read_creds,
    report,
)


# ---------------------------------------------------------------------------
# read_creds
# ---------------------------------------------------------------------------


class TestReadCreds:
    def test_returns_creds_when_both_set(self, monkeypatch):
        monkeypatch.setenv("CF_ACCOUNT_ID", "acct123")
        monkeypatch.setenv("CF_API_TOKEN", "tok456")
        account_id, api_token = read_creds()
        assert account_id == "acct123"
        assert api_token == "tok456"

    def test_exits_when_account_id_missing(self, monkeypatch):
        monkeypatch.delenv("CF_ACCOUNT_ID", raising=False)
        monkeypatch.setenv("CF_API_TOKEN", "tok456")
        with pytest.raises(SystemExit) as exc_info:
            read_creds()
        assert "CF_ACCOUNT_ID" in str(exc_info.value)

    def test_exits_when_api_token_missing(self, monkeypatch):
        monkeypatch.setenv("CF_ACCOUNT_ID", "acct123")
        monkeypatch.delenv("CF_API_TOKEN", raising=False)
        with pytest.raises(SystemExit) as exc_info:
            read_creds()
        assert "CF_API_TOKEN" in str(exc_info.value)

    def test_exits_when_both_missing(self, monkeypatch):
        monkeypatch.delenv("CF_ACCOUNT_ID", raising=False)
        monkeypatch.delenv("CF_API_TOKEN", raising=False)
        with pytest.raises(SystemExit) as exc_info:
            read_creds()
        msg = str(exc_info.value)
        assert "CF_ACCOUNT_ID" in msg
        assert "CF_API_TOKEN" in msg


# ---------------------------------------------------------------------------
# build_multipart
# ---------------------------------------------------------------------------


class TestBuildMultipart:
    FILENAME = "worker.mjs"
    SOURCE = "export default { async fetch(req) { return new Response('hi'); } };"

    def _parse_parts(self, body, boundary):
        """Split a multipart body into a list of (headers_str, body_bytes) tuples."""
        delimiter = f"--{boundary}".encode()
        close = f"--{boundary}--".encode()
        parts = []
        segments = body.split(delimiter)
        for seg in segments[1:]:  # skip preamble before first boundary
            seg = seg.lstrip(b"\r\n")
            if seg.startswith(b"--"):
                break  # closing delimiter
            # Split headers from body on the first blank line
            header_end = seg.find(b"\r\n\r\n")
            if header_end == -1:
                continue
            headers = seg[:header_end].decode("utf-8")
            body_part = seg[header_end + 4 :].rstrip(b"\r\n")
            parts.append((headers, body_part))
        return parts

    def test_returns_bytes_and_content_type_string(self):
        body, ct = build_multipart(self.FILENAME, self.SOURCE)
        assert isinstance(body, bytes)
        assert isinstance(ct, str)

    def test_content_type_is_multipart_form_data(self):
        _, ct = build_multipart(self.FILENAME, self.SOURCE)
        assert ct.startswith("multipart/form-data; boundary=")

    def test_boundary_in_content_type_matches_body(self):
        body, ct = build_multipart(self.FILENAME, self.SOURCE)
        boundary = ct.split("boundary=", 1)[1]
        assert f"--{boundary}".encode() in body

    def test_metadata_part_present(self):
        body, ct = build_multipart(self.FILENAME, self.SOURCE)
        boundary = ct.split("boundary=", 1)[1]
        parts = self._parse_parts(body, boundary)
        # First part should be metadata
        assert len(parts) >= 2
        meta_headers, meta_body = parts[0]
        assert 'name="metadata"' in meta_headers
        assert "application/json" in meta_headers

    def test_metadata_json_contains_main_module(self):
        body, ct = build_multipart(self.FILENAME, self.SOURCE)
        boundary = ct.split("boundary=", 1)[1]
        parts = self._parse_parts(body, boundary)
        _, meta_body = parts[0]
        meta = json.loads(meta_body.decode("utf-8"))
        assert meta["main_module"] == self.FILENAME

    def test_metadata_json_contains_compatibility_date(self):
        body, ct = build_multipart(self.FILENAME, self.SOURCE)
        boundary = ct.split("boundary=", 1)[1]
        parts = self._parse_parts(body, boundary)
        _, meta_body = parts[0]
        meta = json.loads(meta_body.decode("utf-8"))
        assert "compatibility_date" in meta

    def test_module_part_present(self):
        body, ct = build_multipart(self.FILENAME, self.SOURCE)
        boundary = ct.split("boundary=", 1)[1]
        parts = self._parse_parts(body, boundary)
        assert len(parts) >= 2
        mod_headers, mod_body = parts[1]
        assert self.FILENAME in mod_headers

    def test_module_part_content_type_is_js_module(self):
        body, ct = build_multipart(self.FILENAME, self.SOURCE)
        boundary = ct.split("boundary=", 1)[1]
        parts = self._parse_parts(body, boundary)
        mod_headers, _ = parts[1]
        assert "application/javascript+module" in mod_headers

    def test_module_part_body_matches_source(self):
        body, ct = build_multipart(self.FILENAME, self.SOURCE)
        boundary = ct.split("boundary=", 1)[1]
        parts = self._parse_parts(body, boundary)
        _, mod_body = parts[1]
        assert mod_body.decode("utf-8") == self.SOURCE

    def test_body_ends_with_closing_boundary(self):
        body, ct = build_multipart(self.FILENAME, self.SOURCE)
        boundary = ct.split("boundary=", 1)[1]
        assert body.rstrip(b"\r\n").endswith(f"--{boundary}--".encode())

    def test_form_field_name_matches_main_module(self):
        """The module form-field name must equal main_module so CF can find the entry."""
        body, ct = build_multipart(self.FILENAME, self.SOURCE)
        boundary = ct.split("boundary=", 1)[1]
        parts = self._parse_parts(body, boundary)
        mod_headers, _ = parts[1]
        # The Content-Disposition name attr must equal the filename
        assert f'name="{self.FILENAME}"' in mod_headers


# ---------------------------------------------------------------------------
# deploy  (mocks urllib.request.urlopen -- no real network calls)
# ---------------------------------------------------------------------------


class TestDeploy:
    def _make_module_file(self, tmp_path, source="export default {};"):
        p = tmp_path / "worker.mjs"
        p.write_text(source, encoding="utf-8")
        return str(p)

    def _mock_response(self, payload_dict, status=200):
        """Return a mock that mimics urllib.request.urlopen()'s context-manager response."""
        response_mock = MagicMock()
        response_mock.read.return_value = json.dumps(payload_dict).encode("utf-8")
        response_mock.__enter__ = lambda s: s
        response_mock.__exit__ = MagicMock(return_value=False)
        return response_mock

    def test_puts_to_correct_url(self, tmp_path):
        module_path = self._make_module_file(tmp_path)
        success_payload = {"success": True, "result": {"id": "abc"}, "errors": []}

        with patch("urllib.request.urlopen", return_value=self._mock_response(success_payload)) as mock_open:
            deploy("acct123", "tok456", "my-worker", module_path)

        call_args = mock_open.call_args[0][0]
        assert call_args.full_url == f"{API_BASE}/accounts/acct123/workers/scripts/my-worker"

    def test_request_method_is_put(self, tmp_path):
        module_path = self._make_module_file(tmp_path)
        payload = {"success": True, "result": {}, "errors": []}

        with patch("urllib.request.urlopen", return_value=self._mock_response(payload)) as mock_open:
            deploy("acct123", "tok456", "my-worker", module_path)

        req = mock_open.call_args[0][0]
        assert req.method == "PUT"

    def test_authorization_header_sent(self, tmp_path):
        module_path = self._make_module_file(tmp_path)
        payload = {"success": True, "result": {}, "errors": []}

        with patch("urllib.request.urlopen", return_value=self._mock_response(payload)) as mock_open:
            deploy("acct123", "tok789", "my-worker", module_path)

        req = mock_open.call_args[0][0]
        assert req.get_header("Authorization") == "Bearer tok789"

    def test_content_type_header_is_multipart(self, tmp_path):
        module_path = self._make_module_file(tmp_path)
        payload = {"success": True, "result": {}, "errors": []}

        with patch("urllib.request.urlopen", return_value=self._mock_response(payload)) as mock_open:
            deploy("acct123", "tok456", "my-worker", module_path)

        req = mock_open.call_args[0][0]
        assert req.get_header("Content-type").startswith("multipart/form-data")

    def test_returns_parsed_json_payload(self, tmp_path):
        module_path = self._make_module_file(tmp_path)
        expected = {"success": True, "result": {"id": "xyz"}, "errors": []}

        with patch("urllib.request.urlopen", return_value=self._mock_response(expected)):
            result = deploy("acct123", "tok456", "my-worker", module_path)

        assert result == expected

    def test_exits_when_module_file_missing(self, tmp_path):
        with pytest.raises(SystemExit):
            deploy("acct123", "tok456", "my-worker", str(tmp_path / "missing.mjs"))

    def test_handles_http_error_with_json_body(self, tmp_path):
        module_path = self._make_module_file(tmp_path)
        error_payload = {"success": False, "errors": [{"code": 10000, "message": "Authentication error"}]}
        http_err = urllib.error.HTTPError(
            url="https://api.cloudflare.com/...",
            code=403,
            msg="Forbidden",
            hdrs={},
            fp=io.BytesIO(json.dumps(error_payload).encode()),
        )

        with patch("urllib.request.urlopen", side_effect=http_err):
            result = deploy("acct123", "bad-token", "my-worker", module_path)

        assert result == error_payload

    def test_handles_http_error_with_non_json_body(self, tmp_path):
        module_path = self._make_module_file(tmp_path)
        http_err = urllib.error.HTTPError(
            url="https://api.cloudflare.com/...",
            code=500,
            msg="Internal Server Error",
            hdrs={},
            fp=io.BytesIO(b"plain text error"),
        )

        with patch("urllib.request.urlopen", side_effect=http_err):
            with pytest.raises(SystemExit) as exc_info:
                deploy("acct123", "tok456", "my-worker", module_path)
        assert "500" in str(exc_info.value)

    def test_handles_url_error(self, tmp_path):
        module_path = self._make_module_file(tmp_path)
        url_err = urllib.error.URLError(reason="Name or service not known")

        with patch("urllib.request.urlopen", side_effect=url_err):
            with pytest.raises(SystemExit):
                deploy("acct123", "tok456", "my-worker", module_path)


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


class TestReport:
    def test_success_payload_returns_zero(self, capsys):
        payload = {"success": True, "result": {"id": "abc"}, "errors": []}
        rc = report(payload, "my-worker")
        assert rc == 0

    def test_success_prints_script_name(self, capsys):
        payload = {"success": True, "result": {"id": "abc"}, "errors": []}
        report(payload, "my-worker")
        out = capsys.readouterr().out
        assert "my-worker" in out

    def test_success_prints_script_id_when_present(self, capsys):
        payload = {"success": True, "result": {"id": "script-abc-123"}, "errors": []}
        report(payload, "my-worker")
        out = capsys.readouterr().out
        assert "script-abc-123" in out

    def test_failure_payload_returns_one(self, capsys):
        payload = {
            "success": False,
            "errors": [{"code": 10000, "message": "Authentication error"}],
        }
        rc = report(payload, "my-worker")
        assert rc == 1

    def test_failure_prints_error_message_to_stderr(self, capsys):
        payload = {
            "success": False,
            "errors": [{"code": 10000, "message": "Authentication error"}],
        }
        report(payload, "my-worker")
        err = capsys.readouterr().err
        assert "Authentication error" in err

    def test_failure_prints_error_code_to_stderr(self, capsys):
        payload = {
            "success": False,
            "errors": [{"code": 10000, "message": "Something bad"}],
        }
        report(payload, "my-worker")
        err = capsys.readouterr().err
        assert "10000" in err

    def test_failure_with_empty_errors_list_returns_one(self, capsys):
        payload = {"success": False, "errors": []}
        rc = report(payload, "my-worker")
        assert rc == 1

    def test_result_without_id_does_not_crash(self, capsys):
        payload = {"success": True, "result": {}, "errors": []}
        rc = report(payload, "my-worker")
        assert rc == 0
