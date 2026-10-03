"""
Reconcile two vCards that share a UID using PID / CLIENTPIDMAP identities.

RFC 6350 section 5.5 defines the PID property parameter and the CLIENTPIDMAP
property.  CLIENTPIDMAP maps a small, card-local source number to a globally
meaningful source URI, and PID parameter values of the form "source.pid"
attach a property instance to one of those sources.  Two property instances
therefore represent the same logical property only when their *resolved*
identities -- the (source URI, pid) pair -- are equal.  Comparing property
names or the raw digits of PID values is not sufficient: the same local
number can denote different sources in different cards, and different local
numbers can denote the same source.

The entry point is :func:`reconcile_vcards`.  It takes two parsed VCARD
components with the same UID plus an explicit conflict policy, and returns a
new merged card together with a :class:`ReconciliationReport`.  Neither input
card is modified.  The merged card carries a freshly allocated, conflict-free
set of CLIENTPIDMAP source numbers, and every PID parameter is rewritten to
reference the new numbers.

Deterministic handling for the awkward cases:

* A property may carry several PID values; it matches when any of its
  resolved identities matches, and adopted properties accumulate the union
  of identities seen so far.
* Same source, same pid, different content: a conflict, settled by the
  explicit policy (``prefer-a``, ``prefer-b`` or ``keep-both``).
* A PID whose source number has no CLIENTPIDMAP mapping cannot be resolved;
  the value is dropped from the output and a warning is recorded.  Such a
  property never matches a property from the other card.
* A property with no PID at all has no identity; it is kept as an
  independent value.  No fuzzy de-duplication is attempted -- only
  byte-identical content is collapsed, which keeps repeated merges of the
  same card from accumulating copies.

ALTID parameters and group labels travel with whichever property instance is
adopted, so alternative representations stay associated.
"""

from collections import namedtuple

from .base import Component, ContentLine

PREFER_A = 'prefer-a'
PREFER_B = 'prefer-b'
KEEP_BOTH = 'keep-both'
CONFLICT_POLICIES = (PREFER_A, PREFER_B, KEEP_BOTH)

ReconciliationResult = namedtuple('ReconciliationResult', ['card', 'report'])

# Properties that describe the card itself rather than a contact attribute;
# they are never matched, merged or copied from the second card.
_SKIP_NAMES = frozenset(['version', 'uid', 'clientpidmap'])


class ReconciliationReport(object):
    """
    Structured outcome of :func:`reconcile_vcards`.

    @ivar identical: properties of card B whose identity matched a property
        of card A with equal content (or exact duplicates of a PID-less
        property); each entry is a dict with ``name`` and ``identities``.
    @ivar conflicts: list of dicts with ``name``, ``identities``,
        ``resolution``, ``value_a`` and ``value_b``.
    @ivar added: properties copied from card B because no identity matched;
        each entry is a dict with ``name`` and ``value``.
    @ivar warnings: human readable notes, e.g. PID values referencing a
        source number with no CLIENTPIDMAP mapping.
    @ivar clientpidmap: the reassigned ``(number, uri)`` pairs written to
        the merged card.
    """

    def __init__(self):
        self.identical = []
        self.conflicts = []
        self.added = []
        self.warnings = []
        self.clientpidmap = []

    def __repr__(self):
        return ('<ReconciliationReport identical=%d conflicts=%d added=%d '
                'warnings=%d>' % (len(self.identical), len(self.conflicts),
                                  len(self.added), len(self.warnings)))


def _source_mappings(card):
    """
    Return ``(ordered, mapping)`` for a card's CLIENTPIDMAP properties:
    ``ordered`` is a list of ``(number, uri)`` in order of appearance and
    ``mapping`` maps source number to source URI.
    """
    ordered = []
    mapping = {}
    for line in card.contents.get('clientpidmap', []):
        number, sep, uri = str(line.value).partition(';')
        number = number.strip()
        uri = uri.strip()
        if not sep or not number.isdigit() or not uri:
            continue
        number = int(number)
        if number not in mapping:
            ordered.append((number, uri))
        mapping[number] = uri
    return ordered, mapping


def _line_identities(line, mapping, side, report):
    """
    Split a line's PID parameter into resolved identities and raw values.

    Returns ``(identities, raw)`` where ``identities`` is a set of
    ``(source_uri, pid)`` tuples and ``raw`` is a list of PID values that
    carry no source number (e.g. ``PID=5``).  Values whose source number
    has no CLIENTPIDMAP mapping are dropped and reported; they cannot be
    compared across cards and must not survive CLIENTPIDMAP renumbering.
    """
    pids = line.params.get('PID')
    if not pids:
        return set(), []
    identities = set()
    raw = []
    for value in pids:
        value = str(value)
        source, dot, pid = value.partition('.')
        if dot and source.isdigit():
            uri = mapping.get(int(source))
            if uri is None:
                report.warnings.append(
                    '%s: PID value %r on card %s references source %s with '
                    'no CLIENTPIDMAP mapping; value dropped'
                    % (line.name, value, side, source))
            else:
                identities.add((uri, pid))
        else:
            raw.append(value)
    return identities, raw


def _params_key(line, ignore_pid=True):
    return dict((k, sorted(str(v) for v in values))
                for k, values in line.params.items()
                if not (ignore_pid and k == 'PID'))


def _same_content(line_a, line_b):
    """Equal modulo the PID parameter and the group label."""
    return (line_a.name == line_b.name
            and line_a.value == line_b.value
            and _params_key(line_a) == _params_key(line_b)
            and sorted(line_a.singletonparams) == sorted(line_b.singletonparams))


def _exact_duplicate(line_a, line_b):
    """
    Byte-identical modulo the PID parameter.  Used only for properties with
    no usable identity, so re-merging a card does not add copies; this is
    exact comparison, not fuzzy de-duplication.
    """
    return line_a.group == line_b.group and _same_content(line_a, line_b)


def _describe(line, identities):
    return {
        'name': line.name,
        'value': line.value,
        'identities': sorted('%s|%s' % (uri, pid) for uri, pid in identities),
    }


def reconcile_vcards(card_a, card_b, conflict_policy):
    """
    Merge ``card_b`` into a copy of ``card_a`` using PID/CLIENTPIDMAP
    property identities.

    ``conflict_policy`` must be one of ``PREFER_A``, ``PREFER_B`` or
    ``KEEP_BOTH``; it is applied whenever both cards carry the same
    (source URI, pid) identity with different content.

    Returns a ``ReconciliationResult`` named tuple with the merged ``card``
    and a ``ReconciliationReport``.  The inputs are not modified.
    """
    if conflict_policy not in CONFLICT_POLICIES:
        raise ValueError('conflict_policy must be one of %r, got %r'
                         % (CONFLICT_POLICIES, conflict_policy))
    uid_a = card_a.getChildValue('uid')
    uid_b = card_b.getChildValue('uid')
    if not uid_a or uid_a != uid_b:
        raise ValueError('reconcile_vcards requires two cards with the same '
                         'UID, got %r and %r' % (uid_a, uid_b))

    report = ReconciliationReport()
    ordered_a, map_a = _source_mappings(card_a)
    ordered_b, map_b = _source_mappings(card_b)

    merged = Component.duplicate(card_a)
    for line in list(merged.contents.get('clientpidmap', [])):
        merged.remove(line)

    # Identity bookkeeping for every line living in the merged card:
    # id(line) -> [set of (uri, pid) identities, list of raw PID values].
    line_idents = {}
    # name -> lines of the merged card, for candidate lookup.
    index = {}

    def register(line, identities, raw):
        line_idents[id(line)] = [set(identities), list(raw)]
        index.setdefault(line.name.lower(), []).append(line)

    def unregister(line):
        line_idents.pop(id(line), None)
        named = index.get(line.name.lower())
        if named and line in named:
            named.remove(line)

    for line in merged.lines():
        if line.name.lower() in _SKIP_NAMES:
            continue
        identities, raw = _line_identities(line, map_a, 'a', report)
        register(line, identities, raw)

    def adopt_copy(line_b, identities, raw):
        """Append a copy of a card B line, keeping group and ALTID."""
        new_line = ContentLine.duplicate(line_b)
        merged.add(new_line)
        register(new_line, identities, raw)
        return new_line

    for line_b in card_b.lines():
        name = line_b.name.lower()
        if name in _SKIP_NAMES:
            continue
        identities_b, raw_b = _line_identities(line_b, map_b, 'b', report)
        candidates = index.get(name, [])

        match = None
        if identities_b:
            for candidate in candidates:
                if line_idents[id(candidate)][0] & identities_b:
                    match = candidate
                    break

        if match is not None:
            entry = line_idents[id(match)]
            if _same_content(match, line_b):
                entry[0] |= identities_b
                for value in raw_b:
                    if value not in entry[1]:
                        entry[1].append(value)
                report.identical.append(_describe(line_b, identities_b))
            else:
                conflict = {
                    'name': match.name,
                    'identities': sorted('%s|%s' % pair
                                         for pair in entry[0] | identities_b),
                    'resolution': conflict_policy,
                    'value_a': match.value,
                    'value_b': line_b.value,
                }
                report.conflicts.append(conflict)
                if conflict_policy == PREFER_A:
                    entry[0] |= identities_b
                elif conflict_policy == PREFER_B:
                    unregister(match)
                    merged.remove(match)
                    adopt_copy(line_b, entry[0] | identities_b, raw_b)
                else:  # KEEP_BOTH
                    adopt_copy(line_b, identities_b, raw_b)
            continue

        # No identity match.  Properties without a usable identity are kept
        # as independent values; only exact duplicates are suppressed so
        # that merging the same card twice stays idempotent.
        if any(_exact_duplicate(candidate, line_b) for candidate in candidates):
            report.identical.append(_describe(line_b, identities_b))
            continue
        adopt_copy(line_b, identities_b, raw_b)
        report.added.append(_describe(line_b, identities_b))

    # Non-line children (components) of card B have no identity either;
    # copy them unless an exact duplicate already exists.
    for child in card_b.components():
        if not any(_component_equal(child, existing)
                   for existing in merged.components()):
            merged.add(Component.duplicate(child))

    # Reallocate CLIENTPIDMAP source numbers: card A's sources first, then
    # any new URIs contributed by card B, deduplicated by URI.
    new_numbers = {}
    for _number, uri in ordered_a + ordered_b:
        if uri not in new_numbers:
            new_numbers[uri] = len(new_numbers) + 1

    # Rewrite every PID parameter to the reallocated source numbers.
    for line in list(merged.lines()):
        entry = line_idents.get(id(line))
        if entry is None:
            continue
        identities, raw = entry
        pid_values = ['%d.%s' % (new_numbers[uri], pid)
                      for uri, pid in
                      sorted(identities, key=lambda pair: (new_numbers[pair[0]],
                                                           pair[1]))]
        pid_values.extend(raw)
        if pid_values:
            line.params['PID'] = pid_values
        else:
            line.params.pop('PID', None)

    for uri, number in sorted(((uri, num) for uri, num in new_numbers.items()),
                              key=lambda pair: pair[1]):
        line = merged.add('clientpidmap')
        line.behavior = None  # keep the ';' in the value unescaped
        line.value = '%d;%s' % (number, uri)
        report.clientpidmap.append((number, uri))

    return ReconciliationResult(merged, report)


def _component_equal(comp_a, comp_b):
    if comp_a.name != comp_b.name:
        return False
    children_a = sorted(child.serialize() for child in comp_a.getChildren())
    children_b = sorted(child.serialize() for child in comp_b.getChildren())
    return children_a == children_b
