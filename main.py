import os
import json
import subprocess
import requests
from contextlib import asynccontextmanager
from fastapi import FastAPI
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

RENDER_TMP_ROOT = os.getenv("RENDER_TMP_ROOT","/tmp/storybit-render")
os.makedirs(RENDER_TMP_ROOT, exist_ok=True)
REMOTION_PROJECT_DIR = os.getenv("REMOTION_PROJECT_DIR","/root/remotion-renderer")
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
    "Callout / Annotation": "Callout",
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
        #
        # NOTE: a plain "fps=30" here just drops/duplicates frames.
        # When the source isn't already 30 (or an exact multiple of
        # 30, e.g. 25fps PAL footage), that duplication happens on a
        # fixed cadence (e.g. one repeated frame every 6 frames for
        # 25fps -> 30fps), which is perceived as periodic
        # stutter/freezing ("stucking") in otherwise-moving footage.
        # "framerate" blends neighbouring frames instead of duplicating
        # them, so the change is smooth, and it is ~25x faster than
        # minterpolate.
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

        start_frame = (
            to_frames(w["start"])
            + scene_offset_frames
        )

        end_frame = (
            to_frames(w["end"])
            + scene_offset_frames
        )

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

        # Never let a direction (B-roll/overlay) segment
        # collapse to zero (or negative) duration. Independent
        # rounding of start/end can otherwise produce a
        # near-instant segment, which made the background
        # video appear to flicker/jump between clips ("shaking").
        if end_frame <= start_frame:
            end_frame = start_frame + 1

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
                    template_name,
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

    os.makedirs(
        asset_dir,
        exist_ok=True,
    )

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


    all_directions.append(
        _build_outro_direction(total_frames)
    )

    total_frames += OUTRO_FRAMES


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

@app.post("/render/{video_id}")
async def render_video(video_id: str):
    timeline = get_timeline(video_id)
    output_path = render_timeline(video_id, timeline, RENDER_TMP_ROOT)
    video_url = upload_to_supabase(video_id, output_path)
    return {
        "video_id": video_id,
        "video_url": video_url,
    }