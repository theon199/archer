"""Explicitly synthetic adversarial stdio fixture; never a production connector."""

import json
import os
import signal
import sys
import time
from pathlib import Path

mode, pid_file = sys.argv[1:3]
Path(pid_file).write_text(str(os.getpid()))
if mode in {"ignore-term", "flood-ignore-term"}:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
if mode == "flood-ignore-term":
    while True:
        sys.stdout.buffer.write(b"SYNTHETIC_SECRET" + b"x" * (1024 * 1024))
        sys.stdout.buffer.flush()
if mode == "oversized":
    sys.stdout.buffer.write(b"SYNTHETIC_SECRET" + b"x" * (256 * 1024) + b"\n")
elif mode == "unterminated":
    sys.stdout.buffer.write(b"x" * (256 * 1024))
elif mode == "partial":
    sys.stdout.buffer.write(b'{"jsonrpc":"2.0","SYNTHETIC_SECRET":')
elif mode == "invalid-json":
    sys.stdout.buffer.write(b"SYNTHETIC_SECRET\n")
elif mode == "invalid-utf8":
    sys.stdout.buffer.write(b"\xff\xfe\n")
elif mode in {"frames", "bytes", "exact", "one-over"}:
    prefix = b'{"jsonrpc":"2.0","method":"fixture","params":{"value":"'
    suffix = b'"}}\n'
    size = 100 if mode == "frames" else 128 * 1024
    if mode == "one-over":
        size += 1
    frame = prefix + b"x" * (size - len(prefix) - len(suffix)) + suffix
    for _ in range(129 if mode == "frames" else 17 if mode == "bytes" else 1):
        sys.stdout.buffer.write(frame)
elif mode == "fragmented":
    frame = (
        json.dumps({"jsonrpc": "2.0", "method": "fixture", "params": {"value": "你好"}}, ensure_ascii=False) + "\n"
    ).encode()
    for byte in frame:
        sys.stdout.buffer.write(bytes([byte]))
        sys.stdout.buffer.flush()
        time.sleep(0.001)
sys.stdout.buffer.flush()
if mode in {"partial", "eof", "fragmented", "exact"}:
    raise SystemExit(0)
time.sleep(60)
