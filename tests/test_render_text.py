from __future__ import annotations

import unittest

from mailattic.query import render_text


class RenderTextTests(unittest.TestCase):
    """Tests for render_text() — the HTML-to-plain-text cleaner."""

    def test_none_input_returns_empty_string(self) -> None:
        self.assertEqual(render_text(None), "")

    def test_empty_string_returns_empty_string(self) -> None:
        self.assertEqual(render_text(""), "")

    def test_plain_text_passthrough(self) -> None:
        result = render_text("Hello world")
        self.assertEqual(result, "Hello world")

    def test_removes_html_tags(self) -> None:
        result = render_text("<p>Hello <b>world</b></p>")
        self.assertNotIn("<", result)
        self.assertNotIn(">", result)
        self.assertIn("Hello", result)
        self.assertIn("world", result)

    def test_removes_style_block(self) -> None:
        body = (
            "<html><head>"
            "<style>body { font-family: Arial; } p { color: red; }</style>"
            "</head><body><p>Content here</p></body></html>"
        )
        result = render_text(body)
        self.assertNotIn("<style>", result)
        self.assertNotIn("font-family", result)
        self.assertIn("Content here", result)

    def test_removes_font_face_block(self) -> None:
        body = (
            "@font-face { font-family: 'Calibri'; src: url('calibri.ttf'); }\n"
            "<p>Real content</p>"
        )
        result = render_text(body)
        self.assertNotIn("@font-face", result)
        self.assertIn("Real content", result)

    def test_unescapes_html_entities(self) -> None:
        result = render_text("Price &amp; Terms &lt;details&gt; &#169; 2024")
        self.assertIn("&", result)
        self.assertIn("©", result)
        self.assertNotIn("&amp;", result)
        self.assertNotIn("&lt;", result)
        self.assertNotIn("&gt;", result)

    def test_collapses_horizontal_whitespace(self) -> None:
        result = render_text("Hello   \t   world")
        self.assertNotIn("   ", result)
        self.assertIn("Hello world", result)

    def test_collapses_multiple_blank_lines(self) -> None:
        result = render_text("Line 1\n\n\n\nLine 2")
        # Should not have more than one blank line between content
        self.assertNotIn("\n\n\n", result)
        self.assertIn("Line 1", result)
        self.assertIn("Line 2", result)

    def test_outlook_export_sample(self) -> None:
        """Simulate a realistic Outlook PST export body with HTML noise."""
        body = """\
<html xmlns:v="urn:schemas-microsoft-com:vml">
<head>
<style>
@font-face {
 font-family:"Cambria Math";
 panose-1:2 4 5 3 5 4 6 3 2 4;
}
@font-face {
 font-family:Calibri;
 panose-1:2 15 5 2 2 2 4 3 2 4;
}
p.MsoNormal, li.MsoNormal, div.MsoNormal
 {margin:0cm;
 font-size:12.0pt;
 font-family:"Calibri",sans-serif;}
a:link, span.MsoHyperlink
 {color:#0563C1;
 text-decoration:underline;}
</style>
</head>
<body>
<p class="MsoNormal">Dear Customer,</p>
<p class="MsoNormal">&nbsp;</p>
<p class="MsoNormal">Please find the quotation attached. Price: USD&nbsp;150/box</p>
<p class="MsoNormal">&nbsp;</p>
<p class="MsoNormal">Best regards,<br>Sean</p>
</body>
</html>"""
        result = render_text(body)
        # Must not contain HTML artefacts
        self.assertNotIn("<", result)
        self.assertNotIn(">", result)
        self.assertNotIn("@font-face", result)
        self.assertNotIn("<style>", result)
        # Must contain readable content
        self.assertIn("Dear Customer", result)
        self.assertIn("quotation", result)
        self.assertIn("Best regards", result)
        self.assertIn("Sean", result)

    def test_plain_text_braces_not_treated_as_css(self) -> None:
        """Issue #289: plain text with curly braces is not treated as CSS rules."""
        examples = [
            ("價格：{100 USD/box}，含運。", "價格：{100 USD/box}，含運。"),
            ("Hello {name}, your order shipped.", "Hello {name}, your order shipped."),
            (
                'API sample: {"order_id": 77, "qty": 200}',
                'API sample: {"order_id": 77, "qty": 200}',
            ),
            (
                "規格如下：厚度 2.0 mm，公差 {±0.5} mm。",
                "規格如下：厚度 2.0 mm，公差 {±0.5} mm。",
            ),
            (
                "Please check items {\n  A-100\n  E-205\n} before Friday.",
                "Please check items {\n A-100\n E-205\n} before Friday.",
            ),
            (
                "Please confirm the quote: unit price 100 USD per box, "
                "minimum 20 boxes {Pickup Riverside}.",
                "Please confirm the quote: unit price 100 USD per box, "
                "minimum 20 boxes {Pickup Riverside}.",
            ),
        ]
        for inp, expected in examples:
            with self.subTest(input=inp):
                self.assertEqual(render_text(inp), expected)

    def test_line_indentation_collapses_to_single_space(self) -> None:
        """L1: Leading horizontal indentation on lines collapses to a single space."""
        inp = "Items:\n    A-100\n\tE-205\nend"
        expected = "Items:\n A-100\n E-205\nend"
        self.assertEqual(render_text(inp), expected)

    def test_outlook_and_media_css_rules_cleaned(self) -> None:
        """L2: Escaped namespaces (v\\:*), *, and @media blocks are stripped."""
        cases = [
            (
                "v\\:* {behavior:url(#default#VML);} "
                "o\\:* {behavior:url(#default#VML);} Dear Ana",
                "Dear Ana",
            ),
            (
                ".ExternalClass * {line-height:100%;} Dear Ana",
                "Dear Ana",
            ),
            (
                "@media print {.noprint {display:none;}} Dear Ana",
                "Dear Ana",
            ),
        ]
        for inp, expected in cases:
            with self.subTest(input=inp):
                self.assertEqual(render_text(inp), expected)

    def test_url_query_parameters_not_unescaped_as_legacy_entities(self) -> None:
        """Issue #289: URL parameters with legacy entity names are preserved."""
        examples = [
            (
                "https://portal.example.com/track?id=5&region=xx&timestamp=1700&currency=USD&quote_id=77",
                "https://portal.example.com/track?id=5&region=xx&timestamp=1700&currency=USD&quote_id=77",
            ),
            (
                "https://portal.example.com/api?item=9&copy=1&lang=zh&not=1&para=2&sect=3",
                "https://portal.example.com/api?item=9&copy=1&lang=zh&not=1&para=2&sect=3",
            ),
            ("AT&T, R&D, P&L", "AT&T, R&D, P&L"),
        ]
        for inp, expected in examples:
            with self.subTest(input=inp):
                self.assertEqual(render_text(inp), expected)

    def test_pseudo_element_and_functional_selectors_are_stripped(self) -> None:
        """Issue #289: the stricter selector grammar still knows ``::`` and ``:f()``."""
        for rule in (
            'p::before {content:"x";}',
            'a::after{content:"";}',
            "p.MsoNormal::first-line {color:red;}",
            "li:nth-child(2) {color:red;}",
            "@MEDIA print {p {color:red;}}",
        ):
            with self.subTest(rule=rule):
                self.assertEqual(render_text(f"{rule}\nDear Ana"), "Dear Ana")

    def test_plain_text_double_colon_before_braces_is_kept(self) -> None:
        """Not CSS: the word before ``::`` is no selector, so nothing is stripped."""
        text = "Price::list {a:b;}\nstd::map {see: below}"
        self.assertEqual(render_text(text), text)


if __name__ == "__main__":
    unittest.main()
