import unittest

import _support  # noqa: F401
from value_coercion import coerce_optional_limit_price


class CoerceOptionalLimitPrice(unittest.TestCase):
    def test_absent_returns_none(self):
        self.assertIsNone(coerce_optional_limit_price(None))

    def test_positive_float_passthrough(self):
        self.assertEqual(coerce_optional_limit_price(1.25), 1.25)
        self.assertEqual(coerce_optional_limit_price("2.5"), 2.5)

    def test_numeric_error_is_parameterized(self):
        with self.assertRaisesRegex(ValueError, "AMD: limit_price must be numeric"):
            coerce_optional_limit_price(
                "nope",
                numeric_error="AMD: limit_price must be numeric",
            )

    def test_positive_error_is_parameterized(self):
        with self.assertRaisesRegex(ValueError, "AMD: limit_price must be positive"):
            coerce_optional_limit_price(
                0,
                positive_error="AMD: limit_price must be positive",
            )

    def test_without_positive_error_allows_non_positive(self):
        self.assertEqual(coerce_optional_limit_price(0), 0.0)
        self.assertEqual(coerce_optional_limit_price(-1.5), -1.5)

    def test_without_numeric_error_preserves_float_failure(self):
        with self.assertRaises(ValueError):
            coerce_optional_limit_price("nope")


if __name__ == "__main__":
    unittest.main()
