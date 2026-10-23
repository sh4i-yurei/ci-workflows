import io
import http.client
import base64
import json
import os
import threading
import time
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from contextlib import contextmanager, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import pytest
import yaml

from ci_workflows.checks import main, survivor_check
from ci_workflows.tailnet import (
    ValidateClient,
    federated_access_token,
    _key,
    policy_cases,
)
from ci_workflows.errors import ConfigurationError
from ci_workflows.tailnet import InfrastructureError
from ci_workflows.survivors import load_explanations


GRANT = {"src": ["group:admins"], "dst": ["tag:hub"], "ip": ["443"]}
ACL = {"action": "accept", "src": ["tag:hub"], "dst": ["tag:web:80", "[::1]:443"]}


@contextmanager
def endpoint(answers):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers["Content-Length"])
            body = json.loads(self.rfile.read(length))
            requests.append((self.path, body, self.headers.get("Authorization")))
            status, payload = answers.get(json.dumps(body, sort_keys=True), (200, {}))
            if status == "timeout":
                time.sleep(0.15)
                return
            self.send_response(status)
            self.end_headers()
            self.wfile.write(
                payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            )

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


def invoke(policy, url, survivors=None, token="secret-token"):
    output = io.StringIO()
    args = [
        "policy",
        "--file",
        str(policy),
        "--tailnet",
        "example.com",
        "--api-base-url",
        url,
        "--timeout",
        "0.05",
    ]
    if survivors:
        args += ["--survivors-file", str(survivors)]
    with (
        patch.dict(os.environ, {"TS_ACCESS_TOKEN": token}),
        redirect_stdout(output),
    ):
        code = main(args)
    return code, output.getvalue()


def write_policy(tmp_path, entries=None):
    path = tmp_path / "policy.hujson"
    path.write_text(
        "// comment\n"
        + json.dumps(entries or {"grants": [GRANT], "acls": [ACL]})
        + "\n"
    )
    return path


def mutants(policy):
    result = []
    for kind in ("grants", "acls"):
        for position, entry in enumerate(policy.get(kind, [])):
            deleted = json.loads(json.dumps(policy))
            del deleted[kind][position]
            result.append(deleted)
            source = json.loads(json.dumps(policy))
            source[kind][position]["src"] = ["*"]
            result.append(source)
            destination = json.loads(json.dumps(policy))
            destination[kind][position]["dst"] = (
                ["*"]
                if kind == "grants"
                else ["*:" + value.rsplit(":", 1)[1] for value in entry["dst"]]
            )
            result.append(destination)
    return result


def test_hujson_mutants_order_count_and_acl_ports(tmp_path):
    policy = write_policy(tmp_path)
    policy.write_text(policy.read_text().replace('"ip": ["443"]}', '"ip": ["443",]}'))
    baseline = json.loads(json.dumps({"grants": [GRANT], "acls": [ACL]}))
    candidates = mutants(baseline)
    answers = {
        json.dumps(body, sort_keys=True): (200, {"message": "test failed"})
        for body in candidates
    }
    with endpoint(answers) as (url, requests):
        assert invoke(policy, url) == (
            0,
            "6 mutants killed; 0 survived; 7 validate requests\n",
        )
    assert [(path, body, auth) for path, body, auth in requests] == [
        (
            "/api/v2/tailnet/example.com/acl/validate",
            body,
            "Basic " + base64.b64encode(b"secret-token:").decode(),
        )
        for body in [baseline, *candidates]
    ]


def test_mutants_follow_policy_section_order(tmp_path):
    policy = tmp_path / "policy.hujson"
    policy.write_text(json.dumps({"acls": [ACL], "grants": [GRANT]}))
    baseline = {"acls": [ACL], "grants": [GRANT]}
    candidates = mutants({"grants": [GRANT], "acls": [ACL]})
    answers = {
        json.dumps(body, sort_keys=True): (200, {"message": "test failed"})
        for body in candidates
    }
    with endpoint(answers) as (url, requests):
        assert invoke(policy, url) == (
            0,
            "6 mutants killed; 0 survived; 7 validate requests\n",
        )
    assert [body for _, body, _ in requests] == [
        baseline,
        *candidates[3:],
        *candidates[:3],
    ]


def test_policy_tests_section_is_preserved_without_being_mutated(tmp_path):
    policy = write_policy(
        tmp_path, {"tests": [{"src": "tag:a", "accept": ["tag:b:80"]}]}
    )
    with endpoint({}) as (url, requests):
        assert invoke(policy, url) == (
            0,
            "0 mutants killed; 0 survived; 1 validate requests\n",
        )
    assert [body for _, body, _ in requests] == [
        {"tests": [{"src": "tag:a", "accept": ["tag:b:80"]}]}
    ]


def test_policy_tests_before_grants_do_not_stop_mutations(tmp_path):
    policy = write_policy(tmp_path, {"tests": [], "grants": [GRANT]})
    expected = {"tests": [], "grants": [GRANT]}
    answers = {
        json.dumps(body, sort_keys=True): (200, {"message": "test failed"})
        for body in mutants(expected)
    }
    with endpoint(answers) as (url, requests):
        assert invoke(policy, url) == (
            0,
            "3 mutants killed; 0 survived; 4 validate requests\n",
        )
    assert len(requests) == 4


def test_acl_survivor_uses_acl_key_and_original_selectors(tmp_path):
    policy = write_policy(tmp_path, {"acls": [ACL]})
    with endpoint({}) as (url, _):
        assert invoke(policy, url) == (
            1,
            "\n".join(
                [
                    f'acl 1 (["tag:hub"] -> ["tag:web:80", "[::1]:443"]): no accept test fails when it is deleted (key={_key("acl", ACL, "deleted")}); unexplained',
                    f'acl 1 (["tag:hub"] -> ["tag:web:80", "[::1]:443"]): no deny test fails when its source is widened to * (key={_key("acl", ACL, "source")}); unexplained',
                    f'acl 1 (["tag:hub"] -> ["tag:web:80", "[::1]:443"]): no deny test fails when its destination is widened to * (key={_key("acl", ACL, "destination")}); unexplained',
                    "0 mutants killed; 3 survived; 4 validate requests",
                    "",
                ]
            ),
        )


@pytest.mark.parametrize(
    "error", [http.client.IncompleteRead(b"partial"), http.client.BadStatusLine("bad")]
)
def test_http_protocol_errors_are_infrastructure_errors(monkeypatch, error):
    monkeypatch.setattr(
        "ci_workflows.tailnet.urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(error),
    )
    client = ValidateClient("https://api.tailscale.com", "example.com", "token")
    with pytest.raises(InfrastructureError, match=type(error).__name__):
        client.post(client.path, {})


def test_baseline_failure_sends_no_mutants(tmp_path):
    policy = write_policy(tmp_path)
    with endpoint(
        {
            json.dumps({"grants": [GRANT], "acls": [ACL]}, sort_keys=True): (
                200,
                {"message": "line 4, column 2: test failed"},
            )
        }
    ) as (url, requests):
        code, output = invoke(policy, url)
    assert (code, output, len(requests)) == (
        1,
        "baseline policy tests failed: line 4, column 2: test failed\n",
        1,
    )


@pytest.mark.parametrize(
    "status,payload", [(401, {}), (403, {}), (500, {}), (200, b"not json")]
)
def test_bad_response_is_infrastructure_error(tmp_path, status, payload):
    policy = write_policy(tmp_path)
    with endpoint(
        {
            json.dumps({"grants": [GRANT], "acls": [ACL]}, sort_keys=True): (
                status,
                payload,
            )
        }
    ) as (url, requests):
        code, output = invoke(policy, url)
    assert code == 2
    assert output == (
        "infrastructure error: validate returned invalid JSON\n"
        if status == 200
        else f"infrastructure error: validate returned HTTP {status}\n"
    )
    assert "secret-token" not in output
    assert len(requests) == 1


def test_timeout_is_infrastructure_error(tmp_path):
    policy = write_policy(tmp_path)
    with endpoint(
        {
            json.dumps({"grants": [GRANT], "acls": [ACL]}, sort_keys=True): (
                "timeout",
                {},
            )
        }
    ) as (url, requests):
        code, output = invoke(policy, url)
    assert code == 2
    assert "infrastructure error:" in output
    assert len(requests) == 1


def test_survivor_explanation_moves_but_edit_invalidates_it(tmp_path):
    policy = write_policy(tmp_path, {"grants": [GRANT]})
    with endpoint({}) as (url, _requests):
        code, output = invoke(policy, url)
        assert code == 1
        assert (
            'grant 1 (["group:admins"] -> ["tag:hub"]): no accept test fails when it is deleted'
            in output
        )
        assert (
            'grant 1 (["group:admins"] -> ["tag:hub"]): no deny test fails when its source is widened to *'
            in output
        )
        keys = [
            line.split("key=", 1)[1].split(")", 1)[0]
            for line in output.splitlines()
            if "key=" in line
        ]
        assert len(keys) == 3
        survivors = tmp_path / "survivors.yml"
        survivors.write_text(
            yaml.safe_dump(
                [
                    {
                        "mutant": key,
                        "reason": "The case cannot distinguish this mutation.",
                    }
                    for key in keys
                ]
            )
        )
        policy.write_text(
            json.dumps(
                {
                    "grants": [
                        {"src": ["tag:other"], "dst": ["tag:other"], "ip": ["80"]},
                        GRANT,
                    ]
                }
            )
        )
        code, output = invoke(policy, url, survivors)
        assert code == 1
        assert all(
            "explained: The case cannot distinguish this mutation." in line
            for line in output.splitlines()
            if line.startswith("grant 2")
        )
        policy.write_text(
            json.dumps(
                {
                    "grants": [
                        {"src": ["group:admins"], "dst": ["tag:new"], "ip": ["443"]}
                    ]
                }
            )
        )
        code, output = invoke(policy, url, survivors)
        assert code == 1
        assert "stale survivor explanation" in output


def test_both_gates_accept_one_mixed_survivors_file(tmp_path):
    policy = write_policy(tmp_path, {"grants": [GRANT]})
    with endpoint({}) as (url, _requests):
        code, output = invoke(policy, url)
        assert code == 1
        keys = [
            line.split("key=", 1)[1].split(")", 1)[0]
            for line in output.splitlines()
            if "key=" in line
        ]
        shared = tmp_path / ".mutation-survivors.yml"
        shared.write_text(
            yaml.safe_dump(
                [
                    {"mutant": "pkg.f.x__mutmut_3", "reason": "Equivalent"},
                    *({"mutant": key, "reason": "Policy exception"} for key in keys),
                ]
            )
        )
        assert invoke(policy, url, shared) == (
            0,
            "\n".join(
                line.replace("unexplained", "explained: Policy exception")
                for line in output.splitlines()
            )
            + "\n",
        )
    result = tmp_path / "result.meta"
    result.write_text(json.dumps({"exit_code_by_key": {"pkg.f.x__mutmut_3": 0}}))
    assert survivor_check(result, shared, "mutmut") == (
        0,
        "mutation survivors accepted",
    )


@pytest.mark.parametrize(
    "body,error",
    [
        ("", None),
        ("not a list", "survivors file must be a list"),
        ("- 3\n", "each survivor needs mutant and reason"),
        ("- mutant: 3\n  reason: why\n", "each survivor needs mutant and reason"),
        ("- mutant: ''\n  reason: why\n", "each survivor needs mutant and reason"),
        ("- mutant: key\n  reason: 3\n", "each survivor needs mutant and reason"),
        (
            "- mutant: key\n  reason: why\n- mutant: key\n  reason: again\n",
            "duplicate survivor key: key",
        ),
    ],
)
def test_shared_explanations_loader_validates_entries(tmp_path, body, error):
    path = tmp_path / "survivors.yml"
    path.write_text(body)
    if error is None:
        assert (
            load_explanations(path, duplicate_error="duplicate survivor key: {name}")
            == {}
        )
    else:
        with pytest.raises(ConfigurationError, match=f"^{error}$"):
            load_explanations(path, duplicate_error="duplicate survivor key: {name}")


def test_identical_mutant_is_reported_without_request(tmp_path):
    policy = write_policy(
        tmp_path, {"grants": [{"src": ["*"], "dst": ["tag:hub"], "ip": ["443"]}]}
    )
    with endpoint({}) as (url, requests):
        code, output = invoke(policy, url)
    assert code == 1
    assert "cannot be widened" in output
    assert "3 validate requests" in output
    assert len(requests) == 3


def test_invalid_hujson_reports_line(tmp_path):
    policy = tmp_path / "broken.hujson"
    policy.write_text('{"grants": [\n invalid] }')
    with endpoint({}) as (url, requests):
        code, output = invoke(policy, url)
    assert code == 2
    assert "line 2" in output
    assert requests == []


@pytest.mark.parametrize("body", ["{grants: []}", "{'grants': []}"])
def test_json5_only_syntax_is_not_hujson(tmp_path, body):
    policy = tmp_path / "policy.hujson"
    policy.write_text(body)
    with endpoint({}) as (url, requests):
        code, output = invoke(policy, url)
    assert code == 2
    assert "line 1" in output
    assert requests == []


def test_explanation_for_now_killed_mutant_is_stale(tmp_path):
    policy = write_policy(tmp_path, {"grants": [GRANT]})
    with endpoint({}) as (url, _requests):
        code, output = invoke(policy, url)
    assert code == 1
    key = next(
        line.split("key=", 1)[1].split(")", 1)[0]
        for line in output.splitlines()
        if "key=" in line
    )
    survivors = tmp_path / "survivors.yml"
    survivors.write_text(
        yaml.safe_dump([{"mutant": key, "reason": "Previously unkillable."}])
    )
    deleted = {"grants": []}
    with endpoint(
        {json.dumps(deleted, sort_keys=True): (200, {"message": "test failed"})}
    ) as (url, _requests):
        code, output = invoke(policy, url, survivors)
    assert code == 1
    assert f"stale survivor explanation: {key}" in output
    assert output.endswith("1 mutants killed; 2 survived; 4 validate requests\n")


def test_validate_client_rejects_any_other_path_before_network():
    client = ValidateClient(
        "http://127.0.0.1:1", "example.com", "secret-token", timeout=0.01
    )
    with pytest.raises(ValueError, match="validate endpoint only"):
        client.post("/api/v2/tailnet/example.com/acl", {})


def test_validate_client_path_guard_keeps_exact_error():
    client = ValidateClient("http://127.0.0.1:1", "example.com", "token")
    with pytest.raises(ValueError) as error:
        client.post("/api/v2/tailnet/example.com/acl", {})
    assert str(error.value) == "validate endpoint only"


def test_validate_client_keeps_authority_ending_in_x_and_exact_configuration_error():
    client = ValidateClient("https://apiX/", "example.com", "token")
    assert client.base_url == "https://apiX"
    with pytest.raises(ConfigurationError) as error:
        ValidateClient("invalid", "example.com", "token")
    assert str(error.value) == (
        "valid endpoint, tailnet, access token, and positive timeout are required"
    )


def test_parallel_pytest_sessions_get_distinct_external_temp_roots(tmp_path):
    repository = Path(__file__).resolve().parents[1]
    sample = tmp_path / "sample_test.py"
    sample.write_text(
        "def test_tmp(tmp_path):\n    print('TEMP_ROOT=' + str(tmp_path.parent))\n"
    )
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-s",
        "-c",
        str(repository / "pyproject.toml"),
        str(sample),
    ]
    workers = [
        subprocess.Popen(
            command,
            cwd=repository,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    outputs = [worker.communicate(timeout=30) for worker in workers]
    assert all(worker.returncode == 0 for worker in workers), outputs
    roots = [
        next(
            line.removeprefix("TEMP_ROOT=")
            for line in output.splitlines()
            if line.startswith("TEMP_ROOT=")
        )
        for output, _ in outputs
    ]
    assert len(set(roots)) == 2
    assert all(not Path(root).is_relative_to(repository) for root in roots)


def test_validate_client_rejects_base_url_with_path_or_query():
    for base_url in (
        "https://api.tailscale.com/unexpected",
        "https://api.tailscale.com/?unexpected=1",
    ):
        with pytest.raises(ConfigurationError, match="valid endpoint"):
            ValidateClient(base_url, "example.com", "token")


def test_validate_client_default_timeout_is_ten_seconds():
    client = ValidateClient("https://api.tailscale.com", "example.com", "token")
    assert client.timeout == 10


@pytest.mark.parametrize(
    "base,tailnet,token,timeout",
    [
        ("bad", "example.com", "token", 10),
        ("https://", "example.com", "token", 10),
        ("https://api.tailscale.com", "", "token", 10),
        ("https://api.tailscale.com", "example.com", "", 10),
        ("https://api.tailscale.com", "example.com", "token", 0),
    ],
)
def test_validate_client_rejects_each_invalid_input(base, tailnet, token, timeout):
    with pytest.raises(ConfigurationError, match="valid endpoint"):
        ValidateClient(base, tailnet, token, timeout)


def test_validate_request_has_exact_path_headers_and_timeout(monkeypatch):
    seen = []

    class Response(io.BytesIO):
        status = 200

    def fake_open(request, timeout):
        seen.append(
            (
                request.full_url,
                request.get_method(),
                request.get_header("Content-type"),
                timeout,
            )
        )
        return Response(b"{}")

    monkeypatch.setattr("ci_workflows.tailnet.urlopen", fake_open)
    client = ValidateClient(
        "https://api.tailscale.com/", "team/example.com", "token", timeout=2.5
    )
    assert client.post(client.path, {}) == (False, "policy test failed")
    assert seen == [
        (
            "https://api.tailscale.com/api/v2/tailnet/team%2Fexample.com/acl/validate",
            "POST",
            "application/hujson",
            2.5,
        )
    ]


def test_validate_client_reports_exact_result_details(monkeypatch):
    class Response(io.BytesIO):
        status = 200

    replies = iter(
        [
            Response(b"not JSON"),
            Response(
                b'{"message":"failed","data":[{"user":"tag:a","errors":["denied"]}]}'
            ),
            Response(b'{"data":[{"user":"tag:a","warnings":["warning"]}]}'),
            Response(b'{"message":"failed"}'),
            Response(b'{"data":[{"user":"tag:a","errors":["denied"]}]}'),
        ]
    )
    monkeypatch.setattr(
        "ci_workflows.tailnet.urlopen", lambda *_args, **_kwargs: next(replies)
    )
    client = ValidateClient("https://api.tailscale.com", "example.com", "token")
    with pytest.raises(InfrastructureError, match="^validate returned invalid JSON$"):
        client.post(client.path, {})
    assert client.post(client.path, {}) == (True, "failed; denied")
    assert client.post(client.path, {}) == (True, "policy test failed")
    assert client.post(client.path, {}) == (True, "failed")
    assert client.post(client.path, {}) == (True, "denied")


def test_validate_client_preserves_trailing_semicolon_and_capital_x(monkeypatch):
    class Response(io.BytesIO):
        status = 200

    monkeypatch.setattr(
        "ci_workflows.tailnet.urlopen",
        lambda *_args, **_kwargs: Response(
            b'{"message":"denied for tag:boX;","data":[{"errors":["blocked; X"]}]}'
        ),
    )
    client = ValidateClient("https://api.tailscale.com", "example.com", "token")
    assert client.post(client.path, {}) == (True, "denied for tag:boX;; blocked; X")


def test_validate_client_preserves_message_only_trailing_semicolon(monkeypatch):
    class Response(io.BytesIO):
        status = 200

    monkeypatch.setattr(
        "ci_workflows.tailnet.urlopen",
        lambda *_args, **_kwargs: Response(b'{"message":"denied for tag:boX;"}'),
    )
    client = ValidateClient("https://api.tailscale.com", "example.com", "token")
    assert client.post(client.path, {}) == (True, "denied for tag:boX;")


def test_validate_client_accepts_status_only_result(monkeypatch):
    class Response(io.BytesIO):
        status = 200

    monkeypatch.setattr(
        "ci_workflows.tailnet.urlopen",
        lambda *_args, **_kwargs: Response(b'{"status":200}'),
    )
    client = ValidateClient("https://api.tailscale.com", "example.com", "token")
    assert client.post(client.path, {}) == (False, "policy test failed")


def test_mutations_allow_policy_without_grants_or_acls(tmp_path):
    policy = tmp_path / "policy.hujson"
    policy.write_text('{"tests": []}')
    with endpoint({}) as (url, requests):
        code, output = invoke(policy, url)
    assert (code, output, len(requests)) == (
        0,
        "0 mutants killed; 0 survived; 1 validate requests\n",
        1,
    )


def test_policy_cli_defaults_use_environment_file_and_official_endpoint(
    tmp_path, monkeypatch
):
    policy = write_policy(tmp_path, {"grants": []})
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TS_TAILNET", "example.com")
    monkeypatch.setenv("TS_ACCESS_TOKEN", "secret-token")
    captured = []

    class Response(io.BytesIO):
        status = 200

    def fake_urlopen(request, *, timeout):
        captured.append(
            (
                request.full_url,
                request.get_method(),
                timeout,
                request.get_header("Authorization"),
                json.loads(request.data),
            )
        )
        return Response(b'{"status":200}')

    monkeypatch.setattr("ci_workflows.tailnet.urlopen", fake_urlopen)
    output = io.StringIO()
    default_survivors = tmp_path / ".mutation-survivors.yml"
    default_survivors.write_text("invalid\n")
    with redirect_stdout(output):
        code = main(["policy", "--file", str(policy)])
    assert (code, output.getvalue(), captured) == (
        2,
        "configuration error: survivors file must be a list\n",
        [],
    )
    default_survivors.unlink()
    output = io.StringIO()
    with redirect_stdout(output):
        code = main(["policy", "--file", str(policy)])
    assert (code, output.getvalue()) == (
        0,
        "0 mutants killed; 0 survived; 1 validate requests\n",
    )
    assert captured == [
        (
            "https://api.tailscale.com/api/v2/tailnet/example.com/acl/validate",
            "POST",
            10,
            "Basic c2VjcmV0LXRva2VuOg==",
            {"grants": []},
        )
    ]


def test_policy_cli_passes_explicit_timeout_to_validate(tmp_path, monkeypatch):
    policy = write_policy(tmp_path, {"grants": []})
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TS_ACCESS_TOKEN", "secret-token")
    captured = []

    class Response(io.BytesIO):
        status = 200

    def fake_urlopen(request, *, timeout):
        captured.append((request.full_url, timeout))
        return Response(b'{"status":200}')

    monkeypatch.setattr("ci_workflows.tailnet.urlopen", fake_urlopen)
    output = io.StringIO()
    with redirect_stdout(output):
        code = main(
            [
                "policy",
                "--file",
                str(policy),
                "--tailnet",
                "another.example",
                "--api-base-url",
                "https://validate.example.test",
                "--timeout",
                "0.05",
            ]
        )
    assert (code, output.getvalue(), captured) == (
        0,
        "0 mutants killed; 0 survived; 1 validate requests\n",
        [
            (
                "https://validate.example.test/api/v2/tailnet/another.example/acl/validate",
                0.05,
            )
        ],
    )


def test_policy_cli_without_access_token_requires_federated_identity(
    tmp_path, monkeypatch
):
    policy = write_policy(tmp_path, {"grants": []})
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TS_TAILNET", "example.com")
    monkeypatch.delenv("TS_ACCESS_TOKEN", raising=False)
    for name in (
        "TS_OAUTH_ID",
        "TS_AUDIENCE",
        "ACTIONS_ID_TOKEN_REQUEST_URL",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    requests = []
    monkeypatch.setattr(
        "ci_workflows.tailnet.urlopen",
        lambda *args, **kwargs: requests.append((args, kwargs)),
    )
    output = io.StringIO()
    with redirect_stdout(output):
        code = main(["policy", "--file", str(policy)])
    assert (code, output.getvalue(), requests) == (
        2,
        "configuration error: federated Tailscale identity inputs are missing\n",
        [],
    )


def test_policy_cases_uses_official_endpoint_and_caller_timeout(monkeypatch, tmp_path):
    seen = []
    policy = write_policy(tmp_path, {"grants": []})

    class Client:
        def __init__(self, base_url, tailnet, token, timeout):
            seen.append((base_url, tailnet, token, timeout))
            self.path = "/api/v2/tailnet/example.com/acl/validate"

        def post(self, path, body):
            assert (path, body) == (self.path, {"grants": []})
            return False, ""

    monkeypatch.setattr("ci_workflows.tailnet.ValidateClient", Client)
    assert policy_cases(
        policy, "example.com", "secret-token", tmp_path / "missing.yml", timeout=2.5
    ) == (
        0,
        "0 mutants killed; 0 survived; 1 validate requests",
    )
    assert seen == [("https://api.tailscale.com", "example.com", "secret-token", 2.5)]
    assert policy_cases(
        policy, "example.com", "secret-token", tmp_path / "missing.yml"
    ) == (
        0,
        "0 mutants killed; 0 survived; 1 validate requests",
    )
    assert seen[-1] == ("https://api.tailscale.com", "example.com", "secret-token", 10)


def test_policy_cases_masks_baseline_and_reports_exact_survivors(tmp_path):
    policy = write_policy(tmp_path, {"grants": [GRANT]})
    baseline = {"grants": [GRANT]}
    with endpoint(
        {
            json.dumps(baseline, sort_keys=True): (
                200,
                {"message": "secret-token failed"},
            )
        }
    ) as (url, _):
        assert invoke(policy, url) == (
            1,
            "baseline policy tests failed: [MASKED] failed\n",
        )
    with endpoint({}) as (url, _):
        assert invoke(policy, url) == (
            1,
            "\n".join(
                [
                    f'grant 1 (["group:admins"] -> ["tag:hub"]): no accept test fails when it is deleted (key={_key("grant", GRANT, "deleted")}); unexplained',
                    f'grant 1 (["group:admins"] -> ["tag:hub"]): no deny test fails when its source is widened to * (key={_key("grant", GRANT, "source")}); unexplained',
                    f'grant 1 (["group:admins"] -> ["tag:hub"]): no deny test fails when its destination is widened to * (key={_key("grant", GRANT, "destination")}); unexplained',
                    "0 mutants killed; 3 survived; 4 validate requests",
                    "",
                ]
            ),
        )


def test_policy_cases_keeps_infrastructure_detail_without_token(monkeypatch, tmp_path):
    policy = write_policy(tmp_path, {"grants": []})
    monkeypatch.setattr(
        "ci_workflows.tailnet.federated_access_token",
        lambda: (_ for _ in ()).throw(
            InfrastructureError("identity endpoint unavailable")
        ),
    )
    assert policy_cases(policy, "example.com", "", tmp_path / "missing.yml") == (
        2,
        "infrastructure error: identity endpoint unavailable",
    )


def test_policy_cases_masks_token_in_infrastructure_detail(monkeypatch, tmp_path):
    policy = write_policy(tmp_path, {"grants": []})

    class Client:
        path = "/api/v2/tailnet/example.com/acl/validate"

        def __init__(self, *_args):
            pass

        def post(self, *_args):
            raise InfrastructureError("secret-token failed")

    monkeypatch.setattr("ci_workflows.tailnet.ValidateClient", Client)
    assert policy_cases(
        policy, "example.com", "secret-token", tmp_path / "missing.yml"
    ) == (
        2,
        "infrastructure error: [MASKED] failed",
    )


def test_federated_http_protocol_error_is_infrastructure_error(monkeypatch):
    environment = {
        "TS_OAUTH_ID": "client-id",
        "TS_AUDIENCE": "audience",
        "ACTIONS_ID_TOKEN_REQUEST_URL": "https://example.test/oidc",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "request-token",
    }
    monkeypatch.setattr(
        "ci_workflows.tailnet.urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            http.client.BadStatusLine("bad")
        ),
    )
    with patch.dict(os.environ, environment, clear=True):
        with pytest.raises(
            InfrastructureError, match="federated token exchange failed: BadStatusLine"
        ):
            federated_access_token()


def test_survivor_key_is_canonical_and_content_sensitive():
    first = {"src": ["group:admins"], "dst": ["tag:hub"]}
    reordered = {"dst": ["tag:hub"], "src": ["group:admins"]}
    assert _key("grant", first, "source") == _key("grant", reordered, "source")
    assert _key("grant", first, "source") != _key("grant", first, "destination")
    assert _key("grant", first, "source") != _key(
        "grant", {**first, "dst": ["tag:new"]}, "source"
    )
    assert _key("grant", first, "source") == (
        "tailscale:grant:6517a6da41e89be44c1e1e66db52092213ac018ecb0d65541a5427e03ebcf837:source"
    )
    assert _key("grant", {"src": ["grüp:admins"], "dst": ["tag:hub"]}, "source") == (
        "tailscale:grant:3f9e846fad696734ee00c87c0b26e0850be6a04868b0b149f1f5840041e434bf:source"
    )


def test_federated_exchange_uses_github_identity_and_masks_token(monkeypatch):
    calls = []

    class Response(io.BytesIO):
        status = 200

    def fake_open(request, timeout):
        calls.append(
            (
                request.full_url,
                request.get_header("Authorization"),
                request.data,
                timeout,
                request.get_method(),
                request.get_header("Content-type"),
            )
        )
        if len(calls) == 1:
            return Response(b'{"value":"signed-jwt"}')
        return Response(b'{"access_token":"secret-access"}')

    monkeypatch.setattr("ci_workflows.tailnet.urlopen", fake_open)
    environment = {
        "TS_OAUTH_ID": "client-id",
        "TS_AUDIENCE": "api.tailscale.com/client-id",
        "ACTIONS_ID_TOKEN_REQUEST_URL": "https://example.test/oidc?existing=1",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "github-request-token",
        "GITHUB_ACTIONS": "true",
    }
    output = io.StringIO()
    with patch.dict(os.environ, environment), redirect_stdout(output):
        assert federated_access_token() == "secret-access"
    assert (
        calls[0][0]
        == "https://example.test/oidc?existing=1&audience=api.tailscale.com%2Fclient-id"
    )
    assert calls[0][1:] == ("Bearer github-request-token", None, 10, "GET", None)
    assert calls[1][0] == "https://api.tailscale.com/api/v2/oauth/token-exchange"
    assert parse_qs(calls[1][2].decode()) == {
        "client_id": ["client-id"],
        "jwt": ["signed-jwt"],
    }
    assert calls[1][3:] == (10, "POST", "application/x-www-form-urlencoded")
    assert output.getvalue() == "::add-mask::secret-access\n"


@pytest.mark.parametrize(
    "missing",
    [
        "TS_OAUTH_ID",
        "TS_AUDIENCE",
        "ACTIONS_ID_TOKEN_REQUEST_URL",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
    ],
)
def test_each_missing_federated_input_is_rejected(monkeypatch, missing):
    environment = {
        "TS_OAUTH_ID": "client-id",
        "TS_AUDIENCE": "audience",
        "ACTIONS_ID_TOKEN_REQUEST_URL": "https://example.test/oidc",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "request-token",
    }
    environment.pop(missing)
    with patch.dict(os.environ, environment, clear=True):
        with pytest.raises(
            ConfigurationError,
            match="^federated Tailscale identity inputs are missing$",
        ):
            federated_access_token()


@pytest.mark.parametrize("identity", [b"[]", b'{"value":3}', b"{}"])
def test_invalid_identity_response_is_infrastructure_error(monkeypatch, identity):
    class Response(io.BytesIO):
        status = 200

    monkeypatch.setattr(
        "ci_workflows.tailnet.urlopen", lambda *_args, **_kwargs: Response(identity)
    )
    environment = {
        "TS_OAUTH_ID": "client-id",
        "TS_AUDIENCE": "audience",
        "ACTIONS_ID_TOKEN_REQUEST_URL": "https://example.test/oidc",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "request-token",
    }
    with patch.dict(os.environ, environment, clear=True):
        with pytest.raises(
            InfrastructureError, match="^GitHub returned an invalid identity token$"
        ):
            federated_access_token()


@pytest.mark.parametrize("token_response", [b"[]", b'{"access_token":3}', b"{}"])
def test_invalid_access_token_response_is_infrastructure_error(
    monkeypatch, token_response
):
    class Response(io.BytesIO):
        status = 200

    replies = iter([Response(b'{"value":"jwt"}'), Response(token_response)])
    monkeypatch.setattr(
        "ci_workflows.tailnet.urlopen", lambda *_args, **_kwargs: next(replies)
    )
    environment = {
        "TS_OAUTH_ID": "client-id",
        "TS_AUDIENCE": "audience",
        "ACTIONS_ID_TOKEN_REQUEST_URL": "https://example.test/oidc",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "request-token",
    }
    with patch.dict(os.environ, environment, clear=True):
        with pytest.raises(
            InfrastructureError, match="^Tailscale returned no access token$"
        ):
            federated_access_token()


def test_connection_error_is_infrastructure_error(tmp_path):
    policy = write_policy(tmp_path)
    code, output = invoke(policy, "http://127.0.0.1:1")
    assert code == 2
    assert output.startswith("infrastructure error: validate request failed:")
    assert "secret-token" not in output


def test_data_failure_kills_mutant_and_bad_shape_fails(tmp_path):
    policy = write_policy(tmp_path, {"grants": [GRANT]})
    baseline = {"grants": [GRANT]}
    candidates = mutants(baseline)
    answers = {
        json.dumps(body, sort_keys=True): (
            200,
            {"data": [{"user": "tag:S", "errors": ["denied"], "warnings": []}]},
        )
        for body in candidates
    }
    with endpoint(answers) as (url, requests):
        assert invoke(policy, url) == (
            0,
            "3 mutants killed; 0 survived; 4 validate requests\n",
        )
    assert len(requests) == 4
    with endpoint({json.dumps(baseline, sort_keys=True): (200, {"data": "wrong"})}) as (
        url,
        requests,
    ):
        code, output = invoke(policy, url)
    assert (code, output, len(requests)) == (
        2,
        "infrastructure error: validate returned an invalid test result\n",
        1,
    )


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"unexpected": True},
        {"status": "200"},
        {"message": 8},
        {"data": [{"user": 4}]},
        {"data": ["wrong"]},
        {"data": [{"errors": "wrong"}]},
        {"data": [{"warnings": [8]}]},
    ],
)
def test_validate_rejects_wrong_test_error_shapes(tmp_path, payload):
    policy = write_policy(tmp_path)
    body = {"grants": [GRANT], "acls": [ACL]}
    with endpoint({json.dumps(body, sort_keys=True): (200, payload)}) as (
        url,
        requests,
    ):
        code, output = invoke(policy, url)
    assert (code, output, len(requests)) == (
        2,
        "infrastructure error: validate returned an invalid test result\n",
        1,
    )


@pytest.mark.parametrize(
    "policy_body,diagnostic",
    [
        ([], "policy must be a HuJSON object"),
        ({"grants": {}}, "grants must be a list"),
        ({"acls": [8]}, "acls 1 must be an object"),
        (
            {"grants": [{"src": [], "dst": ["tag:hub"]}]},
            "grants 1 src must be a nonempty list of strings",
        ),
        (
            {"grants": [{"src": ["tag:S"], "dst": [4]}]},
            "grants 1 dst must be a nonempty list of strings",
        ),
    ],
)
def test_invalid_policy_structure_fails_before_network(
    tmp_path, policy_body, diagnostic
):
    policy = tmp_path / "policy.hujson"
    policy.write_text(json.dumps(policy_body))
    with endpoint({}) as (url, requests):
        code, output = invoke(policy, url)
    assert (code, output, requests) == (2, f"configuration error: {diagnostic}\n", [])


@pytest.mark.parametrize(
    "args",
    [
        ("", "tailnet", "token", 1),
        ("url", "", "token", 1),
        ("url", "tailnet", "", 1),
        ("url", "tailnet", "token", 0),
    ],
)
def test_validate_client_rejects_missing_inputs(args):
    with pytest.raises(ConfigurationError, match="positive timeout are required"):
        ValidateClient(*args)


def test_non_200_response_without_http_error_is_infrastructure_error(monkeypatch):
    class Response(io.BytesIO):
        status = 201

    monkeypatch.setattr(
        "ci_workflows.tailnet.urlopen", lambda *_args, **_kwargs: Response(b"{}")
    )
    client = ValidateClient("https://api.tailscale.com", "example.com", "token")
    with pytest.raises(InfrastructureError, match="HTTP 201"):
        client.post(client.path, {})


@pytest.mark.parametrize(
    "body,expected",
    [
        ("not a list", "survivors file must be a list"),
        ("- mutant: tailscale:bad", "each survivor needs mutant and reason"),
        (
            "- mutant: tailscale:bad\n  reason: why\n- mutant: tailscale:bad\n  reason: why",
            "duplicate survivor key: tailscale:bad",
        ),
    ],
)
def test_bad_explanation_file_is_configuration_error(tmp_path, body, expected):
    policy = write_policy(tmp_path)
    survivors = tmp_path / "survivors.yml"
    survivors.write_text(body)
    with endpoint({}) as (url, requests):
        code, output = invoke(policy, url, survivors)
    assert (code, output, requests) == (2, f"configuration error: {expected}\n", [])


def test_federated_check_prints_no_access_token(monkeypatch, tmp_path):
    policy = write_policy(tmp_path, {"grants": []})
    monkeypatch.setattr(
        "ci_workflows.tailnet.federated_access_token", lambda: "federated-secret"
    )
    with endpoint({}) as (url, _requests):
        code, output = invoke(policy, url, token="")
    assert (code, output) == (0, "0 mutants killed; 0 survived; 1 validate requests\n")
    assert "federated-secret" not in output


def test_federated_exchange_without_github_runner_emits_no_token(monkeypatch):
    class Response(io.BytesIO):
        status = 200

    replies = iter(
        [Response(b'{"value":"jwt"}'), Response(b'{"access_token":"hidden"}')]
    )
    monkeypatch.setattr(
        "ci_workflows.tailnet.urlopen", lambda *_args, **_kwargs: next(replies)
    )
    environment = {
        "TS_OAUTH_ID": "client-id",
        "TS_AUDIENCE": "audience",
        "ACTIONS_ID_TOKEN_REQUEST_URL": "https://example.test/oidc",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "github-request-token",
        "GITHUB_ACTIONS": "false",
    }
    output = io.StringIO()
    with patch.dict(os.environ, environment), redirect_stdout(output):
        assert federated_access_token() == "hidden"
    assert output.getvalue() == ""


def test_survivors_file_can_be_absent_or_contain_other_tool_keys(tmp_path):
    policy = write_policy(tmp_path, {"grants": []})
    missing = tmp_path / "missing.yml"
    other = tmp_path / "other.yml"
    other.write_text("- mutant: python.function.mutant\n  reason: unrelated\n")
    with endpoint({}) as (url, _requests):
        assert invoke(policy, url, missing) == (
            0,
            "0 mutants killed; 0 survived; 1 validate requests\n",
        )
        assert invoke(policy, url, other) == (
            0,
            "0 mutants killed; 0 survived; 1 validate requests\n",
        )


def test_federated_token_errors_do_not_leak_credential(monkeypatch):
    environment = {
        "TS_OAUTH_ID": "client-id",
        "TS_AUDIENCE": "audience",
        "ACTIONS_ID_TOKEN_REQUEST_URL": "https://example.test/oidc",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "github-secret",
        "GITHUB_ACTIONS": "false",
    }

    class Response(io.BytesIO):
        status = 200

    with patch.dict(os.environ, environment):
        monkeypatch.setattr(
            "ci_workflows.tailnet.urlopen", lambda *_args, **_kwargs: Response(b"{}")
        )
        with pytest.raises(InfrastructureError, match="invalid identity token"):
            federated_access_token()
        responses = iter([Response(b'{"value":"jwt"}'), Response(b"{}")])
        monkeypatch.setattr(
            "ci_workflows.tailnet.urlopen", lambda *_args, **_kwargs: next(responses)
        )
        with pytest.raises(InfrastructureError, match="no access token"):
            federated_access_token()
        monkeypatch.setattr(
            "ci_workflows.tailnet.urlopen",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError()),
        )
        with pytest.raises(InfrastructureError, match="TimeoutError"):
            federated_access_token()


def test_missing_federated_inputs_are_configuration_error(monkeypatch):
    for name in (
        "TS_OAUTH_ID",
        "TS_AUDIENCE",
        "ACTIONS_ID_TOKEN_REQUEST_URL",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(
        ConfigurationError, match="federated Tailscale identity inputs are missing"
    ):
        federated_access_token()
