"""Byte-oriented parsing and upgrading of vCard 2.1 data.

vCard 2.1 (RFC 2425/2426 era, exported by many old phones) has features that
make the regular text-oriented :func:`vobject.base.readComponents` path
unsafe for non-ASCII files:

* values are bytes, optionally ``QUOTED-PRINTABLE`` encoded, with a
  per-property ``CHARSET`` (e.g. ``N;CHARSET=GBK;ENCODING=QUOTED-PRINTABLE``);
* long encoded values use the quoted-printable *soft break* (``=`` at the
  end of a physical line), which is distinct from vCard *folding* (the next
  physical line starts with a space or tab) and from the continuation of a
  *binary* block (``ENCODING=BASE64``/``b`` spanning several lines);
* a ``CHARSET`` parameter applies only to the property that carries it.

This module works on :class:`bytes`/binary streams end to end.  The whole
file is never decoded as one string: the structured separators (``;``) of
``N``/``ADR``/``ORG`` are located on the byte stream while quoted-printable
escapes are still opaque, then each component is quoted-printable decoded
and finally decoded with the charset declared on the owning property only.

Error locations are 1-based *physical* line numbers.
"""

import base64
import binascii
import codecs

from .base import Component, ContentLine, ParseError, parseParams
from .vcard import (Address, Name, NameBehavior, AddressBehavior,
                    OrgBehavior, VCard3_0, registerBehavior)
from . import vcard as _vcard

# ---------------------------------------------------------------------------
# Public error reporting
# ---------------------------------------------------------------------------


class ParseIssue(object):
    """A non-fatal decode/escape problem reported in ``errors='replace'``.

    Attributes:
        kind: ``'unknown-charset'``, ``'bad-charset'``,
            ``'bad-quoted-printable'``, ``'bad-escape'``, ``'bad-base64'``,
            ``'dangling-soft-break'``, ``'unterminated-card'``,
            ``'unexpected-end'``, ``'malformed-line'`` or ``'stray-bytes'``.
        message: human readable description.
        line: 1-based physical line number.
        property: owning property name, or ``None`` for card/file problems.
    """

    __slots__ = ('kind', 'message', 'line', 'property')

    def __init__(self, kind, message, line, prop=None):
        self.kind = kind
        self.message = message
        self.line = line
        self.property = prop

    def __repr__(self):
        return "ParseIssue({0!r}, line={1}, property={2!r}): {3}".format(
            self.kind, self.line, self.property, self.message)

    def __str__(self):
        where = "line {0}".format(self.line)
        if self.property:
            where += " ({0})".format(self.property)
        return "{0}: {1}".format(where, self.message)

    def __eq__(self, other):
        if not isinstance(other, ParseIssue):
            return NotImplemented
        return (self.kind == other.kind and self.message == other.message
                and self.line == other.line and self.property == other.property)

    def __hash__(self):
        return hash((self.kind, self.message, self.line, self.property))


STRICT = 'strict'
REPLACE = 'replace'

# Values structured by raw semicolons.  Separators are located on the byte
# stream so an encoded '=3B' or an escaped '\;' can never split a field.
_STRUCTURED_SEMICOLON = frozenset(('N', 'ADR', 'ORG'))

# Lead-byte ranges of DBCS charsets whose trail byte may be 0x3B/0x5C, so raw
# (un-encoded) values must be scanned with lead/trail awareness.
_LEAD_RANGES = {
    'gb2312': ((0x81, 0xFE),),
    'gbk': ((0x81, 0xFE),),
    'gb18030': ((0x81, 0xFE),),
    'big5': ((0x81, 0xFE),),
    'cp949': ((0x81, 0xFE),),
    'euc-kr': ((0x81, 0xFE),),
    'euc-jp': ((0x8E, 0x8E), (0xA1, 0xFE)),
    'shift-jis': ((0x81, 0x9F), (0xE0, 0xFC)),
    'cp932': ((0x81, 0x9F), (0xE0, 0xFC)),
}

_HEXDIGITS = set(b'0123456789ABCDEFabcdef')
_NAME_HEAD = set(b'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz'
                 b'0123456789-_')
_BASE64_CHARS = set(b'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz'
                    b'0123456789+/=\t ')


def _lead_ranges(charset):
    """Lead-byte ``(lo, hi)`` ranges for *charset*, or ``None``."""
    if not charset:
        return None
    name = charset.lower().replace('_', '-')
    if name in _LEAD_RANGES:
        return _LEAD_RANGES[name]
    try:
        canonical = codecs.lookup(name).name.lower().replace('_', '-')
    except (LookupError, TypeError):
        return None
    return _LEAD_RANGES.get(canonical)


# ---------------------------------------------------------------------------
# Byte input -> physical lines
# ---------------------------------------------------------------------------


def _read_bytes(source):
    if isinstance(source, (bytes, bytearray, memoryview)):
        return bytes(source)
    if hasattr(source, 'read'):
        data = source.read()
        if isinstance(data, str):
            raise TypeError(
                "vcard21 needs a binary stream; open the file in 'rb' mode "
                "(read() returned str)")
        return data
    raise TypeError("vcard21 input must be bytes or a binary stream")


def _physical_lines(data):
    """Yield ``(line_number, content)``; content excludes CR/LF/CRLF."""
    line_no = 0
    start = 0
    length = len(data)
    i = 0
    while i < length:
        if data[i] in (0x0D, 0x0A):
            line_no += 1
            if data[i] == 0x0D and i + 1 < length and data[i + 1] == 0x0A:
                end = i
                i += 2
            else:
                end = i
                i += 1
            yield line_no, data[start:end]
            start = i
        else:
            i += 1
    if start < length:
        line_no += 1
        yield line_no, data[start:]


# ---------------------------------------------------------------------------
# Logical blocks: QP soft breaks vs vCard folding vs binary continuation
# ---------------------------------------------------------------------------


class _Segment(object):
    """A piece of one logical property coming from one physical line."""

    __slots__ = ('data', 'line', 'soft', 'fold')

    def __init__(self, data, line, soft=False, fold=False):
        self.data = data
        self.line = line
        # Soft: previous physical line ended a QP value with '=' right here.
        self.soft = soft
        # Fold: this physical line began with vCard folding whitespace.
        self.fold = fold


class _Block(object):
    """One assembled logical line plus continuation provenance."""

    __slots__ = ('header', 'segments', 'is_qp', 'is_base64', 'dangling')

    def __init__(self, header, first_value, line_no, is_qp, is_base64):
        self.header = header
        self.segments = [_Segment(first_value, line_no)]
        self.is_qp = is_qp
        self.is_base64 = is_base64
        self.dangling = False

    @property
    def line(self):
        return self.segments[0].line


def _value_colon(line):
    """Index of the ':' separating header from value (outside double quotes)."""
    quote = False
    for i, ch in enumerate(line):
        if ch == 0x22:
            quote = not quote
        elif ch == 0x3A and not quote:
            return i
    return -1


def _looks_like_header(line):
    """True if a physical line is a property/content line (``NAME[:params]:``)."""
    if not line or line[0] not in _NAME_HEAD:
        return False
    i = 0
    n = len(line)
    while i < n and line[i] in _NAME_HEAD:
        i += 1
    if i < n and line[i] == 0x2E:  # optional "group.NAME"
        i += 1
        j = i
        while i < n and line[i] in _NAME_HEAD:
            i += 1
        if i == j:
            return False
    colon = _value_colon(line)
    return colon >= i


def _header_signals(header):
    """Return ``(is_qp, is_base64)`` from the bytes before the first ':'."""
    upper = header.upper()
    is_qp = b'QUOTED-PRINTABLE' in upper
    is_b64 = (b'ENCODING=BASE64' in upper or b'ENCODING=B' in upper
              or b';BASE64' in upper or upper.endswith(b';B')
              or b';B;' in upper)
    return is_qp, is_b64


def _is_base64_junk(line):
    """True for a bare physical line that can only be a binary continuation."""
    stripped = line.strip()
    if not stripped or _looks_like_header(line):
        return False
    # Beneath a BASE64 property a line of pure base64 alphabet is a binary
    # continuation; a new property always has a ':' and thus looks like a
    # header.
    return all(ch in _BASE64_CHARS for ch in stripped)


def _assemble_blocks(lines):
    """Group physical lines into logical blocks.

    Yields ``('block', _Block)`` or ``('stray', (line_no, raw))``.

    * Physical lines beginning with space/tab are vCard *folded* lines.
    * A ``'='``-terminated quoted-printable value consumes the next physical
      line as a *soft-break* continuation (folded or not).
    * Physical lines beneath a BASE64 property that contain only base64
      alphabet characters are *binary block* continuations.
    """
    current = None
    pending_soft = False

    def finalize(blk, soft):
        if soft and blk.is_qp:
            blk.dangling = True
        return blk

    for line_no, raw in lines:
        folded = raw[:1] in (b' ', b'\t')
        body = raw[1:] if folded else raw

        if not raw.strip():
            if current is not None:
                yield ('block', finalize(current, pending_soft))
                current = None
                pending_soft = False
            continue

        if current is not None:
            if pending_soft:
                # A soft break only consumes a true value continuation.
                # A non-folded line that looks like a new property header
                # means the previous value had a dangling soft break.
                if folded or not _looks_like_header(raw):
                    current.segments.append(
                        _Segment(body, line_no, soft=True, fold=folded))
                    pending_soft = current.is_qp and body.endswith(b'=')
                    continue
                yield ('block', finalize(current, pending_soft))
                current = None
                pending_soft = False
            else:
                if folded:
                    current.segments.append(
                        _Segment(body, line_no, fold=True))
                    pending_soft = current.is_qp and body.endswith(b'=')
                    continue
                if current.is_base64 and _is_base64_junk(raw):
                    current.segments.append(_Segment(raw, line_no))
                    continue
                yield ('block', finalize(current, pending_soft))
                current = None
                pending_soft = False

        if _looks_like_header(raw):
            colon = _value_colon(raw)
            is_qp, is_b64 = _header_signals(raw[:colon])
            current = _Block(raw[:colon], raw[colon + 1:], line_no,
                             is_qp, is_b64)
            pending_soft = is_qp and raw[colon + 1:].endswith(b'=')
        else:
            yield ('stray', (line_no, raw))

    if current is not None:
        yield ('block', finalize(current, pending_soft))


# ---------------------------------------------------------------------------
# Header parsing (ASCII name + parameters)
# ---------------------------------------------------------------------------


def _parse_header(block, issues, errors):
    """Return ``(group, name, params, charset, encoding)``.

    ``params`` uses vobject's list shape (``['CHARSET', 'GBK']`` or a bare
    ``['HOME']``).  The header is pure ASCII; a non-ASCII header is fatal in
    strict mode and recorded (with ASCII replacement) in replace mode.
    """
    try:
        header = block.header.decode('ascii')
    except UnicodeDecodeError as exc:
        issue = ParseIssue(
            'malformed-line',
            "non-ASCII bytes in property header: {0}".format(exc),
            block.line)
        _handle(issues, errors, issue)
        header = block.header.decode('ascii', 'replace')

    if '.' in header.split(';', 1)[0]:
        group, rest = header.split('.', 1)
    else:
        group, rest = None, header
    if ';' in rest:
        name, param_string = rest.split(';', 1)
        params = parseParams(';' + param_string)
    else:
        name = rest
        params = []

    charset = None
    encoding = None
    singleton = []
    cleaned = []
    for item in params:
        key = item[0].upper()
        if key == 'CHARSET' and len(item) > 1:
            charset = item[1]
            continue  # applied during decode, never copied to the vobject line
        if key == 'ENCODING' and len(item) > 1:
            value = item[1].upper()
            if value in ('B', 'BASE64'):
                encoding = 'BASE64'
            elif value == 'QUOTED-PRINTABLE':
                encoding = 'QUOTED-PRINTABLE'
            else:
                encoding = value
            continue
        if key in ('QUOTED-PRINTABLE', 'BASE64', 'B') and len(item) == 1:
            encoding = ('QUOTED-PRINTABLE' if key == 'QUOTED-PRINTABLE'
                        else 'BASE64')
            continue
        cleaned.append(item)
    return group, name.replace('_', '-').upper(), cleaned, charset, encoding


def _handle(issues, errors, issue):
    """Record *issue* (replace) or raise (strict).

    Identical reports (same kind, physical line and property) are merged so a
    single corrupt byte is not reported twice at an escape boundary.
    """
    if errors == STRICT:
        err = ParseError(issue.message, issue.line)
        err.issue = issue
        raise err
    key = (issue.kind, issue.line, issue.property)
    if not any((i.kind, i.line, i.property) == key for i in issues):
        issues.append(issue)


# ---------------------------------------------------------------------------
# Value scanning: separators while QP is still opaque
# ---------------------------------------------------------------------------


class _RawRun(object):
    """A run of bytes that are not quoted-printable escapes."""

    __slots__ = ('data', 'line')

    def __init__(self, data, line):
        self.data = data
        self.line = line


class _QPEscape(object):
    """A ``=XX`` quoted-printable escape (resolved after field splitting)."""

    __slots__ = ('byte', 'line')

    def __init__(self, byte, line):
        self.byte = byte
        self.line = line


class _BadQP(object):
    """A malformed quoted-printable escape (e.g. ``=ZZ`` or trailing ``=``)."""

    __slots__ = ('raw', 'line')

    def __init__(self, raw, line):
        self.raw = raw
        self.line = line


class _TextEscape(object):
    r"""A backslash text escape (``\;`` ``\,`` ``\\`` ``\n``)."""

    __slots__ = ('char', 'line', 'bad')

    def __init__(self, char, line, bad=False):
        self.char = char
        self.line = line
        self.bad = bad


class _Separator(object):
    __slots__ = ('char', 'line')

    def __init__(self, char, line):
        self.char = char
        self.line = line


def _scan_segment(data, line, is_qp, split_semicolon, lead_ranges,
                   pending_lead):
    """Tokenise one physical segment's bytes.

    ``pending_lead`` indicates the previous raw segment ended on a DBCS lead
    byte whose trail byte is the first byte here.  Returns ``(tokens,
    pending_lead)``.
    """
    tokens = []
    run = bytearray()
    run_line = line
    i = 0
    n = len(data)

    def flush():
        if run:
            tokens.append(_RawRun(bytes(run), run_line))
            run.clear()

    while i < n:
        ch = data[i]
        if pending_lead:
            run.append(ch)
            pending_lead = False
            i += 1
            continue
        if is_qp and ch == 0x3D and i + 2 < n:
            if data[i + 1] in _HEXDIGITS and data[i + 2] in _HEXDIGITS:
                flush()
                tokens.append(_QPEscape(int(data[i + 1:i + 3], 16), line))
                i += 3
                continue
            if data[i + 1] in (0x0D, 0x0A):
                # soft break (physical lines are already split, so this
                # cannot occur here, but keep the branch explicit)
                pass
            else:
                flush()
                tokens.append(_BadQP(bytes(data[i:i + 3]), line))
                i += 3
                continue
        if ch == 0x5C and i + 1 < n:
            nxt = data[i + 1]
            if nxt in (0x3B, 0x2C, 0x5C, 0x6E, 0x4E):
                flush()
                tokens.append(_TextEscape(nxt, line))
                i += 2
                continue
            flush()
            tokens.append(_TextEscape(nxt, line, bad=True))
            i += 2
            continue
        if split_semicolon and ch == 0x3B:
            flush()
            tokens.append(_Separator(ch, line))
            i += 1
            continue
        if is_qp and ch == 0x3D:
            flush()
            tokens.append(_BadQP(bytes(data[i:]), line))
            i = n
            continue
        run.append(ch)
        if lead_ranges and _is_lead(ch, lead_ranges):
            pending_lead = True
        i += 1
    flush()
    return tokens, pending_lead


def _scan_segments(segments, is_qp, split_semicolon, lead_ranges):
    """Tokenise assembled value while quoted-printable stays opaque."""
    tokens = []
    pending_lead = False
    for idx, seg in enumerate(segments):
        data = seg.data
        # A soft break ('=' immediately before CRLF) is removed entirely;
        # it may fall in the middle of an '=XX' escape, so strip the
        # terminating '=' before tokenising this segment. The dangling
        # final '=' is stripped here too and reported in _build_property.
        if is_qp and data.endswith(b'='):
            data = data[:-1]
        toks, pending_lead = _scan_segment(
            data, seg.line, is_qp, split_semicolon, lead_ranges,
            pending_lead)
        tokens.extend(toks)
    if pending_lead:
        # A raw DBCS lead byte with no trail byte anywhere: record on the
        # last physical segment; the decode stage reports it.
        tokens.append(_RawRun(b'', segments[-1].line))
    return tokens


def _is_lead(value, ranges):
    return any(lo <= value <= hi for lo, hi in ranges)


# ---------------------------------------------------------------------------
# Component decoding (QP -> text escapes -> charset)
# ---------------------------------------------------------------------------


def _decode_components(tokens, charset, prop, line, issues, errors,
                       split_semicolon):
    """Decode tokens into structured components (list of unicode strings)."""
    ranges = _lead_ranges(charset)
    components = []
    current_tokens = [[]]

    for tok in tokens:
        if isinstance(tok, _Separator):
            components.append(current_tokens[-1])
            current_tokens.append([])
        else:
            current_tokens[-1].append(tok)
    components.append(current_tokens[-1])

    codec_name = _resolve_charset(charset, prop, line, issues, errors)
    decoded = []
    for toks in components:
        decoded.append(_decode_one(toks, codec_name, prop,
                                   line, issues, errors, ranges))
    return decoded


def _resolve_charset(charset, prop, line, issues, errors):
    """Return the usable codec name for *charset*, reporting unknowns."""
    if charset is None:
        return 'utf-8'
    try:
        return codecs.lookup(charset).name
    except LookupError:
        issue = ParseIssue(
            'unknown-charset',
            "unknown CHARSET {0!r} on {1}".format(charset, prop),
            line, prop)
        _handle(issues, errors, issue)
        # non-ASCII bytes then surface as U+FFFD (replace) or as a
        # bad-charset failure (strict never returns from _handle anyway)
        return 'ascii'


def _decode_one(tokens, codec_name, prop, base_line, issues, errors,
                ranges):
    """Decode one structured component or a whole scalar value.

    Bytes are accumulated between vCard text escapes (``\\;`` ``\\,``
    ``\\\\`` ``\\n``); each byte section is decoded as a unit with the
    property's charset, strict first and replace on failure.  Decoding whole
    sections (rather than byte-by-byte) keeps DBCS pairs that span a folded
    physical line intact.
    """

    def decode_section(data, line_no):
        if not data:
            return ''
        try:
            return data.decode(codec_name, STRICT)
        except UnicodeDecodeError as exc:
            issue = ParseIssue(
                'bad-charset',
                "could not decode bytes as {0}: {1}".format(
                    codec_name, exc.reason),
                line_no, prop)
            _handle(issues, errors, issue)
            return data.decode(codec_name, REPLACE)

    out = []
    raw = bytearray()
    raw_line = base_line

    def flush():
        if raw:
            out.append(decode_section(bytes(raw), raw_line))
            raw.clear()

    for tok in tokens:
        if isinstance(tok, _RawRun):
            if not raw:
                raw_line = tok.line
            raw.extend(tok.data)
        elif isinstance(tok, _QPEscape):
            if not raw:
                raw_line = tok.line
            raw.append(tok.byte)
        elif isinstance(tok, _BadQP):
            if raw and ranges and _is_lead(raw[-1], ranges):
                issue = ParseIssue(
                    'bad-charset',
                    "truncated multibyte sequence before malformed "
                    "quoted-printable escape", tok.line, prop)
                _handle(issues, errors, issue)
            flush()
            issue = ParseIssue(
                'bad-quoted-printable',
                "malformed quoted-printable escape {0!r}".format(
                    tok.raw.decode('ascii', 'replace')),
                tok.line, prop)
            _handle(issues, errors, issue)
            out.append('\ufffd')
        elif isinstance(tok, _TextEscape):
            if raw and ranges and _is_lead(raw[-1], ranges):
                issue = ParseIssue(
                    'bad-charset',
                    "truncated multibyte sequence before backslash escape",
                    tok.line, prop)
                _handle(issues, errors, issue)
            flush()
            if tok.bad:
                issue = ParseIssue(
                    'bad-escape',
                    r"unknown vCard escape \{0}".format(chr(tok.char)),
                    tok.line, prop)
                _handle(issues, errors, issue)
                out.append(chr(tok.char))
            elif tok.char in (0x3B, 0x2C, 0x5C):
                out.append(chr(tok.char))
            else:  # \n or \N
                out.append(chr(10))
    flush()
    return ''.join(out)

# ---------------------------------------------------------------------------
# Binary values and property construction
# ---------------------------------------------------------------------------


def _decode_base64(block, prop, issues, errors):
    """Join binary continuation segments and decode base64 to bytes."""
    joined = b''.join(seg.data for seg in block.segments)
    compact = bytes(ch for ch in joined if ch not in (0x09, 0x20, 0x0D, 0x0A))
    try:
        return base64.b64decode(compact, validate=True), True
    except (binascii.Error, ValueError) as exc:
        issue = ParseIssue(
            'bad-base64',
            "invalid base64 data on {0}: {1}".format(prop, exc),
            block.line, prop)
        _handle(issues, errors, issue)
        # Replace policy: keep the recoverable bytes; non-alphabet input is
        # dropped via the lenient decoder rather than raising.
        sanitized = bytes(ch for ch in compact
                          if ch in b'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
                                  b'abcdefghijklmnopqrstuvwxyz0123456789+/')
        padded = sanitized + b'=' * (-len(sanitized) % 4)
        return base64.b64decode(padded, validate=False), False


def _build_property(block, issues, errors):
    """Turn an assembled logical block into a vobject ContentLine."""
    group, name, params, charset, encoding = _parse_header(
        block, issues, errors)

    if block.dangling:
        issue = ParseIssue(
            'dangling-soft-break',
            "quoted-printable value on {0} ends with '=' and has no "
            "continuation line".format(name),
            block.segments[-1].line, name)
        _handle(issues, errors, issue)

    if encoding == 'BASE64':
        raw, lossless = _decode_base64(block, name, issues, errors)
        params.append(['ENCODING', 'b'])
        line = ContentLine(name, params, raw, group=group, encoded=False,
                           lineNumber=block.line)
        line.vcard21_lossless = lossless
        line.vcard21_charset = charset
        if name in ('PHOTO', 'LOGO'):
            line.behavior = _vcard.Photo
        return line

    is_qp = encoding == 'QUOTED-PRINTABLE' or block.is_qp
    split = name in _STRUCTURED_SEMICOLON
    ranges = _lead_ranges(charset)
    tokens = _scan_segments(block.segments, is_qp, split, ranges)

    before = len(issues)
    components = _decode_components(
        tokens, charset, name, block.line, issues, errors, split)
    lossless = len(issues) == before and not block.dangling

    line = ContentLine(name, params, None, group=group, encoded=False,
                       lineNumber=block.line)
    line.vcard21_lossless = lossless
    line.vcard21_charset = charset
    line.vcard21_qp = is_qp

    if name == 'N':
        values = (components + [''] * 5)[:5]
        line.value = Name(**dict(zip(
            ('family', 'given', 'additional', 'prefix', 'suffix'), values)))
        line.behavior = NameBehavior
        line.isNative = True
    elif name == 'ADR':
        values = (components + [''] * 7)[:7]
        line.value = Address(**dict(zip(
            ('box', 'extended', 'street', 'city', 'region', 'code',
             'country'), values)))
        line.behavior = AddressBehavior
        line.isNative = True
    elif name == 'ORG':
        line.value = components
        line.behavior = OrgBehavior
        line.isNative = True
    else:
        line.value = components[0] if components else ''
    return line


# ---------------------------------------------------------------------------
# Card stream
# ---------------------------------------------------------------------------


class VCard21ParseResult(object):
    """One parsed card.

    Attributes:
        card: a vobject :class:`~vobject.base.Component`.
        issues: list of :class:`ParseIssue` encountered for this card.
        lossy: dict mapping property name (uppercase) to the first
            :class:`ParseIssue` that makes its upgrade non-lossless.
    """

    __slots__ = ('card', 'issues', 'lossy')

    def __init__(self, card, issues, lossy):
        self.card = card
        self.issues = issues
        self.lossy = lossy


VCard2_1 = type('VCard2_1', (VCard3_0,), {
    'name': 'VCARD',
    'description': 'vCard 2.1, defined in the vCard 2.1 spec',
    'versionString': '2.1',
})
registerBehavior(VCard2_1, default=False)


def _new_card():
    card = Component('VCARD')
    card.setBehavior(VCard2_1)
    object.__setattr__(card, 'vcard21_issues', [])
    object.__setattr__(card, 'vcard21_lossy', {})
    return card



# ---------------------------------------------------------------------------
# Public parsing API
# ---------------------------------------------------------------------------


def parseVCard21Stream(source, errors=STRICT):
    """Parse binary vCard 2.1 data, yielding :class:`VCard21ParseResult`.

    ``source`` is :class:`bytes`/`bytearray`/a binary file-like object.  The
    whole input is never decoded as one string.  With ``errors='strict'``
    the first problem raises :class:`~vobject.base.ParseError` carrying the
    physical line number; with ``errors='replace'`` invalid bytes become
    U+FFFD, unknown escapes keep the escaped character, and every problem is
    collected in the result's ``issues`` list.
    """
    if errors not in (STRICT, REPLACE):
        raise ValueError("errors must be vcard21.STRICT or vcard21.REPLACE")
    data = _read_bytes(source)

    card = None
    issues = []
    lossy = {}

    def start_card(line_no):
        new = Component('VCARD')
        new.setBehavior(VCard2_1)
        object.__setattr__(new, 'vcard21_issues', [])
        object.__setattr__(new, 'vcard21_lossy', {})
        return new

    def close_card(card, issues, lossy, line_no):
        object.__setattr__(card, 'vcard21_issues', list(issues))
        object.__setattr__(card, 'vcard21_lossy', dict(lossy))
        return VCard21ParseResult(card, list(issues), dict(lossy))

    for kind, payload in _assemble_blocks(_physical_lines(data)):
        if kind == 'stray':
            line_no, raw = payload
            issue = ParseIssue(
                'stray-bytes',
                "unparseable bytes outside of any property: {0!r}".format(
                    bytes(raw[:40])),
                line_no)
            _handle(issues, errors, issue)
            continue

        block = payload
        group, name, params, charset, encoding = _parse_header(
            block, issues, errors)

        if name == 'BEGIN':
            target = block.segments[0].data.decode('ascii', 'replace') \
                .strip().upper()
            if target == 'VCARD':
                if card is not None:
                    issue = ParseIssue(
                        'unterminated-card',
                        "BEGIN:VCARD before previous card was closed",
                        block.line)
                    _handle(issues, errors, issue)
                    yield close_card(card, issues, lossy, block.line)
                card = start_card(block.line)
                issues = []
                lossy = {}
            else:
                # Nested non-VCARD components are preserved as opaque
                # children on the current card.
                if card is None:
                    card = start_card(block.line)
                    issues = []
                    lossy = {}
                sub = Component(target)
                sub.group = group
                card.contents.setdefault(target.lower(), []).append(sub)
            continue

        if name == 'END':
            target = block.segments[0].data.decode('ascii', 'replace') \
                .strip().upper()
            if target != 'VCARD':
                continue
            if card is None:
                issue = ParseIssue(
                    'unexpected-end',
                    "END:VCARD without matching BEGIN:VCARD", block.line)
                _handle(issues, errors, issue)
                continue
            result = close_card(card, issues, lossy, block.line)
            card = None
            issues = []
            lossy = {}
            yield result
            continue

        if card is None:
            card = start_card(block.line)
            issues = []
            lossy = {}

        line = _build_property(block, issues, errors)
        line.group = group
        if not line.vcard21_lossless:
            lossy.setdefault(
                line.name,
                issues[-1] if issues else ParseIssue(
                    'bad-quoted-printable',
                    "lossy decoding", line.lineNumber, line.name))
        card.contents.setdefault(line.name.lower(), []).append(line)

    if card is not None:
        last_line = 0
        if data:
            last_line = data.count(b'\n') + data.count(b'\r') \
                - data.count(b'\r\n') + 1
        issue = ParseIssue(
            'unterminated-card',
            "VCARD was never closed by END:VCARD", max(last_line, 1))
        _handle(issues, errors, issue)
        yield close_card(card, issues, lossy, last_line)


def readVCard21(source, errors=STRICT):
    """Parse and return the first vCard 2.1 as :class:`VCard21ParseResult`."""
    return next(parseVCard21Stream(source, errors))


def readVCard21Components(source, errors=STRICT):
    """Parse binary data and return a list of all cards."""
    return list(parseVCard21Stream(source, errors))


# ---------------------------------------------------------------------------
# Upgrade to canonical vCard 3.0 UTF-8
# ---------------------------------------------------------------------------


def _upgrade_line(line):
    """Return a 3.0 ContentLine copy of a decoded 2.1 line."""
    new = ContentLine(line.name, [], None, group=line.group, encoded=False,
                      lineNumber=line.lineNumber)
    for key, values in line.params.items():
        new.params[key] = list(values)
    for bare in line.singletonparams:
        new.singletonparams.append(bare)
    value = line.value
    if isinstance(value, bytes):
        # binary value carried as canonical 3.0 base64 text; mark the line
        # encoded so VCardTextBehavior does not re-encode it.
        new.value = base64.b64encode(value).decode('ascii')
        new.encoded = True
        new.params['ENCODING'] = ['b']
    elif line.isNative:
        new.value = value
        new.isNative = True
        new.behavior = line.behavior
    else:
        new.value = value
        # Re-apply 3.0 text escaping (';' ',' '\' newlines) on serialize.
        new.behavior = _vcard.VCardTextBehavior
    return new


def upgradeToVCard30(result):
    """Return ``(component, lossy_fields)`` for one parsed 2.1 result.

    ``lossy_fields`` is a list of ``(property_name, issue)`` tuples for every
    property whose bytes could not be carried into 3.0 UTF-8 losslessly.
    Binary properties that decoded cleanly are upgraded losslessly.
    """
    card3 = Component('VCARD')
    card3.setBehavior(VCard3_0)

    version = ContentLine('VERSION', [], '3.0', encoded=False)
    card3.contents.setdefault('version', []).append(version)

    lossy_fields = []
    for child in result.card.getChildren():
        if child.name == 'VERSION':
            continue
        if child.name in result.lossy:
            lossy_fields.append((child.name, result.lossy[child.name]))
        upgraded = _upgrade_line(child)
        card3.contents.setdefault(upgraded.name.lower(), []).append(upgraded)

    card3.VCARD30_LOSSY = lossy_fields
    return card3, lossy_fields


def serializeVCard30(result):
    """Serialize one parse result as canonical vCard 3.0 UTF-8 bytes.

    Returns ``(data, lossy_fields)`` where ``data`` is :class:`bytes` folded
    at 75 UTF-8 bytes and ``lossy_fields`` lists properties that cannot be
    upgraded without information loss.
    """
    card3, lossy_fields = upgradeToVCard30(result)
    # 2.1 files frequently lack FN; serialization only needs to be a
    # faithful canonical rendering, not a validated 3.0 card.
    text = card3.serialize(validate=False)
    if isinstance(text, bytes):
        text = text.decode('utf-8')
    return text.encode('utf-8'), lossy_fields


def serializeVCard30All(source, errors=REPLACE):
    """Parse all cards from bytes/stream and return ``(b'\\r\\n'-joined,
    lossy)``.
    """
    chunks = []
    lossy = []
    for result in parseVCard21Stream(source, errors):
        data, fields = serializeVCard30(result)
        chunks.append(data)
        lossy.extend(fields)
    return b'\r\n'.join(chunks), lossy
