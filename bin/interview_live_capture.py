#!/usr/bin/env python3
"""Purpose: audio capture command building for interview-live (no subprocess
lifecycle — the coordinator owns the Popen).
Responsibilities:
  - Resolve mic/monitor devices (INTERVIEW_MIC/INTERVIEW_MONITOR or pactl)
  - Build the live capture argv: mic+monitor downmixed to mono L/R stereo
    (L=interviewer, R=guest) so whisper -di labels speakers 0/1
  - Build the replay argv from a meeting-rec two-stream file or a plain
    stereo file, decided by ffprobe stream counts
Rationale: canticos-9x1 — capturing and transcription are separate concerns;
  command building is pure and unit-testable, and swapping devices backends
  (PipeWire/pulse/others) touches only this module.
"""

import os
import subprocess
import sys


def resolve_devices(env=None):
    """Return (mic, monitor) from env overrides or pipewire defaults."""
    env = env if env is not None else os.environ
    mic = env.get("INTERVIEW_MIC")
    monitor = env.get("INTERVIEW_MONITOR")
    if mic and monitor:
        return mic, monitor
    try:
        mic = subprocess.run(["pactl", "get-default-source"], capture_output=True, text=True, check=True).stdout.strip()
        sink = subprocess.run(["pactl", "get-default-sink"], capture_output=True, text=True, check=True).stdout.strip()
        return mic, f"{sink}.monitor"
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        sys.stderr.write("interview-live: cannot resolve audio devices — set INTERVIEW_MIC/INTERVIEW_MONITOR\n")
        raise SystemExit(1) from e


def probe_streams(source, timeout=30):
    """Number of audio streams in a file (0 on probe failure)."""
    try:
        p = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=channels", "-of", "csv=p=0", str(source)],
            capture_output=True, text=True, timeout=timeout)
        return [ln for ln in p.stdout.splitlines() if ln.strip()]
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return []


def build_live_cmd(mic, monitor, out_path):
    """Live capture: two pulse inputs mixed to L/R stereo at 16 kHz.
    -y is required: ffmpeg exits instantly (with -nostdin it cannot prompt)
    when a previous session left capture.wav in the interview dir."""
    flt = ("[0:a]pan=mono|c0=c0[m0];[1:a]pan=mono|c0=c0[m1];"
           "[m0][m1]join=inputs=2:channel_layout=stereo[a]")
    return [
        "ffmpeg", "-hide_banner", "-nostats", "-loglevel", "warning", "-nostdin", "-y",
        "-f", "pulse", "-i", mic,
        "-f", "pulse", "-i", monitor,
        "-filter_complex", flt,
        "-map", "[a]", "-ar", "16000", str(out_path),
    ]


def probe_duration(path, timeout=30):
    """Audio duration in seconds (0.0 on probe failure)."""
    try:
        p = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=timeout)
        return float(p.stdout.strip())
    except (ValueError, subprocess.SubprocessError, OSError):
        return 0.0


def build_window_cmd(source, start_s, dur_s, out_path):
    """Slice one replay window [start_s, start_s+dur_s) from the converted
    file so --replay can stream it as if it had just been captured."""
    return [
        "ffmpeg", "-hide_banner", "-nostats", "-loglevel", "warning", "-y",
        "-ss", f"{start_s:.3f}", "-t", f"{dur_s:.3f}", "-i", str(source),
        "-vn", "-ac", "2", "-ar", "16000", str(out_path),
    ]


def build_replay_cmd(source, out_path):
    """Replay conversion. Two audio streams (meeting-rec track0=mic /
    track1=monitor) or one stereo stream (channel0=mic, channel1=monitor)."""
    streams = probe_streams(source)
    if len(streams) >= 2:
        flt = "[0:a:0]pan=mono|c0=c0[m0];[0:a:1]pan=mono|c0=c0[m1];[m0][m1]join=inputs=2:channel_layout=stereo[a]"
    else:
        flt = "[0:a]pan=mono|c0=c0[m0];[0:a]pan=mono|c0=c1[m1];[m0][m1]join=inputs=2:channel_layout=stereo[a]"
    return [
        "ffmpeg", "-hide_banner", "-nostats", "-loglevel", "warning", "-y",
        "-i", str(source),
        "-filter_complex", flt,
        "-map", "[a]", "-ar", "16000", str(out_path),
    ]