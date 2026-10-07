import os
import json
import subprocess
import requests
import threading
import uuid
import shutil
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from supabase import create_client
import random


load_dotenv()


FPS = 30
WIDTH = 1920
HEIGHT = 1080
CAPTION_WORDS_PER_LINE = 10
OUTRO_FRAMES = 150
OUTRO_VARIANTS = [
    "confetti", "hearts", "wave", "stickers", "actions", "namaste",
    "fireworks", "balloons", "rocket", "diya", "reactions", "sparkle",
]

# ------------------------------------------------------------
# OUTPUT SIZE CAP
# Hard limit is 500 MB. We aim a bit lower (default 450 MB) when
# re-encoding so the result reliably ends up under the limit.
# ------------------------------------------------------------
MAX_OUTPUT_MB = int(os.getenv("MAX_OUTPUT_MB", "500"))
TARGET_OUTPUT_MB = int(os.getenv("TARGET_OUTPUT_MB", "450"))
MAX_OUTPUT_BYTES = MAX_OUTPUT_MB * 1024 * 1024
TARGET_OUTPUT_BYTES = TARGET_OUTPUT_MB * 1024 * 1024
AUDIO_BITRATE_BPS = 128_000
MAX_SHRINK_ATTEMPTS = 3

RENDER_TMP_ROOT = os.getenv("RENDER_TMP_ROOT", "/tmp/storybit-render")
os.makedirs(RENDER_TMP_ROOT, exist_ok=True)
REMOTION_PROJECT_DIR = os.getenv("REMOTION_PROJECT_DIR")
REMOTION_ENTRY = os.path.join(REMOTION_PROJECT_DIR, "src", "index.ts")
REMOTION_COMPOSITION_ID = os.getenv("REMOTION_COMPOSITION_ID", "MainVideo")


supabase_url_env = os.getenv("SUPABASE_URL")
supabase_key_env = os.getenv("SUPABASE_KEY")
supabase = create_client(supabase_url_env, supabase_key_env)


def get_timeline(video_id: str):
    return (
        supabase
        .table("videos")
        .select("*")
        .eq("id", video_id)
        .execute()
        .data[0]["timeline"]
    )


TEMPLATE_COMPONENT_MAP = {
    "Architecture Diagram": "Architecture",
    "Archive Photo": "ArchivePhoto",
    "Bar Chart": "BarChart",
    "Before / After": "BeforeAfter",
    "Big Number": "BigNumber",
    "Word-Synced Captions": "Captions",
    "Case File": "CaseFile",
    "Chapter Card": "ChapterCard",
    "Chat Conversation": "ChatConversation",
    "Comparison Columns": "ComparisonColumns",
    "Decision Tree": "DecisionTree",
    "Document Highlight": "DocumentHighlight",
    "End Screen": "EndScreen",
    "Media + Floating Card": "FloatingCard",
    "Funnel": "Funnel",
    "Gauge / Meter": "Gauge",
    "Globe Zoom to Location": "GlobeZoom",
    "Hierarchy / Tree": "Hierarchy",
    "Icon Array": "IconArray",
    "Image + Label / Caption": "ImageCaption",
    "Image Grid": "ImageGrid",
    "Image Montage": "ImageMontage",
    "Investigation Board": "InvestigationBoard",
    "Key Statement": "KeyStatement",
    "Leaderboard": "Leaderboard",
    "Linear Process": "LinearProcess",
    "Line Chart": "LineChart",
    "Location Tag": "LocationTag",
    "Lower Third": "LowerThird",
    "Myth vs Fact": "MythFact",
    "News Headline Card": "NewsHeadline",
    "Newspaper Clipping": "NewspaperClipping",
    "Notification Pop": "NotificationPop",
    "Number Comparison": "NumberComparison",
    "Person Intro": "PersonIntro",
    "Person Intro Card": "PersonIntro",  # added: DB name differs from the original map key
    "Pie / Donut Chart": "PieDonut",
    "Profile Card": "ProfileCard",
    "Pros & Cons": "ProsCons",
    "Punch Word": "PunchWord",
    "Question Hook": "QuestionHook",
    "Quote Card": "QuoteCard",
    "Radius / Range": "RadiusRange",
    "Countdown Rank Reveal": "RankReveal",
    "A → B Relationship": "Relationship",
    "Roadmap": "Roadmap",
    "Scribble Annotation": "ScribbleAnnotation",
    "Search Bar Typing": "SearchBar",
    "Social Post": "SocialPost",
    "Source Citation": "SourceCitation",
    "Stacked Kinetic Text": "StackedText",
    "Statistic Overlay": "StatOverlay",
    "Sticky Notes Board": "StickyNotes",
    "Structured List": "StructuredList",
    "Subscribe Reminder": "SubscribeReminder",
    "Timeline": "Timeline",
    "Title Card": "TitleCard",
    "Title + Metadata": "TitleMetadata",
    "Travel Route Map": "TravelRoute",
    "VS Face-Off": "VsFaceOff",
    "Thank You Outro": "ThankYou",
}


def _run(cmd: list):
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise RuntimeError(
            "command failed:\n"
            + " ".join(cmd)
            + "\n--- stderr ---\n"
            + proc.stderr.decode("utf-8", errors="ignore")[-4000:]
        )
    return proc


def _download(url: str, dest_path: str) -> str:
    if os.path.exists(dest_path):
        return dest_path

    os.makedirs(os.path.dirname(dest_path), exist_ok=True)

    tmp_path = f"{dest_path}.{uuid.uuid4().hex}.part"

    try:
        with requests.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(tmp_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 16):
                    f.write(chunk)

        os.replace(tmp_path, dest_path)  # atomic rename
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass

    return dest_path


def _normalize_video(src_path: str, dest_path: str):
    """
    Convert downloaded B-roll into a browser/Remotion-friendly MP4.

    Output:
    - H.264
    - yuv420p
    - 1920x1080
    - 30 FPS
    - faststart
    - no audio

    Writes to a temp file and renames, so dest_path is never
    a partially written file.
    """

    os.makedirs(os.path.dirname(dest_path), exist_ok=True)

    tmp_dest = f"{dest_path}.{uuid.uuid4().hex}.tmp.mp4"

    try:
        _run([
            "ffmpeg",
            "-y",
            "-i", src_path,

            # "framerate" blends neighbouring frames instead of
            # duplicating them, avoiding periodic stutter when the
            # source isn't 30fps (e.g. 25fps footage).
            "-vf",
            (
                "scale=1920:1080:"
                "force_original_aspect_ratio=increase,"
                "crop=1920:1080,"
                "setsar=1,"
                "framerate=fps=30"
            ),

            # Browser-friendly video.
            "-c:v", "libx264",
            "-preset", "superfast",
            "-crf", "23",

            # Important for Chromium/Remotion compatibility.
            "-pix_fmt", "yuv420p",
            "-profile:v", "main",
            "-level:v", "4.1",

            # Put MP4 metadata at the beginning.
            "-movflags", "+faststart",

            # B-roll doesn't need its source audio.
            "-an",

            tmp_dest,
        ])

        os.replace(tmp_dest, dest_path)  # atomic rename
    finally:
        if os.path.exists(tmp_dest):
            try:
                os.remove(tmp_dest)
            except Exception:
                pass


def _video_duration_frames(path: str) -> int:
    try:
        proc = _run([
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            path,
        ])
        seconds = float(proc.stdout.decode().strip())
        # trim 2 frames (~0.07s) so the frozen last frame is never shown
        return max(1, int(seconds * FPS) - 2)
    except Exception as e:
        print(f"[warn] could not read video duration ({path}): {e}")
        return 0


def _video_duration_seconds(path: str) -> float:
    proc = _run([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        path,
    ])
    return float(proc.stdout.decode().strip())


def _ext_from_url(url: str, default: str) -> str:
    tail = url.split("?")[0]
    if "." in tail.rsplit("/", 1)[-1]:
        return tail.rsplit(".", 1)[-1]
    return default


def _safe_remove(path: str):
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except Exception:
            pass


def _try_video_asset(video: dict, asset_dir: str, video_id: str, label: str):
    """
    Download + normalize a video entry. Returns the asset dict,
    or None if it could not be used.
    """
    video_item_id = video.get("id")
    url = video.get("video_url")

    if not url:
        print(f"[warn] {label} video id={video_item_id} has no video_url")
        return None

    ext = _ext_from_url(url, "mp4")
    source_path = os.path.join(asset_dir, f"video_{video_item_id}_source.{ext}")
    final_filename = f"video_{video_item_id}.mp4"
    final_path = os.path.join(asset_dir, final_filename)

    try:
        _download(url, source_path)
        _normalize_video(source_path, final_path)

        if not os.path.exists(final_path):
            raise RuntimeError(f"Normalized video was not created: {final_path}")

        print(f"[video] {label}: id={video_item_id}")
        print(f"[video] query: {video.get('query', '')}")
        print(f"[video] normalized: {source_path} -> {final_path}")

        return {
            "kind": "video",
            "duration_frames": _video_duration_frames(final_path),
            "path": f"render-assets/{video_id}/{final_filename}",
        }

    except Exception as e:
        print(f"[warn] {label} video processing failed ({url}): {e}")
        _safe_remove(final_path)
        return None


def _try_photo_asset(photo: dict, asset_dir: str, video_id: str, label: str):
    """
    Download a photo entry. Returns the asset dict, or None if it
    could not be used.
    """
    photo_item_id = photo.get("id")
    url = photo.get("image_url")

    if not url:
        print(f"[warn] {label} photo id={photo_item_id} has no image_url")
        return None

    ext = _ext_from_url(url, "jpg")
    filename = f"photo_{photo_item_id}.{ext}"
    local_path = os.path.join(asset_dir, filename)

    try:
        _download(url, local_path)

        if not os.path.exists(local_path):
            raise RuntimeError(f"Photo was not downloaded: {local_path}")

        print(f"[photo] {label}: id={photo_item_id}")
        print(f"[photo] query: {photo.get('query', '')}")
        print(f"[photo] downloaded: {local_path}")

        return {
            "kind": "photo",
            "path": f"render-assets/{video_id}/{filename}",
        }

    except Exception as e:
        print(f"[warn] {label} photo download failed ({url}): {e}")
        return None


def _pick_asset(direction: dict, video_id: str):

    # 1. GET AVAILABLE MEDIA
    assets = direction.get("asserts") or {}
    videos = assets.get("videos") or []
    photos = assets.get("photos") or []

    # 2. GET SELECTED MEDIA
    selected_media_id = direction.get("selected_media_id")
    selected_media_type = direction.get("selected_media_type")

    print(
        f"[media] selected_media_id={selected_media_id}, "
        f"selected_media_type={selected_media_type}"
    )

    # 3. ASSET DIRECTORY
    asset_dir = os.path.join(
        REMOTION_PROJECT_DIR,
        "public",
        "render-assets",
        video_id,
    )
    os.makedirs(asset_dir, exist_ok=True)

    # 4. SELECTED VIDEO
    if selected_media_id is not None and selected_media_type == "video":

        selected_video = next(
            (v for v in videos if str(v.get("id")) == str(selected_media_id)),
            None,
        )

        if selected_video is None:
            print(f"[warn] selected video id={selected_media_id} not found in asserts")
        else:
            result = _try_video_asset(selected_video, asset_dir, video_id, "selected")
            if result:
                return result

    # 5. SELECTED PHOTO
    if selected_media_id is not None and selected_media_type == "photo":

        selected_photo = next(
            (p for p in photos if str(p.get("id")) == str(selected_media_id)),
            None,
        )

        if selected_photo is None:
            print(f"[warn] selected photo id={selected_media_id} not found in asserts")
        else:
            result = _try_photo_asset(selected_photo, asset_dir, video_id, "selected")
            if result:
                return result

    # 6. NO SELECTION -> fallback: video first, photo second
    if selected_media_id is None:

        if videos:
            result = _try_video_asset(videos[0], asset_dir, video_id, "fallback selected")
            if result:
                return result

        if photos:
            result = _try_photo_asset(photos[0], asset_dir, video_id, "fallback selected")
            if result:
                return result

    # 7. NOTHING FOUND
    print(f"[warn] no usable media found for video_id={video_id}")

    return None


def to_frames(seconds: float) -> int:
    return round(seconds * FPS)


def _extract_template(direction: dict):
    if "template_name" in direction:
        name = direction["template_name"]
        props = dict(direction.get("template_props") or {})
        text = direction.get("text", "")
        return name, props, text

    legacy = direction.get("template")
    if isinstance(legacy, dict) and "name" in legacy:
        name = legacy.get("name")
        props = dict(legacy.get("props") or {})
        text = props.get("text", "")
        return name, props, text

    return None, None, None


def _fill_image_urls(node, photos, counter):

    if isinstance(node, dict):
        for key, value in node.items():
            if key == "image_url" and isinstance(value, str) and not value:
                node[key] = photos[counter[0] % len(photos)]
                counter[0] += 1
            else:
                _fill_image_urls(value, photos, counter)
    elif isinstance(node, list):
        for item in node:
            _fill_image_urls(item, photos, counter)


def _resolve_template_image(direction: dict, props: dict, template_name: str = ""):
    photos = (
        (direction.get("asserts") or {}).get("photos")
        or direction.get("template_photos")
        or []
    )
    urls = [p.get("image_url", "") for p in photos if p.get("image_url")]

    if not urls:
        return

    if template_name == "Before / After":
        if not props.get("before_url"):
            props["before_url"] = urls[0]
        if not props.get("after_url"):
            props["after_url"] = urls[1] if len(urls) > 1 else urls[0]
        return

    _fill_image_urls(props, urls, [0])


def normalize_scene(
    scene: dict,
    scene_offset_frames: int,
    video_id: str,
) -> dict:

    words = scene.get("word_timestamps", [])

    scene_duration_frames = (
        to_frames(words[-1]["end"])
        if words
        else 0
    )

    global_words = []
    prev_word_end_frame = None

    for w in words:

        start_frame = to_frames(w["start"]) + scene_offset_frames
        end_frame = to_frames(w["end"]) + scene_offset_frames

        # Ensure no overlap/backwards jump with the previous
        # word. Independent rounding of each word's start/end
        # can otherwise make two adjacent words both "active"
        # (or neither active) on the same frame, which is what
        # made the captions flicker/shake.
        if (
            prev_word_end_frame is not None
            and start_frame < prev_word_end_frame
        ):
            start_frame = prev_word_end_frame

        # Never let a word collapse to zero (or negative)
        # duration.
        if end_frame <= start_frame:
            end_frame = start_frame + 1

        global_words.append({
            "word": w["word"],
            "start_frame": start_frame,
            "end_frame": end_frame,
        })

        prev_word_end_frame = end_frame

    # -----------------------------------------
    # DIRECTIONS
    # -----------------------------------------

    directions = sorted(
        scene.get("directions", []),
        key=lambda d: d["start"],
    )

    norm_directions = []

    for idx, d in enumerate(directions):

        start_frame = to_frames(d["start"]) + scene_offset_frames
        end_frame = to_frames(d["end"]) + scene_offset_frames

        # Ensure no gaps
        if (
            norm_directions
            and start_frame != norm_directions[-1]["end_frame"]
        ):
            start_frame = norm_directions[-1]["end_frame"]

        # Never let a direction (B-roll/overlay) segment
        # collapse to zero (or negative) duration. Independent
        # rounding of start/end can otherwise produce a
        # near-instant segment, which made the background
        # video appear to flicker/jump between clips ("shaking").
        if end_frame <= start_frame:
            end_frame = start_frame + 1

        entry = {
            "id": f"{scene['id']}_{idx}",
            "type": d.get("type", "B-roll"),
            "start_frame": start_frame,
            "end_frame": end_frame,
        }

        # -----------------------------------------
        # PEXELS VIDEO / IMAGE
        # -----------------------------------------

        if d.get("asserts"):

            background = _pick_asset(d, video_id)

            if background:
                entry["background"] = background

        # -----------------------------------------
        # TEMPLATE
        # -----------------------------------------

        (
            template_name,
            template_props,
            template_text,
        ) = _extract_template(d)

        if template_name:

            component = TEMPLATE_COMPONENT_MAP.get(template_name)

            if component is None:

                print(
                    f"[warn] unknown template_name "
                    f"'{template_name}' "
                    f"on direction {entry['id']}, "
                    f"skipping overlay"
                )

            else:

                _resolve_template_image(d, template_props, template_name)

                entry["overlay"] = {
                    "component": component,
                    "template_name": template_name,
                    "props": template_props,
                    "text": template_text,
                }

                # -------------------------------------
                # OVERLAY TIMING (B-roll + overlay only)
                #
                # The B-roll plays from entry start_frame
                # to end_frame. The animation plays only
                # from overlay start_frame to end_frame,
                # in sync with the voice. Both are absolute
                # frames on the same timeline.
                # full_screen_animation gets no overlay
                # frames and covers the whole beat.
                # -------------------------------------

                if (
                    d.get("type") == "B-roll+overlay_animation"
                    and d.get("overlay_start") is not None
                    and d.get("overlay_end") is not None
                ):
                    o_start = to_frames(d["overlay_start"]) + scene_offset_frames
                    o_end = to_frames(d["overlay_end"]) + scene_offset_frames

                    o_start = max(start_frame, min(o_start, end_frame))
                    o_end = max(o_start + 1, min(o_end, end_frame))

                    entry["overlay"]["start_frame"] = o_start
                    entry["overlay"]["end_frame"] = o_end

        norm_directions.append(entry)

    # -----------------------------------------
    # MAKE LAST DIRECTION REACH SCENE END
    # -----------------------------------------

    if norm_directions:

        scene_end_frame = scene_offset_frames + scene_duration_frames

        norm_directions[-1]["end_frame"] = max(
            norm_directions[-1]["end_frame"],
            scene_end_frame,
        )

    return {
        "duration_frames": scene_duration_frames,
        "words": global_words,
        "directions": norm_directions,
    }


def _build_outro_direction(start_frame: int) -> dict:
    variant = random.choice(OUTRO_VARIANTS)
    props = {
        "title": "Thank you for watching!",
        "subtitle": "See you in the next one",
        "variant": variant,
        "background": "theme",
    }
    print(f"[outro] variant: {variant}")
    return {
        "id": "outro_thank_you",
        "type": "Template",
        "start_frame": start_frame,
        "end_frame": start_frame + OUTRO_FRAMES,
        "overlay": {
            "component": TEMPLATE_COMPONENT_MAP["Thank You Outro"],
            "template_name": "Thank You Outro",
            "props": props,
            "text": props["title"],
        },
    }


def build_render_props(
    timeline: dict,
    tmp_dir: str,
    video_id: str,
) -> dict:

    scenes = timeline["scenes"]

    scene_durations = []

    for scene in scenes:

        words = scene.get("word_timestamps", [])

        last_end = words[-1]["end"] if words else 0.0

        scene_durations.append(to_frames(last_end))

    scene_offsets = []

    running = 0

    for duration in scene_durations:
        scene_offsets.append(running)
        running += duration

    total_frames = running

    all_words = []
    all_directions = []
    audio_tracks = []

    asset_dir = os.path.join(
        REMOTION_PROJECT_DIR,
        "public",
        "render-assets",
        video_id,
    )

    os.makedirs(asset_dir, exist_ok=True)

    for scene, offset, duration in zip(
        scenes,
        scene_offsets,
        scene_durations,
    ):

        norm = normalize_scene(scene, offset, video_id)

        all_words.extend(norm["words"])
        all_directions.extend(norm["directions"])

        audio_filename = f"scene_{scene['id']}.mp3"

        audio_path = os.path.join(asset_dir, audio_filename)

        _download(scene["audio_url"], audio_path)

        if not os.path.exists(audio_path):
            raise RuntimeError(f"Audio file was not created: {audio_path}")

        remotion_audio_path = f"render-assets/{video_id}/{audio_filename}"

        audio_tracks.append({
            "scene_id": scene["id"],
            "path": remotion_audio_path,
            "offset_frame": offset,
            "duration_frames": duration,
        })

    all_directions.append(_build_outro_direction(total_frames))

    total_frames += OUTRO_FRAMES

    return {
        "fps": FPS,
        "width": WIDTH,
        "height": HEIGHT,
        "total_frames": total_frames,
        "caption_words_per_line": CAPTION_WORDS_PER_LINE,
        "words": all_words,
        "directions": all_directions,
        "audio_tracks": audio_tracks,
    }


def _mb(num_bytes: int) -> float:
    return num_bytes / (1024 * 1024)


def enforce_size_limit(out_path: str, tmp_dir: str) -> str:
    """
    Make sure the rendered video is below MAX_OUTPUT_MB.

    - If it is already small enough, nothing happens.
    - Otherwise it is re-encoded with ffmpeg at a bitrate computed
      from the video duration so the file lands near
      TARGET_OUTPUT_MB. If the result is still too big, the bitrate
      is lowered and it is tried again (up to MAX_SHRINK_ATTEMPTS).

    Returns the path of the final file (same out_path, replaced
    in place).
    """

    size = os.path.getsize(out_path)
    print(f"[size] rendered output: {_mb(size):.1f} MB (limit {MAX_OUTPUT_MB} MB)")

    if size <= MAX_OUTPUT_BYTES:
        return out_path

    duration = _video_duration_seconds(out_path)

    if duration <= 0:
        raise RuntimeError("Could not determine video duration for compression")

    target_bytes = TARGET_OUTPUT_BYTES

    for attempt in range(1, MAX_SHRINK_ATTEMPTS + 1):

        # total bits budget -> subtract audio -> video bitrate
        total_bps = (target_bytes * 8) / duration
        video_bps = int(max(total_bps - AUDIO_BITRATE_BPS, 200_000))

        print(
            f"[size] attempt {attempt}/{MAX_SHRINK_ATTEMPTS}: "
            f"re-encoding at ~{video_bps / 1_000_000:.2f} Mbps video "
            f"(target {_mb(target_bytes):.0f} MB)"
        )

        small_path = os.path.join(tmp_dir, f"output_small_{attempt}.mp4")

        try:
            _run([
                "ffmpeg",
                "-y",
                "-i", out_path,
                "-c:v", "libx264",
                "-preset", "medium",
                "-b:v", str(video_bps),
                "-maxrate", str(video_bps),
                "-bufsize", str(video_bps * 2),
                "-pix_fmt", "yuv420p",
                "-profile:v", "main",
                "-level:v", "4.1",
                "-c:a", "aac",
                "-b:a", str(AUDIO_BITRATE_BPS),
                "-movflags", "+faststart",
                small_path,
            ])

            new_size = os.path.getsize(small_path)
            print(f"[size] re-encoded output: {_mb(new_size):.1f} MB")

            if new_size <= MAX_OUTPUT_BYTES:
                os.replace(small_path, out_path)
                return out_path

            # Still too big -> lower the target and try again.
            target_bytes = int(target_bytes * 0.8)

        finally:
            _safe_remove(small_path)

    raise RuntimeError(
        f"Could not bring video under {MAX_OUTPUT_MB} MB "
        f"after {MAX_SHRINK_ATTEMPTS} attempts"
    )


def render_with_remotion(
    props: dict,
    tmp_dir: str,
) -> str:

    if not REMOTION_PROJECT_DIR:
        raise RuntimeError("REMOTION_PROJECT_DIR is not set")

    props_path = os.path.join(tmp_dir, "props.json")

    with open(props_path, "w", encoding="utf-8") as f:
        json.dump(props, f, indent=2)

    out_path = os.path.join(tmp_dir, "output.mp4")

    remotion_cli = os.path.join(
        REMOTION_PROJECT_DIR,
        "node_modules",
        ".bin",
        "remotion",
    )

    public_dir = os.path.join(REMOTION_PROJECT_DIR, "public")

    _run([
        remotion_cli,
        "render",
        REMOTION_ENTRY,
        REMOTION_COMPOSITION_ID,
        out_path,
        f"--props={props_path}",
        f"--public-dir={public_dir}",
        "--concurrency=4",
    ])

    # Fail with a clear message instead of a confusing
    # FileNotFoundError later during upload.
    if not os.path.exists(out_path):
        raise RuntimeError(
            f"Remotion finished but output is missing: {out_path}"
        )

    # Make sure the final file is under the size limit (500 MB).
    out_path = enforce_size_limit(out_path, tmp_dir)

    return out_path


def render_timeline(
    video_id: str,
    timeline: dict,
    render_tmp_root: str,
) -> str:

    tmp_dir = os.path.join(
        render_tmp_root,
        f"{video_id}-{uuid.uuid4().hex[:8]}",
    )

    os.makedirs(tmp_dir, exist_ok=True)

    props = build_render_props(timeline, tmp_dir, video_id)

    return render_with_remotion(props, tmp_dir)


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("[Lifespan] Server started.")
    yield
    print("[Lifespan] Shutting down.")


app = FastAPI(lifespan=lifespan)

origins = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def upload_to_supabase(video_id: str, output_path: str) -> str:
    bucket = "rendered-videos"
    storage_path = f"{video_id}.mp4"

    with open(output_path, "rb") as f:
        supabase.storage.from_(bucket).upload(
            path=storage_path,
            file=f,
            file_options={
                "content-type": "video/mp4",
                "upsert": "true",
            },
        )

    public_url = supabase.storage.from_(bucket).get_public_url(storage_path)

    supabase.table("videos").update(
        {"video_url": public_url}
    ).eq("id", video_id).execute()

    return public_url


_render_locks: dict[str, threading.Lock] = {}
_render_locks_guard = threading.Lock()


def _get_lock(video_id: str) -> threading.Lock:
    with _render_locks_guard:
        return _render_locks.setdefault(video_id, threading.Lock())


@app.post("/render/{video_id}")
def render_video(video_id: str):
    lock = _get_lock(video_id)

    if not lock.acquire(blocking=False):
        raise HTTPException(
            status_code=409,
            detail="Render already in progress for this video",
        )

    tmp_dir = None

    try:
        timeline = get_timeline(video_id)
        output_path = render_timeline(video_id, timeline, RENDER_TMP_ROOT)
        tmp_dir = os.path.dirname(output_path)

        try:
            video_url = upload_to_supabase(video_id, output_path)
        except Exception as e:
            raise HTTPException(
                status_code=502,
                detail=f"Upload failed: {e}",
            )

        return {
            "video_id": video_id,
            "video_url": video_url,
        }
    finally:
        lock.release()
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)