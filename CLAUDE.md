# Working in this repository

Reid's fork of DCS Retribution (rmac7664/dcs-retribution). Reid has no coding
background: explain things plainly, without jargon.

## Branches

- **Never commit to `dev`.** It mirrors upstream dcs-retribution/dcs-retribution.
- **`logistics`** holds the logistics mod. It is what goes to the Retribution devs.
- **`personal/my-tweaks`** holds Reid's own additions (DCS Escalation merged in,
  DCS-ATC radio presets, Carrier Deck Clear, ATO timeline, KC-135 speed, state.json
  path). It is built on top of `logistics`; bring logistics fixes in by
  cherry-picking them.
- Personal changes never go into `logistics` or upstream.
- Keep to these two working branches. A temporary branch is fine for a large job,
  but fold it back and delete it afterwards.

## How to work

- Batch small fixes into a queue and ship them together, unless Reid says to ship
  now or "run the queue".
- Ask before anything destructive: deleting branches or files, force-pushing,
  rewriting history.
- Don't change git settings without asking.
- Run black and mypy (`mypy game`, `mypy tests`) and the tests before pushing.
- Test a theory by generating a mission or running a turn on one of Reid's saves,
  not by reasoning alone.
- Answer DCS and Retribution questions from the code and data files, not from
  memory. Search the web if those don't settle it.
- After a mission, check the DCS log, state.json and the Retribution log before
  Reid accepts the results.

## Writing

- Use plain, professional language in comments, docstrings, commit messages and
  pull request descriptions.
- Changelog entries: one or two short lines each, no line breaks, with separate
  sections for features and fixes.
- Anything public (commits, pull requests, changelog) is written for any reader:
  no messages to Reid and no stories about test campaigns.

## Notes

- GitHub Actions on the fork can get switched off; if pushes don't start a Build
  run, check the Actions tab.
- The test suite needs a Windows-like setup and plenty of memory; GitHub's Windows
  runners run all of it. Builds download from the Build run's Artifacts.
