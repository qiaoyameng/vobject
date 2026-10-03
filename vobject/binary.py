# -*- coding: utf-8 -*-
"""
Streaming support for binary properties (PHOTO, LOGO, SOUND, ...).

Binary properties are normally carried as folded, base64 encoded content
lines.  Decoding them eagerly forces the whole payload (folded text *and*
raw bytes) into memory, which is wasteful for callers that only want to
stream the data into an object store or an upload.

This module defines two small, duck-typed protocols:

  BinarySink
      Receives the *decoded* bytes while a component is being parsed.

          sink.write(chunk)  # called zero or more times with bytes
          sink.finish()      # optional, called once if the whole property
                             # was read and validated successfully
          sink.abort()       # optional, called once if parsing failed or the
                             # consumer stopped iterating early

      ``finish`` and ``abort`` are mutually exclusive.  The sink object
      itself is handed back as the property's ``value``, so a single object
      can implement both the sink and source protocols (see
      :class:`BytesAttachment`).

  BinarySource
      Produces the raw bytes when a property is serialized.

          source.open()  -> binary file-like object (``read(n)`` only is
                            required; it does not have to be seekable)
          source.repeatable
                            True if ``open`` can be called more than once
          source.close()   optional; releases any resources owned by the
                            source itself

      ``vobject`` only closes the stream returned by ``open``; it never
      calls ``close`` on the source.  Closing a non-repeatable source is
      the caller's responsibility, and serializing it more than once
      raises :class:`BinaryError`.

Resource ownership summary
--------------------------
* Parsing: vobject creates nothing the caller has to close.  The sink is
  notified via ``abort``/``finish`` and decides what to do with its own
  resources (temporary files, multipart uploads, ...).
* Serialization: the stream returned by ``BinarySource.open`` is always
  closed by vobject (even on errors).  The source handle itself is owned
  and closed by the caller.
"""

from __future__ import absolute_import

import binascii


class BinaryError(Exception):
    """Raised when streaming base64 data is invalid or cannot be re-read."""

    def __init__(self, msg, lineNumber=None):
        super(BinaryError, self).__init__(msg)
        self.msg = msg
        if lineNumber is not None:
            self.lineNumber = lineNumber

    def __str__(self):
        if hasattr(self, 'lineNumber'):
            return "At line {0!s}: {1!s}".format(self.lineNumber, self.msg)
        return repr(self.msg)


# ---------------------------------------------------------------------------
# Reference sink / source implementations
# ---------------------------------------------------------------------------

class BytesAttachment(object):
    """
    In-memory attachment implementing both the sink and source protocols.

    Useful as the default attachment type and in tests.
    """

    repeatable = True

    def __init__(self, data=b'', params=None, name=None):
        self.data = bytes(data)
        self.params = params
        self.name = name
        self._done = False
        self._aborted = False

    def write(self, chunk):
        if self._done or self._aborted:
            raise BinaryError("attachment is no longer accepting data")
        if chunk:
            self.data += bytes(chunk)

    def finish(self):
        self._done = True
        return self.data

    def abort(self):
        self._aborted = True
        self.data = b''

    def open(self):
        if self._aborted:
            raise BinaryError("cannot open an aborted attachment")
        import io
        return io.BytesIO(self.data)

    def getvalue(self):
        return self.data

    def __len__(self):
        return len(self.data)

    def __eq__(self, other):
        if isinstance(other, BytesAttachment):
            return self.data == other.data
        if isinstance(other, (bytes, bytearray)):
            return self.data == bytes(other)
        return NotImplemented

    def __ne__(self, other):
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result

    def __repr__(self):
        return "<BytesAttachment: {0} bytes>".format(len(self.data))


class FileAttachment(object):
    """
    Attachment backed by a file path or a file-like object.

    As a *sink*, pass ``path`` (the file is created/truncated while
    parsing) and it is deleted on ``abort``.  As a *source*, pass either an
    existing ``stream`` or a ``path`` to read from.

    A path backed attachment is repeatable.  A stream backed attachment is
    only repeatable if ``repeatable=True`` is given explicitly; otherwise a
    second ``open``/sink use raises :class:`BinaryError`.
    """

    def __init__(self, path=None, stream=None, repeatable=None):
        if path is None and stream is None:
            raise BinaryError("FileAttachment requires a path or stream")
        self.path = path
        self._stream = stream
        self._sink_stream = None
        if repeatable is not None:
            self.repeatable = repeatable
        elif path is not None:
            self.repeatable = True
        else:
            self.repeatable = False
        self._opened = False
        self._aborted = False
        self._finished = False

    # -- sink protocol -----------------------------------------------------
    def write(self, chunk):
        if self._sink_stream is None:
            if self._aborted or self._finished:
                raise BinaryError("attachment is no longer accepting data")
            if self.path is not None:
                self._sink_stream = open(self.path, 'wb')
            elif self.repeatable or not self._opened:
                self._sink_stream = self._stream
            else:
                raise BinaryError("non-repeatable stream can only be used once")
        self._sink_stream.write(bytes(chunk))

    def finish(self):
        if self._sink_stream is not None and self.path is not None:
            self._sink_stream.close()
        self._sink_stream = None
        self._finished = True
        return self

    def abort(self):
        self._aborted = True
        try:
            if self._sink_stream is not None and self.path is not None:
                self._sink_stream.close()
        finally:
            self._sink_stream = None
            if self.path is not None:
                try:
                    import os
                    os.remove(self.path)
                except OSError:
                    pass

    # -- source protocol ---------------------------------------------------
    def open(self):
        if self._aborted:
            raise BinaryError("cannot open an aborted attachment")
        if self.path is not None:
            return open(self.path, 'rb')
        if not self.repeatable and self._opened:
            raise BinaryError("this attachment's stream is not repeatable")
        self._opened = True
        return self._stream

    def close(self):
        """Close a stream owned by the caller."""
        if self._stream is not None and hasattr(self._stream, 'close'):
            self._stream.close()


# ---------------------------------------------------------------------------
# Strict streaming base64 decoder (used while reading)
# ---------------------------------------------------------------------------

_B64_CHARS = frozenset(
    b'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
    b'abcdefghijklmnopqrstuvwxyz'
    b'0123456789+/'
)
_PAD = 0x3D  # ord('=')


class StrictBase64Decoder(object):
    """
    Incrementally decode folded base64 text with strict padding validation.

    Only the base64 alphabet (``A-Za-z0-9+/``) and ``=`` padding are legal;
    folding whitespace must be stripped by the caller before feeding a
    chunk. Padding may only consist of ``=`` or ``==`` as the final one or
    two characters, the total alphabet length must be a multiple of four,
    and nothing may follow the padding.

    Decoded bytes are handed to ``sink.write`` in small chunks, and the
    running decoded length is checked against ``max_size`` (``None`` means
    unlimited), so the size limit applies to the decoded data.
    """

    def __init__(self, sink, max_size=None, lineNumber=None):
        self.sink = sink
        self.max_size = max_size
        self.lineNumber = lineNumber
        self._carry = b''
        self._size = 0
        # True once a complete quad containing '=' padding was consumed.
        self._padded = False
        self._finished = False
        self._aborted = False

    @property
    def size(self):
        return self._size

    def feed(self, text):
        """Decode one chunk of unfolded base64 text (bytes or str)."""
        if self._finished or self._aborted:
            raise BinaryError("decoder is no longer accepting data",
                              self.lineNumber)
        if self._padded:
            self._fail("data found after base64 padding")
        try:
            if isinstance(text, str):
                data = text.encode('ascii')
            else:
                data = bytes(text)
        except UnicodeEncodeError:
            self._fail("non-ASCII byte in base64 data")
        if not data:
            return

        combined = self._carry + data

        bad = next((c for c in combined
                    if c not in _B64_CHARS and c != _PAD), None)
        if bad is not None:
            self._fail("illegal byte 0x{0:02x} in base64 data".format(bad))

        eq = combined.find(b'=')
        if eq != -1:
            if not combined.endswith(b'='):
                self._fail("'=' padding is only allowed at end of data")
            if any(c != _PAD for c in combined[eq + 1:]):
                self._fail("data found after base64 padding")
            if len(combined) - eq > 2:
                self._fail("invalid base64 padding length")

        # Consume whole quads; the 0-3 char tail is carried to the next
        # chunk. A tail that started the final padded quad (e.g. 'YQ='
        # waiting for '=') stays in the carry until it completes.
        usable = len(combined) - (len(combined) % 4)
        quads, self._carry = combined[:usable], combined[usable:]
        if quads:
            self._emit(self._decode_quads(quads))
            if eq != -1 and usable > eq:
                self._padded = True

    def complete(self):
        """
        End the property because the next physical line is not folded.

        The next physical line starts a new property, so no continuation
        line can ever complete a dangling quad: any carry (with or without
        padding) means the binary property was truncated.
        """
        if self._aborted:
            return
        if self._carry:
            if self._padded:
                self._fail("invalid base64 padding")
            self._fail(
                "base64 data length ({0}) is not a multiple of four"
                .format(self._size + len(self._carry)))
        self._finish_sink()

    def finish(self):
        """
        End the property at end of stream and validate it fully.

        No further chunk can ever arrive, so the total alphabet length must
        be a multiple of four and the trailing padding (if any) must be
        valid.
        """
        if self._aborted:
            return
        if self._carry:
            if self._padded:
                self._fail("invalid base64 padding")
            self._fail(
                "base64 data length ({0}) is not a multiple of four"
                .format(self._size + len(self._carry)))
        self._finish_sink()

    def _finish_sink(self):
        try:
            if hasattr(self.sink, 'finish'):
                self.sink.finish()
        except Exception:
            self.abort()
            raise
        self._finished = True

    def abort(self):
        """Tell the sink to discard everything delivered so far."""
        if self._aborted:
            return
        self._aborted = True
        sink_abort = getattr(self.sink, 'abort', None)
        if sink_abort is not None:
            sink_abort()

    def _decode_quads(self, quads):
        try:
            return binascii.a2b_base64(quads)
        except (binascii.Error, ValueError) as exc:
            self._fail(str(exc))

    def _emit(self, decoded):
        self._size += len(decoded)
        if self.max_size is not None and self._size > self.max_size:
            self._fail(
                "binary property exceeds maximum size of {0} decoded "
                "bytes".format(self.max_size))
        self.sink.write(decoded)

    def _fail(self, message):
        try:
            raise BinaryError(message, self.lineNumber)
        except BinaryError:
            self.abort()
            raise


# ---------------------------------------------------------------------------
# Streaming base64 encoder / folder (used while serializing)
# ---------------------------------------------------------------------------

def _b64_encode_chunk(chunk, remainder):
    """
    Base64 encode ``chunk`` carrying up to two raw bytes between chunks.

    Returns ``(encoded_bytes, new_remainder)``.  Only whole 3-byte groups
    are encoded; 1 or 2 trailing bytes become the new remainder. Padding is
    never emitted here; :func:`encode_remainder` finishes the stream.
    """
    data = remainder + bytes(chunk)
    usable = len(data) - (len(data) % 3)
    encoded = binascii.b2a_base64(data[:usable]).rstrip(b'\r\n')
    return encoded, data[usable:]


def encode_remainder(remainder):
    """Base64 encode the final 0, 1 or 2 bytes, with correct ``=`` padding."""
    if not remainder:
        return b''
    return binascii.b2a_base64(remainder).rstrip(b'\r\n')


def stream_base64(source, write, chunk_size=8192, line_length=75,
                  prefix_length=0):
    """
    Read a binary source and write folded base64 text incrementally.

    ``source`` is a binary file-like object (only ``read(n)`` is needed,
    and short reads must be handled gracefully; it need not be seekable).
    ``write`` receives text chunks. The first logical line already has
    ``prefix_length`` characters on it (the property name and parameters),
    continuation lines start with a single space, and every physical line
    (including the last) is terminated by CRLF, per RFC 2445/RFC 6350
    folding.

    The up-to-two-byte remainder is carried across chunks, so arbitrary
    source chunk boundaries never corrupt the encoding or the final
    padding.
    """
    if line_length is None or int(line_length) <= 0:
        line_length = 75
    line_length = int(line_length)

    remainder = b''

    # Stateful folding: the first physical line shares its budget with the
    # already-written property prefix, continuation lines get a fresh
    # line_length - 1 budget (one char is the folding space).
    state = {'budget': max(line_length - prefix_length, 1)}

    def emit(encoded):
        view = encoded
        while view:
            take = min(state['budget'], len(view))
            write(view[:take].decode('ascii'))
            view = view[take:]
            state['budget'] -= take
            if state['budget'] == 0 and view:
                write("\r\n ")
                state['budget'] = line_length - 1

    while True:
        raw = source.read(chunk_size)
        if not raw:
            break
        encoded, remainder = _b64_encode_chunk(raw, remainder)
        if encoded:
            emit(encoded)

    tail = encode_remainder(remainder)
    if tail:
        emit(tail)
    write("\r\n")


def is_binary_source(value):
    """Return True if value looks like a BinarySource (open + repeatable)."""
    return (hasattr(value, 'open') and callable(value.open)
            and hasattr(value, 'repeatable'))
