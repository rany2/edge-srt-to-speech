#!/usr/bin/env python3

import argparse
import asyncio
import logging
import os
import random
import re
import shutil
import subprocess
import tempfile

import edge_tts
import pysrt
import tqdm

logger = logging.getLogger(__name__)
FORMAT = (
    "[%(asctime)s %(filename)s->%(funcName)s():%(lineno)s]%(levelname)s: %(message)s"
)
logging.basicConfig(format=FORMAT)

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


def get_ssml_variables(text):
    result = re.findall("{[^}\s]+}", text)
    result = [x[1:-1] for x in result]
    result = list(dict.fromkeys(result))
    return result


def gen_from_template(text, variables):
    for k, v in variables.items():
        text = text.replace(f"{{{k}}}", str(v))
    return text


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
    logger.debug(f"Generating silence {out_file}...")
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
    logger.debug(f"Generated silence {out_file}")


async def audio_gen(queue, ssml_template, ssml_variables):
    retry_count = 0
    file_length = 0
    communicate = edge_tts.Communicate()
    arg = await queue.get()
    fname, text, duration, enhanced_srt = (
        arg["fname"],
        arg["text"],
        arg["duration"],
        arg["enhanced_srt"],
    )

    if enhanced_srt:
        text_ = text.split("\n")[-1]
        if text_.startswith("edge_tts{") and text_.endswith("}"):
            try:
                text_ = text_[len("edge_tts{") : -len("}")]
                text_ = text_.split(",")
                text_ = {k: v for k, v in [x.split(":") for x in text_]}
                for x in text_.keys():
                    if x not in ["rate", "pitch", "volume", "voice"] + ssml_variables:
                        raise Exception("edge_tts{} is invalid")
                for k, v in text_.items():
                    arg[k] = v
            except ValueError as e:
                text_ = {}
            text = "\n".join(text.split("\n")[:-1])
    text = " ".join(text.split("\n"))

    if ssml_template:
        arg["text"] = text
        text = gen_from_template(ssml_template, arg)

    with open(fname, "wb") as f:
        while True:
            logger.debug(f"Generating {fname}...")
            try:
                async for j in communicate.run(
                    text,
                    codec="audio-24khz-48kbitrate-mono-mp3",
                    pitch=arg["pitch"],
                    rate=arg["rate"],
                    volume=arg["volume"],
                    voice=arg["voice"],
                    boundary_type=1,
                    customspeak=ssml_template,
                ):
                    if j[2] is not None:
                        f.write(j[2])
            except Exception:
                if retry_count > 5:
                    raise Exception(f"Too many retries for {fname}")
                else:
                    retry_count += 1
                    f.seek(0)
                    f.truncate()
                    logger.debug(f"Retrying {fname}...")
                    await asyncio.sleep(retry_count + random.randint(1, 5))
            else:
                file_length = f.tell()
                break

    if file_length > 0:
        temporary_file = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        try:
            ensure_audio_length(fname, temporary_file.name, duration)
        finally:
            temporary_file.close()
            shutil.move(temporary_file.name, fname)
    else:
        silence_gen(fname, duration)

    queue.task_done()

    logger.debug(f"Generated {fname}")


async def _main(
    srt_data,
    voice,
    out_file,
    pitch,
    rate,
    volume,
    batch_size,
    enhanced_srt,
    ssml_template,
    ssml_default_variables,
):
    max_duration = pysrttime_to_seconds(srt_data[-1].end)

    input_files = []
    input_files_start_end = {}

    custom_ssml = ssml_template is not None
    ssml_variables = get_ssml_variables(ssml_template) if custom_ssml else []

    with tempfile.TemporaryDirectory() as temp_dir:
        args = []
        queue = asyncio.Queue()
        for i in range(len(srt_data)):
            logger.debug(f"Preparing {i}...")

            fname = os.path.join(temp_dir, f"{i}.mp3")
            input_files.append(fname)

            start = pysrttime_to_seconds(srt_data[i].start)
            end = pysrttime_to_seconds(srt_data[i].end)

            input_files_start_end[fname] = (start, end)

            duration = pysrttime_to_seconds(srt_data[i].duration)
            args.append(
                {
                    "fname": fname,
                    "text": srt_data[i].text,
                    "pitch": pitch,
                    "rate": rate,
                    "volume": volume,
                    "voice": voice,
                    "duration": duration,
                    "enhanced_srt": enhanced_srt,
                }
            )
            if ssml_default_variables:
                args[-1].update(ssml_default_variables)
        args_len = len(args)
        if logger.getEffectiveLevel() != logging.DEBUG:
            pdbar = tqdm.tqdm(total=args_len, desc="Generating audio")
        else:
            pdbar = None
        for i in range(0, args_len, batch_size):
            tasks = []
            for j in range(i, min(i + batch_size, args_len)):
                tasks.append(audio_gen(queue, ssml_template, ssml_variables))
                await queue.put(args[j])
            for f in asyncio.as_completed(tasks):
                await f
                if pdbar is not None:
                    pdbar.update()
        if pdbar is not None:
            pdbar.close()

        logger.debug("Generating silence and joining...")
        f = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False)
        if logger.getEffectiveLevel() != logging.DEBUG:
            pdbar = tqdm.tqdm(total=len(input_files), desc="Joining audio")
        else:
            pdbar = None
        try:
            last_end = 0
            for i in range(len(input_files)):
                start = input_files_start_end[input_files[i]][0]
                needed = start - last_end
                logger.debug(f"Needed {needed} seconds for {i}")
                if needed > 0.0001:
                    sfname = os.path.join(temp_dir, f"silence_{i}.mp3")
                    silence_gen(sfname, needed)
                    f.write(f"file '{sfname}'\n")
                    last_end += get_duration(sfname)
                f.write(f"file '{input_files[i]}'\n")
                last_end += get_duration(input_files[i])
                if pdbar is not None:
                    pdbar.update()

            x = get_duration(input_files[-1])
            y = input_files_start_end[input_files[i]][0] + x
            needed = max_duration - y
            if needed > 0:
                sfname = os.path.join(temp_dir, "silence_final.mp3")
                silence_gen(sfname, needed)
                f.write(f"file '{sfname}'\n")
            f.flush()
            f.close()

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
        finally:
            f.close()
            os.remove(f.name)
            if pdbar is not None:
                pdbar.close()
    logger.debug(f"Completed {out_file}")


def main():
    parser = argparse.ArgumentParser(description="Converts srt to mp3 using edge-tts")
    parser.add_argument("srt_file", help="srt file to convert")
    parser.add_argument("out_file", help="output file")
    parser.add_argument("--voice", help="voice name", default="en-US-SaraNeural")
    parser.add_argument("--ssml-template", help="custom ssml template")
    parser.add_argument(
        "--ssml-default-variables",
        help="default variables for variables in ssml template",
    )
    parser.add_argument("--parallel-batch-size", help="request batch size", default=50)
    parser.add_argument("--default-speed", help="default speed", default="+0%")
    parser.add_argument("--default-pitch", help="default pitch", default="+0Hz")
    parser.add_argument("--default-volume", help="default volume", default="+0%")
    parser.add_argument("--enable-debug", help="enable debug", action="store_true")
    parser.add_argument(
        "--disable-enhanced-srt",
        help="disable edge-tts specific customizations",
        action="store_true",
    )
    args = parser.parse_args()

    srt_data = parse_srt(args.srt_file)
    voice = args.voice
    out_file = args.out_file
    speed = args.default_speed
    pitch = args.default_pitch
    volume = args.default_volume
    batch_size = int(args.parallel_batch_size)
    enhanced_srt = not args.disable_enhanced_srt
    if args.ssml_template is not None:
        with open(args.ssml_template, "r") as f:
            ssml_template = f.read()
    ssml_default_variables = args.ssml_default_variables
    if batch_size < 1:
        raise Exception("parallel-batch-size must be greater than 0")

    if args.enable_debug:
        logger.setLevel(logging.DEBUG)

    if ssml_default_variables:
        ssml_default_variables = ssml_default_variables.split(",")
        ssml_default_variables = {
            k: v for k, v in [x.split(":") for x in ssml_default_variables]
        }

    asyncio.get_event_loop().run_until_complete(
        _main(
            srt_data=srt_data,
            voice=voice,
            out_file=out_file,
            rate=speed,
            pitch=pitch,
            volume=volume,
            batch_size=batch_size,
            enhanced_srt=enhanced_srt,
            ssml_template=ssml_template,
            ssml_default_variables=ssml_default_variables,
        )
    )


if __name__ == "__main__":
    main()
