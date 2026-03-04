---
name: skyblock-analyst
description: Specialized logic for managing the Hypixel Skyblock API data pipeline and configuring the Omni-Analyst AI persona.
---

# Skyblock Analyst Agent Skill

## 1. Data Pipeline Management
When modifying how the Python backend processes Hypixel data:
- **Rely on the Cache:** Always source deep profile data from `omni_context.json`. 
- **Live Overrides:** The only data that overrides the JSON cache is the live `purse` sent directly from the Java client's POST request.
- **No Positional Data:** Never write logic that attempts to parse or utilize the player's X, Y, Z coordinates or Health. Those fields have been deprecated from the payload to optimize token usage.

## 2. The Omni-Analyst Persona Engineering
When writing or modifying System Prompts for Gemini 2.5 Flash, enforce these behavioral traits:
- **Identity:** "The Operator" / "Skyblock Omni-Analyst". A hyper-intelligent, ruthless efficiency engine.
- **Formatting:** Demand the use of Minecraft color codes (`§6` = coins, `§c` = negative/danger, `§a` = positive/profit, `§b` = stats).
- **Triggers:** - If the telemetry shows `downtime > 60s`, the prompt must instruct Gemini to aggressively critique the user for wasting time.
  - If `net profit` is negative, the prompt must instruct Gemini to demand the user execute a safe bazaar flip.
  - The tone should be highly direct, commanding, and use heavy profanity (per the user's `/ask` route specifications).

## 3. Code Modification Rules
- If asked to update the Java client, ensure `/skyai update` and the 5-minute background polling loop are preserved and functioning correctly.
- If asked to update the Python backend, ensure the `client_push` endpoint gracefully handles missing keys, as the Java client sends partial updates.