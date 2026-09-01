# ai-release-action

A composite GitHub Action that decides the next semantic-version bump for a green
commit, writes the release notes, tags it, publishes a GitHub release, and
optionally dispatches an image build.

The bump decision is made by Claude from the commit range and diffstat, with a
deterministic fallback so a release is never blocked on the API being reachable.

## Why the description is a required input

This started as a script copy-pasted between repos, with the project description
baked in as a constant. One copy kept the wrong project's description — it told the
classifier the project was a Telegram bot when it was actually a scheduler with a web
dashboard — and a dashboard change was classified `none`. The commit went green,
built nothing, and never deployed. Nothing failed; the release simply never happened.

The classifier's core question is *"can this change reach a user?"*, which is
unanswerable without knowing what the project ships. So `project-description` is a
required input with no default: a repo must state its own identity, and a wrong one is
visible in the workflow file rather than buried in a vendored script.

## Usage

```yaml
name: Release

# Release green commits: run after the test workflow succeeds on main.
on:
  workflow_run:
    workflows: [Test]
    types: [completed]

permissions:
  contents: write   # push the tag, create the release
  actions: write    # dispatch the build workflow

jobs:
  release:
    if: ${{ github.event.workflow_run.conclusion == 'success' }}
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
        with:
          ref: ${{ github.event.workflow_run.head_sha }}
          fetch-depth: 0      # required: the bump is read from the commit range
          fetch-tags: true    # required: the previous tag is the baseline

      - uses: Seklfreak/ai-release-action@v1
        with:
          sha: ${{ github.event.workflow_run.head_sha }}
          anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}
          build-workflow: build.yaml
          project-description: >-
            myproject is a self-hosted widget service shipped as a single Go binary
            in a container image. The web dashboard at internal/ui.html is embedded
            into the binary, so visual changes to it ship to users. Only docs/ and
            scripts/ live outside the image.
```

## Inputs

| Input | Required | Default | Notes |
|---|---|---|---|
| `project-description` | **yes** | — | What the project is and which paths ship. See above. |
| `anthropic-api-key` | no | `""` | Omit to always use the fallback path. |
| `sha` | no | `github.sha` | Commit to release. |
| `github-token` | no | `github.token` | Needs `contents: write`. |
| `project-name` | no | repo name | Name used in the prompt. |
| `model` | no | `claude-haiku-4-5` | Bump classification is not a hard task. |
| `non-shipping-extra` | no | `""` | Extra regex alternatives, e.g. `(^research/)`. Fallback path only. |
| `tag-prefix` | no | `v` | |
| `build-workflow` | no | `""` | Workflow(s) dispatched after release, whitespace-separated; empty to skip. An entry may carry a `:regex` path filter — see below. |
| `dry-run` | no | `false` | Decide and print notes, create nothing. |

## Outputs

| Output | Notes |
|---|---|
| `bump` | `major`, `minor`, `patch` or `none`. |
| `version` | New tag, empty when nothing was released. |
| `released` | `true` when a tag and release were created. |

## Bump criteria

- **none** — cannot reach a user of the deployed project: docs, CI workflows,
  repo-side tooling, comments, formatting. Judged against your description.
- **patch** — bug fixes, refactors, chores, dependency bumps that reach users.
- **minor** — new user-facing features or notable backwards-compatible enhancements.
- **major** — breaking changes to the config or deploy contract. Below 1.0.0 the
  action prefers minor unless clearly breaking; that guidance is derived from the
  previous tag, so it stops applying on its own at 1.0.0.

## Fallback behaviour

With no API key, or on any API error, the action never fails the release:

- changes touching **only** non-shipping paths → `none`
- anything else → `patch`, with the raw commit list as notes

The non-shipping set is `*.md`, `.github/`, `.golangci*`, `docs/`, `scripts/`,
`LICENSE`, `.gitignore`, plus whatever `non-shipping-extra` adds. Note this list is
consulted only on the fallback path — when the API is available the model decides,
which is why the description matters more than the regex.

## Requirements

The calling job must check out **full history with tags** (`fetch-depth: 0`,
`fetch-tags: true`). A shallow checkout makes the commit range wrong, so the action
fails fast rather than releasing on a bad diff.

`GITHUB_TOKEN` tag pushes don't trigger workflows. If a tag push is meant to build an
image, pass `build-workflow` and the action dispatches it explicitly. Pass several
(whitespace- or newline-separated) when one release drives more than one build.

## Dispatching only the builds a release actually changed

A repo that ships more than one artifact rarely changes both in the same release. A
`build-workflow` entry may carry a path filter after a colon, and is then dispatched
only when the released commit range touches a matching file:

```yaml
build-workflow: |
  build.yaml
  testflight.yaml:^ios/
```

Here a backend-only release builds the image and leaves the unchanged iOS app alone —
which saves a macOS runner, and, more usefully, saves the testers a build notification
and Apple a review of an app nobody touched. Entries without a filter always dispatch,
so this changes nothing for existing callers.

The filter is an ERE matched against paths relative to the repo root, and must contain
no whitespace (entries are whitespace-separated). Anchor it: `^ios/` and not `ios/`,
or it fires on `backend/ios/`. Skips are announced as workflow notices, because the
failure mode of a filter that never matches is a build that silently stops happening.

Filtering is skipped when the previous tag doesn't exist — on a first release there is
no baseline to diff against, so everything is dispatched.

## Development

```sh
python3 tests/test_propose_release.py
python3 tests/test_dispatch_builds.py
```

The tests build throwaway git repos and assert the fallback classification, including
a regression test for the embedded-asset case that caused this extraction, and the
build-dispatch filtering against a fake `gh`.
