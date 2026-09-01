#!/usr/bin/env bash
# Dispatch the post-release build workflows.
#
# GITHUB_TOKEN tag pushes don't trigger `on: push: tags`, so every build a
# release is meant to produce has to be dispatched explicitly.
#
# Entries in BUILD_WORKFLOW are "<workflow>[:<regex>]", separated by whitespace
# or newlines. With a regex, the workflow is dispatched only when the released
# range touches a file matching it (ERE, matched against paths relative to the
# repo root). That keeps a release from spending an expensive runner on an
# artifact none of its commits changed -- a backend-only release rebuilding an
# untouched iOS app, say. Without one, the workflow is always dispatched, which
# is the behaviour every caller had before filters existed.
#
# A skip is announced as a workflow notice: the failure mode of a wrong filter
# is a build that silently never ships, so it must be visible in the run log.
#
# Usage: dispatch_builds.sh <ref> <version> <base> <head>
# Env:   BUILD_WORKFLOW  the entries; empty means dispatch nothing
#        GH_TOKEN        token for `gh workflow run`
set -euo pipefail

REF=$1        # tag to dispatch against, e.g. v1.2.3
VERSION=$2    # same without the prefix, passed as -f version=
BASE=$3       # previous tag; may not exist on a first release
HEAD_SHA=$4   # the released commit

for entry in ${BUILD_WORKFLOW:-}; do
  wf=${entry%%:*}
  filter=""
  [ "$entry" != "$wf" ] && filter=${entry#*:}

  if [ -n "$filter" ]; then
    if ! git rev-parse -q --verify "$BASE^{commit}" >/dev/null; then
      # No baseline to diff against (first release): filtering would compare
      # against nothing and skip everything, so dispatch instead.
      echo "::notice::$BASE is not a commit — dispatching $wf without its '$filter' filter."
    elif ! git diff --name-only "$BASE".."$HEAD_SHA" | grep -Eq "$filter"; then
      echo "::notice::Nothing in $BASE..$REF matches '$filter' — not dispatching $wf."
      continue
    fi
  fi

  echo "Dispatching $wf for $REF"
  gh workflow run "$wf" --ref "$REF" -f version="$VERSION"
done
