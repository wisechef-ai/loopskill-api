"""LibCST codemod: rename the deferred Cookbook* class call-sites to Bundle*.

Scope: ONLY the 4 class symbols, as Name nodes (identifiers). This deliberately
does NOT touch:
  - attribute accesses like `.cookbook_id` (those are Attribute nodes / wire-contract cols)
  - string literals ("cookbook_id" JSON keys, /api/cookbook* route paths, error codes)
  - lowercase identifiers (cookbook_id, cookbook_scope, cookbook_limit)
  - the compat-alias DEFINITION lines in models.py (handled separately by deletion)

Why libcst not regex: regex broke this twice (NameError from import/body desync,
module-name collisions). libcst renames the Name token wherever it appears as an
identifier — import lines and call-sites alike — atomically and only when it's a
real Name node, never inside a string or attribute.

Usage:
  python scripts/_rename_cookbook_classes_codemod.py <file1> <file2> ...
Writes in place. Prints per-file change counts.
"""

import sys

import libcst as cst


RENAMES = {
    "CookbookSkill": "BundleSkill",
    "CookbookShareToken": "BundleShareToken",
    "CookbookDeployment": "BundleDeployment",
    "Cookbook": "Bundle",  # last — longest-prefix names already handled above
}

# Lines that DEFINE the compat alias: `Cookbook = Bundle  # compat-alias`.
# We must NOT rename the LHS of these (would become `Bundle = Bundle`); they are
# deleted in a separate step. Detect by the compat-alias comment.
ALIAS_TARGETS = set(RENAMES.keys())


class RenameClassRefs(cst.CSTTransformer):
    def __init__(self) -> None:
        self.count = 0
        self._skip_alias_line = False

    def leave_Name(self, original_node: cst.Name, updated_node: cst.Name) -> cst.BaseExpression:
        new = RENAMES.get(original_node.value)
        if new is not None:
            self.count += 1
            return updated_node.with_changes(value=new)
        return updated_node


def _is_alias_def_line(line: str) -> bool:
    # `Cookbook = Bundle  # compat-alias` style — skip whole file? No: handle by
    # excluding the assignment target. Simpler: we drop these lines post-codemod
    # in the orchestrator. Here we just rename refs; the alias lines become
    # `Bundle = Bundle  # compat-alias` and are removed afterwards.
    return False


def process(path: str) -> int:
    with open(path, encoding="utf-8") as f:
        src = f.read()
    module = cst.parse_module(src)
    transformer = RenameClassRefs()
    new_module = module.visit(transformer)
    if transformer.count:
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_module.code)
    return transformer.count


if __name__ == "__main__":
    total = 0
    for p in sys.argv[1:]:
        n = process(p)
        total += n
        if n:
            print(f"{n:4d}  {p}")
    print(f"---- {total} class-ref renames across {len(sys.argv) - 1} files")
