"""Replay-verified clips with an offline GIF encoder for tinyraid's PNG frames.

The built-in decoder deliberately accepts only RGB8, noninterlaced PNGs with
filter 0, the format emitted by the reference renderer. Other renderers can
supply an argument-array encoder instead. Formats follow the primary specs:
https://www.w3.org/Graphics/GIF/spec-gif89a.txt
https://www.w3.org/TR/png-3/
"""

from __future__ import annotations

import json
import math
from pathlib import Path
import shutil
import string
import struct
import sys
import zlib

from .config import ProjectConfig
from .orchestrate import child_environment, run_case, write_json
from .processes import run_process


class UnsupportedFrame(ValueError):
    """A valid frame needs an explicitly configured encoder."""


def _finite(value) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _load(path: Path):
    def reject(value):
        raise ValueError(f"nonfinite JSON value {value}")
    return json.loads(path.read_text(encoding="utf-8"), parse_constant=reject)


def _events(path: Path) -> list[dict]:
    rows = [_load_line(line) for line in path.read_text(encoding="utf-8").splitlines()]
    if (not rows or any(type(row) is not dict for row in rows)
            or rows[0].get("type") != "run_begin" or rows[-1].get("type") != "run_end"):
        raise ValueError("source or replay has incomplete game events")
    return [row for row in rows if row.get("type") != "decision"]


def _load_line(line: str):
    def reject(value):
        raise ValueError(f"nonfinite JSON value {value}")
    return json.loads(line, parse_constant=reject)


def _settings(config: ProjectConfig) -> dict:
    settings = config.raw.get("clips", {})
    allowed = {"replay_args", "encoder_command", "output_name", "fps", "simulation_hz", "frame_every"}
    if type(settings) is not dict or set(settings) - allowed:
        raise ValueError("unknown or malformed clips settings")
    for name in ("replay_args", "encoder_command"):
        value = settings.get(name)
        if value is not None and (type(value) is not list or not value
                                  or any(type(item) is not str or "\0" in item for item in value)):
            raise ValueError(f"clips.{name} must be a nonempty argument array")
    fps = settings.get("fps", 10)
    hz = settings.get("simulation_hz", 60)
    if type(fps) is not int or not 1 <= fps <= 100:
        raise ValueError("clips.fps must be an integer from 1 to 100")
    if not _finite(hz) or hz <= 0:
        raise ValueError("clips.simulation_hz must be finite and positive")
    if "frame_every" in settings and (type(settings["frame_every"]) is not int or settings["frame_every"] < 1):
        raise ValueError("clips.frame_every must be a positive integer")
    output = settings.get("output_name", "clip.gif")
    if (type(output) is not str or not output or output in {".", ".."}
            or any(character in output for character in "/\\:\0")
            or output.endswith((".", " "))):
        raise ValueError("clips.output_name must be a plain file name")
    if "encoder_command" not in settings and not output.lower().endswith(".gif"):
        raise ValueError("the built-in encoder writes .gif files")
    return {**settings, "fps": fps, "simulation_hz": hz, "output_name": output}


def _expand(arguments: list[str], values: dict) -> list[str]:
    result = []
    for argument in arguments:
        chunks = []
        for literal, field, spec, conversion in string.Formatter().parse(argument):
            chunks.append(literal)
            if field is not None:
                if field not in values or spec or conversion:
                    raise ValueError(f"unsupported clip placeholder: {field}")
                chunks.append(str(values[field]))
        result.append("".join(chunks))
    if not result or not result[0]:
        raise ValueError("empty clip command")
    return result


def _png_rgb(path: Path) -> tuple[int, int, bytes]:
    payload = path.read_bytes()
    if payload[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("invalid PNG signature")
    position, header, compressed, ended = 8, None, bytearray(), False
    while position < len(payload):
        if position + 12 > len(payload):
            raise ValueError("truncated PNG chunk")
        length = struct.unpack_from(">I", payload, position)[0]
        kind = payload[position + 4:position + 8]
        data = payload[position + 8:position + 8 + length]
        end = position + 12 + length
        if end > len(payload) or zlib.crc32(kind + data) != struct.unpack_from(">I", payload, end - 4)[0]:
            raise ValueError("invalid PNG chunk or checksum")
        if kind == b"IHDR":
            if header is not None or len(data) != 13:
                raise ValueError("invalid PNG header")
            header = struct.unpack(">IIBBBBB", data)
        elif kind == b"IDAT":
            compressed.extend(data)
        elif kind == b"IEND":
            ended = True
            if data or end != len(payload):
                raise ValueError("invalid PNG end")
            break
        position = end
    if header is None or not ended:
        raise ValueError("incomplete PNG")
    width, height, depth, color, compression, filtering, interlace = header
    if (depth, color, compression, filtering, interlace) != (8, 2, 0, 0, 0):
        raise UnsupportedFrame("built-in GIF supports noninterlaced RGB8 PNG frames only")
    if not 1 <= width <= 4096 or not 1 <= height <= 4096:
        raise UnsupportedFrame("PNG dimensions exceed the built-in clip limit")
    stride = width * 3
    expected = height * (stride + 1)
    decoder = zlib.decompressobj()
    raw = decoder.decompress(compressed, expected + 1)
    if len(raw) != expected or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
        raise ValueError("PNG scanline size mismatch")
    pixels = bytearray()
    for y in range(height):
        row = raw[y * (stride + 1):(y + 1) * (stride + 1)]
        if row[0] != 0:
            raise UnsupportedFrame("built-in GIF supports PNG filter 0 only; configure an encoder")
        pixels.extend(row[1:])
    return width, height, bytes(pixels)


def _literal_lzw(pixels: bytes) -> bytes:
    # Clear before 200 literals. The decoder never reaches 512 entries, so all
    # codes remain nine bits even for long frames; no hidden dictionary state.
    output = bytearray()
    bits = count = 0

    def emit(code):
        nonlocal bits, count
        bits |= code << count
        count += 9
        while count >= 8:
            output.append(bits & 255)
            bits >>= 8
            count -= 8

    for start in range(0, len(pixels), 200):
        emit(256)
        for value in pixels[start:start + 200]:
            emit(value)
    emit(257)
    if count:
        output.append(bits & 255)
    return bytes(output)


def _write_gif(frames: list[tuple[int, Path]], target: Path, hz: float, fps: int,
               *, start_step: float | None = None, end_step: int | None = None) -> tuple[float, int]:
    # A capture summarizes the interval ending at its step. The final capture
    # must not invent another full interval after the source has already ended.
    start = max(0, frames[0][0] - hz / fps) if start_step is None else start_step
    end = frames[-1][0] if end_step is None else end_step
    budget = math.floor(max(0, end - start) * 100 / hz)
    previous_step, total_delay, timed = start, 0, []
    for step, path in frames:
        if step <= previous_step or step > end:
            raise ValueError("clip frame steps must increase within the capture window")
        boundary = min(budget, math.floor((step - start) * 100 / hz))
        delay = boundary - total_delay
        if delay > 0:
            if delay > 65535:
                raise ValueError("clip frame delay exceeds GIF range")
            timed.append((path, delay))
            total_delay = boundary
        previous_step = step
    if len(timed) < 2:
        raise UnsupportedFrame("fewer than two frames have measurable duration inside the capture window")
    width, height, _ = _png_rgb(frames[0][1])
    palette = bytes(channel for index in range(256) for channel in (
        ((index >> 5) & 7) * 255 // 7, ((index >> 2) & 7) * 255 // 7, (index & 3) * 255 // 3))
    with target.open("wb") as stream:
        stream.write(b"GIF89a" + struct.pack("<HHBBB", width, height, 0xF7, 0, 0) + palette)
        stream.write(b"!\xff\x0bNETSCAPE2.0\x03\x01\x00\x00\x00")
        for path, delay in timed:
            frame_width, frame_height, rgb = _png_rgb(path)
            if (frame_width, frame_height) != (width, height):
                raise ValueError("clip frame dimensions changed")
            stream.write(b"!\xf9\x04\x04" + struct.pack("<H", delay) + b"\x00\x00")
            stream.write(b"," + struct.pack("<HHHHB", 0, 0, width, height, 0) + b"\x08")
            indexed = bytes(((rgb[i] >> 5) << 5) | ((rgb[i + 1] >> 5) << 2) | (rgb[i + 2] >> 6)
                            for i in range(0, len(rgb), 3))
            data = _literal_lzw(indexed)
            for offset in range(0, len(data), 255):
                block = data[offset:offset + 255]
                stream.write(bytes([len(block)]) + block)
            stream.write(b"\x00")
        stream.write(b";")
    return total_delay / 100, len(timed)


def build_clip(config: ProjectConfig, source_dir: Path, output_dir: Path, *, seconds: float = 10) -> dict:
    """Only create a clip after independent input replay matches source events."""
    settings = _settings(config)
    if not _finite(seconds) or seconds <= 0:
        raise ValueError("clip seconds must be finite and positive")
    source_dir, output_dir = Path(source_dir).resolve(), Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=False)
    result = {"v": 1, "status": "unmeasured", "path": None, "frames": 0,
              "seconds": 0, "reason": None, "replay_dir": str(output_dir / "replay")}
    try:
        if not settings.get("replay_args") or config.replay_command is None:
            result["reason"] = "frame replay arguments and input replay command are required"
            return result
        if not (source_dir / "inputs.json").is_file():
            result["reason"] = "source input recording is missing"
            return result
        source = _load(source_dir / "run.json")
        if (type(source) is not dict or source.get("valid") is not True
                or source.get("exit_code") not in {config.exit_codes["ok"], config.exit_codes["oracle"]}):
            raise ValueError("source execution is invalid")
        original_events = _events(source_dir / "telemetry.jsonl")
        expected_code = config.exit_codes["oracle"] if original_events[-1].get("outcome") == "oracle" else config.exit_codes["ok"]
        if (source["exit_code"] != expected_code or source.get("wall_timeout") is not False
                or source.get("result") != original_events[-1]):
            raise ValueError("source result or exit code disagrees with its game events")
        frame_dir = output_dir / "replay-frames"
        frame_dir.mkdir()
        selected_dir = output_dir / "frames"
        selected_dir.mkdir()
        target = output_dir / settings["output_name"]
        values = {"frame_dir": str(frame_dir), "frames_dir": str(frame_dir),
                  "output_file": str(target), "fps": settings["fps"], "seconds": seconds,
                  "python": sys.executable, "project_dir": str(config.root)}
        arguments = _expand(settings["replay_args"], values)
        replay = run_case(config, output_dir / "replay", seed=source["seed"], map_name=source["map"],
                          profile=source["profile"], duration=source.get("seconds", config.duration),
                          tuning=source.get("tuning", {}), replay_inputs=source_dir / "inputs.json",
                          extra_args=tuple(arguments))
        if (replay.get("valid") is not True
                or replay.get("exit_code") not in {config.exit_codes["ok"], config.exit_codes["oracle"]}
                or _events(output_dir / "replay" / "telemetry.jsonl") != original_events):
            raise ValueError("input replay did not reproduce the source game events")
        end_step = original_events[-1].get("step")
        if type(end_step) is not int or end_step < 0:
            raise ValueError("source has no final simulation step")
        candidates = sorted((int(path.stem), path) for path in frame_dir.glob("*.png") if path.stem.isdecimal())
        start_step = max(0, end_step - seconds * settings["simulation_hz"])
        frames = [(step, path) for step, path in candidates if start_step < step <= end_step]
        if len(frames) < 2:
            result["reason"] = "fewer than two frames were captured in the requested final interval"
            return result
        for number, (_, path) in enumerate(frames, 1):
            shutil.copyfile(path, selected_dir / f"{number:07d}.png")
        selected = {path for _, path in frames}
        for _, path in candidates:
            if path not in selected:
                path.unlink()
        result["frames"] = len(frames)
        if settings.get("encoder_command"):
            command = _expand(settings["encoder_command"], {**values, "frames_dir": str(selected_dir)})
            code, timed_out = run_process(command, cwd=output_dir, env=child_environment(config),
                                          stdout_path=output_dir / "encoder.log", timeout_s=config.wall_timeout)
            if code != 0 or timed_out or not target.is_file() or target.stat().st_size == 0:
                raise ValueError("configured encoder failed or produced no clip")
            # We verified replay and encoder completion, not the media duration
            # of an arbitrary external codec. Never infer it from frame count.
            duration = None
            result["duration_measured"] = False
            result["encoder_command"] = command
        else:
            duration, encoded = _write_gif(frames, target, settings["simulation_hz"], settings["fps"],
                                            start_step=start_step, end_step=end_step)
            result.update(frames=encoded, duration_measured=True)
        result.update(status="created", path=str(target), seconds=duration)
    except UnsupportedFrame as error:
        result.update(status="unmeasured", reason=str(error))
    except (OSError, ValueError, TypeError, KeyError, OverflowError, zlib.error, struct.error) as error:
        result.update(status="failed", reason=str(error))
    finally:
        if result["status"] != "created":
            candidate = output_dir / settings["output_name"]
            if candidate.is_file():
                candidate.unlink()
        write_json(output_dir / "clip.json", result)
    return result
