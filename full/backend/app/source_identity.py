"""Original KEGE record numbers for linked 19/20/21 questions."""
from __future__ import annotations

import re

from .models import TaskRevision


def display_source_id(revision: TaskRevision) -> str:
    """Keep storage identity separate from a verified parent record number.

    The alias is narrow: generic source metadata cannot rename a task. Child
    UUIDs, versions, history and grading remain independent of this label.
    """
    metadata = revision.source_metadata
    if not isinstance(metadata, dict) or metadata.get("kege_schema") != "kege-current-bank/2.0":
        return revision.source_id
    parent = metadata.get("kege_record_id")
    question = metadata.get("kege_question_number")
    if (not isinstance(parent, str) or not 1 <= len(parent) <= 255
            or re.fullmatch(r"[0-9]+", parent) is None
            or not isinstance(question, int) or isinstance(question, bool) or question not in (20, 21)
            or revision.line_number != question
            or revision.source_id != f"{parent}~{question}"
            or revision.source_url != f"https://kompege.ru/task?id={parent}"):
        return revision.source_id
    return parent
