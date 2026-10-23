"""Test tailnet policy rules through Tailscale's validate endpoint."""

from __future__ import annotations

import copy
import base64
import hashlib
import json
import http.client
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit, urlunsplit, parse_qsl
from urllib.request import Request, urlopen

import jsonyx

from ci_workflows.errors import ConfigurationError
from ci_workflows.survivors import load_explanations


class InfrastructureError(Exception):
    """A response cannot be used as policy test evidence."""


class ValidateClient:
    def __init__(self, base_url: str, tailnet: str, token: str, timeout: float = 10):
        parsed = urlsplit(base_url)
        if (
            not token
            or not tailnet
            or timeout <= 0
            or parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ConfigurationError(
                "valid endpoint, tailnet, access token, and positive timeout are required"
            )
        self.base_url = base_url.rstrip("/")
        self.path = f"/api/v2/tailnet/{quote(tailnet, safe='')}/acl/validate"
        self.token = token
        self.timeout = timeout

    def post(self, path: str, body: dict) -> tuple[bool, str]:
        if path != self.path:
            raise ValueError("validate endpoint only")
        request = Request(
            self.base_url + path,
            data=json.dumps(body, separators=(",", ":")).encode(),
            method="POST",
            headers={
                "Authorization": "Basic "
                + base64.b64encode(f"{self.token}:".encode()).decode(),
                "Content-Type": "application/hujson",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                if response.status != 200:
                    raise InfrastructureError(
                        f"validate returned HTTP {response.status}"
                    )
                content = response.read()
        except HTTPError as exc:
            raise InfrastructureError(f"validate returned HTTP {exc.code}") from None
        except (URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
            raise InfrastructureError(
                f"validate request failed: {type(exc).__name__}"
            ) from None
        try:
            payload = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise InfrastructureError("validate returned invalid JSON") from None
        if not isinstance(payload, dict) or (
            payload and not ({"message", "data", "status"} & payload.keys())
        ):
            raise InfrastructureError("validate returned an invalid test result")
        message = payload.get("message", "")
        data = payload.get("data", [])
        status = payload.get("status", 200)
        if (
            not isinstance(status, int)
            or not isinstance(message, str)
            or not isinstance(data, list)
            or any(
                not isinstance(item, dict)
                or not isinstance(item.get("user", ""), str)
                or any(
                    not isinstance(item.get(field, []), list)
                    or not all(isinstance(value, str) for value in item.get(field, []))
                    for field in ("errors", "warnings")
                )
                for item in data
            )
        ):
            raise InfrastructureError("validate returned an invalid test result")
        details = "; ".join(
            part
            for part in [
                message,
                *(error for item in data for error in item.get("errors", [])),
            ]
            if part
        )
        return bool(message or data), details or "policy test failed"


def federated_access_token() -> str:
    client_id = os.environ.get("TS_OAUTH_ID")
    audience = os.environ.get("TS_AUDIENCE")
    request_url = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL")
    request_token = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
    if not all((client_id, audience, request_url, request_token)):
        raise ConfigurationError("federated Tailscale identity inputs are missing")
    parts = urlsplit(request_url)
    query = urlencode([*parse_qsl(parts.query), ("audience", audience)])
    oidc_url = urlunsplit(
        (parts.scheme, parts.netloc, parts.path, query, parts.fragment)
    )
    oidc = Request(oidc_url, headers={"Authorization": f"Bearer {request_token}"})
    try:
        with urlopen(oidc, timeout=10) as response:
            identity = json.load(response)
        jwt = identity.get("value") if isinstance(identity, dict) else None
        if not isinstance(jwt, str) or not jwt:
            raise InfrastructureError("GitHub returned an invalid identity token")
        exchange = Request(
            "https://api.tailscale.com/api/v2/oauth/token-exchange",
            data=urlencode({"client_id": client_id, "jwt": jwt}).encode(),
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urlopen(exchange, timeout=10) as response:
            result = json.load(response)
    except (
        HTTPError,
        URLError,
        TimeoutError,
        OSError,
        http.client.HTTPException,
        ValueError,
        UnicodeDecodeError,
    ) as exc:
        raise InfrastructureError(
            f"federated token exchange failed: {type(exc).__name__}"
        ) from None
    token = result.get("access_token") if isinstance(result, dict) else None
    if not isinstance(token, str) or not token:
        raise InfrastructureError("Tailscale returned no access token")
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::add-mask::{token}")
    return token


def _parse_policy(path: Path) -> dict:
    try:
        policy = jsonyx.loads(
            path.read_text(),
            allow=jsonyx.allow.COMMENTS | jsonyx.allow.TRAILING_COMMA,
        )
    except jsonyx.JSONSyntaxError as exc:
        raise ConfigurationError(
            f"invalid HuJSON: line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from None
    if not isinstance(policy, dict):
        raise ConfigurationError("policy must be a HuJSON object")
    for section in ("grants", "acls"):
        entries = policy.get(section, [])
        if not isinstance(entries, list):
            raise ConfigurationError(f"{section} must be a list")
        for position, entry in enumerate(entries, 1):
            if not isinstance(entry, dict):
                raise ConfigurationError(f"{section} {position} must be an object")
            for field in ("src", "dst"):
                value = entry.get(field)
                if (
                    not isinstance(value, list)
                    or not value
                    or not all(isinstance(item, str) and item for item in value)
                ):
                    raise ConfigurationError(
                        f"{section} {position} {field} must be a nonempty list of strings"
                    )
    return policy


def _key(kind: str, entry: dict, mutation: str) -> str:
    canonical = json.dumps(
        entry, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    return f"tailscale:{kind}:{digest}:{mutation}"


def _mutations(policy: dict):
    for section in policy:
        if section not in {"grants", "acls"}:
            continue
        kind = "grant" if section == "grants" else "acl"
        for index, entry in enumerate(policy[section]):
            label = f"{kind} {index + 1} ({json.dumps(entry['src'])} -> {json.dumps(entry['dst'])})"
            for mutation, description in (
                ("deleted", "no accept test fails when it is deleted"),
                ("source", "no deny test fails when its source is widened to *"),
                (
                    "destination",
                    "no deny test fails when its destination is widened to *",
                ),
            ):
                mutant = copy.deepcopy(policy)
                if mutation == "deleted":
                    del mutant[section][index]
                elif mutation == "source":
                    mutant[section][index]["src"] = ["*"]
                elif kind == "grant":
                    mutant[section][index]["dst"] = ["*"]
                else:
                    destinations = []
                    for destination in entry["dst"]:
                        destinations.append("*:" + destination.rsplit(":", 1)[1])
                    mutant[section][index]["dst"] = destinations
                yield label, _key(kind, entry, mutation), description, mutant


def policy_cases(
    policy_file: Path,
    tailnet: str,
    token: str,
    survivors_file: Path,
    api_base_url: str = "https://api.tailscale.com",
    timeout: float = 10,
) -> tuple[int, str]:
    policy = _parse_policy(policy_file)
    explanations = {
        name: reason
        for name, reason in load_explanations(
            survivors_file, duplicate_error="duplicate survivor key: {name}"
        ).items()
        if name.startswith("tailscale:")
    }
    requests = 0
    try:
        if not token:
            token = federated_access_token()
        client = ValidateClient(api_base_url, tailnet, token, timeout)
        failed, detail = client.post(client.path, policy)
        requests += 1
        if failed:
            return (
                1,
                f"baseline policy tests failed: {detail.replace(token, '[MASKED]')}",
            )
        killed = 0
        survivors = []
        present_keys = set()
        unexplained = False
        for label, key, description, mutant in _mutations(policy):
            if mutant == policy:
                description = f"cannot be widened; {description}"
                survived = True
            else:
                failed, _ = client.post(client.path, mutant)
                survived = not failed
                requests += 1
            if survived:
                present_keys.add(key)
                reason = explanations.get(key)
                unexplained |= not bool(reason)
                suffix = f"explained: {reason}" if reason else "unexplained"
                survivors.append(f"{label}: {description} (key={key}); {suffix}")
            else:
                killed += 1
        stale = sorted(explanations.keys() - present_keys)
        survivors.extend(f"stale survivor explanation: {name}" for name in stale)
        summary = f"{killed} mutants killed; {len(survivors) - len(stale)} survived; {requests} validate requests"
        return (1 if unexplained or stale else 0), "\n".join([*survivors, summary])
    except InfrastructureError as exc:
        detail = str(exc).replace(token, "[MASKED]") if token else str(exc)
        return 2, f"infrastructure error: {detail}"
