"""Plain-language reasons for a failed job (B25).

`explain_failure` walks a table of rules, first match wins. A rule can ask for
a failure category, an exit code and a pattern in the last lines of the log;
every condition a rule names must hold. Advice may use `{named}` groups the
pattern captured.
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass

from app.models import JobAttempt
from app.storage import storage

TAIL_LINES = 40
TAIL_CHARS = 6000

# Import name -> the name pip knows it by, for the usual mismatches.
PIP_NAMES = {
    "cv2": "opencv-python",
    "sklearn": "scikit-learn",
    "PIL": "pillow",
    "yaml": "pyyaml",
    "skimage": "scikit-image",
    "bs4": "beautifulsoup4",
    "dotenv": "python-dotenv",
}


@dataclass(frozen=True)
class Rule:
    title: str
    advice: str
    categories: tuple[str, ...] = ()
    exit_codes: tuple[int, ...] = ()
    pattern: str | None = None
    build_failed: bool = False


RULES: tuple[Rule, ...] = (
    Rule(
        "Cancelled",
        "This job was cancelled before it finished. Submit it again (or use Clone) when you want to run it.",
        categories=("cancelled",),
    ),
    Rule(
        "Ran out of time",
        "The job reached its maximum runtime and was stopped. Raise the maximum runtime under Advanced, "
        "save checkpoints so a rerun can continue, or make each run shorter (fewer epochs, a subset of the data).",
        categories=("timeout",),
    ),
    Rule(
        "The lab PC stopped responding",
        "The PC running your job went offline or lost contact. The platform retries such jobs automatically; "
        "this one used all its attempts. Retry it, and tell the lab admin if it keeps happening.",
        categories=("lease_lost", "node_lost"),
    ),
    Rule(
        "The environment could not be built",
        "The packages in your requirements.txt could not be installed or the environment failed its test. "
        "Open the build log, fix the package name or version (pin versions that work together) and submit again.",
        build_failed=True,
    ),
    Rule(
        "The environment could not be started",
        "The environment (software image) failed to download or start on the PC. Retrying often works; "
        "if it fails again, choose another environment or ask the lab admin.",
        categories=("environment",),
    ),
    Rule(
        "GPU out of memory",
        "Your model and data did not fit in the GPU's memory. Lower the batch size, use a smaller model or "
        "image size, enable mixed precision (fp16/bf16), or request a GPU with more memory.",
        pattern=r"CUDA out of memory|CUDA error: out of memory|OutOfMemoryError|"
        r"ResourceExhaustedError|Resource exhausted: OOM|CUBLAS_STATUS_ALLOC_FAILED",
    ),
    Rule(
        "The job was killed",
        "The system stopped your job (exit code 137), usually because it used more RAM than it was given. "
        "Load data in smaller pieces, lower the batch size or number of data-loader workers, or request more RAM "
        "under Advanced.",
        exit_codes=(137, -9),
    ),
    Rule(
        "The job was killed",
        "The system stopped your job, usually because it used more RAM than it was given. Load data in smaller "
        "pieces, lower the batch size or number of data-loader workers, or request more RAM under Advanced.",
        pattern=r"(?m)^Killed\s*$",
    ),
    Rule(
        "A Python package is missing",
        "Your script imports '{module}', which is not installed in the environment. Add '{package}' to the "
        "packages box (requirements.txt) and submit again, or pick a catalog environment that includes it.",
        pattern=r"(?:ModuleNotFoundError|ImportError): No module named ['\"](?P<module>[\w.\-]+)['\"]",
    ),
    Rule(
        "A file was not found",
        "Your script looked for '{path}', but it is not in the job. Only the files you upload are available: "
        "include the data in your upload, or download or generate it inside the script, and use paths relative "
        "to the script rather than paths on your own computer.",
        pattern=r"FileNotFoundError: .*?['\"](?P<path>[^'\"]+)['\"]",
    ),
    Rule(
        "The outputs were too large",
        "The files your script wrote are bigger than the lab allows per job. Save fewer or smaller files "
        "(for example only the final model) and submit again.",
        categories=("resource",),
    ),
)


def _matches(rule: Rule, category: str | None, exit_code: int | None, tail: str, build_failed: bool):
    """The pattern's captured groups (possibly empty) when every condition of the rule holds, else None."""
    if rule.build_failed and not build_failed:
        return None
    if rule.categories and category not in rule.categories:
        return None
    if rule.exit_codes and exit_code not in rule.exit_codes:
        return None
    if rule.pattern is None:
        return {}
    found = re.search(rule.pattern, tail)
    return found.groupdict() if found else None


def _fill(text: str, groups: dict[str, str | None]) -> str:
    values = {k: v for k, v in groups.items() if v}
    module = values.get("module")
    if module:
        top = module.split(".")[0]
        values["module"] = top
        values["package"] = PIP_NAMES.get(top, top)
    for key in re.findall(r"{(\w+)}", text):
        values.setdefault(key, "?")
    return text.format(**values)


def explain_failure(
    attempt: JobAttempt | None, log_tail: str = "", *, build_failed: bool = False
) -> dict[str, str] | None:
    """`{"title", "advice"}` for the failure of this attempt, or None when no
    rule fits. `attempt` is None for a job that failed before it ever ran."""
    category = attempt.failure_category if attempt is not None else None
    exit_code = attempt.exit_code if attempt is not None else None
    for rule in RULES:
        groups = _matches(rule, category, exit_code, log_tail or "", build_failed)
        if groups is not None:
            return {"title": rule.title, "advice": _fill(rule.advice, groups)}
    if category == "user_code" or (exit_code not in (None, 0) and category is None):
        code = f" (exit code {exit_code})" if exit_code is not None else ""
        return {
            "title": "Your script stopped with an error",
            "advice": f"The script exited with an error{code}. The last lines of the log above show what went wrong; "
            "fix it and submit again.",
        }
    return None


def _read_tail(attempt: JobAttempt, lines: int) -> str:
    last = int(attempt.last_log_seq or 0)
    out: list[str] = []
    for seq in range(max(1, last - lines + 1), last + 1):
        try:
            raw = storage.get_log_chunk(f"{attempt.output_namespace}logs/{seq}.jsonl.gz")
        except Exception:
            continue
        for row in raw.decode(errors="replace").splitlines():
            try:
                out.append(json.loads(row).get("line", ""))
            except ValueError:
                continue
    return "\n".join(out)[-TAIL_CHARS:]


async def log_tail(attempt: JobAttempt | None, lines: int = TAIL_LINES) -> str:
    """The last lines the attempt logged (storage reads run off the event loop)."""
    if attempt is None or not int(attempt.last_log_seq or 0):
        return ""
    return await asyncio.to_thread(_read_tail, attempt, lines)

