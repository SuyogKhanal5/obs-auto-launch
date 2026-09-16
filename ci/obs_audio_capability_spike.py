"""CROSS_PLATFORM_PLAN.md Phase 6 research spike -- NOT part of the shipped app.

Connects to a real, already-running, WebSocket-enabled OBS instance and dumps every input kind it
reports, plus the default settings schema for whichever ones look like a per-app/per-process audio
capture source. This is the empirical check the plan requires before implementing
configure_process_audio_capture for Linux/macOS: hardcoding a source-type string from
documentation alone risks silently mis-configuring OBS instead of failing cleanly, so this script's
real output (from the "OBS audio capability spike" CI job) is what actually decides that string,
not a guess.

Usage: python ci/obs_audio_capability_spike.py [host] [port] [password]
"""
import json
import sys
import time

import obsws_python as obsws

# Recognized names for the concept across ecosystems (PipeWire "node.name"/application capture,
# CoreAudio-based per-app capture, plus generic "process"/"application" wording some OBS builds
# or forks might use) -- deliberately broad so this doesn't miss a real kind by guessing too
# narrowly; the point of this script is to surface everything plausible for a human (or a future
# implementation) to read, not to make the final naming call itself.
CANDIDATE_NEEDLES = ("pipewire", "coreaudio", "core_audio", "core-audio", "app", "process", "capture")


def connect_with_retries(host, port, password, attempts=30, delay_seconds=2):
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            client = obsws.ReqClient(host=host, port=port, password=password, timeout=5)
            print(f"SPIKE: connected to OBS WebSocket at {host}:{port} on attempt {attempt}.")
            return client
        except Exception as exc:
            last_exc = exc
            time.sleep(delay_seconds)
    print(f"SPIKE: FAILED to connect to OBS WebSocket after {attempts} attempts: {last_exc}", file=sys.stderr)
    return None


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else "localhost"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 4455
    password = sys.argv[3] if len(sys.argv) > 3 else ""

    client = connect_with_retries(host, port, password)
    if client is None:
        sys.exit(1)

    try:
        version = client.get_version()
        print(f"SPIKE: OBS version {version.obs_version}, WebSocket protocol {version.obs_web_socket_version}")
    except Exception as exc:
        print(f"SPIKE: get_version failed (continuing anyway): {exc}", file=sys.stderr)

    try:
        kinds = client.get_input_kind_list().input_kinds
    except Exception as exc:
        print(f"SPIKE: FAILED get_input_kind_list: {exc}", file=sys.stderr)
        sys.exit(1)

    print("SPIKE: all input kinds reported by this OBS instance:")
    print(json.dumps(kinds, indent=2))

    candidates = [k for k in kinds if any(needle in k.lower() for needle in CANDIDATE_NEEDLES)]
    print("SPIKE: candidate per-app-audio-capture kinds (name match only, not confirmed correct):")
    print(json.dumps(candidates, indent=2))

    for kind in candidates:
        try:
            defaults = client.get_input_default_settings(kind).default_input_settings
            print(f"SPIKE: default settings schema for '{kind}':")
            print(json.dumps(defaults, indent=2))
        except Exception as exc:
            print(f"SPIKE: get_input_default_settings failed for '{kind}': {exc}", file=sys.stderr)

    if not candidates:
        print(
            "SPIKE: no candidate kind matched CANDIDATE_NEEDLES -- see the full kind list above "
            "and update CANDIDATE_NEEDLES or investigate manually.",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
