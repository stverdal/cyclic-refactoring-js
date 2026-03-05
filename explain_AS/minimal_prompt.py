from __future__ import annotations

from typing import Dict, List


_TYPE_EXCLUSION_NOTES = {
    "python": (
        "- Type-only references under TYPE_CHECKING do NOT count as dependencies (Python).\n"
        "How to check that an edge A->B in the cycle has been successfully broken:\n"
        "- There is not a single import/reference from B in file A (except if under TYPE_CHECKING in Python)."
    ),
    "csharp": (
        "- Unused `using` directives do NOT count as dependencies (C#).\n"
        "How to check that an edge A->B in the cycle has been successfully broken:\n"
        "- There is not a single type reference from B in file A."
    ),
    "javascript": (
        "- `import type` statements (TypeScript type-only imports) do NOT count as dependencies.\n"
        "How to check that an edge A->B in the cycle has been successfully broken:\n"
        "- There is not a single import/require/export-from reference to B in file A (except `import type` in TypeScript)."
    ),
}

BASE_TEMPLATE = """Please refactor to break this dependency cycle:

Cycle size: {size}
{chain}

Remove preferably just one static edge, ensuring no new cycles are introduced and behavior remains unchanged.

Important:
- My ATD metric treats ANY module/type reference as a dependency (dynamic/lazy all count).
- So making imports dynamic or lazy is NOT sufficient.
- We care about architecture (static coupling), not runtime import order.
{type_exclusion_note}

Done when:
- The cycle is broken
- All public APIs remain identical
- Tests pass confirming no behavioral changes
- No new cycles are created in the dependency graph

{edge_check_note}
- If you introduce a new file, do not just make the cycle longer (e.g., A->C->B->A).
- It is not enough to remove some imports/references: for the chosen broken edge, ALL relevant references must be removed.
"""


def _pretty_node(node_id: str) -> str:
    return (node_id or "").strip().replace("\\", "/") or "<?>"


def cycle_chain_str(nodes: List[str]) -> str:
    if not nodes:
        return "N/A"
    pretty = [_pretty_node(n) for n in nodes]
    return " -> ".join(pretty + [pretty[0]])


def build_minimal_prompt(cycle_nodes: List[str], language: str = "python") -> str:
    nodes = [str(n) for n in (cycle_nodes or [])]
    size = len(nodes)
    chain = cycle_chain_str(nodes)
    full_note = _TYPE_EXCLUSION_NOTES.get(language, _TYPE_EXCLUSION_NOTES["python"])
    # Split the note into the type-exclusion bullet and the edge-check paragraph
    parts = full_note.split("How to check that an edge A->B in the cycle has been successfully broken:\n")
    type_note = parts[0].rstrip()
    edge_check = "How to check that an edge A->B in the cycle has been successfully broken:\n" + (parts[1] if len(parts) > 1 else "")
    return BASE_TEMPLATE.format(size=size, chain=chain, type_exclusion_note=type_note, edge_check_note=edge_check).rstrip() + "\n"
