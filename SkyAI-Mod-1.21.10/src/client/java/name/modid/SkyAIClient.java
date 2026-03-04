package name.modid;

import com.google.gson.Gson;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;

import net.fabricmc.api.ClientModInitializer;
import net.fabricmc.fabric.api.client.command.v2.ClientCommandManager;
import net.fabricmc.fabric.api.client.command.v2.ClientCommandRegistrationCallback;
import net.fabricmc.fabric.api.client.event.lifecycle.v1.ClientTickEvents;
import net.fabricmc.fabric.api.client.screen.v1.ScreenEvents;
import net.fabricmc.fabric.api.client.screen.v1.ScreenKeyboardEvents;
import net.fabricmc.fabric.api.client.screen.v1.ScreenMouseEvents;
import net.minecraft.client.Minecraft;
import net.minecraft.client.gui.GuiGraphics;
import net.minecraft.client.gui.screens.inventory.AbstractContainerScreen;

import com.mojang.blaze3d.platform.Window;
import org.lwjgl.glfw.GLFW;
import org.lwjgl.glfw.GLFWCharCallbackI;
import net.minecraft.network.chat.Component;
import net.minecraft.util.FormattedCharSequence;
import net.minecraft.world.scores.DisplaySlot;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.util.ArrayList;
import java.util.List;

public class SkyAIClient implements ClientModInitializer {

	// -----------------------------------------------------------------------
	// Chat history model
	// -----------------------------------------------------------------------
	private record ChatMessage(String sender, String text) {
	}

	/**
	 * Ordered list of all chat messages (user + AI).
	 * All structural modifications MUST occur on the game/render thread.
	 */
	private static final List<ChatMessage> chatHistory = new ArrayList<>();
	private static final int MAX_HISTORY = 60;

	// -----------------------------------------------------------------------
	// Input state
	// -----------------------------------------------------------------------
	private static final StringBuilder inputBuffer = new StringBuilder();
	private static final int MAX_INPUT_LEN = 120;
	/** True when the user has clicked the input box or the HUD is focused. */
	private static boolean inputFocused = false;
	/** Tick counter for blinking cursor animation (blinks every 10 ticks). */
	private static int cursorBlink = 0;

	// -----------------------------------------------------------------------
	// Telemetry Timer
	// -----------------------------------------------------------------------
	private static int telemetryTickCounter = 0;
	private static final int TICKS_UNTIL_UPDATE = 1200; // 60 seconds

	// Background profile-refresh poll — every 5 minutes (6000 ticks)
	private static int profilePollCounter = 0;
	private static final int PROFILE_POLL_TICKS = 6000;

	// -----------------------------------------------------------------------
	// UI Window
	// -----------------------------------------------------------------------
	private static int windowX = 5;
	private static int windowY = 5;
	private static int windowWidth = 220;
	private static int windowHeight = 290; // taller to fit chat + input

	// Scaling constraints
	private static final int MIN_WIDTH = 160;
	private static final int MIN_HEIGHT = 200;
	private static final int SCALE_STEP = 20;
	private static final int HEADER_HEIGHT = 15;
	private static final int INPUT_HEIGHT = 16; // height of the input row

	// Drag state
	private static boolean isDragging = false;
	private static int dragOffsetX = 0;
	private static int dragOffsetY = 0;

	// Config mode — drag enabled; toggled by /skyai gui
	private static boolean configMode = true;

	// Scroll state (for the chat history area)
	private static float scrollAmount = 0.0f;

	// Gson instance for safe JSON I/O
	private static final Gson GSON = new Gson();

	// -----------------------------------------------------------------------
	// Lifecycle
	// -----------------------------------------------------------------------
	@Override
	public void onInitializeClient() {
		SkyAI.LOGGER.info("SkyAI Client Initialized: Booting Chat Systems...");

		// --- /skyai gui + /skyai update ---
		ClientCommandRegistrationCallback.EVENT.register((dispatcher,
				registryAccess) -> dispatcher.register(ClientCommandManager.literal("skyai")
						.then(ClientCommandManager.literal("gui")
								.executes(ctx -> {
									configMode = !configMode;
									scrollAmount = 0.0f;
									String state = configMode ? "§aENABLED" : "§cDISABLED";
									ctx.getSource().sendFeedback(
											Component.literal("§e[SkyAI] §fDrag mode: " + state));
									return 1;
								}))
						.then(ClientCommandManager.literal("update")
								.executes(ctx -> {
									Minecraft mc = ctx.getSource().getClient();
									if (mc.player != null) {
										String username = mc.player.getName().getString();
										sendProfileUpdate(username);
										ctx.getSource().sendFeedback(
												Component
														.literal("§a[SkyAI] Requesting profile update from server..."));
									}
									return 1;
								}))));

		// --- Tick loop: background telemetry + cursor blink + profile refresh ---
		ClientTickEvents.END_CLIENT_TICK.register(client -> {
			if (client.player == null)
				return;

			// Cursor blink
			cursorBlink = (cursorBlink + 1) % 20;

			// Periodic telemetry log (every 60 s)
			telemetryTickCounter++;
			if (telemetryTickCounter >= TICKS_UNTIL_UPDATE) {
				telemetryTickCounter = 0;
				extractTelemetry(client);
			}

			// Periodic profile refresh — fire GET every 5 minutes (6000 ticks)
			profilePollCounter++;
			if (profilePollCounter >= PROFILE_POLL_TICKS) {
				profilePollCounter = 0;
				String username = client.player.getName().getString();
				sendProfileUpdate(username);
			}
		});

		// --- Screen events: render HUD, keyboard, mouse ---
		ScreenEvents.AFTER_INIT.register((client, screen, scaledWidth, scaledHeight) -> {
			if (!(screen instanceof AbstractContainerScreen))
				return;

			// ----------------------------------------------------------------
			// Keyboard — Fabric 0.138.4+1.21.10: (Screen, KeyEvent context)
			// ----------------------------------------------------------------
			ScreenKeyboardEvents.allowKeyPress(screen).register((scr, context) -> {
				if (!inputFocused)
					return true;
				int key = context.key();

				// ENTER (257) or NUMPAD ENTER (335)
				if (key == GLFW.GLFW_KEY_ENTER || key == GLFW.GLFW_KEY_KP_ENTER) {
					String question = inputBuffer.toString().trim();
					if (!question.isEmpty()) {
						inputBuffer.setLength(0);
						scrollAmount = 0.0f;
						sendChatQuestion(question, client);
					}
					return false;
				}

				// BACKSPACE
				if (key == GLFW.GLFW_KEY_BACKSPACE) {
					if (inputBuffer.length() > 0)
						inputBuffer.deleteCharAt(inputBuffer.length() - 1);
					return false;
				}

				// ESC — blur input, let Minecraft close the screen normally
				if (key == GLFW.GLFW_KEY_ESCAPE) {
					inputFocused = false;
					return true;
				}

				// All other keys (printable chars) are handled by allowCharTyped below.
				return true; // Pass unused keys through to the OS / game
			});

			// ----------------------------------------------------------------
			// Keyboard — Text Typing via GLFW char callback.
			// FIX 3: Forward to prevCharCb to avoid severing other mods' input chains.
			// ----------------------------------------------------------------
			long windowHandle = GLFW.glfwGetCurrentContext();
			// FIX 3: Use a holder array so the lambda can reference the previous callback
			// without violating Java's definite-assignment rules for local variables.
			GLFWCharCallbackI[] prevCbHolder = { null };
			prevCbHolder[0] = GLFW.glfwSetCharCallback(windowHandle, (win, codepoint) -> {
				if (inputFocused && inputBuffer.length() < MAX_INPUT_LEN) {
					inputBuffer.appendCodePoint(codepoint);
				}
				// Forward to the previously registered callback so the input chain is intact
				if (prevCbHolder[0] != null) {
					prevCbHolder[0].invoke(win, codepoint);
				}
			});
			// Restore the previous callback when the screen closes
			ScreenEvents.remove(screen).register(scr -> GLFW.glfwSetCharCallback(windowHandle, prevCbHolder[0]));

			// Render the HUD overlay
			ScreenEvents.afterRender(screen).register(
					(scr, guiGraphics, mouseX, mouseY, tickDelta) -> renderAiWindow(guiGraphics));

			// ----------------------------------------------------------------
			// Mouse click — Fabric 0.138.4+1.21.10: (Screen, MouseButtonEvent)
			// ----------------------------------------------------------------
			ScreenMouseEvents.allowMouseClick(screen).register((scr, click) -> {
				if (click.button() == 0) {
					int mx = (int) click.x();
					int my = (int) click.y();

					// [+] / [-] scale buttons in the header
					if (my >= windowY && my <= windowY + HEADER_HEIGHT) {
						int plusX = windowX + windowWidth - 14;
						int minusX = windowX + windowWidth - 28;
						if (mx >= plusX && mx <= windowX + windowWidth) {
							windowWidth += SCALE_STEP;
							windowHeight += SCALE_STEP;
							return false;
						} else if (mx >= minusX && mx < plusX) {
							windowWidth = Math.max(MIN_WIDTH, windowWidth - SCALE_STEP);
							windowHeight = Math.max(MIN_HEIGHT, windowHeight - SCALE_STEP);
							return false;
						}
					}

					// Start drag on header in config mode
					if (configMode && mx >= windowX && mx <= windowX + windowWidth
							&& my >= windowY && my <= windowY + HEADER_HEIGHT) {
						isDragging = true;
						dragOffsetX = mx - windowX;
						dragOffsetY = my - windowY;
						return false;
					}

					// Click inside input row → focus and return true so REI drops its focus
					int inputRowY = windowY + windowHeight - INPUT_HEIGHT;
					if (mx >= windowX && mx <= windowX + windowWidth
							&& my >= inputRowY && my <= windowY + windowHeight) {
						inputFocused = true;
						return true; // CRITICAL: returns true so REI sees the click & drops focus
					}

					// Click inside chat body → absorb but defocus input
					if (mx >= windowX && mx <= windowX + windowWidth
							&& my >= windowY && my <= windowY + windowHeight) {
						inputFocused = false;
						return false;
					}
				}
				// Click completely outside → defocus and pass through to REI etc.
				inputFocused = false;
				return true;
			}); // end allowMouseClick

			// Mouse release — Fabric 0.138.4+1.21.10: (Screen, MouseButtonEvent)
			ScreenMouseEvents.allowMouseRelease(screen).register((scr, click) -> {
				if (click.button() == 0)
					isDragging = false;
				return true;
			});

			// Mouse drag — Fabric 0.138.4+1.21.10: (Screen, MouseButtonEvent, double,
			// double)
			ScreenMouseEvents.allowMouseDrag(screen).register((scr, click, deltaX, deltaY) -> {
				if (isDragging && click.button() == 0) {
					windowX = (int) click.x() - dragOffsetX;
					windowY = (int) click.y() - dragOffsetY;
				}
				return true;
			});

			// Scroll — scroll chat history when cursor is inside window
			ScreenMouseEvents.allowMouseScroll(screen).register((scr, mouseX, mouseY, horizAmount, vertAmount) -> {
				int mx = (int) mouseX;
				int my = (int) mouseY;
				if (mx >= windowX && mx <= windowX + windowWidth
						&& my >= windowY && my <= windowY + windowHeight) {
					scrollAmount -= (float) vertAmount * 10.0f;
					if (scrollAmount < 0.0f)
						scrollAmount = 0.0f;
					return false;
				}
				return true;
			});
		});
	}

	// -----------------------------------------------------------------------
	// Chat send helpers
	// -----------------------------------------------------------------------

	/**
	 * Append a "You: ..." entry, create a stable placeholder ChatMessage object,
	 * and fire the HTTP request.
	 *
	 * FIX 2: We store the placeholder as an object reference and use indexOf()
	 * to find it later, preventing stale-index corruption when messages shift.
	 */
	private static void sendChatQuestion(String question, Minecraft client) {
		addMessage("§e[You]", question);

		// FIX 2: track the placeholder by object identity, not by index
		ChatMessage placeholder = new ChatMessage("§b[SkyAI]", "§7Thinking...");
		addMessage(placeholder);

		String uuid = client.player.getUUID().toString().replace("-", "");
		Long purse = scrapePurseFromScoreboard(client);
		long purseVal = (purse != null) ? purse : 0L;

		// FIX 5: Build JSON safely with Gson — no manual escaping needed
		JsonObject payload = new JsonObject();
		payload.addProperty("uuid", uuid);
		payload.addProperty("question", question);
		payload.addProperty("purse", purseVal);

		SkyAI.LOGGER.info("[SkyAI] Chat question: {}", question);
		sendRawToPython(GSON.toJson(payload), placeholder);
	}

	/** Append a plain text message by sender + text strings. */
	private static void addMessage(String sender, String text) {
		if (chatHistory.size() >= MAX_HISTORY)
			chatHistory.remove(0);
		chatHistory.add(new ChatMessage(sender, text));
	}

	/**
	 * Append an already-constructed ChatMessage object.
	 * FIX 2: used to insert placeholders we can track by reference.
	 */
	private static void addMessage(ChatMessage msg) {
		if (chatHistory.size() >= MAX_HISTORY)
			chatHistory.remove(0);
		chatHistory.add(msg);
	}

	/** Called on menu open — uses ANALYZE_CONTEXT auto-trigger. */
	private static void sendClientPush(Minecraft client) {
		if (client.player == null)
			return;

		String uuid = client.player.getUUID().toString().replace("-", "");
		Long purse = scrapePurseFromScoreboard(client);
		long purseVal = (purse != null) ? purse : 0L;

		// FIX 5: Gson payload
		JsonObject payload = new JsonObject();
		payload.addProperty("uuid", uuid);
		payload.addProperty("question", "ANALYZE_CONTEXT");
		payload.addProperty("purse", purseVal);

		// FIX 2: placeholder by object reference
		ChatMessage placeholder = new ChatMessage("§b[SkyAI]", "§7Analyzing...");
		addMessage(placeholder);

		SkyAI.LOGGER.info("[SkyAI] Sending auto-context push: {}", GSON.toJson(payload));
		sendRawToPython(GSON.toJson(payload), placeholder);
	}

	/**
	 * Fire an async GET to /api/v1/player/{username} to refresh omni_context.json.
	 * FIX 1: all chatHistory mutations inside callbacks are wrapped in
	 * mc.execute().
	 * FIX 2: placeholder tracked by object reference.
	 */
	private static void sendProfileUpdate(String username) {
		ChatMessage placeholder = new ChatMessage("§b[SkyAI]",
				"§7Fetching fresh profile data for " + username + "...");
		addMessage(placeholder);

		HttpClient httpClient = HttpClient.newHttpClient();
		HttpRequest request = HttpRequest.newBuilder()
				.uri(URI.create("http://127.0.0.1:8000/api/v1/player/" + username))
				.GET()
				.build();
		httpClient.sendAsync(request, HttpResponse.BodyHandlers.ofString())
				.thenAccept(response -> {
					if (response.statusCode() == 200) {
						SkyAI.LOGGER.info("[SkyAI] Profile update success for '{}'.", username);
						// FIX 1: mutate chatHistory on the game thread
						Minecraft.getInstance().execute(() -> {
							int idx = chatHistory.indexOf(placeholder);
							if (idx >= 0) {
								chatHistory.set(idx, new ChatMessage("§a[SkyAI]",
										"Profile refreshed! Re-analyzing..."));
							}
							// Trigger a new AI analysis with the fresh omni_context.json
							Minecraft mc = Minecraft.getInstance();
							if (mc.player != null) {
								sendClientPush(mc);
								mc.player.displayClientMessage(
										Component.literal("§a[SkyAI] Profile updated — re-analyzing!"), false);
							}
						});
					} else {
						SkyAI.LOGGER.warn("[SkyAI] Profile update HTTP {}: {}",
								response.statusCode(), response.body());
						// FIX 1: mutate chatHistory on the game thread
						Minecraft.getInstance().execute(() -> {
							int idx = chatHistory.indexOf(placeholder);
							if (idx >= 0) {
								chatHistory.set(idx, new ChatMessage("§c[SkyAI]",
										"Profile update failed (HTTP " + response.statusCode() + ")."));
							}
						});
					}
				})
				.exceptionally(ex -> {
					SkyAI.LOGGER.error("[SkyAI] Profile update failed: {}", ex.getMessage());
					// FIX 1: mutate chatHistory on the game thread
					Minecraft.getInstance().execute(() -> {
						int idx = chatHistory.indexOf(placeholder);
						if (idx >= 0) {
							chatHistory.set(idx, new ChatMessage("§c[SkyAI]",
									"Update failed: is main.py running?"));
						}
					});
					return null;
				});
	}

	// -----------------------------------------------------------------------
	// HTTP sender
	// FIX 1: All chatHistory mutations are inside mc.execute() blocks.
	// FIX 2: Placeholder tracked by object reference via indexOf().
	// FIX 5: Response parsed with Gson instead of manual extractStringField.
	// -----------------------------------------------------------------------
	private static void sendRawToPython(String requestBody, ChatMessage placeholder) {
		HttpClient httpClient = HttpClient.newHttpClient();
		HttpRequest request = HttpRequest.newBuilder()
				.uri(URI.create("http://127.0.0.1:8000/api/v1/client_push"))
				.header("Content-Type", "application/json")
				.POST(HttpRequest.BodyPublishers.ofString(requestBody))
				.build();

		httpClient.sendAsync(request, HttpResponse.BodyHandlers.ofString())
				.thenAccept(response -> {
					// FIX 5: Gson parsing — safe against malformed or escaped JSON
					String aiText = null;
					try {
						JsonObject json = JsonParser.parseString(response.body()).getAsJsonObject();
						if (json.has("directive") && !json.get("directive").isJsonNull()) {
							aiText = json.get("directive").getAsString();
						}
					} catch (Exception parseEx) {
						SkyAI.LOGGER.error("[SkyAI] Failed to parse response JSON: {}", parseEx.getMessage());
					}

					final String finalAiText = aiText;
					// FIX 1: mutate chatHistory on the game thread
					Minecraft.getInstance().execute(() -> {
						if (finalAiText != null) {
							// FIX 2: find placeholder by reference, not by stale index
							int i = chatHistory.indexOf(placeholder);
							if (i >= 0) {
								chatHistory.set(i, new ChatMessage("§b[SkyAI]", finalAiText));
							}
							Minecraft mc = Minecraft.getInstance();
							if (mc.player != null) {
								mc.player.displayClientMessage(
										Component.literal("§b[SkyAI] Response ready — scroll to read."), false);
							}
						}
					});
				})
				.exceptionally(ex -> {
					SkyAI.LOGGER.error("HTTP Error: {}", ex.getMessage());
					// FIX 1: mutate chatHistory on the game thread
					Minecraft.getInstance().execute(() -> {
						int i = chatHistory.indexOf(placeholder);
						if (i >= 0) {
							chatHistory.set(i,
									new ChatMessage("§c[SkyAI]", "Error: Server unreachable. Is main.py running?"));
						}
					});
					return null;
				});
	}

	// -----------------------------------------------------------------------
	// HUD Renderer
	// FIX 4: enableScissor uses physical pixel coordinates (logical * guiScale).
	// -----------------------------------------------------------------------
	private static void renderAiWindow(GuiGraphics g) {
		Minecraft mc = Minecraft.getInstance();
		if (mc.screen == null || mc.player == null || mc.level == null)
			return;

		try {
			int chatAreaHeight = windowHeight - HEADER_HEIGHT - 4 - INPUT_HEIGHT - 2;
			int inputRowY = windowY + windowHeight - INPUT_HEIGHT;

			// --- Background ---
			g.fill(windowX, windowY, windowX + windowWidth, windowY + windowHeight, 0x90000000);

			// --- Header ---
			g.fill(windowX, windowY, windowX + windowWidth, windowY + HEADER_HEIGHT, 0xFF1A1A3A);
			String header = configMode ? "§eSkyAI Chat §7[DRAG]" : "§bSkyAI Chat";
			g.drawString(mc.font, header, windowX + 5, windowY + 4, 0xFFFFFF, false);
			// [+] / [-] scale buttons — right-aligned in the header
			g.fill(windowX + windowWidth - 28, windowY + 1, windowX + windowWidth - 15, windowY + HEADER_HEIGHT - 1,
					0xFF2A2A5A);
			g.fill(windowX + windowWidth - 14, windowY + 1, windowX + windowWidth - 1, windowY + HEADER_HEIGHT - 1,
					0xFF2A2A5A);
			g.drawString(mc.font, "-", windowX + windowWidth - 24, windowY + 4, 0xFFCCCCCC, false);
			g.drawString(mc.font, "+", windowX + windowWidth - 10, windowY + 4, 0xFFCCCCCC, false);

			// --- Build all rendered lines from chat history ---
			List<FormattedCharSequence> allLines = new ArrayList<>();
			for (ChatMessage msg : chatHistory) {
				// Render sender label then the body text, wrapped to window width
				String combined = msg.sender() + " §f" + msg.text();
				List<FormattedCharSequence> wrapped = mc.font.split(
						Component.literal(combined), windowWidth - 12);
				allLines.addAll(wrapped);
				// Small gap between messages
				allLines.add(FormattedCharSequence.EMPTY);
			}

			// Remove trailing empty line
			if (!allLines.isEmpty() && allLines.get(allLines.size() - 1) == FormattedCharSequence.EMPTY) {
				allLines.remove(allLines.size() - 1);
			}

			int lineH = mc.font.lineHeight + 1;
			int totalTextH = allLines.size() * lineH;
			int chatTop = windowY + HEADER_HEIGHT + 2;

			// Clamp scroll so it auto-shows latest messages (scroll to bottom)
			float maxScroll = Math.max(0, totalTextH - chatAreaHeight);
			if (scrollAmount > maxScroll)
				scrollAmount = maxScroll;

			// FIX 4: Convert logical GUI coordinates to physical framebuffer pixels
			// so the scissor box is correctly positioned at any GUI scale setting.
			Window window = mc.getWindow();
			double scale = window.getGuiScale();
			g.enableScissor(
					(int) Math.round(windowX * scale),
					(int) Math.round(chatTop * scale),
					(int) Math.round((windowX + windowWidth) * scale),
					(int) Math.round((chatTop + chatAreaHeight) * scale));

			int lineY = chatTop - (int) scrollAmount;
			for (FormattedCharSequence line : allLines) {
				g.drawString(mc.font, line, windowX + 5, lineY, 0xE0E0E0, false);
				lineY += lineH;
			}
			g.disableScissor();

			// Scroll bar
			if (maxScroll > 0) {
				float scrollRatio = scrollAmount / maxScroll;
				int barH = Math.max(10, (int) ((float) chatAreaHeight / totalTextH * chatAreaHeight));
				int barY = chatTop + (int) (scrollRatio * (chatAreaHeight - barH));
				g.fill(windowX + windowWidth - 3, barY,
						windowX + windowWidth - 1, barY + barH, 0xAA5577FF);
			}

			// --- Separator line ---
			g.fill(windowX, inputRowY - 2, windowX + windowWidth, inputRowY - 1, 0xFF334466);

			// --- Input box ---
			int inputBg = inputFocused ? 0xFF1C2C44 : 0xFF111111;
			g.fill(windowX, inputRowY, windowX + windowWidth, windowY + windowHeight, inputBg);

			String displayText = inputBuffer.toString();
			String cursor = (inputFocused && cursorBlink < 10) ? "|" : "";
			String prefix = inputFocused ? "§7> §f" : "§8> §7";
			g.drawString(mc.font, Component.literal(prefix + displayText + "§e" + cursor),
					windowX + 4, inputRowY + 4, 0xFFFFFF, false);

		} catch (Exception e) {
			SkyAI.LOGGER.error("SkyAI Render Error: ", e);
		}
	}

	// -----------------------------------------------------------------------
	// Telemetry Extraction (background log — preserved)
	// -----------------------------------------------------------------------
	private static void extractTelemetry(Minecraft client) {
		if (client.player == null)
			return;
		double x = Math.round(client.player.getX() * 10.0) / 10.0;
		double y = Math.round(client.player.getY() * 10.0) / 10.0;
		double z = Math.round(client.player.getZ() * 10.0) / 10.0;
		float health = client.player.getHealth();
		Long purse = scrapePurseFromScoreboard(client);
		SkyAI.LOGGER.info("📊 TELEMETRY | HP: {} | POS: ({}, {}, {}) | Purse: {}",
				health, x, y, z, (purse != null ? purse : "N/A"));
	}

	// -----------------------------------------------------------------------
	// Scoreboard Purse Scraper
	// FIX 6: Null-safe checks on scores list and each entry's owner() value.
	// -----------------------------------------------------------------------
	private static Long scrapePurseFromScoreboard(Minecraft mc) {
		if (mc.level == null || mc.level.getScoreboard() == null)
			return null;
		var objective = mc.level.getScoreboard().getDisplayObjective(DisplaySlot.SIDEBAR);
		if (objective == null)
			return null;

		// FIX 6: listPlayerScores may return null — guard before iterating
		var scores = mc.level.getScoreboard().listPlayerScores(objective);
		if (scores == null)
			return null;

		for (var scoreEntry : scores) {
			// FIX 6: individual entries may have a null owner
			String owner = scoreEntry.owner();
			if (owner == null)
				continue;
			if (owner.contains("Purse:") || owner.contains("Piggy:")) {
				String numericPart = owner.replaceAll("[^0-9]", "");
				try {
					return Long.parseLong(numericPart);
				} catch (NumberFormatException e) {
					return null;
				}
			}
		}
		return null;
	}
}
