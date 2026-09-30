"""Resource-bounded child grader. Restricted source execution is not an OS sandbox."""
import ast
import builtins
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import graders

IMPORTS = frozenset(("collections", "bisect", "functools", "itertools", "math", "decimal", "typing"))
BANNED = frozenset(("eval", "exec", "open", "compile", "input", "globals", "locals", "getattr",
                    "setattr", "delattr", "vars", "dir", "help", "breakpoint", "exit", "quit", "print"))


def safe_module(source):
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and (node.id in BANNED or node.id.startswith("__")):
            raise ValueError("forbidden source name")
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise ValueError("forbidden source attribute")
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            modules = [a.name.split(".")[0] for a in node.names] if isinstance(node, ast.Import) else [node.module]
            if getattr(node, "level", 0) or any(m not in IMPORTS for m in modules):
                raise ValueError("unsupported import")
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.Import, ast.ImportFrom,
                                 ast.Assign, ast.AnnAssign, ast.Expr)):
            raise ValueError("unsupported top-level statement")
        if isinstance(node, ast.Expr) and not isinstance(node.value, (ast.Constant,)):
            raise ValueError("top-level execution forbidden")
    allowed = {k: v for k, v in vars(builtins).items()
               if k not in BANNED and not k.startswith("_")}
    def importer(name, globals=None, locals=None, fromlist=(), level=0):
        if level or name.split(".")[0] not in IMPORTS:
            raise ImportError("unsupported import")
        return builtins.__import__(name, globals, locals, fromlist, level)
    allowed["__import__"] = importer
    allowed["__build_class__"] = builtins.__build_class__
    module = {"__builtins__": allowed, "__name__": "submission"}
    exec(compile(tree, "<synthetic-submission>", "exec"), module)
    return module


def main():
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (4, 4))
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_FSIZE, (1024 * 1024, 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
    except (ImportError, ValueError, OSError):
        pass
    request = json.loads(sys.stdin.read(1024 * 1024))
    try:
        files = request["files"]
        if request["task_id"] in ("exact_edit", "structured_extract"):
            graders.CHECKS[request["task_id"]](files)
        else:
            graders.CHECKS[request["task_id"]](safe_module(next(iter(files.values()))))
        result = {"passed": True, "error": None, "grader_version": graders.VERSION}
    except BaseException as e:
        # Keep synthetic failure type/detail, with no traceback or host locals.
        result = {"passed": False, "error": type(e).__name__ + ": " + str(e)[:200],
                  "grader_version": graders.VERSION}
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
