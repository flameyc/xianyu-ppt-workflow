import unittest

from production_router import choose_route


def payload(**changes):
    base = {
        "agreed_total": None,
        "billable_pages": None,
        "unit_price": None,
        "price_evidence": None,
        "complexity": "standard",
        "requested_route": "auto",
        "override_reason": None,
    }
    base.update(changes)
    return base


class ProductionRouterTests(unittest.TestCase):
    def test_local_review_missing_evidence_and_unknown_scope(self):
        self.assertTrue(choose_route(payload(unit_price='4'))['provisional'])
        self.assertFalse(choose_route(payload(unit_price='4',price_evidence='客户已确认'))['provisional'])
        self.assertTrue(choose_route(payload(complexity='complex'))['requires_scope_decision'])
        for data in ({'complexity':[]},{'requested_route':{}},{'unit_price':'1e999999'}, {'price_evidence':True}):
            with self.subTest(data=data),self.assertRaises(ValueError):
                choose_route(payload(**data))
        with self.assertRaises(ValueError):
            choose_route(payload(),{'quality_threshold':0})

    def test_threshold_below_and_above(self):
        self.assertEqual(
            choose_route(payload(unit_price="9.999"))["route"], "economy"
        )
        self.assertEqual(
            choose_route(payload(unit_price="10.001"))["route"], "quality"
        )

    def test_exact_ten_is_quality(self):
        result = choose_route(payload(unit_price="10.00"))
        self.assertEqual(result["route"], "quality")
        self.assertEqual(result["effective_unit_price"], "10")

    def test_unknown_price_is_provisional_economy(self):
        result = choose_route(payload())
        self.assertEqual(result["route"], "economy")
        self.assertTrue(result["provisional"])
        self.assertIsNone(result["effective_unit_price"])
        self.assertIn("price_confirmation_required", result["warnings"])

    def test_total_divided_by_billable_pages(self):
        result = choose_route(payload(agreed_total=80, billable_pages=20))
        self.assertEqual(result["effective_unit_price"], "4")
        self.assertEqual(result["route"], "economy")

    def test_explicit_price_wins_and_mismatch_warns(self):
        result = choose_route(payload(
            unit_price="9",
            agreed_total="200",
            billable_pages=20,
        ))
        self.assertEqual(result["effective_unit_price"], "9")
        self.assertEqual(result["basis"]["price_source"], "unit_price")
        self.assertIn("price_mismatch", result["warnings"])

    def test_complex_low_price_keeps_economy(self):
        result = choose_route(payload(
            unit_price="8",
            complexity="complex",
        ))
        self.assertEqual(result["route"], "economy")
        self.assertIn("scope_conflict", result["warnings"])
        self.assertTrue(result["requires_scope_decision"])

    def test_manual_override_requires_reason_and_keeps_conflict(self):
        with self.assertRaises(ValueError):
            choose_route(payload(
                unit_price="8",
                requested_route="quality",
            ))

        result = choose_route(payload(
            unit_price="8",
            complexity="complex",
            requested_route="quality",
            override_reason="本人决定走质量路线",
        ))
        self.assertEqual(result["route"], "quality")
        self.assertIn("scope_conflict", result["warnings"])
        self.assertIn("high_cost_low_price_conflict", result["warnings"])
        self.assertTrue(result["requires_scope_decision"])

    def test_reject_nan_bool_zero_pages_negative_and_bad_enum(self):
        bad_cases = [
            {"unit_price": float("nan")},
            {"unit_price": True},
            {"billable_pages": 0, "agreed_total": 80},
            {"unit_price": "-1"},
            {"complexity": "hard"},
            {"requested_route": "fast"},
        ]
        for changes in bad_cases:
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    choose_route(payload(**changes))

    def test_checks_exist_for_both_routes(self):
        result = choose_route(payload(unit_price="10"))
        expected = {
            "content_coverage",
            "native_text_editable",
            "final_full_page_visual_check",
            "revision_source_binding",
        }
        self.assertEqual(set(result["checks"]["economy"]), expected)
        self.assertEqual(set(result["checks"]["quality"]), expected)


if __name__ == "__main__":
    unittest.main()
