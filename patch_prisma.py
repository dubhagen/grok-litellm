"""Keep LiteLLM's Prisma engine hook working with prisma-client-py 0.13+.

Reinstalling litellm restores the upstream assignment, which raises
AttributeError on a slotted client and the proxy then reports that it is
not connected to a database. start.sh runs this before launching LiteLLM.
"""

from __future__ import annotations

import sys
from pathlib import Path

OLD = """\
    def _write_engine(prisma_client: _PrismaClient, engine: _PrismaEngine) -> None:
        prisma_client._Prisma__engine = engine
"""

NEW = """\
    def _write_engine(prisma_client: _PrismaClient, engine: _PrismaEngine) -> None:
        # prisma-client-py 0.13+ stores the engine behind the `_engine` setter.
        # Assigning the old mangled name raises AttributeError on a slotted client
        # and LiteLLM then drops the database connection.
        setter = getattr(type(prisma_client), "_engine", None)
        if isinstance(setter, property) and setter.fset is not None:
            prisma_client._engine = engine
            return
        prisma_client._Prisma__engine = engine
"""


def main() -> int:
    root = Path(__file__).resolve().parent
    matches = list((root / ".venv").glob("lib/python*/site-packages/litellm/proxy/db/prisma_client.py"))
    if len(matches) != 1:
        print(f"expected one prisma_client.py, found {len(matches)}", file=sys.stderr)
        return 1
    path = matches[0]
    text = path.read_text()
    if NEW in text:
        return 0
    if OLD not in text:
        print(f"prisma engine hook not found in {path}", file=sys.stderr)
        return 1
    path.write_text(text.replace(OLD, NEW, 1))
    print(f"patched {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
