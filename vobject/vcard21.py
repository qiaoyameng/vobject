"""Byte-level import path for legacy vCard 2.1 streams.

vCard 2.1 files exported by old phones are byte streams, not text: each
property may carry its own CHARSET parameter (GBK, BIG5, ...), values may
be QUOTED-PRINTABLE encoded, and unencoded ASCII properties are mixed in
freely.  Decoding the whole file to a string before parsing destroys
exactly the information needed to decode those properties, so this module
parses the raw bytes directly and only decodes each property value with
the charset declared on that property.

Line assembly distinguishes three different continuation mechanisms:

* quoted-printable soft breaks: a trailing "=" at the end of a physical
  line of a property declared ENCODING=QUOTED-PRINTABLE.  The soft break
  is removed before decoding, so a break may even land in the middle of
  a "=XX" escape,
* vCard line folding: a physical line starting with SPACE or TAB
  continues the previous line (the whitespace itself is removed),
* binary block continuation: the base64 body of a property declared
  ENCODING=B / ENCODING=BASE64 spans physical lines until a blank line.

Structured values (N, ADR, ORG) are split into fields on raw ";" bytes
*before* quoted-printable and charset decoding, so an encoded "=3B"
semicolon is treated as data, not as a field separator.

Decoding problems (unknown charsets, broken quoted-printable escapes,
undecodable bytes, malformed lines) are reported with the physical line
number where they occur.  With errors='strict' the first problem raises
vobject.base.ParseError; with errors='replace' the problem is recorded in
a VCard21Report and a replacement is used so parsing can continue.

Typical usage::

    from vobject.vcard21 import VCard21Reader, serializeTo3_0

    reader = VCard21Reader(open('contacts.vcf', 'rb'), errors='replace')
    for card in reader:
        utf8_bytes = serializeTo3_0(card)  # see serializeTo3_0
    for issue in reader.report.lossy_fields:
        print('cannot upgrade losslessly:', issue)
"""

from __future__ import absolute_import

import codecs
import quopri
import re

from .base import (Component, ContentLine, ParseError, Stack, backslashEscape,
                   getBehavior)
from .vcard import VCard3_0

# Properties whose values are structured (";"-separated fields).  The
# field separators must be interpreted on the raw (still encoded) bytes.
STRUCTURED_NAMES = frozenset(['N', 'ADR', 'ORG'])

_BASE64_LINE = re.compile(rb'^[A-Za-z0-9+/=\t ]*$')
_HEX_ESCAPE = re.compile(rb'=[0-9A-Fa-f]{2}')
_WHITESPACE = re.compile(rb'\s+')
# A line that itself starts a content line (NAME or NAME;PARAMS:...).
# Used to refuse quoted-printable soft breaks that would swallow the
# next property (or END:) when a value's final line ends in "=".
_CONTENT_LINE_START = re.compile(
    rb'^[A-Za-z0-9_\-]+(\.[A-Za-z0-9_\-]+)?(;[^\r\n:]*)?:')


# ------------------------------- reporting -----------------------------------

class VCard21Issue(object):
    """One problem found while importing a vCard 2.1 byte stream.

    @ivar line_number: 1-based physical line number in the input stream.
    @ivar property_name: name of the property being parsed, if known.
    @ivar card_index: 0-based index of the top-level component being parsed.
    @ivar message: human readable description of the problem.
    @ivar lossy: True if the field cannot be upgraded to vCard 3.0 without
        data loss (replacement characters, unknown charset, broken escape,
        skipped line).
    """
    def __init__(self, message, line_number=None, property_name=None,
                 card_index=None, lossy=False):
        self.message = message
        self.line_number = line_number
        self.property_name = property_name
        self.card_index = card_index
        self.lossy = lossy

    def __repr__(self):
        return ("<VCard21Issue card={0!s} line={1!s} {2!s}: {3!s}>"
                .format(self.card_index, self.line_number,
                        self.property_name, self.message))


class VCard21Report(object):
    """Collects VCard21Issues found while parsing with errors='replace'."""
    def __init__(self):
        self.issues = []

    def add(self, issue):
        self.issues.append(issue)

    @property
    def ok(self):
        """True if no problems were found at all."""
        return not self.issues

    @property
    def lossy_fields(self):
        """Issues for fields that cannot be losslessly upgraded to 3.0."""
        return [issue for issue in self.issues if issue.lossy]

    def __len__(self):
        return len(self.issues)


# ------------------------------- byte input ----------------------------------

def _read_all_bytes(stream_or_bytes):
    """Return the raw bytes of the input, or raise TypeError for text."""
    if isinstance(stream_or_bytes, (bytes, bytearray)):
        return bytes(stream_or_bytes)
    read = getattr(stream_or_bytes, 'read', None)
    if read is not None:
        data = read()
        if isinstance(data, (bytes, bytearray)):
            return bytes(data)
    raise TypeError(
        "vCard 2.1 import requires bytes or a binary stream; do not decode "
        "the file to text first (open it in 'rb' mode)")


def _split_physical_lines(data):
    """Split raw bytes into physical lines; line number == index + 1."""
    return re.split(rb'\r\n|\r|\n', data)


# --------------------------- header (name/params) ----------------------------

def _split_unquoted(data, sep):
    """Split bytes on sep, ignoring separators inside double quotes."""
    parts = []
    buf = []
    in_quotes = False
    for i in range(len(data)):
        char = data[i:i + 1]
        if char == b'"':
            in_quotes = not in_quotes
        elif char == sep and not in_quotes:
            parts.append(b''.join(buf))
            buf = []
            continue
        buf.append(char)
    parts.append(b''.join(buf))
    return parts


def _find_value_colon(line):
    """Index of the first ':' outside quotes, or -1."""
    in_quotes = False
    for i in range(len(line)):
        char = line[i:i + 1]
        if char == b'"':
            in_quotes = not in_quotes
        elif char == b':' and not in_quotes:
            return i
    return -1


def _parse_header(header):
    """Parse header bytes into (group, name, params, singletons).

    params is a list of (name, [value, ...]) tuples, singletons a list of
    bare parameter tokens (vCard 2.1 allows values without a name, e.g.
    "TEL;HOME;VOICE" or a bare "QUOTED-PRINTABLE").  All values are bytes.
    """
    tokens = _split_unquoted(header, b';')
    first = tokens[0].strip()
    group = None
    if b'.' in first:
        group, first = first.split(b'.', 1)
    params = []
    singletons = []
    for token in tokens[1:]:
        token = token.strip()
        if not token:
            continue
        if b'=' in token:
            pname, pvalues = token.split(b'=', 1)
            values = []
            for value in _split_unquoted(pvalues, b','):
                value = value.strip()
                if len(value) >= 2 and value[:1] == b'"' \
                        and value[-1:] == b'"':
                    value = value[1:-1]
                values.append(value)
            params.append((pname.strip(), values))
        else:
            singletons.append(token)
    return group, first, params, singletons


def _encoding_kind(params, singletons):
    """Return 'qp', 'binary' or None based on ENCODING params/singletons."""
    tokens = []
    for pname, values in params:
        if pname.upper() == b'ENCODING':
            tokens.extend(v.upper() for v in values)
    tokens.extend(s.upper() for s in singletons)
    if b'QUOTED-PRINTABLE' in tokens:
        return 'qp'
    if b'B' in tokens or b'BASE64' in tokens:
        return 'binary'
    return None


# ----------------------------- logical lines ---------------------------------

class _LogicalLine(object):
    """A logical line: a list of (physical_line_number, bytes) segments."""
    def __init__(self):
        self.segments = []

    def append(self, line_number, data):
        self.segments.append([line_number, data])

    @property
    def start_line(self):
        return self.segments[0][0]

    def joined(self):
        return b''.join(segment for _, segment in self.segments)

    def line_for_offset(self, offset):
        """Physical line number containing the given joined offset."""
        for line_number, segment in self.segments:
            if offset < len(segment):
                return line_number
            offset -= len(segment)
        return self.segments[-1][0]


def _iter_logical_lines(physical_lines):
    """Assemble physical lines into logical lines.

    Handles, in order of precedence: binary (base64) blocks running until
    a blank line, quoted-printable soft breaks (trailing "="), and vCard
    folding (leading whitespace).
    """
    count = len(physical_lines)
    index = 0
    while index < count:
        raw = physical_lines[index]
        if raw.strip() == b'':
            index += 1
            continue
        logical = _LogicalLine()
        logical.append(index + 1, raw)
        kind = None
        colon = _find_value_colon(raw)
        if colon >= 0:
            _, _, params, singletons = _parse_header(raw[:colon])
            kind = _encoding_kind(params, singletons)
        if kind == 'binary':
            # base64 body: continuation lines are not marked; the block
            # runs until a blank line (or a line that cannot be base64).
            index += 1
            while index < count:
                candidate = physical_lines[index]
                if candidate.strip() == b'' or \
                        not _BASE64_LINE.match(candidate):
                    break
                logical.append(index + 1, candidate.strip())
                index += 1
        else:
            while index + 1 < count:
                _, current = logical.segments[-1]
                following = physical_lines[index + 1]
                if kind == 'qp' and current.endswith(b'=') and \
                        not _CONTENT_LINE_START.match(following):
                    # quoted-printable soft break: drop the "=" and glue
                    # the next physical line on directly; this also heals
                    # breaks that land in the middle of a "=XX" escape.
                    logical.segments[-1][1] = current[:-1]
                    index += 1
                    logical.append(index + 1, physical_lines[index])
                elif following[:1] in (b' ', b'\t'):
                    # vCard folding: whitespace prefix marks continuation.
                    index += 1
                    logical.append(index + 1, following[1:])
                else:
                    break
            index += 1
        yield logical


# ------------------------------ value decoding -------------------------------

class _ProblemHandler(object):
    """Apply the strict/replace policy to one import."""
    def __init__(self, errors, report):
        self.errors = errors
        self.report = report

    def problem(self, message, line_number, property_name=None,
                card_index=None, lossy=False):
        if self.errors == 'strict':
            raise ParseError(message, line_number)
        self.report.add(VCard21Issue(message, line_number, property_name,
                                     card_index, lossy))


def _decode_bytes(raw, charset, handler, line_number, property_name,
                  card_index):
    """Decode bytes with charset, honoring the strict/replace policy."""
    try:
        return raw.decode(charset)
    except UnicodeDecodeError as error:
        handler.problem(
            "Cannot decode {0} value with charset {1}: {2}".format(
                property_name, charset, error),
            line_number, property_name, card_index, lossy=True)
        return raw.decode(charset, errors='replace')


def _resolve_charset(charset_param, default_charset, handler, line_number,
                     property_name, card_index):
    """Return a usable charset name, reporting unknown charsets."""
    if charset_param is None:
        return default_charset
    try:
        return codecs.lookup(charset_param).name
    except (LookupError, TypeError):
        handler.problem(
            "Unknown charset {0!r} on property {1}".format(charset_param,
                                                           property_name),
            line_number, property_name, card_index, lossy=True)
        return default_charset


def _check_qp_escapes(value, value_offset, logical, handler, property_name,
                      card_index):
    """Report every '=' that is not a valid =XX escape (soft breaks are
    already removed, so any remaining bare '=' is broken)."""
    position = 0
    while True:
        position = value.find(b'=', position)
        if position < 0:
            return
        if _HEX_ESCAPE.match(value, position):
            position += 3
            continue
        line_number = logical.line_for_offset(value_offset + position)
        handler.problem(
            "Invalid quoted-printable escape in {0}: {1!r}".format(
                property_name, value[position:position + 3]),
            line_number, property_name, card_index, lossy=True)
        position += 1


def _build_content_line(logical, handler, default_charset, card_index):
    """Turn a logical line into a decoded ContentLine (or None to skip)."""
    joined = logical.joined()
    start_line = logical.start_line
    colon = _find_value_colon(joined)
    if colon < 0:
        handler.problem("Failed to parse line: {0!r}".format(joined),
                        start_line, card_index=card_index, lossy=True)
        return None
    header, value = joined[:colon], joined[colon + 1:]

    group_b, name_b, params_b, singletons_b = _parse_header(header)
    kind = _encoding_kind(params_b, singletons_b)

    def decode_token(raw):
        try:
            return raw.decode('ascii')
        except UnicodeDecodeError:
            handler.problem(
                "Non-ASCII bytes in property header: {0!r}".format(raw),
                start_line, card_index=card_index, lossy=True)
            return raw.decode('ascii', errors='replace')

    name = decode_token(name_b).upper().replace('_', '-')
    group = decode_token(group_b) if group_b is not None else None
    params = []
    for pname, values in params_b:
        params.append([decode_token(pname).upper()] +
                      [decode_token(pvalue) for pvalue in values])
    singletons = [decode_token(token).upper() for token in singletons_b]

    # CHARSET applies only to this property's value.
    charset_param = None
    kept_params = []
    for param in params:
        if param[0] == 'CHARSET':
            if len(param) > 1 and charset_param is None:
                charset_param = param[1]
        elif param[0] == 'ENCODING':
            remaining = [v for v in param[1:]
                         if v.upper() not in ('QUOTED-PRINTABLE', 'B',
                                              'BASE64')]
            if remaining:
                kept_params.append(['ENCODING'] + remaining)
        else:
            kept_params.append(param)
    singletons = [s for s in singletons
                  if s not in ('QUOTED-PRINTABLE', 'B', 'BASE64')]

    if kind == 'binary':
        # base64 body; continuation lines were glued without separators.
        payload = _WHITESPACE.sub(b'', value)
        try:
            decoded = codecs.decode(payload, 'base64')
        except Exception as error:
            handler.problem(
                "Invalid base64 data in {0}: {1}".format(name, error),
                start_line, name, card_index, lossy=True)
            decoded = payload
        line = ContentLine(name, [], decoded, group=group, encoded=False,
                           lineNumber=start_line)
        # vCard 3.0 spells the binary encoding "b".
        kept_params.append(['ENCODING', 'b'])
    else:
        charset = _resolve_charset(charset_param, default_charset, handler,
                                   start_line, name, card_index)

        def decode_field(raw, offset):
            if kind == 'qp':
                _check_qp_escapes(raw, offset, logical, handler, name,
                                  card_index)
                raw = quopri.decodestring(raw)
            return _decode_bytes(raw, charset, handler, start_line, name,
                                 card_index)

        if name in STRUCTURED_NAMES:
            # Split structured values on raw ";" *before* decoding, so an
            # encoded "=3B" semicolon stays data instead of becoming a
            # field separator.  Re-join with backslash escaping so the
            # standard behaviors recover exactly these fields.
            raw_fields = value.split(b';')
            fields = []
            offset = colon + 1
            for raw_field in raw_fields:
                fields.append(decode_field(raw_field, offset))
                offset += len(raw_field) + 1
            text_value = ';'.join(backslashEscape(field)
                                  for field in fields)
        else:
            text_value = decode_field(value, colon + 1)
        line = ContentLine(name, [], text_value, group=group, encoded=False,
                           lineNumber=start_line)

    # vCard 2.1 bare parameter tokens (TEL;HOME;VOICE) become 3.0 TYPEs.
    if singletons:
        for param in kept_params:
            if param[0] == 'TYPE':
                param.extend(singletons)
                break
        else:
            kept_params.append(['TYPE'] + singletons)

    line.params = {}
    for param in kept_params:
        line.params.setdefault(param[0], []).extend(param[1:])
    line.singletonparams = []
    return line


# ------------------------------ stream parsing -------------------------------

class VCard21Reader(object):
    """Iterate over the vCards in a vCard 2.1 byte stream.

    @param stream_or_bytes: raw bytes, or a binary file-like object.
    @param errors: 'strict' to raise ParseError on the first problem, or
        'replace' to substitute problem data and record issues instead.
    @param default_charset: charset for property values that declare no
        CHARSET parameter of their own (vCard 2.1 defaults to ASCII).
    @ivar report: a VCard21Report with every issue found (replace mode).
    """
    def __init__(self, stream_or_bytes, errors='strict',
                 default_charset='ascii'):
        if errors not in ('strict', 'replace'):
            raise ValueError("errors must be 'strict' or 'replace'")
        self.data = _read_all_bytes(stream_or_bytes)
        self.errors = errors
        self.default_charset = default_charset
        self.report = VCard21Report()

    def __iter__(self):
        return self._generate()

    def _generate(self):
        handler = _ProblemHandler(self.errors, self.report)
        physical_lines = _split_physical_lines(self.data)
        stack = Stack()
        card_index = 0
        last_line = 0
        for logical in _iter_logical_lines(physical_lines):
            last_line = logical.start_line
            line = _build_content_line(logical, handler,
                                       self.default_charset, card_index)
            if line is None:
                continue
            if line.name == 'BEGIN':
                stack.push(Component(line.value, group=line.group))
            elif line.name == 'END':
                if len(stack) == 0:
                    raise ParseError(
                        "Attempted to end the {0} component but it was "
                        "never opened".format(line.value), last_line)
                if line.value.upper() != stack.topName():
                    raise ParseError(
                        "{0} component wasn't closed".format(
                            stack.topName()), last_line)
                if len(stack) > 1:
                    stack.modifyTop(stack.pop())
                    continue
                component = stack.pop()
                behavior = None
                version = component.contents.get('version')
                if version:
                    behavior = getBehavior(component.name,
                                           version[0].value)
                if behavior is None:
                    # vCard 2.1 has no registered behavior; upgrade to the
                    # default (3.0) behavior for the component's children.
                    behavior = getBehavior(component.name)
                if behavior is not None:
                    component.setBehavior(behavior)
                component.transformChildrenToNative()
                yield component
                card_index += 1
            else:
                stack.modifyTop(line)
        if stack.top():
            if stack.top().useBegin:
                raise ParseError(
                    "Component {0!s} was never closed".format(
                        stack.topName()), last_line)
            component = stack.pop()
            if component.name:
                behavior = getBehavior(component.name)
                if behavior is not None:
                    component.setBehavior(behavior)
            component.transformChildrenToNative()
            yield component


def readComponents21(stream_or_bytes, errors='strict',
                     default_charset='ascii', report=None):
    """Yield one Component at a time from a vCard 2.1 byte stream.

    Like vobject.base.readComponents, but takes raw bytes instead of text
    and decodes each property with its own CHARSET.  Pass a VCard21Report
    as report to collect issues when using errors='replace'; for more
    control use VCard21Reader directly.
    """
    reader = VCard21Reader(stream_or_bytes, errors=errors,
                           default_charset=default_charset)
    if report is not None:
        reader.report = report
    return iter(reader)


def serializeTo3_0(component):
    """Serialize an imported component as canonical vCard 3.0 UTF-8 bytes.

    The VERSION line is rewritten to 3.0; values are serialized as UTF-8
    with vCard 3.0 escaping and line folding.  Fields that could not be
    decoded losslessly are listed in the reader's
    VCard21Report.lossy_fields.
    """
    version_lines = component.contents.get('version')
    saved = None
    if version_lines:
        saved = version_lines[0].value
        version_lines[0].value = VCard3_0.versionString
    try:
        text = component.serialize(validate=False)
    finally:
        if saved is not None:
            version_lines[0].value = saved
    return text.encode('utf-8')
