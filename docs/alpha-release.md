# Alpha release preparation

## Draft release notes

GhostDeploy helps independent developers deploy reviewed addon revisions to self-hosted Odoo 18 and 19 servers. The candidate includes a GitHub Actions staging kit, read-only local preflight and evidence verification, restricted SSH failure propagation, and an issue form for independent testers.

A systemd restart affects every database served by that unit. GhostDeploy upgrades only approved candidates on the database allowlist. A partial database upgrade requires operator reconciliation, a verified backup and a separate restore drill. Ubuntu with a dedicated Odoo account, PostgreSQL and a clean full addon checkout is the evaluated environment. See the [compatibility and evidence matrix](testing.md#support-and-verification) and [known boundaries](../README.md).

## Versioning and provenance

`0.3.0.dev0` remains the development version. Propose `0.3.0a1` after maintainer approval. PEP 440 orders this Alpha after the development build and before `0.3.0`. Use monotonically increasing Alpha numbers for distinct public candidates and keep published version identifiers immutable. Tie each public build to a reviewed commit and an annotated version tag after authorization. [PEP 440 version specifiers](https://packaging.python.org/en/latest/specifications/version-specifiers/)

Build from the reviewed commit into a fresh directory. Run strict package metadata checks, then hash both artifacts. Transfer the wheel and checksum through the same reviewed artifact process and compare the received wheel on the staging host before installation.

```sh
python -m build --outdir dist/alpha-candidate
python -m twine check --strict dist/alpha-candidate/*
cd dist/alpha-candidate
sha256sum *.whl *.tar.gz > SHA256SUMS
sha256sum -c SHA256SUMS
python -m venv .smoke
.smoke/bin/python -m pip install ./ghostdeploy-*.whl
.smoke/bin/ghostdeploy --help
.smoke/bin/ghostdeploy version
```

Review [installation and upgrades](installation.md), including preserving a pending journal before upgrading, and the [uninstall procedure](installation.md#uninstall-ghostdeploy). Follow [security reporting](../SECURITY.md) for sensitive issues. The [staging kit](../validation/staging/README.md) covers validation and sanitized evidence.

## Release checklist

- [ ] Maintainer approves the version, changelog, known limitations and SHA.
- [ ] Local checks and repository CI pass on the release commit.
- [ ] A clean install from the wheel succeeds and the source archive includes the staging kit.
- [ ] Publishable artifacts and their SHA-256 checksums match the reviewed version and commit.
- [ ] Independent staging evidence is attached or its pending status is disclosed explicitly.
- [ ] A maintainer authorizes publication and uses the approved distribution channel.

A future GitHub release workflow should be manually authorized and operate on a protected reviewed version tag. Build and test in a job with read-only source access, transfer those exact artifacts and their hashes to a separately approved publication job, and verify the tags and versions match. If a maintainer later enables PyPI Trusted Publishing, bind its OIDC publisher to the repository, workflow and protected environment. Grant `id-token: write` only to that publication job. A GitHub release job needs its own narrowly scoped `contents: write` permission. Keep release creation and publishing behind explicit approvals. [PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/using-a-publisher/) · [GitHub environment rules](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments)
