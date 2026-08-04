# Manual ATS Web verification

Verified on 2026-08-04 against the local development servers:

- UI: `http://localhost:3000/`
- API: `http://127.0.0.1:8010/job-sources/ats/detect`

## Browser interaction

1. Loaded the rendered page and confirmed the “本地实时检测” and local Python
   service labels.
2. Cleared the seven public presets, added `https://127.0.0.1/jobs`, and ran one
   real request without making a public-network request.
3. Confirmed the returned canonical report rendered as one `unknown` / “已阻断”
   result, the blocked summary changed to `1`, and export became enabled.
4. Stopped the API, reran detection, and confirmed the explicit “本地检测失败”
   state plus both retry affordances. While the request was running, all selection
   controls, checkboxes, and add-URL controls were disabled.
5. Restarted the API and used “重试检测”; the same successful blocked result and
   summary returned.

## Automated checks

- `python -m unittest -v`: 124 tests passed.
- ATS / CLI / Web focused suite: 68 tests passed.
- `npm test`: vinext production build and 2 rendered/source contract tests passed.
- `npm run lint`: passed with zero warnings or errors.
- `git diff --check`: passed; Git only reported the repository's expected LF to
  CRLF conversion notices.
