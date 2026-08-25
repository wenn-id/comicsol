import unittest
from unittest.mock import patch

from scripts.schema import (
    CURRENT_PROJECT_SCHEMA_VERSION,
    MIN_READER_PROJECT_SCHEMA_VERSION,
    SUPPORTED_PROJECT_SCHEMA_VERSIONS,
    UnsupportedSchemaVersionError,
    schema_version_error,
)


class SchemaVersionParserTests(unittest.TestCase):
    """Schema versions compare numerically, never lexicographically.

    Lexicographic comparison misdiagnoses double-digit versions: ``"10.0"``
    sorts *below* ``"2.0"`` as text, so a future reader would call a supported
    current version unsupported and vice versa. The parser must order by
    (major, minor) integers instead.
    """

    def test_supported_version_has_no_error(self):
        self.assertEqual("1.1", CURRENT_PROJECT_SCHEMA_VERSION)
        self.assertIsNone(schema_version_error(CURRENT_PROJECT_SCHEMA_VERSION))
        self.assertIsNone(schema_version_error("1.0"))

    def test_newer_double_digit_version_is_reported_as_newer(self):
        error = schema_version_error("10.0")
        self.assertIsInstance(error, UnsupportedSchemaVersionError)
        self.assertIn("upgrade Comic Sol", str(error))

    def test_double_digit_minor_orders_numerically(self):
        error = schema_version_error("1.10")
        self.assertIsInstance(error, UnsupportedSchemaVersionError)
        # "1.10" is newer than "1.1" numerically but older lexicographically;
        # the parser must treat it as newer than the current reader.
        self.assertIn("upgrade Comic Sol", str(error))

    def test_older_version_without_migration_is_unsupported(self):
        error = schema_version_error("0.9")
        self.assertIsInstance(error, UnsupportedSchemaVersionError)
        self.assertIn("no migration path", str(error))

    def test_version_between_reader_and_current_without_migration_fails_closed(self):
        # "1.5" is newer than 1.1 (and newer than any 1.x the reader knows) but
        # has no registered migration, so it must fail closed rather than
        # being treated as an old readable version.
        error = schema_version_error("1.5")
        self.assertIsInstance(error, UnsupportedSchemaVersionError)

    def test_non_string_and_non_version_strings_are_unsupported(self):
        for version in (None, 1.0, ["1.0"], "1", "1.0.0", "v1.0", "1.x", "", " 1.0"):
            with self.subTest(version=version):
                error = schema_version_error(version)
                self.assertIsInstance(error, UnsupportedSchemaVersionError)
                if not isinstance(version, str):
                    self.assertIn("must be a string", str(error))
                else:
                    self.assertIn("invalid version format", str(error))

    def test_registered_version_constants_parse_cleanly(self):
        for version in SUPPORTED_PROJECT_SCHEMA_VERSIONS | {MIN_READER_PROJECT_SCHEMA_VERSION}:
            with self.subTest(version=version):
                self.assertIsNone(schema_version_error(version))

    def test_reader_rejects_manifest_version_above_current_reader_support(self):
        # The reader supports at most 1.1 today; 2.0 must be a "newer" error,
        # not an "older/no migration" error, even though "2.0" < "10.0" both ways.
        error = schema_version_error("2.0")
        self.assertIn("upgrade Comic Sol", str(error))

    def test_major_roll_over_orders_numerically_at_the_reader_boundary(self):
        # With a future 9.0 reader, a 10.0 project must be NEWER (upgrade
        # needed). Lexicographic order says "10.0" < "9.0" and would flip the
        # diagnosis to "no migration path" instead.
        with (
            patch("scripts.schema.CURRENT_PROJECT_SCHEMA_VERSION", "9.0"),
            patch("scripts.schema.SUPPORTED_PROJECT_SCHEMA_VERSIONS", frozenset({"9.0"})),
        ):
            error = schema_version_error("10.0")
        self.assertIsInstance(error, UnsupportedSchemaVersionError)
        self.assertIn("upgrade Comic Sol", str(error))

    def test_double_digit_minor_orders_numerically_at_the_reader_boundary(self):
        # With a future 1.9 reader, a 1.10 project must be NEWER. Lexicographic
        # order says "1.10" < "1.9" and would report the opposite direction.
        with (
            patch("scripts.schema.CURRENT_PROJECT_SCHEMA_VERSION", "1.9"),
            patch("scripts.schema.SUPPORTED_PROJECT_SCHEMA_VERSIONS", frozenset({"1.9"})),
        ):
            error = schema_version_error("1.10")
        self.assertIsInstance(error, UnsupportedSchemaVersionError)
        self.assertIn("upgrade Comic Sol", str(error))


if __name__ == "__main__":
    unittest.main()
