"""Every global name a cycle module uses must be bound in that module.

The 2026-08-16 split of loop.py into eight modules (PARAMETERS.md 9) moved
functions verbatim and left `drift.py` using `sb.KEY` and
`experiments.shadow_models` without importing either — nothing in the suite
exercised that path, and the Monday 2026-08-17 cycle found it after training
and prediction had already run. This is a static check with pyflakes-like
reach: for each module, every Name loaded at module scope or inside a
function must be a builtin, an import, an assignment, a def, a parameter,
or a comprehension variable. It costs a second and would have caught both.
"""
import ast
import builtins
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

MODULES = ['loop.py', 'util.py', 'grading.py', 'training.py', 'predicting.py',
           'delivering.py', 'housekeeping.py', 'drift.py', 'report.py',
           'knobs.py', 'backplay.py', 'shadow.py', 'experiments.py', 'selection.py',
           'render.py', 'simulation.py', 'feedback.py', 'ledger.py', 'heavy_lock.py']


def _bound_names(tree):
    """Names bound anywhere in the module: imports, assignments, defs, args,
    for-targets, with-targets, comprehension targets, except names, globals."""
    names = set(dir(builtins)) | {'__file__', '__name__', '__doc__'}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {(a.asname or a.name).split('.')[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            names |= {a.asname or a.name for a in node.names}
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
            if not isinstance(node, ast.ClassDef):
                a = node.args
                names |= {x.arg for x in a.args + a.kwonlyargs + a.posonlyargs}
                if a.vararg:
                    names.add(a.vararg.arg)
                if a.kwarg:
                    names.add(a.kwarg.arg)
        elif isinstance(node, ast.Lambda):
            a = node.args
            names |= {x.arg for x in a.args + a.kwonlyargs + a.posonlyargs}
            if a.vararg:
                names.add(a.vararg.arg)
            if a.kwarg:
                names.add(a.kwarg.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            names |= set(node.names)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
    return names


class EveryNameIsBound(unittest.TestCase):
    def test_modules_use_no_unbound_global(self):
        for module in MODULES:
            path = REPO / module
            if not path.exists():
                continue
            tree = ast.parse(path.read_text(encoding='utf-8'))
            bound = _bound_names(tree)
            loaded = {n.id for n in ast.walk(tree)
                      if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
            with self.subTest(module=module):
                self.assertEqual(sorted(loaded - bound), [],
                                 f'{module} uses names it never binds')


if __name__ == '__main__':
    unittest.main()
