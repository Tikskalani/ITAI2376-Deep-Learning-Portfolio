"""Record → transcript → notes and action items.

Trilok presses Record in the PWA (or uploads a Voice Memo), and:

  1. the audio lands in data/recordings/ on the laptop, never anywhere else;
  2. faster-whisper transcribes it locally (an hour of audio in minutes on
     the CPU, no API, no cost);
  3. the agent that owns that kind of recording writes the notes: the
     Academic agent for a lecture, the COO for a meeting, the CEO for a
     note to self. Notes end with an "Action items" list; dated items are
     parsed out so they can become deadlines.

Everything is announced on the event stream, so the Home page shows
"transcribing" and "writing notes" as they happen, and the Learn tab
shows the result. process() never raises: a failure lands on the row
with its reason and a Retry button.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from core import db, events
from core.config import REPO_ROOT

log = logging.getLogger(__name__)

KINDS = ("lecture", "meeting", "note")
AGENT_FOR_KIND = {"lecture": "academic", "meeting": "coo", "note": "ceo"}
STATUSES = ("uploaded", "transcribing", "transcribed", "noting", "done", "failed")
DIR = REPO_ROOT / "data" / "recordings"
MAX_BYTES = 600 * 1024 * 1024  # two hours of webm/opus is well under this
ALLOWED = {".webm", ".m4a", ".mp3", ".wav", ".ogg", ".mp4", ".aac", ".flac", ".opus", ".caf", ".weba"}
# What the agent reads. Two hours of speech is about 120k characters; past
# that the end is what gets cut, and the notes say so.
TRANSCRIPT_CHARS = 150_000
_COLS = ("id, kind, title, path, bytes, duration_s, status, project_id, transcript, notes, action_items, "
         "episode_id, error, created_at, updated_at")
_ITEM = re.compile(r"^\s*[-*]\s*(?:\[[ xX]?\]\s*)?(.+?)(?:\s*\((?:due|by)\s+(\d{4}-\d{2}-\d{2})\))?\s*$")
_tasks: set[asyncio.Task[Any]] = set()


def _row(r: Any) -> dict[str, Any]:
    d = dict(r)
    d["action_items"] = list(d.get("action_items") or [])
    return d


def _ext(filename: str) -> str:
    return ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""


async def create(data: bytes, *, filename: str, kind: str = "lecture", title: str = "",
                 project: str | None = None) -> dict[str, Any]:
    """Store the audio and open the row. Raises ValueError on bad input."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    ext = _ext(filename)
    if ext not in ALLOWED:
        raise ValueError(f"unsupported audio type {ext or filename!r}; use webm, m4a, mp3, wav, ogg or mp4")
    if not data:
        raise ValueError("the recording is empty")
    if len(data) > MAX_BYTES:
        raise ValueError(f"the recording is too large ({len(data) // (1024 * 1024)} MB; the cap is {MAX_BYTES // (1024 * 1024)} MB)")
    project_id = None
    if project:
        from core.projects import get_project

        proj = await get_project(project)
        if proj is None:
            raise ValueError(f"no project with slug {project!r}")
        project_id = proj.id
    rec_id = uuid4()
    DIR.mkdir(parents=True, exist_ok=True)
    path = DIR / f"{rec_id}{ext}"
    await asyncio.to_thread(path.write_bytes, data)
    title = (title or "").strip()[:200] or f"{kind.capitalize()} {datetime.now().strftime('%b %d, %H:%M')}"
    row = await db.fetchrow(
        f"insert into recordings (id, kind, title, path, bytes, project_id) values ($1, $2, $3, $4, $5, $6) "
        f"returning {_COLS}",
        rec_id, kind, title, str(path), len(data), project_id,
    )
    await events.emit("recording", title=title, detail="recorded", data={"recording_id": str(rec_id), "status": "uploaded"})
    return _row(row)


async def _set(rec_id: UUID, status: str, **fields: Any) -> None:
    """Fixed statement: every column has a set-flag and a value."""
    await db.execute(
        """
        update recordings set
          status       = $2,
          transcript   = case when $3  then $4::text  else transcript end,
          notes        = case when $5  then $6::text  else notes end,
          action_items = case when $7  then $8::jsonb else action_items end,
          episode_id   = case when $9  then $10::uuid else episode_id end,
          error        = case when $11 then $12::text else error end,
          duration_s   = case when $13 then $14::int  else duration_s end,
          updated_at   = now()
        where id = $1
        """,
        rec_id, status,
        "transcript" in fields, fields.get("transcript"),
        "notes" in fields, fields.get("notes"),
        "action_items" in fields, fields.get("action_items"),
        "episode_id" in fields, fields.get("episode_id"),
        "error" in fields, fields.get("error"),
        "duration_s" in fields, fields.get("duration_s"),
    )


async def _transcribe(path: str) -> tuple[str | None, int | None]:
    """(text, seconds) from local faster-whisper, in a worker thread."""
    from core import transcription

    def work() -> tuple[str | None, int | None]:
        model = transcription._get_model()
        if model is None:
            return None, None
        try:
            segments, info = model.transcribe(path, beam_size=1, task="transcribe", vad_filter=True)
            text = " ".join(seg.text for seg in segments)
        except Exception:
            log.exception("transcription failed for %s", path)
            return None, None
        return re.sub(r"\s+", " ", text).strip() or None, int(getattr(info, "duration", 0) or 0) or None

    return await asyncio.to_thread(work)


def notes_task(kind: str, title: str, transcript: str, *, cut: bool = False) -> str:
    what = {"lecture": "a lecture Trilok attended", "meeting": "a meeting Trilok was in",
            "note": "a note Trilok recorded for himself"}[kind]
    ask = {
        "lecture": ("Write study notes he can learn from: the main ideas in order, each with the one-line "
                    "explanation the lecturer gave, definitions and formulas exactly as stated, worked examples, "
                    "and anything flagged as on the exam or in the assignment. Then five practice questions "
                    "with short answers."),
        "meeting": ("Write the minutes: who was there (as named), what was decided, what was left open and who "
                    "owns it, and the numbers and dates said out loud. No filler."),
        "note": "Write it up cleanly in his words: what he said, as a short memo, nothing added.",
    }[kind]
    tail = ("\n\nThe transcript was cut at the end for length; say so in one line at the top." if cut else "")
    return (
        f"Transcript of {what}, titled \"{title}\". It is machine-transcribed: names and numbers may be "
        f"garbled, so keep them as heard and mark doubtful ones with (?). {ask} Use markdown headings and "
        f"short bullets, in the voice rules. End with a section titled exactly \"## Action items\": one bullet "
        f"per thing he must do, each ending with (due YYYY-MM-DD) when a date was said, otherwise no date. "
        f"If there are none, write \"- none\". Reply with the notes only.{tail}\n\n---\n{transcript}\n---"
    )


def parse_action_items(notes: str) -> list[dict[str, Any]]:
    """The bullets under '## Action items' as {text, due}."""
    if not notes:
        return []
    m = re.search(r"^##\s*Action items\s*$", notes, re.I | re.M)
    if not m:
        return []
    items: list[dict[str, Any]] = []
    for line in notes[m.end():].splitlines():
        if line.startswith("#"):
            break
        hit = _ITEM.match(line)
        if not hit:
            continue
        text = hit.group(1).strip()
        if not text or text.lower() == "none":
            continue
        items.append({"text": text, "due": hit.group(2)})
    return items


async def _to_planner(rec_id: UUID, project_id: UUID | None, items: list[dict[str, Any]]) -> int:
    """Action items land on the Planner, dated when the notes dated them.
    The recording is the source, so a re-run adds nothing twice."""
    from core import planner

    added = 0
    for it in items:
        line = it["text"] + (f" (due {it['due']})" if it.get("due") else "")
        try:
            task = await planner.quick_add(line, project=project_id, source="recording", source_ref=str(rec_id))
            added += int(bool(task.get("created")))
        except Exception:
            log.exception("recording %s: action item did not reach the planner: %s", rec_id, line[:80])
    return added


async def process(rec_id: UUID | str, *, dispatch_fn: Any = None) -> dict[str, Any] | None:
    """Transcribe, then write the notes. Never raises; failures land on the row."""
    rec_id = UUID(str(rec_id))
    row = await db.fetchrow(f"select {_COLS} from recordings where id = $1", rec_id)
    if row is None:
        return None
    rec = _row(row)
    try:
        transcript = rec.get("transcript")
        if not transcript:
            await _set(rec_id, "transcribing", error=None)
            await events.emit("recording", title=rec["title"], detail="transcribing on the laptop",
                              data={"recording_id": str(rec_id), "status": "transcribing"})
            transcript, seconds = await _transcribe(rec["path"])
            if not transcript:
                raise RuntimeError("transcription produced no text (is faster-whisper installed and the audio audible?)")
            await _set(rec_id, "transcribed", transcript=transcript, duration_s=seconds)
        await _set(rec_id, "noting")
        agent = AGENT_FOR_KIND[rec["kind"]]
        await events.emit("recording", agent=agent, title=rec["title"], detail="writing the notes",
                          data={"recording_id": str(rec_id), "status": "noting"})
        if dispatch_fn is None:
            from core.runtime.dispatcher import dispatch as dispatch_fn
        from core.runtime import ledger

        cut = len(transcript) > TRANSCRIPT_CHARS
        with ledger.ensure_scope(f"recording:{rec_id}"):
            result = await dispatch_fn(agent, notes_task(rec["kind"], rec["title"], transcript[:TRANSCRIPT_CHARS], cut=cut),
                                       dispatched_by="recordings", participants=["owner", agent])
        notes = (getattr(result, "text", "") or "").strip()
        if not notes:
            raise RuntimeError("the agent returned no notes")
        items = parse_action_items(notes)
        await _set(rec_id, "done", notes=notes, action_items=items, episode_id=getattr(result, "episode_id", None),
                   error=None)
        await _to_planner(rec_id, rec.get("project_id"), items)
        await events.emit("recording", agent=agent, episode_id=getattr(result, "episode_id", None), title=rec["title"],
                          detail=f"notes ready, {len(items)} action item{'s' if len(items) != 1 else ''}",
                          data={"recording_id": str(rec_id), "status": "done"})
        from core.surfaces.notify import notify

        await notify("recording", f"Notes ready: {rec['title']}",
                     (notes[:400] + ("…" if len(notes) > 400 else "")) + f"\n\n{len(items)} action item{'s' if len(items) != 1 else ''}. Open in Learn.",
                     data={"view": "learn", "recording_id": str(rec_id)})
    except Exception as exc:  # noqa: BLE001 - the row carries the reason; Retry runs it again
        log.exception("recording %s failed", rec_id)
        await _set(rec_id, "failed", error=f"{type(exc).__name__}: {str(exc)[:300]}")
        await events.emit("recording", title=rec["title"], detail=f"failed: {str(exc)[:160]}",
                          data={"recording_id": str(rec_id), "status": "failed"})
    return await get(rec_id)


def start_background(rec_id: UUID | str) -> None:
    """Transcribe and write notes without holding the request open."""
    task = asyncio.create_task(process(rec_id))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def listing(limit: int = 30) -> list[dict[str, Any]]:
    rows = await db.fetch(
        f"select {_COLS.replace('transcript, ', '')} from recordings order by created_at desc limit $1",
        max(1, min(int(limit), 200)),
    )
    return [_row(r) for r in rows]


async def get(rec_id: UUID | str) -> dict[str, Any] | None:
    row = await db.fetchrow(f"select {_COLS} from recordings where id = $1", UUID(str(rec_id)))
    return _row(row) if row else None


def audio_path(rec: dict[str, Any]) -> Path | None:
    """The file on disk, only if it is where create() put it."""
    p = Path(rec["path"]).resolve()
    return p if p.is_file() and DIR.resolve() in p.parents else None


__all__ = ["AGENT_FOR_KIND", "DIR", "KINDS", "MAX_BYTES", "STATUSES", "audio_path", "create", "get", "listing",
           "notes_task", "parse_action_items", "process", "start_background"]
