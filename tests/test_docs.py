import re
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from ghostdeploy.config import load

ROOT = Path(__file__).resolve().parents[1]
HEADING = re.compile(r"^#{1,6} (.+)$", re.MULTILINE)
LINK = re.compile(r"\]\(([^\s)]+)\)")


@pytest.mark.parametrize(
    "document",
    sorted(
        [*ROOT.glob("*.md"), *(ROOT / "docs").glob("*.md"), *(ROOT / "validation").rglob("*.md")]
    ),
)
def test_local_documentation_links(document):
    for link in LINK.findall(document.read_text(encoding="utf-8")):
        parts = urlsplit(link)
        if parts.scheme or parts.netloc:
            continue
        target = (document.parent / parts.path).resolve() if parts.path else document
        assert target.is_relative_to(ROOT) and target.exists(), (
            f"{document.name} links to missing {link}"
        )
        if parts.fragment and target.suffix == ".md":
            headings = HEADING.findall(target.read_text(encoding="utf-8"))
            anchors = {
                re.sub(r"[^\w -]", "", heading.lower()).replace(" ", "-") for heading in headings
            }
            assert parts.fragment in anchors, f"{document.name} links to missing heading at {link}"


@pytest.mark.parametrize("example", sorted((ROOT / "examples").glob("*.yml")))
def test_documented_configuration_examples_validate(example):
    config = load(example)
    assert config.deployment.databases
