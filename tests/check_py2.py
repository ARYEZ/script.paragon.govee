# -*- coding: utf-8 -*-
"""
Paragon Home - Python 2.7 compatibility gate.

Kodi 17.6 embeds Python 2.7, but a 2.7 interpreter is not something a modern
development machine still has lying around. Rather than trust review, this
walks the AST of every shipped source file and fails on constructs 2.7 cannot
parse, plus the standard-library moves that only bite at runtime.

    python3 tests/check_py2.py
"""

from __future__ import print_function

import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# Modules that exist only on Python 3. compat.py is allowed to name them
# because it does so behind a version check.
PY3_ONLY_MODULES = {
    'urllib.request', 'urllib.parse', 'urllib.error', 'queue', 'configparser',
    'builtins', '_thread', 'http.server', 'http.client', 'socketserver',
    'io.StringIO', 'importlib.reload', 'statistics', 'pathlib', 'enum',
    'typing', 'dataclasses', 'secrets', 'asyncio', 'concurrent.futures',
}

EXEMPT = {
    os.path.join(ROOT, 'resources', 'lib', 'compat.py'),
}


class Py2Checker(ast.NodeVisitor):
    def __init__(self, path):
        self.path = path
        self.problems = []

    def fail(self, node, message):
        self.problems.append((getattr(node, 'lineno', 0), message))

    # -- syntax that 2.7 cannot parse --------------------------------------

    def visit_JoinedStr(self, node):
        self.fail(node, 'f-string (not valid on Python 2.7)')
        self.generic_visit(node)

    def visit_AnnAssign(self, node):
        self.fail(node, 'variable annotation (not valid on Python 2.7)')
        self.generic_visit(node)

    def visit_Nonlocal(self, node):
        self.fail(node, '`nonlocal` (not valid on Python 2.7)')
        self.generic_visit(node)

    def visit_YieldFrom(self, node):
        self.fail(node, '`yield from` (not valid on Python 2.7)')
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node):
        self.fail(node, '`async def` (not valid on Python 2.7)')

    def visit_Await(self, node):
        self.fail(node, '`await` (not valid on Python 2.7)')

    def visit_MatMult(self, node):
        self.fail(node, 'matrix multiply operator (Python 3.5+)')

    def visit_Starred(self, node):
        # `f(*args)` is fine on 2.7; starred targets and multiple unpacks are
        # not. Only assignment targets reach here as a Starred node.
        if isinstance(getattr(node, 'ctx', None), ast.Store):
            self.fail(node, 'starred assignment target (Python 3 only)')
        self.generic_visit(node)

    def visit_Dict(self, node):
        if any(key is None for key in node.keys):
            self.fail(node, 'dict unpacking {**x} (Python 3.5+)')
        self.generic_visit(node)

    def visit_Raise(self, node):
        if getattr(node, 'cause', None) is not None:
            self.fail(node, '`raise ... from ...` (Python 3 only)')
        self.generic_visit(node)

    def visit_FunctionDef(self, node):
        args = node.args
        if getattr(args, 'kwonlyargs', None):
            self.fail(node, 'keyword-only arguments (Python 3 only)')
        if getattr(args, 'posonlyargs', None):
            self.fail(node, 'positional-only arguments (Python 3.8+)')
        if node.returns is not None:
            self.fail(node, 'return annotation (Python 3 only)')
        for arg in list(args.args) + list(getattr(args, 'kwonlyargs', [])):
            if getattr(arg, 'annotation', None) is not None:
                self.fail(node, 'argument annotation (Python 3 only)')
        self.generic_visit(node)

    def visit_ClassDef(self, node):
        # Old-style classes behave differently on 2.7 (no super(), no
        # properties on instances), so every class must have a base.
        if not node.bases:
            self.fail(node, 'class %s has no base; use (object) for 2.7'
                      % node.name)
        self.generic_visit(node)

    def visit_Call(self, node):
        func = node.func
        # Zero-argument super() is Python 3 only.
        if isinstance(func, ast.Name) and func.id == 'super' and not node.args:
            self.fail(node, 'zero-argument super() (Python 3 only)')
        self.generic_visit(node)

    # -- imports that only fail at runtime ---------------------------------

    def visit_Import(self, node):
        if self.path in EXEMPT:
            return
        for alias in node.names:
            if alias.name in PY3_ONLY_MODULES:
                self.fail(node, 'imports %s, which is Python 3 only'
                          % alias.name)

    def visit_ImportFrom(self, node):
        if self.path in EXEMPT:
            return
        module = node.module or ''
        if module in PY3_ONLY_MODULES:
            self.fail(node, 'imports from %s, which is Python 3 only' % module)
        for alias in node.names:
            full = '%s.%s' % (module, alias.name)
            if full in PY3_ONLY_MODULES:
                self.fail(node, 'imports %s, which is Python 3 only' % full)


def shipped_files():
    """Every .py file that ends up on the user's device."""
    paths = [os.path.join(ROOT, 'default.py'), os.path.join(ROOT, 'service.py')]
    lib = os.path.join(ROOT, 'resources', 'lib')
    for name in sorted(os.listdir(lib)):
        if name.endswith('.py'):
            paths.append(os.path.join(lib, name))
    return paths


def main():
    failures = 0
    checked = 0

    for path in shipped_files():
        if not os.path.isfile(path):
            print('MISSING %s' % path)
            failures += 1
            continue

        handle = open(path, 'rb')
        try:
            source = handle.read()
        finally:
            handle.close()

        try:
            tree = ast.parse(source, filename=path)
        except SyntaxError as exc:
            print('SYNTAX  %s:%s %s' % (path, exc.lineno, exc.msg))
            failures += 1
            continue

        checker = Py2Checker(path)
        checker.visit(tree)
        checked += 1

        rel = os.path.relpath(path, ROOT)
        if checker.problems:
            for lineno, message in sorted(checker.problems):
                print('PY2     %s:%d %s' % (rel, lineno, message))
            failures += len(checker.problems)
        else:
            print('ok      %s' % rel)

    print('\n%d file(s) checked, %d problem(s)' % (checked, failures))
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
