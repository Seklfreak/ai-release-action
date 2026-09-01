#!/usr/bin/env python3
"""Tests for dispatch_builds.sh, the post-release build dispatch.

A wrong path filter fails silently — the build simply never happens — so the
matching decision is asserted rather than trusted. Each test builds a throwaway
git repo, puts a fake `gh` on PATH that records its arguments, and checks which
workflows were dispatched.

Run: python3 tests/test_dispatch_builds.py
"""
import contextlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "dispatch_builds.sh"


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class Repo:
    """A throwaway git repo tagged v1.0.0, plus a fake `gh` that logs calls."""

    def __init__(self, stack):
        self.dir = stack.enter_context(tempfile.TemporaryDirectory())
        git(self.dir, "init", "-q")
        git(self.dir, "config", "user.email", "t@example.com")
        git(self.dir, "config", "user.name", "Test")
        self.commit("backend/main.go", "package main", "initial")
        git(self.dir, "tag", "v1.0.0")

        self.bin = Path(self.dir) / ".fakebin"
        self.bin.mkdir()
        self.log = Path(self.dir) / "gh.log"
        gh = self.bin / "gh"
        gh.write_text(f'#!/bin/sh\necho "$*" >> "{self.log}"\n')
        gh.chmod(0o755)

    def commit(self, path, content, message):
        f = Path(self.dir) / path
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content)
        git(self.dir, "add", "-A")
        git(self.dir, "commit", "-q", "-m", message)

    def run(self, build_workflow, base="v1.0.0", ref="v1.1.0"):
        """Dispatch as the action would, and return (dispatched, stderr)."""
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.dir, capture_output=True, text=True
        ).stdout.strip()
        p = subprocess.run(
            ["bash", str(SCRIPT), ref, ref.lstrip("v"), base, head],
            cwd=self.dir,
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
                "BUILD_WORKFLOW": build_workflow,
            },
        )
        calls = self.log.read_text().splitlines() if self.log.exists() else []
        return [c.split()[2] for c in calls], p


class TestDispatchBuilds(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.repo = Repo(self.stack)

    def test_unfiltered_entries_always_dispatch(self):
        """The behaviour every caller had before filters existed."""
        self.repo.commit("docs/x.md", "hi", "docs")
        got, p = self.repo.run("build.yaml testflight.yaml")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(got, ["build.yaml", "testflight.yaml"])

    def test_filtered_entry_is_skipped_when_its_paths_are_untouched(self):
        self.repo.commit("backend/main.go", "package main // v2", "fix backend")
        got, p = self.repo.run("build.yaml\ntestflight.yaml:^ios/")
        self.assertEqual(got, ["build.yaml"])
        self.assertIn("not dispatching testflight.yaml", p.stdout)

    def test_filtered_entry_dispatches_when_its_paths_change(self):
        self.repo.commit("ios/App.swift", "import SwiftUI", "ios: tweak")
        got, _ = self.repo.run("build.yaml\ntestflight.yaml:^ios/")
        self.assertEqual(got, ["build.yaml", "testflight.yaml"])

    def test_a_mixed_release_dispatches_both(self):
        self.repo.commit("backend/main.go", "package main // v2", "fix backend")
        self.repo.commit("ios/App.swift", "import SwiftUI", "ios: tweak")
        got, _ = self.repo.run("build.yaml\ntestflight.yaml:^ios/")
        self.assertEqual(got, ["build.yaml", "testflight.yaml"])

    def test_a_missing_baseline_dispatches_unfiltered(self):
        """First release: there is no previous tag to diff against."""
        self.repo.commit("backend/main.go", "package main // v2", "fix backend")
        got, p = self.repo.run("testflight.yaml:^ios/", base="v0.0.0")
        self.assertEqual(got, ["testflight.yaml"])
        self.assertIn("without its", p.stdout)

    def test_a_prefix_only_filter_does_not_match_mid_path(self):
        """'^ios/' must not fire on backend/ios/, or the anchor is decorative."""
        self.repo.commit("backend/ios/notes.go", "package ios", "backend")
        got, _ = self.repo.run("testflight.yaml:^ios/")
        self.assertEqual(got, [])

    def test_no_build_workflow_dispatches_nothing(self):
        self.repo.commit("ios/App.swift", "import SwiftUI", "ios")
        got, p = self.repo.run("")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(got, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
