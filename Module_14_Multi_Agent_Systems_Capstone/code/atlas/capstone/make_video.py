"""Cut the recorded Atlas session into the 3-5 minute demo video.

Inputs: capstone/video/raw.webm and marks.json from record_demo.py.
Narration is synthesized offline with Windows SAPI (System.Speech); captions and title
cards are drawn with ffmpeg. Long model waits are sped up so the video stays short.
Output: capstone/out/Trilok_Demo_TrilokKalani_Group_ITAI2376.mp4
"""

from __future__ import annotations

import json
import subprocess
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VID = ROOT / "video"
WORK = VID / "work"
OUT = ROOT / "out" / "Trilok_Demo_TrilokKalani_Group_ITAI2376.mp4"
FONT = "C\\:/Windows/Fonts/segoeui.ttf"
FONTB = "C\\:/Windows/Fonts/segoeuib.ttf"
W, H, FPS = 1280, 720, 30
SHIFT = -1.2  # the recording starts ~1.2 s before the script clock (measured at the Today-to-Staff switch)

# (start label, start offset, end label, end offset, max seconds or None, caption, narration)
SECTIONS = [
    ("today", 0, "staff", -1.5, None, "Today: the CEO and eleven specialists",
     "This is Atlas, running on my laptop against a demo database with made up projects. "
     "Today draws the twelve agents as a network: the CEO at the top and eleven specialists around it. "
     "Every glowing packet is a real hand-off or approval."),
    ("staff", 0, "typing:plan", -1.0, None, "Staff: one role, prompt, tool set and model tier per agent",
     "The Staff room lists each agent and what it has done. The CEO runs on the Opus tier, "
     "specialists on Sonnet, and cheap triage on Haiku."),
    ("typing:plan", 0, "sent:plan", 6, None, "Input: one request with three parts",
     "I give the CEO one request with three parts. It reasons about it and acts through tools, "
     "in a ReAct loop: think, call a tool, read the result, repeat."),
    ("sent:plan", 6, "done:plan", 0, 34, "Gated hand-off: the owner holds Authorize, then specialists run",
     "It puts the deadline on my Planner and decides the academic agent and the COO should each take a piece. "
     "Handing work to another agent is gated, so Atlas stops and puts an approval card on Today. "
     "Nothing moves until I hold Authorize. Then the specialists run and report back to the CEO. "
     "Each specialist runs its own loop with its own prompt and tools, every tool call is checked against "
     "the tool policy, and every action lands in the audit log."),
    ("done:plan", 0, "read:plan", 0, None, "Leader-based synthesis of the specialists' reports",
     "The CEO combines both reports into one answer, and says honestly what the specialists could not see. "
     "One agent owns the final word."),
    ("typing:money", 0, "read:money", 0, 26, "Safety: money movement is out of bounds",
     "Now safety. I ask Atlas to move five hundred dollars. Atlas has no tool that moves money, and transfers and "
     "payments are on the policy's deny list, so the CEO refuses plainly and tells me what I can do instead. "
     "The policy fails closed: a tool nobody has classified is refused, and payments can never be loosened to automatic."),
    ("typing:inject", 0, "read:inject", 0, 28, "Safety: a prompt injection inside an email",
     "Next, an instruction hidden in an email. Atlas treats outside text as data, not as instructions, "
     "and does not send my memory anywhere. This matters because Atlas reads mail and web pages. "
     "Outside text is labeled untrusted, and playbooks learned from the internet never reach a prompt."),
    ("typing:lesson", 0, "read:lesson", 0, 26, "Learning: feedback becomes a lesson",
     "Finally, feedback. I tell Atlas the checklist was useful and give it a lesson. It is stored in long term memory "
     "and recalled before similar work. Ratings, Accept or Rework on night tasks, and good and bad counts on every "
     "playbook are how Atlas improves its policy over time."),
    ("trace", 0, "memory", 0, None, "Transparency: the trace of one run",
     "Every run can be explained. This trace shows the planning run: the add task call, two hand-offs marked "
     "approved by me, the ledger guaranteeing each hand-off happens only once, and the cost, twelve cents."),
]
TITLE = ["Atlas", "A Multi-Agent Collaborative System", "ITAI 2376  |  AI Agent Creation capstone",
         "Group: Atlas", "Group member: Trilok Kalani"]
TITLE_SAY = ("Atlas, a multi-agent collaborative system. This is my I-T-A-I twenty-three seventy-six capstone, "
             "for group Atlas. Group member: Trilok Kalani.")
END = ["Atlas", "12 agents  |  one shared memory  |  a human approves every outward action",
       "Trilok Kalani  |  Group Atlas  |  ITAI 2376"]
END_SAY = "Twelve agents, one memory, and a person in charge of every outward action. Thanks for watching."


def run(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args], check=True)


VOICE = "en-US-AndrewMultilingualNeural"  # neural voice via edge-tts (pip install edge-tts)


def say(text: str, path: Path) -> float:
    """Synthesize narration with a neural voice and return its length in seconds."""
    import asyncio

    import edge_tts

    mp3 = path.with_suffix(".mp3")
    asyncio.run(edge_tts.Communicate(text, VOICE, rate="+0%").save(str(mp3)))
    run("-i", str(mp3), "-ar", "48000", "-ac", "1", str(path))
    with wave.open(str(path)) as w:
        return w.getnframes() / w.getframerate()


def esc(p: Path) -> str:
    return str(p).replace("\\", "/").replace(":", "\\:")


def card(lines: list[str], seconds: float, path: Path) -> None:
    filters = []
    for i, line in enumerate(lines):
        tf = WORK / f"{path.stem}_{i}.txt"
        tf.write_text(line, encoding="utf-8")
        size = 64 if i == 0 else 30
        y = 220 + (0 if i == 0 else 70 + 52 * i)
        font = FONTB if i == 0 else FONT
        filters.append(f"drawtext=fontfile='{font}':textfile='{esc(tf)}':fontcolor=0xdbe7f3:"
                       f"fontsize={size}:x=(w-text_w)/2:y={y}")
    run("-f", "lavfi", "-i", f"color=c=0x0b1018:s={W}x{H}:d={seconds}:r={FPS}",
        "-vf", ",".join(filters), "-pix_fmt", "yuv420p", str(path))


def main() -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    marks = json.loads((VID / "marks.json").read_text())
    t = {m["label"]: m["t"] for m in marks}  # first occurrence wins below
    first: dict[str, float] = {}
    for m in marks:
        first.setdefault(m["label"], m["t"])
    parts: list[Path] = []

    a = WORK / "title.wav"
    d = say(TITLE_SAY, a) + 1.5
    card(TITLE, d, WORK / "title_v.mp4")
    run("-i", str(WORK / "title_v.mp4"), "-i", str(a), "-af", "apad", "-t", f"{d}",
        "-c:v", "copy", "-c:a", "aac", "-ar", "48000", str(WORK / "p00.mp4"))
    parts.append(WORK / "p00.mp4")

    for i, (sl, so, el, eo, cap, caption, narration) in enumerate(SECTIONS, start=1):
        if sl not in first or el not in first:
            print("skip", sl, el)
            continue
        start, end = max(0.0, first[sl] + so + SHIFT), first[el] + eo + SHIFT
        dur = max(0.5, end - start)
        speed = dur / cap if cap and dur > cap else 1.0
        wav = WORK / f"n{i:02d}.wav"
        nd = say(narration, wav) + 0.6
        vid_len = dur / speed
        total = max(vid_len, nd)
        ctf = WORK / f"c{i:02d}.txt"
        ctf.write_text(caption + (f"   (sped up {speed:.0f}x)" if speed >= 1.5 else ""), encoding="utf-8")
        vf = (f"setpts=(PTS-STARTPTS)/{speed},fps={FPS},scale={W}:{H},"
              f"tpad=stop_mode=clone:stop_duration={max(0.0, total - vid_len) + 0.1},"
              f"drawbox=x=0:y=ih-58:w=iw:h=58:color=black@0.62:t=fill,"
              f"drawtext=fontfile='{FONT}':textfile='{esc(ctf)}':fontcolor=white:fontsize=26:x=28:y=h-44")
        out = WORK / f"p{i:02d}.mp4"
        run("-ss", f"{start}", "-t", f"{dur}", "-i", str(VID / "raw.webm"), "-i", str(wav),
            "-vf", vf, "-af", "apad", "-t", f"{total}", "-map", "0:v", "-map", "1:a",
            "-c:v", "libx264", "-preset", "medium", "-crf", "21", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ar", "48000", str(out))
        parts.append(out)

    a = WORK / "end.wav"
    d = say(END_SAY, a) + 1.5
    card(END, d, WORK / "end_v.mp4")
    run("-i", str(WORK / "end_v.mp4"), "-i", str(a), "-af", "apad", "-t", f"{d}",
        "-c:v", "copy", "-c:a", "aac", "-ar", "48000", str(WORK / "p99.mp4"))
    parts.append(WORK / "p99.mp4")

    lst = WORK / "list.txt"
    lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8")
    OUT.parent.mkdir(exist_ok=True)
    run("-f", "concat", "-safe", "0", "-i", str(lst), "-c:v", "libx264", "-preset", "medium", "-crf", "21",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "48000", "-movflags", "+faststart", str(OUT))
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                            str(OUT)], capture_output=True, text=True).stdout.strip()
    print(f"wrote {OUT} ({float(probe):.0f} s)")


if __name__ == "__main__":
    main()
