import unittest
from unittest.mock import Mock, patch

from discord_router import _edit_with_sold_comps, _post_embed
from sold_comps import _same_item, _search_query, _summarize, format_sold_comps


def sold(
    item_id: str,
    title: str,
    total: float,
    *,
    condition: str = "Brand New",
) -> dict:
    return {
        "itemId": item_id,
        "title": title,
        "soldPrice": str(total),
        "totalPrice": str(total),
        "soldCurrency": "USD",
        "condition": condition,
        "endedAt": "2026-09-03",
        "url": f"https://www.ebay.com/itm/{item_id}",
    }


class SoldCompMatchingTests(unittest.TestCase):
    def test_search_query_preserves_language_and_grade_market(self):
        self.assertEqual(
            _search_query(
                "Charizard 4/102",
                "Pokemon Base Set Charizard PSA 10 Japanese",
                "Japanese",
            ),
            "Charizard 4/102 PSA 10 Japanese -half -mini -acrylic -case -lot",
        )

    def test_rejects_wrong_product_language_condition_and_case(self):
        identity = "Scarlet Violet 151 Booster Box"
        self.assertTrue(_same_item(
            sold("1", "Pokemon Scarlet Violet 151 Booster Box", 275),
            identity,
            identity,
            "English",
            True,
        ))
        for row in (
            sold("2", "Pokemon 151 Ultra Premium Collection Box", 200),
            sold("3", "Pokemon 151 Japanese Booster Box", 300),
            sold("8", "Pokemon 151 Chinese Booster Box", 120),
            sold("4", "Pokemon 151 Booster Box Case", 1500),
            sold("6", "Pokemon 151 Half Booster Box", 140),
            sold("7", "Pokemon 151 Booster Box with Acrylic", 310),
            sold(
                "5",
                "Pokemon Scarlet Violet 151 Booster Box",
                100,
                condition="Pre-Owned",
            ),
        ):
            self.assertFalse(_same_item(
                row, identity, identity, "English", True
            ))

    def test_requires_matching_grade_and_card_number(self):
        identity = "Charizard 4/102 PSA 10"
        self.assertTrue(_same_item(
            sold("1", "Pokemon Base Set Charizard 4/102 PSA 10", 9000),
            identity,
            identity,
            "English",
            False,
        ))
        self.assertFalse(_same_item(
            sold("2", "Pokemon Base Set Charizard 4/102 PSA 9", 1500),
            identity,
            identity,
            "English",
            False,
        ))
        self.assertFalse(_same_item(
            sold("3", "Pokemon Base Set Charizard 4/130 PSA 10", 800),
            identity,
            identity,
            "English",
            False,
        ))
        for grader, grade in (("PSA", "8"), ("CGC", "8.5"), ("CSG", "10")):
            identity = f"Charizard 4/102 {grader} {grade}"
            self.assertTrue(_same_item(
                sold(
                    grader,
                    f"Pokemon Base Set Charizard 4/102 {grader} {grade}",
                    1000,
                ),
                identity,
                identity,
                "English",
                False,
            ))
            self.assertFalse(_same_item(
                sold(f"{grader}-raw", "Pokemon Base Set Charizard 4/102", 400),
                identity,
                identity,
                "English",
                False,
            ))

    def test_rejects_cross_package_sealed_comps(self):
        cases = (
            ("Pokemon 151 Booster Pack", "Pokemon 151 Elite Trainer Box"),
            ("Pokemon 151 Blister", "Pokemon 151 Booster Pack"),
            ("Pokemon 151 ETB", "Pokemon 151 Booster Box"),
            ("Pokemon Build and Battle Box", "Pokemon Booster Box"),
            ("Pokemon 151 Booster Case", "Pokemon 151 Booster Box"),
        )
        for target, candidate in cases:
            self.assertFalse(_same_item(
                sold(target, candidate, 100),
                target,
                target,
                "English",
                True,
            ))
        self.assertTrue(_same_item(
            sold("case", "Pokemon 151 Booster Box Case", 1400),
            "Pokemon 151 Booster Case",
            "Pokemon 151 Booster Case",
            "English",
            True,
        ))

    def test_variant_and_quantity_signatures_are_symmetric(self):
        mismatches = (
            (
                "Pokemon Surging Sparks Booster Box with Acrylic",
                "Pokemon Surging Sparks Booster Box",
            ),
            (
                "Pokemon Surging Sparks Half Booster Box",
                "Pokemon Surging Sparks Booster Box",
            ),
            (
                "Pokemon 151 Mini Tin",
                "Pokemon 151 Tin",
            ),
            (
                "Lot of 2 Pokemon 151 Booster Boxes",
                "Pokemon 151 Booster Box",
            ),
            (
                "Pokemon 151 Booster Box",
                "Lot of 2 Pokemon 151 Booster Boxes",
            ),
        )
        for target, candidate in mismatches:
            self.assertFalse(_same_item(
                sold(target, candidate, 200),
                target,
                target,
                "English",
                True,
            ))

    def test_edition_and_finish_variants_must_match_exactly(self):
        identity = "Base Set Charizard 4/102 1st Edition Holo"
        self.assertTrue(_same_item(
            sold(
                "same",
                "Pokemon Base Set Charizard 4/102 1st Edition Holo",
                5000,
            ),
            identity,
            identity,
            "English",
            False,
        ))
        for candidate in (
            "Pokemon Base Set Charizard 4/102 Unlimited Holo",
            "Pokemon Base Set Charizard 4/102 Holo",
            "Pokemon Base Set Charizard 4/102 1st Edition Non Holo",
            "Pokemon Celebrations Charizard 4/102 1st Edition Holo",
        ):
            self.assertFalse(_same_item(
                sold(candidate, candidate, 500),
                identity,
                identity,
                "English",
                False,
            ))

    def test_summarizes_five_recent_sales_and_removes_outlier(self):
        identity = "Scarlet Violet 151 Booster Box"
        rows = [
            sold("1", "Pokemon Scarlet Violet 151 Booster Box", 270),
            sold("2", "Pokemon Scarlet Violet 151 Booster Box", 275),
            sold("3", "Pokemon Scarlet Violet 151 Booster Box", 280),
            sold("4", "Pokemon Scarlet Violet 151 Booster Box", 285),
            sold("5", "Pokemon Scarlet Violet 151 Booster Box", 290),
            sold("6", "Pokemon Scarlet Violet 151 Booster Box", 40),
            sold("7", "Pokemon 151 Ultra Premium Collection Box", 200),
        ]
        result = _summarize(rows, identity, identity, "English", True)
        self.assertIsNotNone(result)
        self.assertEqual(result["count"], 5)
        self.assertEqual(result["average"], 280)
        self.assertEqual(result["median"], 280)
        rendered = format_sold_comps(result)
        self.assertIn("Avg total: $280.00", rendered)
        self.assertIn("Last 5", rendered)

    @patch("discord_router.get_sold_comps")
    @patch("discord_router.requests.patch")
    def test_webhook_message_is_edited_with_sold_comps(
        self, patch_request: Mock, lookup: Mock
    ):
        patch_request.return_value.raise_for_status.return_value = None
        lookup.return_value = {
            "average": 280,
            "median": 280,
            "count": 3,
            "sales": [
                {"total": 270, "url": "https://example.com/1"},
                {"total": 280, "url": "https://example.com/2"},
                {"total": 290, "url": "https://example.com/3"},
            ],
        }
        _edit_with_sold_comps(
            "https://discord.com/api/webhooks/1/token",
            "123",
            {"fields": []},
            {
                "identity": "Surging Sparks Booster Box",
                "listing_title": "Surging Sparks Booster Box",
                "language": "English",
                "sealed": True,
            },
        )
        url = patch_request.call_args.args[0]
        payload = patch_request.call_args.kwargs["json"]
        self.assertTrue(url.endswith("/messages/123"))
        self.assertEqual(
            payload["embeds"][0]["fields"][0]["name"],
            "📈  Recent eBay Sold Comps",
        )

    @patch("discord_router.POST_TO_DISCORD", True)
    @patch("discord_router._COMPS_EXECUTOR.submit", side_effect=RuntimeError)
    @patch("discord_router.requests.post")
    def test_initial_ping_succeeds_when_enrichment_worker_fails(
        self, post: Mock, _submit: Mock
    ):
        post.return_value.raise_for_status.return_value = None
        post.return_value.json.return_value = {"id": "123"}
        self.assertTrue(_post_embed(
            "https://discord.com/api/webhooks/1/token",
            {"fields": []},
            "sealed",
            "pokemon",
            sold_context={
                "identity": "Pokemon 151 Booster Box",
                "listing_title": "Pokemon 151 Booster Box",
                "language": "English",
                "sealed": True,
            },
        ))