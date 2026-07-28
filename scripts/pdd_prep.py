#!/usr/bin/env python3
"""Per-module prep for `pdd generate --template generic/generate_prompt`.

Writes two throwaway inputs under .pdd/ and prints the generate command:

  .pdd/arch_slice.json  the target module plus its transitive dependencies only.
                        Passing the whole architecture costs ~15k input tokens per
                        call and buys nothing for a leaf module.

  .pdd/story_pack.md    docs/PRD.md followed by the full text of every story the
                        module serves. The generic/generate_prompt template has no
                        DOC_FILES slot, so without this the generator never sees an
                        acceptance criterion — only the one-line description in
                        architecture.json. That is how a pitch limit of 3 reached a
                        prompt when US-01 says 5.

Story selection is by the US-NN citations in the module's reason/description, plus
any extra stories named on the command line.

    python3 scripts/pdd_prep.py prompts/backend/routes/features_python.prompt
    python3 scripts/pdd_prep.py prompts/backend/deps_python.prompt --stories US-06
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
ARCH = ROOT / "architecture.json"
STORY_DIR = ROOT / "docs" / "user_stories"
OUT_DIR = ROOT / ".pdd"

LANG_SUFFIX = {"python": "Python", "typescript": "TypeScript",
               "typescriptreact": "TypeScriptReact", "sql": "SQL"}


def story_index() -> dict[str, pathlib.Path]:
    """US-NN -> story file, read from each story's `**ID:**` line."""
    index = {}
    for path in sorted(STORY_DIR.glob("story__*.md")):
        match = re.search(r"^\*\*ID:\*\*\s*(US-\d\d)", path.read_text(), re.M)
        if match:
            index[match.group(1)] = path
    return index


def transitive_slice(modules: list[dict], target: str) -> list[dict]:
    by_name = {m["filename"]: m for m in modules}
    if target not in by_name:
        sys.exit(f"no module named {target} in architecture.json")
    keep, stack = set(), [target]
    while stack:
        name = stack.pop()
        if name in keep:
            continue
        keep.add(name)
        stack.extend(by_name[name]["dependencies"])
    return [by_name[n] for n in sorted(keep, key=lambda n: by_name[n]["priority"])]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt", help="prompt path as it appears in architecture.json")
    parser.add_argument("--stories", nargs="*", default=[],
                        help="extra US-NN ids to include beyond those cited")
    args = parser.parse_args()

    modules = json.loads(ARCH.read_text())
    sliced = transitive_slice(modules, args.prompt)
    target = sliced[-1] if sliced[-1]["filename"] == args.prompt else \
        next(m for m in sliced if m["filename"] == args.prompt)

    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / "arch_slice.json").write_text(json.dumps(sliced, indent=2))

    index = story_index()
    cited = set(re.findall(r"US-\d\d", target["reason"] + " " + target["description"]))
    cited |= {s if s.startswith("US-") else f"US-{s}" for s in args.stories}
    chosen = [index[c] for c in sorted(cited) if c in index]

    pack = ["# Product scope\n", (ROOT / "docs" / "PRD.md").read_text()]
    if chosen:
        pack.append("\n\n# User stories this module serves\n")
        pack.append("These are the source of truth for behaviour. Every acceptance criterion "
                    "below must be satisfied by this module or by one it depends on.\n")
        for path in chosen:
            pack.append(f"\n---\n\n{path.read_text()}")
    (OUT_DIR / "story_pack.md").write_text("".join(pack))

    stem = pathlib.Path(target["filename"]).stem
    module, _, lang = stem.rpartition("_")
    existing = [m["filename"] for m in modules
                if m["priority"] < target["priority"]
                and (ROOT / m["filename"]).exists()][-3:]

    print(f"module      : {module}  ->  {target['filepath']}")
    print(f"slice       : {len(sliced)} module(s): {[m['filepath'] for m in sliced]}")
    print(f"stories     : {sorted(cited) or 'NONE CITED — pass --stories'}"
          f" -> {[p.name for p in chosen]}")
    print(f"story_pack  : {len((OUT_DIR / 'story_pack.md').read_text())} chars\n")
    print("PDD_COMMAND_MAX_OUTPUT_TOKENS=32000 pdd --local --force \\")
    print("  --output-cost .pdd/costs.csv generate --template generic/generate_prompt \\")
    print(f"  -e MODULE={module} -e LANG_OR_FRAMEWORK={LANG_SUFFIX.get(lang, lang)} \\")
    print(f"  -e ARCHITECTURE_FILE=.pdd/arch_slice.json \\")
    print(f"  -e PRD_FILE=.pdd/story_pack.md -e TECH_STACK_FILE=docs/tech_stack.md \\")
    if existing:
        print(f"  -e EXISTING_PROMPTS={','.join(existing)} \\")
    print(f"  --output {target['filename']}")


if __name__ == "__main__":
    main()
