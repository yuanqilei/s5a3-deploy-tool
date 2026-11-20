"""Build signed Release assets. Never upload the private key."""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from cryptography.hazmat.primitives import serialization
from self_update import REPO, verify, version, validate_file


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--key", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--notes-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    version(args.version)
    raw = args.exe.read_bytes()
    name = f"S5A3-ServerManager-{args.version}.exe"
    data = {"format": 1, "product": "s5a3-deploy-tool", "version": args.version,
            "url": f"https://github.com/{REPO}/releases/download/v{args.version}/{name}",
            "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
            "notes": args.notes_file.read_text(encoding="utf-8-sig")}
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    key = serialization.load_pem_private_key(args.key.read_bytes(), password=None)
    envelope = {"payload": base64.b64encode(payload).decode(),
                "signature": base64.b64encode(key.sign(payload)).decode()}
    verify(envelope)  # Refuse a signing key unrelated to the client trust root.
    validate_file(args.exe, data)
    args.output.mkdir(parents=True, exist_ok=False)
    shutil.copy2(args.exe, args.output / name)
    (args.output / "update.json").write_text(json.dumps(envelope), encoding="utf-8")
    (args.output / "SHA256SUMS.txt").write_text(f"{data['sha256']}  {name}\n", encoding="ascii")
    print(f"Signed update assets: {args.output}")


if __name__ == "__main__":
    main()
