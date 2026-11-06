#!/usr/bin/env python3
"""FAHES-style DMV detector that deletes detected XML text elements."""

from __future__ import annotations

from typing import Iterable

from fahes_structured import main as _main


def main(argv: Iterable[str] | None = None) -> int:
    return _main(argv, delete_xml_labels=True)


if __name__ == "__main__":
    raise SystemExit(main())
