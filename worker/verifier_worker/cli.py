from __future__ import annotations

import logging

import typer

from worker_common.config import load_worker_config

from .daemon import VerifierWorkerDaemon


app = typer.Typer(name="kswarm-verifier-worker", no_args_is_help=False)


@app.callback(invoke_without_command=True)
def run(
    once: bool = typer.Option(False, "--once", help="Run one verifier pass and exit."),
    log_level: str = typer.Option("INFO", "--log-level", help="Python logging level."),
) -> None:
    logging.basicConfig(level=getattr(logging, log_level.upper()), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    daemon = VerifierWorkerDaemon(load_worker_config("verifier_worker"))
    if once:
        daemon.ipfs.check()
        daemon.run_once()
    else:
        daemon.serve_forever()


def main() -> None:
    app()


if __name__ == "__main__":
    main()

