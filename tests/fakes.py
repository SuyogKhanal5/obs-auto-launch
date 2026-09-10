"""Lightweight stand-ins for the pieces of the obsws_python client surface that
autostart_script.py's OBS-facing functions call, so those functions can be exercised
without a live OBS instance. Mirrors the response shapes obsws_python actually returns
(snake_case attributes on a simple object) closely enough for our code paths -- not a
full reimplementation of the OBS WebSocket protocol.
"""

from types import SimpleNamespace

from obsws_python.error import OBSSDKRequestError

RESOURCE_NOT_FOUND_CODE = 600


class FakeObsError(Exception):
    """Stands in for obsws_python.error.OBSSDKRequestError."""


class FakeObsClient:
    def __init__(self, profile_name="Untitled", inputs=None, current_scene="Screen"):
        self.current_scene = current_scene
        self.profiles = {
            profile_name: {
                "video": SimpleNamespace(
                    fps_numerator=60, fps_denominator=1,
                    base_width=1920, base_height=1080,
                    output_width=1920, output_height=1080,
                ),
                "record_directory": r"C:\Videos",
                "params": {("Output", "Mode"): "Simple"},
            }
        }
        self.current_profile = profile_name
        # name -> {"kind": str, "tracks": {"1": bool, ..., "6": bool}}
        self.inputs = inputs or {}
        self.calls = []

    def _profile(self):
        return self.profiles[self.current_profile]

    # --- profiles ---
    def get_profile_list(self):
        return SimpleNamespace(profiles=list(self.profiles), current_profile_name=self.current_profile)

    def set_current_profile(self, name):
        if name not in self.profiles:
            raise FakeObsError(f"no such profile: {name}")
        self.current_profile = name
        self.calls.append(("set_current_profile", name))

    def create_profile(self, name):
        # Real OBS does NOT clone the active profile's settings when creating a new one --
        # it starts from defaults. Mirror that so tests catch code that assumes otherwise.
        self.profiles[name] = {
            "video": SimpleNamespace(
                fps_numerator=30, fps_denominator=1,
                base_width=1280, base_height=720,
                output_width=1280, output_height=720,
            ),
            "record_directory": r"C:\Users\Default\Videos",
            "params": {("Output", "Mode"): "Simple"},
        }
        self.current_profile = name
        self.calls.append(("create_profile", name))

    # --- profile parameters ---
    def get_profile_parameter(self, category, name):
        value = self._profile()["params"].get((category, name))
        return SimpleNamespace(parameter_value=value, parameter_default_value=None)

    def set_profile_parameter(self, category, name, value):
        self._profile()["params"][(category, name)] = value
        self.calls.append(("set_profile_parameter", category, name, value))

    # --- video settings ---
    def get_video_settings(self):
        return self._profile()["video"]

    def set_video_settings(self, numerator, denominator, base_width, base_height, out_width, out_height):
        self._profile()["video"] = SimpleNamespace(
            fps_numerator=numerator, fps_denominator=denominator,
            base_width=base_width, base_height=base_height,
            output_width=out_width, output_height=out_height,
        )
        self.calls.append(("set_video_settings", numerator, denominator, base_width, base_height, out_width, out_height))

    # --- recording directory ---
    def get_record_directory(self):
        return SimpleNamespace(record_directory=self._profile()["record_directory"])

    def set_record_directory(self, path):
        self._profile()["record_directory"] = path
        self.calls.append(("set_record_directory", path))

    # --- scenes ---
    def get_current_program_scene(self):
        return SimpleNamespace(current_program_scene_name=self.current_scene)

    # --- inputs ---
    def get_input_list(self, kind=None):
        inputs = [
            {"inputName": name, "inputKind": info["kind"], "unversionedInputKind": info["kind"]}
            for name, info in self.inputs.items()
            if kind is None or info["kind"] == kind
        ]
        return SimpleNamespace(inputs=inputs)

    def get_input_audio_tracks(self, name):
        if name not in self.inputs:
            raise FakeObsError(f"No source was found by the name of `{name}`")
        return SimpleNamespace(input_audio_tracks=dict(self.inputs[name]["tracks"]))

    def set_input_audio_tracks(self, name, track):
        if name not in self.inputs:
            raise FakeObsError(f"No source was found by the name of `{name}`")
        self.inputs[name]["tracks"] = dict(track)
        self.calls.append(("set_input_audio_tracks", name, dict(track)))

    def get_input_settings(self, name):
        if name not in self.inputs:
            raise OBSSDKRequestError(
                "GetInputSettings", RESOURCE_NOT_FOUND_CODE, f"No source was found by the name of `{name}`"
            )
        return SimpleNamespace(input_settings=dict(self.inputs[name].get("settings", {})))

    def set_input_settings(self, name, settings, overlay):
        if name not in self.inputs:
            raise OBSSDKRequestError(
                "SetInputSettings", RESOURCE_NOT_FOUND_CODE, f"No source was found by the name of `{name}`"
            )
        current = self.inputs[name].setdefault("settings", {})
        if overlay:
            current.update(settings)
        else:
            self.inputs[name]["settings"] = dict(settings)
        self.calls.append(("set_input_settings", name, dict(settings), overlay))

    def create_input(self, sceneName, inputName, inputKind, inputSettings, sceneItemEnabled):
        self.inputs[inputName] = {
            "kind": inputKind, "tracks": {str(i): False for i in range(1, 7)}, "settings": dict(inputSettings),
        }
        self.calls.append(("create_input", sceneName, inputName, inputKind, inputSettings))
        return SimpleNamespace(scene_item_id=len(self.inputs))

    def disconnect(self):
        self.calls.append(("disconnect",))
