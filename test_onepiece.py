"""One Piece singles are priced against the printing the listing title
describes. Offline: the index is built from a small hand-made catalog."""

import unittest

import api_engines


def group(products):
    """(name, code, {subtype: market}) rows -> one tcgcsv-style group triple."""
    prods, prices = [], []
    for pid, (name, code, market) in enumerate(products):
        prods.append({"productId": pid, "name": name,
                      "extendedData": [{"name": "Number", "value": code}]})
        prices += [{"productId": pid, "subTypeName": sub, "marketPrice": mp}
                   for sub, mp in market.items()]
    return ({"name": "test"}, prods, prices)


CATALOG = [
    ("Roronoa Zoro (025)", "OP01-025", {"Normal": 3.44}),
    ("Roronoa Zoro (025) (Parallel)", "OP01-025", {"Foil": 282.75}),
    ("Roronoa Zoro (Reprint)", "OP01-025", {"Normal": 2.10}),
    ("Monkey.D.Luffy (119) (SP)", "OP05-119", {"Foil": 5749.97}),
    ("Monkey.D.Luffy (119) (SP) (Gold)", "OP05-119", {"Foil": 13000.0}),
    ("Monkey.D.Luffy (OP05-119) (Manga)", "OP05-119", {"Foil": 5000.0}),
    ("Monkey.D.Luffy (119) (Alternate Art)", "OP05-119", {"Foil": 163.66}),
    ("Monkey.D.Luffy (119)", "OP05-119", {"Foil": 20.21}),
    ("Nico Robin", "OP05-010", {"Foil": 309.16}),
    ("Nico Robin", "OP05-010", {"Normal": 0.14}),
    ("Monkey.D.Luffy - P-007 (Winner Pack Vol. 1)", "P-007", {"Foil": 249.84}),
    ("Monkey.D.Luffy - P-007 (Tournament Pack Vol. 1)", "P-007", {"Normal": 11.24}),
    ("Mr.2.Bon.Kurei (Bentham)", "OP02-086", {"Normal": 1.50}),
    ("Mr.2.Bon.Kurei (Bentham) (Alternate Art)", "OP02-086", {"Foil": 40.0}),
    ("Both Subtypes", "OP09-001", {"Normal": 5.0, "Foil": 9.0}),
]


class OnePiecePrintingTests(unittest.TestCase):
    def setUp(self):
        self.saved = (api_engines._op_index, api_engines._op_index_until)
        api_engines._op_index = api_engines._op_index_from([group(CATALOG)])
        api_engines._op_index_until = float("inf")

    def tearDown(self):
        api_engines._op_index, api_engines._op_index_until = self.saved

    def price(self, title):
        code = api_engines.parse_title("onepiece", title)
        return api_engines.fetch_onepiece_price(code, title) if code else None

    def test_foil_only_printings_are_priced(self):
        self.assertEqual(
            self.price("Roronoa Zoro OP01-025 SR Alt Art Romance Dawn One Piece English"),
            (282.75, "Roronoa Zoro (025) (Parallel)"),
        )

    def test_a_title_with_no_variant_gets_the_cheapest_plain_printing(self):
        # Base card and its reprint both fit; never the parallel.
        self.assertEqual(self.price("Roronoa Zoro OP01-025 SR Romance Dawn One Piece")[0], 2.10)
        # Two printings share one name: the cheaper is assumed.
        self.assertEqual(self.price("Nico Robin OP05-010 SR One Piece English NM")[0], 0.14)

    def test_the_most_specific_printing_wins(self):
        self.assertEqual(self.price("Monkey D. Luffy OP05-119 SP One Piece")[0], 5749.97)
        self.assertEqual(self.price("Monkey D. Luffy OP05-119 SP Gold One Piece")[0], 13000.0)
        # "Manga Alt Art": the manga printing explains the words "alt art".
        self.assertEqual(self.price("Monkey D Luffy OP05-119 SEC Manga Alt Art English")[0], 5000.0)
        self.assertEqual(self.price("Monkey D Luffy OP05-119 SEC Alt Art English")[0], 163.66)

    def test_a_claimed_variant_with_no_such_printing_is_not_priced(self):
        self.assertIsNone(self.price("Nico Robin OP05-010 SR Alt Art One Piece English"))
        self.assertIsNone(self.price("Roronoa Zoro OP01-025 SR Signed by voice actor One Piece"))
        self.assertIsNone(self.price("Roronoa Zoro OP01-025 Judge Promo One Piece"))

    def test_promo_codes_need_their_event_named(self):
        self.assertEqual(self.price("Monkey D Luffy P-007 Winner Pack Vol 1 One Piece promo")[0], 249.84)
        self.assertEqual(self.price("Monkey D Luffy P-007 Tournament Pack One Piece promo")[0], 11.24)
        self.assertIsNone(self.price("Monkey D Luffy P-007 One Piece promo card"))

    def test_bracket_text_shared_by_every_printing_is_part_of_the_name(self):
        self.assertEqual(self.price("Mr.2 Bon Kurei OP02-086 One Piece Paramount War")[0], 1.50)
        self.assertEqual(self.price("Mr.2 Bon Kurei OP02-086 Alt Art One Piece")[0], 40.0)

    def test_japanese_copies_are_not_priced_on_the_english_catalog(self):
        self.assertIsNone(self.price("One Piece Japanese Roronoa Zoro OP01-025 SR Parallel"))

    def test_lowest_price_across_subtypes(self):
        self.assertEqual(self.price("Both Subtypes OP09-001 One Piece")[0], 5.0)

    def test_unknown_code(self):
        self.assertIsNone(self.price("Some Card OP99-999 One Piece"))


if __name__ == "__main__":
    unittest.main()
