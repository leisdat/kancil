# Contributing to Kancil

Thanks for wanting to make Kancil better. Small, focused PRs beat big
rewrites — Kancil's whole point is staying tiny.

## Ground rules

1. **Stay small.** 106 KB, zero dependencies — that is a feature, not a
   limitation. A PR that adds a dependency needs a very good reason.
2. **No architecture rewrites, no duplicate features.** Keep the CLI/API
   backward compatible. Implement what's missing, don't rebuild what works.
3. **Real features, no demos.** If it ships, it must actually work and be
   tested.

## Workflow

```bash
git clone https://github.com/leisdat/kancil && cd kancil
# work on a branch
git checkout -b fix/short-description
```

## Tests

Everything must stay green:

```bash
python3 -m unittest tests.test_browser   # 102 unit tests, must pass offline
python3 -m unittest tests.test_live       # 5 live tests, needs network + Chromium
```

- New features need unit tests in `tests/test_browser.py`.
- Bug fixes should add a regression test when practical.

## Pull requests

- One concern per PR. Keep the diff reviewable.
- Describe what changed, why, and how you tested it.
- Update `CHANGELOG.md` and `README.md` / `README.id.md` if user-facing
  behavior changed.

## Good first issues

Look for issues labeled `good first issue` — usually small, well-scoped
fixes in the static engine, CLI, or docs.
