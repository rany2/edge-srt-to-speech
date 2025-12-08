#!/usr/bin/env python3

import argparse
import asyncio
import logging
import os
import random
import shutil
import subprocess
import sys
import tempfile

import edge_tts
import pysrt
import tqdm

logger = logging.getLogger(__name__)
FORMAT = (
    "[%(asctime)s %(filename)s->%(funcName)s():%(lineno)s]%(levelname)s: %(message)s"
)
logging.basicConfig(format=FORMAT)


def dep_check():
    if not shutil.which("ffmpeg"):
        print("ffmpeg is not installed", file=sys.stderr)
        sys.exit(1)

    if not shutil.which("ffprobe"):
        print("ffprobe (part of ffmpeg) is not installed", file=sys.stderr)
        sys.exit(1)


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
        retcode = subprocess.call(
            [
                "ffmpeg",
                "-y",
                "-i",
                in_file,
                "-filter:a",
                f"atempo={atempo}",
                "-f",
                "wav",
                out_file,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if retcode != 0:
            raise subprocess.CalledProcessError(retcode, "ffmpeg")
    else:
        shutil.copyfile(in_file, out_file)


def mp3_to_wav(mp3_file, wav_file):
    """Convert MP3 file to WAV format"""
    retcode = subprocess.call(
        [
            "ffmpeg",
            "-y",
            "-i",
            mp3_file,
            "-f",
            "wav",
            wav_file,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if retcode != 0:
        raise subprocess.CalledProcessError(retcode, "ffmpeg")


def wav_to_mp3(wav_file, mp3_file):
    """Convert WAV file to MP3 format"""
    retcode = subprocess.call(
        [
            "ffmpeg",
            "-y",
            "-i",
            wav_file,
            "-c:a",
            "libmp3lame",
            mp3_file,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if retcode != 0:
        raise subprocess.CalledProcessError(retcode, "ffmpeg")


def silence_gen(out_file, duration):
    logger.debug("Generating silence %s...", out_file)
    retcode = subprocess.call(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=cl=mono:r=24000",
            "-t",
            str(duration),
            "-f",
            "wav",
            out_file,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if retcode != 0:
        raise subprocess.CalledProcessError(retcode, "ffmpeg")
    logger.debug("Generated silence %s", out_file)


def get_enhanced_srt_params(text, arg):
    text_ = text.split("\n")[-1]
    if text_.startswith("edge_tts{") and text_.endswith("}"):
        text_ = text_[len("edge_tts{") : -len("}")]
        text_ = text_.split(",")
        text_ = dict([x.split(":") for x in text_])
        for x in text_.keys():
            if x not in ["rate", "volume", "voice"]:
                raise ValueError("edge_tts{} is invalid")
        for k, v in text_.items():
            arg[k] = v
        return arg, "\n".join(text.split("\n")[:-1])
    return arg, text


async def audio_gen(queue):
    retry_count = 0
    retry_limit = 5
    file_length = 0
    arg = await queue.get()
    fname, text, duration, enhanced_srt = (
        arg["fname"],
        arg["text"],
        arg["duration"],
        arg["enhanced_srt"],
    )

    if enhanced_srt:
        arg, text = get_enhanced_srt_params(text, arg)
    text = " ".join(text.split("\n"))

    # Generate mp3 with edge-tts
    mp3_fname = fname
    wav_fname = fname.replace(".mp3", ".wav")

    while True:
        logger.debug("Generating %s...", mp3_fname)
        try:
            communicate = edge_tts.Communicate(
                text,
                rate=arg["rate"],
                volume=arg["volume"],
                voice=arg["voice"],
            )
            await communicate.save(mp3_fname)
        except edge_tts.exceptions.NoAudioReceived:
            with open(mp3_fname, "wb") as fobj:
                fobj.write(b"")
        except Exception as e:
            if retry_count > retry_limit:
                raise Exception(f"Too many retries for {mp3_fname}") from e
            retry_count += 1
            logger.debug("Retrying %s...", mp3_fname)
            await asyncio.sleep(retry_count + random.randint(1, 5))
            continue
        break

    file_length = os.path.getsize(mp3_fname)

    if file_length > 0:
        # Convert to WAV format for processing to avoid issues with MP3 concatenation
        # (see issue #38). Edge-TTS outputs MP3, so we convert to WAV, process, and convert back.
        mp3_to_wav(mp3_fname, wav_fname)
        # ensure length using wav
        temporary_file = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        try:
            ensure_audio_length(wav_fname, temporary_file.name, duration)
        finally:
            temporary_file.close()
            shutil.move(temporary_file.name, wav_fname)
        os.remove(mp3_fname)
    else:
        # Remove empty mp3 file
        if os.path.exists(mp3_fname):
            os.remove(mp3_fname)
        silence_gen(wav_fname, duration)

    queue.task_done()

    logger.debug("Generated %s", wav_fname)


async def _main(
    srt_data,
    voice,
    out_file,
    rate,
    volume,
    batch_size,
    enhanced_srt,
):
    if not srt_data or len(srt_data) == 0:
        raise ValueError("srt_data is empty")

    max_duration = pysrttime_to_seconds(srt_data[-1].end)

    input_files = []
    input_files_start_end = {}

    with tempfile.TemporaryDirectory() as temp_dir:
        args = []
        queue = asyncio.Queue()
        for i, j in enumerate(srt_data):
            logger.debug("Preparing %s...", i)

            fname = os.path.join(temp_dir, f"{i}.mp3")
            input_files.append(fname)

            start = pysrttime_to_seconds(j.start)
            end = pysrttime_to_seconds(j.end)

            input_files_start_end[fname] = (start, end)

            duration = pysrttime_to_seconds(j.duration)
            args.append(
                {
                    "fname": fname,
                    "text": j.text,
                    "rate": rate,
                    "volume": volume,
                    "voice": voice,
                    "duration": duration,
                    "enhanced_srt": enhanced_srt,
                }
            )
        args_len = len(args)
        if logger.getEffectiveLevel() != logging.DEBUG:
            pdbar = tqdm.tqdm(total=args_len, desc="Generating audio")
        else:
            pdbar = None
        for i in range(0, args_len, batch_size):
            tasks = []
            for j in range(i, min(i + batch_size, args_len)):
                tasks.append(audio_gen(queue))
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
            for i, j in enumerate(input_files):
                wav_file = j.replace(".mp3", ".wav")
                start = input_files_start_end[j][0]
                needed = start - last_end
                logger.debug("Needed %f seconds for %s", needed, i)
                if needed > 0.0001:
                    sfname = os.path.join(temp_dir, f"silence_{i}.wav")
                    silence_gen(sfname, needed)
                    f.write(f"file '{sfname}'\n")
                    last_end += get_duration(sfname)
                f.write(f"file '{wav_file}'\n")
                last_end += get_duration(wav_file)
                if pdbar is not None:
                    pdbar.update()

            last_file = input_files[-1]
            last_wav_file = last_file.replace(".mp3", ".wav")
            x = get_duration(last_wav_file)
            y = input_files_start_end[last_file][0] + x
            needed = max_duration - y
            if needed > 0:
                sfname = os.path.join(temp_dir, "silence_final.wav")
                silence_gen(sfname, needed)
                f.write(f"file '{sfname}'\n")
            f.flush()
            f.close()

            # Generate temporary WAV output
            temp_wav = out_file + ".temp.wav"
            retcode = subprocess.call(
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
                    temp_wav,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if retcode != 0:
                raise subprocess.CalledProcessError(retcode, "ffmpeg")

            wav_to_mp3(temp_wav, out_file)
        finally:
            f.close()
            os.remove(f.name)
            if os.path.exists(temp_wav):
                os.remove(temp_wav)
            if pdbar is not None:
                pdbar.close()
    logger.debug("Completed %s", out_file)


def main():
    dep_check()

    parser = argparse.ArgumentParser(description="Converts srt to mp3 using edge-tts")
    parser.add_argument("srt_file", help="srt file to convert")
    parser.add_argument("out_file", help="output file")
    parser.add_argument("--voice", help="voice name", default="en-US-AriaNeural")
    parser.add_argument("--parallel-batch-size", help="request batch size", default=50)
    parser.add_argument("--default-speed", help="default speed", default="+0%")
    parser.add_argument("--default-volume", help="default volume", default="+0%")
    parser.add_argument("--enable-debug", help="enable debug", action="store_true")
    parser.add_argument(
        "--disable-enhanced-srt",
        help="disable edge-tts specific customizations",
        action="store_true",
    )
    args = parser.parse_args()

    srt_data = pysrt.open(args.srt_file)
    voice = args.voice
    out_file = args.out_file
    speed = args.default_speed
    volume = args.default_volume
    batch_size = int(args.parallel_batch_size)
    enhanced_srt = not args.disable_enhanced_srt
    if batch_size < 1:
        raise Exception("parallel-batch-size must be greater than 0")

    if args.enable_debug:
        logger.setLevel(logging.DEBUG)

    asyncio.get_event_loop().run_until_complete(
        _main(
            srt_data=srt_data,
            voice=voice,
            out_file=out_file,
            rate=speed,
            volume=volume,
            batch_size=batch_size,
            enhanced_srt=enhanced_srt,
        )
    )


if __name__ == "__main__":
    main()
