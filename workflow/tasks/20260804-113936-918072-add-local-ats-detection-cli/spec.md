# Local ATS detection CLI

## Goal

Provide a local research-mode command that runs the existing bounded ATS batch detector for an explicitly supplied list of careers-page URLs and emits the existing privacy-safe report format.

## Existing behavior

- `detect_ats_urls` accepts at most 100 caller-supplied URLs, uses 1-8 workers, applies the existing HTTP/robots/SSRF policy, preserves input positions, and deduplicates transport work.
- `serialize_ats_batch_report` emits deterministic JSON or JSONL without input URLs, query strings, tokens, response bodies, or transport details.
- There is no command-line entry point. The local frontend is a simulation and is not connected to Python.

## Proposed interface

```text
python -m job_sources.ats.cli --input <FILE|-> [--format json|jsonl] [--workers 1..8]
```

- `--input` is required. `-` reads standard input; otherwise it names a local file.
- Input is UTF-8, one URL per line. Blank lines are ignored; nonblank lines are stripped at both ends and otherwise preserved.
- Input is bounded to 100 nonblank URLs, 8,192 UTF-8 bytes per physical line, and 128 KiB total.
- `--format` defaults to `json`; `jsonl` uses the existing serializer contract.
- `--workers` defaults to 4 and accepts integers from 1 through 8.
- A successful report is written to standard output with one trailing newline.
- Diagnostics are stable symbolic messages on standard error and never echo argument values, file paths, URLs, query strings, tokens, or bodies.

## Exit codes

- `0`: a report was produced, including reports where every item is blocked by safety policy.
- `1`: an unexpected detection or output failure prevented a report.
- `2`: invalid arguments or invalid/unavailable input.

## Module design

The command is a thin local adapter over the existing deep batch module. Its public test seam is module execution (`python -m job_sources.ats.cli`). Parsing, bounded UTF-8 input, safe diagnostics, batch invocation, serialization, and exit-code mapping remain private implementation details. No second detector interface is introduced.

## Non-goals

- No URL discovery, crawling, sitemap or search-engine traversal.
- No API token, login, cookies, CAPTCHA bypass, browser automation, or private/write endpoints.
- No web server, public HTTP route, scheduler, database, or frontend integration.
- No output-file management; callers may use shell redirection.
- No ATS vendor-rule changes.

## Acceptance criteria

1. A UTF-8 input file containing an explicitly approved URL produces parseable JSON using the existing report schema.
2. `--input -` accepts piped UTF-8 input, and `--format jsonl` emits detection records before cluster records.
3. `--workers` is constrained to 1-8 and inputs are rejected when empty, over 100 URLs, over the byte bounds, unavailable, or invalid UTF-8.
4. Invalid arguments and input failures return `2`; unexpected execution failures return `1`; successful reports, including all-blocked reports, return `0`.
5. Standard output and standard error never expose a supplied URL, query string, token, response body, or rejected argument value.
6. The existing ATS detector and serializer tests continue to pass, and the frontend remains unchanged.

## Verification

- Run the CLI contract tests through module execution.
- Run the full existing `test_ats_detection.py` suite.
- Compile the Python sources.
- Review separately for specification compliance and repository/security standards.
