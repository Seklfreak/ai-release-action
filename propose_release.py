#!/usr/bin/env python3
"""Decide the next semver bump + write release notes for the commits since the last
release, using Claude. Writes {"bump","notes"} to release.json and prints the bump.

Falls back to a patch bump with the raw commit list if no API key is set or the API
call fails, so auto-releases never block on the AI step.

The project description is a required input rather than a constant baked into this
file. That is deliberate: this script started life copy-pasted between repos, and a
copy carrying the wrong project's description silently suppressed real releases —
the classifier answered correctly for a project it had been misdescribed as.

Usage: propose_release.py <previous-tag>
Env:   PROJECT_DESCRIPTION (required)  what the project is and what ships
       PROJECT_NAME                    defaults to the GITHUB_REPOSITORY name
       ANTHROPIC_API_KEY               absent -> fallback path, never blocks
       HEAD_SHA                        defaults to HEAD
       RELEASE_MODEL                   defaults to claude-haiku-4-5
       NON_SHIPPING_EXTRA              extra regex alternatives, e.g. "(^research/)"
       RELEASE_JSON                    output path, defaults to release.json
"""
import json
import os
import re
import subprocess
import sys

PREV = sys.argv[1] if len(sys.argv) > 1 else ""
HEAD = os.environ.get("HEAD_SHA") or "HEAD"
OUT = os.environ.get("RELEASE_JSON") or "release.json"
DESCRIPTION = (os.environ.get("PROJECT_DESCRIPTION") or "").strip()
NAME = (os.environ.get("PROJECT_NAME") or "").strip() or (
    os.environ.get("GITHUB_REPOSITORY", "").split("/")[-1] or "this project"
)


def git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True).stdout.strip()


def write(bump: str, notes: str) -> None:
    json.dump({"bump": bump, "notes": notes}, open(OUT, "w"))
    print(bump)


# A missing description is the failure mode this action exists to prevent, so it is
# a hard error rather than a silent default.
if not DESCRIPTION:
    sys.stderr.write(
        "PROJECT_DESCRIPTION is empty. Set it to a short description of what this "
        "project is and which paths actually ship to users.\n"
    )
    sys.exit(2)

commits = git("log", f"{PREV}..{HEAD}" if PREV else HEAD, "--no-merges", "--pretty=format:- %s")
stat = git("diff", "--stat", f"{PREV}..{HEAD}") if PREV else git("show", "--stat", "--oneline", HEAD)
files = [
    f
    for f in (
        git("diff", "--name-only", f"{PREV}..{HEAD}")
        if PREV
        else git("show", "--name-only", "--pretty=format:", HEAD)
    ).splitlines()
    if f
]

# Nothing in the range (e.g. the release tag already sits at HEAD, as after a manual
# baseline tag): never release.
if PREV and not commits and not files:
    write("none", "")
    sys.exit(0)

# Non-shipping paths: changes touching only these don't warrant a release. Repos add
# their own via NON_SHIPPING_EXTRA (dev harnesses, notebooks, and the like). Note this
# is only consulted on the fallback path -- with the API available, the model decides.
NON_SHIPPING = re.compile(
    r"(\.md$)|(^\.github/)|(^\.golangci)|(^docs/)|(^scripts/)|(^LICENSE)|(^\.gitignore$)"
    + (f"|{os.environ['NON_SHIPPING_EXTRA']}" if os.environ.get("NON_SHIPPING_EXTRA") else "")
)


def fallback(reason: str) -> None:
    # Without the AI, skip releases for changes touching only non-shipping paths;
    # otherwise default to patch so a release is never silently lost.
    if files and all(NON_SHIPPING.search(f) for f in files):
        sys.stderr.write(f"AI step skipped ({reason}); non-shipping changes -> no release.\n")
        write("none", "")
        return
    sys.stderr.write(f"AI step skipped ({reason}); defaulting to patch bump.\n")
    write("patch", f"## Changes\n\n{commits or 'Maintenance release.'}\n")


if not os.environ.get("ANTHROPIC_API_KEY"):
    fallback("ANTHROPIC_API_KEY not set")
    sys.exit(0)

try:
    import anthropic

    tool = {
        "name": "propose_release",
        "description": "Propose the semantic-version bump and release notes.",
        "input_schema": {
            "type": "object",
            "properties": {
                "bump": {"type": "string", "enum": ["major", "minor", "patch", "none"]},
                "notes": {
                    "type": "string",
                    "description": "User-facing markdown release notes (empty when bump is none). "
                    "Group into sections (e.g. Features / Fixes / Maintenance) as relevant; "
                    "omit empty sections; be concise.",
                },
            },
            "required": ["bump", "notes"],
        },
    }

    # Pre-1.0 guidance is derived from the previous tag rather than configured, so it
    # stops applying on its own at 1.0.0.
    pre_1_0 = PREV.lstrip("v").startswith("0.") if PREV else True
    major_rule = (
        "- major: only genuinely breaking changes to the config/deploy contract.\n"
        "  Pre-1.0, strongly prefer minor over major unless clearly breaking."
        if pre_1_0
        else "- major: genuinely breaking changes to the public or deploy contract."
    )

    prompt = f"""You decide the next semantic-version bump and write release notes for {NAME}.

{DESCRIPTION}

Choose the bump:
- none: SKIP the release entirely when the changes cannot reach a user of the
  deployed project — e.g. documentation/markdown only, CI workflow changes only,
  repo-side tooling only, comments, or formatting. Judge this against the project
  description above, not against assumptions about what the project is. When
  genuinely in doubt, prefer patch over none.
- patch: bug fixes, refactors, chores, dependency bumps that reach users.
- minor: new user-facing features or notable backwards-compatible enhancements.
{major_rule}

Previous release: {PREV or "(none yet)"}

Commits:
{commits or "(no commit messages)"}

Files changed:
{stat or "(none)"}

Call propose_release with the bump and concise, user-facing markdown notes."""

    client = anthropic.Anthropic()
    msg = client.messages.create(
        # Haiku is plenty for bump-classification + concise notes; override if desired.
        model=os.environ.get("RELEASE_MODEL", "claude-haiku-4-5"),
        max_tokens=2000,
        tools=[tool],
        tool_choice={"type": "tool", "name": "propose_release"},
        messages=[{"role": "user", "content": prompt}],
    )
    result = next(b.input for b in msg.content if b.type == "tool_use")
    bump = result["bump"] if result.get("bump") in ("major", "minor", "patch", "none") else "patch"
    write(bump, "" if bump == "none" else (result.get("notes") or commits or "Maintenance release."))
except Exception as e:  # noqa: BLE001 — never block a release on the AI step
    fallback(f"API error: {e}")
