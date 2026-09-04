from __future__ import annotations

import logging

import typer

from worker_common.config import load_worker_config

from .runner import AggregatorRunner


app = typer.Typer(name="kswarm-aggregator-runner", no_args_is_help=False)


@app.callback(invoke_without_command=True)
def run(
    once: bool = typer.Option(False, "--once", help="Run one aggregation pass and exit."),
    allow_completed_branches: bool = typer.Option(False, "--allow-completed-branches", help="Allow completed, attested branches before settlement for local demos."),
    log_level: str = typer.Option("INFO", "--log-level", help="Python logging level."),
) -> None:
    logging.basicConfig(level=getattr(logging, log_level.upper()), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    runner = AggregatorRunner(load_worker_config("aggregator_runner"), allow_completed_branches=allow_completed_branches)
    if once:
        runner.run_once()
    else:
        runner.serve_forever()


def main() -> None:
    app()


if __name__ == "__main__":
    main()

