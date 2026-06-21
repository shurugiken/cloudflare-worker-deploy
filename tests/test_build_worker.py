"""Tests for build_worker.py -- pure logic, no real filesystem writes, no network."""

import base64
import io
import json
import os
import sys
import textwrap
import tempfile
from unittest.mock import patch, mock_open

import pytest

# Make sure the project root is importable regardless of cwd.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from build_worker import (
    WORKER_TEMPLATE,
    is_local_asset,
    js_escape,
    inline_assets,
    build,
)


# ---------------------------------------------------------------------------
# js_escape
# ---------------------------------------------------------------------------


class TestJsEscape:
    def test_plain_string_unchanged(self):
        assert js_escape("<h1>Hello</h1>") == "<h1>Hello</h1>"

    def test_backtick_escaped(self):
        result = js_escape("pre`post")
        assert result == "pre\\`post"

    def test_dollar_brace_escaped(self):
        result = js_escape("value ${x} end")
        assert result == "value \\${x} end"

    def test_backslash_escaped_first(self):
        # A literal backslash must become \\
        result = js_escape("a\\b")
        assert result == "a\\\\b"

    def test_backslash_before_backtick(self):
        # \` should become \\`  (backslash escaped first, then backtick)
        result = js_escape("\\`")
        assert result == "\\\\\\`"

    def test_multiple_specials_in_sequence(self):
        html = "`${name}`"
        result = js_escape(html)
        # backticks -> \`  and ${ -> \${
        assert "\\`" in result
        assert "\\${" in result
        # No bare backtick or ${ remain
        assert "`" not in result.replace("\\`", "")
        assert "${" not in result.replace("\\${", "")

    def test_empty_string(self):
        assert js_escape("") == ""

    def test_newlines_and_unicode_preserved(self):
        html = "<p>\nHello\n</p>é"
        assert js_escape(html) == html  # no specials to escape


# ---------------------------------------------------------------------------
# is_local_asset
# ---------------------------------------------------------------------------


class TestIsLocalAsset:
    def test_relative_path_is_local(self):
        assert is_local_asset("images/logo.png") is True

    def test_http_is_not_local(self):
        assert is_local_asset("http://example.com/img.png") is False

    def test_https_is_not_local(self):
        assert is_local_asset("https://cdn.example.com/img.png") is False

    def test_protocol_relative_is_not_local(self):
        assert is_local_asset("//cdn.example.com/img.png") is False

    def test_data_uri_is_not_local(self):
        assert is_local_asset("data:image/png;base64,abc") is False

    def test_mailto_is_not_local(self):
        assert is_local_asset("mailto:user@example.com") is False

    def test_tel_is_not_local(self):
        assert is_local_asset("tel:+15551234567") is False

    def test_hash_fragment_is_not_local(self):
        assert is_local_asset("#section") is False

    def test_empty_string_is_not_local(self):
        assert is_local_asset("") is False

    def test_whitespace_only_is_not_local(self):
        assert is_local_asset("   ") is False

    def test_dot_relative_path_is_local(self):
        assert is_local_asset("./style.css") is True

    def test_parent_relative_is_local(self):
        assert is_local_asset("../fonts/font.woff2") is True


# ---------------------------------------------------------------------------
# inline_assets  (uses real tempfiles for local asset resolution)
# ---------------------------------------------------------------------------


class TestInlineAssets:
    def _make_png(self):
        """Return a few bytes that will be treated as a PNG (content doesn't matter for base64)."""
        # A real PNG signature + minimal stub — just needs to be non-empty bytes.
        return b"\x89PNG\r\n\x1a\nFAKE"

    def test_local_image_inlined(self, tmp_path):
        png_bytes = self._make_png()
        img_file = tmp_path / "logo.png"
        img_file.write_bytes(png_bytes)

        html = '<img src="logo.png">'
        result = inline_assets(html, str(tmp_path))

        expected_b64 = base64.b64encode(png_bytes).decode("ascii")
        assert f"data:image/png;base64,{expected_b64}" in result

    def test_remote_url_not_inlined(self, tmp_path):
        html = '<img src="https://example.com/logo.png">'
        result = inline_assets(html, str(tmp_path))
        assert result == html  # unchanged

    def test_missing_local_file_not_inlined(self, tmp_path):
        html = '<img src="missing.png">'
        result = inline_assets(html, str(tmp_path))
        assert result == html  # unchanged (file does not exist)

    def test_already_data_uri_not_inlined(self, tmp_path):
        html = '<img src="data:image/png;base64,abc">'
        result = inline_assets(html, str(tmp_path))
        assert result == html

    def test_href_local_inlined(self, tmp_path):
        css_bytes = b"body { color: red; }"
        css_file = tmp_path / "style.css"
        css_file.write_bytes(css_bytes)

        html = '<link href="style.css">'
        result = inline_assets(html, str(tmp_path))

        expected_b64 = base64.b64encode(css_bytes).decode("ascii")
        assert f"data:text/css;base64,{expected_b64}" in result

    def test_query_string_stripped_for_path_lookup(self, tmp_path):
        png_bytes = self._make_png()
        img_file = tmp_path / "logo.png"
        img_file.write_bytes(png_bytes)

        html = '<img src="logo.png?v=1">'
        result = inline_assets(html, str(tmp_path))

        assert "data:image/png;base64," in result

    def test_multiple_assets_all_inlined(self, tmp_path):
        img1 = tmp_path / "a.png"
        img1.write_bytes(self._make_png())
        img2 = tmp_path / "b.png"
        img2.write_bytes(self._make_png())

        html = '<img src="a.png"><img src="b.png">'
        result = inline_assets(html, str(tmp_path))

        assert result.count("data:image/png;base64,") == 2


# ---------------------------------------------------------------------------
# build  (module shape / template output)
# ---------------------------------------------------------------------------


class TestBuild:
    def _build_html(self, html_content, do_inline=False):
        """Run build() against a temp file and return the produced worker source."""
        with tempfile.TemporaryDirectory() as td:
            html_path = os.path.join(td, "index.html")
            out_path = os.path.join(td, "worker.mjs")
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(html_content)
            build(html_path, out_path, do_inline)
            with open(out_path, "r", encoding="utf-8") as f:
                return f.read()

    def test_output_is_es_module(self):
        src = self._build_html("<h1>Hi</h1>")
        assert "export default" in src

    def test_output_has_fetch_handler(self):
        src = self._build_html("<h1>Hi</h1>")
        assert "async fetch(request)" in src

    def test_output_has_text_html_content_type(self):
        src = self._build_html("<h1>Hi</h1>")
        assert "text/html" in src

    def test_output_has_cache_control_header(self):
        src = self._build_html("<h1>Hi</h1>")
        assert "cache-control" in src

    def test_html_wrapped_in_template_literal(self):
        src = self._build_html("<p>Hello</p>")
        # The HTML constant must be a template literal
        assert "const HTML = `" in src
        assert "<p>Hello</p>" in src

    def test_response_uses_html_constant(self):
        src = self._build_html("<p>x</p>")
        assert "new Response(HTML" in src

    def test_backtick_in_html_escaped(self):
        html_with_tick = "<script>var s = `hello`;</script>"
        src = self._build_html(html_with_tick)
        # The produced source must not contain an unescaped backtick inside
        # the template literal.  After "const HTML = `" the very next backtick
        # that closes the literal should be the one before the semicolon at the
        # end of that line.  Instead of parsing JS, we verify that js_escape ran:
        assert "\\`" in src

    def test_dollar_brace_in_html_escaped(self):
        html_with_interp = "<p>${title}</p>"
        src = self._build_html(html_with_interp)
        assert "\\${" in src

    def test_backslash_in_html_escaped(self):
        html_with_bs = "<p>C:\\Users\\file</p>"
        src = self._build_html(html_with_bs)
        assert "\\\\" in src

    def test_exits_when_html_file_missing(self):
        with pytest.raises(SystemExit):
            build("/nonexistent/path/index.html", "/tmp/out.mjs", False)

    def test_worker_file_written(self):
        with tempfile.TemporaryDirectory() as td:
            html_path = os.path.join(td, "index.html")
            out_path = os.path.join(td, "worker.mjs")
            with open(html_path, "w") as f:
                f.write("<h1>hi</h1>")
            build(html_path, out_path, False)
            assert os.path.isfile(out_path)

    def test_newline_convention_is_unix(self):
        with tempfile.TemporaryDirectory() as td:
            html_path = os.path.join(td, "index.html")
            out_path = os.path.join(td, "worker.mjs")
            with open(html_path, "w") as f:
                f.write("<p>hi</p>")
            build(html_path, out_path, False)
            with open(out_path, "rb") as f:
                raw = f.read()
            assert b"\r\n" not in raw, "worker.mjs must use Unix line endings"
