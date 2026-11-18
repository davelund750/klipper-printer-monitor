"""Tests for the version and changelog shown on Settings' About tab.
Run with: python3 -m unittest discover -s tests"""

import importlib
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server"))


class VersionTest(unittest.TestCase):
    def test_describing_the_version(self):
        server = importlib.import_module("server")
        for described, shown in [
            ("v1.2.0", "1.2.0"),  # a kiosk, on a release
            ("v1.2.0-1-gabc1234", "1.2.0 + 1 change (abc1234)"),
            ("v1.2.0-3-gabc1234", "1.2.0 + 3 changes (abc1234)"),
            ("v1.2.0-3-gabc1234-dirty", "1.2.0 + 3 changes (abc1234), edited"),
            ("abc1234", "development (abc1234)"),  # before any release
            ("abc1234-dirty", "development (abc1234)"),
            ("", "unknown"),  # not a git copy
        ]:
            with self.subTest(described=described):
                self.assertEqual(server.describe_version(described), shown)


class ChangelogTest(unittest.TestCase):
    SAMPLE = """# Changelog

Intro text for developers.

## Unreleased

### Added
- Not installed yet.

## 1.1.0 - 2026-10-04

### Added
- A [linked](README.md#x) thing with `code`, **bold**, and *italics*,
  wrapped onto a second line.
- Another thing.

### Fixed
- A fix.

## 2026-09-25: first release

The start.

### Dashboard
- Wait times.
"""

    def test_releases_newest_first_without_unreleased(self):
        server = importlib.import_module("server")
        newer, older = server.parse_changelog(self.SAMPLE)
        self.assertEqual((newer["version"], newer["date"], newer["label"]), ("1.1.0", "2026-10-04", None))
        self.assertEqual(newer["sections"], [
            {"title": "Added", "items": [
                "A linked thing with code, bold, and italics, wrapped onto a second line.",
                "Another thing.",
            ]},
            {"title": "Fixed", "items": ["A fix."]},
        ])
        self.assertEqual((older["version"], older["date"], older["label"]), (None, "2026-09-25", "first release"))
        self.assertEqual(older["notes"], ["The start."])
        self.assertEqual(older["sections"], [{"title": "Dashboard", "items": ["Wait times."]}])

    def test_the_real_changelog_reads(self):
        server = importlib.import_module("server")
        # Before the first release, there's only "Unreleased", which is left out.
        for release in server.read_changelog():
            with self.subTest(release=release["version"] or release["date"]):
                self.assertTrue(release["sections"])
                for section in release["sections"]:
                    self.assertTrue(section["items"])
                    for item in section["items"]:
                        self.assertNotIn("`", item)
                        self.assertNotRegex(item, r"\]\(")


if __name__ == "__main__":
    unittest.main()
