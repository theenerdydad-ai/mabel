"""Run MABEL's local development server."""

import uvicorn

from mabel.config import Settings


def main() -> None:
    settings = Settings.from_environment()
    uvicorn.run("server:app", host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
