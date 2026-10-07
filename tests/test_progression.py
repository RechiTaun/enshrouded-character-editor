"""Progression mutations use synthetic assets/saves, never a live profile."""

from dataclasses import replace
from fractions import Fraction
from io import BytesIO
from pathlib import Path
import struct
import unittest
from unittest.mock import patch

from test_game_rules import (assets, balance, executable, open_rules,
                             progression_rules, reward_table, skill_table)
from test_player_state import character, knowledge, payload
from test_save_format import container
from game_rules import PointBudget
from player_state import Experience, PlayerState, Skill
from save_format import Bdb, KNOW, Knowledge, SaveDocument, SaveError


class ProgressionTests(unittest.TestCase):
    def setUp(self):
        self.index, self.data, _ = assets()
        self.rules = open_rules(index=self.index, resource_data=self.data)
        self.read = patch.object(Path, "open", side_effect=lambda *a, **kw: BytesIO(self.data))
        self.read.start()
        self.addCleanup(self.read.stop)
        self.stale = patch.object(self.rules, "assert_unchanged")
        self.stale_check = self.stale.start()
        self.addCleanup(self.stale.stop)

    def document(self, raw=None):
        if raw is None:
            raw = payload(skill_nodes=())
        return SaveDocument(container([
            (123, b"CHAR", character(raw)), (123, KNOW, knowledge()),
            (456, b"CHAR", character()), (456, KNOW, knowledge()),
            (123, b"COUT", b"synthetic opaque appearance"),
        ]))

    def test_exact_curve_and_early_table(self):
        rules = self.rules.progression()
        self.assertEqual(tuple(rules.required_xp(i) for i in range(1, 6)), rules.early_xp)
        for level in range(6, 46):
            expected = int(Fraction(6092) + Fraction(225, 2) * level + Fraction(1, 2))
            self.assertEqual(rules.required_xp(level), expected)
        self.assertEqual((rules.required_xp(25), rules.required_xp(26),
                          rules.required_xp(30)), (8905, 9017, 9467))
        self.assertEqual(replace(rules, kill_start=200, kill_end=50).required_xp(30), 9467)
        for invalid in (0, 46, -1, True, 1.5, "30"):
            with self.subTest(level=invalid), self.assertRaises(SaveError):
                rules.required_xp(invalid)

    def test_budget_matches_bonus_flags_and_invested_not_effective_rank(self):
        budget = self.rules.point_budget(25, [], Knowledge(knowledge()))
        self.assertEqual(budget, PointBudget(48, 57, 0))
        self.assertEqual(budget.available, 105)
        allocated = [Skill(0, 345, 100, 25), Skill(1, 346, 101, (2 << 5) | 31)]
        budget = self.rules.point_budget(30, allocated, Knowledge(knowledge()))
        self.assertEqual((budget.earned, budget.spent, budget.available), (115, 7, 108))
        changed = Knowledge(knowledge()).patch(1000, 0)
        changed = Knowledge(changed).patch(1001, 99)
        self.assertEqual(self.rules.point_budget(25, [], Knowledge(changed)).bonus_points, 54)

    def test_unknown_duplicate_or_orphaned_nodes_and_invalid_ranks_reject(self):
        for skills in ([Skill(0, 999, 0, 0)],
                       [Skill(0, 345, 0, 0), Skill(1, 345, 0, 0)],
                       [Skill(0, 0, 100, 0)], [Skill(0, 0, 0, 1)],
                       [Skill(0, 346, 100, 4 << 5)]):
            with self.subTest(skills=skills), self.assertRaises(SaveError):
                self.rules.point_budget(25, skills, Knowledge(knowledge()))
        self.assertEqual(self.rules.point_budget(
            25, [Skill(0, 346, 0, 31)], Knowledge(knowledge())).spent, 0)

    def test_real_progression_paths_and_exact_preservation(self):
        doc = self.document()
        original = doc.blob(123, b"CHAR").original
        original_state = PlayerState(original)
        for target, required, grant, points in ((26, 9017, 4252, 2),
                                               (30, 9467, 40996, 10), (45, 11155, None, 40)):
            doc.edit_character(123, "Hero", target, self.rules)
            state = PlayerState(doc.blob(123, b"CHAR").data)
            self.assertEqual(state.experience(), Experience(target, 1548, 0, required))
            self.assertEqual(Bdb(state.character).character(), ("Hero", target))
            self.assertEqual(state.skills(), original_state.skills())
            self.assertEqual(state.inventory(), original_state.inventory())
            plan = original_state.level_plan(target, self.rules.progression(),
                                             original_state.experience())
            if grant is not None:
                self.assertEqual(plan.xp_granted, grant)
            self.assertEqual(plan.points_added, points)
            expected = bytearray(original_state.payload)
            _, components = original_state.player()
            for component, values in (("Level", (target,)), ("Experience", (1548, 0, required))):
                item = components[component]
                at, _, _ = item.schema.array("dataStorage")
                struct.pack_into(f"<{len(values)}I", expected, item.offset + at, *values)
            self.assertEqual(state.payload, bytes(expected))
            allowed = {Bdb(original).values + 4 * Bdb(original).field("level") + j
                       for j in range(4)}
            for i, (old, new) in enumerate(zip(original_state.payload, expected)):
                if old != new:
                    allowed.update(original_state.positions[i] + j for j in range(4))
            self.assertTrue({i for i, (a, b) in enumerate(zip(original, state.character))
                             if a != b} <= allowed)
            serialized = doc.serialize()
            roundtrip = SaveDocument(serialized)
            self.assertEqual(roundtrip.blob(123, KNOW).data, knowledge())
            for owner, tag in ((456, b"CHAR"), (456, KNOW), (123, b"COUT")):
                self.assertEqual(roundtrip.blob(owner, tag).compressed, doc.blob(owner, tag).compressed)
            self.assertEqual(doc.progression_budget(123, self.rules).earned, (target - 1) * 2 + 57)
        doc.edit_character(123, "Hero", 25, self.rules)
        self.assertEqual(doc.serialize(), doc.original)
        self.assertFalse(doc.changes)
        self.assertFalse(doc.progression_sources)

    def test_staged_inventory_name_and_existing_skill_handles_survive_retarget(self):
        doc = self.document(payload(skill_nodes=((345, 111, 25), (346, 222, 95))))
        original_skills = PlayerState(doc.blob(123, b"CHAR").data).skills()
        # The quantity mutation is separately tested; compose its same-width payload patch here.
        state = PlayerState(doc.blob(123, b"CHAR").data)
        doc.blob(123, b"CHAR").data = state.patch_quantity(
            100, 0, 22, item_id=1234, max_stack=500)
        for target in (30, 26, 25):
            doc.edit_character(123, "Abcd", target, self.rules)
            edited = PlayerState(doc.blob(123, b"CHAR").data)
            self.assertEqual(edited.skills(), original_skills)
            self.assertEqual(edited.inventory()[0].quantity, 22)
            self.assertEqual(Bdb(edited.character).character(), ("Abcd", target))
        self.assertEqual(edited.experience().required, 4252)

    def test_invalid_level_or_xp_is_atomic(self):
        for raw, target in ((payload(skill_nodes=()), 24), (payload(skill_nodes=()), 46),
                            (payload(skill_nodes=()), 0), (payload(skill_nodes=()), -1),
                            (payload(xp=(1548, 1, 4252)), 30),
                            (payload(xp=(4252, 0, 4252)), 30),
                            (payload(xp=(10000, 0, 20000)), 26)):
            doc = self.document(raw)
            with self.subTest(target=target), self.assertRaises(SaveError):
                doc.edit_character(123, "Abcd", target, self.rules)
            self.assertEqual(doc.serialize(), doc.original)
            self.assertFalse(doc.changes)

    def test_zero_cached_threshold_is_initialized_only_on_increase(self):
        doc = self.document(payload(skill_nodes=(), xp=(1548, 0, 0)))
        doc.edit_character(123, "Hero", 26, self.rules)
        self.assertIn("XP grant 8905", doc.changes[(123, "level")])
        doc.edit_character(123, "Hero", 25, self.rules)
        self.assertEqual(doc.serialize(), doc.original)

    def test_missing_or_stale_rules_and_missing_knowledge_reject(self):
        doc = self.document()
        with self.assertRaisesRegex(SaveError, "Load installed"):
            doc.edit_character(123, "Hero", 30)
        self.stale_check.side_effect = SaveError("Rules changed")
        with self.assertRaisesRegex(SaveError, "Rules changed"):
            doc.edit_character(123, "Hero", 30, self.rules)
        self.assertEqual(doc.serialize(), doc.original)
        self.stale_check.side_effect = None
        missing = SaveDocument(container([(123, b"CHAR", character(payload(skill_nodes=())))]))
        with self.assertRaises(SaveError):
            missing.edit_character(123, "Hero", 30, self.rules)
        self.assertEqual(missing.serialize(), missing.original)
        doc.edit_character(123, "Hero", 30, self.rules)
        self.stale_check.side_effect = SaveError("Rules changed")
        with self.assertRaisesRegex(SaveError, "Rules changed"):
            doc.serialize()

    def test_staged_bonus_edits_cannot_create_overspending(self):
        self.rules._skill_rules = {345: replace(
            self.rules._point_tables()[0][345], cost=104)}
        doc = self.document(payload())
        doc.edit_character(123, "Hero", 30, self.rules)
        for key in range(1000, 1003):
            doc.edit_knowledge(123, key, 0)
        before = doc.blob(123, KNOW).data
        with self.assertRaisesRegex(SaveError, "exceed"):
            doc.edit_knowledge(123, 1003, 0)
        self.assertEqual(doc.blob(123, KNOW).data, before)
        self.assertEqual(doc.progression_budget(123, self.rules).available, 2)
        doc.serialize()

    def test_bad_installed_tables_and_curves_fail_explicitly(self):
        for resources in (
                [(0x87654321, balance(cap=101))],
                [(0x87654321, balance(points=3))],
                [(0x87654321, balance()), (0x11112222, skill_table(((345, 1, 4, 0),))),
                 (0x33334444, reward_table())],
                [(0x87654321, balance()), (0x11112222, skill_table()),
                 (0x33334444, reward_table(((1000, 2),)))],
                [(0x87654321, balance()), (0x11112222, skill_table())]):
            index, data, _ = assets(resources)
            rules = open_rules(executable()[0], index, data)
            with patch.object(Path, "open", side_effect=lambda *a, **kw: BytesIO(data)), \
                    self.assertRaises(SaveError):
                rules.point_budget(25, [], Knowledge(knowledge()))

    def test_unverified_executable_is_readonly_even_with_matching_metadata(self):
        rules = open_rules(index=self.index, resource_data=self.data, verified=False)
        self.assertEqual(rules.items_for({123})[123].max_stack, 5000)
        with self.assertRaisesRegex(SaveError, "not verified"):
            rules.point_budget(25, [], Knowledge(knowledge()))
        with patch.object(rules, "assert_unchanged"):
            doc = self.document()
            with self.assertRaisesRegex(SaveError, "not verified"):
                doc.edit_character(123, "Hero", 30, rules)
            self.assertEqual(doc.serialize(), doc.original)


if __name__ == "__main__":
    unittest.main()
