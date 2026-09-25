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


def _ext_from_url(url: str, default: str) -> str:
    tail = url.split("?")[0]
    if "." in tail.rsplit("/", 1)[-1]:
        return tail.rsplit(".", 1)[-1]
    return default


def _pick_asset(direction: dict, tmp_dir: str):
    asserts = direction.get("asserts") or {}
    videos = asserts.get("videos") or []
    photos = asserts.get("photos") or []

    if videos:
        v = videos[0]
        url = v["video_url"]
        ext = _ext_from_url(url, "mp4")
        path = os.path.join(tmp_dir, "assets", f"video_{v['id']}.{ext}")
        try:
            _download(url, path)
            return "video", path
        except Exception as e:
            print(f"[warn] video download failed ({url}): {e}")

    if photos:
        p = photos[0]
        url = p["image_url"]
        ext = _ext_from_url(url, "jpg")
        path = os.path.join(tmp_dir, "assets", f"photo_{p['id']}.{ext}")
        try:
            _download(url, path)
            return "photo", path
        except Exception as e:
            print(f"[warn] photo download failed ({url}): {e}")

    return None, None


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


def normalize_scene(scene: dict, scene_offset_frames: int, tmp_dir: str) -> dict:
    words = scene.get("word_timestamps", [])
    scene_duration_frames = to_frames(words[-1]["end"]) if words else 0

    global_words = []
    for w in words:
        global_words.append({
            "word": w["word"],
            "start_frame": to_frames(w["start"]) + scene_offset_frames,
            "end_frame": to_frames(w["end"]) + scene_offset_frames,
        })

    directions = sorted(scene.get("directions", []), key=lambda d: d["start"])
    norm_directions = []

    for idx, d in enumerate(directions):
        start_frame = to_frames(d["start"]) + scene_offset_frames
        end_frame = to_frames(d["end"]) + scene_offset_frames

        if norm_directions and start_frame != norm_directions[-1]["end_frame"]:
            start_frame = norm_directions[-1]["end_frame"]

        entry = {
            "id": f"{scene['id']}_{idx}",
            "type": d.get("type", "B-roll"),
            "start_frame": start_frame,
            "end_frame": end_frame,
        }

        if d.get("asserts"):
            kind, local_path = _pick_asset(d, tmp_dir)
            entry["background"] = {"kind": kind, "path": local_path}

        template_name, template_props, template_text = _extract_template(d)
        if template_name:
            component = TEMPLATE_COMPONENT_MAP.get(template_name)
            if component is None:
                print(f"[warn] unknown template_name '{template_name}' on direction {entry['id']}, skipping overlay")
            else:
                _resolve_template_image(d, template_props)
                entry["overlay"] = {
                    "component": component,
                    "template_name": template_name,
                    "props": template_props,
                    "text": template_text,
                }

        norm_directions.append(entry)

    if norm_directions:
        scene_end_frame = scene_offset_frames + scene_duration_frames
        norm_directions[-1]["end_frame"] = max(norm_directions[-1]["end_frame"], scene_end_frame)

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

    # =========================================================
    # Calculate duration of every scene
    # =========================================================

    scene_durations = []

    for scene in scenes:
        words = scene.get("word_timestamps", [])

        last_end = words[-1]["end"] if words else 0.0

        scene_durations.append(
            to_frames(last_end)
        )

    # =========================================================
    # Calculate global scene offsets
    # =========================================================

    scene_offsets = []

    running = 0

    for duration in scene_durations:
        scene_offsets.append(running)
        running += duration

    total_frames = running

    # =========================================================
    # Final render data
    # =========================================================

    all_words = []
    all_directions = []
    audio_tracks = []

    # =========================================================
    # Remotion public directory
    #
    # Physical files:
    #
    # /opt/storybit-remotion/public/
    #     render-assets/
    #         <video_id>/
    #             scene_1.mp3
    #             scene_2.mp3
    #
    # =========================================================

    audio_dir = os.path.join(
        REMOTION_PROJECT_DIR,
        "public",
        "render-assets",
        video_id,
    )

    os.makedirs(
        audio_dir,
        exist_ok=True,
    )

    # =========================================================
    # Process every scene
    # =========================================================

    for scene, offset, duration in zip(
        scenes,
        scene_offsets,
        scene_durations,
    ):

        # -----------------------------------------------------
        # Normalize directions
        # -----------------------------------------------------

        norm = normalize_scene(
            scene,
            offset,
            tmp_dir,
        )

        all_words.extend(
            norm["words"]
        )

        all_directions.extend(
            norm["directions"]
        )

        # -----------------------------------------------------
        # Download scene audio
        # -----------------------------------------------------

        audio_filename = (
            f"scene_{scene['id']}.mp3"
        )

        audio_path = os.path.join(
            audio_dir,
            audio_filename,
        )

        _download(
            scene["audio_url"],
            audio_path,
        )

        # -----------------------------------------------------
        # Verify downloaded audio exists
        # -----------------------------------------------------

        if not os.path.exists(audio_path):
            raise RuntimeError(
                f"Audio file was not created: {audio_path}"
            )

        # -----------------------------------------------------
        # Path passed to Remotion
        #
        # IMPORTANT:
        #
        # Do NOT include "public/" here.
        #
        # Correct:
        #
        # render-assets/video_id/scene_1.mp3
        #
        # Wrong:
        #
        # public/render-assets/video_id/scene_1.mp3
        #
        # -----------------------------------------------------

        remotion_audio_path = (
            f"render-assets/{video_id}/{audio_filename}"
        )

        audio_tracks.append(
            {
                "scene_id": scene["id"],
                "path": remotion_audio_path,
                "offset_frame": offset,
                "duration_frames": duration,
            }
        )

    # =========================================================
    # Final props
    # =========================================================

    return {
        "fps": FPS,
        "width": WIDTH,
        "height": HEIGHT,

        "total_frames": total_frames,

        "caption_words_per_line": (
            CAPTION_WORDS_PER_LINE
        ),

        "words": all_words,

        "directions": all_directions,

        "audio_tracks": audio_tracks,
    }


def render_with_remotion(props: dict, tmp_dir: str) -> str:
    if not REMOTION_PROJECT_DIR:
        raise RuntimeError("REMOTION_PROJECT_DIR is not set")

    props_path = os.path.join(
        tmp_dir,
        "props.json",
    )

    with open(
        props_path,
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(props, f)

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