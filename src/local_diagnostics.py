"""Private, bounded local logs; never persist credential values."""
from datetime import datetime
import os
from pathlib import Path
import re
import threading


def redact(message, secrets=()):
    text = str(message)
    for value in secrets:
        if isinstance(value, str) and len(value) >= 3:
            text = text.replace(value, "[REDACTED]")
    text = re.sub(r"(?i)(https?://)[^\s/@]+:[^\s/@]+@", r"\1[REDACTED]@", text)
    text = re.sub(r"(?i)(authorization\s*:\s*(?:bearer|basic)\s+)\S+", r"\1[REDACTED]", text)
    return re.sub(r'''(?i)((?:password|passwd|token|secret)["']?\s*[:=]\s*["']?)[^\s,"';}]+''', r"\1[REDACTED]", text)


class SessionLog:
    def __init__(self, directory=None):
        self.lock = threading.Lock()
        self.path = None
        self.directory = Path(directory) if directory else Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "S5A3" / "logs"
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            self.path = self.directory / (datetime.now().strftime("deploy-%Y%m%d-%H%M%S-") + str(os.getpid()) + ".log")
            self.path.touch(exist_ok=False)
        except OSError:
            self.path = None

    def write(self, message, secrets=()):
        clean = redact(message, secrets)
        if self.path:
            try:
                with self.lock:
                    if self.path.stat().st_size < 20 * 1024 * 1024:
                        with self.path.open("a", encoding="utf-8") as output:
                            output.write(datetime.now().isoformat(timespec="seconds") + " " + clean + "\n")
            except OSError:
                pass  # Logging failure must not interrupt a deployment.
        return clean
