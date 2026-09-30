"""Public, synthetic task specifications; grader answers live elsewhere."""
from pathlib import Path

VERSION = "router-mini-v1.0.0"
HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures" / "v1"

TASKS = [
    {"id": "exact_edit", "version": "1.0.0", "title": "Section-scoped exact edit",
     "routing": {"task_type": "edit", "complexity": "simple", "score": 2.2},
     "fixtures": ["config.ini"], "outputs": ["config.ini"],
     "instruction": "Return the complete config.ini after changing only the numeric value of retry_count in the [service] section from 3 to 5. Preserve every other byte, including comments, spacing, blank lines and final newline. The [archive] retry_count stays as supplied."},
    {"id": "structured_extract", "version": "1.0.0", "title": "Revision-aware invoice extraction",
     "routing": {"task_type": "extract", "complexity": "normal", "score": 3.6},
     "fixtures": ["invoices.jsonl"], "outputs": ["summary.json"],
     "instruction": "For each invoice id, use its greatest numeric revision regardless of input order. Retain only latest records with status open and currency USD. Return summary.json with exactly keys invoices and totals. invoices is an array of {id,customer,amount}, sorted ascending by id. totals is an array of {customer,amount}, sorted ascending by customer. Amounts are exact decimal sums formatted as strings with two fractional digits; no floating-point rounding."},
    {"id": "routine_code", "version": "1.0.0", "title": "Integer range union",
     "routing": {"task_type": "code", "complexity": "normal", "score": 5.1},
     "fixtures": [], "outputs": ["solution.py"],
     "instruction": "Implement union_ranges(ranges) in solution.py for half-open integer intervals [start,end). Input is a list of two-element lists or tuples. Validate all inputs: endpoints must be integers (bool is invalid), start <= end, every element has exactly two endpoints; invalid input raises ValueError. Drop empty intervals. Return a new list of [start,end] sorted ascending, merging overlap AND adjacency. Do not mutate input. Empty input returns []. Use only Python 3.9 standard library; no top-level execution."},
    {"id": "bug_fix", "version": "1.0.0", "title": "Expiry-aware LRU repair",
     "routing": {"task_type": "debug", "complexity": "normal", "score": 6.4},
     "fixtures": ["cache.py"], "outputs": ["cache.py"],
     "instruction": "Repair the supplied Cache class while preserving constructor, put(key,value,now,ttl), and get(key,now). capacity must be an integer > 0 (bool invalid), otherwise ValueError. Calls use nondecreasing finite numeric now and ttl >= 0; do not add validation for these. An item expires when now >= expiry. Before any put or get remove all expired entries. A successful get and every put make that key most recently used. When putting a new key at capacity, evict least recently used. Updating an existing key must not evict another. ttl=0 removes any existing value for that key and stores nothing. A missing/expired get raises KeyError. Use Python 3.9 standard library, no top-level execution."},
    {"id": "weighted_schedule", "version": "1.0.0", "title": "Deterministic weighted scheduling",
     "routing": {"task_type": "reason", "complexity": "hard", "score": 7.8},
     "fixtures": [], "outputs": ["solution.py"],
     "instruction": "Implement select_jobs(jobs) in solution.py. Each job is a dict with exactly id,start,end,reward: id is a unique nonempty string; the three numbers are integers (bool invalid), and start < end. Invalid input raises ValueError. Jobs occupy [start,end); touching jobs are compatible. Return selected ids in chronological order (start,end,id), maximizing total reward. Among equal rewards choose fewer jobs; among those choose the lexicographically smallest id sequence in chronological order. Empty selection is allowed. Do not mutate input. Target O(n^2) or better for n up to 200; avoid enumerating all subsets. Use Python 3.9 standard library, no top-level execution."},
    {"id": "circular_boundaries", "version": "1.0.0", "title": "Circular interval boundary rules",
     "routing": {"task_type": "reason", "complexity": "hard", "score": 8.6},
     "fixtures": [], "outputs": ["solution.py"],
     "instruction": "Implement owner(period,segments,t) in solution.py. period is an integer > 0; t is any integer; each segment is a dict with exactly id,start,end,priority. id is a unique nonempty string, priority an integer, and start/end integers in [0,period). bool is never an integer here. Invalid inputs raise ValueError. Normalize t modulo period. A segment covers clockwise [start,end); when start>end it wraps across zero; start=end covers the FULL cycle. Among covering segments choose greatest priority, then smallest clockwise length (full cycle length=period), then lexicographically smallest id. Return the winning id or None. Do not mutate input. Use Python 3.9 standard library, no top-level execution."},
]


def task_by_id(task_id):
    return next(t for t in TASKS if t["id"] == task_id)


def public_task(task):
    return {**task, "fixture_contents": {name: (FIXTURES / name).read_text(encoding="utf-8")
                                         for name in task["fixtures"]}}


def prompt_for(task):
    chunks = ["Solve this synthetic benchmark task in one attempt. Use only the provided specification and fixtures. Do not inspect parent directories, benchmark code, graders, or reference answers. Do not run commands or execute the generated code. Return final JSON only, matching the provided schema: {\"files\":[{\"path\":\"relative filename\",\"content\":\"complete UTF-8 file content\"}]}. Return exactly the required files. No Markdown fences or prose.",
              "Task version: " + VERSION + "/" + task["id"] + "@" + task["version"],
              task["instruction"], "Execution constraints for Python outputs: imports are restricted to collections, bisect, functools, itertools, math, decimal, typing. Only constants, imports, function/class definitions and docstrings are allowed at top level. Do not use file/network/process access, print, eval/exec/compile, dynamic attribute access (getattr/setattr/etc.), introspection, or dunder attributes. These public restrictions are also checked by the grader.",
              "Required outputs: " + ", ".join(task["outputs"])]
    for name, content in task["fixture_contents"].items():
        chunks.append("Fixture " + name + ":\n" + content)
    return "\n\n".join(chunks)
