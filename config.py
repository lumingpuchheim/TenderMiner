"""TenderMining configuration — where the state lives.

doc/STORAGE.md 6.1. One question, answered in one place: **which directory holds
this deployment's state?** (Two, since 6.5: the trained-model registry answers
it separately — see `models_root` for why it is its own variable and not a
subdirectory of the first.)

Until now the answer was hardcoded eighteen times as the string `'data'`, and
once as `REPO / 'data'` — the code directory's own path. That is the coupling
this module removes: an image ships code, state must outlive any container, and
the two cannot be the same directory.

Resolution order, most specific first:

1. an explicit `--data-dir` on the command line
2. the `TM_DATA_DIR` environment variable
3. `./data` — today's behaviour, unchanged, so nothing breaks by default

The default is deliberately CWD-relative rather than repository-relative. A
cycle run from the checkout resolves exactly as it always has; a cycle run
anywhere else, or in a container with `TM_DATA_DIR=/data`, resolves to state
that has nothing to do with where the code happens to sit.

Nothing here moves any data. Choosing a location and relocating the existing
state is an operator decision — `inside_checkout()` exists so the programs can
point out that it has not been made yet, without making it for them.
"""

import os
from pathlib import Path

ENV_VAR = 'TM_DATA_DIR'
FALLBACK = 'data'

MODELS_ENV_VAR = 'TM_MODELS_DIR'
MODELS_FALLBACK = 'models'

REPO = Path(__file__).resolve().parent


def data_root(explicit=None):
    """The state directory for this run, as a Path. See the module docstring
    for the order. Not created here: a missing root is a real condition that
    each program reports in its own words."""
    if explicit:
        return Path(explicit)
    env = os.environ.get(ENV_VAR)
    if env:
        return Path(env)
    return Path(FALLBACK)


def models_root(explicit=None):
    """The trained-model registry's directory — `registry.jsonl`, `CURRENT`
    and the model binaries. Same three-step order as `data_root`, its own
    variable, and the same CWD-relative default, so nothing moves by itself.

    It is separate from the data root because 5.1 has not been decided: the
    registry may yet become rows in the database rather than a directory. It
    is *configurable* because 6.5 found it was the one piece of state still
    named relative to the working directory — a cycle in a container would
    have written its promoted model into the image's own code directory,
    where the next container would not find it.
    """
    if explicit:
        return Path(explicit)
    env = os.environ.get(MODELS_ENV_VAR)
    if env:
        return Path(env)
    return Path(MODELS_FALLBACK)


def inside_checkout(root=None):
    """Is the state sitting inside the code checkout?

    True is not an error — it is today's default and it works. It is worth
    saying out loud because it is the one thing standing between this system
    and being containerised, and because a `git clean` in the wrong mood would
    take the ledgers with it.
    """
    root = Path(root or data_root()).resolve()
    return root == REPO or REPO in root.parents


def describe(root=None):
    """One line for a program to print at startup, so which state is in use is
    never a guess."""
    root = Path(root or data_root())
    src = ('--data-dir' if root != data_root(None) else
           f'${ENV_VAR}' if os.environ.get(ENV_VAR) else f'default ./{FALLBACK}')
    warn = '  (inside the code checkout — see doc/STORAGE.md 6.1)' \
        if inside_checkout(root) else ''
    return f'{root.resolve()} [{src}]{warn}'
