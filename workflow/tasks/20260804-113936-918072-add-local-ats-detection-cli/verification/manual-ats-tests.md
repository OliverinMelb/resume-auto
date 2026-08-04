# Manual ATS verification

- started: `2026-08-04T12:02:05.1641177+08:00`
- ended: `2026-08-04T12:02:14.2052392+08:00`
- interpreter: `Python 3.12.13` from the project `.venv`

## Commands

```powershell
.\.venv\Scripts\python.exe -m unittest -v test_ats_detection.py test_ats_detection_cli.py
.\.venv\Scripts\python.exe -m compileall -q job_sources\ats test_ats_detection.py test_ats_detection_cli.py
git diff --check
```

## Results

- `unittest`: PASS, 53 tests, 6.039 seconds
- `compileall`: PASS
- `git diff --check`: PASS

The test matrix contains the 42 pre-existing detector, HTTP-policy, batch, serializer, and clustering tests plus 11 CLI contract tests. Ten CLI cases execute `python -m job_sources.ats.cli` through a subprocess helper. The output-failure case also executes the module in a subprocess and closes its stdout pipe to verify safe exit code `1`.
