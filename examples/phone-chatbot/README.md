
# Pipecat Phone Challenge

This example demonstrates how to use Daily, OpenAI (OpenRouter), and Cartesia TTS in a simple dial-in scenario with **silence monitoring**. If the user is silent for a period of time, the bot will prompt them with a message using Cartesia TTS. After a number of repeated silences, the call is terminated automatically.

## Topic
The following features have been fully implemented:

- Silence tracking using timestamps of last audio activity
- Prompt delivery using Cartesia TTS
- LLM conversation powered by OpenRouter (GPT)
- Session logging: total duration, number of prompts, and silence events
- Testable via Daily Prebuilt for easy simulation

🐳 Dockerized for consistency and reproducibility

## Step-by-step instructions

1. Build the image:

   ```bash
   docker build -t pipecat-dialin .
   ```

2. Create a `test.env` file with your API keys. Example:

   ```
   DAILY_API_KEY=your_daily_api_key
   CARTESIA_API_KEY=your_cartesia_key
   OPEN_ROUTER_API_KEY=your_openrouter_key
   OPEN_ROUTER_BASE_URL=BASE_URL
   OPEN_ROUTER_MODEL=MODEL
   ```

3. Run the bot (in test mode):

   ```bash
   docker run --env-file .env -it pipecat-dialin \
     python simple_dialin.py \
     -u "https://yourdomain.daily.co/room" \
     -t "your_daily_token" \
     -b "{\"test_mode\": true, \"dialin_settings\": {\"call_id\": \"test-call\", \"call_domain\": \"yourdomain.daily.co\"}}"
   ```

---

## Main Issue Addressed in This Task

Cartesia TTS initialization was failing due to internal async setup (`TaskManager` not initialized) when calling `run_tts()` too early or without proper handling.

Additionally:
- `run_tts()` is an **async generator**, but we initially used `await` on it like a coroutine
- First audio outputs were causing `'NoneType' object has no attribute 'pts'` errors in downstream frame handlers

---

## How We Fixed It

1. **Correctly handled async generator** using:

   ```python
   async for frame in tts.run_tts("Are you still there?"):
       await task.queue_frames([frame])
   ```

2. **Wrapped TTS usage in try/except** to prevent crashes:

   ```python
   try:
       async for frame in tts.run_tts("Are you still there?"):
           await task.queue_frames([frame])
   except Exception as e:
       logger.error(f"TTS generation failed: {e}")
   ```

3. **Used an activity monitor** to track silence and reset timers on audio activity

4. **Tracked stats** like total duration, silence prompts, and user utterances

---

## Contact

For help or questions related to this project:

- **Lucas Kaewlaor** — GitHub: [@lucaskaewlaor](https://github.com/lucaskaewlaor)  
- For general support on Daily API or Pipecat framework, join the [Pipecat Discord](https://discord.gg/pipecat)
