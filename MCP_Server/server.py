# ableton_mcp_server.py
from mcp.server.fastmcp import FastMCP, Context
import socket
import json
import logging
import os
import random
from dataclasses import dataclass
from contextlib import asynccontextmanager
from typing import AsyncIterator, Dict, Any, List, Union, Optional

from .telemetry import record_startup
from .telemetry_decorator import telemetry_tool, rich_telemetry_tool
from . import music_theory

ABLETON_HOST = os.environ.get("ABLETON_HOST", "localhost")
ABLETON_PORT = int(os.environ.get("ABLETON_PORT", "9877"))

PROJECT_STATE_PATH = os.path.join(os.path.dirname(__file__), "project_key.json")


def _load_project_state() -> Dict[str, Any]:
    try:
        with open(PROJECT_STATE_PATH, "r") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {"tonic": None, "mode": None, "tempo": None, "genre": None}


def _save_project_state(state: Dict[str, Any]) -> None:
    with open(PROJECT_STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


PROJECT = _load_project_state()

# Configure logging
logging.basicConfig(level=logging.INFO, 
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("AbletonMCPServer")

@dataclass
class AbletonConnection:
    host: str
    port: int
    sock: socket.socket = None
    
    def connect(self) -> bool:
        """Connect to the Ableton Remote Script socket server"""
        if self.sock:
            return True

        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(5.0)
            self.sock.connect((self.host, self.port))
            self.sock.settimeout(None)
            logger.info(f"Connected to Ableton at {self.host}:{self.port}")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to Ableton at {self.host}:{self.port}: {str(e)}")
            self.sock = None
            return False
    
    def disconnect(self):
        """Disconnect from the Ableton Remote Script"""
        if self.sock:
            try:
                self.sock.close()
            except Exception as e:
                logger.error(f"Error disconnecting from Ableton: {str(e)}")
            finally:
                self.sock = None

    def receive_full_response(self, sock, buffer_size=8192):
        """Receive the complete response, potentially in multiple chunks"""
        chunks = []
        sock.settimeout(15.0)  # Increased timeout for operations that might take longer
        
        try:
            while True:
                try:
                    chunk = sock.recv(buffer_size)
                    if not chunk:
                        if not chunks:
                            raise Exception("Connection closed before receiving any data")
                        break
                    
                    chunks.append(chunk)
                    
                    # Check if we've received a complete JSON object
                    try:
                        data = b''.join(chunks)
                        json.loads(data.decode('utf-8'))
                        logger.info(f"Received complete response ({len(data)} bytes)")
                        return data
                    except json.JSONDecodeError:
                        # Incomplete JSON, continue receiving
                        continue
                except socket.timeout:
                    logger.warning("Socket timeout during chunked receive")
                    break
                except (ConnectionError, BrokenPipeError, ConnectionResetError) as e:
                    logger.error(f"Socket connection error during receive: {str(e)}")
                    raise
        except Exception as e:
            logger.error(f"Error during receive: {str(e)}")
            raise
            
        # If we get here, we either timed out or broke out of the loop
        if chunks:
            data = b''.join(chunks)
            logger.info(f"Returning data after receive completion ({len(data)} bytes)")
            try:
                json.loads(data.decode('utf-8'))
                return data
            except json.JSONDecodeError:
                raise Exception("Incomplete JSON response received")
        else:
            raise Exception("No data received")

    def send_command(self, command_type: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
        """Send a command to Ableton and return the response"""
        if not self.sock and not self.connect():
            raise ConnectionError("Not connected to Ableton")
        
        command = {
            "type": command_type,
            "params": params or {}
        }
        
        # Check if this is a state-modifying command
        is_modifying_command = command_type in [
            "create_midi_track", "create_audio_track", "set_track_name",
            "create_clip", "create_audio_clip", "add_notes_to_clip", "set_clip_name",
            "set_tempo", "fire_clip", "stop_clip", "set_device_parameter", "set_multiple_device_parameters",
            "delete_track", "delete_clip", "delete_device",
            "start_playback", "stop_playback", "load_instrument_or_effect",
            # Arrangement view commands
            "switch_to_arrangement_view", "set_current_song_time",
            "duplicate_session_clip_to_arrangement"
        ]

        # Commands whose work on Live's main thread can take noticeably longer
        # than the default modifying-command budget (e.g. importing/decoding a
        # large audio file). Give them a wider socket timeout so we don't time
        # out before the Remote Script's own queue does.
        long_running_commands = {"create_audio_clip": 65.0}
        
        try:
            logger.info(f"Sending command: {command_type} with params: {params}")
            
            # Send the command
            self.sock.sendall(json.dumps(command).encode('utf-8'))
            logger.info(f"Command sent, waiting for response...")
            
            # Set timeout based on command type
            if command_type in long_running_commands:
                timeout = long_running_commands[command_type]
            else:
                timeout = 15.0 if is_modifying_command else 10.0
            self.sock.settimeout(timeout)

            # Receive the response
            response_data = self.receive_full_response(self.sock)
            logger.info(f"Received {len(response_data)} bytes of data")

            # Parse the response
            response = json.loads(response_data.decode('utf-8'))
            logger.info(f"Response parsed, status: {response.get('status', 'unknown')}")

            if response.get("status") == "error":
                logger.error(f"Ableton error: {response.get('message')}")
                raise Exception(response.get("message", "Unknown error from Ableton"))
            
            return response.get("result", {})
        except socket.timeout:
            logger.error("Socket timeout while waiting for response from Ableton")
            self.sock = None
            raise Exception("Timeout waiting for Ableton response")
        except (ConnectionError, BrokenPipeError, ConnectionResetError) as e:
            logger.error(f"Socket connection error: {str(e)}")
            self.sock = None
            raise Exception(f"Connection to Ableton lost: {str(e)}")
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON response from Ableton: {str(e)}")
            if 'response_data' in locals() and response_data:
                logger.error(f"Raw response (first 200 bytes): {response_data[:200]}")
            self.sock = None
            raise Exception(f"Invalid response from Ableton: {str(e)}")
        except Exception as e:
            logger.error(f"Error communicating with Ableton: {str(e)}")
            self.sock = None
            raise Exception(f"Communication error with Ableton: {str(e)}")

@asynccontextmanager
async def server_lifespan(server: FastMCP) -> AsyncIterator[Dict[str, Any]]:
    """Manage server startup and shutdown lifecycle"""
    try:
        logger.info("AbletonMCP server starting up")

        # Record startup event for telemetry
        try:
            record_startup()
        except Exception as e:
            logger.debug(f"Failed to record startup telemetry: {e}")

        try:
            ableton = get_ableton_connection()
            logger.info("Successfully connected to Ableton on startup")
        except Exception as e:
            logger.warning(f"Could not connect to Ableton on startup: {str(e)}")
            logger.warning("Make sure the Ableton Remote Script is running")

        yield {}
    finally:
        global _ableton_connection
        if _ableton_connection:
            logger.info("Disconnecting from Ableton on shutdown")
            _ableton_connection.disconnect()
            _ableton_connection = None
        logger.info("AbletonMCP server shut down")

# Create the MCP server with lifespan support
mcp = FastMCP(
    "AbletonMCP",
    lifespan=server_lifespan
)

# Global connection for resources
_ableton_connection = None

def get_ableton_connection():
    """Get or create a persistent Ableton connection"""
    global _ableton_connection

    if _ableton_connection is not None and _ableton_connection.sock is not None:
        try:
            # Check if the socket is still alive by peeking for data
            # MSG_PEEK + MSG_DONTWAIT will raise BlockingIOError if alive but no data,
            # or return b'' if the remote end has closed the connection.
            _ableton_connection.sock.setblocking(False)
            try:
                data = _ableton_connection.sock.recv(1, socket.MSG_PEEK)
                if data == b'':
                    raise ConnectionError("Remote end closed")
            except BlockingIOError:
                pass  # Socket is alive, just no data waiting — this is normal
            finally:
                _ableton_connection.sock.setblocking(True)
            return _ableton_connection
        except Exception as e:
            logger.warning(f"Existing connection is no longer valid: {str(e)}")
            try:
                _ableton_connection.disconnect()
            except:
                pass
            _ableton_connection = None
    
    # Connection doesn't exist or is invalid, create a new one
    if _ableton_connection is None:
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                logger.info(f"Connecting to Ableton at {ABLETON_HOST}:{ABLETON_PORT} (attempt {attempt}/{max_attempts})...")
                _ableton_connection = AbletonConnection(host=ABLETON_HOST, port=ABLETON_PORT)
                if _ableton_connection.connect():
                    logger.info("Created new persistent connection to Ableton")
                    return _ableton_connection
                else:
                    _ableton_connection = None
            except Exception as e:
                logger.error(f"Connection attempt {attempt} failed: {str(e)}")
                if _ableton_connection:
                    _ableton_connection.disconnect()
                    _ableton_connection = None

            if attempt < max_attempts:
                import time
                time.sleep(1.0)
        
        # If we get here, all connection attempts failed
        if _ableton_connection is None:
            logger.error("Failed to connect to Ableton after multiple attempts")
            raise Exception("Could not connect to Ableton. Make sure the Remote Script is running.")
    
    return _ableton_connection


# Core Tool endpoints

@mcp.tool()
@telemetry_tool("get_session_info")
def get_session_info(ctx: Context, user_prompt: str = "") -> str:
    """Get detailed information about the current Ableton session

    Parameters:
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_session_info")
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting session info from Ableton: {str(e)}")
        return f"Error getting session info: {str(e)}"

@mcp.tool()
@telemetry_tool("get_track_info")
def get_track_info(ctx: Context, track_index: int, user_prompt: str = "") -> str:
    """
    Get detailed information about a specific track in Ableton.

    Parameters:
    - track_index: The index of the track to get information about
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_track_info", {"track_index": track_index})
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting track info from Ableton: {str(e)}")
        return f"Error getting track info: {str(e)}"

@mcp.tool()
@telemetry_tool("create_midi_track")
def create_midi_track(ctx: Context, index: int = -1, user_prompt: str = "") -> str:
    """
    Create a new MIDI track in the Ableton session.

    Parameters:
    - index: The index to insert the track at (-1 = end of list)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("create_midi_track", {"index": index})
        return f"Created new MIDI track: {result.get('name', 'unknown')}"
    except Exception as e:
        logger.error(f"Error creating MIDI track: {str(e)}")
        return f"Error creating MIDI track: {str(e)}"


@mcp.tool()
@telemetry_tool("delete_track")
def delete_track(ctx: Context, track_index: int, user_prompt: str = "") -> str:
    """
    Delete a track from the Ableton session, including all its clips and devices.

    This is destructive and cannot be undone from the MCP server - use Ableton's
    own undo (Cmd/Ctrl+Z) in the app if you need to recover the track.

    Parameters:
    - track_index: The index of the track to delete
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("delete_track", {"track_index": track_index})
        return f"Deleted track '{result.get('deleted_track_name', track_index)}'"
    except Exception as e:
        logger.error(f"Error deleting track: {str(e)}")
        return f"Error deleting track: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("set_track_name")
def set_track_name(ctx: Context, track_index: int, name: str, user_prompt: str = "") -> str:
    """
    Set the name of a track.

    Parameters:
    - track_index: The index of the track to rename
    - name: The new name for the track
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_track_name", {"track_index": track_index, "name": name})
        return f"Renamed track to: {result.get('name', name)}"
    except Exception as e:
        logger.error(f"Error setting track name: {str(e)}")
        return f"Error setting track name: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("create_clip")
def create_clip(ctx: Context, track_index: int, clip_index: int, length: float = 4.0, user_prompt: str = "") -> str:
    """
    Create a new MIDI clip in the specified track and clip slot.

    Parameters:
    - track_index: The index of the track to create the clip in
    - clip_index: The index of the clip slot to create the clip in
    - length: The length of the clip in beats (default: 4.0)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("create_clip", {
            "track_index": track_index, 
            "clip_index": clip_index, 
            "length": length
        })
        return f"Created new clip at track {track_index}, slot {clip_index} with length {length} beats"
    except Exception as e:
        logger.error(f"Error creating clip: {str(e)}")
        return f"Error creating clip: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("create_audio_clip")
def create_audio_clip(ctx: Context, track_index: int, clip_index: int, path: str, user_prompt: str = "") -> str:
    """
    Create a new audio clip in an audio track's clip slot by importing a file.

    Requires Ableton Live 12.0.5 or newer — the underlying
    ClipSlot.create_audio_clip Live API was introduced in 12.0.5 and is not
    available in earlier 12.0.x releases.

    Parameters:
    - track_index: The index of the audio track to create the clip in
    - clip_index: The index of the clip slot to create the clip in
    - path: Absolute path to a supported audio file (e.g. a .wav). The target
      track must be an audio track and the clip slot must be empty.
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("create_audio_clip", {
            "track_index": track_index,
            "clip_index": clip_index,
            "path": path
        })
        return f"Created audio clip '{result.get('name', 'clip')}' at track {track_index}, slot {clip_index} (length {result.get('length', '?')} beats)"
    except Exception as e:
        logger.error(f"Error creating audio clip: {str(e)}")
        return f"Error creating audio clip: {str(e)}"

@mcp.tool()
@telemetry_tool("get_clip_notes")
def get_clip_notes(ctx: Context, track_index: int, clip_index: int, user_prompt: str = "") -> str:
    """
    Read all MIDI notes out of a clip.

    Returns each note's pitch, start_time, duration, velocity, and mute state,
    along with the clip's name and length. Useful for inspecting what's already
    in a clip before editing it, or for reading back what add_notes_to_clip wrote.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot containing the clip
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_clip_notes", {
            "track_index": track_index,
            "clip_index": clip_index
        })
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting clip notes: {str(e)}")
        return f"Error getting clip notes: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("add_notes_to_clip", capture_notes=True)
def add_notes_to_clip(
    ctx: Context,
    track_index: int,
    clip_index: int,
    notes: List[Dict[str, Union[int, float, bool]]],
    snap_to_scale: bool = False,
    key: Optional[Dict[str, str]] = None,
    user_prompt: str = ""
) -> str:
    """
    Add MIDI notes to a clip.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot containing the clip
    - notes: List of note dictionaries, each with pitch, start_time, duration, velocity, and mute
    - snap_to_scale: If True, snap each note's pitch to the nearest pitch in `key`
      (or the project key set via set_project_key, if `key` is omitted)
    - key: Optional {"tonic": ..., "mode": ...} override for snapping. Defaults to the project key.
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        snapped = []
        if snap_to_scale:
            tonic = (key or {}).get("tonic") or PROJECT.get("tonic")
            mode = (key or {}).get("mode") or PROJECT.get("mode")
            if not tonic or not mode:
                raise ValueError(
                    "snap_to_scale requires a key — pass `key` or call set_project_key first"
                )
            music_theory.validate_tonic_mode(tonic, mode)

            for note in notes:
                original_pitch = note["pitch"]
                snapped_pitch = music_theory.snap_to_scale(original_pitch, tonic, mode)
                if snapped_pitch != original_pitch:
                    snapped.append({"from": original_pitch, "to": snapped_pitch})
                    note["pitch"] = snapped_pitch

        ableton = get_ableton_connection()
        ableton.send_command("add_notes_to_clip", {
            "track_index": track_index,
            "clip_index": clip_index,
            "notes": notes
        })

        message = f"Added {len(notes)} notes to clip at track {track_index}, slot {clip_index}"
        if snapped:
            message += f". Snapped {len(snapped)} note(s) to scale: {snapped}"
        return message
    except Exception as e:
        logger.error(f"Error adding notes to clip: {str(e)}")
        return f"Error adding notes to clip: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("set_project_key")
def set_project_key(
    ctx: Context,
    tonic: str,
    mode: str,
    tempo: Optional[float] = None,
    genre: Optional[str] = None,
    user_prompt: str = ""
) -> str:
    """
    Set the project's tonal center (key/mode), and optionally tempo and genre.

    This is the foundation for every composition tool: chord/bass/drum generators
    and snap_to_scale all read this state. Does not touch Live unless tempo is given.

    Parameters:
    - tonic: Root note, e.g. "Bb", "C#", "F" (see music_theory.NOTE_NAMES)
    - mode: Scale/mode name, e.g. "major", "minor", "dorian", "phrygian_dominant"
    - tempo: If given, also sets the Live project tempo
    - genre: Optional genre tag (e.g. "dnb_liquid"), used by generators for style defaults
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        music_theory.validate_tonic_mode(tonic, mode)

        if genre is not None and genre not in music_theory.GENRES:
            raise ValueError(f"Unknown genre '{genre}'. Expected one of {sorted(music_theory.GENRES)}")

        PROJECT["tonic"] = tonic
        PROJECT["mode"] = mode
        if tempo is not None:
            PROJECT["tempo"] = tempo
        if genre is not None:
            PROJECT["genre"] = genre
        _save_project_state(PROJECT)

        if tempo is not None:
            ableton = get_ableton_connection()
            ableton.send_command("set_tempo", {"tempo": tempo})

        result = dict(PROJECT)
        result["scale_pitch_classes"] = sorted(music_theory.scale_pitch_classes(tonic, mode))
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error setting project key: {str(e)}")
        return f"Error setting project key: {str(e)}"

@mcp.tool()
@telemetry_tool("get_project_key")
def get_project_key(ctx: Context, user_prompt: str = "") -> str:
    """
    Get the project's current tonal center (key/mode), tempo, and genre,
    as previously set with set_project_key.

    Parameters:
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        result = dict(PROJECT)
        if PROJECT.get("tonic") and PROJECT.get("mode"):
            result["scale_pitch_classes"] = sorted(
                music_theory.scale_pitch_classes(PROJECT["tonic"], PROJECT["mode"])
            )
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting project key: {str(e)}")
        return f"Error getting project key: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("set_clip_name")
def set_clip_name(ctx: Context, track_index: int, clip_index: int, name: str, user_prompt: str = "") -> str:
    """
    Set the name of a clip.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot containing the clip
    - name: The new name for the clip
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_clip_name", {
            "track_index": track_index,
            "clip_index": clip_index,
            "name": name
        })
        return f"Renamed clip at track {track_index}, slot {clip_index} to '{name}'"
    except Exception as e:
        logger.error(f"Error setting clip name: {str(e)}")
        return f"Error setting clip name: {str(e)}"

@mcp.tool()
@telemetry_tool("delete_clip")
def delete_clip(ctx: Context, track_index: int, clip_index: int, user_prompt: str = "") -> str:
    """
    Delete the clip in a track's clip slot.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot containing the clip
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("delete_clip", {
            "track_index": track_index,
            "clip_index": clip_index
        })
        return f"Deleted clip '{result.get('deleted_clip_name', clip_index)}' at track {track_index}, slot {clip_index}"
    except Exception as e:
        logger.error(f"Error deleting clip: {str(e)}")
        return f"Error deleting clip: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("set_tempo")
def set_tempo(ctx: Context, tempo: float, user_prompt: str = "") -> str:
    """
    Set the tempo of the Ableton session.

    Parameters:
    - tempo: The new tempo in BPM
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_tempo", {"tempo": tempo})
        return f"Set tempo to {tempo} BPM"
    except Exception as e:
        logger.error(f"Error setting tempo: {str(e)}")
        return f"Error setting tempo: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("load_instrument_or_effect")
def load_instrument_or_effect(ctx: Context, track_index: int, uri: str, user_prompt: str = "") -> str:
    """
    Load an instrument or effect onto a track using its URI.

    Parameters:
    - track_index: The index of the track to load the instrument on
    - uri: The URI of the instrument or effect to load (e.g., 'query:Synths#Instrument%20Rack:Bass:FileId_5116')
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("load_browser_item", {
            "track_index": track_index,
            "item_uri": uri
        })
        
        # Check if the instrument was loaded successfully
        if result.get("loaded", False):
            new_devices = result.get("new_devices", [])
            if new_devices:
                return f"Loaded instrument with URI '{uri}' on track {track_index}. New devices: {', '.join(new_devices)}"
            else:
                devices = result.get("devices_after", [])
                return f"Loaded instrument with URI '{uri}' on track {track_index}. Devices on track: {', '.join(devices)}"
        else:
            return f"Failed to load instrument with URI '{uri}'"
    except Exception as e:
        logger.error(f"Error loading instrument by URI: {str(e)}")
        return f"Error loading instrument by URI: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("get_device_parameters")
def get_device_parameters(
    ctx: Context,
    track_index: int,
    device_index: int,
    name_filter: str = None,
    user_prompt: str = ""
) -> str:
    """
    List the parameters of a device (instrument or effect) on a track, with their
    current values and valid ranges.

    Works for any device, including sound-design synths like Wavetable, Analog,
    Operator, Drift, and Sampler - their oscillators, filters, envelopes, and LFOs
    are all exposed as named parameters here. These devices can have 50-100+
    parameters, so pass name_filter (a case-insensitive substring, e.g. "Filter",
    "Osc 1", "Env") to narrow the list to the section you're designing.

    Parameters:
    - track_index: The index of the track the device is on
    - device_index: The index of the device on the track (see get_track_info for the device list)
    - name_filter: Optional case-insensitive substring to filter parameter names by
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_device_parameters", {
            "track_index": track_index,
            "device_index": device_index,
            "name_filter": name_filter
        })
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting device parameters: {str(e)}")
        return f"Error getting device parameters: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("set_device_parameter")
def set_device_parameter(
    ctx: Context,
    track_index: int,
    device_index: int,
    value: float,
    parameter_name: str = None,
    parameter_index: int = None,
    user_prompt: str = ""
) -> str:
    """
    Set the value of a parameter on a device (instrument or effect), e.g. filter
    cutoff, resonance, attack/decay/sustain/release, mix amount, etc.

    Identify the parameter with either parameter_name or parameter_index (use
    get_device_parameters first to discover the available names, indices, and
    valid min/max range for the target device).

    Parameters:
    - track_index: The index of the track the device is on
    - device_index: The index of the device on the track
    - value: The new value to set, must be within the parameter's min/max range
    - parameter_name: The name of the parameter to set (e.g. "Cutoff", "Resonance")
    - parameter_index: The index of the parameter to set, alternative to parameter_name
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_device_parameter", {
            "track_index": track_index,
            "device_index": device_index,
            "parameter_name": parameter_name,
            "parameter_index": parameter_index,
            "value": value
        })
        return f"Set '{result.get('parameter_name', parameter_name)}' on '{result.get('device_name', 'device')}' to {result.get('value', value)}"
    except Exception as e:
        logger.error(f"Error setting device parameter: {str(e)}")
        return f"Error setting device parameter: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("set_multiple_device_parameters")
def set_multiple_device_parameters(
    ctx: Context,
    track_index: int,
    device_index: int,
    parameters: List[Dict[str, Union[str, int, float]]],
    user_prompt: str = ""
) -> str:
    """
    Set several parameters on a device in one call - useful for designing a sound
    on a synth like Wavetable, Analog, or Operator where you typically want to move
    several controls together (e.g. oscillator wave + filter cutoff + envelope decay).

    Use get_device_parameters first to find each parameter's name/index and valid
    min/max range. Each entry is applied independently: a bad value in one entry
    is reported in the response but does not block the others from being applied.

    Parameters:
    - track_index: The index of the track the device is on
    - device_index: The index of the device on the track
    - parameters: List of dicts, each with "value" and either "parameter_name" or
      "parameter_index", e.g. [{"parameter_name": "Cutoff", "value": 0.4},
      {"parameter_name": "Resonance", "value": 0.2}]
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_multiple_device_parameters", {
            "track_index": track_index,
            "device_index": device_index,
            "parameters": parameters
        })
        applied = result.get("applied", [])
        errors = result.get("errors", [])
        summary = f"Set {len(applied)} parameter(s) on '{result.get('device_name', 'device')}'"
        if errors:
            error_list = ", ".join(f"{e.get('parameter_name') or e.get('parameter_index')}: {e.get('error')}" for e in errors)
            summary += f". {len(errors)} failed: {error_list}"
        return summary
    except Exception as e:
        logger.error(f"Error setting multiple device parameters: {str(e)}")
        return f"Error setting multiple device parameters: {str(e)}"

@mcp.tool()
@telemetry_tool("delete_device")
def delete_device(ctx: Context, track_index: int, device_index: int, user_prompt: str = "") -> str:
    """
    Delete a device (instrument or effect) from a track.

    Parameters:
    - track_index: The index of the track the device is on
    - device_index: The index of the device on the track
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("delete_device", {
            "track_index": track_index,
            "device_index": device_index
        })
        return f"Deleted device '{result.get('deleted_device_name', device_index)}' from track {track_index}"
    except Exception as e:
        logger.error(f"Error deleting device: {str(e)}")
        return f"Error deleting device: {str(e)}"

@mcp.tool()
@telemetry_tool("fire_clip")
def fire_clip(ctx: Context, track_index: int, clip_index: int, user_prompt: str = "") -> str:
    """
    Start playing a clip.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot containing the clip
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("fire_clip", {
            "track_index": track_index,
            "clip_index": clip_index
        })
        return f"Started playing clip at track {track_index}, slot {clip_index}"
    except Exception as e:
        logger.error(f"Error firing clip: {str(e)}")
        return f"Error firing clip: {str(e)}"

@mcp.tool()
@telemetry_tool("stop_clip")
def stop_clip(ctx: Context, track_index: int, clip_index: int, user_prompt: str = "") -> str:
    """
    Stop playing a clip.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot containing the clip
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("stop_clip", {
            "track_index": track_index,
            "clip_index": clip_index
        })
        return f"Stopped clip at track {track_index}, slot {clip_index}"
    except Exception as e:
        logger.error(f"Error stopping clip: {str(e)}")
        return f"Error stopping clip: {str(e)}"

@mcp.tool()
@telemetry_tool("start_playback")
def start_playback(ctx: Context, user_prompt: str = "") -> str:
    """Start playing the Ableton session.

    Parameters:
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("start_playback")
        return "Started playback"
    except Exception as e:
        logger.error(f"Error starting playback: {str(e)}")
        return f"Error starting playback: {str(e)}"

@mcp.tool()
@telemetry_tool("stop_playback")
def stop_playback(ctx: Context, user_prompt: str = "") -> str:
    """Stop playing the Ableton session.

    Parameters:
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("stop_playback")
        return "Stopped playback"
    except Exception as e:
        logger.error(f"Error stopping playback: {str(e)}")
        return f"Error stopping playback: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("get_browser_tree")
def get_browser_tree(ctx: Context, category_type: str = "all", user_prompt: str = "") -> str:
    """
    Get a hierarchical tree of browser categories from Ableton.

    Parameters:
    - category_type: Type of categories to get ('all', 'instruments', 'sounds', 'drums', 'audio_effects', 'midi_effects')
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_browser_tree", {
            "category_type": category_type
        })
        
        # Check if we got any categories
        if "available_categories" in result and len(result.get("categories", [])) == 0:
            available_cats = result.get("available_categories", [])
            return (f"No categories found for '{category_type}'. "
                   f"Available browser categories: {', '.join(available_cats)}")
        
        # Format the tree in a more readable way
        total_folders = result.get("total_folders", 0)
        formatted_output = f"Browser tree for '{category_type}' (showing {total_folders} folders):\n\n"
        
        def format_tree(item, indent=0):
            output = ""
            if item:
                prefix = "  " * indent
                name = item.get("name", "Unknown")
                path = item.get("path", "")
                has_more = item.get("has_more", False)
                
                # Add this item
                output += f"{prefix}• {name}"
                if path:
                    output += f" (path: {path})"
                if has_more:
                    output += " [...]"
                output += "\n"
                
                # Add children
                for child in item.get("children", []):
                    output += format_tree(child, indent + 1)
            return output
        
        # Format each category
        for category in result.get("categories", []):
            formatted_output += format_tree(category)
            formatted_output += "\n"
        
        return formatted_output
    except Exception as e:
        error_msg = str(e)
        if "Browser is not available" in error_msg:
            logger.error(f"Browser is not available in Ableton: {error_msg}")
            return f"Error: The Ableton browser is not available. Make sure Ableton Live is fully loaded and try again."
        elif "Could not access Live application" in error_msg:
            logger.error(f"Could not access Live application: {error_msg}")
            return f"Error: Could not access the Ableton Live application. Make sure Ableton Live is running and the Remote Script is loaded."
        else:
            logger.error(f"Error getting browser tree: {error_msg}")
            return f"Error getting browser tree: {error_msg}"

@mcp.tool()
@rich_telemetry_tool("get_browser_items_at_path")
def get_browser_items_at_path(ctx: Context, path: str, user_prompt: str = "") -> str:
    """
    Get browser items at a specific path in Ableton's browser.

    Parameters:
    - path: Path in the format "category/folder/subfolder"
            where category is one of the available browser categories in Ableton
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_browser_items_at_path", {
            "path": path
        })
        
        # Check if there was an error with available categories
        if "error" in result and "available_categories" in result:
            error = result.get("error", "")
            available_cats = result.get("available_categories", [])
            return (f"Error: {error}\n"
                   f"Available browser categories: {', '.join(available_cats)}")
        
        return json.dumps(result, indent=2)
    except Exception as e:
        error_msg = str(e)
        if "Browser is not available" in error_msg:
            logger.error(f"Browser is not available in Ableton: {error_msg}")
            return f"Error: The Ableton browser is not available. Make sure Ableton Live is fully loaded and try again."
        elif "Could not access Live application" in error_msg:
            logger.error(f"Could not access Live application: {error_msg}")
            return f"Error: Could not access the Ableton Live application. Make sure Ableton Live is running and the Remote Script is loaded."
        elif "Unknown or unavailable category" in error_msg:
            logger.error(f"Invalid browser category: {error_msg}")
            return f"Error: {error_msg}. Please check the available categories using get_browser_tree."
        elif "Path part" in error_msg and "not found" in error_msg:
            logger.error(f"Path not found: {error_msg}")
            return f"Error: {error_msg}. Please check the path and try again."
        else:
            logger.error(f"Error getting browser items at path: {error_msg}")
            return f"Error getting browser items at path: {error_msg}"

@mcp.tool()
@rich_telemetry_tool("load_drum_kit")
def load_drum_kit(ctx: Context, track_index: int, rack_uri: str, kit_path: str, user_prompt: str = "") -> str:
    """
    Load a drum rack and then load a specific drum kit into it.

    Parameters:
    - track_index: The index of the track to load on
    - rack_uri: The URI of the drum rack to load (e.g., 'Drums/Drum Rack')
    - kit_path: Path to the drum kit inside the browser (e.g., 'drums/acoustic/kit1')
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        
        # Step 1: Load the drum rack
        result = ableton.send_command("load_browser_item", {
            "track_index": track_index,
            "item_uri": rack_uri
        })
        
        if not result.get("loaded", False):
            return f"Failed to load drum rack with URI '{rack_uri}'"
        
        # Step 2: Get the drum kit items at the specified path
        kit_result = ableton.send_command("get_browser_items_at_path", {
            "path": kit_path
        })
        
        if "error" in kit_result:
            return f"Loaded drum rack but failed to find drum kit: {kit_result.get('error')}"
        
        # Step 3: Find a loadable drum kit
        kit_items = kit_result.get("items", [])
        loadable_kits = [item for item in kit_items if item.get("is_loadable", False)]
        
        if not loadable_kits:
            return f"Loaded drum rack but no loadable drum kits found at '{kit_path}'"
        
        # Step 4: Load the first loadable kit
        kit_uri = loadable_kits[0].get("uri")
        load_result = ableton.send_command("load_browser_item", {
            "track_index": track_index,
            "item_uri": kit_uri
        })
        
        return f"Loaded drum rack and kit '{loadable_kits[0].get('name')}' on track {track_index}"
    except Exception as e:
        logger.error(f"Error loading drum kit: {str(e)}")
        return f"Error loading drum kit: {str(e)}"

# ── Arrangement view tools ────────────────────────────────────────────────────

@mcp.tool()
@telemetry_tool("switch_to_arrangement_view")
def switch_to_arrangement_view(ctx: Context, user_prompt: str = "") -> str:
    """Switch Ableton's main window to the Arrangement view.

    Parameters:
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        ableton.send_command("switch_to_arrangement_view")
        return "Switched to Arrangement view"
    except Exception as e:
        logger.error(f"Error switching to arrangement view: {str(e)}")
        return f"Error switching to arrangement view: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("set_arrangement_time")
def set_arrangement_time(ctx: Context, time: float, user_prompt: str = "") -> str:
    """
    Move the arrangement playhead to a specific position.

    Parameters:
    - time: Position in beats from the start of the arrangement (e.g. 8.0 = bar 3 in 4/4)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_current_song_time", {"time": time})
        return f"Playhead moved to beat {result.get('current_song_time', time)}"
    except Exception as e:
        logger.error(f"Error setting arrangement time: {str(e)}")
        return f"Error setting arrangement time: {str(e)}"


@mcp.tool()
@telemetry_tool("get_arrangement_clips")
def get_arrangement_clips(ctx: Context, track_index: int, user_prompt: str = "") -> str:
    """
    List all clips placed in the Arrangement timeline for a track.

    Returns each clip's index, name, start_time, end_time, length, and type.
    The index can be passed to get_arrangement_clip_notes to read a MIDI clip's notes.

    Parameters:
    - track_index: The index of the track to inspect
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_arrangement_clips", {"track_index": track_index})
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting arrangement clips: {str(e)}")
        return f"Error getting arrangement clips: {str(e)}"

@mcp.tool()
@telemetry_tool("get_arrangement_clip_notes")
def get_arrangement_clip_notes(ctx: Context, track_index: int, clip_index: int, user_prompt: str = "") -> str:
    """
    Read all MIDI notes out of a clip placed in the Arrangement timeline.

    Returns each note's pitch, start_time, duration, velocity, and mute state,
    along with the clip's name and length. Use get_arrangement_clips first to
    find the clip's index.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip within track.arrangement_clips, as
      returned by get_arrangement_clips
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_arrangement_clip_notes", {
            "track_index": track_index,
            "clip_index": clip_index
        })
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting arrangement clip notes: {str(e)}")
        return f"Error getting arrangement clip notes: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("duplicate_to_arrangement")
def duplicate_to_arrangement(
    ctx: Context,
    track_index: int,
    clip_index: int,
    destination_time: float,
    user_prompt: str = ""
) -> str:
    """
    Copy a Session-view clip into the Arrangement timeline.

    Uses Live's track.duplicate_clip_to_arrangement() API (Live 11 / 12).
    The clip is placed at destination_time beats from the start of the
    arrangement on the same track it lives in.

    Typical workflow:
      1. create_clip / add_notes_to_clip to build a Session clip
      2. Call duplicate_to_arrangement once per bar/section you need
      3. Call switch_to_arrangement_view to confirm the result in Live

    Parameters:
    - track_index:       Index of the track that owns the Session clip
    - clip_index:        Index of the clip slot in that track (Session view)
    - destination_time:  Beat position in the arrangement to place the clip
                         (e.g. 0.0 = start, 8.0 = bar 3 in 4/4)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command(
            "duplicate_session_clip_to_arrangement",
            {
                "track_index": track_index,
                "clip_index": clip_index,
                "destination_time": destination_time
            }
        )
        clip_name = result.get("clip_name", "clip")
        track_name = result.get("track_name", f"track {track_index}")
        return (
            f"Duplicated '{clip_name}' from Session slot {clip_index} "
            f"on '{track_name}' to arrangement at beat {destination_time}"
        )
    except Exception as e:
        logger.error(f"Error duplicating clip to arrangement: {str(e)}")
        return f"Error duplicating clip to arrangement: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("create_arrangement_midi_clip")
def create_arrangement_midi_clip(
    ctx: Context,
    track_index: int,
    start_time: float,
    length: float = 4.0,
    user_prompt: str = ""
) -> str:
    """
    Create a brand-new empty MIDI clip directly in the Arrangement timeline,
    without going through a Session clip slot first.

    Parameters:
    - track_index: Index of the MIDI track to create the clip on
    - start_time: Beat position in the arrangement where the clip starts
    - length: Length of the clip in beats (default 4.0)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("create_arrangement_midi_clip", {
            "track_index": track_index,
            "start_time": start_time,
            "length": length
        })
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error creating arrangement MIDI clip: {str(e)}")
        return f"Error creating arrangement MIDI clip: {str(e)}"

@mcp.tool()
@telemetry_tool("get_locators")
def get_locators(ctx: Context, user_prompt: str = "") -> str:
    """
    List all arrangement locators (markers/cue points) with their name and
    beat position. Use this to find a named marker (e.g. "Drop") before
    navigating or placing clips relative to it.

    Parameters:
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_locators")
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting locators: {str(e)}")
        return f"Error getting locators: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("set_locator")
def set_locator(ctx: Context, time: float, name: str, user_prompt: str = "") -> str:
    """
    Create (or rename, if one already exists at that time) an arrangement
    locator/marker at a beat position.

    Parameters:
    - time: Beat position in the arrangement (beats from the start)
    - name: Name for the marker, e.g. "Drop", "Intro", "Break"
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_locator", {"time": time, "name": name})
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error setting locator: {str(e)}")
        return f"Error setting locator: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("create_section_markers")
def create_section_markers(
    ctx: Context,
    sections: List[Dict[str, Union[str, int]]],
    user_prompt: str = ""
) -> str:
    """
    Create a set of arrangement locators from a song structure, given in bars.

    Converts each section's bar number to a beat position using the project's
    time signature, then places a named locator there via set_locator.

    Parameters:
    - sections: List of {"name": str, "bar": int} dicts, e.g.
      [{"name": "intro", "bar": 1}, {"name": "drop", "bar": 17}].
      Bar 1 is the start of the arrangement (beat 0).
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        session_info = ableton.send_command("get_session_info")
        beats_per_bar = session_info.get("signature_numerator", 4)

        created = []
        for section in sections:
            name = section["name"]
            bar = section["bar"]
            time = (bar - 1) * beats_per_bar
            result = ableton.send_command("set_locator", {"time": time, "name": name})
            created.append({"name": result.get("name", name), "time": result.get("time", time)})

        return json.dumps({"markers_created": len(created), "markers": created}, indent=2)
    except Exception as e:
        logger.error(f"Error creating section markers: {str(e)}")
        return f"Error creating section markers: {str(e)}"

@mcp.tool()
@telemetry_tool("get_track_hierarchy")
def get_track_hierarchy(ctx: Context, user_prompt: str = "") -> str:
    """
    List all tracks with their group/folder membership, so group tracks and
    the tracks nested inside them can be identified without guessing indices.

    Returns each track's index, name, color, whether it's a group (folder)
    track, and the index of the group track it belongs to (if any).

    Parameters:
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_track_hierarchy")
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting track hierarchy: {str(e)}")
        return f"Error getting track hierarchy: {str(e)}"

@mcp.tool()
@telemetry_tool("get_selected")
def get_selected(ctx: Context, user_prompt: str = "") -> str:
    """
    Get the track, scene, and device currently selected in the Live UI.

    Parameters:
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_selected")
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting selection: {str(e)}")
        return f"Error getting selection: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("set_track_color")
def set_track_color(ctx: Context, track_index: int, color: int, user_prompt: str = "") -> str:
    """
    Set a track's color.

    Parameters:
    - track_index: The index of the track to recolor
    - color: 24-bit RGB integer (e.g. 0xFF0000 for red, 16711680 decimal)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_track_color", {
            "track_index": track_index,
            "color": color
        })
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error setting track color: {str(e)}")
        return f"Error setting track color: {str(e)}"


# ── Composition generators (M3) ─────────────────────────────────────────────

def _resolve_key(key: Optional[Dict[str, str]]) -> (str, str):
    """Resolve a {tonic, mode} dict, falling back to the project key. Raises if neither is set."""
    tonic = (key or {}).get("tonic") or PROJECT.get("tonic")
    mode = (key or {}).get("mode") or PROJECT.get("mode")
    if not tonic or not mode:
        raise ValueError("No key given and no project key set — pass `key` or call set_project_key first")
    music_theory.validate_tonic_mode(tonic, mode)
    return tonic, mode


def _prepare_destination_clip(
    ableton,
    track_index: int,
    total_beats: float,
    clip_index: Optional[int],
    arrangement_start: Optional[float]
) -> Dict[str, Any]:
    """Resolve (and create if needed) the clip notes should be written into.

    Exactly one of clip_index (Session slot) or arrangement_start (Arrangement
    beat position) must be given. Returns {"mode": "session"|"arrangement", "clip_index": int}
    where clip_index is always relative to the destination's own indexing
    (clip_slots for session, arrangement_clips for arrangement).
    """
    if (clip_index is None) == (arrangement_start is None):
        raise ValueError("Pass exactly one of clip_index or arrangement_start")

    if clip_index is not None:
        track_info = ableton.send_command("get_track_info", {"track_index": track_index})
        slot = track_info["clip_slots"][clip_index]
        if not slot["has_clip"]:
            ableton.send_command("create_clip", {
                "track_index": track_index,
                "clip_index": clip_index,
                "length": total_beats
            })
        return {"mode": "session", "clip_index": clip_index}

    ableton.send_command("create_arrangement_midi_clip", {
        "track_index": track_index,
        "start_time": arrangement_start,
        "length": total_beats
    })
    clips = ableton.send_command("get_arrangement_clips", {"track_index": track_index})["clips"]
    for clip in clips:
        if abs(clip["start_time"] - arrangement_start) < 1e-6:
            return {"mode": "arrangement", "clip_index": clip["index"]}
    raise Exception("Could not locate newly created arrangement clip")


def _write_notes(ableton, track_index: int, destination: Dict[str, Any], notes: List[Dict[str, Any]]) -> None:
    command = "add_notes_to_clip" if destination["mode"] == "session" else "add_notes_to_arrangement_clip"
    ableton.send_command(command, {
        "track_index": track_index,
        "clip_index": destination["clip_index"],
        "notes": notes
    })


@mcp.tool()
@rich_telemetry_tool("generate_chord_progression")
def generate_chord_progression(
    ctx: Context,
    track_index: int,
    bars: int,
    clip_index: Optional[int] = None,
    arrangement_start: Optional[float] = None,
    progression: Optional[List[int]] = None,
    chord_size: int = 4,
    octave: int = 4,
    voicing: str = "open",
    key: Optional[Dict[str, str]] = None,
    rhythm: str = "sustained",
    user_prompt: str = ""
) -> str:
    """
    Write a diatonic chord progression as MIDI notes into a clip.

    Parameters:
    - track_index: Index of the MIDI track to write to
    - bars: Length of the progression in bars
    - clip_index: Session clip slot to write into (creates the clip if empty).
      Pass exactly one of clip_index / arrangement_start.
    - arrangement_start: Beat position in the Arrangement to create the clip at.
    - progression: Scale degrees to use, e.g. [1, 6, 4, 5]. If omitted, picks an
      idiomatic progression for the project's mode.
    - chord_size: 3 = triad, 4 = seventh chord, 5 = ninth, etc.
    - octave: Register for the chord roots (Ableton convention, C3 == MIDI 60)
    - voicing: "close", "open", or "drop2"
    - key: Optional {"tonic": ..., "mode": ...} override. Defaults to the project key.
    - rhythm: "sustained" (long chords), "stab" (short hits on downbeats), or
      "liquid" (sustained with slight overlap into the next chord)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        tonic, mode = _resolve_key(key)

        chosen_progression = progression or music_theory.DEFAULT_PROGRESSIONS.get(
            mode, music_theory.DEFAULT_PROGRESSION_FALLBACK
        )

        ableton = get_ableton_connection()
        session_info = ableton.send_command("get_session_info")
        beats_per_bar = session_info.get("signature_numerator", 4)
        total_beats = bars * beats_per_bar
        chord_dur = total_beats / len(chosen_progression)

        if rhythm == "stab":
            note_dur = min(0.5, chord_dur)
        elif rhythm == "liquid":
            note_dur = chord_dur * 1.1
        else:  # sustained
            note_dur = chord_dur

        notes = []
        chords = []
        for i, degree in enumerate(chosen_progression):
            raw_pitches = music_theory.diatonic_chord(tonic, mode, degree, octave, size=chord_size)
            voiced_pitches = music_theory.voice_chord(raw_pitches, voicing=voicing)
            start = i * chord_dur
            for pitch in voiced_pitches:
                notes.append({
                    "pitch": pitch,
                    "start_time": start,
                    "duration": note_dur,
                    "velocity": 90,
                    "mute": False
                })
            chords.append({"degree": degree, "pitches": voiced_pitches, "start": start, "duration": chord_dur})

        destination = _prepare_destination_clip(ableton, track_index, total_beats, clip_index, arrangement_start)
        _write_notes(ableton, track_index, destination, notes)

        return json.dumps({
            "notes_written": len(notes),
            "key": {"tonic": tonic, "mode": mode},
            "progression": chosen_progression,
            "chords": chords
        }, indent=2)
    except Exception as e:
        logger.error(f"Error generating chord progression: {str(e)}")
        return f"Error generating chord progression: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("generate_drum_pattern")
def generate_drum_pattern(
    ctx: Context,
    track_index: int,
    bars: int,
    clip_index: Optional[int] = None,
    arrangement_start: Optional[float] = None,
    genre: Optional[str] = None,
    pattern: Optional[Dict[str, Any]] = None,
    swing: float = 0.0,
    humanize: float = 0.0,
    user_prompt: str = ""
) -> str:
    """
    Write a genre-style drum pattern as MIDI notes into a clip on a drum rack track.

    Parameters:
    - track_index: Index of the MIDI track (drum rack) to write to
    - bars: Number of bars to repeat the pattern for
    - clip_index: Session clip slot to write into (creates the clip if empty).
      Pass exactly one of clip_index / arrangement_start.
    - arrangement_start: Beat position in the Arrangement to create the clip at.
    - genre: Genre key from music_theory.GENRES (e.g. "dnb_liquid", "house", "trap").
      Defaults to the project genre set via set_project_key.
    - pattern: Optional override, e.g. {"kick": [0, 2], "snare": [1, 3]} (beat
      offsets within a bar) or {"hat": "rolls"} for continuous 8th-note rolls.
      Merged on top of the genre's default pattern if both are given.
    - swing: 0..1, delays off-beat 8th notes for a swung feel
    - humanize: 0..1, randomizes note timing and velocity slightly
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        genre_key = genre or PROJECT.get("genre")
        genre_def = music_theory.GENRES.get(genre_key, {}) if genre_key else {}

        pattern_def = dict(genre_def.get("drums", {}))
        if pattern:
            pattern_def.update(pattern)
        if not pattern_def:
            raise ValueError("No drum pattern available — pass `pattern` or a known `genre`")

        ableton = get_ableton_connection()
        session_info = ableton.send_command("get_session_info")
        beats_per_bar = session_info.get("signature_numerator", 4)
        total_beats = bars * beats_per_bar

        notes = []
        piece_counts = {}
        for piece, offsets in pattern_def.items():
            pitch = music_theory.DRUM_PITCHES.get(piece, 36)
            offset_list = [i * 0.5 for i in range(beats_per_bar * 2)] if offsets == "rolls" else offsets

            for bar in range(bars):
                bar_start = bar * beats_per_bar
                for offset in offset_list:
                    start_time = bar_start + offset
                    velocity = 100

                    if swing > 0 and (offset * 2) % 2 == 1:
                        start_time += swing * 0.25
                    if humanize > 0:
                        start_time += random.uniform(-humanize, humanize) * 0.05
                        velocity += int(random.uniform(-humanize, humanize) * 20)

                    notes.append({
                        "pitch": pitch,
                        "start_time": max(0.0, start_time),
                        "duration": 0.1,
                        "velocity": max(1, min(127, velocity)),
                        "mute": False
                    })
                    piece_counts[piece] = piece_counts.get(piece, 0) + 1

        destination = _prepare_destination_clip(ableton, track_index, total_beats, clip_index, arrangement_start)
        _write_notes(ableton, track_index, destination, notes)

        return json.dumps({
            "notes_written": len(notes),
            "pieces": piece_counts,
            "genre": genre_key
        }, indent=2)
    except Exception as e:
        logger.error(f"Error generating drum pattern: {str(e)}")
        return f"Error generating drum pattern: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("apply_sound_design_preset")
def apply_sound_design_preset(
    ctx: Context,
    track_index: int,
    device_index: int,
    preset: str,
    user_prompt: str = ""
) -> str:
    """
    Apply a starter sound design patch to a device by preset name.

    Parameters:
    - track_index: Index of the track holding the device
    - device_index: Index of the device on that track
    - preset: Preset name from music_theory.SOUND_PRESETS (e.g. "reese_sub", "warm_pad")
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        preset_def = music_theory.SOUND_PRESETS.get(preset)
        if preset_def is None:
            raise ValueError(f"Unknown preset '{preset}'. Expected one of {sorted(music_theory.SOUND_PRESETS)}")

        ableton = get_ableton_connection()
        device_info = ableton.send_command("get_device_parameters", {
            "track_index": track_index,
            "device_index": device_index
        })

        available_names = {p["name"] for p in device_info["parameters"]}

        to_apply = []
        skipped = []
        for name, value in preset_def["params"].items():
            if name in available_names:
                to_apply.append({"parameter_name": name, "value": value})
            else:
                skipped.append({"name": name, "reason": "parameter not found on device"})

        applied = []
        if to_apply:
            result = ableton.send_command("set_multiple_device_parameters", {
                "track_index": track_index,
                "device_index": device_index,
                "parameters": to_apply
            })
            applied = result.get("applied", to_apply)
            for failure in result.get("errors", []):
                skipped.append(failure)

        return json.dumps({
            "device_name": device_info.get("device_name"),
            "preset": preset,
            "applied": applied,
            "skipped": skipped
        }, indent=2)
    except Exception as e:
        logger.error(f"Error applying sound design preset: {str(e)}")
        return f"Error applying sound design preset: {str(e)}"


# Main execution
def main():
    """Run the MCP server"""
    mcp.run()

if __name__ == "__main__":
    main()