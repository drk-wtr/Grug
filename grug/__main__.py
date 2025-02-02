import contextlib

import anyio
from loguru import logger

from grug.db import init_db
from grug.discord_client import DiscordClient
from grug.scheduler import start_scheduler
from grug.settings import settings

# TODO: evaluate llm caching: https://python.langchain.com/api_reference/community/cache.html


# noinspection PyTypeChecker
async def main():
    """Main application entrypoint."""
    if not settings.discord_token:
        raise ValueError("`DISCORD_TOKEN` env variable is required to run the Grug bot.")
    if not settings.openai_api_key:
        raise ValueError("`OPENAI_API_KEY` env variable is required to run the Grug bot.")

    logger.info("Starting Grug...")

    init_db()

    async with anyio.create_task_group() as tg:
        tg.start_soon(DiscordClient().start, settings.discord_token.get_secret_value())
        tg.start_soon(start_scheduler)

    logger.info("Grug has shut down...")


if __name__ == "__main__":
    # Don't log the stack trace for a keyboard interrupt
    with contextlib.suppress(KeyboardInterrupt):
        anyio.run(main)

    logger.info("Shutting down Grug...")
