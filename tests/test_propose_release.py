#!/usr/bin/env python3
"""Tests for propose_release.py, exercising the no-API fallback paths.

These run without an ANTHROPIC_API_KEY on purpose: the fallback is what protects a
release when the API is down, so it is the part that must not regress. Each test
builds a throwaway git repo, commits fixture files, and asserts the chosen bump.

Run: python3 tests/test_propose_release.py
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "propose_release.py"
DESCRIPTION = "A widget service. Everything outside docs/ and scripts/ ships to users."


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class Repo:
    """A throwaway git repo with a v1.0.0 baseline tag."""

    def __init__(self, stack):
        self.dir = stack.enter_context(tempfile.TemporaryDirectory())
        git(self.dir, "init", "-q")
        git(self.dir, "config", "user.email", "t@example.com")
        git(self.dir, "config", "user.name", "Test")
        self.commit("main.go", "package main", "initial")
        git(self.dir, "tag", "v1.0.0")

    def commit(self, path, content, message):
        f = Path(self.dir) / path
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content)
        git(self.dir, "add", "-A")
        git(self.dir, "commit", "-q", "-m", message)

    def run(self, prev="v1.0.0", **env):
        out = Path(self.dir) / "release.json"
        e = {
            **os.environ,
            "PROJECT_DESCRIPTION": DESCRIPTION,
            "RELEASE_JSON": str(out),
            **env,
        }
        e.pop("ANTHROPIC_API_KEY", None)
        p = subprocess.run(
            [sys.executable, str(SCRIPT), prev],
            cwd=self.dir,
            capture_output=True,
            text=True,
            env=e,
        )
        notes = json.loads(out.read_text())["notes"] if out.exists() else None
        return p.returncode, p.stdout.strip(), notes


class TestProposeRelease(unittest.TestCase):
    def setUp(self):
        import contextlib

        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.repo = Repo(self.stack)

    def test_missing_description_is_a_hard_error(self):
        """The bug this action exists to prevent must fail loudly, not default."""
        code, _, _ = self.repo.run(PROJECT_DESCRIPTION="")
        self.assertEqual(code, 2)

    def test_empty_range_never_releases(self):
        """Tag already at HEAD, as after a manual baseline tag."""
        _, bump, _ = self.repo.run()
        self.assertEqual(bump, "none")

    def test_docs_only_change_is_skipped(self):
        self.repo.commit("README.md", "# hi", "docs: readme")
        _, bump, _ = self.repo.run()
        self.assertEqual(bump, "none")

    def test_shipping_change_falls_back_to_patch(self):
        self.repo.commit("main.go", "package main // v2", "fix: thing")
        _, bump, notes = self.repo.run()
        self.assertEqual(bump, "patch")
        self.assertIn("fix: thing", notes)

    def test_mixed_change_releases(self):
        """A docs change riding along with a code change still ships."""
        self.repo.commit("README.md", "# hi", "docs")
        self.repo.commit("main.go", "package main // v2", "fix")
        _, bump, _ = self.repo.run()
        self.assertEqual(bump, "patch")

    def test_embedded_asset_ships(self):
        """The routined case: a template embedded in the binary is not 'just an asset'."""
        self.repo.commit("internal/httpapi/ui.html", "<html>", "ui: logo")
        _, bump, _ = self.repo.run()
        self.assertEqual(bump, "patch")

    def test_non_shipping_extra_is_honoured(self):
        self.repo.commit("research/notebook.ipynb", "{}", "research")
        _, bump, _ = self.repo.run(NON_SHIPPING_EXTRA="(^research/)")
        self.assertEqual(bump, "none")

    def test_non_shipping_extra_does_not_leak(self):
        """Without the extra pattern, the same path ships."""
        self.repo.commit("research/notebook.ipynb", "{}", "research")
        _, bump, _ = self.repo.run()
        self.assertEqual(bump, "patch")


if __name__ == "__main__":
    unittest.main(verbosity=2)
