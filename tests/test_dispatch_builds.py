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
        # The fake fails the first FAIL_RUNS `workflow run` calls the way
        # GitHub does when its API hiccups, and answers `run list` with a run
        # id when RUN_EXISTS is set -- the two halves of a dispatch that has
        # to be verified rather than trusted.
        self.fail_file = Path(self.dir) / "fail-runs"
        gh = self.bin / "gh"
        gh.write_text(
            "#!/bin/sh\n"
            f'echo "$*" >> "{self.log}"\n'
            'case "$1 $2" in\n'
            '  "workflow run")\n'
            f'    n=$(cat "{self.fail_file}" 2>/dev/null || echo 0)\n'
            '    if [ "$n" -gt 0 ]; then\n'
            f'      echo $((n - 1)) > "{self.fail_file}"\n'
            '      echo "could not create workflow dispatch event: HTTP 500" >&2\n'
            "      exit 1\n"
            "    fi ;;\n"
            '  "run list")\n'
            '    [ "${FAKE_GH_RUN_EXISTS:-}" = 1 ] && echo 4242 ;;\n'
            "esac\n"
            "exit 0\n"
        )
        gh.chmod(0o755)

    def commit(self, path, content, message):
        f = Path(self.dir) / path
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content)
        git(self.dir, "add", "-A")
        git(self.dir, "commit", "-q", "-m", message)

    def run(self, build_workflow, base="v1.0.0", ref="v1.1.0", fail_runs=0, run_exists=False):
        """Dispatch as the action would, and return (dispatched, result).

        `dispatched` lists the workflow of every `gh workflow run` attempt, in
        order, so a retry shows up as a repeat.
        """
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.dir, capture_output=True, text=True
        ).stdout.strip()
        self.fail_file.write_text(str(fail_runs))
        p = subprocess.run(
            ["bash", str(SCRIPT), ref, ref.lstrip("v"), base, head],
            cwd=self.dir,
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
                "BUILD_WORKFLOW": build_workflow,
                "DISPATCH_RETRY_DELAY": "0",
                "FAKE_GH_RUN_EXISTS": "1" if run_exists else "",
            },
        )
        calls = self.log.read_text().splitlines() if self.log.exists() else []
        return [c.split()[2] for c in calls if c.startswith("workflow run ")], p


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

    def test_a_dispatch_error_after_the_run_was_created_is_not_a_failure(self):
        """GitHub answered a 500 with the TestFlight run already on its way,
        and the release went red with everything built. The run is what
        counts, not the answer."""
        got, p = self.repo.run("build.yaml testflight.yaml", fail_runs=1, run_exists=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(got, ["build.yaml", "testflight.yaml"])
        self.assertIn("running for v1.1.0 after all", p.stdout)

    def test_a_dispatch_error_with_no_run_is_retried(self):
        got, p = self.repo.run("build.yaml", fail_runs=1)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(got, ["build.yaml", "build.yaml"])

    def test_a_dispatch_that_never_lands_fails_the_release(self):
        """A release without its build must not pass quietly."""
        got, p = self.repo.run("build.yaml", fail_runs=5)
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(got, ["build.yaml"] * 3)
        self.assertIn("Could not dispatch build.yaml", p.stdout)

    def test_no_build_workflow_dispatches_nothing(self):
        self.repo.commit("ios/App.swift", "import SwiftUI", "ios")
        got, p = self.repo.run("")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(got, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
