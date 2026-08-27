"""Local Windows-friendly entry point for the fact-gated rewrite pipeline."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import ntpath
import os
import re
import shutil
import stat
import sys
from pathlib import Path
from typing import Iterable

from pydantic import ValidationError

from .importer import parse_raw_job
from .repository import NotFoundError, Repository
from .rewrite_factory import create_rewrite_pipeline
from .rewrite_schemas import FactAnswer, FactAnswersRequest, RewriteRequest
from .settings import get_settings


_TEXT_RESUME_SUFFIXES = {".md", ".txt"}
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
_INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
_EMAIL = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
_PHONE = re.compile(r"(?<![\w])(?:\+?\d[\d()\s.-]{6,}\d)(?![\w])")
_TARGET_JD_MAX_CHARACTERS = 100_000


def read_target_jd_file(path: Path) -> str:
    """Read one safe UTF-8 Markdown/text JD at the CLI boundary."""
    if not isinstance(path, Path):
        raise ValueError("target JD file path is invalid")
    if path.suffix.lower() not in _TEXT_RESUME_SUFFIXES:
        raise ValueError("target JD file must be .md or .txt")
    if os.name == "nt":
        _validate_windows_target_path(str(path))
    absolute = path.absolute()
    _reject_reparse_components(absolute)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(absolute, flags)
    except OSError:
        raise ValueError("target JD file is unavailable") from None
    try:
        binary = os.fdopen(descriptor, "rb")
    except Exception:
        os.close(descriptor)
        raise ValueError("target JD file is unavailable") from None
    try:
        with binary:
            opened_stat = os.fstat(binary.fileno())
            if not stat.S_ISREG(opened_stat.st_mode):
                raise ValueError("target JD file is unsafe")
            _reject_reparse_components(absolute)
            if os.name == "nt":
                _verify_windows_opened_path(binary.fileno(), absolute)
            else:
                path_stat = _nonfollowing_stat(absolute)
                if (
                    not stat.S_ISREG(path_stat.st_mode)
                    or not os.path.samestat(opened_stat, path_stat)
                ):
                    raise ValueError("target JD file is unsafe")
            with io.TextIOWrapper(
                binary, encoding="utf-8-sig", errors="strict"
            ) as reader:
                content = reader.read(_TARGET_JD_MAX_CHARACTERS + 1)
    except UnicodeError:
        raise ValueError("target JD file is not valid UTF-8") from None
    except OSError:
        raise ValueError("target JD file is unavailable") from None
    if len(content) > _TARGET_JD_MAX_CHARACTERS:
        raise ValueError("target JD file exceeds 100000 characters")
    content = content.strip()
    if not content:
        raise ValueError("target JD file is empty")
    return content


def _reject_reparse_components(path: Path) -> None:
    absolute = path.absolute()
    for component in [*reversed(absolute.parents), absolute]:
        if os.path.lexists(component) and _is_reparse_point(component):
            raise ValueError("target JD file is unsafe")


def _nonfollowing_stat(path: Path):
    try:
        return os.stat(path, follow_symlinks=False)
    except (NotImplementedError, TypeError):
        return os.lstat(path)


def _validate_windows_target_path(value: str) -> None:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("target JD file is unsafe")
    normalized = value.replace("/", "\\")
    folded = normalized.casefold()
    if folded.startswith(("\\\\?\\", "\\\\.\\", "\\??\\")):
        raise ValueError("target JD file is unsafe")
    drive, tail = ntpath.splitdrive(normalized)
    if ":" in tail or (
        ":" in drive
        and not (len(drive) == 2 and drive[0].isalpha() and drive[1] == ":")
    ):
        raise ValueError("target JD file is unsafe")


def _normalize_windows_final_path(value: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("target JD file is unsafe")
    normalized = value.replace("/", "\\")
    folded = normalized.casefold()
    if folded.startswith("\\\\?\\unc\\"):
        normalized = "\\\\" + normalized[8:]
    elif folded.startswith("\\\\?\\"):
        normalized = normalized[4:]
    elif folded.startswith("\\\\.\\"):
        raise ValueError("target JD file is unsafe")
    return ntpath.normcase(ntpath.normpath(normalized)).casefold()


def _verify_windows_opened_path(descriptor: int, intended_path: Path) -> None:
    try:
        actual = _windows_final_path_for_descriptor(descriptor)
        actual_normalized = _normalize_windows_final_path(actual)
        intended_normalized = _normalize_windows_final_path(str(intended_path))
    except Exception:
        raise ValueError("target JD file is unsafe") from None
    if actual_normalized != intended_normalized:
        raise ValueError("target JD file is unsafe")


def _windows_final_path_for_descriptor(descriptor: int) -> str:
    if os.name != "nt":
        raise OSError("Windows handle paths are unavailable")
    try:
        import ctypes
        from ctypes import wintypes
        import msvcrt

        handle = msvcrt.get_osfhandle(descriptor)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_final_path = kernel32.GetFinalPathNameByHandleW
        get_final_path.argtypes = (
            wintypes.HANDLE,
            wintypes.LPWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
        )
        get_final_path.restype = wintypes.DWORD
    except Exception:
        raise OSError("Windows handle paths are unavailable") from None

    capacity = 512
    while capacity <= 32_768:
        buffer = ctypes.create_unicode_buffer(capacity)
        length = get_final_path(handle, buffer, capacity, 0)
        if length == 0:
            raise OSError(ctypes.get_last_error(), "Windows handle path failed")
        if length < capacity:
            return buffer.value
        capacity = length + 1
    raise OSError("Windows handle path is too long")


class _InputAction(argparse.Action):
    """Collect repeatable ID/path inputs in the order the user supplied them."""

    def __init__(self, option_strings, dest, **kwargs):
        self.kind = kwargs.pop("kind")
        super().__init__(option_strings, dest, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        inputs = list(getattr(namespace, self.dest, None) or [])
        inputs.append((self.kind, values))
        setattr(namespace, self.dest, inputs)


class _SafeArgumentParser(argparse.ArgumentParser):
    """Avoid echoing user-supplied argument values on validation failures."""

    def error(self, message):
        raise ValueError("invalid command arguments")


def bootstrap_request(
    repository: Repository,
    candidate_id: str | None,
    candidate_name: str | None,
    target_role: str | None,
    resume_paths: Iterable[Path] = (),
    jd_paths: Iterable[Path] = (),
    output_name: str | None = None,
    *,
    source_resume_ids: Iterable[str] = (),
    job_ids: Iterable[str] = (),
    resume_inputs: Iterable[tuple[str, str | Path]] | None = None,
    job_inputs: Iterable[tuple[str, str | Path]] | None = None,
) -> RewriteRequest:
    """Create the canonical, ID-only request while retaining no file paths."""
    sanitized_explicit_name = None
    if output_name is not None:
        _validate_explicit_output_name(output_name)
        sanitized_explicit_name = _sanitize_output_name(output_name)
    candidate, role = _resolve_candidate(
        repository, candidate_id, candidate_name, target_role
    )
    resume_ids = _reuse_or_import_resumes(
        repository, candidate["id"], _combined_inputs(
            source_resume_ids, resume_paths, resume_inputs
        )
    )
    resolved_job_ids, jobs = _reuse_or_import_jobs(
        repository, candidate["id"], role, _combined_inputs(
            job_ids, jd_paths, job_inputs
        )
    )
    if not resume_ids or not resolved_job_ids:
        raise ValueError("at least one resume and one job are required")
    generated_name = sanitized_explicit_name or _default_output_name(candidate, jobs, role)
    return RewriteRequest(
        candidate_id=candidate["id"],
        source_resume_ids=_ordered_unique(resume_ids),
        job_ids=_ordered_unique(resolved_job_ids),
        output_name=_sanitize_output_name(generated_name),
    )


def export_ready_pdf(
    source_pdf: Path,
    output_dir: Path,
    output_name: str,
    run_id: str,
) -> Path:
    """Copy a READY artifact below a user-selected directory without overwriting."""
    source_input = Path(source_pdf)
    if _is_reparse_point(source_input):
        raise ValueError("ready PDF is unavailable")
    source = source_input.resolve(strict=True)
    if not source.is_file() or source.suffix.lower() != ".pdf":
        raise ValueError("ready PDF is unavailable")
    name = _validated_export_name(output_name)
    root_input = Path(output_dir)
    root_input.mkdir(parents=True, exist_ok=True)
    if _is_reparse_point(root_input):
        raise ValueError("output directory is unsafe")
    root = root_input.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("output directory is unavailable")

    suffix = run_id[-6:] if len(run_id) >= 6 else run_id
    candidate = root / name
    attempt = 0
    while True:
        target = candidate if attempt == 0 else _collision_name(root, name, suffix, attempt)
        resolved_target = target.resolve(strict=False)
        if resolved_target.parent != root:
            raise ValueError("export path escapes output directory")
        try:
            # Reserve before copy2 so an existing file is never overwritten.
            with target.open("xb"):
                pass
        except FileExistsError:
            attempt += 1
            continue
        except OSError as error:
            raise ValueError("unable to reserve export path") from error
        try:
            if _is_reparse_point(target):
                raise ValueError("export target is unsafe")
            shutil.copy2(source, target)
            return target
        except Exception:
            target.unlink(missing_ok=True)
            raise


def _resolve_candidate(
    repository: Repository,
    candidate_id: str | None,
    candidate_name: str | None,
    target_role: str | None,
) -> tuple[dict, str | None]:
    if candidate_id:
        if candidate_name or target_role:
            raise ValueError("candidate ID cannot be combined with candidate details")
        candidate = repository.get_candidate(candidate_id)
        return candidate, _first_role(candidate)
    name, role = _nonempty(candidate_name), _nonempty(target_role)
    if not name or not role:
        raise ValueError("candidate name and target role are required")
    return repository.create_candidate(name, [role], ""), role


def _reuse_or_import_resumes(
    repository: Repository,
    candidate_id: str,
    inputs: Iterable[tuple[str, str | Path]],
) -> list[str]:
    existing = {
        item["content_sha256"]: item["id"]
        for item in repository.list_source_resumes(candidate_id)
    }
    result: list[str] = []
    for kind, value in inputs:
        if kind == "id":
            resume = repository.get_source_resume(str(value))
            if resume["candidate_id"] != candidate_id:
                raise ValueError("source resume belongs to another candidate")
            result.append(resume["id"])
        elif kind == "path":
            path = _readable_file(Path(value))
            if path.suffix.lower() not in _TEXT_RESUME_SUFFIXES:
                raise ValueError("resume paths must be .md or .txt")
            content = path.read_text(encoding="utf-8-sig")
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            resume_id = existing.get(digest)
            if resume_id is None:
                resume = repository.create_source_resume(candidate_id, path.name, content)
                resume_id = resume["id"]
                existing[digest] = resume_id
            result.append(resume_id)
        else:
            raise ValueError("resume input is invalid")
    return _ordered_unique(result)


def _reuse_or_import_jobs(
    repository: Repository,
    candidate_id: str,
    target_role: str | None,
    inputs: Iterable[tuple[str, str | Path]],
) -> tuple[list[str], list[dict]]:
    result: list[str] = []
    jobs: list[dict] = []
    by_id: dict[str, dict] = {}
    for kind, value in inputs:
        if kind == "id":
            job = repository.get_job(str(value))
            if job.get("candidate_id") != candidate_id:
                raise ValueError("job belongs to another candidate")
        elif kind == "path":
            path = _readable_file(Path(value))
            body = path.read_text(encoding="utf-8-sig").strip()
            if not body:
                raise ValueError("job description is empty")
            fingerprint = hashlib.sha256(body.encode("utf-8")).hexdigest()
            job = repository.find_job_by_fingerprint(candidate_id, fingerprint)
            if job is None:
                title, salary, detail = parse_raw_job(body)
                title = _nonempty(title) or target_role
                if not title:
                    raise ValueError("job title or target role is required")
                detail = _nonempty(detail) or body
                job = repository.create_job({
                    "candidate_id": candidate_id,
                    "source": "local-file",
                    "title": title,
                    "salary": salary,
                    "detail": detail,
                    "company": _job_company(body),
                    "job_fingerprint": fingerprint,
                })
        else:
            raise ValueError("job input is invalid")
        if job["id"] not in by_id:
            result.append(job["id"])
            jobs.append(job)
            by_id[job["id"]] = job
    return result, jobs


def _default_output_name(candidate: dict, jobs: list[dict], target_role: str | None) -> str:
    candidate_label = _nonempty(candidate.get("display_name")) or "resume"
    first_job = jobs[0]
    job_label = _nonempty(first_job.get("company")) or _nonempty(first_job.get("title")) or target_role or "target"
    return f"{candidate_label} {job_label}-定制版.pdf"


def _job_company(body: str) -> str:
    sections = re.split(r"\n\s*\n", body)
    for section in sections:
        lines = section.splitlines()
        if not lines:
            continue
        heading = lines[0].strip().lstrip("#").strip().lower()
        if heading in {"公司", "company"}:
            return "\n".join(lines[1:]).strip()
    return ""


def _sanitize_output_name(value: str) -> str:
    value = _INVALID_FILENAME.sub("-", str(value)).strip()
    stem = value[:-4] if value.lower().endswith(".pdf") else value
    stem = stem.rstrip(". ")
    if not stem:
        stem = "resume"
    device_stem = stem.split(".", 1)[0].rstrip(". ").upper()
    if device_stem in _WINDOWS_RESERVED:
        stem = f"resume-{device_stem}"
    return _validated_export_name(_cap_output_filename(f"{stem}.pdf"))


def _validated_export_name(value: str) -> str:
    if not isinstance(value, str) or not value or Path(value).name != value:
        raise ValueError("export filename is unsafe")
    if any(character in value for character in '<>:"/\\|?*') or value.rstrip(". ") != value:
        raise ValueError("export filename is unsafe")
    if not value.lower().endswith(".pdf"):
        value += ".pdf"
    if len(value) > 180:
        raise ValueError("export filename is unsafe")
    stem = Path(value).stem.split(".", 1)[0].rstrip(". ").upper()
    if stem in _WINDOWS_RESERVED:
        raise ValueError("export filename is unsafe")
    return value


def _validate_explicit_output_name(value: str) -> None:
    if not isinstance(value, str) or not value or Path(value).name != value:
        raise ValueError("output filename is unsafe")
    if "\x00" in value or "/" in value or "\\" in value or value in {".", ".."}:
        raise ValueError("output filename is unsafe")


def _cap_output_filename(value: str) -> str:
    if len(value) <= 180:
        return value
    suffix = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    ending = f"-{suffix}.pdf"
    stem = Path(value).stem[: 180 - len(ending)].rstrip(". ") or "resume"
    return f"{stem}{ending}"


def _collision_name(root: Path, name: str, suffix: str, attempt: int) -> Path:
    stem = Path(name).stem
    marker = f"-{suffix}" if attempt == 1 else f"-{suffix}-{attempt}"
    max_stem_length = 180 - len(marker) - len(".pdf")
    stem = stem[:max_stem_length].rstrip(". ") or "resume"
    return root / f"{stem}{marker}.pdf"


def _readable_file(value: Path) -> Path:
    path = Path(value).resolve(strict=True)
    if not path.is_file():
        raise ValueError("input path is unavailable")
    return path


def _is_reparse_point(path: Path) -> bool:
    try:
        is_junction = bool(getattr(path, "is_junction", lambda: False)())
    except OSError:
        is_junction = True
    try:
        attributes = os.lstat(path).st_file_attributes
    except (AttributeError, OSError):
        attributes = 0
    return path.is_symlink() or is_junction or bool(attributes & 0x400)


def _nonempty(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _first_role(candidate: dict) -> str | None:
    roles = candidate.get("target_roles")
    return _nonempty(roles[0]) if isinstance(roles, list) and roles else None


def _ordered_unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _combined_inputs(
    ids: Iterable[str], paths: Iterable[Path],
    inputs: Iterable[tuple[str, str | Path]] | None,
) -> list[tuple[str, str | Path]]:
    if inputs is not None:
        return list(inputs)
    return [("id", value) for value in ids] + [("path", value) for value in paths]


def _build_parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(prog="rewrite_resume")
    commands = parser.add_subparsers(
        dest="command", required=True, parser_class=_SafeArgumentParser
    )
    run = commands.add_parser("run")
    run.add_argument("--candidate-id")
    run.add_argument("--candidate-name")
    run.add_argument("--target-role")
    run.add_argument("--source-resume-id", dest="resume_inputs", action=_InputAction, kind="id")
    run.add_argument("--resume-path", dest="resume_inputs", action=_InputAction, kind="path")
    run.add_argument("--job-id", dest="job_inputs", action=_InputAction, kind="id")
    run.add_argument("--jd-path", dest="job_inputs", action=_InputAction, kind="path")
    run.add_argument("--output-name")
    run.add_argument("--output-dir")
    resume = commands.add_parser("resume")
    resume.add_argument("--run-id", required=True)
    resume.add_argument("--facts-json", required=True)
    resume.add_argument("--output-dir")
    return parser


def _build_pipeline(repository: Repository, settings):
    return create_rewrite_pipeline(repository, settings)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
        settings = get_settings()
        repository = Repository(settings.database_path)
        pipeline = _build_pipeline(repository, settings)
        if args.command == "run":
            resume_inputs = args.resume_inputs or []
            job_inputs = args.job_inputs or []
            request = bootstrap_request(
                repository, args.candidate_id, args.candidate_name, args.target_role,
                [Path(value) for kind, value in resume_inputs if kind == "path"],
                [Path(value) for kind, value in job_inputs if kind == "path"],
                args.output_name,
                source_resume_ids=[value for kind, value in resume_inputs if kind == "id"],
                job_ids=[value for kind, value in job_inputs if kind == "id"],
                resume_inputs=resume_inputs,
                job_inputs=job_inputs,
            )
            run = pipeline.run(request)
        else:
            answers = _read_fact_answers(Path(args.facts_json))
            run = pipeline.resume(args.run_id, answers)
        exported = _export_if_ready(run, args.output_dir)
        _print_result(run, exported)
        return _status_exit_code(run.get("status"))
    except (OSError, ValueError, ValidationError, NotFoundError, json.JSONDecodeError):
        _print_result({"id": None, "status": "FAILED", "blocking_questions": []}, None)
        print("resume rewrite command failed", file=sys.stderr)
        return 1
    except Exception:
        _print_result({"id": None, "status": "FAILED", "blocking_questions": []}, None)
        print("resume rewrite command failed", file=sys.stderr)
        return 1


def _read_fact_answers(path: Path) -> list[FactAnswer]:
    raw = json.loads(_readable_file(path).read_text(encoding="utf-8-sig"))
    facts = FactAnswersRequest.model_validate_json(
        json.dumps({"fact_answers": raw}, ensure_ascii=False), strict=True
    ).fact_answers
    if len({item.question_id for item in facts}) != len(facts):
        raise ValueError("duplicate fact answers are not allowed")
    return facts


def _export_if_ready(run: dict, output_dir: str | None) -> Path | None:
    if run.get("status") != "READY":
        return None
    source = Path(run["artifact_dir"]) / "resume.pdf"
    directory = Path(output_dir) if output_dir else Path.cwd() / "resume-agent-exports"
    return export_ready_pdf(source, directory, run["output_name"], run["id"])


def _print_result(run: dict, exported: Path | None) -> None:
    result = {
        "id": run.get("id"),
        "status": run.get("status", "FAILED"),
        "blocking_questions": run.get("blocking_questions", []),
        "exported_path": str(exported) if exported else None,
    }
    print(json.dumps(_redact_nested(result), ensure_ascii=False, separators=(",", ":")))


def _redact_nested(value):
    if isinstance(value, str):
        return _PHONE.sub(
            "[REDACTED_PHONE]", _EMAIL.sub("[REDACTED_EMAIL]", value)
        )
    if isinstance(value, dict):
        return {key: _redact_nested(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_nested(item) for item in value]
    return value


def _status_exit_code(status: object) -> int:
    if status == "READY":
        return 0
    if status == "NEEDS_FACTS":
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
