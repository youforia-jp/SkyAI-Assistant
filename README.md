# SkyAI — Hypixel Skyblock Omni-Operator

SkyAI is a real-time AI assistant embedded directly into Hypixel Skyblock, streaming live game telemetry and scoreboard data into a Gemini 2.5 Flash-powered backend that delivers personalized, context-aware advice through a draggable in-game overlay. It tracks multi-user sessions by UUID, caches profile snapshots, and re-analyzes your character automatically in the background — so the AI always knows your current state without you having to ask.

---

## System Architecture

```
┌─────────────────────────────────────────┐      HTTP/JSON (localhost:8000)
│   SkyAI-Mod-1.21.10  (Java / Fabric)   │ ──────────────────────────────────►  ┌───────────────────────────────────────────┐
│                                         │                                        │   SkyAI-Backend  (Python / FastAPI)       │
│  • Fabric 1.21.10 client mod            │ ◄──────────────────────────────────── │                                           │
│  • In-game HUD overlay (chat + input)   │      AI directive (JSON response)      │  • Uvicorn async server                   │
│  • Background telemetry ticker          │                                        │  • Per-UUID session + history tracking    │
│  • /skyai gui  /skyai update commands   │                                        │  • omni_context.json profile cache        │
│  • GLFW native keyboard capture         │                                        │  • Google Gemini 2.5 Flash via API        │
└─────────────────────────────────────────┘                                        └───────────────────────────────────────────┘
```

The Java client collects telemetry (purse from scoreboard, player UUID) and posts it to the local FastAPI server. The server enriches the prompt with a cached omni_context profile snapshot and calls Gemini, then streams the response back into the in-game chat overlay.

---

## Key Features

- **Multi-user UUID routing** — each player's session, chat history, and profile cache are fully isolated by UUID; no hardcoded usernames.
- **Live in-game HUD** — a scrollable, resizable, draggable AI chat overlay rendered directly on container screens with accurate GUI-scale scissor clipping.
- **Background profile refresh** — the mod polls `/api/v1/player/{username}` every 5 minutes and on `/skyai update`, keeping the AI's context fresh without interrupting gameplay.
- **Dynamic AI context injection** — every request carries the player's purse balance and full omni_context.json snapshot, so Gemini's answers are grounded in your actual character data.
- **Thread-safe async pipeline** — HTTP responses arrive on a worker thread and are safely marshalled back to Minecraft's game thread via `Minecraft.getInstance().execute(...)`.
- **Stable placeholder tracking** — AI "Thinking…" messages are tracked by object reference (not integer index) to survive concurrent list mutations.

---

## Installation & Setup

### Prerequisites

| Requirement | Version |
|---|---|
| Java | 21+ |
| Python | 3.11+ |
| Fabric Loader | 0.16+ |
| Fabric API | matching 1.21.10 |

---

### 1 · Python Backend

```bash
# Clone the repo
git clone https://github.com/YOUR_USERNAME/SkyAI.git
cd SkyAI/SkyAI-Backend

# Install dependencies
pip install -r requirements.txt

# Add your API keys (files are gitignored — never committed)
echo "YOUR_HYPIXEL_API_KEY" > api_key.txt
echo "YOUR_GEMINI_API_KEY"  > gemini_key.txt

# Start the server (stays running in the background)
uvicorn main:app --host 127.0.0.1 --port 8000 --reload
```

> **API Key files** — `api_key.txt` (Hypixel) and `gemini_key.txt` (Google AI Studio) must be placed in `SkyAI-Backend/`. They are listed in `.gitignore` and will never be committed.

---

### 2 · Fabric Mod

```bash
cd SkyAI/SkyAI-Mod-1.21.10

# Build the mod JAR
.\gradlew build          # Windows
./gradlew build          # macOS / Linux
```

The output JAR is written to `SkyAI-Mod-1.21.10/build/libs/SkyAI.jar`.

Copy `SkyAI.jar` into your Minecraft `mods/` folder alongside Fabric API.

---

### 3 · In-Game Commands

| Command | Effect |
|---|---|
| `/skyai gui` | Toggle window drag mode (reposition the HUD) |
| `/skyai update` | Manually trigger a fresh profile fetch + AI re-analysis |

---

## Project Structure

```
SkyAI/
├── SkyAI-Backend/
│   ├── main.py               # FastAPI app — routes, Gemini calls, session logic
│   ├── Omni.py               # Omni-analyst persona + context builder
│   ├── gemini_key.example.txt # Template (copy → gemini_key.txt and fill in)
│   └── requirements.txt
│
└── SkyAI-Mod-1.21.10/
    ├── src/client/java/name/modid/
    │   └── SkyAIClient.java  # Entire in-game HUD, input handling, HTTP client
    ├── build.gradle
    └── gradle.properties
```

---

## 🚧 Current Status & Known Issues

The backend FastAPI architecture and Gemini AI routing are fully operational. However, there is an active bug in the Fabric 1.21 UI client layer currently being patched. **Fabric Keyboard Listener Conflict:** The native `ScreenKeyboardEvents.allowKeyPress` is aggressively consuming raw keystrokes, which prevents the OS from passing character data down to the `allowCharTyped` event. Actively rewriting the input handler to safely bypass the Fabric UI event stack and capture native keyboard input.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Game Client | Java 21, Fabric Loader 0.16, Fabric API 1.21.10 |
| Rendering | Minecraft `GuiGraphics`, LWJGL 3 / GLFW |
| HTTP (client) | Java 11 `HttpClient` (async) |
| Backend Server | Python 3.11, FastAPI, Uvicorn |
| AI | Google Gemini 2.5 Flash (`google-generativeai`) |
| Data | JSON flat-file cache, per-UUID session isolation |

---

## License

MIT — see [LICENSE](LICENSE) for details.
