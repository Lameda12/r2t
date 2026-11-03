"""Typer commands and Rich presentation only."""

from __future__ import annotations

import typer

from . import __version__

app = typer.Typer(
    name="r2t",
    help="Found a cool repo? Try it locally.",
    no_args_is_help=True,
    add_completion=False,
)


def _version(value: bool) -> None:
    if value:
        typer.echo(f"r2t {__version__}")
        raise typer.Exit(0)


@app.callback()
def _root(
    version: bool = typer.Option(
        False, "--version", callback=_version, is_eager=True, help="Show the version and exit."
    ),
) -> None:
    pass


def main() -> None:
    app()
