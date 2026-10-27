# Rusty Arms tools

Python tools for reading the disc's file formats: parsers and writers, validators, and later a level viewer and a Blender add-on. They are Phase 1 of the [roadmap](https://github.com/rusty-arms/core/blob/main/docs/roadmap.md), "Read the whole disc".

This repo holds **only code**. It has no game data and no code from the original game, and it never will. Every parser is written from a spec in [`specs`](https://github.com/rusty-arms/specs), the project's clean-room wall, and tested on made-up data ([clean-room policy](https://github.com/rusty-arms/core/blob/main/docs/clean-room.md)).

Standard library Python only, 3.11 or later.

## Status

Each format arrives after its spec:

| Format | Spec | Parser |
|---|---|---|
| MST archive | [mst-archive.md](https://github.com/rusty-arms/specs/blob/main/formats/mst-archive.md) | `ratools/mst.py`: reads, writes and checks; round-trips synthetic archives. Not yet run on the disc |

The Phase 1 exit gate: every entry type round-trips (parse, write, and get the same bytes back), and the level viewer's screenshots match Dolphin's.

## Layout

| Path | What it holds |
|---|---|
| `ratools/binary.py` | The big-endian `Reader` and `Writer` that every parser shares. Reads are bounds-checked. |
| `ratools/<format>.py` | One module per format. Its docstring names the spec it implements, and the spec's version. |
| `tests/` | Unit tests on synthetic fixtures, built from the spec's description. |

## Running the tests

```sh
python3 -m unittest discover -s tests
```

CI runs them on Python 3.11 and 3.13, on GitHub-hosted runners. Nothing in CI touches the disc.

## Using the tools on a real disc

You need your own disc and the files extracted from it. The tools read them where they are, and their output stays with them: never commit extracted files, converted assets or screenshots. In this project, the owner runs the tools in the local lab and reports only pass or fail to implementer sessions.

To check an MST archive against its spec, parse it, write it back and compare the bytes:

```sh
python3 -m ratools.mst check path/to/archive.mst
```

It prints only counts, offsets and the spec sections that pass or fail, never entry names or data, so its output can be shared. It exits with 0 when every section passes.

## Contributing

Read [CLAUDE.md](CLAUDE.md) first. It applies to people as well as agents. In short: work from the specs only, keep test fixtures synthetic, and never weaken a test or a round-trip check to make it pass. Turn on the pre-commit checks with:

```sh
git config core.hooksPath .githooks
```

## Licence

MIT OR Apache-2.0, at your option.
