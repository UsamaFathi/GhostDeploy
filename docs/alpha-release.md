# Maintainer checklist for GhostDeploy 0.3.0a1

`0.3.0a1` is the approved package version for preparation. `v0.3.0a1` is the proposed Git tag. The first public release is a GitHub **pre-release** for disposable staging evaluation. Publication requires separate, explicit maintainer approval. [Release notes](releases/0.3.0a1.md) · [Release gates](testing.md#release-gate)

Use increasing PEP 440 Alpha numbers for distinct public candidates and preserve published version identifiers. Each public artifact set must correspond to the exact reviewed and tagged commit. [PEP 440 version specifiers](https://packaging.python.org/en/latest/specifications/version-specifiers/)

## Review and verify the final main commit

1. Review the changes on `release/0.3.0a1`, the candidate checksums and branch CI. Confirm the release notes disclose the pending external SSH gate.
2. After review, merge the approved pull request using the repository's normal controls. Fetch and check out the merged `main` in a clean clone.
3. Run the following in a POSIX shell with Git, Python 3.11+, authenticated GitHub CLI and approval to proceed. An absent run ID or failed check means stop and resolve it.

```sh
set -eu
git fetch origin main
git switch --detach origin/main
test -z "$(git status --porcelain)"
RELEASE_SHA=$(git rev-parse HEAD)
CI_RUN=$(gh run list --branch main --commit "$RELEASE_SHA" --workflow ci.yml \
  --limit 1 --json databaseId --jq '.[0].databaseId // empty')
test -n "$CI_RUN"
gh run watch "$CI_RUN" --exit-status
test "$(gh run view "$CI_RUN" --json headSha,event,conclusion \
  --jq '[.headSha, .event, .conclusion] | join(" ")')" = "$RELEASE_SHA push success"
```

## Build and verify from the reviewed commit

Build from a tracked-source export in a fresh directory. Use these artifacts for the pre-release. Run the [quality checks](testing.md#hermetic-suite) and [fresh installation walkthrough](quick-start.md) against the final commit and wheel. On a machine used for offline checks, `doctor` may report missing host prerequisites. Exercise successful host diagnostics on a disposable Ubuntu Odoo environment.

```sh
WORK=$(mktemp -d)
mkdir "$WORK/source" "$WORK/dist"
git archive --format=tar --output="$WORK/source.tar" "$RELEASE_SHA"
tar -xf "$WORK/source.tar" -C "$WORK/source"
python3 -m venv "$WORK/tools"
"$WORK/tools/bin/python" -m pip install 'build>=1.2,<2' 'twine>=6,<7'
(cd "$WORK/source" && "$WORK/tools/bin/python" -m build --outdir "$WORK/dist")
WHEEL=ghostdeploy-0.3.0a1-py3-none-any.whl
SDIST=ghostdeploy-0.3.0a1.tar.gz
"$WORK/tools/bin/python" -m twine check --strict "$WORK/dist/$WHEEL" "$WORK/dist/$SDIST"
(cd "$WORK/dist" && sha256sum "$WHEEL" "$SDIST" > SHA256SUMS && sha256sum -c SHA256SUMS)
python3 -m venv "$WORK/smoke"
"$WORK/smoke/bin/python" -m pip install "$WORK/dist/$WHEEL"
"$WORK/smoke/bin/python" -m pip check
"$WORK/smoke/bin/ghostdeploy" --help
test "$("$WORK/smoke/bin/ghostdeploy" version)" = 0.3.0a1
"$WORK/smoke/bin/ghostdeploy-ssh" --help
```

Confirm the wheel's `METADATA` and sdist's root `PKG-INFO` both report `0.3.0a1`. List archive members and verify the sdist includes `validation/staging/`. Review the archives for private keys, credentials, personal files, customer data, database backups and deployment state. Record the final SHA, CI URL and artifact hashes.

## Publish after separate maintainer approval

Continue in the same shell so the commands use the artifacts verified above. Create an annotated tag only after approving the exact `main` commit and files. Use a draft to verify the uploaded assets and notes before making the **pre-release** public.

```sh
TAG=v0.3.0a1
test "$(git rev-parse HEAD)" = "$RELEASE_SHA"
(cd "$WORK/dist" && sha256sum -c SHA256SUMS)
git tag -a "$TAG" "$RELEASE_SHA" -m 'GhostDeploy 0.3.0a1'
git push origin "refs/tags/$TAG"
gh release create "$TAG" --verify-tag --draft --prerelease \
  --title 'GhostDeploy 0.3.0a1' --notes-file docs/releases/0.3.0a1.md
gh release upload "$TAG" "$WORK/dist/$WHEEL" "$WORK/dist/$SDIST" "$WORK/dist/SHA256SUMS"
gh release view "$TAG"
# After checking the draft's target, pre-release setting, notes and all three assets:
gh release edit "$TAG" --draft=false --prerelease
gh release view "$TAG"
```

Invite independent testers using the [staging kit](../validation/staging/README.md) and [Alpha issue form](https://github.com/UsamaFathi/GhostDeploy/issues/new?template=alpha-validation.yml). Keep the external gate **PENDING** until independent evidence is recorded. PyPI publication and a Beta decision require their own maintainer review. [GitHub CLI release creation](https://cli.github.com/manual/gh_release_create) · [Release editing](https://cli.github.com/manual/gh_release_edit)
