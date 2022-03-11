#!/usr/bin/env python3

import argparse
import asyncio
import logging
import os
import shutil
import subprocess
import tempfile

import edge_tts
import pysrt

if shutil.which("ffmpeg") is None:
    print("ffmpeg is not installed")
    exit(1)

if shutil.which("ffprobe") is None:
    print("ffprobe (part of ffmpeg) is not installed")
    exit(1)


def parse_srt(srt_file):
    subs = pysrt.open(srt_file)
    return subs


def pysrttime_to_seconds(t):
    return (t.hours * 60 + t.minutes) * 60 + t.seconds + t.milliseconds / 1000


def get_duration(in_file):
    duration = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            in_file,
        ]
    ).decode("utf-8")
    return float(duration)


def ensure_audio_length(in_file, out_file, length):
    duration = get_duration(in_file)
    atempo = duration / length
    if atempo < 0.5:
        atempo = 0.5
    elif atempo > 100:
        atempo = 100
    if atempo > 1:
        process = subprocess.call(
            [
                "ffmpeg",
                "-y",
                "-i",
                in_file,
                "-filter:a",
                f"atempo={atempo}",
                out_file,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if process != 0:
            raise Exception("ffmpeg failed")
    else:
        shutil.copyfile(in_file, out_file)


def silence_gen(out_file, duration):
    logging.debug(f"Generating {out_file}...")
    process = subprocess.call(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"anullsrc=cl=mono:r=24000",
            "-t",
            str(duration),
            out_file,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if process != 0:
        raise Exception("ffmpeg failed")
    logging.debug(f"Generated {out_file}")


async def audio_gen(
    fname, communicate, text, pitch, rate, volume, voice_name, duration
):
    with open(fname, "wb") as f:
        logging.debug(f"Generating {fname}...")
        async for j in communicate.run(
            " ".join(text.split("\n")),
            codec="audio-24khz-48kbitrate-mono-mp3",
            pitch=pitch,
            rate=rate,
            volume=volume,
            voice=voice_name,
        ):
            if j[2] is not None:
                f.write(j[2])

        temporary_file = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        try:
            ensure_audio_length(fname, temporary_file.name, duration)
        finally:
            temporary_file.close()
            shutil.move(temporary_file.name, fname)
            temporary_file = None

    logging.debug(f"Generated {fname}")


async def _main(srt_data, voice_name, out_file, pitch, rate, volume):
    communicate = edge_tts.Communicate()

    max_duration = pysrttime_to_seconds(srt_data[-1].end)

    input_files = []
    input_files_start_end = {}

    with tempfile.TemporaryDirectory() as temp_dir:
        coros = []
        for i in range(len(srt_data)):
            logging.debug(f"Preparing {i}...")

            fname = os.path.join(temp_dir, f"{i}.mp3")
            input_files.append(fname)

            start = pysrttime_to_seconds(srt_data[i].start)
            end = pysrttime_to_seconds(srt_data[i].end)

            input_files_start_end[fname] = (start, end)

            duration = pysrttime_to_seconds(srt_data[i].duration)
            coros.append(
                audio_gen(
                    fname=fname,
                    communicate=communicate,
                    text=srt_data[i].text,
                    pitch=pitch,
                    rate=rate,
                    volume=volume,
                    voice_name=voice_name,
                    duration=duration,
                )
            )
        coros_len = len(coros)
        for i in range(0, coros_len, 100):
            await asyncio.gather(*coros[i : i + 100])

        logging.debug("Generating silence and joining...")
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as f:
            last_end = 0
            for i in range(len(input_files)):
                start = input_files_start_end[input_files[i]][0]
                needed = start - last_end
                logging.debug(f"Needed {needed} seconds for {i}")
                if needed > 0.0001:
                    sfname = os.path.join(temp_dir, f"silence_{i}.mp3")
                    silence_gen(sfname, needed)
                    f.write(f"file '{sfname}'\n")
                    last_end += get_duration(sfname)
                f.write(f"file '{input_files[i]}'\n")
                last_end += get_duration(input_files[i])

            x = get_duration(input_files[-1])
            y = input_files_start_end[input_files[i]][0] + x
            needed = max_duration - y
            if needed > 0:
                sfname = os.path.join(temp_dir, "silence_final.mp3")
                silence_gen(sfname, needed)
                f.write(f"file '{sfname}'\n")

            f.flush()
            process = subprocess.call(
                [
                    "ffmpeg",
                    "-y",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    f.name,
                    "-c",
                    "copy",
                    out_file,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if process != 0:
                raise Exception("ffmpeg failed")
    logging.debug(f"Completed {out_file}")


def main():
    parser = argparse.ArgumentParser(description="Converts srt to mp3 using edge-tts")
    parser.add_argument("srt_file", help="srt file to convert")
    parser.add_argument("out_file", help="output file")
    parser.add_argument("--voice", help="voice name", default="en-US-SaraNeural")
    parser.add_argument("--default-speed", help="default speed", default="+0%")
    parser.add_argument("--default-pitch", help="default pitch", default="+0Hz")
    parser.add_argument("--default-volume", help="default volume", default="+0%")
    parser.add_argument("--disable-debug", help="disable debug", action="store_true")
    args = parser.parse_args()

    srt_data = parse_srt(args.srt_file)
    voice_name = args.voice
    out_file = args.out_file
    speed = args.default_speed
    pitch = args.default_pitch
    volume = args.default_volume

    if not args.disable_debug:
        logging.basicConfig(level=logging.DEBUG)

    asyncio.get_event_loop().run_until_complete(
        _main(
            srt_data=srt_data,
            voice_name=voice_name,
            out_file=out_file,
            rate=speed,
            pitch=pitch,
            volume=volume,
        )
    )


if __name__ == "__main__":
    main()
