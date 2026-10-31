"""Fixed paper-fee regression vectors; all prices/fees are exact USD Decimals.

For ordinary markets, expected fees are ceil-to-cent of
0.07 * contracts * P * (1-P) (taker) or
0.0175 * contracts * P * (1-P) (maker). These vectors pin both rates and
rounding behavior so a changed formula or rounding mode fails visibly.
"""
import unittest
from decimal import Decimal

from app.fees import estimate_fee


class FeeRegressionTests(unittest.TestCase):
    def test_taker_fee_fixed_vectors(self):
        # Expected fees: $0.02 for 1 @ $0.50; $0.12 for 10 @ $0.20;
        # $1.47 for 100 @ $0.30; $0.70 for 1,000 @ $0.99.
        vectors = [
            ("0.50", 1, "0.02"),
            ("0.20", 10, "0.12"),
            ("0.30", 100, "1.47"),
            ("0.99", 1000, "0.70"),
        ]
        for price, quantity, expected in vectors:
            with self.subTest(price=price, quantity=quantity):
                result = estimate_fee("KXTEST-1", Decimal(price), quantity, "taker")
                self.assertEqual(result["fee"], Decimal(expected))
                self.assertEqual(result["fee_type"], "taker")
                self.assertEqual(result["fee_status"], "estimated")

    def test_maker_fee_fixed_vectors(self):
        # Expected fees: $0.01 for 1 @ $0.50; $0.03 for 10 @ $0.20;
        # $0.37 for 100 @ $0.30; $0.18 for 1,000 @ $0.99.
        vectors = [
            ("0.50", 1, "0.01"),
            ("0.20", 10, "0.03"),
            ("0.30", 100, "0.37"),
            ("0.99", 1000, "0.18"),
        ]
        for price, quantity, expected in vectors:
            with self.subTest(price=price, quantity=quantity):
                result = estimate_fee("KXTEST-1", Decimal(price), quantity, "maker")
                self.assertEqual(result["fee"], Decimal(expected))
                self.assertEqual(result["fee_type"], "maker")
                self.assertEqual(result["fee_status"], "estimated")

    def test_cent_ceiling_exact_cent_and_fractional_cent_vectors(self):
        # At P=$0.50 and 400 contracts, the raw fees are exactly $7.00 taker
        # and $1.75 maker. At one contract, raw fees $0.0175/$0.004375 must
        # round upward to $0.02/$0.01, never round down or to nearest.
        vectors = [
            ("0.50", 400, "taker", "7.00"),
            ("0.50", 400, "maker", "1.75"),
            ("0.50", 1, "taker", "0.02"),
            ("0.50", 1, "maker", "0.01"),
        ]
        for price, quantity, fee_type, expected in vectors:
            with self.subTest(price=price, quantity=quantity, fee_type=fee_type):
                self.assertEqual(
                    estimate_fee("KXTEST-1", Decimal(price), quantity, fee_type)["fee"],
                    Decimal(expected),
                )

    def test_price_extremes_and_maximum_quantity(self):
        # The accepted boundaries are open: $0 and $1 are invalid, while
        # $0.0001 and $0.9999 at 1,000 contracts both ceil to one cent.
        for price in ("0.0001", "0.9999"):
            for fee_type in ("taker", "maker"):
                with self.subTest(price=price, fee_type=fee_type):
                    self.assertEqual(
                        estimate_fee("KXTEST-1", Decimal(price), 1000, fee_type)["fee"],
                        Decimal("0.01"),
                    )
        for price in ("0", "1"):
            with self.subTest(invalid_price=price), self.assertRaises(ValueError):
                estimate_fee("KXTEST-1", Decimal(price), 1, "taker")

    def test_inx_and_nasdaq100_special_fee_schedules_are_unavailable(self):
        # These prefixes must not silently receive the general estimated rate.
        for ticker in ("INX-TEST", "inx-test", "NASDAQ100-TEST", "nasdaq100-test"):
            for fee_type in ("taker", "maker"):
                with self.subTest(ticker=ticker, fee_type=fee_type):
                    result = estimate_fee(ticker, Decimal("0.50"), 10, fee_type)
                    self.assertIsNone(result["fee"])
                    self.assertEqual(result["fee_type"], fee_type)
                    self.assertEqual(result["fee_status"], "unavailable")

    def test_quantity_boundaries(self):
        # Quantity 1 and 1,000 are valid; 0 and 1,001 exceed the supported range.
        self.assertEqual(estimate_fee("KXTEST-1", Decimal("0.50"), 1, "taker")["fee"], Decimal("0.02"))
        self.assertEqual(estimate_fee("KXTEST-1", Decimal("0.50"), 1000, "taker")["fee"], Decimal("17.50"))
        for quantity in (0, 1001, True):
            with self.subTest(quantity=quantity), self.assertRaises(ValueError):
                estimate_fee("KXTEST-1", Decimal("0.50"), quantity, "taker")


if __name__ == "__main__":
    unittest.main()
