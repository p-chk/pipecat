#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#
import argparse
import asyncio
import os
import sys

from call_connection_manager import CallConfigManager, SessionManager
from dotenv import load_dotenv
from loguru import logger

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import EndTaskFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.llm_service import FunctionCallParams
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.transports.services.daily import DailyDialinSettings, DailyParams, DailyTransport
import time
from pipecat.frames.frames import OutputAudioRawFrame
from pipecat.processors.frame_processor import FrameDirection
from pipecat.pipeline.task import PipelineParams
from pipecat.frames.frames import TextFrame

from pipecat.processors.frame_processor import FrameProcessor
from pipecat.frames.frames import UserAudioRawFrame, OutputAudioRawFrame

class ActivityMonitor(FrameProcessor):
    def __init__(self):
        super().__init__()
        self.last_audio_time = time.time()

    def update_activity(self):
        self.last_audio_time = time.time()
        logger.debug(f"[ActivityMonitor] Updated last_audio_time: {self.last_audio_time}")

    async def process_frame(self, frame, direction):
        if isinstance(frame, (UserAudioRawFrame, OutputAudioRawFrame)):
            self.update_activity()
        await super().process_frame(frame, direction)



load_dotenv(override=True)

logger.remove(0)
logger.add(sys.stderr, level="DEBUG")

daily_api_key = os.getenv("DAILY_API_KEY", "")
daily_api_url = os.getenv("DAILY_API_URL", "https://api.daily.co/v1")


async def main(
    room_url: str,
    token: str,
    body: dict,
):
    # ------------ CONFIGURATION AND SETUP ------------

    # Create a config manager using the provided body
    call_config_manager = CallConfigManager.from_json_string(body) if body else CallConfigManager()

    # Get important configuration values
    test_mode = call_config_manager.is_test_mode()

    # Get dialin settings if present
    dialin_settings = call_config_manager.get_dialin_settings()

    # Initialize the session manager
    session_manager = SessionManager()
    last_audio_time = time.time()
    start_time = time.time()
    silence_events = 0
    prompt_count = 0


    def update_last_audio_time():
        nonlocal last_audio_time
        last_audio_time = time.time()
        logger.debug(f"[ActivityMonitor] Updated last_audio_time: {last_audio_time}")


    # ------------ TRANSPORT SETUP ------------

    # Set up transport parameters
    if test_mode:
        logger.info("Running in test mode")
        transport_params = DailyParams(
            api_url=daily_api_url,
            api_key=daily_api_key,
            audio_in_enabled=True,
            audio_out_enabled=True,
            video_out_enabled=False,
            vad_analyzer=SileroVADAnalyzer(),
            transcription_enabled=True,
        )
    else:
        daily_dialin_settings = DailyDialinSettings(
            call_id=dialin_settings.get("call_id"), call_domain=dialin_settings.get("call_domain")
        )
        transport_params = DailyParams(
            api_url=daily_api_url,
            api_key=daily_api_key,
            dialin_settings=daily_dialin_settings,
            audio_in_enabled=True,
            audio_out_enabled=True,
            video_out_enabled=False,
            vad_analyzer=SileroVADAnalyzer(),
            transcription_enabled=True,
        )

    # Initialize transport with Daily
    transport = DailyTransport(
        room_url,
        token,
        "Simple Dial-in Bot",
        transport_params,
    )

    # Initialize TTS
    tts = CartesiaTTSService(
        api_key=os.getenv("CARTESIA_API_KEY", ""),
        voice_id="b7d50908-b17c-442d-ad8d-810c63997ed9",  # Use Helpful Woman voice by default
    )

    # ------------ FUNCTION DEFINITIONS ------------

    async def terminate_call(params: FunctionCallParams):
        """Function the bot can call to terminate the call upon completion of a voicemail message."""
        if session_manager:
            # Mark that the call was terminated by the bot
            session_manager.call_flow_state.set_call_terminated()

        # Then end the call
        await params.llm.queue_frame(EndTaskFrame(), FrameDirection.UPSTREAM)

    # Define function schemas for tools
    terminate_call_function = FunctionSchema(
        name="terminate_call",
        description="Call this function to terminate the call.",
        properties={},
        required=[],
    )

    # Create tools schema
    tools = ToolsSchema(standard_tools=[terminate_call_function])

    # ------------ LLM AND CONTEXT SETUP ------------

    # Set up the system instruction for the LLM
    system_instruction = """You are Chatbot, a friendly, helpful robot. Your goal is to demonstrate your capabilities in a succinct way. Your output will be converted to audio so don't include special characters in your answers. Respond to what the user said in a creative and helpful way, but keep your responses brief. Start by introducing yourself. If the user ends the conversation, **IMMEDIATELY** call the `terminate_call` function. """

    # Initialize LLM
    llm = OpenAILLMService(
    api_key=os.getenv("OPEN_ROUTER_API_KEY"),
    base_url=os.getenv("OPEN_ROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
    model=os.getenv("OPEN_ROUTER_MODEL", "openai/gpt-3.5-turbo")
)

    # Register functions with the LLM
    llm.register_function("terminate_call", terminate_call)

    # Create system message and initialize messages list
    messages = [call_config_manager.create_system_message(system_instruction)]

    # Initialize LLM context and aggregator
    context = OpenAILLMContext(messages, tools)
    context_aggregator = llm.create_context_aggregator(context)

    # ------------ PIPELINE SETUP ------------

    # Build pipeline
    activity_monitor = ActivityMonitor()

    pipeline = Pipeline([
        transport.input(),
        activity_monitor,
        context_aggregator.user(),
        llm,
        tts,
        transport.output(),
        context_aggregator.assistant(),
    ])

    # Create pipeline task
    task = PipelineTask(pipeline, params=PipelineParams(allow_interruptions=True))

    async def silence_monitor(task, activity_monitor):
        nonlocal silence_events
        prompts = 0
        max_prompts = 3
        prompt_interval = 10
        check_interval = 1

        try:
            logger.debug("Warming up TTS engine...")
            async for _ in tts.run_tts(" "):
                break
            logger.debug("TTS warm-up complete.")
        except Exception as e:
            logger.error(f"TTS warm-up failed: {e}")

        while prompts < max_prompts:
            await asyncio.sleep(check_interval)

            time_since_audio = time.time() - activity_monitor.last_audio_time
            logger.info(f"Time since last audio: {time_since_audio}")

            if time_since_audio >= prompt_interval:
                prompts += 1
                silence_events += 1
                logger.info(f"No speech detected. Prompt {prompts} of {max_prompts}")
                
                try:
                    async for frame in tts.run_tts("Are you still there?"):
                        await task.queue_frames([frame])
                except Exception as e:
                    logger.error(f"TTS generation failed: {e}")
                
                activity_monitor.update_activity()

        logger.info("Ending call due to silence.")
        await task.queue_frames([EndTaskFrame()])
        await handle_participant_exit()



    # ------------ EVENT HANDLERS ------------

    @transport.event_handler("on_first_participant_joined")
    async def on_first_participant_joined(transport, participant):
        nonlocal start_time
        start_time = time.time()
        await transport.capture_participant_transcription(participant["id"])
        await task.queue_frames([context_aggregator.user().get_context_frame()])
        logger.debug(f"First participant joined: {participant['id']}")
        activity_monitor.update_activity()
        asyncio.create_task(silence_monitor(task, activity_monitor))

    @transport.event_handler("on_transcription_message")
    async def on_transcription_message(transport, message):
        nonlocal prompt_count
        text = message.get("text", "")
        is_final = message.get("rawResponse", {}).get("is_final", False)
        if text and is_final:
            logger.debug(f"Transcription received: {text}")
            prompt_count += 1
            activity_monitor.update_activity()


    @transport.event_handler("on_participant_left")
    async def on_participant_left(transport, participant, reason):
        logger.debug(f"Participant left: {participant}, reason: {reason}")
        await handle_participant_exit()

    
    async def handle_participant_exit():
        duration = time.time() - start_time
        logger.info(f"Participant left after {duration:.2f} seconds")
        logger.info(f"Total silence events: {silence_events}")
        logger.info(f"Total number of prompts: {prompt_count}")
        await task.cancel()

    # ------------ RUN PIPELINE ------------

    if test_mode:
        logger.debug("Running in test mode (can be tested in Daily Prebuilt)")

    runner = PipelineRunner()
    await runner.run(task)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Simple Dial-in Bot")
    parser.add_argument("-u", "--url", type=str, help="Room URL")
    parser.add_argument("-t", "--token", type=str, help="Room Token")
    parser.add_argument("-b", "--body", type=str, help="JSON configuration string")

    args = parser.parse_args()

    # Log the arguments for debugging
    logger.info(f"Room URL: {args.url}")
    logger.info(f"Token: {args.token}")
    logger.info(f"Body provided: {bool(args.body)}")

    asyncio.run(main(args.url, args.token, args.body))
