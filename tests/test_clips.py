"""Clips require independent replay and a decodable multi-frame animation."""

from dataclasses import replace
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
import zlib

import tests  # noqa: F401
from shakedown.clips import build_clip, _write_gif, UnsupportedFrame
from shakedown.config import load_config
from shakedown.orchestrate import run_case


ROOT = Path(__file__).resolve().parents[1]


def png(path, width, height, rgb, filter_byte=0):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    raw = b"".join(bytes([filter_byte]) + rgb[y * width * 3:(y + 1) * width * 3] for y in range(height))
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def decode_lzw(data, minimum):
    """Independent variable-width GIF decoder, including dictionary references."""
    clear, end = 1 << minimum, (1 << minimum) + 1
    table = {i: bytes([i]) for i in range(clear)}
    next_code, width, bit, previous = end + 1, minimum + 1, 0, None
    pixels = bytearray()
    while bit + width <= len(data) * 8:
        code = sum(((data[(bit + shift) // 8] >> ((bit + shift) % 8)) & 1) << shift
                   for shift in range(width))
        bit += width
        if code == clear:
            table = {i: bytes([i]) for i in range(clear)}
            next_code, width, previous = end + 1, minimum + 1, None
            continue
        if code == end:
            return bytes(pixels)
        if code in table:
            entry = table[code]
        elif code == next_code and previous:
            entry = previous + previous[:1]
        else:
            raise AssertionError(f"invalid LZW dictionary reference {code}")
        pixels.extend(entry)
        if previous is not None:
            table[next_code] = previous + entry[:1]
            next_code += 1
            if next_code == 1 << width and width < 12:
                width += 1
        previous = entry
    raise AssertionError("GIF image has no LZW end code")


def decode_gif(path):
    payload = path.read_bytes()
    assert payload[:6] == b"GIF89a"
    width, height, packed = struct.unpack_from("<HHB", payload, 6)
    offset = 13 + (3 * (2 << (packed & 7)) if packed & 128 else 0)
    frames, delays, delay = [], [], None

    def blocks(position):
        output = bytearray()
        while payload[position]:
            size = payload[position]
            output.extend(payload[position + 1:position + 1 + size])
            position += size + 1
        return bytes(output), position + 1

    while payload[offset] != 59:
        marker = payload[offset]
        offset += 1
        if marker == 33:
            label = payload[offset]
            offset += 1
            data, offset = blocks(offset)
            if label == 249:
                delay = struct.unpack_from("<H", data, 1)[0]
        elif marker == 44:
            _, _, frame_width, frame_height, flags = struct.unpack_from("<HHHHB", payload, offset)
            offset += 9
            assert (frame_width, frame_height) == (width, height)
            if flags & 128:
                offset += 3 * (2 << (flags & 7))
            minimum = payload[offset]
            offset += 1
            data, offset = blocks(offset)
            pixels = decode_lzw(data, minimum)
            assert len(pixels) == width * height
            frames.append(pixels)
            delays.append(delay)
        else:
            raise AssertionError(f"unexpected GIF marker {marker}")
    assert offset == len(payload) - 1
    return (width, height), frames, delays


class ClipTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.config = load_config(ROOT / "examples/tinyraid/shakedown.toml")
        self.config = replace(self.config, duration=1.2, raw={**self.config.raw, "clips": {
            "replay_args": ["--frame-dir", "{frame_dir}", "--frame-every", "6"],
            "fps": 10, "simulation_hz": 60}})

    def source(self, name="source", **overrides):
        folder = self.base / name
        metadata = run_case(self.config, folder, seed=7, map_name="yard", profile="cautious", **overrides)
        self.assertTrue(metadata["valid"], metadata)
        return folder

    def test_builtin_animation_pixels_and_delays_decode_independently(self):
        first, second = self.base / "first.png", self.base / "second.png"
        # More than 200 pixels exercises several clear codes and sub-blocks.
        png(first, 20, 20, bytes([255, 0, 0]) * 400)
        png(second, 20, 20, bytes([0, 255, 255]) * 400)
        target = self.base / "clip.gif"
        duration, encoded = _write_gif([(6, first), (12, second)], target, 60, 10)
        dimensions, frames, delays = decode_gif(target)
        self.assertEqual(dimensions, (20, 20))
        self.assertEqual(frames, [bytes([224]) * 400, bytes([31]) * 400])
        self.assertEqual(delays, [10, 10])
        self.assertEqual(duration, .2)
        self.assertEqual(encoded, 2)

    def test_sparse_capture_does_not_extend_past_the_requested_tail(self):
        source = self.source(duration=2)
        settings = {**self.config.raw["clips"], "replay_args": [
            "--frame-dir", "{frame_dir}", "--frame-every", "60"], "fps": 1}
        config = replace(self.config, raw={**self.config.raw, "clips": settings})
        result = build_clip(config, source, self.base / "sparse", seconds=1.5)
        self.assertEqual(result["status"], "created", result)
        _, frames, delays = decode_gif(Path(result["path"]))
        self.assertEqual(len(frames), 2)
        self.assertEqual(delays, [50, 100])
        self.assertEqual(sum(delays) / 100, 1.5)
        self.assertEqual(result["seconds"], 1.5)

    def test_centisecond_quantization_never_overshoots_a_non_aligned_window(self):
        first, second = self.base / "one.png", self.base / "two.png"
        png(first, 2, 2, bytes([0, 0, 0]) * 4)
        png(second, 2, 2, bytes([255, 255, 255]) * 4)
        target = self.base / "rounded.gif"
        duration, _ = _write_gif([(6, first), (12, second)], target, 60, 10,
                                 start_step=1, end_step=13)
        _, _, delays = decode_gif(target)
        self.assertEqual(delays, [8, 10])
        self.assertEqual(duration, .18)
        self.assertLessEqual(duration, (12 - 1) / 60)
        self.assertLessEqual(duration, (13 - 1) / 60)

    def test_real_input_replay_creates_only_the_requested_final_interval(self):
        source = self.source()
        result = build_clip(self.config, source, self.base / "clip", seconds=.5)
        self.assertEqual(result["status"], "created", result)
        dimensions, frames, delays = decode_gif(Path(result["path"]))
        self.assertEqual(dimensions, (384, 288))
        self.assertEqual(len(frames), 5)
        self.assertEqual(sum(delays) / 100, .5)
        self.assertEqual(result["frames"], 5)
        self.assertEqual(result["seconds"], .5)
        self.assertEqual(json.loads((self.base / "clip/clip.json").read_text()), result)
        replay = json.loads((self.base / "clip/replay/run.json").read_text())
        self.assertTrue(replay["valid"])

    def test_oracle_replay_can_create_a_clip(self):
        source = self.source(duration=14, extra_args=("--scenario", "stuck"))
        result = build_clip(self.config, source, self.base / "oracle-clip", seconds=.2)
        self.assertEqual(result["status"], "created", result)
        replay = json.loads((self.base / "oracle-clip/replay/run.json").read_text())
        self.assertEqual(replay["exit_code"], 4)

    def test_missing_recording_or_frame_configuration_is_unmeasured(self):
        missing = self.base / "missing"
        missing.mkdir()
        result = build_clip(self.config, missing, self.base / "without-inputs")
        self.assertEqual(result["status"], "unmeasured")
        config = replace(self.config, raw={key: value for key, value in self.config.raw.items() if key != "clips"})
        result = build_clip(config, missing, self.base / "without-config")
        self.assertEqual(result["status"], "unmeasured")

    def test_changed_valid_input_fails_replay_and_never_publishes_a_clip(self):
        source = self.source()
        data = json.loads((source / "inputs.json").read_text())
        data["inputs"][0]["input"]["move"] = [0, 1]
        (source / "inputs.json").write_text(json.dumps(data))
        result = build_clip(self.config, source, self.base / "bad-replay", seconds=.5)
        self.assertEqual(result["status"], "failed", result)
        self.assertIsNone(result["path"])
        self.assertFalse((self.base / "bad-replay/clip.gif").exists())

    def test_external_encoder_exit_zero_without_output_is_not_success(self):
        source = self.source()
        settings = {**self.config.raw["clips"], "encoder_command": ["{python}", "-c", "pass"]}
        config = replace(self.config, raw={**self.config.raw, "clips": settings})
        result = build_clip(config, source, self.base / "empty-encoder", seconds=.5)
        self.assertEqual(result["status"], "failed", result)

    def test_external_encoder_uses_shell_free_selected_frame_arguments(self):
        source = self.source()
        settings = {**self.config.raw["clips"], "output_name": "example.mp4", "encoder_command": [
            "{python}", "-c", "import pathlib,sys; assert len(list(pathlib.Path(sys.argv[1]).glob('*.png')))==5; pathlib.Path(sys.argv[2]).write_bytes(b'fixture encoder output')",
            "{frames_dir}", "{output_file}"]}
        config = replace(self.config, raw={**self.config.raw, "clips": settings})
        result = build_clip(config, source, self.base / "external", seconds=.5)
        self.assertEqual(result["status"], "created", result)
        self.assertEqual(Path(result["path"]).read_bytes(), b"fixture encoder output")
        self.assertIsNone(result["seconds"])
        self.assertFalse(result["duration_measured"])

    def test_unsupported_png_filter_is_explicit_and_leaves_no_partial_clip(self):
        first, second = self.base / "filtered-one.png", self.base / "filtered-two.png"
        png(first, 2, 2, bytes([0, 0, 0]) * 4, filter_byte=1)
        png(second, 2, 2, bytes([0, 0, 0]) * 4, filter_byte=1)
        with self.assertRaises(UnsupportedFrame):
            _write_gif([(6, first), (12, second)], self.base / "filter.gif", 60, 10)
        source = self.source()
        def unsupported(*arguments, **keywords):
            raise UnsupportedFrame("filter unsupported")
        with patch("shakedown.clips._write_gif", side_effect=unsupported):
            result = build_clip(self.config, source, self.base / "unsupported", seconds=.5)
        self.assertEqual(result["status"], "unmeasured")
        self.assertIn("unsupported", result["reason"])
        self.assertFalse((self.base / "unsupported/clip.gif").exists())

    def test_invalid_settings_and_existing_evidence_are_refused(self):
        for settings in ({"fps": True}, {"simulation_hz": 0}, {"simulation_hz": 10**400},
                         {"output_name": "../bad.gif"}, {"unknown": 1}):
            with self.subTest(settings=settings):
                config = replace(self.config, raw={**self.config.raw, "clips": settings})
                with self.assertRaises(ValueError):
                    build_clip(config, self.base, self.base / "invalid")
        existing = self.base / "existing"
        existing.mkdir()
        with self.assertRaises(FileExistsError):
            build_clip(self.config, self.base, existing)


if __name__ == "__main__":
    unittest.main()
