"""Discord bot interface for the Grug assistant server."""

from datetime import UTC, datetime, timedelta

import discord
import discord.utils
from discord import Member, VoiceState, app_commands
from discord.ext import voice_recv
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph.graph import CompiledGraph
from langgraph.prebuilt import create_react_agent
from langgraph.store.postgres import AsyncPostgresStore
from loguru import logger
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from grug.ai_agents.discord_voice_agent import SpeechRecognitionSink
from grug.ai_agents.should_respond_agent import EvaluationAgent
from grug.ai_tools import all_ai_tools
from grug.settings import settings
from grug.utils import InterceptLogHandler, get_interaction_response

# TODO: move these explanations to the docs and update the below comments to link to those docs
# Why the `members` intent is necessary for the Grug Discord bot:
#
# The `members` intent in a Discord bot is used to receive events and information about guild members. This includes
# receiving updates about members joining, leaving, or updating their presence or profile in the guilds (servers) the
# bot is part of. Specifically, for the Grug app, the `members` intent is necessary for several reasons:
#
# 1. **Initializing Guild Members**: When the bot starts and loads guilds, it initializes guild members by creating
# Discord accounts for them in the Grug database. This process requires access to the list of members in each guild.
# 2. **Attendance Tracking**: The bot tracks attendance for events. To do this effectively, it needs to know about all
# members in the guild, especially to send reminders or updates about events.
# 3. **Food Scheduling**: Similar to attendance tracking, food scheduling involves assigning and reminding members about
# their responsibilities. The bot needs to know who the members are to manage this feature.
# 4. **User Account Management**: The bot manages user accounts, including adding new users when they join the guild and
# updating user information. The `members` intent allows the bot to receive events related to these activities.
#
# Without the `members` intent, the bot would not be able to access detailed information about guild members, which
# would significantly limit its functionality related to user and event management.

# TODO: write out why the message_content intent is required (it's needed for the bot to make contextual responses)


class Client(discord.Client):
    ai_agent: CompiledGraph = None
    evaluation_agent = EvaluationAgent()

    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True

        super().__init__(intents=intents)

        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()


discord_client = Client()
discord.utils.setup_logging(handler=InterceptLogHandler())


# Command Error Handling
async def on_tree_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    return await get_interaction_response(interaction).send_message(
        content=str(error),
        ephemeral=True,
    )


discord_client.tree.on_error = on_tree_error


def get_bot_invite_url() -> str | None:
    return (
        f"https://discord.com/api/oauth2/authorize?client_id={discord_client.user.id}&permissions=8&scope=bot"
        if discord_client.user
        else None
    )


@discord_client.event
async def on_ready():
    """
    Event handler for when the bot is ready.

    Documentation: https://discordpy.readthedocs.io/en/stable/api.html#discord.on_ready
    """

    if discord_client.ai_agent is None:
        raise ValueError("ai_app not initialized in the Discord bot.")

    logger.info(f"Logged in as {discord_client.user} (ID: {discord_client.user.id})")
    logger.info(f"Discord bot invite URL: {get_bot_invite_url()}")


@discord_client.event
async def on_message(message: discord.Message):
    """on_message event handler for the Discord bot."""

    # TODO: make a tool that can search chat history for a given channel

    # ignore messages from self and all bots
    if message.author == discord_client.user or message.author.bot:
        return

    channel_is_text_or_thread = isinstance(message.channel, discord.TextChannel) or isinstance(
        message.channel, discord.Thread
    )

    should_respond = False
    if (
        settings.discord_bot_enable_contextual_responses
        and channel_is_text_or_thread
        and discord_client.user not in message.mentions
    ):
        # Evaluate the message to determine if the bot should respond
        response_eval = await discord_client.evaluation_agent.evaluate_message(
            message=message.content,
            conversation_history=[
                message.content
                async for message in message.channel.history(after=datetime.now(tz=UTC) - timedelta(days=1))
            ],
        )
        logger.info(f"response_eval: {response_eval}")

        # Determine if the bot should respond based on the evaluation scores
        should_respond = response_eval.relevance_score > 7 and response_eval.confidence_score > 5

    # Message is @mention or DM or should_respond is True
    # TODO: might need a tool that will get the image from a url and provide it to the model for image evaluation
    if (
        isinstance(message.channel, discord.DMChannel)
        or (channel_is_text_or_thread and discord_client.user in message.mentions)
        or should_respond
    ):
        async with message.channel.typing():

            ai_prompt = message.content

            # Handle replies
            message_replied_to: str | None = message.reference.resolved.content if message.reference else None
            if message_replied_to:
                ai_prompt = (
                    f'The following message is a reply to "{message_replied_to}".  '
                    f"the reply to that message is {message.content}, respond accordingly."
                )

            final_state = await discord_client.ai_agent.ainvoke(
                {"messages": [HumanMessage(content=ai_prompt)]},
                config={
                    "configurable": {
                        "thread_id": str(message.channel.id),
                        "user_id": f"{str(message.guild.id) + '-' if message.guild else ''}{message.author.id}",
                    }
                },
            )

            await message.channel.send(
                content=final_state["messages"][-1].content,
                reference=message if channel_is_text_or_thread else None,
            )


# TODO: change this so it's stored in the DB
voice_channel_dict = {}


@discord_client.event
async def on_voice_state_update(member: Member, before: VoiceState, after: VoiceState):
    # Ignore bot users
    if member.bot:
        return

    # If the user joined a voice channel
    if after.channel is not None:
        logger.info(f"{member.display_name} joined {after.channel.name}")

        # TODO: need a better way to determine which voice channel the bot should be in, i think having it auto join
        #       is the smoothest, but we need a way to configure that so that users can set which voice channel the bot
        #       connects to by default.

        # TODO: add tooling and store in DB for which voice channel the bot is assigned to be in.
        #       since bots can only be in one voice channel at a time, we need to handle this accordingly.
        # If the bot is not currently in the voice channel, connect to the voice channel
        if discord_client.user.id not in [member.id for member in after.channel.members]:
            voice_channel_dict[after.channel.id] = await after.channel.connect(cls=voice_recv.VoiceRecvClient)
            voice_channel_dict[after.channel.id].listen(SpeechRecognitionSink(discord_channel=after.channel))

    # If the user left a voice channel
    elif before.channel is not None:
        logger.info(f"{member.display_name} left {before.channel.name}")

        if len(before.channel.members) <= 1 and discord_client.user.id in [
            member.id for member in before.channel.members
        ]:
            voice_client = voice_channel_dict.get(before.channel.id)
            if voice_client:
                await voice_client.disconnect()
            else:
                logger.warning(f"Voice client not found for channel {before.channel.id}")


async def start_discord_bot():
    # Create the db schema for the scheduler
    async with await AsyncConnection.connect(settings.postgres_dsn.replace("+psycopg", "")) as conn:
        await conn.execute("CREATE SCHEMA IF NOT EXISTS genai")

    # Create a connection pool to the Postgres database for the Discord bot AI agents to use
    async with AsyncConnectionPool(
        conninfo=settings.postgres_dsn.replace("+psycopg", ""),
        max_size=20,
        kwargs={
            "autocommit": True,
            "prepare_threshold": 0,
            "row_factory": dict_row,
            "options": "-c search_path=genai",
        },
    ) as pool:
        # Configure `store` and `checkpointer` for long-term and short-term memory
        # (Ref: https://langchain-ai.github.io/langgraphjs/concepts/memory/#what-is-memory)
        store = AsyncPostgresStore(pool)
        await store.setup()
        checkpointer = AsyncPostgresSaver(pool)
        await checkpointer.setup()

        # Add the ReAct agent to the Discord client
        discord_client.ai_agent = create_react_agent(
            model=ChatOpenAI(
                model_name=settings.ai_openai_model,
                temperature=0,
                max_tokens=None,
                max_retries=2,
                openai_api_key=settings.openai_api_key,
            ),
            tools=all_ai_tools,
            checkpointer=checkpointer,
            store=store,
            state_modifier=settings.ai_base_instructions,
        )

        # Add the should_respond agent to the Discord client
        discord_client.evaluation_agent = EvaluationAgent()

        try:
            await discord_client.start(settings.discord_token.get_secret_value())
        finally:
            # Disconnect from all voice channels
            logger.info("Disconnecting from all voice channels...")
            for voice_client in voice_channel_dict.values():
                await voice_client.disconnect()

            # Close the Discord client
            logger.info("Closing the Discord client...")
            await discord_client.close()
