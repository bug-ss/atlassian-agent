"""Turning exception trees into something a person can act on.

The MCP client runs its session inside an anyio task group, so almost every
failure arrives wrapped in an `ExceptionGroup` whose own message is
"unhandled errors in a TaskGroup" - true, and useless. The cause worth showing
is inside.
"""

from __future__ import annotations

from collections.abc import Iterator


def iter_causes(exc: BaseException) -> Iterator[BaseException]:
    """Flatten nested `ExceptionGroup`s into their leaf exceptions."""
    nested = getattr(exc, "exceptions", None)
    if nested:
        for sub in nested:
            yield from iter_causes(sub)
    else:
        yield exc


def explain(exc: BaseException, *, prefer: tuple[type[BaseException], ...] = ()) -> str:
    """The most useful message in an exception tree.

    `prefer` names exception types to surface ahead of the rest - typically the
    ones carrying a message written for the person reading it.
    """
    causes = list(iter_causes(exc))
    preferred = prefer or (ValueError, RuntimeError)
    for cause in causes:
        if isinstance(cause, preferred) and str(cause):
            return str(cause)
    for cause in causes:
        if str(cause):
            return f"{type(cause).__name__}: {cause}"
    # Some exceptions carry no message at all (ConnectionResetError); the type
    # name is still more informative than the group's "unhandled errors".
    if causes:
        return type(causes[0]).__name__
    return str(exc) or type(exc).__name__
