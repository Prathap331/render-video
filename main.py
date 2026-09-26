import os
import json
import subprocess
import requests
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()


FPS = 30
WIDTH = 1920
HEIGHT = 1080
CAPTION_WORDS_PER_LINE = 10

RENDER_TMP_ROOT = os.getenv("RENDER_TMP_ROOT", "/tmp/storybit-render")
REMOTION_PROJECT_DIR = os.getenv("REMOTION_PROJECT_DIR", "/opt/storybit-remotion")
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
    "Title Card": "TitleCard",
    "Title + Metadata": "TitleMetadata",
    "Big Number": "BigNumber",
    "Number Comparison": "NumberComparison",
    "Quote Card": "QuoteCard",
    "Key Statement": "KeyStatement",
    "Structured List": "StructuredList",
    "Comparison Columns": "ComparisonColumns",
    "Bar Chart": "BarChart",
    "Line Chart": "LineChart",
    "Pie / Donut Chart": "PieDonut",
    "Leaderboard": "Leaderboard",
    "Timeline": "Timeline",
    "A → B Relationship": "Relationship",
    "Person Intro Card": "PersonIntro",
    "Image + Label / Caption": "ImageCaption",
    "Linear Process": "LinearProcess",
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
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(dest_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 16):
                f.write(chunk)
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
    """

    os.makedirs(os.path.dirname(dest_path), exist_ok=True)

    _run([
        "ffmpeg",
        "-y",
        "-i", src_path,

        # Make every video the same size as the Remotion composition.
        "-vf",
        (
            "scale=1920:1080:"
            "force_original_aspect_ratio=increase,"
            "crop=1920:1080,"
            "setsar=1,"
            "fps=30"
        ),

        # Browser-friendly video.
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "23",

        # Important for Chromium/Remotion compatibility.
        "-pix_fmt", "yuv420p",
        "-profile:v", "main",
        "-level:v", "4.1",

        # Put MP4 metadata at the beginning.
        "-movflags", "+faststart",

        # B-roll doesn't need its source audio.
        "-an",

        dest_path,
    ])


def _ext_from_url(url: str, default: str) -> str:
    tail = url.split("?")[0]
    if "." in tail.rsplit("/", 1)[-1]:
        return tail.rsplit(".", 1)[-1]
    return default


def _pick_asset(direction: dict, video_id: str):

    assets = direction.get("asserts") or {}

    videos = assets.get("videos") or []
    photos = assets.get("photos") or []

    asset_dir = os.path.join(
        REMOTION_PROJECT_DIR,
        "public",
        "render-assets",
        video_id,
    )

    os.makedirs(asset_dir, exist_ok=True)

    # =========================================================
    # PREFER VIDEO
    # =========================================================

    if videos:

        video = videos[0]

        url = video["video_url"]

        ext = _ext_from_url(url, "mp4")

        # Original downloaded file.
        source_filename = (
            f"video_{video['id']}_source.{ext}"
        )

        source_path = os.path.join(
            asset_dir,
            source_filename,
        )

        # Final normalized file that Remotion will use.
        final_filename = (
            f"video_{video['id']}.mp4"
        )

        final_path = os.path.join(
            asset_dir,
            final_filename,
        )

        try:

            # -------------------------------------------------
            # DOWNLOAD ORIGINAL
            # -------------------------------------------------

            _download(
                url,
                source_path,
            )

            # -------------------------------------------------
            # NORMALIZE FOR REMOTION
            # -------------------------------------------------

            # Always normalize the source.
            #
            # This is intentional because an old final MP4
            # may already exist and may be the problematic file.
            _normalize_video(
                source_path,
                final_path,
            )

            if not os.path.exists(final_path):
                raise RuntimeError(
                    f"Normalized video was not created: "
                    f"{final_path}"
                )

            print(
                f"[video] normalized: "
                f"{source_path} -> {final_path}"
            )

            return {
                "kind": "video",
                "path": (
                    f"render-assets/"
                    f"{video_id}/"
                    f"{final_filename}"
                ),
            }

        except Exception as e:

            print(
                f"[warn] video processing failed "
                f"({url}): {e}"
            )

            # If normalization failed, remove the broken
            # output so Remotion never receives it.
            if os.path.exists(final_path):
                try:
                    os.remove(final_path)
                except Exception:
                    pass

    # =========================================================
    # OTHERWISE IMAGE
    # =========================================================

    if photos:

        photo = photos[0]

        url = photo["image_url"]

        ext = _ext_from_url(
            url,
            "jpg",
        )

        filename = (
            f"photo_{photo['id']}.{ext}"
        )

        local_path = os.path.join(
            asset_dir,
            filename,
        )

        try:

            _download(
                url,
                local_path,
            )

            return {
                "kind": "photo",
                "path": (
                    f"render-assets/"
                    f"{video_id}/"
                    f"{filename}"
                ),
            }

        except Exception as e:

            print(
                f"[warn] photo download failed "
                f"({url}): {e}"
            )

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


def _resolve_template_image(direction: dict, props: dict):
    if props.get("image_url"):
        return
    photos = (direction.get("asserts") or {}).get("photos") or []
    if photos:
        props["image_url"] = photos[0].get("image_url", "")


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

    # -----------------------------------------
    # WORD TIMESTAMPS
    # -----------------------------------------

    global_words = []

    for w in words:
        global_words.append({
            "word": w["word"],
            "start_frame": (
                to_frames(w["start"])
                + scene_offset_frames
            ),
            "end_frame": (
                to_frames(w["end"])
                + scene_offset_frames
            ),
        })

    # -----------------------------------------
    # DIRECTIONS
    # -----------------------------------------

    directions = sorted(
        scene.get("directions", []),
        key=lambda d: d["start"],
    )

    norm_directions = []

    for idx, d in enumerate(directions):

        start_frame = (
            to_frames(d["start"])
            + scene_offset_frames
        )

        end_frame = (
            to_frames(d["end"])
            + scene_offset_frames
        )

        # Ensure no gaps
        if (
            norm_directions
            and start_frame
            != norm_directions[-1]["end_frame"]
        ):
            start_frame = (
                norm_directions[-1]["end_frame"]
            )

        entry = {
            "id": f"{scene['id']}_{idx}",
            "type": d.get(
                "type",
                "B-roll",
            ),
            "start_frame": start_frame,
            "end_frame": end_frame,
        }

        # -----------------------------------------
        # PEXELS VIDEO / IMAGE
        # -----------------------------------------

        if d.get("asserts"):

            background = _pick_asset(
                d,
                video_id,
            )

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

            component = TEMPLATE_COMPONENT_MAP.get(
                template_name
            )

            if component is None:

                print(
                    f"[warn] unknown template_name "
                    f"'{template_name}' "
                    f"on direction {entry['id']}, "
                    f"skipping overlay"
                )

            else:

                _resolve_template_image(
                    d,
                    template_props,
                )

                entry["overlay"] = {
                    "component": component,
                    "template_name": template_name,
                    "props": template_props,
                    "text": template_text,
                }

        norm_directions.append(entry)

    # -----------------------------------------
    # MAKE LAST DIRECTION REACH SCENE END
    # -----------------------------------------

    if norm_directions:

        scene_end_frame = (
            scene_offset_frames
            + scene_duration_frames
        )

        norm_directions[-1]["end_frame"] = max(
            norm_directions[-1]["end_frame"],
            scene_end_frame,
        )

    return {
        "duration_frames": scene_duration_frames,
        "words": global_words,
        "directions": norm_directions,
    }


def build_render_props(
    timeline: dict,
    tmp_dir: str,
    video_id: str,
) -> dict:

    scenes = timeline["scenes"]

    # -----------------------------------------
    # CALCULATE SCENE DURATIONS
    # -----------------------------------------

    scene_durations = []

    for scene in scenes:

        words = scene.get(
            "word_timestamps",
            [],
        )

        last_end = (
            words[-1]["end"]
            if words
            else 0.0
        )

        scene_durations.append(
            to_frames(last_end)
        )

    # -----------------------------------------
    # CALCULATE OFFSETS
    # -----------------------------------------

    scene_offsets = []

    running = 0

    for duration in scene_durations:

        scene_offsets.append(running)

        running += duration

    total_frames = running

    # -----------------------------------------
    # OUTPUT ARRAYS
    # -----------------------------------------

    all_words = []
    all_directions = []
    audio_tracks = []

    # -----------------------------------------
    # REMOTION PUBLIC ASSET DIRECTORY
    # -----------------------------------------

    asset_dir = os.path.join(
        REMOTION_PROJECT_DIR,
        "public",
        "render-assets",
        video_id,
    )

    os.makedirs(
        asset_dir,
        exist_ok=True,
    )

    # -----------------------------------------
    # PROCESS EVERY SCENE
    # -----------------------------------------

    for scene, offset, duration in zip(
        scenes,
        scene_offsets,
        scene_durations,
    ):

        norm = normalize_scene(
            scene,
            offset,
            video_id,
        )

        all_words.extend(
            norm["words"]
        )

        all_directions.extend(
            norm["directions"]
        )

        # -------------------------------------
        # AUDIO
        # -------------------------------------

        audio_filename = (
            f"scene_{scene['id']}.mp3"
        )

        audio_path = os.path.join(
            asset_dir,
            audio_filename,
        )

        _download(
            scene["audio_url"],
            audio_path,
        )

        if not os.path.exists(
            audio_path
        ):
            raise RuntimeError(
                f"Audio file was not created: "
                f"{audio_path}"
            )

        remotion_audio_path = (
            f"render-assets/"
            f"{video_id}/"
            f"{audio_filename}"
        )

        audio_tracks.append({
            "scene_id": scene["id"],
            "path": remotion_audio_path,
            "offset_frame": offset,
            "duration_frames": duration,
        })

    # -----------------------------------------
    # FINAL PROPS
    # -----------------------------------------

    return {
        "fps": FPS,
        "width": WIDTH,
        "height": HEIGHT,

        "total_frames": total_frames,

        "caption_words_per_line":
            CAPTION_WORDS_PER_LINE,

        "words": all_words,

        "directions":
            all_directions,

        "audio_tracks":
            audio_tracks,
    }



def render_with_remotion(
    props: dict,
    tmp_dir: str,
) -> str:

    if not REMOTION_PROJECT_DIR:
        raise RuntimeError(
            "REMOTION_PROJECT_DIR is not set"
        )

    props_path = os.path.join(
        tmp_dir,
        "props.json",
    )

    with open(
        props_path,
        "w",
        encoding="utf-8",
    ) as f:

        json.dump(
            props,
            f,
            indent=2,
        )

    out_path = os.path.join(
        tmp_dir,
        "output.mp4",
    )

    _run([
        "/opt/storybit-remotion/node_modules/.bin/remotion",
        "render",
        REMOTION_ENTRY,
        REMOTION_COMPOSITION_ID,
        out_path,
        f"--props={props_path}",
        "--public-dir=/opt/storybit-remotion/public",
        "--concurrency=4",
    ])

    return out_path


def render_timeline(
    video_id: str,
    timeline: dict,
    render_tmp_root: str,
) -> str:

    tmp_dir = os.path.join(
        render_tmp_root,
        video_id,
    )

    os.makedirs(
        tmp_dir,
        exist_ok=True,
    )

    props = build_render_props(
        timeline,
        tmp_dir,
        video_id,
    )

    return render_with_remotion(
        props,
        tmp_dir,
    )


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


@app.post("/render/{video_id}")
async def render_video(video_id: str):
    timeline = get_timeline(video_id)
    output_path = render_timeline(video_id, timeline, RENDER_TMP_ROOT)
    return {"video_id": video_id, "output_path": output_path}