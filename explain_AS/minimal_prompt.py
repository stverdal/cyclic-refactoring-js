from __future__ import annotations

from typing import List

from context import require_language
from language import edge_semantics_text


BASE_TEMPLATE = """Please refactor to break this dependency cycle:

Cycle size: {size}
{chain}

Remove preferably just one static edge, ensuring no new cycles are introduced and behavior remains unchanged.

{semantics}

Done when:
- The cycle is broken
- All public APIs remain identical
- Tests pass confirming no behavioral changes
- No new cycles are created in the dependency graph

- If you introduce a new file, do not just make the cycle longer (e.g., A->C->B->A).
- It is not enough to remove some imports/references: for the chosen broken edge, ALL relevant references must be removed.

IMPORTANT: Do NOT touch external dependencies.
- Do NOT attempt to install, restore, download, or build any packages or libraries (no pip install, no dotnet restore, no npm install, etc.).
- Do NOT modify package manifests, lock files, or dependency configuration (requirements.txt, pyproject.toml, *.csproj PackageReference, package.json, etc.).
- If a module comes from an external or private library, leave ALL references to it unchanged. Only refactor the project's own source files.
- If you encounter import errors or missing packages, IGNORE them. They are expected — private/internal packages are not available in this environment.
- Focus exclusively on restructuring the project's own source code to break the cycle.

IMPORTANT: You are running in a fully automated headless environment.
- Do NOT ask for human input or confirmation at any point.
- Do NOT suggest opening files in a web browser or any GUI application.
- Do NOT ask the user to test, verify, or review anything manually.
- Do NOT wait for a response. Complete the task autonomously and finish.
- If you believe the refactoring is done, commit the changes and finish the interaction.
"""


def _pretty_node(node_id: str) -> str:
    return (node_id or "").strip().replace("\\", "/") or "<?>"


def cycle_chain_str(nodes: List[str]) -> str:
    if not nodes:
        return "N/A"
    pretty = [_pretty_node(n) for n in nodes]
    return " -> ".join(pretty + [pretty[0]])


def build_minimal_prompt(cycle_nodes: List[str], language: str = "python") -> str:
    language = require_language(language)
    nodes = [str(n) for n in (cycle_nodes or [])]
    size = len(nodes)
    chain = cycle_chain_str(nodes)
    semantics = edge_semantics_text(language)
    return BASE_TEMPLATE.format(size=size, chain=chain, semantics=semantics).rstrip() + "\n"
