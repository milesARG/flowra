"""Offline checks: text cleanup rules + (optional) GPU transcription of a wav.

Usage: python selftest.py [file.wav]
"""
import sys, time, wave
import numpy as np
import flowra

cfg = {**flowra.DEFAULT_CONFIG, "replacements": {"my email": "me@flowra.dev", "sign off": "Best,\nAlex"}}
cases = {
    "um so let's meet Tuesday, no wait, Wednesday at five": "So let's meet Wednesday at five",
    "hello team new paragraph the build is green": "Hello team\n\nThe build is green",
    "first point new line second point": "First point\nSecond point",
    "send it to my email please": "Send it to me@flowra.dev please",
    "is it done question mark": "Is it done?",
    "I'm sorry for the delay, I mean it": "I'm sorry for the delay, I mean it",
    "rename it to camel case user name.": "Rename it to userName.",
    "the constant is constant case max retry count": "The constant is MAX_RETRY_COUNT",
    "file called snake case my file name.": "File called my_file_name.",
    "kebab case main nav bar": "main-nav-bar",
    "groceries bullet point milk bullet point eggs": "Groceries\n- Milk\n- Eggs",
    "thanks for the help sign off": "Thanks for the help Best,\nAlex",
}
ok = True
for src, want in cases.items():
    got = flowra.tidy(src, cfg)
    ok &= got == want
    print("PASS" if got == want else "FAIL", repr(got), "" if got == want else f"(want {want!r})")
print("undo match:", bool(flowra.UNDO.match(" Scratch that.")))

if len(sys.argv) > 1:
    with wave.open(sys.argv[1]) as w:
        rate = w.getframerate()
        a = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768
    a = np.interp(np.linspace(0, len(a) - 1, int(len(a) * 16000 / rate)), np.arange(len(a)), a).astype(np.float32)
    live = flowra.read_config()
    tx = flowra.Transcriber(live)
    t = time.time(); tx.ensure_loaded(); print(f"load {time.time()-t:.1f}s")
    for label, audio in (("normal", a), ("whisper-quiet (x0.08)", a * 0.08)):
        t = time.time(); raw = tx.transcribe(audio)
        print(f"{label}: {time.time()-t:.2f}s -> {flowra.tidy(raw, live)!r}")
sys.exit(0 if ok else 1)
