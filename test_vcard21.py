"""Tests for byte-oriented vCard 2.1 parsing and 3.0 upgrading."""

import base64
import io
import os
import unittest

from vobject import vcard21
from vobject.base import ParseError
from vobject.vcard21 import STRICT, REPLACE

HERE = os.path.dirname(os.path.abspath(__file__))


def fixture(name):
    with open(os.path.join(HERE, "test_files", name), "rb") as fh:
        return fh.read()


MIXED = fixture("vcard21_mixed_charsets.vcf")
CORRUPT = fixture("vcard21_corrupt.vcf")


class ByteInputTests(unittest.TestCase):
    def test_bytes_and_binary_stream_equivalent(self):
        from_bytes = vcard21.readVCard21Components(MIXED, REPLACE)
        from_stream = vcard21.readVCard21Components(
            io.BytesIO(MIXED), REPLACE)
        self.assertEqual(
            [r.card.fn.value for r in from_bytes],
            [r.card.fn.value for r in from_stream])

    def test_text_stream_rejected(self):
        with self.assertRaises(TypeError):
            vcard21.readVCard21(io.StringIO("BEGIN:VCARD"))

    def test_multiple_cards(self):
        results = vcard21.readVCard21Components(MIXED)
        self.assertEqual(2, len(results))
        self.assertEqual("a王b", results[0].card.fn.value)
        self.assertEqual("北京", results[1].card.fn.value)


class ContinuationTests(unittest.TestCase):
    """QP soft break vs vCard folding vs binary block continuation."""

    @classmethod
    def setUpClass(cls):
        cls.result = vcard21.readVCard21(MIXED)
        cls.card = cls.result.card

    def test_soft_break_inside_escape(self):
        # physical lines 4-5: "a=CD=" CRLF "=F5b" -> soft break falls in the
        # middle of the GBK escape CD F5 (王)
        self.assertEqual("a王b", self.card.fn.value)
        self.assertEqual(self.card.fn.lineNumber, 4)

    def test_soft_break_combined_with_fold(self):
        # physical lines 6-7: line 7 starts with folding whitespace AND is a
        # QP continuation; Big5 (different charset) -> 手機
        self.assertEqual("手機", self.card.note.value)
        self.assertEqual(self.card.note.params.get("CHARSET"), None)
        self.assertEqual(self.card.note.lineNumber, 6)

    def test_plain_vcard_folding(self):
        # physical lines 8-9, ASCII value folded with a leading space
        self.assertEqual("12345", self.card.tel.value)
        self.assertEqual(["HOME"], self.card.tel.singletonparams)

    def test_binary_block_continuation(self):
        # physical lines 10-11: base64 continues on the next physical line
        expected = base64.b64decode("/9j/4AAQSkZJRgABAQE=")
        self.assertEqual(expected, self.card.photo.value)
        self.assertEqual(["JPEG"], self.card.photo.params["TYPE"])
        self.assertEqual(["b"], self.card.photo.params["ENCODING"])


class StructuredValueTests(unittest.TestCase):
    def test_encoded_semicolon_does_not_split_fields(self):
        result = vcard21.readVCard21(MIXED)
        name = result.card.n.value
        # =3B inside the field is data, not a N separator
        self.assertEqual("王;伟", name.family)
        self.assertEqual("", name.given)
        self.assertEqual("", name.additional)

    def test_charset_is_per_property(self):
        result = vcard21.readVCard21(MIXED)
        # FN was GBK, NOTE was Big5; the other must not inherit either
        self.assertEqual("a王b", result.card.fn.value)
        self.assertEqual("手機", result.card.note.value)
        # CHARSET is a 2.1 transport hint and removed from the parsed line
        self.assertNotIn("CHARSET", result.card.fn.params)

    def test_structured_split_after_decoding_order(self):
        # Raw GBK value: U+2010 (A9 5C) then ';' split; the 0x5C trail byte
        # must not be treated as a backslash escape.
        data = (b"BEGIN:VCARD\r\nVERSION:2.1\r\nORG;CHARSET=GBK:"
                + "‐".encode("gbk")
                + b";tail\r\nEND:VCARD\r\n")
        result = vcard21.readVCard21(data)
        self.assertEqual(["‐", "tail"], result.card.org.value)

    def test_adr_fields(self):
        data = (
            b"BEGIN:VCARD\r\nVERSION:2.1\r\n"
            b"ADR;CHARSET=GBK;ENCODING=QUOTED-PRINTABLE:"
            b"PO=3BBOX;=B1=B1=BE=A9;;;;100081;\r\n"
            b"END:VCARD\r\n")
        result = vcard21.readVCard21(data)
        adr = result.card.adr.value
        self.assertEqual("PO;BOX", adr.box)
        self.assertEqual("北京", adr.extended)
        self.assertEqual("100081", adr.code)


class ErrorPolicyTests(unittest.TestCase):
    def test_strict_raises_at_first_physical_line(self):
        with self.assertRaises(ParseError) as ctx:
            vcard21.readVCard21(CORRUPT, STRICT)
        self.assertEqual(3, ctx.exception.lineNumber)
        self.assertEqual("unknown-charset", ctx.exception.issue.kind)

    def test_replace_collects_physical_line_locations(self):
        result = vcard21.readVCard21(CORRUPT, REPLACE)
        kinds = [(i.kind, i.line, i.property) for i in result.issues]
        self.assertIn(("unknown-charset", 3, "FN"), kinds)
        self.assertIn(("bad-quoted-printable", 4, "NOTE"), kinds)
        self.assertIn(("bad-escape", 5, "TEL"), kinds)
        self.assertIn(("bad-base64", 6, "LOGO"), kinds)
        self.assertIn(("dangling-soft-break", 7, "N"), kinds)

    def test_replace_substitution(self):
        result = vcard21.readVCard21(CORRUPT, REPLACE)
        self.assertIn("\ufffd", result.card.note.value)
        # unknown backslash escape keeps the escaped character
        self.assertEqual("1q", result.card.tel.value)

    def test_lossy_maps_properties(self):
        result = vcard21.readVCard21(CORRUPT, REPLACE)
        self.assertEqual(
            {"FN", "NOTE", "TEL", "LOGO", "N"}, set(result.lossy))

    def test_clean_input_has_no_issues(self):
        result = vcard21.readVCard21(MIXED, REPLACE)
        self.assertEqual([], result.issues)


class UpgradeTests(unittest.TestCase):
    def test_lossless_upgrade_to_utf8(self):
        result = vcard21.readVCard21(MIXED)
        data, lossy = vcard21.serializeVCard30(result)
        # pure ASCII headers plus UTF-8 bodies
        self.assertIn(b"VERSION:3.0\r\n", data)
        self.assertIn("FN:a王b\r\n".encode("utf-8"), data)
        self.assertIn("NOTE:手機\r\n".encode("utf-8"), data)
        # an encoded semicolon survives as a 3.0 backslash escape
        self.assertIn("N:王\\;伟;;;;\r\n".encode("utf-8"), data)
        # clean photo round-trips
        self.assertIn(b"ENCODING=b", data)
        self.assertEqual([], lossy)

    def test_lossy_fields_listed(self):
        result = vcard21.readVCard21(CORRUPT, REPLACE)
        _, lossy = vcard21.serializeVCard30(result)
        names = {name for name, _issue in lossy}
        self.assertEqual({"FN", "NOTE", "TEL", "LOGO", "N"}, names)
        # every reported field carries a physical line
        for name, issue in lossy:
            self.assertTrue(issue.line >= 1)

    def test_upgrade_roundtrip(self):
        result = vcard21.readVCard21(MIXED)
        data, _ = vcard21.serializeVCard30(result)
        # The emitted 3.0 bytes parse back through the regular vobject path.
        from vobject.base import readComponents
        card = next(readComponents(data.decode("utf-8"), allowQP=False))
        self.assertEqual("3.0", card.version.value)
        self.assertEqual("a王b", card.fn.value)

    def test_serialize_all_cards(self):
        data, lossy = vcard21.serializeVCard30All(MIXED, REPLACE)
        self.assertEqual(2, data.count(b"BEGIN:VCARD"))
        self.assertEqual([], lossy)


if __name__ == "__main__":
    unittest.main()


class ParameterFormsTests(unittest.TestCase):
    def test_encoding_b_short_form_and_charset_alias(self):
        data = (
            b"BEGIN:VCARD\r\nVERSION:2.1\r\n"
            b"FN;CHARSET=gbk;QUOTED-PRINTABLE:=CD=F5=CE=B0\r\n"
            b"LOGO;ENCODING=b;TYPE=PNG:iVBORw0KGgo=\r\n"
            b"END:VCARD\r\n")
        result = vcard21.readVCard21(data)
        self.assertEqual("王伟", result.card.fn.value)
        self.assertEqual(
            base64.b64decode(b"iVBORw0KGgo="), result.card.logo.value)
        self.assertEqual(["PNG"], result.card.logo.params["TYPE"])
        self.assertNotIn("CHARSET", result.card.fn.params)

    def test_charset_does_not_leak_between_properties(self):
        data = (
            b"BEGIN:VCARD\r\nVERSION:2.1\r\n"
            b"FN;CHARSET=GBK;ENCODING=QUOTED-PRINTABLE:=CD=F5=CE=B0\r\n"
            b"NOTE:plain ascii\r\nEND:VCARD\r\n")
        result = vcard21.readVCard21(data)
        self.assertIsNone(result.card.note.vcard21_charset)
        self.assertEqual("plain ascii", result.card.note.value)


class QuotedParameterTests(unittest.TestCase):
    def test_colon_inside_quoted_parameter(self):
        data = (b'BEGIN:VCARD\r\nVERSION:2.1\r\n'
                b'NOTE;X-URL="http://x/y":a:b:c\r\nEND:VCARD\r\n')
        result = vcard21.readVCard21(data)
        self.assertEqual("a:b:c", result.card.note.value)
        self.assertEqual(["http://x/y"], result.card.note.params["X-URL"])
