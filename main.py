import os
import asyncio
import logging
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict
from dotenv import load_dotenv

import interactions
from interactions.api.voice.audio import Audio
import redis.asyncio as redis
import uuid

# Load environment variables
load_dotenv()


# region Constants and Configuration
class Config:
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    AUDIOS_DIR = Path("audio")
    CONSENT_FILE = Path("consent.json")

    REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
    REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
    REDIS_DB = int(os.getenv("REDIS_DB", "0"))

    MESSAGE_FILES = {
        "start": Audio(Path("messages/start-recording.wav")),
        "stop": Audio(Path("messages/stop-recording.wav")),
        "error": Audio(Path("messages/error-message.wav")),
    }

    QUEUE_CONFIGURATION = {
        "AudioQueue": os.getenv("AUDIO_QUEUE").encode(),
        "RejectedAudioQueue": os.getenv("REJECTED_AUDIO_QUEUE").encode(),
        "ProcessedAudioQueue": os.getenv("PROCESSED_AUDIO_QUEUE").encode(),
    }


Config.AUDIOS_DIR.mkdir(parents=True, exist_ok=True)


# region Logging Configuration
def setup_logging() -> logging.Logger:
    """Configure and return the main application logger."""
    logger = logging.getLogger(__name__)
    logger.setLevel(logging.INFO)

    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s - %(filename)s:%(lineno)d"
    )

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    file_handler = logging.FileHandler("logs.log")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger


logger = setup_logging()
# endregion

redis_client = redis.Redis(
    host=Config.REDIS_HOST, port=Config.REDIS_PORT, db=Config.REDIS_DB
)


# region State Management
class VoiceStateManager:
    """Manages active voice recordings across guilds."""

    def __init__(self):
        self.active_recordings: Dict[int, interactions.ActiveVoiceState] = {}

    def add(self, guild_id: int, voice_state: interactions.ActiveVoiceState) -> None:
        self.active_recordings[guild_id] = voice_state

    def remove(self, guild_id: int) -> Optional[interactions.ActiveVoiceState]:
        return self.active_recordings.pop(guild_id, None)

    def get(self, guild_id: int) -> Optional[interactions.ActiveVoiceState]:
        return self.active_recordings.get(guild_id)


voice_state_manager = VoiceStateManager()


class ConsentManager:
    """Manages user consent storage and retrieval."""

    def __init__(self, consent_file: Path):
        self.consent_file = consent_file

    def load(self) -> Dict[str, bool]:
        """Load consent data from JSON file."""
        if not self.consent_file.exists():
            return {}

        try:
            with open(self.consent_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            logger.exception("Failed to load consent data")
            return {}

    def save(self, data: Dict[str, bool]) -> None:
        """Save consent data to JSON file."""
        try:
            with open(self.consent_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4)
        except Exception:
            logger.exception("Failed to save consent data")


consent_manager = ConsentManager(Config.CONSENT_FILE)
# endregion


# region Core Functionality
async def request_consent(
    ctx: interactions.SlashContext, member: interactions.Member
) -> None:
    """Send consent request to a user via DM or channel."""
    buttons = [
        interactions.Button(
            style=interactions.ButtonStyle.SUCCESS,
            label="Permitir",
            custom_id=f"consent_allow_{member.id}",
        ),
        interactions.Button(
            style=interactions.ButtonStyle.DANGER,
            label="Denegar",
            custom_id=f"consent_deny_{member.id}",
        ),
    ]

    try:
        await member.send("¿Consientes en que te robemos tu audio?", components=buttons)
    except Exception:
        logger.warning(
            f"Couldn't DM {member.username}, falling back to channel message"
        )
        await ctx.channel.send(
            f"{member.mention}, ¿Consientes en que te robemos tu audio? Responde aquí:",
            components=buttons,
        )


async def process_user_recording(
    ctx: interactions.SlashContext,
    user_id: int,
    file_path: Path,
    consent_data: Dict[str, bool],
    channel_id: int,
) -> Optional[Path]:
    """Process a single user's recording and return transcript path."""
    if not consent_data.get(str(user_id), False):
        file_path.unlink(missing_ok=True)
        logger.info(f"Deleted unconsented recording from {user_id}")
        return None

    member = ctx.guild.get_member(user_id)
    if not member or member.bot:
        return None

    # Create user directory
    safe_name = re.sub(r"[^\w\-_]", "_", member.username).strip("_")[:64]
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H-%M")
    user_dir = Config.AUDIOS_DIR / safe_name / timestamp
    user_dir.mkdir(parents=True, exist_ok=True)

    # Move recording to user directory
    identifier = str(uuid.uuid4())
    dest_path = (user_dir / f"{identifier}-{timestamp}").with_suffix(".wav")
    shutil.move(file_path, dest_path)
    await redis_client.publish(
        Config.QUEUE_CONFIGURATION["AudioQueue"],
        json.dumps(
            {
                "id": identifier,
                "path": str(dest_path),
                "userId": user_id,
                "user": member.username,
                "channelId": channel_id,
                "guildId": ctx.guild_id,
            }
        ),
    )
    return dest_path


# endregion

# region Bot Setup
bot = interactions.Client(
    activity=interactions.Activity(
        name="Doxeando gente", type=interactions.ActivityType.LISTENING
    ),
    token=Config.BOT_TOKEN,
    logger=logger,
)

queue = redis_client.pubsub()


@interactions.component_callback(
    re.compile(r"consent_allow_*"), re.compile(r"consent_deny_*")
)
async def handle_consent_response(ctx: interactions.ComponentContext):
    """Handle consent response from users."""
    custom_id = ctx.custom_id
    _, action, user_id = custom_id.split("_")

    if ctx.user.id != int(user_id):
        await ctx.send("No puedes responder a esta solicitud.", ephemeral=True)
        return

    consent_data = consent_manager.load()
    consent_data[user_id] = action == "allow"
    consent_manager.save(consent_data)

    await ctx.send("Tu preferencia ha sido guardada.", ephemeral=True)


@interactions.Task.create(interactions.IntervalTrigger(seconds=15))
async def scheduled_transcription_task(ctx: interactions.SlashContext):
    """Scheduled task to process recordings every 1 minutes."""

    if voice_state := voice_state_manager.get(ctx.guild_id):
        logger.info(f"Processing recordings in guild {ctx.guild_id}")
        try:
            await voice_state.stop_recording()
            consent_data = consent_manager.load()
            channel_id = voice_state.channel.id
            tasks = [
                process_user_recording(
                    ctx, user_id, Path(file_path), consent_data, channel_id
                )
                for user_id, file_path in voice_state.recorder.output.items()
            ]
            recording_paths = await asyncio.gather(*tasks)
            successful = sum(1 for t in recording_paths if t is not None)

            logger.info(f"Processed {successful} recording_paths in {ctx.guild_id}")

            await voice_state.start_recording(
                output_dir=Config.AUDIOS_DIR, encoding="wav"
            )

        except Exception:
            logger.exception("Error processing recordings")
            await ctx.send("Error al procesar las grabaciones", ephemeral=True)
            if voice_state.connected:
                await voice_state.play(Config.MESSAGE_FILES["error"])
                await voice_state.disconnect()


@interactions.slash_command(name="start", description="Inicia la grabación de voz")
@interactions.check(interactions.guild_only())
@interactions.max_concurrency(interactions.Buckets.GUILD, 1)
async def start_recording(ctx: interactions.SlashContext):
    """Start voice recording in the current channel."""
    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.send("Debes estar en un canal de voz.", ephemeral=True)
        return

    if voice_state_manager.get(ctx.guild_id):
        await ctx.send("Ya hay una grabación activa.", ephemeral=True)
        return

    try:
        voice_state = await ctx.author.voice.channel.connect()
        await voice_state.start_recording(output_dir=Config.AUDIOS_DIR, encoding="wav")
        voice_state_manager.add(ctx.guild_id, voice_state)
        voice_state.play_no_wait(Config.MESSAGE_FILES["start"])

        # Request consent from new members
        consent_data = consent_manager.load()
        members_needing_consent = [
            m
            for m in ctx.author.voice.channel.members
            if not m.bot and str(m.id) not in consent_data
        ]

        for member in members_needing_consent:
            await request_consent(ctx, member)

        await ctx.send("🎙️ Grabación iniciada!")
        scheduled_transcription_task.start(ctx)

    except Exception:
        logger.exception("Error starting recording")
        await ctx.send("Error al iniciar la grabación", ephemeral=True)
        if voice_state := voice_state_manager.get(ctx.guild_id):
            await voice_state.play(Config.MESSAGE_FILES["error"])
            await voice_state.disconnect()


@interactions.slash_command(name="stop", description="Detiene la grabación de voz")
@interactions.check(interactions.guild_only())
@interactions.max_concurrency(interactions.Buckets.GUILD, 1)
async def stop_recording(ctx: interactions.SlashContext):
    """Stop active voice recording."""
    if not (voice_state := voice_state_manager.remove(ctx.guild_id)):
        await ctx.send("No hay grabaciones activas.", ephemeral=True)
        return

    try:
        scheduled_transcription_task.stop()
        await voice_state.stop_recording()
        task = voice_state.play_no_wait(Config.MESSAGE_FILES["stop"])

        # Process remaining recordings
        consent_data = consent_manager.load()
        tasks = [
            process_user_recording(
                ctx, user_id, Path(file_path), consent_data, voice_state.channel.id
            )
            for user_id, file_path in voice_state.recorder.output.items()
        ]

        recording_paths = await asyncio.gather(*tasks)
        successful = sum(1 for t in recording_paths if t is not None)

        await ctx.send(f"⏹️ Grabación detenida. Procesadas {successful} grabaciones")
        await task
        await voice_state.disconnect()

    except Exception:
        logger.exception("Error stopping recording")
        await ctx.send("Error al detener la grabación", ephemeral=True)
        if voice_state.connected:
            await voice_state.play(Config.MESSAGE_FILES["error"])
            await voice_state.disconnect()


@interactions.listen()
async def on_ready():
    logger.info(f"Logged in as {bot.user.username}#{bot.user.discriminator}")
    await queue.subscribe(Config.QUEUE_CONFIGURATION["RejectedAudioQueue"])
    counter = 0
    async for message in queue.listen():
        if message["type"] != "message":
            continue
        body = json.loads(message["data"])
        if message["channel"] == Config.QUEUE_CONFIGURATION["RejectedAudioQueue"]:
            if counter != 7:
                continue
            if voice_state := voice_state_manager.get(body["guildId"]):
                voice_state.current_audio
                if not voice_state.connected:
                    continue
                await voice_state.play(Config.MESSAGE_FILES["error"])
                counter = 0

        counter += 1


# Handle voice user join event
@interactions.listen(interactions.events.VoiceUserJoin)
async def on_voice_user_join(event: interactions.events.VoiceUserJoin):
    if event.author.bot or event.author.id == bot.user.id:
        return

    voice_state = voice_state_manager.get(event.channel.guild.id)
    if not voice_state or event.channel.id != voice_state.channel.id:
        return

    consent_data = consent_manager.load()
    if str(event.author.id) not in consent_data:
        await request_consent(event, event.author)


# Handle voice user leave event
@interactions.listen(interactions.events.VoiceUserLeave)
async def on_voice_user_leave(event: interactions.events.VoiceUserLeave):
    if event.author.bot or event.author.id == bot.user.id:
        return

    voice_state = voice_state_manager.get(event.channel.guild.id)
    if not voice_state or event.channel.id != voice_state.channel.id:
        return

    consent_data = consent_manager.load()
    remaining = [
        m
        for m in event.channel.members
        if not m.bot and str(m.id) in consent_data and m.id != event.author.id
    ]
    if not remaining:
        scheduled_transcription_task.stop()
        await voice_state.disconnect()
        voice_state_manager.remove(event.channel.guild.id)
        await event.channel.send("Todos los miembros han abandonado el canal.")


# endregion

# region Main Execution
if __name__ == "__main__":
    try:
        logger.info("Starting bot...")
        bot.start()
    except KeyboardInterrupt:
        logger.info("Shutting down bot...")
    finally:
        for guild_id in list(voice_state_manager.active_recordings):
            if voice_state := voice_state_manager.remove(guild_id):
                asyncio.run(voice_state.disconnect())
        logger.info("Bot stopped.")
# endregion
