"""Error type and exit codes shared by every finvault command."""

from __future__ import annotations

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_BUSY = 3
EXIT_MISMATCH = 4
EXIT_CONCURRENT_CHANGE = 5


class FinvaultError(Exception):
    """A failure the user can act on.

    ``message`` says what went wrong; ``hint`` says what to do about it.
    """

    def __init__(
        self,
        message: str,
        hint: str = "",
        exit_code: int = EXIT_FAILURE,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.exit_code = exit_code
        self.details: dict[str, object] = details or {}

    def __str__(self) -> str:
        if self.hint:
            return f"{self.message} — {self.hint}"
        return self.message


class ConcurrentChangeError(FinvaultError):
    """A shared file changed while finvault was rewriting it."""

    def __init__(self, path: str) -> None:
        super().__init__(
            f"{path} changed while finvault was rewriting it; nothing was changed",
            "another Claude Code session wrote to it; run the command again",
            EXIT_CONCURRENT_CHANGE,
            {"path": path},
        )
