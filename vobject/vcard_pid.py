"""
Property-level reconciliation for vCard 4.0 cards sharing a UID, based on the
PID parameter and the CLIENTPIDMAP property (RFC 6350, sections 6.2.7 and 7.7).

Two devices frequently add their own properties to the same contact.  The
digits used in PID values are *local*: a PID of "1" only makes sense after it
has been resolved through that card's CLIENTPIDMAP into an absolute client
URI.  Merging "by property name" or by comparing the raw digits inside PID is
therefore wrong.  This module:

1. resolves every local PID number to a source URI through CLIENTPIDMAP;
2. compares *full* property identities ``(origin, source, discriminator)``,
   so equal numbers from different sources stay distinct and different
   numbers on the same source stay distinct;
3. applies an explicit conflict policy to genuine same-identity clashes;
4. emits a fresh card with CLIENTPIDMAP numbers reallocated contiguously and
   every PID reference rewritten consistently;
5. preserves ALTID alternatives and property groups as atomic units;
6. never mutates its inputs and is idempotent -- re-merging the produced card
   with either original does not keep growing duplicates.

The public entry point is :func:`reconcile`.
"""

from __future__ import unicode_literals

import hashlib
import re
from collections import OrderedDict

# ---------------------------------------------------------------------------
# Conflict policies
# ---------------------------------------------------------------------------

LOCAL_WINS = "local-wins"
REMOTE_WINS = "remote-wins"
KEEP_BOTH = "keep-both"
CONFLICT_POLICIES = (LOCAL_WINS, REMOTE_WINS, KEEP_BOTH)

# Identity origins.
ORIGIN_MAPPED = "mapped"     # resolved numeric PID via a real CLIENTPIDMAP
ORIGIN_UNMAPPED = "unmapped"  # numeric PID with no matching CLIENTPIDMAP
ORIGIN_URI = "uri"           # PID token already an absolute URI
ORIGIN_ANON = "anon"         # synthetic identity for a PID-less property

_CLIENT_ORIGINS = frozenset((ORIGIN_MAPPED, ORIGIN_UNMAPPED, ORIGIN_ANON))

_ANON_SCHEME = "urn:x-vobject-recon:anon"
_UNMAPPED_SCHEME = "urn:x-vobject-recon:unmapped"


class ReconcileError(ValueError):
    """Raised when the two cards cannot be reconciled."""


# ---------------------------------------------------------------------------
# Parsing (RFC 6350)
# ---------------------------------------------------------------------------

_NAME_RE = re.compile(r"^(?P<group>[A-Za-z0-9-]+\.)?(?P<name>[A-Za-z0-9*-]+)")
_NUMERIC_TOKEN_RE = re.compile(
    r"^(?P<client>\d+)(?:\.(?P<prop>[0-9A-Za-z-]+))?$")
_URI_TOKEN_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:.+$")
_DIGITS_RE = re.compile(r"^\d+$")


def _unfold(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    out = []
    for line in text.split("\n"):
        if line and line[0] in (" ", "\t") and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


def _split_quoted(s, sep):
    """Split on *sep*, ignoring separators that are quoted or backslash-escaped."""
    parts, buf = [], []
    quoted = False
    escaped = False
    for ch in s:
        if escaped:
            buf.append(ch)
            escaped = False
            continue
        if ch == "\\":
            buf.append(ch)
            escaped = True
            continue
        if ch == '"':
            quoted = not quoted
            buf.append(ch)
        elif ch == sep and not quoted:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return parts


_PARAM_NAME_RE = re.compile(r"[A-Za-z0-9-]+")
_SCHEME_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")
# When a PID carries an unquoted URI and the property value is itself a URI,
# the two URIs are only separable by recognizing where the value starts.  RFC
# 6350 mandates quoting here; this anchor list is the best-effort fallback for
# unquoted real-world cards.
_VALUE_URI_ANCHOR_RE = re.compile(
    r":(?:tel|mailto|http|https|urn|ftp|ldap|sip|sips|xmpp|geo):")


def _strip_quote(value):
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1]
    return value


def split_content_line(rest, line):
    """Split the part of a content line after its name.

    *rest* is either ``:value`` or ``;params:value``.  Returns
    ``(params, raw_value)``.  Quoted parameter values and unquoted URI-shaped
    PID values are handled (the latter should be quoted per RFC 6350, but
    unquoted forms are accepted).
    """
    if rest.startswith(":"):
        return OrderedDict(), rest[1:]
    if not rest.startswith(";"):
        raise ReconcileError("Malformed content line: {0!r}".format(line))
    section = rest[1:]
    value_colon = _find_value_colon(section)
    if value_colon == -1:
        raise ReconcileError("No value colon: {0!r}".format(line))
    return _parse_params(section[:value_colon]), section[value_colon + 1:]


def _find_value_colon(section):
    i, n = 0, len(section)
    while i < n:
        ch = section[i]
        if ch == '"':
            i += 1
            while i < n and section[i] != '"':
                i += 1
            i += 1
            continue
        if ch == ":":
            return i
        if ch in ";,":
            i += 1
            continue
        match = _PARAM_NAME_RE.match(section, i)
        if match is None:
            i += 1
            continue
        name = match.group(0).upper()
        j = match.end()
        if j < n and section[j] == "=":
            j += 1
            while True:
                j = _skip_param_value(section, j, name)
                if j < n and section[j] == ",":
                    j += 1
                    continue
                break
        i = j
    return -1


def _skip_param_value(section, j, name):
    n = len(section)
    if j < n and section[j] == '"':
        j += 1
        while j < n and section[j] != '"':
            j += 1
        return min(j + 1, n)
    if name == "PID" and _SCHEME_RE.match(section, j):
        k = j
        while k < n:
            if section[k] == ";" and                     re.match(r"[A-Za-z0-9-]+=", section[k + 1:]):
                return k
            k += 1
        anchors = [m.start() for m in
                   _VALUE_URI_ANCHOR_RE.finditer(section, j)]
        if anchors:
            return anchors[0]
        last = section.rfind(":")
        return last if last >= j else n
    while j < n and section[j] not in ";:,":
        j += 1
    return j


def _parse_params(header):
    params = OrderedDict()
    chunks, buf, quoted = [], [], False
    for ch in header:
        if ch == '"':
            quoted = not quoted
            buf.append(ch)
        elif ch == ";" and not quoted:
            chunks.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    chunks.append("".join(buf))
    for chunk in chunks:
        if not chunk:
            continue
        if "=" not in chunk:
            params.setdefault(chunk.upper(), [])
            continue
        name, _, raw = chunk.partition("=")
        values, piece, quoted = [], [], False

        def push():
            token = "".join(piece)
            if token:
                values.append(_strip_quote(token))

        for ch in raw:
            if ch == '"':
                quoted = not quoted
                piece.append(ch)
            elif ch == "," and not quoted:
                push()
                piece = []
            else:
                piece.append(ch)
        push()
        params.setdefault(name.upper(), []).extend(values)
    return params


def _unescape_text(token):
    out, i = [], 0
    while i < len(token):
        ch = token[i]
        if ch == "\\" and i + 1 < len(token):
            nxt = token[i + 1]
            out.append({"n": "\n", "N": "\n", "\\": "\\", ",": ",",
                        ";": ";"}.get(nxt, nxt))
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _parse_components(raw, value_type):
    # Components are stored *decoded* (a literal backslash/comma/semicolon or
    # newline); escaping is applied once, at serialization time, which keeps
    # parse -> serialize -> parse a fixed point.
    if value_type == "URI":
        return [[raw]]
    comps = _split_quoted(raw, ";")
    return [[_unescape_text(v) for v in _split_quoted(c, ",")]
            for c in comps]


class Property(object):
    """One vCard content line (a data property, never a scaffold line)."""

    __slots__ = ("group", "name", "params", "raw_value", "value_type",
                 "components", "order")

    def __init__(self, group, name, params, raw_value, order):
        self.group = group
        self.name = name.upper()
        self.params = params
        self.raw_value = raw_value
        if "VALUE" in params:
            self.value_type = params["VALUE"][0].upper()
        else:
            self.value_type = "TEXT"
        self.components = _parse_components(raw_value, self.value_type)
        self.order = order

    def clone(self):
        params = OrderedDict((k, list(v)) for k, v in self.params.items())
        return Property(self.group, self.name, params, self.raw_value,
                        self.order)


def parse_vcard(text):
    """Parse a VCARD into ``(uid, pidmaps, properties)``.

    *pidmaps* is an OrderedDict of local id -> source URI.  Duplicate or
    malformed CLIENTPIDMAP lines raise :class:`ReconcileError`.
    """
    uid = None
    pidmaps = OrderedDict()
    properties = []
    inside = False
    order = 0
    for line in _unfold(text):
        if not line.strip():
            continue
        m = _NAME_RE.match(line)
        if not m:
            raise ReconcileError("Unparseable vCard line: {0!r}".format(line))
        rest = line[m.end():]
        group = m.group("group")
        group = group[:-1] if group else None
        name = m.group("name").upper()

        if name == "BEGIN":
            inside = True
            continue
        if name == "END":
            continue
        if not inside:
            continue

        params, raw_value = split_content_line(rest, line)

        if name == "VERSION":
            continue
        if name == "UID":
            uid = raw_value
            continue
        if name == "CLIENTPIDMAP":
            local, uri = _parse_clientpidmap(raw_value, line)
            if local in pidmaps and pidmaps[local] != uri:
                raise ReconcileError(
                    "Conflicting CLIENTPIDMAP for local id {0}".format(local))
            pidmaps[local] = uri
            continue

        properties.append(Property(group, name, params, raw_value, order))
        order += 1

    if not inside:
        raise ReconcileError("Input does not contain a VCARD component")
    return uid, pidmaps, properties


def _parse_clientpidmap(raw_value, line):
    parts = _split_quoted(raw_value, ";")
    if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
        raise ReconcileError(
            "Malformed CLIENTPIDMAP line: {0!r}".format(line))
    token, uri = parts[0].strip(), parts[1].strip()
    if not _DIGITS_RE.match(token):
        raise ReconcileError(
            "Non-numeric CLIENTPIDMAP local id: {0!r}".format(line))
    return int(token), uri


# ---------------------------------------------------------------------------
# Serialization (RFC 6350)
# ---------------------------------------------------------------------------

def _escape_text(value):
    return (value.replace("\\", "\\\\").replace(",", "\\,")
            .replace(";", "\\;").replace("\n", "\\n"))


def _encode_value(prop):
    if prop.value_type == "URI":
        return prop.raw_value
    return ";".join(",".join(_escape_text(v) for v in comp)
                    for comp in prop.components)


_PARAM_SPECIAL_CHARS = re.compile(r'[",;:\n]')


def _encode_params(params):
    chunks = []
    for name, values in params.items():
        if not values:
            chunks.append(name)
            continue
        if name == "PID":
            # RFC 6350: the whole PID param is quoted iff any value lists more
            # than one pid (i.e. contains a comma).  Colons/semicolons are
            # legal inside a pid value (URI pids) and must not trigger quoting.
            pid_value = ",".join(values)
            if "," in pid_value:
                pid_value = '"' + pid_value + '"'
            chunks.append("PID=" + pid_value)
            continue
        encoded = []
        for val in values:
            if val == "" or _PARAM_SPECIAL_CHARS.search(val):
                val = '"' + val.replace('"', "'") + '"'
            encoded.append(val)
        chunks.append("{0}={1}".format(name, ",".join(encoded)))
    return "".join(";" + c for c in chunks)


def _fold(line):
    data = line.encode("utf-8")
    if len(data) <= 75:
        return [line]
    out = [data[:75].decode("utf-8", "ignore")]
    rest = data[75:]
    while rest:
        out.append(" " + rest[:74].decode("utf-8", "ignore"))
        rest = rest[74:]
    return out


def serialize_property(prop):
    prefix = (prop.group + ".") if prop.group else ""
    line = "{0}{1}{2}:{3}".format(prefix, prop.name,
                                  _encode_params(prop.params),
                                  _encode_value(prop))
    return "\r\n".join(_fold(line))


def serialize_vcard(properties, uid, clientpidmaps):
    lines = ["BEGIN:VCARD", "VERSION:4.0"]
    if uid is not None:
        lines.append("UID:" + uid)
    for local_id, uri in clientpidmaps:
        lines.extend(_fold("CLIENTPIDMAP:{0};{1}".format(local_id, uri)))
    for prop in properties:
        lines.append(serialize_property(prop))
    lines.append("END:VCARD")
    return "\r\n".join(lines) + "\r\n"


# ---------------------------------------------------------------------------
# Identity / fingerprint
# ---------------------------------------------------------------------------

# Parameters that carry bookkeeping rather than the property value.  PID
# carries property identity, ALTID alternative-group membership and PREF is a
# display hint; none of them change what the value *is*, so they are excluded
# from the content fingerprint (and therefore from exact-equality tests).
_NON_CONTENT_PARAMS = frozenset(("PID", "ALTID", "PREF"))


def parse_pid_param(params):
    tokens = []
    for value in params.get("PID", []):
        # A PID parameter value may itself be a quoted list of comma-separated
        # pids; commas inside a quoted element (e.g. a URI) are not separators.
        buf, quoted, elem = [], None, []
        for ch in value:
            if ch == '"':
                quoted = not quoted
                continue
            if ch == "," and not quoted:
                tokens.append("".join(buf).strip())
                buf = []
                continue
            buf.append(ch)
        tokens.append("".join(buf).strip())
    return [t for t in tokens if t]


def content_fingerprint(prop):
    """Hash of everything that makes a property a distinct *value*.

    PID/ALTID parameters and the group prefix are bookkeeping, not content,
    so they are excluded.  Name, value type, structured value and every other
    parameter participate -- there is no fuzzy matching.
    """
    parts = ["name=" + prop.name, "vtype=" + prop.value_type]
    for pname in sorted(prop.params):
        if pname in _NON_CONTENT_PARAMS:
            continue
        parts.append("{0}={1}".format(pname,
                                      ",".join(sorted(prop.params[pname]))))
    parts.append("value=" + _encode_value(prop))
    return hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()


def _unmapped_source(uid, local):
    # Content-independent and side-independent on purpose: a numeric PID with
    # no CLIENTPIDMAP carries no source information at all, so the only stable
    # handle is (UID, local number).  This makes the synthetic source round
    # trip through the emitted CLIENTPIDMAP, which keeps re-merging the
    # produced card a fixed point.  Two genuinely different devices on the
    # same UID both using an unmapped local number are indistinguishable and
    # are therefore treated as one synthetic source (reported as a warning).
    return "{scheme}:{uid}?pid={local}".format(
        scheme=_UNMAPPED_SCHEME, uid=uid or "no-uid", local=local)


# ---------------------------------------------------------------------------
# Records and families
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Records / families
# ---------------------------------------------------------------------------

# An identity is a tuple (origin, source, discriminator, bare) where
#   origin       - one of the ORIGIN_* constants (emission provenance only);
#   source       - absolute client URI (mapped/unmapped/anon) or the URI PID;
#   discriminator- the dotted property id (None for a URI PID);
#   bare         - True when the source token was a client number without a
#                  dotted property id, i.e. it identified a whole vCard slot.
#
# Matching across cards uses (source, discriminator, bare) -- never the origin
# and never the raw local number alone.  Bare tokens are aligned on a stable
# content-derived slot key so repeated merges cannot manufacture copies.

class Record(object):
    __slots__ = ("side", "prop", "fingerprint", "identities", "opaque_pids")

    def __init__(self, side, prop):
        self.side = side
        self.prop = prop
        self.fingerprint = content_fingerprint(prop)
        self.identities = set()
        self.opaque_pids = []

    def add_identity(self, origin, source, discriminator, bare):
        self.identities.add((origin, source, discriminator, bool(bare)))


class Family(object):
    """Atomic adoption unit: one singleton or one side's ALTID group."""

    __slots__ = ("side", "members", "altid", "order", "signature",
                 "identities", "keep_both_loser")

    def __init__(self, side, members, altid, order):
        self.side = side
        self.members = members
        self.altid = altid
        self.order = order
        self.signature = tuple(sorted(m.fingerprint for m in members))
        ids = set()
        for member in members:
            ids |= member.identities
        self.identities = ids
        self.keep_both_loser = False

    def refresh_identities(self):
        ids = set()
        for member in self.members:
            ids |= member.identities
        self.identities = ids


def _prop_disc(rec):
    """Stable exact-content property discriminator (see module docstring)."""
    h = hashlib.sha1()
    h.update(rec.prop.name.encode("utf-8"))
    h.update(b"\x1f")
    h.update(rec.fingerprint.encode("utf-8"))
    return "p-" + h.hexdigest()[:12]


_BARE_MARK = "b-"   # output marker for a property that came from a bare PID


def _bare_marker(rec):
    """Reconstructable marker naming the bare slot of *rec*'s content."""
    return _BARE_MARK + _prop_disc(rec)[2:]


def _add_numeric_identity(rec, origin, source, prop_id, bare_input):
    """Add the identity set for one numeric PID token.

    A bare client number identifies a whole vCard slot, not one property, so
    it contributes:
      * a bare slot identity keyed by the *source only* (used to notice that
        two values from the same source both sat on a bare slot), and
      * an exact content-key identity (used to align that specific value
        across cards and repeated merges).
    The exact key is rendered with the ``b-`` marker so the emitted token is
    parsed back symmetrically.  Explicit dotted property ids are kept verbatim.
    """
    if bare_input:
        rec.add_identity(origin, source, None, True)
        rec.add_identity(origin, source, _bare_marker(rec), False)
    else:
        # A previously emitted b- marker is a reconstructed bare slot: it
        # contributes both the exact content key and the source-only slot.
        if prop_id is not None and prop_id.startswith(_BARE_MARK):
            rec.add_identity(origin, source, prop_id, False)
            rec.add_identity(origin, source, None, True)
        else:
            rec.add_identity(origin, source, prop_id, False)


def _resolve_records(side, props, pidmaps, uid, report):
    records = []
    for prop in props:
        rec = Record(side, prop)
        for token in parse_pid_param(prop.params):
            m = _NUMERIC_TOKEN_RE.match(token)
            if m:
                local = int(m.group("client"))
                prop_id = m.group("prop")
                mapped = local in pidmaps
                source = pidmaps[local] if mapped else _unmapped_source(
                    uid, local)
                origin = ORIGIN_MAPPED if mapped else ORIGIN_UNMAPPED
                _add_numeric_identity(rec, origin, source, prop_id,
                                      bare_input=(prop_id is None))
                if not mapped:
                    _warn_unmapped(report, side, prop.name, local, source)
            elif _URI_TOKEN_RE.match(token):
                rec.add_identity(ORIGIN_URI, token, None, False)
            else:
                rec.opaque_pids.append(token)
                report.warnings.append({
                    "kind": "malformed-pid",
                    "side": side,
                    "property": prop.name,
                    "token": token,
                })
        records.append(rec)
    return records


def _warn_unmapped(report, side, property_name, local, source):
    report.warnings.append({
        "kind": "unmapped-pid",
        "side": side,
        "property": property_name,
        "local_pid": local,
        "synthetic_source": source,
    })


def _anon_source(uid):
    return "{0}:{1}".format(_ANON_SCHEME, uid or "no-uid")


def _assign_anonymous(uid, records, report):
    """Give every PID-less record a content-keyed anonymous identity.

    The source is per-UID and the discriminator is the exact content key, so
    equal PID-less values converge (on both sides and across repeated
    merges) while distinct values stay independent.  Exact match only.
    """
    source = _anon_source(uid)
    seen = set()
    for rec in records:
        if rec.identities:
            continue
        rec.add_identity(ORIGIN_ANON, source, _prop_disc(rec), False)
        key = (rec.prop.name, rec.fingerprint)
        if key not in seen:
            seen.add(key)
            report.warnings.append({
                "kind": "anonymous-property",
                "side": rec.side,
                "property": rec.prop.name,
                "note": "a property carried no PID; retained as an "
                        "independent value with a deterministic synthetic "
                        "identity",
            })


def _build_families(side, records):
    grouped = OrderedDict()
    emitted = []
    grouped_seen = set()
    for rec in records:
        altid = rec.prop.params.get("ALTID", [None])[0]
        if altid is not None:
            grouped.setdefault(altid, []).append(rec)
    for rec in records:
        altid = rec.prop.params.get("ALTID", [None])[0]
        if altid is None:
            emitted.append(Family(side, [rec], None, rec.prop.order))
        elif altid not in grouped_seen:
            members = grouped[altid]
            emitted.append(Family(side, members, altid,
                                  min(m.prop.order for m in members)))
            grouped_seen.add(altid)
    emitted.sort(key=lambda f: f.order)
    return emitted


# ---------------------------------------------------------------------------
# Report / result
# ---------------------------------------------------------------------------

class ReconcileReport(object):
    def __init__(self, uid):
        self.uid = uid
        self.clientpidmap = OrderedDict()
        self.entries = []
        self.warnings = []
        self.conflicts = []
        self.dropped = []

    def to_dict(self):
        return {
            "uid": self.uid,
            "clientpidmap": OrderedDict(
                (str(k), v) for k, v in self.clientpidmap.items()),
            "entries": list(self.entries),
            "warnings": list(self.warnings),
            "conflicts": list(self.conflicts),
            "dropped": list(self.dropped),
        }


class ReconcileResult(object):
    __slots__ = ("card", "report")

    def __init__(self, card, report):
        self.card = card
        self.report = report


# ---------------------------------------------------------------------------
# Matching helpers
# ---------------------------------------------------------------------------

def _bare_slots(family):
    return {(source, disc)
            for _origin, source, disc, bare in family.identities if bare}


def _exact_keys(family):
    return {(source, disc)
            for _origin, source, disc, bare in family.identities
            if not bare}


def _ident_label(key):
    source, disc, bare = key
    text = source if disc is None else "{0}#{1}".format(source, disc)
    return ("bare:" if bare else "") + text


class _UnionFind(object):
    def __init__(self):
        self.parent = {}

    def find(self, x):
        self.parent.setdefault(x, x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if ra < rb:
            self.parent[rb] = ra
        else:
            self.parent[ra] = rb


def _canonicalize_exact_clashes(local_families, remote_families):
    """Pre-connection rename for explicit property ids reused across sides.

    Used only for keep-both.  When both sides use the same dotted property id
    under the same resolved source for *different* content, the local side
    keeps the original slot and every remote record claiming that exact
    (source, id) with differing content is moved -- *before* families are
    connected -- onto a deterministic id naming the occupied slot and its own
    content.  Connecting after renaming means a re-fed original loser joins its
    already-emitted duplicate instead of creating a fresh conflict, so copies
    never accumulate.  Winner-takes-all policies do not call this; they need
    the raw clash so they can drop the losing side.  Bare slots and b-
    markers are left untouched (they align on content already).
    """
    owned = {}
    for family in local_families:
        for member in family.members:
            for origin, source, disc, bare in member.identities:
                if not bare and isinstance(disc, str) and                         not disc.startswith(_BARE_MARK) and                         not disc.startswith("dup-"):
                    owned.setdefault((source, disc), set()).add(
                        member.fingerprint)

    for family in remote_families:
        for member in family.members:
            new = set()
            for origin, source, disc, bare in member.identities:
                clash = (not bare and isinstance(disc, str)
                         and not disc.startswith(_BARE_MARK)
                         and not disc.startswith("dup-")
                         and (source, disc) in owned
                         and member.fingerprint not in owned[(source, disc)])
                if clash:
                    new.add((origin, source,
                             "dup-{0}-{1}".format(
                                 disc.replace(".", "_"),
                                 member.fingerprint[:10]),
                             False))
                else:
                    new.add((origin, source, disc, bare))
            member.identities = new
        family.refresh_identities()


def _family_components(local_families, remote_families):
    """Union cross-side families sharing an exact key or a bare slot."""
    uf = _UnionFind()
    for family in local_families + remote_families:
        uf.find(id(family))

    exact_index, bare_index = {}, {}
    for family in local_families:
        for key in _exact_keys(family):
            exact_index.setdefault(key, []).append(family)
        for key in _bare_slots(family):
            bare_index.setdefault(key, []).append(family)

    def connect(family, index, keys):
        for key in keys:
            for other in index.get(key, ()):
                uf.union(id(family), id(other))

    for family in remote_families:
        connect(family, exact_index, _exact_keys(family))
        connect(family, bare_index, _bare_slots(family))

    components = OrderedDict()
    for family in local_families + remote_families:
        components.setdefault(uf.find(id(family)), []).append(family)
    return list(components.values())


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def reconcile(local, remote, policy):
    """Reconcile two vCard 4.0 cards that share a UID."""
    if policy not in CONFLICT_POLICIES:
        raise ReconcileError(
            "Unknown conflict policy {0!r}; expected one of {1}".format(
                policy, ", ".join(CONFLICT_POLICIES)))

    uid_l, maps_l, props_l = parse_vcard(local)
    uid_r, maps_r, props_r = parse_vcard(remote)
    if uid_l is None or uid_l != uid_r:
        raise ReconcileError(
            "Cannot reconcile cards: UIDs must be present and equal "
            "(got {0!r} and {1!r})".format(uid_l, uid_r))
    uid = uid_l

    report = ReconcileReport(uid)
    local_records = _resolve_records("local", props_l, maps_l, uid, report)
    remote_records = _resolve_records("remote", props_r, maps_r, uid, report)
    _assign_anonymous(uid, local_records + remote_records, report)

    local_families = _build_families("local", local_records)
    remote_families = _build_families("remote", remote_records)
    if policy == KEEP_BOTH:
        _canonicalize_exact_clashes(local_families, remote_families)

    adopted = _select(local_families, remote_families, policy, report)
    output_props, clientpidmaps = _emit(uid, adopted, report)
    return ReconcileResult(serialize_vcard(output_props, uid,
                                           clientpidmaps), report)


# ---------------------------------------------------------------------------
# Selection / conflict policy
# ---------------------------------------------------------------------------

def _family_summary(family):
    return {
        "side": family.side,
        "altid": family.altid,
        "properties": [m.prop.name for m in family.members],
        "fingerprints": list(family.signature),
    }


def _drop(family, report):
    for member in family.members:
        report.dropped.append({
            "side": family.side,
            "property": member.prop.name,
            "reason": "conflict",
            "fingerprint": member.fingerprint,
        })


def _fold_identities(lfam, rfam):
    """Fold remote member identities into equal-content local members."""
    by_fp = {m.fingerprint: m for m in rfam.members}
    for local_member in lfam.members:
        remote_member = by_fp.get(local_member.fingerprint)
        if remote_member is None:
            continue
        local_member.identities |= remote_member.identities
        for token in remote_member.opaque_pids:
            if token not in local_member.opaque_pids:
                local_member.opaque_pids.append(token)
    lfam.refresh_identities()


def _select(local_families, remote_families, policy, report):
    components = _family_components(local_families, remote_families)

    def rank(fams):
        locals_ = [f for f in fams if f.side == "local"]
        if locals_:
            return (0, min(f.order for f in locals_))
        return (1, min(f.order for f in fams))

    components.sort(key=rank)
    adopted = []

    for fams in components:
        locals_ = sorted((f for f in fams if f.side == "local"),
                         key=lambda f: f.order)
        remotes = sorted((f for f in fams if f.side == "remote"),
                         key=lambda f: f.order)

        if not locals_:
            adopted.extend(remotes)
            continue
        if not remotes:
            adopted.extend(locals_)
            continue

        local_sigs = [f.signature for f in locals_]
        remote_sigs = [f.signature for f in remotes]
        same_families = sorted(local_sigs) == sorted(remote_sigs) and \
            len(locals_) == len(remotes)

        if same_families:
            remote_by_sig = {f.signature: f for f in remotes}
            for lfam in locals_:
                _fold_identities(lfam, remote_by_sig[lfam.signature])
                adopted.append(lfam)
            continue

        # Genuine conflict: same identity anchor, different content.
        exact = set()
        for lf in locals_:
            exact |= _exact_keys(lf)
        remote_exact = set()
        for rf in remotes:
            remote_exact |= _exact_keys(rf)
        shared_exact = exact & remote_exact

        bare = set()
        for lf in locals_:
            bare |= _bare_slots(lf)
        remote_bare = set()
        for rf in remotes:
            remote_bare |= _bare_slots(rf)
        shared_bare = bare & remote_bare

        anchors = sorted(_ident_label((src, disc, False))
                         for src, disc in shared_exact)
        anchors += sorted("bare:{0}#{1}".format(src, disc)
                          for src, disc in shared_bare)

        conflict = {
            "policy": policy,
            "identities": anchors,
            "local": [_family_summary(f) for f in locals_],
            "remote": [_family_summary(f) for f in remotes],
        }

        if policy == LOCAL_WINS:
            conflict["resolution"] = "local"
            adopted.extend(locals_)
            for fam in remotes:
                _drop(fam, report)
        elif policy == REMOTE_WINS:
            conflict["resolution"] = "remote"
            adopted.extend(remotes)
            for fam in locals_:
                _drop(fam, report)
        else:
            conflict["resolution"] = "keep-both"
            adopted.extend(_keep_both(locals_, remotes))
        report.conflicts.append(conflict)

    return adopted


def _keep_both(locals_, remotes_):
    """Keep every distinct value once, keyed by exact content signature.

    All families in the conflict component that share a content signature are
    the same value (no matter which side or which id they arrived on), so they
    collapse into a single adopted family whose identity set is the union of
    theirs.  Distinct signatures -- genuinely different values -- all survive.
    A surviving family whose signature only existed remotely is flagged as a
    loser so emission can move it off any exact id claimed by a local winner.
    """
    by_sig = OrderedDict()
    for family in locals_ + remotes_:
        by_sig.setdefault(family.signature, []).append(family)

    result = []
    for signature, families in by_sig.items():
        locals_here = [f for f in families if f.side == "local"]
        chosen = (locals_here[0] if locals_here
                  else sorted(families, key=lambda f: f.order)[0])
        for family in families:
            if family is not chosen:
                _fold_identities(chosen, family)
        if not locals_here:
            chosen.keep_both_loser = True
        result.append(chosen)
    result.sort(key=lambda f: (0 if f.side == "local" else 1, f.order))
    return result


# ---------------------------------------------------------------------------
# Emission
# ---------------------------------------------------------------------------

def _flatten(adopted):
    return [(fam, member) for fam in adopted for member in fam.members]


def _repair_internal_collisions(adopted, source_to_client):
    """Resolve distinct output records claiming the same (client, exact id).

    This covers malformed input where one device reused one property id for
    different values.  Equal content is the same property and is untouched;
    differing content gets a deterministic content-derived id.
    """
    flat = _flatten(adopted)
    occurrences = {}
    for fam, rec in flat:
        for origin, source, disc, bare in rec.identities:
            if bare or source not in source_to_client:
                continue
            occurrences.setdefault((source, disc), []).append(
                (origin, rec))
    for (source, disc), owners in occurrences.items():
        records = {id(rec): rec for _o, rec in owners}
        if len({rec.fingerprint for rec in records.values()}) <= 1:
            continue
        for origin, rec in owners:
            rec.identities.discard((origin, source, disc, False))
            rec.identities.add((origin, source,
                                "x-" + rec.fingerprint[:12], False))
    for family in adopted:
        family.refresh_identities()


def _emit(uid, adopted, report):
    client_sources = set()
    for fam, rec in _flatten(adopted):
        for origin, source, disc, bare in rec.identities:
            if origin in _CLIENT_ORIGINS:
                client_sources.add(source)

    ordered_sources = sorted(client_sources)
    source_to_client = OrderedDict(
        (source, i) for i, source in enumerate(ordered_sources, start=1))
    clientpidmaps = [(source_to_client[s], s) for s in ordered_sources]

    _repair_internal_collisions(adopted, source_to_client)

    altid_labels = _canonical_family_labels(adopted)
    group_labels = _canonical_group_labels(_flatten(adopted))

    flat = _flatten(adopted)
    output_props = []
    for fam, rec in flat:
        prop = rec.prop.clone()

        client_tokens, uri_tokens = [], []
        for origin, source, disc, bare in rec.identities:
            if origin not in _CLIENT_ORIGINS:
                uri_tokens.append(source)
                continue
            if bare and disc is None:
                # Source-only whole-card slot; it carries no per-property
                # token (the content key is emitted separately).
                continue
            client = source_to_client[source]
            token = ("{0}.{1}".format(client, disc)
                     if disc is not None else str(client))
            client_tokens.append(token)
        client_tokens = _dedup_sorted(client_tokens, _numeric_token_key)
        uri_tokens = sorted(set(uri_tokens))
        pid_tokens = client_tokens + uri_tokens
        for token in rec.opaque_pids:
            if token not in pid_tokens:
                pid_tokens.append(token)
        if pid_tokens:
            prop.params["PID"] = [",".join(pid_tokens)]
        else:
            prop.params.pop("PID", None)

        if fam.altid is not None:
            prop.params["ALTID"] = [altid_labels[id(fam)]]
        else:
            prop.params.pop("ALTID", None)

        if prop.group is not None:
            prop.group = group_labels[(fam.side, rec.prop.group,
                                       id(rec))]

        report.entries.append({
            "side": rec.side,
            "property": prop.name,
            "pid": prop.params.get("PID", [None])[0],
            "altid": prop.params.get("ALTID", [None])[0],
            "group": prop.group,
            "fingerprint": rec.fingerprint,
            "identities": sorted(
                "{0}:{1}".format(origin,
                                 source if disc is None
                                 else "{0}#{1}{2}".format(
                                     source, disc,
                                                     " (bare)" if bare else ""))
                for origin, source, disc, bare in rec.identities),
        })
        output_props.append(prop)

    report.clientpidmap = OrderedDict(clientpidmaps)
    return output_props, clientpidmaps


def _numeric_token_key(tok):
    m = _NUMERIC_TOKEN_RE.match(tok)
    if not m:
        return (1, 0, tok)
    prop = m.group("prop")
    return (0, int(m.group("client")), prop if prop is not None else "")


def _dedup_sorted(values, key):
    seen, out = set(), []
    for value in sorted(values, key=key):
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


# ---------------------------------------------------------------------------
# ALTID / group label canonicalization
# ---------------------------------------------------------------------------

def _canonical_family_labels(adopted):
    """Map ALTID-bearing family id -> stable label; equal content shares it."""
    signatures = OrderedDict()
    for family in adopted:
        if family.altid is not None:
            signatures[id(family)] = family.signature
    sig_to_label, used, result = {}, set(), {}
    counter = 0
    for fid, sig in sorted(signatures.items(), key=lambda kv: (kv[1], kv[0])):
        if sig in sig_to_label:
            result[fid] = sig_to_label[sig]
            continue
        counter += 1
        label = "alt{0}".format(counter)
        while label in used:
            counter += 1
            label = "alt{0}".format(counter)
        used.add(label)
        sig_to_label[sig] = label
        result[fid] = label
    return result


def _canonical_group_labels(flat):
    """Map ``(side, raw group, record-id)`` -> canonical ``itemNN`` label.

    Groups with identical content sets share one label; distinct content sets
    that happened to use the same raw label are renamed apart, so ALTID/group
    bookkeeping stays attached to the adopted properties it came in with.
    """
    content = OrderedDict()
    members = []
    for fam, rec in flat:
        if rec.prop.group is None:
            continue
        key = (fam.side, rec.prop.group)
        content.setdefault(key, set()).add(rec.fingerprint)
        members.append((fam, rec, key))
    sig_of = {key: tuple(sorted(fps)) for key, fps in content.items()}

    sig_to_label, used, mapping = {}, set(), {}
    counter = 0
    for key in sorted(content.keys(), key=lambda k: (sig_of[k], k[0], k[1])):
        sig = sig_of[key]
        side, label = key
        collision = any(k[1] == label and sig_of[k] != sig
                        for k in content if k != key)
        if sig in sig_to_label:
            newlabel = sig_to_label[sig]
        elif not collision and label not in used and \
                re.match(r"^[A-Za-z0-9-]+$", label or ""):
            newlabel = label
            used.add(label)
            sig_to_label[sig] = label
        else:
            counter += 1
            newlabel = "item{0}".format(counter)
            while newlabel in used:
                counter += 1
                newlabel = "item{0}".format(counter)
            used.add(newlabel)
            sig_to_label[sig] = newlabel
        mapping[key] = newlabel

    return {(fam.side, rec.prop.group, id(rec)): mapping[key]
            for fam, rec, key in members}
