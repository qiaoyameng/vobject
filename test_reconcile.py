"""
Tests for vobject.reconcile: PID/CLIENTPIDMAP based vCard reconciliation.
"""

import unittest

import vobject
from vobject.reconcile import (
    KEEP_BOTH,
    PREFER_A,
    PREFER_B,
    reconcile_vcards,
)

URN_A = 'urn:uuid:device-a'
URN_B = 'urn:uuid:device-b'
URN_C = 'urn:uuid:device-c'


def make_card(extra_lines, sources=((1, URN_A),), uid='contact-1'):
    lines = ['BEGIN:VCARD', 'VERSION:4.0', 'UID:%s' % uid, 'FN:Alice']
    lines += ['CLIENTPIDMAP:%d;%s' % pair for pair in sources]
    lines += list(extra_lines)
    lines.append('END:VCARD')
    return vobject.readOne('\r\n'.join(lines) + '\r\n')


def tels(card):
    return card.contents.get('tel', [])


class IdentityResolutionTest(unittest.TestCase):
    """The two headline cases: local numbers alone must not decide identity."""

    def test_same_number_different_source_stays_separate(self):
        card_a = make_card(['TEL;PID=1.5:111'], sources=((1, URN_A),))
        card_b = make_card(['TEL;PID=1.5:222'], sources=((1, URN_B),))
        result = reconcile_vcards(card_a, card_b, PREFER_A)

        self.assertEqual(len(tels(result.card)), 2)
        self.assertEqual(result.report.conflicts, [])
        self.assertEqual(len(result.report.added), 1)
        self.assertEqual(result.report.clientpidmap,
                         [(1, URN_A), (2, URN_B)])
        # B's property is rewritten to the reallocated source number.
        rewritten = [t for t in tels(result.card) if t.value == '222'][0]
        self.assertEqual(rewritten.params['PID'], ['2.5'])

    def test_different_number_same_source_is_merged(self):
        card_a = make_card(['TEL;PID=1.5:111'], sources=((1, URN_A),))
        card_b = make_card(['TEL;PID=2.5:111'], sources=((2, URN_A),))
        result = reconcile_vcards(card_a, card_b, PREFER_A)

        self.assertEqual(len(tels(result.card)), 1)
        tel_matches = [e for e in result.report.identical
                       if e['name'] == 'TEL']
        self.assertEqual(len(tel_matches), 1)
        self.assertEqual(tel_matches[0]['identities'], ['%s|5' % URN_A])
        self.assertEqual(result.report.conflicts, [])
        # One shared source URI, one CLIENTPIDMAP entry, references rewritten.
        self.assertEqual(result.report.clientpidmap, [(1, URN_A)])
        self.assertEqual(tels(result.card)[0].params['PID'], ['1.5'])

    def test_same_source_different_pid_stays_separate(self):
        card_a = make_card(['TEL;PID=1.5:111'], sources=((1, URN_A),))
        card_b = make_card(['TEL;PID=2.7:222'], sources=((2, URN_A),))
        result = reconcile_vcards(card_a, card_b, PREFER_A)

        self.assertEqual(len(tels(result.card)), 2)
        self.assertEqual(result.report.conflicts, [])
        self.assertEqual(result.report.clientpidmap, [(1, URN_A)])
        rewritten = [t for t in tels(result.card) if t.value == '222'][0]
        self.assertEqual(rewritten.params['PID'], ['1.7'])


class ConflictPolicyTest(unittest.TestCase):
    """Same source, same pid, different content: the explicit policy decides."""

    def setUp(self):
        self.card_a = make_card(['TEL;PID=1.5:111'], sources=((1, URN_A),))
        self.card_b = make_card(['TEL;PID=2.5:222'], sources=((2, URN_A),))

    def test_prefer_a(self):
        result = reconcile_vcards(self.card_a, self.card_b, PREFER_A)
        self.assertEqual([t.value for t in tels(result.card)], ['111'])
        self.assertEqual(len(result.report.conflicts), 1)
        conflict = result.report.conflicts[0]
        self.assertEqual(conflict['resolution'], PREFER_A)
        self.assertEqual(conflict['value_a'], '111')
        self.assertEqual(conflict['value_b'], '222')

    def test_prefer_b(self):
        result = reconcile_vcards(self.card_a, self.card_b, PREFER_B)
        self.assertEqual([t.value for t in tels(result.card)], ['222'])
        self.assertEqual(len(result.report.conflicts), 1)
        # The adopted property keeps the union of identities.
        self.assertEqual(tels(result.card)[0].params['PID'], ['1.5'])

    def test_keep_both(self):
        result = reconcile_vcards(self.card_a, self.card_b, KEEP_BOTH)
        self.assertEqual(sorted(t.value for t in tels(result.card)),
                         ['111', '222'])
        self.assertEqual(len(result.report.conflicts), 1)

    def test_invalid_policy_rejected(self):
        self.assertRaises(ValueError, reconcile_vcards,
                          self.card_a, self.card_b, 'newest-wins')

    def test_uid_mismatch_rejected(self):
        other = make_card([], uid='contact-2')
        self.assertRaises(ValueError, reconcile_vcards,
                          self.card_a, other, PREFER_A)


class AwkwardCasesTest(unittest.TestCase):

    def test_property_with_multiple_pids(self):
        card_a = make_card(['TEL;PID=1.5:111'], sources=((1, URN_A),))
        card_b = make_card(['TEL;PID=2.9,2.5:111'], sources=((2, URN_A),))
        result = reconcile_vcards(card_a, card_b, PREFER_A)

        self.assertEqual(len(tels(result.card)), 1)
        # The adopted property accumulates the union of identities.
        self.assertEqual(tels(result.card)[0].params['PID'], ['1.5', '1.9'])

    def test_missing_source_mapping(self):
        card_a = make_card(['TEL;PID=1.5:111'], sources=((1, URN_A),))
        card_b = make_card(['TEL;PID=4.2:333'], sources=((1, URN_B),))
        result = reconcile_vcards(card_a, card_b, PREFER_A)

        self.assertEqual(len(result.report.warnings), 1)
        self.assertIn('4.2', result.report.warnings[0])
        # No identity could be resolved: kept as an independent value, and
        # the dangling PID reference is dropped from the output.
        self.assertEqual(len(tels(result.card)), 2)
        appended = [t for t in tels(result.card) if t.value == '333'][0]
        self.assertNotIn('PID', appended.params)

    def test_no_pid_properties_kept_as_independent_values(self):
        card_a = make_card(['TEL:+1 555 0100', 'EMAIL:a@example.com'])
        card_b = make_card(['TEL:+15550100', 'EMAIL:a@example.com'])
        result = reconcile_vcards(card_a, card_b, PREFER_A)

        # No fuzzy de-duplication: the two TELs differ and both survive.
        self.assertEqual(len(tels(result.card)), 2)
        # Byte-identical values collapse instead of multiplying.
        self.assertEqual(len(result.card.contents.get('email', [])), 1)

    def test_repeated_merge_is_idempotent(self):
        card_a = make_card(
            ['TEL;PID=1.5:111', 'EMAIL:a@example.com',
             'TEL;PID=9.9:999'],
            sources=((1, URN_A),))
        card_b = make_card(
            ['TEL;PID=2.5:111', 'TEL;PID=2.7:222', 'EMAIL:a@example.com',
             'TEL;PID=4.4:999'],
            sources=((2, URN_A), (3, URN_B),))

        first = reconcile_vcards(card_a, card_b, PREFER_A)
        second = reconcile_vcards(first.card, card_b, PREFER_A)

        self.assertEqual(first.card.serialize(), second.card.serialize())
        self.assertEqual(len(tels(second.card)), 3)
        self.assertEqual(second.report.added, [])
        self.assertEqual(second.report.conflicts, [])

    def test_inputs_are_not_modified(self):
        card_a = make_card(['TEL;PID=1.5:111'])
        card_b = make_card(['TEL;PID=1.5:222', 'EMAIL:b@example.com'],
                           sources=((1, URN_B),))
        before_a = card_a.serialize()
        before_b = card_b.serialize()
        reconcile_vcards(card_a, card_b, PREFER_B)
        self.assertEqual(card_a.serialize(), before_a)
        self.assertEqual(card_b.serialize(), before_b)


class AssociationTest(unittest.TestCase):
    """ALTID groups and group labels travel with the adopted property."""

    def test_group_and_altid_preserved_for_added_property(self):
        card_a = make_card(['TEL;PID=1.5:111'])
        card_b = make_card(['item1.TEL;PID=1.3;ALTID=7:333'],
                           sources=((1, URN_B),))
        result = reconcile_vcards(card_a, card_b, PREFER_A)

        appended = [t for t in tels(result.card) if t.value == '333'][0]
        self.assertEqual(appended.group, 'item1')
        self.assertEqual(appended.params['ALTID'], ['7'])

    def test_group_and_altid_follow_preferred_property(self):
        card_a = make_card(['home.TEL;PID=1.5;ALTID=1:111'])
        card_b = make_card(['work.TEL;PID=2.5;ALTID=2:222'],
                           sources=((2, URN_A),))
        result = reconcile_vcards(card_a, card_b, PREFER_B)

        self.assertEqual(len(tels(result.card)), 1)
        adopted = tels(result.card)[0]
        self.assertEqual(adopted.value, '222')
        self.assertEqual(adopted.group, 'work')
        self.assertEqual(adopted.params['ALTID'], ['2'])


class ClientPidMapRewriteTest(unittest.TestCase):

    def test_reallocation_and_reference_rewrite(self):
        card_a = make_card(['TEL;PID=2.4:111'],
                           sources=((1, URN_A), (2, URN_C)))
        card_b = make_card(['TEL;PID=1.4,1.7:111', 'TEL;PID=2.9:222'],
                           sources=((1, URN_C), (2, URN_B)))
        result = reconcile_vcards(card_a, card_b, PREFER_A)

        self.assertEqual(result.report.clientpidmap,
                         [(1, URN_A), (2, URN_C), (3, URN_B)])
        by_value = dict((t.value, t) for t in tels(result.card))
        # B's local source 1 is A's local source 2 (URN_C): the properties
        # match, and the adopted property accumulates both pids.
        self.assertEqual(by_value['111'].params['PID'], ['2.4', '2.7'])
        # B's new property is rewritten from its local source 2 to source 3.
        self.assertEqual(by_value['222'].params['PID'], ['3.9'])
        # The serialized card contains matching, unescaped CLIENTPIDMAPs.
        serialized = result.card.serialize()
        self.assertIn('CLIENTPIDMAP:3;%s' % URN_B, serialized)
        # Round-trip: the merged card parses back cleanly.
        reparsed = vobject.readOne(serialized)
        self.assertEqual(len(tels(reparsed)), 2)


if __name__ == '__main__':
    unittest.main()
