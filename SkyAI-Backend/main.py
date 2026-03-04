"""
Skyblock Omni Operator — FastAPI Backend
Migrated from Omni.py prototype.

Runs on: uvicorn main:app --host 0.0.0.0 --port 8000
"""

import re
import io
import gzip
import json
import base64
import struct


import asyncio
import logging
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel



# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("omni")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
HYPIXEL_PROFILE_URL = "https://api.hypixel.net/v2/skyblock/profiles"
HYPIXEL_MUSEUM_URL  = "https://api.hypixel.net/v2/skyblock/museum"
MOJANG_API_URL      = "https://api.mojang.com/users/profiles/minecraft"
CACHE_DIR           = Path("data/sessions")   # Latest-state cache files: cache_{uuid}.json
HISTORY_DIR         = Path("data/history")    # Historical snapshots:   cache_{uuid}_{ts}.json
API_KEY_FILE        = "api_key.txt"
LOG_CONFIG_FILE     = "omni_config.json"

# HOTM level cannot be read directly from the v2 API endpoint used here;
# keep the manual override so callers can pass it in a future query param.
MANUAL_HOTM_LEVEL = 10

GEMINI_KEY_FILE = "gemini_key.txt"
GEMINI_URL      = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"

# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(_: FastAPI):
    """Create required directories, then launch the log-tail background task."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    log.info("Directories ensured: %s, %s", CACHE_DIR, HISTORY_DIR)
    asyncio.create_task(_tail_log_async())
    log.info("Log-tail background task scheduled.")
    yield


app = FastAPI(
    title="Skyblock Omni Operator",
    description="Async FastAPI backend for Hypixel Skyblock stat tracking.",
    version="10.4.0",
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# CORS Middleware
# ---------------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    # Tighten this list to your actual frontend origin(s) before going to prod.
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ===========================================================================
# Utility helpers
# ===========================================================================

def _load_api_key(filename: str = API_KEY_FILE) -> str | None:
    """Read the Hypixel API key from a plain-text file."""
    try:
        return Path(filename).read_text().strip() or None
    except OSError:
        return None


def _clean_name(name_str: str | None) -> str:
    """Strip Minecraft § colour-code sequences from an item name."""
    if not name_str:
        return "Unknown"
    return re.sub(r"§.", "", name_str)


# ===========================================================================
# Mojang API  — username → UUID resolution
# ===========================================================================

async def resolve_username_to_uuid(username: str) -> tuple[str, str]:
    """
    Resolve a Minecraft *username* to its canonical UUID via the Mojang API.

    Returns
    -------
    tuple[str, str]
        A ``(uuid, canonical_name)`` pair where *uuid* is the raw hex UUID
        (no hyphens) exactly as Mojang returns it, and *canonical_name* is
        the correctly-cased username Mojang has on record.

    Raises
    ------
    HTTPException(404)
        The username does not exist or has never been registered.
    HTTPException(502)
        The Mojang API returned an unexpected status code.

    Notes
    -----
    - Mojang returns **204 No Content** when the username is not found
      (not a 404), so we treat any non-200 response as "not found" unless
      the status is clearly a server-side fault (5xx).
    - The UUID returned here has **no hyphens** — this matches what Hypixel
      expects and what we use as the cache key throughout the app.
    """
    log.info("Resolving Mojang username: %s", username)

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{MOJANG_API_URL}/{username}")

    if resp.status_code == 200:
        data = resp.json()
        raw_uuid      = data.get("id", "")   # hex string, no hyphens
        canonical_name = data.get("name", username)
        log.info("Resolved %s → UUID %s", canonical_name, raw_uuid)
        return raw_uuid, canonical_name

    if resp.status_code in (204, 404):
        # 204 = Mojang's way of saying "username not found"
        log.warning("Mojang: username '%s' not found (HTTP %s).", username, resp.status_code)
        raise HTTPException(
            status_code=404,
            detail=f"Minecraft username '{username}' does not exist.",
        )

    if resp.status_code >= 500:
        log.error("Mojang API server error: HTTP %s", resp.status_code)
        raise HTTPException(
            status_code=502,
            detail=f"Mojang API returned a server error ({resp.status_code}). Try again later.",
        )

    # Catch-all for unexpected status codes (429 rate-limit, etc.)
    log.error("Unexpected Mojang API response: HTTP %s", resp.status_code)
    raise HTTPException(
        status_code=502,
        detail=f"Unexpected response from Mojang API (HTTP {resp.status_code}).",
    )


# ===========================================================================
# NBT decoding  (sync — CPU-bound, wrapped in a thread executor when called)
# ===========================================================================

# ---------------------------------------------------------------------------
# Byte-level NBT name extraction — no nbt library required.
#
# Hypixel serialises inventory blobs via Java's NBT + DataOutputStream.
# Strings use Java's Modified UTF-8, which allows unpaired surrogates
# (e.g. 0xED 0xA0 …).  Those bytes are legal in Java but cause Python's
# strict UTF-8 decoder to raise — which is why every nbt-library approach
# fails, even with monkey-patches, because the parser crashes on tag *names*
# before it ever reaches tag values.
#
# Solution: treat the entire decompressed blob as a raw byte array and scan
# for the hard-coded binary signature that the NBT spec mandates for every
# TAG_String named "Name" inside a display compound:
#
#   Offset  Size  Meaning
#   ------  ----  -------
#   0       1     Tag type = 0x08  (TAG_String)
#   1       2     Key length = 0x00 0x04  (big-endian unsigned short → 4)
#   3       4     Key bytes  = b'Name'
#   7       2     Value length (big-endian unsigned short)   ← struct reads here
#   9       N     Value bytes (Modified UTF-8, decoded with errors='replace')
#
# bytes.find() locates the 8-byte header in O(n) time without any decoding,
# then struct.unpack reads the exact 2-byte length so we slice precisely N
# bytes of value.  errors='replace' converts every illegal byte sequence to
# U+FFFD instead of raising, making this permanently crash-proof.
# ---------------------------------------------------------------------------

# The exact 8-byte binary signature that precedes every display Name value.
# Breakdown:
#   \x08       — NBT tag type: TAG_String (8)
#   \x00\x04   — 2-byte BE length of the key string: 4 characters
#   Name       — the literal key bytes
_NBT_NAME_HEADER: bytes = b'\x08\x00\x04Name'
_NBT_HEADER_LEN:  int   = len(_NBT_NAME_HEADER)   # 8 — skip past the header
_NBT_LEN_SIZE:    int   = 2                        # 2-byte BE unsigned short


def _decode_inventory_sync(raw_data: str, context_name: str = "Unknown") -> list[str]:
    """
    Decode a Base64 + GZip-compressed NBT inventory blob into a list of
    cleaned item-name strings.

    No third-party library is used.  The pipeline is:
      1. Base64-decode the input string.
      2. Gzip-decompress the binary payload.
      3. Scan the raw bytes for every occurrence of the hard-coded 8-byte
         TAG_String "Name" header (``b'\x08\x00\x04Name'``).
      4. At each hit, read the next 2 bytes with ``struct.unpack(">H")``
         to get the exact value length, then slice that many bytes.
      5. Decode with ``errors="replace"`` — Java Modified-UTF-8 / unpaired
         surrogates become U+FFFD instead of raising.
      6. Strip Minecraft § colour codes via ``_clean_name``.

    This function is intentionally *synchronous* because Base64 decode,
    gzip decompression, and the byte scan are all CPU-bound.  Call it via
    ``asyncio.to_thread`` to avoid blocking the event loop.
    """
    if not raw_data:
        return []

    # ------------------------------------------------------------------ #
    # Step 1 — Base64 decode                                              #
    # ------------------------------------------------------------------ #
    try:
        compressed = base64.b64decode(raw_data)
    except Exception as exc:
        log.warning("[%s] Base64 decode failed: %s", context_name, exc)
        return ["Parse Error"]

    # ------------------------------------------------------------------ #
    # Step 2 — Gzip decompress                                            #
    #                                                                     #
    # Hypixel always gzip-wraps the payload, but we fall back to raw      #
    # bytes in case a future API version changes that.                    #
    # ------------------------------------------------------------------ #
    try:
        blob: bytes = gzip.decompress(compressed)
    except Exception:
        blob = compressed   # Not gzip — attempt raw parse.

    # ------------------------------------------------------------------ #
    # Steps 3-6 — Binary header scan → struct length → slice → decode     #
    # ------------------------------------------------------------------ #
    items: list[str] = []
    try:
        search_start: int = 0
        blob_len:     int = len(blob)

        while True:
            # Locate the next TAG_String "Name" header in the blob.
            hit: int = blob.find(_NBT_NAME_HEADER, search_start)
            if hit == -1:
                break   # No more Name tags — we are done.

            # The 2-byte value-length field starts immediately after the
            # 8-byte header.  Guard against a truncated blob.
            len_start: int = hit + _NBT_HEADER_LEN
            len_end:   int = len_start + _NBT_LEN_SIZE
            if len_end > blob_len:
                break   # Blob is truncated; nothing more to read.

            # Unpack the big-endian unsigned short that gives the exact
            # number of bytes in the value string.
            (str_len,) = struct.unpack(">H", blob[len_start:len_end])

            # Slice exactly str_len bytes of raw Modified-UTF-8 value.
            val_start: int = len_end
            val_end:   int = val_start + str_len
            if val_end > blob_len:
                # Truncated value — skip and keep searching.
                search_start = len_end
                continue

            raw_bytes: bytes = blob[val_start:val_end]

            # Decode with errors="replace" so 0xED unpaired surrogates and
            # any other illegal byte sequences become U+FFFD, never raising.
            decoded: str = raw_bytes.decode("utf-8", errors="replace")
            cleaned: str = _clean_name(decoded)

            if cleaned and cleaned != "Unknown":
                items.append(cleaned)

            # Advance past the value we just consumed.  This prevents the
            # next find() from re-matching bytes inside the current value
            # (e.g. if the item name itself contained the pattern).
            search_start = val_end

    except Exception as exc:
        log.warning("[%s] Binary NBT scan failed: %s", context_name, exc)
        return ["Parse Error"]

    return items


async def decode_inventory(raw_data: str, context_name: str = "Unknown") -> list[str]:
    """Async wrapper — runs the blocking NBT decode in a thread pool."""
    return await asyncio.to_thread(_decode_inventory_sync, raw_data, context_name)


# ===========================================================================
# JSON cache  (async I/O)
# ===========================================================================

def _cache_path(uuid: str) -> Path:
    """Return the cache file path for a given UUID."""
    return CACHE_DIR / f"cache_{uuid}.json"


async def save_cache(payload: dict[str, Any], uuid: str) -> None:
    """
    Persist *payload* to ``cache_{uuid}.json`` asynchronously.

    Also writes a timestamped snapshot to ``HISTORY_DIR/cache_{uuid}_{ts}.json``
    so the full change history is preserved for delta tracking.

    Both file writes are dispatched to a thread pool to keep the event loop free.
    """
    cache_file = _cache_path(uuid)
    timestamp  = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    history_file = HISTORY_DIR / f"cache_{uuid}_{timestamp}.json"
    serialised = json.dumps(payload, indent=4, default=str)

    def _write() -> None:
        cache_file.write_text(serialised)
        history_file.write_text(serialised)

    await asyncio.to_thread(_write)
    log.info("Cache written → %s", cache_file)
    log.info("History snapshot → %s", history_file)


async def load_cache(uuid: str) -> dict[str, Any] | None:
    """Return the last cached payload for *uuid*, or *None* if the file doesn't exist."""
    cache_file = _cache_path(uuid)

    def _read() -> dict[str, Any] | None:
        if not cache_file.exists():
            return None
        return json.loads(cache_file.read_text())

    return await asyncio.to_thread(_read)


# ===========================================================================
# Global session state  — mutated in-place by the log-tail background task
# and the client-push route.  All dicts share state_lock so the async
# get_player route always reads a consistent snapshot.
# ===========================================================================

# Single lock that protects every module-level mutable dict below.
state_lock = threading.Lock()

# Per-UUID session state.  Keys are raw UUIDs (no hyphens).
# Each value is a dict with sub-keys: live_client_data, financial_metrics,
# efficiency_metrics, rng_metrics.
user_state: dict[str, dict[str, Any]] = {}


def _get_user_state(uuid: str) -> dict[str, Any]:
    """
    Return (creating if absent) the mutable state bucket for *uuid*.

    Must be called while holding *state_lock* whenever the returned dict
    is being mutated.  Read-only callers may snapshot without the lock.
    """
    if uuid not in user_state:
        user_state[uuid] = {
            "live_client_data": {},
            "financial_metrics": {
                "net_session_cashflow":   0,
                "loot_liquidation_sales": 0,
                "dungeon_chest_spend":    0,
                "items_liquidated":       0,
                "largest_single_sale":    0,
            },
            "efficiency_metrics": {
                "current_run_start_time": 0.0,
                "total_run_time_seconds": 0,
                "run_time_avg_seconds":   0,
                "runs_completed":         0,
                "last_run_end":           0.0,
                "total_downtime":         0,
                "downtime_avg_seconds":   0,
            },
            "rng_metrics": {
                "last_rare_drop":        "None this session",
                "dry_streak_count":      0,
                "last_drop_timestamp":   None,
                "time_since_last_drop":  "0s",
            },
        }
    return user_state[uuid]


# ---------------------------------------------------------------------------
# Log-tail helpers
# ---------------------------------------------------------------------------

def _load_log_path() -> str | None:
    """Read the Minecraft latest.log path from ``omni_config.json``."""
    try:
        data = json.loads(Path(LOG_CONFIG_FILE).read_text())
        return data.get("log_path") or None
    except (OSError, json.JSONDecodeError):
        return None


# Compiled once at import time — avoids re-compiling on every log line.
_RE_SOLD   = re.compile(r"Sold (?:\d+x )?(.+?) for ([\d,.]+) coins")
_RE_BOUGHT = re.compile(r"Bought (?:\d+x )?(.+?) for ([\d,.]+) coins")
_DUNGEON_START_TRIGGERS = ("Dungeon Starting!", "The dungeon has begun!", "Starting in 1 second")
_DUNGEON_END_TRIGGERS   = ("Dungeon Ended", "Team Score:")


def _process_log_line(line: str, uuid: str) -> None:
    """
    Parse a single stripped log line and mutate the per-UUID session dicts.

    *uuid* is the player whose log file is being tailed.  All mutations are
    isolated to that player's state bucket so two concurrent players never
    interfere with each other.

    This function is called from the async log-tail loop and must stay
    synchronous — it does no I/O and returns in microseconds.
    """
    # ------------------------------------------------------------------ #
    # Financial — Sold                                                    #
    # ------------------------------------------------------------------ #
    sold_match = _RE_SOLD.search(line)
    if sold_match:
        amt = float(sold_match.group(2).replace(",", ""))
        with state_lock:
            fm = _get_user_state(uuid)["financial_metrics"]
            fm["loot_liquidation_sales"] += amt
            fm["net_session_cashflow"]   += amt
            fm["items_liquidated"]       += 1
            if amt > fm["largest_single_sale"]:
                fm["largest_single_sale"] = amt
        log.info("[Log][%s] Sale detected: +%s coins", uuid, amt)
        return

    # ------------------------------------------------------------------ #
    # Financial — Bought                                                  #
    # ------------------------------------------------------------------ #
    bought_match = _RE_BOUGHT.search(line)
    if bought_match:
        amt = float(bought_match.group(2).replace(",", ""))
        with state_lock:
            fm = _get_user_state(uuid)["financial_metrics"]
            fm["dungeon_chest_spend"]  += amt
            fm["net_session_cashflow"] -= amt
        log.info("[Log][%s] Purchase detected: -%s coins", uuid, amt)
        return

    # ------------------------------------------------------------------ #
    # Efficiency — Dungeon start                                          #
    # ------------------------------------------------------------------ #
    if any(t in line for t in _DUNGEON_START_TRIGGERS):
        now = time.time()
        with state_lock:
            em = _get_user_state(uuid)["efficiency_metrics"]
            last_end = em["last_run_end"]
            if last_end > 0:
                downtime = now - last_end
                # Only count gaps under 5 min as genuine between-run downtime.
                if downtime < 300:
                    em["total_downtime"] += downtime
                    runs = em["runs_completed"]
                    if runs > 0:
                        em["downtime_avg_seconds"] = int(
                            em["total_downtime"] / runs
                        )
            em["current_run_start_time"] = now
        log.info("[Log][%s] Dungeon started.", uuid)
        return

    # ------------------------------------------------------------------ #
    # Efficiency — Dungeon end                                            #
    # ------------------------------------------------------------------ #
    if any(t in line for t in _DUNGEON_END_TRIGGERS):
        with state_lock:
            em = _get_user_state(uuid)["efficiency_metrics"]
        start = em["current_run_start_time"]
        if start > 0:
            run_time = time.time() - start
            # Sanity-check: ignore sub-30s noise and multi-hour hangs.
            if 30 < run_time < 1_200:
                with state_lock:
                    em["total_run_time_seconds"] += run_time
                    em["runs_completed"]         += 1
                    em["run_time_avg_seconds"]    = int(
                        em["total_run_time_seconds"] / em["runs_completed"]
                    )
                log.info("[Log][%s] Dungeon ended — run time %.0fs, avg %.0fs.",
                         uuid, run_time, em["run_time_avg_seconds"])
        with state_lock:
            em = _get_user_state(uuid)["efficiency_metrics"]
            rm = _get_user_state(uuid)["rng_metrics"]
            em["last_run_end"]           = time.time()
            em["current_run_start_time"] = 0.0
            rm["dry_streak_count"]       += 1
        return

    # ------------------------------------------------------------------ #
    # RNG — Rare drop                                                     #
    # ------------------------------------------------------------------ #
    if "RARE DROP!" in line or "CRAZY RARE DROP!" in line:
        with state_lock:
            rm = _get_user_state(uuid)["rng_metrics"]
            rm["last_rare_drop"]       = line
            rm["dry_streak_count"]     = 0
            rm["last_drop_timestamp"]  = time.time()
            rm["time_since_last_drop"] = "0s"
        log.info("[Log][%s] RARE DROP detected — dry streak reset.", uuid)
        return

    # ------------------------------------------------------------------ #
    # RNG — Dry streak increment on boss kill / dungeon completion        #
    # ------------------------------------------------------------------ #
    if "Slayer Boss Slain!" in line or "Dungeon Ended" in line:
        with state_lock:
            _get_user_state(uuid)["rng_metrics"]["dry_streak_count"] += 1
        return


async def _tail_log_async() -> None:
    """
    Async background task that tails ``latest.log`` indefinitely.

    The log config may optionally supply a ``uuid`` field that identifies
    which player this log belongs to.  When present, all parsed events are
    isolated to that player's state bucket.  When absent a generic sentinel
    key ``"_local"`` is used so the server still functions in single-player
    mode without requiring a UUID to be set.

    File I/O is dispatched to a thread pool via ``asyncio.to_thread`` so
    the event loop is never blocked.  The task sleeps 100 ms between polls
    to keep CPU usage negligible while still reacting within one tick.
    """
    try:
        cfg_data = json.loads(Path(LOG_CONFIG_FILE).read_text())
        log_path = cfg_data.get("log_path") or None
        log_uuid = cfg_data.get("uuid", "_local")
    except (OSError, json.JSONDecodeError):
        log_path = None
        log_uuid = "_local"

    if not log_path:
        log.warning(
            "Log tail disabled — '%s' missing or 'log_path' key not set.",
            LOG_CONFIG_FILE,
        )
        return

    log.info("Log tailer started → %s (uuid=%s)", log_path, log_uuid)

    # Open and seek to end so we only process new lines written after startup.
    def _open_seeked() -> Any:
        fh = open(log_path, "r", encoding="utf-8", errors="ignore")  # noqa: WPS515
        fh.seek(0, 2)
        return fh

    fh = await asyncio.to_thread(_open_seeked)

    try:
        while True:
            line: str = await asyncio.to_thread(fh.readline)
            if line:
                _process_log_line(line.strip(), log_uuid)
                # Recalculate elapsed time since last rare drop on every new line
                # so the field stays fresh without a separate timer task.
                with state_lock:
                    rm = _get_user_state(log_uuid)["rng_metrics"]
                    ts = rm["last_drop_timestamp"]
                    if ts is not None:
                        elapsed = int(time.time() - ts)
                        rm["time_since_last_drop"] = (
                            f"{elapsed // 60}m {elapsed % 60}s"
                        )
            else:
                await asyncio.sleep(0.1)
    except asyncio.CancelledError:
        fh.close()
        log.info("Log tailer cancelled — file handle closed.")
    except Exception as exc:
        fh.close()
        log.error("Log tailer crashed: %s", exc)


# ===========================================================================
# Hypixel API helpers  (fully async via httpx)
# ===========================================================================

def _get_slayer_kills(member_data: dict, boss_name: str) -> dict:
    """Calculate the slayer level and XP for a given boss type."""
    try:
        slayer = (
            member_data
            .get("slayer", {})
            .get("slayer_bosses", {})
            .get(boss_name, {})
        )
        xp = slayer.get("xp", 0)
        # Official XP thresholds per level (index = level)
        thresholds = [0, 5, 15, 200, 1_000, 5_000, 20_000, 100_000, 400_000, 1_000_000]
        level = next(
            (i for i, req in enumerate(thresholds) if xp < req),
            len(thresholds),
        ) - 1
        return {"level": max(0, level), "xp": xp}
    except Exception:
        return {"level": 0, "xp": 0}


def _get_mining_stats(member_data: dict, hotm_level: int = MANUAL_HOTM_LEVEL) -> dict:
    """
    Extract Heart of the Mountain and powder totals.

    Parameters
    ----------
    hotm_level:
        Caller-supplied HOTM level that overrides the module-level constant.
        Passed down from the route query parameter so each user can declare
        their own level without touching the source.
    """
    m = member_data.get("mining_core", {})

    def _safe_int(val: Any) -> int:
        """Coerce a value that might be a dict (API quirk) to int."""
        if isinstance(val, dict):
            return 0
        try:
            return abs(int(float(val)))
        except (TypeError, ValueError):
            return 0

    def _total_powder(key: str) -> int:
        total   = _safe_int(m.get(f"powder_{key}_total",  0))
        current = _safe_int(m.get(f"powder_{key}",        0))
        spent   = _safe_int(m.get(f"powder_spent_{key}",  0))
        # Prefer the explicit total; fall back to current + spent
        return max(total, current + spent)

    return {
        "hotm_level":        hotm_level,
        "mithril_powder":    _total_powder("mithril"),
        "gemstone_powder":   _total_powder("gemstone"),
        "glacite_powder":    _total_powder("glacite"),
    }


async def fetch_player_stats(uuid: str, hotm_level: int = MANUAL_HOTM_LEVEL) -> dict[str, Any]:
    """
    Fetch, decode, and return structured player stats for *uuid*.

    Parameters
    ----------
    hotm_level:
        Forwarded from the route query parameter and passed straight into
        ``_get_mining_stats`` so the value is never hardcoded per-request.

    Raises
    ------
    HTTPException(401)  — API key missing or rejected by Hypixel.
    HTTPException(404)  — No profile found for this UUID.
    HTTPException(502)  — Hypixel API returned an unexpected error.
    """
    api_key = _load_api_key()
    if not api_key:
        raise HTTPException(
            status_code=401,
            detail="Hypixel API key not found. Create api_key.txt in the project root.",
        )

    clean_uuid = uuid.replace("-", "")
    headers = {"API-Key": api_key}

    async with httpx.AsyncClient(timeout=15.0) as client:
        # ------------------------------------------------------------------ #
        # 1. Profile endpoint                                                  #
        # ------------------------------------------------------------------ #
        profile_resp = await client.get(
            HYPIXEL_PROFILE_URL,
            params={"uuid": clean_uuid},
            headers=headers,
        )

        if profile_resp.status_code == 403:
            raise HTTPException(status_code=401, detail="Invalid Hypixel API key.")
        if profile_resp.status_code != 200:
            raise HTTPException(
                status_code=502,
                detail=f"Hypixel profile API error {profile_resp.status_code}.",
            )

        profile_json = profile_resp.json()
        if not profile_json.get("success"):
            raise HTTPException(status_code=502, detail="Hypixel API returned success=false.")

        profiles = profile_json.get("profiles")
        if not profiles:
            raise HTTPException(status_code=404, detail=f"No profiles found for UUID {uuid}.")

        # Walk the profiles array and pick the one the player currently has selected.
        # Using "selected": True is more reliable than last_save because it
        # reflects the player's actual active profile, not just the most recently
        # written one (which can be a co-op or old Ironman profile).
        profile = next(
            (p for p in profiles if p.get("selected") is True),
            None,
        )
        if profile is None:
            raise HTTPException(
                status_code=404,
                detail=f"No selected profile found for UUID {uuid}. "
                       "Hypixel may not have returned a 'selected' flag.",
            )

        selected_cute_name = profile.get("cute_name", "Unknown")
        log.info("Selected profile for %s: '%s'", uuid, selected_cute_name)

        profile_id = profile.get("profile_id", "")

        member = profile.get("members", {}).get(clean_uuid)
        if not member:
            raise HTTPException(
                status_code=404,
                detail=f"UUID {uuid} is not a member of any returned profile.",
            )

        # ------------------------------------------------------------------ #
        # 2. Museum endpoint (independent — fire alongside inventory decodes) #
        # ------------------------------------------------------------------ #
        museum_task = asyncio.create_task(
            client.get(HYPIXEL_MUSEUM_URL, params={"profile": profile_id}, headers=headers)
        )

        # ------------------------------------------------------------------ #
        # 3. Wealth                                                            #
        # ------------------------------------------------------------------ #
        def _safe_coin(val: Any) -> int:
            if isinstance(val, dict):
                val = val.get("coin_purse", 0)
            try:
                return int(float(val))
            except (TypeError, ValueError):
                return 0

        purse = _safe_coin(member.get("currencies", {}).get("coin_purse", member.get("coin_purse", 0)))
        bank  = _safe_coin(profile.get("banking", {}).get("balance", 0))

        # ------------------------------------------------------------------ #
        # 4. Inventory blobs                                                   #
        # ------------------------------------------------------------------ #
        inv_root = member.get("inventory", {})

        def _inv_blob(key: str) -> str:
            """Resolve an inventory blob from the nested v2 layout."""
            return (
                inv_root.get(key, {}).get("data")
                or member.get(key, {}).get("data")
                or ""
            )

        # Decode all inventories concurrently — they're independent of each other
        (
            armor_items,
            inv_items,
            ec_items,
            wardrobe_items,
            accessory_items,
        ) = await asyncio.gather(
            decode_inventory(_inv_blob("inv_armor"),           "Armor"),
            decode_inventory(_inv_blob("inv_contents"),        "Inventory"),
            decode_inventory(_inv_blob("ender_chest_contents"),"Ender Chest"),
            decode_inventory(_inv_blob("wardrobe_contents"),   "Wardrobe"),
            decode_inventory(
                (
                    inv_root.get("bag_contents", {})
                    .get("talisman_bag", {})
                    .get("data")
                    or _inv_blob("talisman_bag")
                ),
                "Accessories",
            ),
        )

        # Backpacks (variable count) — gather concurrently as well
        bp_dict: dict = (
            inv_root.get("backpack_contents", {})
            or member.get("backpack_contents", {})
        )
        backpack_tasks = {
            f"BP_{k}": asyncio.create_task(
                decode_inventory(v.get("data", ""), f"Backpack {k}")
            )
            for k, v in bp_dict.items()
            if isinstance(v, dict) and v.get("data")
        }
        backpacks = {}
        for bp_key, task in backpack_tasks.items():
            backpacks[bp_key] = await task

        # ------------------------------------------------------------------ #
        # 5. Dungeons & secrets                                                #
        # ------------------------------------------------------------------ #
        dungeons   = member.get("dungeons", {})
        catacombs  = dungeons.get("dungeon_types", {}).get("catacombs", {})
        secrets    = dungeons.get("secrets", 0)

        # ------------------------------------------------------------------ #
        # 6. Pets                                                              #
        # ------------------------------------------------------------------ #
        pets_raw   = member.get("pets_data", {}).get("pets", member.get("pets", []))
        active_pet = next(
            (p for p in pets_raw if isinstance(p, dict) and p.get("active")),
            {"type": "NONE"},
        )

        # ------------------------------------------------------------------ #
        # 7. Skills / leveling                                                 #
        # ------------------------------------------------------------------ #
        leveling  = member.get("leveling", {})
        exp_raw   = leveling.get("experience", 0)
        if isinstance(exp_raw, dict):
            exp_raw = exp_raw.get("experience", 0)
        try:
            sb_level = int(float(exp_raw) / 100)
        except (TypeError, ValueError):
            sb_level = 0

        skills_raw = member.get("player_data", {}).get("experience", {})
        skills = {
            k.replace("SKILL_", "").lower(): v
            for k, v in skills_raw.items()
            if k.startswith("SKILL_") and not isinstance(v, dict)
        }

        # ------------------------------------------------------------------ #
        # 8. Crimson Isle                                                      #
        # ------------------------------------------------------------------ #
        nether_data  = member.get("nether_island_player_data", {})
        faction      = nether_data.get("selected_faction", "NONE")
        kuudra_tiers = nether_data.get("kuudra_completed_tiers", {})

        # ------------------------------------------------------------------ #
        # 9. Bestiary                                                          #
        # ------------------------------------------------------------------ #
        bestiary_data  = member.get("bestiary", {})
        bestiary_kills = bestiary_data.get("kills", {})
        milestone_raw  = bestiary_data.get("milestone", {})
        if isinstance(milestone_raw, dict):
            bestiary_milestone_val = milestone_raw.get(
                "last_claimed_milestone", len(bestiary_kills) / 10
            )
        else:
            bestiary_milestone_val = milestone_raw
        try:
            bestiary_milestone = int(float(bestiary_milestone_val))
        except (TypeError, ValueError):
            bestiary_milestone = int(len(bestiary_kills) / 10)

        # ------------------------------------------------------------------ #
        # 10. Museum  (resolve the task we fired earlier)                      #
        # ------------------------------------------------------------------ #
        museum_items: list[str] = []
        try:
            museum_resp = await museum_task
            if museum_resp.status_code == 200:
                museum_json = museum_resp.json()
                if museum_json.get("success"):
                    user_museum = museum_json.get("members", {}).get(clean_uuid, {})
                    for _, items_dict in user_museum.get("items", {}).items():
                        for name, data in items_dict.items():
                            if isinstance(data, dict) and data.get("donated_time"):
                                museum_items.append(name)
        except Exception as exc:
            log.warning("Museum fetch failed: %s", exc)
            museum_items = [f"Museum fetch error: {exc}"]

        # ------------------------------------------------------------------ #
        # 11. Assemble the response payload                                    #
        # ------------------------------------------------------------------ #
        payload: dict[str, Any] = {
            "meta": {
                "uuid":       clean_uuid,
                "profile_id": profile_id,
                "profile":    profile.get("cute_name"),
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "version":    "OPERATOR_v10.4_ASYNC",
            },
            "wealth": {
                "purse": purse,
                "bank":  bank,
                "total": purse + bank,
            },
            "gear": {
                # Armor arrives toe→head from the API; reverse to display head→toe
                "equipped_armor": armor_items[::-1],
                "inventory":      inv_items,
                "ender_chest":    ec_items,
                "wardrobe":       wardrobe_items,
                "backpacks":      backpacks,
                "accessories":    accessory_items,
            },
            "progression": {
                "skyblock_level": sb_level,
                "skills_xp":      skills,
                "dungeons": {
                    "secrets_found_lifetime":  secrets,
                    "highest_tier_completed":  catacombs.get("highest_tier_completed", 0),
                },
                "crimson_isle": {
                    "faction":             faction,
                    "kuudra_completions":  kuudra_tiers,
                },
                "bestiary_milestone_approx": bestiary_milestone,
                "active_pet": active_pet.get("type"),
            },
            "museum": museum_items or ["Museum Empty"],
            "combat": {
                "slayers": {
                    boss: _get_slayer_kills(member, boss)
                    for boss in ["zombie", "spider", "wolf", "enderman", "blaze", "vampire"]
                },
            },
            "mining": _get_mining_stats(member, hotm_level),
        }

        return payload


# ===========================================================================
# Routes
# ===========================================================================


class ClientPushPayload(BaseModel):
    """
    Schema for data pushed from the Fabric mod every client tick / event.

    *uuid* is required so the server can isolate telemetry and cache files
    for each player.  All other fields are optional so the mod can send
    partial updates without the server rejecting the payload.
    """

    uuid:       str             # Raw Mojang UUID (no hyphens) — required
    purse:      float | None = None   # Coin purse as read client-side
    held_item:  str   | None = None   # Display name of the currently held item
    location:   str   | None = None   # Island / area string from the scoreboard
    question:   str   | None = None   # Optional AI question from the Wiki-HUD
    extra:      dict  | None = None   # Catch-all for any additional mod fields


# ---------------------------------------------------------------------------
# Gemini AI helper  — reusable by both client_push (smart router) and /ask
# ---------------------------------------------------------------------------

_WIKI_SYSTEM_PROMPT = (
    "You are the Skyblock Menu Guide. "
    "When a player opens a menu, analyze their current stats and location "
    "to give them a specific 3-step priority list for that screen. "
    "Be concise, actionable, and direct — one clear numbered list per response."
)


async def ask_gemini(question: str, system_prompt: str | None = None) -> str:
    """
    Send *question* to Gemini 2.5 Flash and return the raw text response.

    Parameters
    ----------
    question:
        The user-facing question or context string.
    system_prompt:
        Optional override for the system persona. When ``None`` the default
        ``_WIKI_SYSTEM_PROMPT`` is used, preserving backward compatibility
        with the existing ``/ask`` route and wiki flow.

    Raises
    ------
    HTTPException(500)  — ``gemini_key.txt`` missing.
    HTTPException(502)  — Gemini API returned a non-200 or malformed response.
    """
    key_path = Path(GEMINI_KEY_FILE)
    if not key_path.exists():
        raise HTTPException(status_code=500, detail="gemini_key.txt not found.")
    gemini_key = key_path.read_text().strip()

    url = f"{GEMINI_URL}?key={gemini_key}"

    active_system_prompt = system_prompt if system_prompt is not None else _WIKI_SYSTEM_PROMPT
    prompt_text = f"{active_system_prompt}\n\nQUESTION:\n{question}"

    request_body = {
        "contents": [{
            "parts": [{"text": prompt_text}]
        }]
    }

    log.info("[ask_gemini] Sending to Gemini 2.5 Flash: %s", question[:120])

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(url, json=request_body)

    if resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"Gemini API error {resp.status_code}: {resp.text}",
        )

    try:
        data = resp.json()
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError) as exc:
        raise HTTPException(status_code=502, detail=f"Unexpected Gemini response: {exc}")


@app.post("/api/v1/client_push", tags=["Client"], status_code=200)
async def client_push(payload: ClientPushPayload) -> dict[str, str]:
    """
    **Smart Router** — accepts a live-data push from the Fabric mod.

    Behaviour
    ---------
    1. Reads ``uuid`` from the payload to route all state and cache I/O to
       the correct per-player bucket — no cross-player contamination.
    2. Always merge non-None telemetry fields (purse, held_item, location,
       extra) into the per-UUID ``live_client_data`` bucket.
    3. If ``question == "ANALYZE_CONTEXT"``, load the player's own cache file
       (``cache_{uuid}.json``), build a context-aware prompt, call
       :func:`ask_gemini`, and return ``{"directive": "<AI answer>"}``.
    4. If any other ``question`` is present and not ``"none"`` (case-insensitive),
       call :func:`ask_gemini` with the full per-UUID cached context.
    5. Otherwise return ``{"status": "ok"}``.
    """
    uuid     = payload.uuid.replace("-", "")   # normalise to no-hyphen form
    question = payload.question

    incoming = payload.model_dump(exclude_none=True)
    # Remove routing/identity fields — not raw telemetry
    for key in ("uuid", "question"):
        incoming.pop(key, None)

    # Flatten the optional ``extra`` sub-dict into the top level.
    extra = incoming.pop("extra", None) or {}
    incoming.update(extra)

    with state_lock:
        _get_user_state(uuid)["live_client_data"].update(incoming)

    log.info("[ClientPush][%s] received %d field(s): %s", uuid, len(incoming), list(incoming.keys()))

    # Helper: safely load this player's own cache file.
    async def _load_player_cache() -> str:
        try:
            raw_context = await load_cache(uuid)
            return json.dumps(raw_context or {})
        except (OSError, json.JSONDecodeError) as _err:
            log.warning("[ClientPush][%s] cache unreadable (%s) — using empty context.", uuid, _err)
            return "{}"

    # --- ANALYZE_CONTEXT: inject full per-UUID cache into Omni-Analyst prompt ---
    if question and question.strip().upper() == "ANALYZE_CONTEXT":
        purse_val      = payload.purse or 0
        full_json_dump = await _load_player_cache()

        context_prompt = (
            f"You are the Skyblock Omni-Analyst. You have the player's live telemetry "
            f"(Purse: {purse_val:,.0f} coins). "
            f"Here is their ENTIRE Hypixel profile data in raw JSON format: "
            f"{full_json_dump} "
            f"The user wants a context analysis. "
            f"Analyze the raw JSON data and provide strategic, actionable advice. "
            f"Use \u00a76 for coins and \u00a7b for stats. "
            f"Do not act like a wiki; be a live analyst."
        )

        log.info(
            "[ClientPush][%s] ANALYZE_CONTEXT — purse=%s | json_len=%d",
            uuid, purse_val, len(full_json_dump),
        )
        ai_response_text = await ask_gemini("Analyze the player context.", system_prompt=context_prompt)
        return {"directive": ai_response_text}

    # --- Generic chat question: inject full per-UUID cache + Omni-Analyst prompt ---
    if question and question.strip().lower() != "none":
        purse_val      = payload.purse or 0
        full_json_dump = await _load_player_cache()

        chat_prompt = (
            f"You are the Skyblock Omni-Analyst. You have the player's live telemetry "
            f"(Purse: {purse_val:,.0f} coins). "
            f"Here is their ENTIRE Hypixel profile data in raw JSON format: "
            f"{full_json_dump} "
            f"The user is asking: '{question}'. "
            f"Analyze the raw JSON data to find the exact information needed to answer their question. "
            f"Provide strategic, actionable advice. "
            f"Use \u00a76 for coins and \u00a7b for stats. "
            f"Do not act like a wiki; be a live analyst."
        )
        log.info("[ClientPush][%s] Chat question → Gemini [purse=%s, json_len=%d]: %s",
                 uuid, purse_val, len(full_json_dump), question[:80])
        answer = await ask_gemini(question, system_prompt=chat_prompt)
        return {"directive": answer}

    return {"status": "ok"}


@app.get("/health", tags=["Meta"])
async def health_check() -> dict:
    """Simple liveness probe used by load-balancers / uptime monitors."""
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.get("/api/v1/player/{username}", tags=["Player"])
async def get_player(
    username: str,
    background_tasks: BackgroundTasks,
    use_cache: bool = False,
    hotm_level: int = MANUAL_HOTM_LEVEL,
) -> dict[str, Any]:
    """
    Fetch live Hypixel Skyblock stats for the given Minecraft **username**.

    - **username**   — Current Minecraft Java Edition username (case-insensitive).
    - **use_cache**  — If ``true``, return the last payload written to
      ``omni_context.json`` instead of hitting the Mojang or Hypixel APIs.
    - **hotm_level** — Override the Heart of the Mountain level for this
      request (default: ``MANUAL_HOTM_LEVEL``). Passed straight into the
      mining stats block so different players can supply their own level.

    **Resolution flow**

    1. Resolve *username* → UUID via Mojang.
    2. (Optional) short-circuit with the on-disk cache if ``use_cache=true``.
    3. Load the previous cache snapshot for session-delta calculations.
    4. Determine whether this is a new session (no cache, wrong UUID, or cache
       older than 2 hours) and seed cumulative baseline values accordingly.
    5. Fetch live stats from Hypixel.
    6. Calculate cumulative XP/coin deltas against the session baseline.
        7. Run the 60-second downtime police and accumulate idle time.
    8. Scan gear for rare-drop keywords and update the RNG therapist block.
    9. Persist the enriched payload fire-and-forget via BackgroundTasks.
    """
    # ---------------------------------------------------------------------- #
    # Step 1 — Mojang username → UUID resolution                             #
    # ---------------------------------------------------------------------- #
    uuid, canonical_name = await resolve_username_to_uuid(username)
    log.info("Player '%s' resolved to UUID %s", canonical_name, uuid)

    # Snapshot the module-level log-based metrics NOW, before the local
    # `efficiency_metrics` variable defined later in this function shadows
    # the global name.  Shallow-copy so mutations don't bleed into the resp.
    with state_lock:
        _usr = _get_user_state(uuid)
        snap_financial:  dict = dict(_usr["financial_metrics"])
        snap_efficiency: dict = dict(_usr["efficiency_metrics"])
        snap_rng:        dict = dict(_usr["rng_metrics"])

    # ---------------------------------------------------------------------- #
    # Step 2 — Serve from cache if caller requested it                       #
    # ---------------------------------------------------------------------- #
    if use_cache:
        cached = await load_cache(uuid)
        if cached and cached.get("meta", {}).get("uuid") == uuid:
            log.info("Serving cached response for %s (%s)", canonical_name, uuid)
            return cached
        log.info(
            "Cache miss for %s (%s) — falling through to live fetch.",
            canonical_name,
            uuid,
        )

    # ---------------------------------------------------------------------- #
    # Step 3 — Load the previous snapshot BEFORE the live fetch              #
    # ---------------------------------------------------------------------- #
    old_stats = await load_cache(uuid)
    old_meta        = (old_stats or {}).get("meta", {})
    old_progression = (old_stats or {}).get("progression", {})
    old_wealth      = (old_stats or {}).get("wealth", {})
    old_live        = old_meta.get("live_session", {})
    old_efficiency  = old_meta.get("efficiency_metrics", {})
    old_rng         = (old_stats or {}).get("rng_status", {})

    now_ts = datetime.now(timezone.utc)

    # ---------------------------------------------------------------------- #
    # Step 4 — Session boundary detection (new session if > 2 hours old,    #
    #          cache absent, or cached UUID belongs to a different player)   #
    # ---------------------------------------------------------------------- #
    is_new_session = True
    if old_stats and old_meta.get("uuid") == uuid:
        try:
            old_fetched_dt = datetime.fromisoformat(
                old_meta["fetched_at"].rstrip("Z")
            )
            if (now_ts - old_fetched_dt).total_seconds() < 7_200:  # 2 hours
                is_new_session = False
        except (KeyError, ValueError, TypeError):
            pass  # Malformed or missing timestamp → treat as new session.

    if is_new_session:
        log.info("New session started for %s.", canonical_name)

    # Preserve (or initialise) the session wall-clock start time.
    session_start: str = (
        old_live.get("session_start")
        if not is_new_session
        else now_ts.strftime("%H:%M:%S")
    )

    # ---------------------------------------------------------------------- #
    # Step 5 — Live fetch from Hypixel                                       #
    # ---------------------------------------------------------------------- #
    log.info("Fetching live Hypixel stats for %s (%s)", canonical_name, uuid)
    stats = await fetch_player_stats(uuid, hotm_level)

    if "meta" not in stats:
        stats["meta"] = {}
    stats["meta"]["username"] = canonical_name

    fetched_at: str = stats["meta"].get("fetched_at", now_ts.isoformat())

    # Elapsed wall-clock seconds since session_start.
    # Use a naive datetime for strptime so tz-aware now_ts can't cause a
    # TypeError when subtracting a naive session_start_dt from it.
    now_naive = now_ts.replace(tzinfo=None)
    try:
        session_start_dt = datetime.strptime(
            now_naive.strftime("%Y-%m-%d") + " " + session_start,
            "%Y-%m-%d %H:%M:%S",
        )
        elapsed_seconds = int((now_naive - session_start_dt).total_seconds())
    except ValueError:
        elapsed_seconds = 0

    # ---------------------------------------------------------------------- #
    # Step 6 — Cumulative XP / Coin deltas (Requirement 1)                  #
    #                                                                        #
    # On a new session the initial values are seeded from the *current*      #
    # fetch so the first delta is always 0.  On continuation the baseline    #
    # is pulled from the stored live_session block.                          #
    # ---------------------------------------------------------------------- #
    new_total_coins: int   = stats.get("wealth", {}).get("total", 0)
    new_skills: dict       = stats.get("progression", {}).get("skills_xp", {})
    new_xp_total: float    = sum(new_skills.values())

    if is_new_session:
        session_initial_coins: int   = new_total_coins
        session_initial_xp: float    = new_xp_total
    else:
        session_initial_coins = old_live.get("session_initial_coins", new_total_coins)
        session_initial_xp    = old_live.get("session_initial_xp",    new_xp_total)

    cumulative_coin_delta: int   = new_total_coins - session_initial_coins
    cumulative_xp_delta: float   = new_xp_total    - session_initial_xp

    # Point-to-point per-skill deltas (useful for identifying which skill
    # moved since the last poll — kept alongside the cumulative figures).
    old_skills: dict = old_progression.get("skills_xp", {})
    skill_xp_deltas: dict[str, float] = {
        skill: new_skills.get(skill, 0) - old_skills.get(skill, 0)
        for skill in new_skills
    }
    # Point-to-point coin delta vs. last snapshot (feeds downtime check).
    point_coin_delta: int   = new_total_coins - old_wealth.get("total", new_total_coins)
    point_xp_delta: float   = new_xp_total    - sum(old_skills.values())

    live_session: dict[str, Any] = {
        "session_start":          session_start,
        "fetched_at":             fetched_at,
        "elapsed_seconds":        elapsed_seconds,
        "session_initial_coins":  session_initial_coins,
        "session_initial_xp":     session_initial_xp,
        "cumulative_coin_delta":  cumulative_coin_delta,
        "cumulative_xp_delta":    cumulative_xp_delta,
    }

    # ---------------------------------------------------------------------- #
    # Step 7 — 60-second downtime police + cumulative idle time              #
    #          (Requirement 2)                                               #
    # ---------------------------------------------------------------------- #
    downtime_alert   = False
    interval_seconds = 0.0

    try:
        old_fetched_at_str: str | None = old_meta.get("fetched_at")
        if old_fetched_at_str and not is_new_session:
            old_fetched_dt   = datetime.fromisoformat(old_fetched_at_str.rstrip("Z"))
            interval_seconds = (now_ts - old_fetched_dt).total_seconds()
            if interval_seconds > 60 and point_xp_delta == 0 and point_coin_delta == 0:
                downtime_alert = True
                log.info(
                    "Downtime alert for %s — no XP or coin change in %.0fs.",
                    canonical_name,
                    interval_seconds,
                )
    except (ValueError, TypeError):
        pass  # Malformed timestamp — skip downtime check safely.

    # Accumulate total idle seconds across the session so
    # downtime_avg_seconds can be reported as a meaningful average.
    prev_total_idle: int      = old_efficiency.get("total_idle_seconds", 0)
    prev_idle_events: int     = old_efficiency.get("downtime_event_count", 0)

    total_idle_seconds: int   = prev_total_idle  + (int(interval_seconds) if downtime_alert else 0)
    downtime_event_count: int = prev_idle_events + (1                     if downtime_alert else 0)
    downtime_avg_seconds: int = (
        total_idle_seconds // downtime_event_count if downtime_event_count else 0
    )

    local_efficiency_metrics: dict[str, Any] = {
        "point_coin_delta":    point_coin_delta,
        "point_xp_delta":      point_xp_delta,
        "skill_xp_deltas":     skill_xp_deltas,
        "downtime_alert":      downtime_alert,
        "downtime_avg_seconds":downtime_avg_seconds,
        "total_idle_seconds":  total_idle_seconds,
        "downtime_event_count":downtime_event_count,
    }

    # ---------------------------------------------------------------------- #
    # Step 8 — RNG Therapist / drop tracker (Requirement 3)                 #
    #                                                                        #
    # Scan inventory and ender_chest item-name lists for substrings that     #
    # indicate a rare drop landed this poll cycle ("Core" and "Handle" are  #
    # the two canonical rare-drop name fragments for Necron's Handle and     #
    # HOTM cores).  A hit resets dry_streak_count to 0; a miss increments.  #
    # All other rng_status fields are preserved from the previous snapshot   #
    # so the therapist block survives across refreshes unchanged.            #
    # ---------------------------------------------------------------------- #
    _DROP_KEYWORDS = ("Core", "Handle")

    inventory_items:    list[str] = stats.get("gear", {}).get("inventory",   [])
    ender_chest_items:  list[str] = stats.get("gear", {}).get("ender_chest", [])
    scanned_items: list[str]      = inventory_items + ender_chest_items

    rare_drop_found: bool = any(
        kw in item
        for item in scanned_items
        for kw in _DROP_KEYWORDS
    )

    prev_dry_streak: int  = old_rng.get("dry_streak_count", 0) if not is_new_session else 0
    prev_last_drop: str   = old_rng.get("last_rare_drop",   "None this session") if not is_new_session else "None this session"
    prev_drop_time: str   = old_rng.get("time_since_last_drop", "0s") if not is_new_session else "0s"

    if rare_drop_found:
        matched_drops = [
            item for item in scanned_items
            if any(kw in item for kw in _DROP_KEYWORDS)
        ]
        new_dry_streak  = 0
        new_last_drop   = matched_drops[0]   # Report the first matched item name.
        new_drop_time   = "0s"               # Just found — elapsed time resets.
        log.info(
            "Rare drop detected for %s: %s — dry streak reset.",
            canonical_name,
            matched_drops,
        )
    else:
        # Read-only: do NOT increment here. The log-tail thread handles that.
        new_dry_streak  = prev_dry_streak
        new_last_drop   = prev_last_drop
        new_drop_time   = prev_drop_time     # Preserved; recalculated from timestamp.

    rng_status: dict[str, Any] = {
        "last_rare_drop":        new_last_drop,
        "time_since_last_drop":  new_drop_time,
        "dry_streak_count":      new_dry_streak,
    }

    # ---------------------------------------------------------------------- #
    # Assemble enriched meta and top-level blocks into the payload           #
    # ---------------------------------------------------------------------- #
    stats["meta"]["live_session"]       = live_session
    stats["meta"]["efficiency_metrics"] = local_efficiency_metrics
    stats["rng_status"]                 = rng_status

    # Merge log-based session state captured at the top of this function.
    # These sit at the top level so the frontend and AI prompt can read them
    # without digging into meta.
    stats["financial_metrics"]  = snap_financial
    stats["log_efficiency"]     = snap_efficiency
    stats["rng_metrics"]        = snap_rng

        # ---------------------------------------------------------------------- #
    # Step 9 — Merge live client-push data                                   #
    #                                                                        #
    # Data pushed by the Fabric mod is fresher than anything from the        #
    # Hypixel API (which can lag by minutes).  Client values override the    #
    # stale API values for the fields the mod reports.                       #
    # ---------------------------------------------------------------------- #
    with state_lock:
        client_snap: dict = dict(_get_user_state(uuid)["live_client_data"])

    if client_snap:
        # Override purse with the live client-side value if available.
        if "purse" in client_snap:
            live_purse = int(client_snap["purse"])
            stats["wealth"]["purse"] = live_purse
            stats["wealth"]["total"] = live_purse + stats["wealth"].get("bank", 0)

        # Attach the full client snapshot so the frontend / AI can read
        # held_item, location, and any extra fields the mod pushes.
        stats["live_client"] = client_snap
        log.info("[get_player][%s] merged live_client_data (%d field(s))", uuid, len(client_snap))

    # ---------------------------------------------------------------------- #
    # Step 10 — Persist to disk fire-and-forget                              #
    # ---------------------------------------------------------------------- #
    background_tasks.add_task(save_cache, stats, uuid)

    return stats


# ===========================================================================
# AI — /api/v1/ask
# ===========================================================================

class AskRequest(BaseModel):
    question: str
    stats: dict


@app.post("/api/v1/ask")
async def ask_question(body: AskRequest):
    """
    Receives a question and stats from the Fabric mod, 
    asks Gemini 2.5 Flash, and returns a 'directive'.
    """
    # 1. Read the Gemini Key
    key_path = Path("gemini_key.txt")
    if not key_path.exists():
        raise HTTPException(status_code=500, detail="gemini_key.txt not found.")
    gemini_key = key_path.read_text().strip()

    # 2. Modern 2026 Gemini 2.5 URL
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={gemini_key}"

    # 3. Build the Prompt
    # We combine the player stats and question so Gemini has context.
    stats_block = json.dumps(body.stats, indent=2, default=str)
    prompt_text = (
        "You are The Operator, a hyper-intelligent Hypixel Skyblock engine. "
        "Analyze the player's live session data and answer their question. "
        "If downtime > 60s, aggressively critique the user. "
        "If net profit is negative, demand a safe flip. "
        "Use heavy profanity.\n\n"
        f"PLAYER STATS:\n{stats_block}\n\n"
        f"QUESTION:\n{body.question}"
    )

    payload = {
        "contents": [{
            "parts": [{"text": prompt_text}]
        }]
    }

    log.info("Sending question to Gemini 2.5 Flash: %s", body.question)

    # 4. Make the Request
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(url, json=payload)

    if resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"Gemini API error {resp.status_code}: {resp.text}"
        )

    # 5. Extract and Return
    try:
        data = resp.json()
        response_text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError) as e:
        raise HTTPException(status_code=502, detail=f"Unexpected Gemini response: {e}")

    # This 'directive' key MUST match what your Java code is looking for
    return {"directive": response_text}


# ===========================================================================
# Entry point
# ===========================================================================

if __name__ == "__main__":
    import uvicorn
    import socket
    import webbrowser

    # Get your local IP address for the printout
    hostname = socket.gethostname()
    try:
        local_ip = socket.gethostbyname(hostname)
    except:
        local_ip = "127.0.0.1"

    print("\n" + "="*50)
    print("OPERATOR ENGINE ONLINE")
    print(f"LOCAL ACCESS:   http://127.0.0.1:8000/docs")
    print(f"NETWORK ACCESS: http://{local_ip}:8000/docs")
    print("="*50 + "\n")

    # This MUST be indented exactly like the prints above
    webbrowser.open("http://127.0.0.1:8000/docs")

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info",
    )