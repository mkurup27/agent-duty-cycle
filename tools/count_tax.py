"""Experiment 6: count the state-externalization tax.

  python3 tools/count_tax.py            # table + JSON

Counts logical source lines (non-blank, non-comment, non-docstring) in:
  * TAX-marked regions of the stateless arm, by category
  * each arm's arm-specific code (what you write beyond the shared core)
  * shared core (identical for all arms, for scale)

Infrastructure glue (s3mini.py, db.py) is reported separately: it is code
the stateless arm needs, but it would shrink with heavier dependencies
(boto3, psycopg) that the 48 MB Functions build limit makes awkward.
"""

import ast
import io
import json
import re
import tokenize
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FN = ROOT / "arms/functions/packages/agent/handle"


def logical_lines(src):
    """Line numbers holding real code: skip blanks, comments, docstrings."""
    doc_lines = set()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef,
                             ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(getattr(first, "value", None),
                                                          ast.Constant) \
                    and isinstance(first.value.value, str):
                doc_lines.update(range(first.lineno, first.end_lineno + 1))
    code = set()
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type in (tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE,
                        tokenize.INDENT, tokenize.DEDENT, tokenize.ENCODING,
                        tokenize.ENDMARKER):
            continue
        for ln in range(tok.start[0], tok.end[0] + 1):
            if ln not in doc_lines:
                code.add(ln)
    return code


def count_file(p):
    return len(logical_lines(p.read_text()))


def tax_regions(p):
    src = p.read_text()
    code = logical_lines(src)
    out, cat, start = defaultdict(int), None, None
    for i, line in enumerate(src.splitlines(), 1):
        m = re.match(r"\s*# TAX:begin (\w+)", line)
        if m:
            cat, start = m.group(1), i
        elif re.match(r"\s*# TAX:end", line) and cat:
            out[cat] += sum(1 for ln in code if start < ln < i)
            cat = None
    return dict(out)


def main():
    tax = defaultdict(int)
    for p in FN.glob("*.py"):
        for k, v in tax_regions(p).items():
            tax[k] += v
    rep = {
        "stateless_tax_by_category": dict(tax),
        "stateless_tax_total": sum(tax.values()),
        "stateless_infra_glue": {"s3mini.py": count_file(FN / "s3mini.py"),
                                 "db.py": count_file(FN / "db.py")},
        "arm_specific_code": {
            "a_droplet": {"core/server.py": count_file(ROOT / "core/server.py")},
            "b_functions": {f.name: count_file(f) for f in sorted(FN.glob("*.py"))
                            if f.name != "local_blobs.py"},
            "c_managed_agents": {"core/server.py": count_file(ROOT / "core/server.py"),
                                 "core/deliver.py": count_file(ROOT / "core/deliver.py"),
                                 "start_daemon.py": count_file(
                                     ROOT / "arms/managed_agents/start_daemon.py")},
        },
        "shared_core": {f.name: count_file(f) for f in sorted((ROOT / "core").glob("*.py"))
                        if f.name not in ("server.py", "deliver.py")},
        "not_counted": "deploy scripts, bootstrap/control-plane tooling, tests, fakes",
    }
    for k in ("b_functions", "c_managed_agents", "a_droplet"):
        rep["arm_specific_code"][k]["total"] = sum(rep["arm_specific_code"][k].values())
    rep["shared_core"]["total"] = sum(rep["shared_core"].values())
    print(json.dumps(rep, indent=2))


if __name__ == "__main__":
    main()
