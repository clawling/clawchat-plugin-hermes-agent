"""Every ``docs/<path>.md`` a source file cites must exist in this repository.

This repository is published on its own, so a comment pointing at a document
that only exists elsewhere (another repository, an unpublished design note)
is a dead reference for every reader. Host documents are cited by their path
in the Hermes source tree instead (e.g. ``website/docs/...``), which this
check deliberately skips.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_DOC_REF = re.compile(r"(?<![\w/.-])docs/[\w./-]+\.md")


def test_source_comments_only_cite_docs_that_exist():
    sources = [ROOT / "__init__.py", ROOT / "clawchat_cli.py", *sorted((ROOT / "clawchat_gateway").rglob("*.py"))]
    missing = sorted(
        f"{source.relative_to(ROOT)}: {ref}"
        for source in sources
        for ref in set(_DOC_REF.findall(source.read_text(encoding="utf-8")))
        if not (ROOT / ref).is_file()
    )
    assert missing == []
