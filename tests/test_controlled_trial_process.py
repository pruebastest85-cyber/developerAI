import json
import copy
import pickle
from dataclasses import asdict, fields
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from controlled_trial_process import (
    _COMMAND_FIELDS,
    _CommandAuthority,
    _ProcessIdentity,
    _ProcessReference,
    _StableRoot,
    _atomic_json,
    _command_auth,
    _current_user_sid,
    _path_identity,
    _read_json,
    _read_capability,
    _secure_private_path,
    _validate_command,
    _valid_status,
    _windows_security,
    TrialProcessClient,
    TrialProcessError,
    TrialProcessHandle,
    TrialProcessLauncher,
)


PLAN = {
    "schema_version": "1",
    "goal": "Validate lifecycle",
    "completed": False,
    "steps": [{
        "id": "patch_main",
        "tool": "patch_generator",
        "action": "generate_patch",
        "args": {
            "path": "main.py",
            "new_content": "def value():\n    return 2\n",
        },
        "goal": "Generate a reviewed patch",
        "depends_on": [],
        "justification": "Exercise the operational approval boundary.",
    }],
    "message": "One reviewed operation is proposed.",
}


class DelayedModelServer:
    def __init__(self, delay=0.5):
        self.delay = delay
        self.calls = 0
        self.received = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                self.rfile.read(length)
                owner.calls += 1
                owner.received.set()
                time.sleep(owner.delay)
                content = json.dumps(PLAN, separators=(",", ":"))
                body = json.dumps({
                    "id": "simulated-request",
                    "model": "simulated-model",
                    "choices": [{
                        "message": {"content": content},
                        "finish_reason": "stop",
                    }],
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                    },
                }, separators=(",", ":")).encode()
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    pass

            def log_message(self, _format, *_args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )

    @property
    def port(self):
        return self.server.server_address[1]

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def environment(server):
    values = dict(os.environ)
    values.update({
        "DEVELOPERAI_MODEL_PROVIDER": "lm_studio",
        "DEVELOPERAI_MODEL_BASE_URL": f"http://127.0.0.1:{server.port}/v1",
        "DEVELOPERAI_MODEL_NAME": "simulated-model",
        "DEVELOPERAI_MODEL_STRUCTURED_OUTPUT_MODE": "prompt_json",
        "DEVELOPERAI_MODEL_CONNECT_TIMEOUT": "2",
        "DEVELOPERAI_MODEL_READ_TIMEOUT": "5",
    })
    values.pop("DEVELOPERAI_MODEL_API_KEY", None)
    return values


class TrialFixture:
    def __init__(self):
        self.root = Path(tempfile.mkdtemp(
            prefix="developerai-real-trial-"
        )).resolve()
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "main.py").write_text(
            "def value():\n    return 1\n", encoding="utf-8"
        )

    def launch(self, server):
        started = time.monotonic()
        handle = TrialProcessLauncher.start(
            self.source,
            self.root,
            "Inspect the fixture without changing it.",
            environ=environment(server),
        )
        return handle, TrialProcessClient(handle), time.monotonic() - started


class ControlledTrialProcessTests(unittest.TestCase):
    def wait_state(self, client, expected, timeout=30):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            try:
                last = client.status(timeout=0.2)
            except TrialProcessError as error:
                if error.code == "client_timeout":
                    continue
                raise
            if last["state"] == expected:
                return last
            time.sleep(0.05)
        self.fail(f"state {expected!r} not reached; last={last!r}")

    def test_detached_owner_survives_initiator_and_keeps_one_session(self):
        fixture = TrialFixture()
        client = None
        try:
            with DelayedModelServer(delay=1.0) as server:
                handle, client, launch_seconds = fixture.launch(server)
                self.assertLess(launch_seconds, 0.8)
                planning = self.wait_state(client, "planning")
                self.assertEqual(planning["transport_calls"], 0)
                self.assertTrue(server.received.wait(20))
                with self.assertRaises(TrialProcessError) as premature:
                    client.command(
                        "approve_plan",
                        session_id=planning["session_id"],
                        owner_runtime_id=handle.owner_runtime_id,
                        workflow_runtime_id=None,
                        plan_id=planning["plan_id"],
                    )
                self.assertEqual(premature.exception.code, "invalid_command")
                with self.assertRaises(TrialProcessError) as timed_out:
                    client.wait_for_sequence(1, timeout=0.01)
                self.assertEqual(timed_out.exception.code, "client_timeout")
                self.assertTrue(os.path.exists(fixture.root))

                pending = self.wait_state(client, "pending_plan")
                self.assertEqual(server.calls, 1)
                self.assertEqual(pending["transport_calls"], 1)
                self.assertFalse(
                    (fixture.root / "owner-request.json").exists()
                )
                self.assertFalse((fixture.root / "owner-spec.json").exists())
                for ipc_name in (
                    "owner-status.json", "public-evidence.json",
                ):
                    self.assertNotIn(
                        "Inspect the fixture without changing it.",
                        (fixture.root / ipc_name).read_text(encoding="utf-8"),
                    )
                self.assertIsNotNone(pending["session_id"])
                self.assertIsNotNone(pending["plan_id"])
                self.assertIsNone(pending["workflow_runtime_id"])
                restored = client.public_view()
                self.assertEqual(restored.session_id, pending["session_id"])
                self.assertEqual(restored.plan_id, pending["plan_id"])
                self.assertFalse(hasattr(restored, "execute"))

                command_path = fixture.root / "owner-command.json"
                command_path.write_text(
                    '{"version":1,"unexpected":true}', encoding="utf-8"
                )
                deadline = time.monotonic() + 2
                rejected = None
                while time.monotonic() < deadline:
                    rejected = client.status()
                    if rejected["error_code"] == "invalid_status":
                        break
                    time.sleep(0.05)
                self.assertEqual(rejected["state"], "pending_plan")
                self.assertEqual(rejected["error_code"], "invalid_status")
                self.assertFalse(command_path.exists())

                for field, replacement in (
                    ("owner_runtime_id", "otr_" + "0" * 32),
                    ("session_id", "wrong-session"),
                    ("plan_id", "mp1_" + "0" * 64),
                ):
                    arguments = {
                        "session_id": pending["session_id"],
                        "owner_runtime_id": handle.owner_runtime_id,
                        "workflow_runtime_id": None,
                        "plan_id": pending["plan_id"],
                    }
                    arguments[field] = replacement
                    with self.subTest(field=field):
                        with self.assertRaises(TrialProcessError) as caught:
                            client.command("approve_plan", **arguments)
                        self.assertEqual(caught.exception.code, "stale_command")

                sequence = client.command(
                    "approve_plan",
                    session_id=pending["session_id"],
                    owner_runtime_id=handle.owner_runtime_id,
                    workflow_runtime_id=None,
                    plan_id=pending["plan_id"],
                )
                paused = client.wait_for_sequence(sequence, timeout=5)
                self.assertEqual(paused["state"], "awaiting_approval")
                self.assertEqual(paused["session_id"], pending["session_id"])
                self.assertIsNotNone(paused["workflow_runtime_id"])
                self.assertIsNotNone(paused["approval_request_id"])
                self.assertEqual(paused["transport_calls"], 1)
                self.assertEqual(server.calls, 1)
                self.assertEqual(
                    (fixture.source / "main.py").read_text(encoding="utf-8"),
                    "def value():\n    return 1\n",
                )
                with self.assertRaises(TrialProcessError) as duplicate:
                    client.command(
                        "approve_plan",
                        session_id=paused["session_id"],
                        owner_runtime_id=handle.owner_runtime_id,
                        workflow_runtime_id=paused["workflow_runtime_id"],
                        plan_id=paused["plan_id"],
                        sequence=sequence,
                    )
                self.assertEqual(
                    duplicate.exception.code, "duplicate_command"
                )
        finally:
            if client is not None and fixture.root.exists():
                client.terminate_and_cleanup()
            elif fixture.root.exists():
                import shutil
                shutil.rmtree(fixture.root)
        self.assertFalse(fixture.root.exists())

    def test_missing_and_corrupt_evidence_never_restore_authority(self):
        fixture = TrialFixture()
        client = None
        try:
            with DelayedModelServer() as server:
                _, client, _ = fixture.launch(server)
                pending = self.wait_state(client, "pending_plan")
                evidence = fixture.root / "public-evidence.json"
                original = evidence.read_bytes()
                evidence.unlink()
                with self.assertRaises(TrialProcessError) as missing:
                    client.public_view()
                self.assertEqual(
                    missing.exception.code, "evidence_unavailable"
                )
                evidence.write_text("{broken", encoding="utf-8")
                with self.assertRaises(TrialProcessError) as corrupt:
                    client.public_view()
                self.assertEqual(
                    corrupt.exception.code, "evidence_unavailable"
                )
                evidence.unlink()
                _atomic_json(
                    evidence,
                    json.loads(original.decode("utf-8")),
                    client._root_guard,
                )
                self.assertEqual(
                    client.public_view().session_id, pending["session_id"]
                )
        finally:
            if client is not None and fixture.root.exists():
                client.terminate_and_cleanup()

    def test_dead_owner_is_lost_and_public_evidence_cannot_continue(self):
        fixture = TrialFixture()
        client = None
        try:
            with DelayedModelServer() as server:
                handle, client, _ = fixture.launch(server)
                pending = self.wait_state(client, "pending_plan")
                os.kill(handle.pid, signal.SIGTERM)
                deadline = time.monotonic() + 3
                lost = None
                while time.monotonic() < deadline:
                    lost = client.status()
                    if lost["state"] == "lost":
                        break
                    time.sleep(0.05)
                self.assertEqual(lost["state"], "lost")
                self.assertEqual(
                    client.public_view().session_id, pending["session_id"]
                )
                with self.assertRaises(TrialProcessError):
                    client.command(
                        "approve_plan",
                        session_id=pending["session_id"],
                        owner_runtime_id=handle.owner_runtime_id,
                        workflow_runtime_id=None,
                        plan_id=pending["plan_id"],
                    )
                self.assertEqual(server.calls, 1)
        finally:
            if client is not None and fixture.root.exists():
                client.terminate_and_cleanup()

    def test_owner_terminated_before_pending_has_no_evidence(self):
        fixture = TrialFixture()
        client = None
        try:
            with DelayedModelServer(delay=2.0) as server:
                handle, client, _ = fixture.launch(server)
                self.wait_state(client, "planning")
                self.assertTrue(server.received.wait(20))
                os.kill(handle.pid, signal.SIGTERM)
                with self.assertRaises(TrialProcessError):
                    client.public_view()
                self.assertLessEqual(server.calls, 1)
        finally:
            if client is not None and fixture.root.exists():
                client.terminate_and_cleanup()

    def test_owner_survives_a_completely_separate_initiator(self):
        fixture = TrialFixture()
        client = None
        script = (
            "import json,sys;"
            "from controlled_trial_process import TrialProcessLauncher;"
            "p=json.load(sys.stdin);"
            "h=TrialProcessLauncher.start("
            "p['source'],p['root'],p['request']);"
            "json.dump({'root':str(h.root),'owner':h.owner_runtime_id,"
            "'pid':h.pid,'creation':h.process_identity.creation_time,"
            "'executable':h.process_identity.executable,"
            "'root_identity':list(h.root_identity)},sys.stdout)"
        )
        try:
            with DelayedModelServer(delay=1.0) as server:
                started = time.monotonic()
                initiator = subprocess.run(
                    [sys.executable, "-c", script],
                    input=json.dumps({
                        "source": str(fixture.source),
                        "root": str(fixture.root),
                        "request": "Inspect without changing files.",
                    }),
                    text=True,
                    capture_output=True,
                    check=True,
                    cwd=str(Path(__file__).resolve().parents[1]),
                    env=environment(server),
                    timeout=5,
                )
                self.assertLess(time.monotonic() - started, 0.8)
                self.assertEqual(initiator.returncode, 0)
                public = json.loads(initiator.stdout)
                handle = TrialProcessHandle(
                    Path(public["root"]),
                    public["owner"],
                    public["pid"],
                    _ProcessIdentity(
                        public["pid"],
                        public["creation"],
                        public["executable"],
                    ),
                    tuple(public["root_identity"]),
                )
                client = TrialProcessClient(handle)
                with self.assertRaises(TrialProcessError) as timed_out:
                    client.status(timeout=0.01)
                self.assertEqual(timed_out.exception.code, "client_timeout")
                pending = self.wait_state(client, "pending_plan")
                self.assertEqual(server.calls, 1)
                self.assertEqual(pending["transport_calls"], 1)
                sequence = client.command(
                    "approve_plan",
                    session_id=pending["session_id"],
                    owner_runtime_id=handle.owner_runtime_id,
                    workflow_runtime_id=None,
                    plan_id=pending["plan_id"],
                )
                paused = client.wait_for_sequence(sequence, timeout=5)
                self.assertEqual(paused["state"], "awaiting_approval")
                self.assertEqual(
                    paused["session_id"], pending["session_id"]
                )
                self.assertEqual(server.calls, 1)
        finally:
            if client is not None and fixture.root.exists():
                client.terminate_and_cleanup()


class ControlledTrialProcessSecurityTests(unittest.TestCase):
    def status(self):
        return {
            "version": 1,
            "owner_runtime_id": "otr_" + "1" * 32,
            "state": "pending_plan",
            "pid": 123,
            "heartbeat_ns": 1,
            "session_id": "session",
            "workflow_runtime_id": None,
            "plan_id": "plan",
            "approval_request_id": None,
            "error_code": None,
            "evidence_path": "unused",
            "last_sequence": 0,
            "transport_calls": 1,
        }

    def command(self, capability):
        value = {
            "version": 1,
            "sequence": 1,
            "owner_runtime_id": "otr_" + "1" * 32,
            "session_id": "session",
            "workflow_runtime_id": None,
            "plan_id": "plan",
            "expected_state": "pending_plan",
            "action": "approve_plan",
            "request_id": None,
            "auth": None,
        }
        value["auth"] = _command_auth(capability, value)
        return value

    def test_command_requires_independent_capability_and_exact_version(self):
        capability = b"a" * 32
        authority = _CommandAuthority(capability)
        command = self.command(capability)
        _validate_command(command, self.status(), authority)
        for invalid in (None, "", "0" * 64):
            with self.subTest(auth=invalid):
                altered = dict(command, auth=invalid)
                with self.assertRaises(TrialProcessError):
                    _validate_command(altered, self.status(), authority)
        with self.assertRaises(TrialProcessError):
            _validate_command(
                command, self.status(), _CommandAuthority(b"b" * 32)
            )
        for version in (True, False, "1", -1):
            with self.subTest(version=version):
                altered = dict(command, version=version)
                altered["auth"] = _command_auth(capability, altered)
                with self.assertRaises(TrialProcessError):
                    _validate_command(altered, self.status(), authority)

    def test_authenticated_command_rejects_any_post_signature_change(self):
        capability = b"a" * 32
        authority = _CommandAuthority(capability)
        original = self.command(capability)
        for field, value in (
            ("owner_runtime_id", "otr_" + "2" * 32),
            ("session_id", "other"),
            ("plan_id", "other"),
            ("sequence", 2),
            ("action", "reject_plan"),
            ("expected_state", "awaiting_approval"),
        ):
            with self.subTest(field=field):
                altered = dict(original, **{field: value})
                with self.assertRaises(TrialProcessError) as caught:
                    _validate_command(altered, self.status(), authority)
                self.assertEqual(caught.exception.code, "invalid_command")

    def test_expected_state_is_mandatory_signed_and_live(self):
        capability = b"a" * 32
        authority = _CommandAuthority(capability)
        command = self.command(capability)
        _validate_command(command, self.status(), authority)
        unsigned_change = dict(command, expected_state="awaiting_approval")
        with self.assertRaises(TrialProcessError) as changed:
            _validate_command(unsigned_change, self.status(), authority)
        self.assertEqual(changed.exception.code, "invalid_command")
        stale = dict(command, expected_state="awaiting_approval", auth=None)
        stale["auth"] = authority.sign(stale)
        with self.assertRaises(TrialProcessError) as mismatch:
            _validate_command(stale, self.status(), authority)
        self.assertEqual(mismatch.exception.code, "stale_command")
        missing = dict(command)
        del missing["expected_state"]
        with self.assertRaises(TrialProcessError):
            _validate_command(missing, self.status(), authority)

    def test_public_handle_has_no_authority_and_client_is_opaque(self):
        public_names = {item.name for item in fields(TrialProcessHandle)}
        self.assertNotIn("capability", public_names)
        handle = TrialProcessHandle(
            Path("root"),
            "otr_" + "1" * 32,
            1,
            _ProcessIdentity(1, 2, "python"),
            (3, 4),
        )
        self.assertNotIn("capability", asdict(handle))
        authority = _CommandAuthority(b"a" * 32)
        for operation in (
            lambda: copy.copy(authority),
            lambda: copy.deepcopy(authority),
            lambda: pickle.dumps(authority),
        ):
            with self.assertRaises(TypeError):
                operation()
        self.assertNotIn((b"a" * 32).hex(), repr(authority))

    def test_two_trials_have_distinct_non_transferable_capabilities(self):
        first = b"a" * 32
        second = b"b" * 32
        self.assertNotEqual(first, second)
        command = self.command(first)
        with self.assertRaises(TrialProcessError):
            _validate_command(
                command, self.status(), _CommandAuthority(second)
            )

    def test_status_rejects_bool_security_integers(self):
        root = Path(tempfile.mkdtemp(
            prefix="developerai-real-trial-"
        )).resolve()
        try:
            value = dict(
                self.status(),
                pid=os.getpid(),
                evidence_path=str(root / "public-evidence.json"),
            )
            self.assertTrue(_valid_status(value, root))
            for field in (
                "version", "pid", "heartbeat_ns", "last_sequence",
                "transport_calls",
            ):
                with self.subTest(field=field):
                    self.assertFalse(
                        _valid_status(dict(value, **{field: True}), root)
                    )
        finally:
            import shutil
            shutil.rmtree(root)

    def test_private_acl_and_capability_are_not_public(self):
        fixture = TrialFixture()
        client = None
        try:
            with DelayedModelServer() as server:
                handle, client, _ = fixture.launch(server)
                pending = ControlledTrialProcessTests().wait_state(
                    client, "pending_plan"
                )
                secret = _read_capability(client._root_guard)
                for operation in (
                    lambda: copy.copy(client),
                    lambda: copy.deepcopy(client),
                    lambda: pickle.dumps(client),
                ):
                    with self.assertRaises(TypeError):
                        operation()
                self.assertNotIn("capability", repr(handle))
                if os.name == "nt":
                    acl = subprocess.run(
                        ["icacls", str(fixture.root)],
                        check=True,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                    ).stdout
                    self.assertNotIn("CodexSandboxUsers", acl)
                    self.assertNotIn("(I)", acl)
                for name in ("owner-status.json", "public-evidence.json"):
                    self.assertNotIn(
                        secret.hex(),
                        (fixture.root / name).read_text(encoding="utf-8"),
                    )
                    if os.name == "nt":
                        owner, protected, entries = _windows_security(
                            fixture.root / name
                        )
                        self.assertTrue(owner)
                        self.assertTrue(protected)
                        self.assertEqual(
                            {entry[3] for entry in entries},
                            {
                                _current_user_sid(),
                                "S-1-5-18",
                                "S-1-5-32-544",
                            },
                        )
                        file_acl = subprocess.run(
                            ["icacls", str(fixture.root / name)],
                            check=True,
                            capture_output=True,
                            text=True,
                            encoding="utf-8",
                            errors="replace",
                        ).stdout
                        self.assertNotIn("CodexSandboxUsers", file_acl)
                        self.assertNotIn("(I)", file_acl)
                self.assertEqual(pending["transport_calls"], 1)
        finally:
            if client is not None and fixture.root.exists():
                client.terminate_and_cleanup()

    def test_atomic_json_uses_random_exclusive_temp_and_rejects_destination_type(self):
        root = Path(tempfile.mkdtemp(
            prefix="developerai-real-trial-"
        )).resolve()
        _secure_private_path(root, directory=True)
        root_guard = _StableRoot(root, _path_identity(root))
        try:
            destination = root / "owner-status.json"
            destination.mkdir()
            with self.assertRaises(TrialProcessError):
                _atomic_json(
                    destination, {"safe": True}, root_guard,
                )
            self.assertTrue(destination.is_dir())
            self.assertEqual(
                list(root.glob(".developerai-ipc-*.tmp")), []
            )
        finally:
            root_guard.close()
            import shutil
            shutil.rmtree(root)

    def test_cleanup_rejects_process_identity_mismatch_without_signal(self):
        fixture = TrialFixture()
        client = None
        try:
            with DelayedModelServer() as server:
                handle, client, _ = fixture.launch(server)
                ControlledTrialProcessTests().wait_state(
                    client, "pending_plan"
                )
                wrong = _ProcessIdentity(
                    handle.pid,
                    handle.process_identity.creation_time + 1,
                    handle.process_identity.executable,
                )
                reference = mock.Mock()
                reference.identity.return_value = wrong
                with mock.patch(
                    "controlled_trial_process._ProcessReference",
                    return_value=reference,
                ):
                    with self.assertRaises(TrialProcessError):
                        client.terminate_and_cleanup()
                    reference.terminate_and_wait.assert_not_called()
                    reference.close.assert_called_once()
                self.assertTrue(fixture.root.exists())
        finally:
            if client is not None and fixture.root.exists():
                client.terminate_and_cleanup()

    def test_acl_failure_is_closed_before_owner_start(self):
        fixture = TrialFixture()
        with mock.patch(
            "controlled_trial_process._secure_private_path",
            side_effect=TrialProcessError("invalid_trial_root"),
        ), mock.patch("controlled_trial_process.subprocess.Popen") as popen:
            with self.assertRaises(TrialProcessError):
                TrialProcessLauncher.start(
                    fixture.source, fixture.root, "request"
                )
            popen.assert_not_called()
        import shutil
        shutil.rmtree(fixture.root)

    @unittest.skipUnless(os.name == "nt", "Windows DACL contract")
    def test_unexpected_dacl_is_rejected_at_sensitive_read(self):
        fixture = TrialFixture()
        client = None
        try:
            with DelayedModelServer() as server:
                _, client, _ = fixture.launch(server)
                ControlledTrialProcessTests().wait_state(
                    client, "pending_plan"
                )
                status = fixture.root / "owner-status.json"
                subprocess.run(
                    [
                        "icacls", str(status), "/grant",
                        "*S-1-5-32-545:(R)",
                    ],
                    check=True,
                    capture_output=True,
                )
                with self.assertRaises(TrialProcessError):
                    client.status()
                _secure_private_path(status, directory=False)
                self.assertEqual(
                    client.status()["state"], "pending_plan"
                )
        finally:
            if client is not None and fixture.root.exists():
                client.terminate_and_cleanup()

    def test_stable_process_reference_terminates_without_os_kill_on_windows(self):
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=(
                subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            ),
        )
        reference = None
        try:
            reference = _ProcessReference(process.pid)
            identity = reference.identity()
            self.assertEqual(identity.pid, process.pid)
            with mock.patch(
                "controlled_trial_process.os.kill"
            ) as kill:
                reference.terminate_and_wait(timeout=5)
                if os.name == "nt":
                    kill.assert_not_called()
            process.wait(timeout=5)
        finally:
            if reference is not None:
                reference.close()
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)

    @unittest.skipUnless(os.name == "nt", "Windows root handle contract")
    def test_stable_root_remains_bound_when_pathname_is_reused(self):
        fixture = TrialFixture()
        client = None
        replacement = fixture.root.with_name(fixture.root.name + "-moved")
        try:
            with DelayedModelServer() as server:
                _, client, _ = fixture.launch(server)
                ControlledTrialProcessTests().wait_state(
                    client, "pending_plan"
                )
                os.rename(fixture.root, replacement)
                fixture.root.mkdir()
                marker = fixture.root / "external-marker.txt"
                marker.write_text("outside", encoding="utf-8")
                self.assertEqual(client.status()["state"], "pending_plan")
                client.terminate_and_cleanup()
                client = None
                self.assertEqual(marker.read_text(encoding="utf-8"), "outside")
                self.assertFalse(replacement.exists())
        finally:
            if client is not None:
                client.terminate_and_cleanup()
            if replacement.exists():
                import shutil
                shutil.rmtree(replacement)
            if fixture.root.exists():
                import shutil
                shutil.rmtree(fixture.root)


    @unittest.skipUnless(os.name == "nt", "Windows DACL contract")
    def test_plain_file_is_rejected_without_destroying_the_session(self):
        """A hostile plain file must be a rejected command, never fatal.

        Regression for the single-file denial of service: anyone able to
        drop an unprotected ``owner-command.json`` in the root used to
        kill the live session because ``file_exists`` let a
        ``TrialProcessError`` escape the owner loop.
        """
        root = Path(tempfile.mkdtemp(
            prefix="developerai-real-trial-"
        )).resolve()
        _secure_private_path(root, directory=True)
        guard = _StableRoot(root, _path_identity(root))
        try:
            hostile = root / "owner-command.json"
            hostile.write_text(
                '{"version":1,"unexpected":true}', encoding="utf-8"
            )
            self.assertFalse(_windows_security(hostile)[1])
            self.assertTrue(guard.file_exists(hostile.name))
            self.assertFalse(guard.file_exists("no-such-entry.json"))
            with self.assertRaises(TrialProcessError) as rejected:
                _read_json(hostile, _COMMAND_FIELDS, guard)
            self.assertEqual(rejected.exception.code, "invalid_status")
            guard.delete_file(hostile.name)
            self.assertFalse(guard.file_exists(hostile.name))
        finally:
            guard.close()
            import shutil
            shutil.rmtree(root)


if __name__ == "__main__":
    unittest.main()
