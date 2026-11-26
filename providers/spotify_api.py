import os
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from dotenv import load_dotenv

def get_spotify_client(base_dir):
    load_dotenv(os.path.join(base_dir, ".env"))
    
    client_id = os.getenv("SPOTIPY_CLIENT_ID")
    client_secret = os.getenv("SPOTIPY_CLIENT_SECRET")
    redirect_uri = os.getenv("SPOTIPY_REDIRECT_URI", "http://127.0.0.1:8888/callback")
    
    if not client_id or not client_secret:
        raise Exception("Spotify credentials not found in .env")
        
    cache_path = os.path.join(base_dir, ".spotify_cache")
    
    # Needs permissions to read and modify what is currently playing
    auth_manager = SpotifyOAuth(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        scope="user-modify-playback-state user-read-playback-state",
        cache_path=cache_path,
        open_browser=True
    )
    
    return spotipy.Spotify(auth_manager=auth_manager)

def spotify_play(base_dir, query=None):
    try:
        sp = get_spotify_client(base_dir)
        
        # Check if active device exists, if not this might fail
        devices = sp.devices()
        if not devices or not devices.get('devices'):
            return "Error: No active Spotify devices found. Please open Spotify on your PC or phone first."
            
        if query:
            results = sp.search(q=query, type='track', limit=1)
            tracks = results['tracks']['items']
            if not tracks:
                return f"No songs found for '{query}'"
            track_uri = tracks[0]['uri']
            track_name = tracks[0]['name']
            artist_name = tracks[0]['artists'][0]['name']
            sp.start_playback(uris=[track_uri])
            return f"Playing: {track_name} by {artist_name}"
        else:
            sp.start_playback()
            return "Resumed playback."
    except Exception as e:
        return f"Spotify error: {str(e)}"

def spotify_pause(base_dir):
    try:
        sp = get_spotify_client(base_dir)
        sp.pause_playback()
        return "Paused playback."
    except Exception as e:
        return f"Spotify error: {str(e)}"

def spotify_skip(base_dir):
    try:
        sp = get_spotify_client(base_dir)
        sp.next_track()
        return "Skipped to next track."
    except Exception as e:
        return f"Spotify error: {str(e)}"


def spotify_now_playing(base_dir):
    """What is playing, and where in the track.

    Without this VANTA could start music but not answer "what is this song",
    which is the question people actually ask.
    """
    try:
        sp = get_spotify_client(base_dir)
        state = sp.current_playback()
        if not state or not state.get("item"):
            return "Nothing is playing on Spotify right now."
        item = state["item"]
        artists = ", ".join(a["name"] for a in item.get("artists", []))
        position = state.get("progress_ms", 0) // 1000
        length = item.get("duration_ms", 0) // 1000
        status = "Playing" if state.get("is_playing") else "Paused"
        device = (state.get("device") or {}).get("name", "unknown device")
        return (f"{status}: '{item['name']}' by {artists} "
                f"({position // 60}:{position % 60:02d} of {length // 60}:{length % 60:02d}) "
                f"on {device}. Album: {item.get('album', {}).get('name', 'unknown')}.")
    except Exception as e:
        return f"Spotify error: {e}"


def spotify_search(base_dir, query, kind="track", limit=5):
    """Find something without starting it, so a choice can be offered."""
    if not str(query or "").strip():
        return "Error: a search query is required."
    try:
        sp = get_spotify_client(base_dir)
        kind = kind if kind in {"track", "album", "artist", "playlist"} else "track"
        results = sp.search(q=query, type=kind, limit=max(1, min(int(limit), 10)))
        items = results.get(f"{kind}s", {}).get("items", [])
        if not items:
            return f"No {kind} found for '{query}'."
        lines = [f"{kind.title()}s matching '{query}':"]
        for item in items:
            if kind == "track":
                artists = ", ".join(a["name"] for a in item.get("artists", []))
                lines.append(f"- '{item['name']}' by {artists}  [uri: {item['uri']}]")
            else:
                lines.append(f"- {item['name']}  [uri: {item['uri']}]")
        return "\n".join(lines)
    except Exception as e:
        return f"Spotify error: {e}"


def spotify_volume(base_dir, percent):
    try:
        level = max(0, min(100, int(float(percent))))
    except (TypeError, ValueError):
        return "Error: volume must be a number between 0 and 100."
    try:
        sp = get_spotify_client(base_dir)
        sp.volume(level)
        return f"Spotify volume set to {level}%."
    except Exception as e:
        return f"Spotify error: {e}"


def spotify_queue(base_dir, uri):
    """Queue a track instead of interrupting what is already playing."""
    if not str(uri or "").strip():
        return "Error: a track uri is required. Use SPOTIFY_SEARCH to find one."
    try:
        sp = get_spotify_client(base_dir)
        sp.add_to_queue(uri)
        return f"Queued {uri}."
    except Exception as e:
        return f"Spotify error: {e}"
