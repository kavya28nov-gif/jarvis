"""
voice_input.py
---------------
Records from mic and transcribes via Groq Whisper.
Stops recording automatically ~0.8 s after the user goes silent
instead of always waiting a fixed duration.
"""

import io
import os
import time
import wave
import logging

import numpy as np
import requests
import sounddevice as sd
from dotenv import load_dotenv

# Loaded here too so running this file standalone (the __main__ block
# below) still picks up .env -- main.py also calls this, but
# load_dotenv() is safe to call more than once.
load_dotenv()

logger = logging.getLogger("jarvis.voice_input")

SAMPLE_RATE    = 16000
CHANNELS       = 1
CHUNK          = 1600          # 100 ms per block
SILENCE_THRESH = 200           # RMS below this = silence (higher = ignores background music)
SILENCE_AFTER  = 0.8           # stop N seconds after speech ends
MAX_DURATION   = 10.0          # hard cap in seconds
PRE_ROLL_SECS  = 0.3           # keep N seconds before speech starts
MIN_VOICED_SECS = 0.35         # require this much actual voiced audio, or discard

# Whisper hallucinates these on silence/noise-only audio (learned from
# YouTube subtitle spam). If the transcript is exactly one of these,
# it's near-certainly noise, not the user.
HALLUCINATION_BLACKLIST = {
    "thank you", "thank you.", "thanks", "thank you so much",
    "thanks for watching", "thanks for watching!", "thank you for watching",
    "you", "bye", "bye.", ".", "the",
}

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GROQ_STT_URL = "https://api.groq.com/openai/v1/audio/transcriptions"


def listen(max_wait=None):
    """Record until silence, then transcribe. Returns text or None.
    `max_wait`: if speech hasn't STARTED within this many seconds, give
    up early instead of holding the mic open for the full MAX_DURATION
    -- used by follow-up mode, where silence just means "no follow-up"."""
    t_start = time.time()
    logger.info("Listening...")

    pre_roll_blocks = int(PRE_ROLL_SECS * SAMPLE_RATE / CHUNK)
    silence_blocks  = int(SILENCE_AFTER * SAMPLE_RATE / CHUNK)
    max_blocks      = int(MAX_DURATION  * SAMPLE_RATE / CHUNK)
    max_wait_blocks = int(max_wait * SAMPLE_RATE / CHUNK) if max_wait else None

    frames         = []
    ring           = []       # circular pre-roll buffer
    speech_started = False
    silent_count   = 0
    voiced_blocks  = 0        # blocks actually above the threshold
    stop_reason    = "max_duration"

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=CHANNELS,
                        dtype="int16", blocksize=CHUNK) as stream:
        for block_i in range(max_blocks):
            block, _ = stream.read(CHUNK)
            rms = float(np.sqrt(np.mean(block.astype(np.float32) ** 2)))

            if not speech_started and max_wait_blocks and block_i >= max_wait_blocks:
                stop_reason = "no_speech_within_max_wait"
                break

            if not speech_started:
                ring.append(block.copy())
                if len(ring) > pre_roll_blocks:
                    ring.pop(0)
                if rms > SILENCE_THRESH:
                    speech_started = True
                    frames.extend(ring)
                    frames.append(block.copy())
                    silent_count = 0
            else:
                frames.append(block.copy())
                if rms < SILENCE_THRESH:
                    silent_count += 1
                    if silent_count >= silence_blocks:
                        stop_reason = "silence"
                        break
                else:
                    voiced_blocks += 1
                    silent_count = 0

    t_recorded = time.time()
    record_secs = t_recorded - t_start

    if not speech_started or not frames:
        logger.info(f"Didn't catch that. (recorded {record_secs:.1f}s, no speech detected -- "
                    f"check mic input level / SILENCE_THRESH={SILENCE_THRESH})")
        return None

    # A blip of noise can cross the threshold once and trigger "speech",
    # but real words hold energy for a while. Sending near-silent audio
    # to Whisper makes it HALLUCINATE ("Thank you." etc.) -- discard it.
    min_voiced_blocks = int(MIN_VOICED_SECS * SAMPLE_RATE / CHUNK)
    if voiced_blocks < min_voiced_blocks:
        logger.info(f"Discarded: only {voiced_blocks} voiced block(s) -- noise blip, not speech.")
        return None

    logger.info(f"Recording done in {record_secs:.1f}s (stopped due to: {stop_reason})")

    audio = np.concatenate(frames, axis=0)

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(audio.tobytes())
    buf.seek(0)

    if not GROQ_API_KEY:
        logger.error("GROQ_API_KEY not set.")
        return None

    t_stt_start = time.time()
    try:
        response = requests.post(
            GROQ_STT_URL,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            files={"file": ("audio.wav", buf, "audio/wav")},
            data={"model": "whisper-large-v3-turbo", "language": "en"},
            timeout=30,
        )
        response.raise_for_status()
        stt_secs = time.time() - t_stt_start
        text = response.json().get("text", "").strip()
        if text and text.lower().strip() in HALLUCINATION_BLACKLIST:
            logger.info(f"Discarded Whisper hallucination: '{text}'")
            return None
        if text:
            logger.info(f"Heard: {text} (STT took {stt_secs:.1f}s, total {time.time() - t_start:.1f}s)")
            return text
        logger.info(f"Didn't catch that. (STT took {stt_secs:.1f}s, returned empty transcript)")
        return None

    except Exception as e:
        stt_secs = time.time() - t_stt_start
        logger.error(f"[whisper error] {e} (STT call took {stt_secs:.1f}s before failing)")
        return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = listen()
    print(f"Result: {result}")
