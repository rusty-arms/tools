# CLAUDE.md: tools

## What this repo is

`tools` holds the Rusty Arms tools: Python parsers and writers for the disc's file formats, validators, and later a level viewer and a Blender add-on. See [README.md](README.md).

Every parser is written **from a spec in the `specs` repo**, and from nothing else about the game. This is an implementer repo.

It holds **no code from the original game and no game data**, and it never will. Project policy, decisions and status live in `core`: `docs/clean-room.md`, `docs/decisions/`, `PROGRESS.md`.

## You are an implementer

- Work only from the `specs` repo, public documentation of GameCube hardware and media formats, and synthetic test fixtures.
- **Never** open the lab (`~/Develop/rusty-arms/ma-lab`), the disc, extracted files, traces or screenshots of the game. Never run a tool on them yourself.
- The owner runs the tools on the real disc, locally. You get back only pass or fail and which spec section failed, never the data.
- If a spec is unclear, missing or wrong, don't guess and don't look elsewhere. Open an issue in `specs` labelled `spec`, describe what's missing, and stop that part of the work.

## Clean-room rules (short form)

- **Never** access, search for, fetch, clone or read: the 2019 source leak or anything built on it; the community asset compiler; archive copies of the source or prototype discs; the community HD texture pack; decompiler output of the retail executable.
- Exavolt's docs are leads for observers only; implementers don't use them. DJs_MA_TOOLs is MIT, but implementers still work from the specs, not from its code.
- If forbidden material appears anyway: stop, don't summarise or quote it, tell the owner, and log an incident in core's `docs/provenance-log.md`.
- **Never commit** retail game data, disc images, extracted files, savestates, traces, screenshots or captures of the game, converted assets, anything from the lab, or blocklist terms.
- **Test fixtures are synthetic.** Build them in the test from the spec's description, with made-up contents. Never copy bytes from a real file, even a header.
- In committed files, describe forbidden material generically. Never name the owner; write "the owner".
- Don't use the original game's name in package, module or command names.

## How the code is laid out

- `ratools/`: the package. Standard library only, Python 3.11 or later (Blender bundles 3.11).
- One module per format. Its docstring names the spec it implements and that spec's version, for example `Implements specs formats/<name>.md, version 1`. When the spec changes, update the module and the version together.
- `ratools/binary.py`: the big-endian reader that parsers share. GameCube data is big-endian.
- `tests/`: unit tests on synthetic fixtures. A format is done when every entry type round-trips: parse, write, and get the same bytes back.

## Session discipline

1. Start by reading core's `PROGRESS.md` and this repo's `git log -10`.
2. One task per session.
3. Make small commits, one per step.
4. Record where you stopped in core's `PROGRESS.md` before ending the session.

## Approval and guard rails

- Changes to `.github/` and `tests/` need the owner's explicit approval in the chat. The round-trip tests are the Phase 1 gate. CODEOWNERS enforces this on GitHub as well.
- **Never weaken** a test, a validator or a round-trip check to make it pass. Fix the code, or raise the spec question.
- **Never weaken or edit** `scripts/check_no_retail.py`, `scripts/check_links.py`, `.githooks/` or `.github/workflows/guard.yml`. They are copies of core's: change them in core first, then copy them here unchanged.
- Do not route around a blocked tool call. A block is the answer; report it.

## Checks

```sh
python3 -m unittest discover -s tests
python3 scripts/check_no_retail.py --all
python3 scripts/check_links.py
yamllint -s .
```
