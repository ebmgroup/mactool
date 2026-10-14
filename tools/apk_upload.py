#!/usr/bin/env python3
"""Instagram-Release für den Rollout bereitstellen: APK in Teilen in den Bucket `apk`.

    tools/apk_upload.py <datei.apk> <release-name>

Supabase nimmt ~50 MB pro Objekt, deshalb Teile à 40 MB plus `manifest.json`
(Name, Größe, sha256 gesamt und je Teil). Die Macs holen es mit `apk-fetch`.
Zugangsdaten wie macctl.py: ~/.mactool/remote.json oder MACTOOL_SUPABASE_URL/KEY.
"""
import hashlib, json, os, re, sys, urllib.error, urllib.request
from pathlib import Path

PART = 40 * 1024 * 1024


def credentials():
    url, key = os.environ.get("MACTOOL_SUPABASE_URL"), os.environ.get("MACTOOL_SUPABASE_KEY")
    if not (url and key):
        data = json.loads((Path.home() / ".mactool" / "remote.json").read_text())
        url, key = data["url"], data["key"]
    return url.rstrip("/"), key


def upload(url, key, path, data, ctype):
    request = urllib.request.Request(
        f"{url}/storage/v1/object/apk/{path}", data=data, method="POST",
        headers={"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": ctype, "x-upsert": "true"})
    try:
        urllib.request.urlopen(request, timeout=900)
    except urllib.error.HTTPError as e:
        sys.exit(f"Upload {path} fehlgeschlagen: HTTP {e.code} {e.read()[:200]!r}")


def main():
    if len(sys.argv) != 3 or not re.match(r"^[\w.-]+$", sys.argv[2]):
        sys.exit(__doc__)
    source, release = Path(sys.argv[1]), sys.argv[2]
    url, key = credentials()
    data = source.read_bytes()
    parts = []
    for index, start in enumerate(range(0, len(data), PART)):
        chunk, name = data[start:start + PART], f"{release}/part{index:03d}"
        upload(url, key, name, chunk, "application/octet-stream")
        parts.append({"name": name, "bytes": len(chunk), "sha256": hashlib.sha256(chunk).hexdigest()})
        print(f"  {name}  {len(chunk) // 1024 // 1024} MB")
    manifest = {"file": source.name.replace(" ", "_"), "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(), "parts": parts}
    upload(url, key, f"{release}/manifest.json", json.dumps(manifest, indent=1).encode(), "application/json")
    print(f"Bereit: {release}  ->  macctl.py <mac> apk-fetch release={release}")


if __name__ == "__main__":
    main()
