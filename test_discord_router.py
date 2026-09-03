import unittest
from unittest.mock import patch

import discord_router


class DiscordRoutingTests(unittest.TestCase):
    def test_every_game_routes_to_its_correct_tier(self):
        slots = {
            game: {
                "budget": f"https://example.com/{game}/budget",
                "premium": f"https://example.com/{game}/premium",
                "sealed": f"https://example.com/{game}/sealed",
            }
            for game in ("pokemon", "mtg", "lorcana", "onepiece")
        }
        with patch.object(discord_router, "WEBHOOKS", slots):
            for game in slots:
                self.assertEqual(
                    discord_router.determine_channel(
                        game, "Booster Box", sealed=True, market_price=500
                    ),
                    ("sealed", slots[game]["sealed"]),
                )
                self.assertEqual(
                    discord_router.determine_channel(
                        game, "Card Name", sealed=False, market_price=10
                    ),
                    ("budget", slots[game]["budget"]),
                )
                self.assertEqual(
                    discord_router.determine_channel(
                        game,
                        "Card Name",
                        sealed=False,
                        market_price=discord_router.PREMIUM_THRESHOLD,
                    ),
                    ("premium", slots[game]["premium"]),
                )
                self.assertEqual(
                    discord_router.determine_channel(
                        game, "Card Name PSA 8", sealed=False, market_price=10
                    ),
                    ("premium", slots[game]["premium"]),
                )

    @patch("discord_router._post_embed", return_value=True)
    def test_deal_ping_keeps_tcgplayer_price_and_correct_description(self, post):
        discord_router.send_discord_alert(
            "Charizard 4/102",
            "https://example.com/item",
            80,
            5,
            120,
            "https://example.com/webhook",
            "premium",
            "pokemon",
            matched_name="Charizard",
        )
        embed = post.call_args.args[1]
        fields = {field["name"]: field for field in embed["fields"]}
        self.assertEqual(fields["📊  TCGplayer Market Price"]["value"], "$120.00")
        self.assertIn("Charizard 4/102", embed["description"])
        self.assertEqual(
            embed["footer"]["text"],
            "#pokemon/premium  •  Open-Market Engine",
        )
        self.assertEqual(post.call_args.kwargs["sold_context"]["sealed"], False)

    @patch("discord_router._post_embed", return_value=True)
    def test_sealed_ping_keeps_tcgplayer_price_and_sealed_route(self, post):
        discord_router.send_sealed_alert(
            "Surging Sparks Booster Box",
            "https://example.com/box",
            100,
            0,
            150,
            "https://example.com/webhook",
            "pokemon",
            matched_name="Surging Sparks Booster Box",
        )
        embed = post.call_args.args[1]
        fields = {field["name"]: field for field in embed["fields"]}
        self.assertEqual(fields["📊  TCGplayer Market Price"]["value"], "$150.00")
        self.assertEqual(
            embed["footer"]["text"],
            "#pokemon/sealed  •  Open-Market Engine",
        )
        self.assertEqual(post.call_args.args[2:4], ("sealed", "pokemon"))
        self.assertEqual(post.call_args.kwargs["sold_context"]["sealed"], True)

    @patch("discord_router._post_embed", return_value=True)
    def test_restock_description_makes_no_tcgplayer_or_discount_claim(self, post):
        discord_router.send_restock_alert(
            "Surging Sparks Booster Box",
            "https://example.com/restock",
            110,
            "USD",
            "https://example.com/webhook",
            "pokemon",
            "Card Shop",
            channel="restock",
        )
        embed = post.call_args.args[1]
        names = {field["name"] for field in embed["fields"]}
        self.assertNotIn("📊  TCGplayer Market Price", names)
        self.assertNotIn("💸  Discount", names)
        self.assertEqual(
            embed["footer"]["text"],
            "#pokemon/restock  •  Retail Restock Watch",
        )
        self.assertEqual(post.call_args.args[2:4], ("restock", "pokemon"))


if __name__ == "__main__":
    unittest.main()