# SkyAI Project Context & Directives

## Core Identity
SkyAI is a live telemetry and AI-analysis overlay for Minecraft (Hypixel Skyblock). It consists of a Java/Fabric client mod and a Python/FastAPI backend. The AI engine powering the analysis is Gemini 2.5 Flash.

## Architecture & Data Flow
1. **The Client (Java/Fabric 1.21+):**
   - Scrapes the scoreboard for `purse` and tracks chat logs/menus.
   - **Polling:** Automatically pings the server (`GET /api/v1/player/{username}`) every 5 minutes (6000 ticks) to refresh the profile cache.
   - **Chat Push:** Sends a JSON payload containing ONLY `question` and `purse` to the server (`POST /api/v1/client_push`). *Positional data (x, y, z) and health have been deprecated to save tokens.*
   - **UI:** A scalable overlay with `[+]` and `[-]` buttons, utilizing `allowKeyPress` to intercept typing so Minecraft doesn't process keybinds while chatting.

2. **The Server (Python/FastAPI):**
   - **Data Fetching:** Calls Mojang and Hypixel APIs, decodes NBT inventory data (without third-party NBT libraries), and caches it locally to `omni_context.json`.
   - **AI Prompting:** Reads `omni_context.json` and the live `purse` value, injecting them into a system prompt before querying Gemini 2.5 Flash.

## Strict Coding Rules
- **Java:** - ALWAYS use `HttpClient.sendAsync` for network requests to prevent freezing the main Minecraft thread.
  - UI scaling must respect `MIN_WIDTH` and `MIN_HEIGHT` boundaries.
- **Python:** - ALWAYS use `asyncio.to_thread()` for blocking file I/O operations (like reading/writing `omni_context.json` or tailing `latest.log`).
  - Keep AI prompts clean: do not inject data the user didn't ask for unless it directly relates to the Omni-Analyst persona.
- **Formatting:** - AI responses must natively use Minecraft color codes (e.g., `§6` for coins, `§b` for stats, `§c` for warnings).