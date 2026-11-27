"""Run the Atlas web surface.

  python -m scripts.run_web              # binds to 127.0.0.1:8000
  python -m scripts.run_web --port 8765  # different port

Runs alongside scripts/run_worker.py. The worker owns the
scheduler; the web app gives you the dashboard, PWA and API.
Open http://localhost:8000 after start.

Security: 127.0.0.1 only by default. Do not flip to 0.0.0.0 without
adding auth. Tailscale is the recommended way to access from a phone.
"""

from __future__ import annotations

import argparse
import logging

import uvicorn

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s :: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S%z",
)
from core.logredact import install as _install_redaction  # noqa: E402 - after the handlers exist

_install_redaction()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true",
                        help="dev mode; auto-reload on code change")
    parser.add_argument("--log-file", default="",
                        help="write all output here instead of the inherited console (the Windows service uses logs\\web.log)")
    args = parser.parse_args()

    if args.log_file:
        from core.service_io import use_log_file, watchdog

        use_log_file(args.log_file)
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)-7s %(name)s :: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
            force=True,
        )
        _install_redaction()   # force=True replaced the handlers
        # If the app has not finished starting in 90 s, the log gets every stack.
        watchdog(90)

    print(f"Atlas web surface starting on http://{args.host}:{args.port}")
    print("Open that URL in your browser.")
    uvicorn.run(
        "core.web.api:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
        # The dashboard polls about twenty endpoints every refresh; one line per
        # request buried the errors and filled the service log.
        access_log=False,
        # The phone keeps a connection open; without a limit, a stop waited on it
        # forever and the service never restarted cleanly.
        timeout_graceful_shutdown=5,
    )


if __name__ == "__main__":
    main()
