"""The Doom watch frame endpoint authenticates and bounds each published frame."""
from __future__ import annotations

import http.client
import os
import subprocess
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

import yaml

from doom import watch_stream


class PlayInputTests(unittest.TestCase):
    """#102: POST /input only with PLAY_INPUT, the secret and the game's keys; xdotool only on :99 (WB07, WB08)."""
    TOKEN = "b" * 64

    def setUp(self):
        self.pressed = []
        self.keyboard = watch_stream.Keyboard(runner=lambda argv, env, **kw: self.pressed.append((argv, env)))
        self.server = watch_stream.http.server.ThreadingHTTPServer(("127.0.0.1", 0), watch_stream.Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close()))
        for item in (patch.object(watch_stream, "TOKEN", self.TOKEN), patch.object(watch_stream, "keyboard", self.keyboard),
                     patch.object(watch_stream, "PLAY", True)):
            item.start(); self.addCleanup(item.stop)

    def post(self, body: bytes, token=None):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        conn.request("POST", "/input", body=body, headers={"Authorization": "Bearer " + (token or self.TOKEN)})
        r = conn.getresponse(); r.read(); conn.close(); return r.status

    def test_keys_go_to_the_pods_display_only(self):
        self.assertEqual(self.post(b'{"down": ["ArrowUp", "Control"], "up": ["ArrowLeft"]}'), 204)
        argv, env = self.pressed[0]
        self.assertEqual(argv, ["xdotool", "keyup", "Left", "keydown", "Up", "keydown", "Control_L"])
        self.assertEqual(env["DISPLAY"], ":99")

    def test_no_console_no_enter_no_text_no_secret(self):
        for body in (b'{"down": ["`"]}', b'{"down": ["Enter"]}', b'{"down": ["a"]}', b'{"type": "quit"}', b'not json', b'{"down": "x"}'):
            self.assertEqual(self.post(body), 400, body)
        self.assertEqual(self.post(b'{"down": ["ArrowUp"]}', token="c" * 64), 401)
        self.assertEqual(self.pressed, [])

    def test_a_spectator_has_no_input(self):
        with patch.object(watch_stream, "PLAY", False):
            self.assertEqual(self.post(b'{"down": ["ArrowUp"]}'), 404)
        self.assertEqual(self.pressed, [])

    def test_too_many_keys_a_second(self):
        codes = [self.post(b'{"down": ["1"]}') for _ in range(watch_stream.INPUTS_PER_SECOND + 5)]
        self.assertIn(429, codes)


class WatchStreamTests(unittest.TestCase):
    TOKEN = "a" * 64
    JPEG = b"\xff\xd8test-frame\xff\xd9"

    def setUp(self):
        self.frames = watch_stream.Frames()
        self.frames.publish(self.JPEG)
        self.server = watch_stream.http.server.ThreadingHTTPServer(("127.0.0.1", 0), watch_stream.Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.patches = [patch.object(watch_stream, "frames", self.frames), patch.object(watch_stream, "TOKEN", self.TOKEN)]
        for item in self.patches:
            item.start()
        self.host, self.port = self.server.server_address

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, path="/frame", token=None):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=3)
        headers = {} if token is None else {"Authorization": "Bearer " + token}
        conn.request("GET", path, headers=headers)
        response = conn.getresponse()
        body = response.read()
        result = (response.status, response.getheader("Content-Type"), response.getheader("Cache-Control"), body)
        conn.close()
        return result

    def test_frame_requires_bearer_and_does_not_echo_secret(self):
        status, _, _, body = self.request(token="b" * 64)
        self.assertEqual((status, body), (401, b""))
        status, _, _, body = self.request(token=self.TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(body, self.JPEG)
        self.assertNotIn(self.TOKEN.encode(), body)

    def test_only_exact_frame_and_stream_paths_are_available(self):
        self.assertEqual(self.request("/frame?token=" + self.TOKEN, self.TOKEN)[0], 404)
        self.assertEqual(self.request("/health", self.TOKEN)[0], 404)

    def test_frame_publisher_discards_non_jpeg_and_oversized_data(self):
        sequence, frame = self.frames.after(0, 0)
        self.assertEqual((sequence, frame), (1, self.JPEG))
        self.frames.publish(b"not jpeg")
        self.frames.publish(b"\xff\xd8" + b"x" * (watch_stream.MAX_FRAME + 1) + b"\xff\xd9")
        self.assertEqual(self.frames.after(1, 0), (1, self.JPEG))

    def test_stream_is_authenticated_multipart_and_pushes_new_frames(self):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=3)
        conn.request("GET", "/stream", headers={"Authorization": "Bearer " + self.TOKEN})
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        self.assertTrue(response.getheader("Content-Type").startswith("multipart/x-mixed-replace; boundary=frame"))
        self.frames.publish(b"\xff\xd8next-frame\xff\xd9")
        data = response.read1(100)
        self.assertIn(b"--frame", data)
        response.close()
        conn.close()

    def test_watcher_pod_is_private_and_has_no_host_port(self):
        token = "c" * 64
        env = {**os.environ, "WATCH_TOKEN": token}
        proc = subprocess.run([str(Path(__file__).parent / "doom" / "pod.sh"), "watcher",
                              "localhost/emesh-doom:test", "romero", "carmack"],
                             env=env, capture_output=True, text=True, check=True)
        pod = yaml.safe_load(proc.stdout)
        self.assertEqual(pod["metadata"]["name"], "doom-watch-romero")
        self.assertFalse(pod["spec"]["automountServiceAccountToken"])
        container = pod["spec"]["containers"][0]
        self.assertEqual(container["command"][0], "/usr/local/bin/watch-client.sh")
        # the API takes a command of strings only: the port must not be a YAML number (DM12 failed on the lab for that)
        self.assertEqual(container["command"][1:], ["doom.play-romero.svc.cluster.local", "10666", "romero", "carmack"])

        self.assertEqual(container["ports"], [{"containerPort": 17777, "protocol": "TCP"}])
        self.assertTrue(all("hostPort" not in port for port in container["ports"]))
        self.assertEqual(container["env"], [{"name": "WATCH_TOKEN", "value": token}])
        self.assertEqual(pod["spec"]["securityContext"]["runAsUser"], 65534)

    def test_watcher_command_is_strings_whatever_the_names(self):
        env = {**os.environ, "WATCH_TOKEN": "c" * 64}
        proc = subprocess.run([str(Path(__file__).parent / "doom" / "pod.sh"), "watcher",
                              "localhost/emesh-doom:test", "romero", "no", "true"],
                             env=env, capture_output=True, text=True, check=True)
        command = yaml.safe_load(proc.stdout)["spec"]["containers"][0]["command"]
        self.assertEqual(command[1:], ["doom.play-romero.svc.cluster.local", "10666", "true", "no"])
        self.assertTrue(all(isinstance(item, str) for item in command))

    def test_watcher_generator_rejects_invalid_secret(self):
        env = {**os.environ, "WATCH_TOKEN": "not-a-token"}
        proc = subprocess.run([str(Path(__file__).parent / "doom" / "pod.sh"), "watcher",
                              "localhost/emesh-doom:test", "romero", "carmack"],
                             env=env, capture_output=True, text=True)
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("not-a-token", proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
