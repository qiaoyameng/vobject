# -*- coding: utf-8 -*-
"""Tests for vCard 4.0 PID/CLIENTPIDMAP property reconciliation.

Focus:
  * same PID *number* but a different resolved source must NOT match;
  * different PID *numbers* but the same resolved source MUST match.
"""

from __future__ import unicode_literals

import re
import unittest

from vobject.vcard_pid import (
    reconcile, LOCAL_WINS, REMOTE_WINS, KEEP_BOTH, ReconcileError)


def card(props, maps=(), uid="urn:uuid:contact-1"):
    lines = ["BEGIN:VCARD", "VERSION:4.0", "UID:" + uid]
    lines.extend(props)
    for local_id, uri in maps:
        lines.append("CLIENTPIDMAP:{0};{1}".format(local_id, uri))
    lines.append("END:VCARD")
    return "\r\n".join(lines) + "\r\n"


A = "urn:uuid:device-A"
B = "urn:uuid:device-B"


def count_named(text, name):
    return len(re.findall(r"^(?:[A-Za-z0-9-]+\.)?" + name + r"[;:]",
                          text, re.M))


def clientpidmaps(text):
    out = {}
    for line in text.splitlines():
        if line.startswith("CLIENTPIDMAP:"):
            local_id, uri = line[len("CLIENTPIDMAP:"):].split(";", 1)
            out[int(local_id)] = uri
    return out


def pid_tokens(text, name="TEL"):
    tokens = []
    for line in text.splitlines():
        m = re.search(r"PID=([^:]+):", line)
        if not m:
            continue
        value = m.group(1)
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        tokens.extend(t for t in value.split(",") if t)
    return tokens


class SameNumberDifferentSourceTests(unittest.TestCase):
    def test_not_collapsed_keep_both(self):
        local = card(["TEL;PID=1:tel:+1-555-0100"], maps=[(1, A)])
        remote = card(["TEL;PID=1:tel:+1-555-0200"], maps=[(1, B)])
        result = reconcile(local, remote, KEEP_BOTH)
        self.assertEqual(count_named(result.card, "TEL"), 2)
        self.assertEqual(set(result.report.clientpidmap.values()), {A, B})
        self.assertEqual(set(result.report.clientpidmap), {1, 2})
        self.assertEqual(result.report.conflicts, [])

    def test_renumbered_consistently(self):
        local = card(["TEL;PID=9:tel:+1-555-0100"], maps=[(9, A)])
        remote = card(["TEL;PID=7:tel:+1-555-0200"], maps=[(7, B)])
        result = reconcile(local, remote, KEEP_BOTH)
        maps = clientpidmaps(result.card)
        for token in pid_tokens(result.card):
            self.assertRegex(token, r"^\d+\.b-[0-9a-f]+$")
            self.assertIn(int(token.split(".", 1)[0]), maps)

    def test_fixed_point_both_orders(self):
        local = card(["TEL;PID=1:tel:+1-555-0100"], maps=[(1, A)])
        remote = card(["TEL;PID=1:tel:+1-555-0200"], maps=[(1, B)])
        merged = reconcile(local, remote, KEEP_BOTH).card
        self.assertEqual(merged, reconcile(merged, remote, KEEP_BOTH).card)
        self.assertEqual(merged, reconcile(merged, local, KEEP_BOTH).card)
        self.assertEqual(merged, reconcile(merged, merged, KEEP_BOTH).card)


class DifferentNumberSameSourceTests(unittest.TestCase):
    def test_identical_value_collapses(self):
        local = card(["TEL;PID=1:tel:+1-555-0100"], maps=[(1, A)])
        remote = card(["TEL;PID=2:tel:+1-555-0100"], maps=[(2, A)])
        result = reconcile(local, remote, KEEP_BOTH)
        self.assertEqual(count_named(result.card, "TEL"), 1)
        self.assertEqual(result.report.conflicts, [])
        self.assertEqual(set(result.report.clientpidmap.values()), {A})

    def test_dotted_property_ids_aligned(self):
        local = card(["TEL;PID=3.4:tel:+1-555-0100"], maps=[(3, A)])
        remote = card(["TEL;PID=8.4:tel:+1-555-0100"], maps=[(8, A)])
        result = reconcile(local, remote, KEEP_BOTH)
        self.assertEqual(count_named(result.card, "TEL"), 1)
        self.assertEqual(pid_tokens(result.card), ["1.4"])

    def test_same_identity_different_content_conflict(self):
        local = card(["TEL;PID=1:tel:+1-555-AAAA"], maps=[(1, A)])
        remote = card(["TEL;PID=2:tel:+1-555-BBBB"], maps=[(2, A)])
        local_won = reconcile(local, remote, LOCAL_WINS)
        self.assertEqual(count_named(local_won.card, "TEL"), 1)
        self.assertIn("AAAA", local_won.card)
        self.assertEqual(local_won.report.conflicts[0]["resolution"], "local")
        remote_won = reconcile(local, remote, REMOTE_WINS)
        self.assertEqual(count_named(remote_won.card, "TEL"), 1)
        self.assertIn("BBBB", remote_won.card)
        both = reconcile(local, remote, KEEP_BOTH)
        self.assertEqual(count_named(both.card, "TEL"), 2)
        self.assertEqual(both.report.conflicts[0]["resolution"],
                         "keep-both")


class FixedPointTests(unittest.TestCase):
    def assert_stable(self, local, remote, policy=KEEP_BOTH, rounds=4):
        first = reconcile(local, remote, policy).card
        cur_r = cur_l = first
        for _ in range(rounds):
            cur_r = reconcile(cur_r, remote, policy).card
            cur_l = reconcile(cur_l, local, policy).card
        self.assertEqual(first, cur_r)
        self.assertEqual(first, cur_l)
        self.assertEqual(first, reconcile(first, first, policy).card)
        return first

    def test_keep_both_conflict_does_not_grow(self):
        for props in (
            ("TEL;PID=1:tel:AAA", "TEL;PID=2:tel:BBB"),
            ("TEL;PID=1.7:tel:AAA", "TEL;PID=2.7:tel:BBB"),
        ):
            local = card([props[0]], maps=[(1, A)])
            remote = card([props[1]], maps=[(2, A)])
            merged = self.assert_stable(local, remote, KEEP_BOTH)
            self.assertEqual(count_named(merged, "TEL"), 2)

    def test_winner_takes_all_does_not_grow(self):
        local = card(["TEL;PID=1:tel:AAA"], maps=[(1, A)])
        remote = card(["TEL;PID=2:tel:BBB"], maps=[(2, A)])
        for policy, winner_card, loser_card, survivor in (
                (LOCAL_WINS, local, remote, "AAA"),
                (REMOTE_WINS, remote, local, "BBB")):
            merged = reconcile(local, remote, policy).card
            self.assertEqual(count_named(merged, "TEL"), 1)
            self.assertIn(survivor, merged)
            # Re-feeding on the winning side, or the result to itself, must be
            # byte-stable; re-feeding on the losing side may flip the value
            # (the policy is directional) but must never grow past one TEL.
            self.assertEqual(merged,
                             reconcile(merged, winner_card, policy).card)
            self.assertEqual(merged,
                             reconcile(merged, merged, policy).card)
            cur = merged
            for _ in range(4):
                cur = reconcile(cur, loser_card, policy).card
                self.assertEqual(count_named(cur, "TEL"), 1)

    def test_no_pid_identical_converges_distinct_kept(self):
        local = card(["NOTE:same", "NOTE:left-only"])
        remote = card(["NOTE:same", "NOTE:right-only"])
        merged = self.assert_stable(local, remote)
        self.assertEqual(count_named(merged, "NOTE"), 3)
        merged = self.assert_stable(card(["NOTE:abc"]), card(["NOTE:abcd"]))
        self.assertEqual(count_named(merged, "NOTE"), 2)


class EdgeCaseTests(unittest.TestCase):
    def test_multiple_pids_on_one_property(self):
        local = card(['TEL;PID="1,2":tel:X'], maps=[(1, A), (2, B)])
        remote = card(["TEL;PID=3:tel:X"], maps=[(3, A)])
        self.assertEqual(count_named(
            reconcile(local, remote, KEEP_BOTH).card, "TEL"), 1)

    def test_multiple_pids_shared_id_but_other_value(self):
        local = card(['TEL;PID="1,2":tel:X'], maps=[(1, A), (2, B)])
        remote = card(["TEL;PID=3:tel:Y"], maps=[(3, A)])
        result = reconcile(local, remote, KEEP_BOTH)
        self.assertEqual(count_named(result.card, "TEL"), 2)
        self.assertEqual(len(result.report.conflicts), 1)

    def test_missing_clientpidmap(self):
        local = card(["TEL;PID=1:tel:AAA"])
        remote = card(["TEL;PID=2:tel:BBB"], maps=[(2, A)])
        result = reconcile(local, remote, KEEP_BOTH)
        self.assertEqual(count_named(result.card, "TEL"), 2)
        self.assertIn("unmapped-pid",
                      {w["kind"] for w in result.report.warnings})
        merged = result.card
        self.assertEqual(merged, reconcile(merged, remote, KEEP_BOTH).card)
        self.assertEqual(merged, reconcile(merged, local, KEEP_BOTH).card)

    def test_absolute_uri_pid(self):
        local = card(["TEL;PID=urn:uuid:thing%239:tel:Z"])
        remote = card(["TEL;PID=urn:uuid:thing%239:tel:Z"])
        result = reconcile(local, remote, KEEP_BOTH)
        self.assertEqual(count_named(result.card, "TEL"), 1)
        self.assertIn("urn:uuid:thing%239", result.card)

    def test_altid_family_adopted_atomically(self):
        local = card(["item1.TEL;PID=1.1;ALTID=w;TYPE=work:tel:W",
                      "item1.TEL;PID=1.2;ALTID=w;TYPE=work:tel:WW"],
                     maps=[(1, A)])
        remote = card(["item2.TEL;PID=2.1;ALTID=9;TYPE=work:tel:W",
                       "item2.TEL;PID=2.2;ALTID=9;TYPE=work:tel:WW"],
                      maps=[(2, A)])
        result = reconcile(local, remote, KEEP_BOTH)
        self.assertEqual(count_named(result.card, "TEL"), 2)
        self.assertEqual(set(re.findall(r"ALTID=([^;:]+)", result.card)),
                         {"alt1"})
        self.assertEqual(set(re.findall(r"^([A-Za-z0-9-]+)\.TEL",
                                        result.card, re.M)), {"item1"})

    def test_group_label_collision_distinct_content(self):
        local = card(["item1.TEL;PID=1:tel:X"], maps=[(1, A)])
        remote = card(["item1.TEL;PID=2:tel:Y"], maps=[(2, B)])
        result = reconcile(local, remote, KEEP_BOTH)
        groups = re.findall(r"^([A-Za-z0-9-]+)\.TEL", result.card, re.M)
        self.assertEqual(len(groups), 2)
        self.assertEqual(len(set(groups)), 2)

    def test_inputs_are_not_modified(self):
        local = card(["TEL;PID=1:tel:A"], maps=[(1, A)])
        remote = card(["TEL;PID=1:tel:B"], maps=[(1, B)])
        local_copy, remote_copy = local, remote
        reconcile(local, remote, KEEP_BOTH)
        self.assertEqual(local, local_copy)
        self.assertEqual(remote, remote_copy)

    def test_structured_values_and_escaping_roundtrip(self):
        local = card(["N:Doe" + chr(92) + ", Jr;Jane;;Dr.;"])
        remote = card(["NOTE:line1" + chr(92) + "nline2; semi"])
        result = reconcile(local, remote, KEEP_BOTH)
        self.assertIn("Doe" + chr(92) + ", Jr;Jane;;Dr.;", result.card)
        self.assertIn("line1" + chr(92) + "nline2; semi", result.card)

    def test_uid_required_and_matching(self):
        with self.assertRaises(ReconcileError):
            reconcile(card(["TEL:tel:A"], uid="urn:uuid:one"),
                      card(["TEL:tel:B"], uid="urn:uuid:two"), KEEP_BOTH)
        no_uid = "BEGIN:VCARD\r\nVERSION:4.0\r\nTEL:tel:A\r\nEND:VCARD\r\n"
        with self.assertRaises(ReconcileError):
            reconcile(no_uid, card(["TEL:tel:B"]), KEEP_BOTH)

    def test_unknown_policy_rejected(self):
        with self.assertRaises(ReconcileError):
            reconcile(card(["TEL:tel:A"]), card(["TEL:tel:B"]), "avg")

    def test_malformed_clientpidmap_rejected(self):
        broken = ("BEGIN:VCARD\r\nVERSION:4.0\r\nUID:urn:uuid:contact-1\r\n"
                  "CLIENTPIDMAP:nonsense\r\nTEL:tel:A\r\nEND:VCARD\r\n")
        with self.assertRaises(ReconcileError):
            reconcile(broken, card(["TEL:tel:B"]), KEEP_BOTH)


if __name__ == "__main__":
    unittest.main()
