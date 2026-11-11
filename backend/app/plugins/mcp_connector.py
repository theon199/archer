"""Reviewed fixed MCP mappings. Production launches only isolated local images.

Native commands exist solely for explicitly labeled development/test fixtures.
No server descriptions, annotations, sampling or elicitation convey authority.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import stat
import uuid
from pathlib import Path

from jsonschema import Draft202012Validator
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, get_default_environment
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.config import settings
from app.plugins.mcp_transport import bounded_stdio_client as stdio_client
from app.storage.database import get_db


class SDKLogRedaction(logging.Filter):
    def filter(self, record):
        record.msg = "MCP SDK diagnostic; server payload redacted"
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True


for _logger_name in ["mcp.client.stdio", "mcp.client.session", "mcp.shared.dispatcher"]:
    logging.getLogger(_logger_name).addFilter(SDKLogRedaction())


class Mapping(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(pattern=r"^mcp_[a-z0-9_]{1,48}$")
    account_id: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=500)
    remote_tool: str = Field(min_length=1, max_length=128)
    input_schema: dict
    output_schema: dict
    # Digest of the complete advertised tool object, not merely annotations.
    advertised_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    image: str | None = None
    development_command: list[str] | None = Field(default=None, max_length=32)
    read_only: bool

    @model_validator(mode="after")
    def reviewed(self):
        if not self.read_only:
            raise ValueError("MCP writes require a separate reconciliation design")
        if bool(self.image) == bool(self.development_command):
            raise ValueError("Choose exactly one launch profile")
        if self.image and not re.fullmatch(r"(?:[a-zA-Z0-9._/:-]+@)?sha256:[a-f0-9]{64}", self.image):
            raise ValueError("Container image must be pinned by digest")
        for schema in [self.input_schema, self.output_schema]:
            # Local closed schemas only: no remote retrieval or arbitrary resolvers.
            text = json.dumps(schema)
            if (
                "$ref" in text
                or "$dynamicRef" in text
                or schema.get("type") != "object"
                or schema.get("additionalProperties") is not False
            ):
                raise ValueError("Use closed object schemas without references")
            Draft202012Validator.check_schema(schema)
        if self.development_command and (
            not Path(self.development_command[0]).is_absolute() or settings.env not in {"test", "development"}
        ):
            raise ValueError("Native MCP commands are restricted to explicit development/tests")
        return self


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class Validated:
    def __init__(self, arguments):
        self.arguments = arguments

    def model_dump(self, **kwargs):
        return self.arguments


class ReviewedSchema:
    def __init__(self, schema):
        self.schema = schema

    def model_json_schema(self):
        return self.schema

    def model_validate(self, arguments):
        if not isinstance(arguments, dict) or len(json.dumps(arguments).encode()) > 16384:
            raise ValueError("MCP arguments exceed limit")
        if not Draft202012Validator(self.schema).is_valid(arguments):
            raise ValueError("Invalid MCP arguments")
        return Validated(arguments)


def container_command(image, container, identity=None):
    command = [
        "/usr/bin/docker",
        "run",
        "--rm",
        "--pull=never",
        "-i",
        "--name",
        container,
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--user=65534:65534",
        "--pids-limit=64",
        "--memory=256m",
        "--cpus=1",
        "--tmpfs=/tmp:rw,noexec,nosuid,size=16m",
        "--log-driver=none",
        image,
    ]
    if identity is not None:
        command[-1:-1] = [
            "--label",
            "archer.resource-token=" + identity["identity_token"],
            "--label",
            "archer.worker-owner=" + identity["worker_owner"],
        ]
    return command


async def docker_control(*arguments):
    process = await asyncio.create_subprocess_exec(
        "/usr/bin/docker",
        *arguments,
        env={"PATH": "/usr/bin:/bin", "HOME": "/tmp"},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), 5)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    return process.returncode, output


class MCPResourceCleanupError(RuntimeError):
    """Container cleanup is unverified; do not report a known completed outcome."""


async def remove_owned_container(container, ownership=None):
    # Names are generated internally. Do not accept caller/model-controlled names.
    if not re.fullmatch(r"archer-mcp-[a-f0-9]{32}", container):
        raise ValueError("Invalid owned connector container name")
    try:
        target = container
        if ownership is not None:
            code, raw = await docker_control("container", "inspect", container)
            if code == 0:
                value = json.loads(raw)[0]
                labels = value["Config"].get("Labels") or {}
                if (
                    labels.get("archer.resource-token") != ownership["identity_token"]
                    or labels.get("archer.worker-owner") != ownership["worker_owner"]
                    or value["Config"]["Image"] != ownership["image_reference"]
                    or not re.fullmatch(r"[a-f0-9]{64}", value["Id"])
                ):
                    raise MCPResourceCleanupError("Container identity mismatch; manual review required")
                target = value["Id"]
            else:
                code, remaining = await docker_control(
                    "container", "ls", "--all", "--filter", f"name=^/{container}$", "--format", "{{.Names}}"
                )
                if code != 0 or remaining.strip():
                    raise MCPResourceCleanupError("Cannot verify missing resource")
                return
        await docker_control("rm", "-f", target)
        code, remaining = await docker_control(
            "container", "ls", "--all", "--filter", f"name=^/{container}$", "--format", "{{.Names}}"
        )
        if code != 0 or remaining.strip():
            raise MCPResourceCleanupError("Cannot verify owned connector container removal")
    except Exception:
        raise MCPResourceCleanupError("Cannot verify owned connector container removal") from None


async def complete_cleanup(awaitable):
    task = asyncio.create_task(awaitable)
    interrupted = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            interrupted = True
    task.result()
    if interrupted:
        raise asyncio.CancelledError


class ReviewedMCP:
    fixed_action = "invoke"

    def __init__(self, mapping):
        self.mapping = mapping
        self.name = mapping.name
        self.description = mapping.description
        self.account_id = mapping.account_id
        self.version = digest(mapping.model_dump())

    async def currently_granted(self, account_id):
        async with get_db() as db:
            grant = await (
                await db.execute(
                    "SELECT enabled,tool_version FROM tool_grants WHERE account_id=? AND tool_name=? AND action='invoke'",
                    (account_id, self.name),
                )
            ).fetchone()
            return bool(grant and grant["enabled"] and grant["tool_version"] == self.version)

    async def execute(self, account_id, arguments, *, operation_id=None):
        if account_id != self.account_id:
            return {"success": False, "status": "denied", "error": "Connector is not owned by this account"}
        mapping = self.mapping
        container = None
        resource = None
        try:
            if mapping.image:
                container = "archer-mcp-" + uuid.uuid4().hex
                from app.plugins.mcp_ownership import require_worker, reserve

                resource = {
                    "name": container,
                    "identity_token": uuid.uuid4().hex,
                    "account_id": account_id,
                    "operation_id": operation_id,
                    "image_reference": mapping.image,
                    "worker_owner": require_worker(),
                }
                await reserve(container, resource["identity_token"], account_id, operation_id, mapping.image)
                command = container_command(mapping.image, container, resource)
            else:
                if settings.env not in {"test", "development"}:
                    raise ValueError("Native MCP test commands cannot run in production")
                command = mapping.development_command
            # SDK merges safe default env keys: override even HOME/PATH, no ambient keys.
            environment = {key: "" for key in get_default_environment()}
            environment.update(HOME="/tmp", PATH="/usr/bin:/bin", LANG="C.UTF-8")
            params = StdioServerParameters(command=command[0], args=command[1:], env=environment, cwd="/tmp")
            with open(os.devnull, "w") as errors:
                async with stdio_client(params, errlog=errors) as (read, write):
                    async with ClientSession(read, write, read_timeout_seconds=5) as session:
                        await session.initialize()
                        tools = await session.list_tools()
                        matches = [tool for tool in tools.tools if tool.name == mapping.remote_tool]
                        if (
                            len(matches) != 1
                            or digest(matches[0].model_dump(mode="json", by_alias=True, exclude_none=True))
                            != mapping.advertised_sha256
                        ):
                            return {
                                "success": False,
                                "status": "denied",
                                "error": "MCP advertised definition changed; owner review required",
                            }
                        if not await self.currently_granted(account_id):
                            return {"success": False, "status": "denied", "error": "Connector grant was revoked"}
                        advertised = matches[0].model_dump(mode="json", by_alias=True, exclude_none=True)
                        if any(marker in json.dumps(advertised) for marker in ["$ref", "$dynamicRef"]):
                            return {
                                "success": False,
                                "status": "denied",
                                "error": "MCP referenced schemas are unsupported",
                            }
                        result = await session.call_tool(mapping.remote_tool, arguments, read_timeout_seconds=5)
                        if not await self.currently_granted(account_id):
                            return {"success": False, "status": "denied", "error": "Connector grant was revoked"}
                        if result.is_error:
                            return {"success": False, "status": "failed", "error": "MCP server reported failure"}
                        value = result.structured_content
                        if (
                            value is None
                            or len(json.dumps(value).encode()) > 65536
                            or not Draft202012Validator(mapping.output_schema).is_valid(value)
                        ):
                            return {
                                "success": False,
                                "status": "failed",
                                "error": "MCP result violates reviewed output contract",
                            }
                        return {"success": True, "status": "succeeded", "data": value, "untrusted_content": True}
        finally:
            if resource is not None:
                from app.plugins.mcp_ownership import release

                await complete_cleanup(release(resource))


def load_mappings(path):
    if path is None:
        return []
    # Deployment-owned config; never from model/API input. Reject unsafe file modes.
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor) as source:
        info = os.fstat(source.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_size > 131072
        ):
            raise ValueError("MCP config must be owned, regular, private and bounded")
        values = json.load(source)
    if not isinstance(values, list) or len(values) > 16:
        raise ValueError("Expected at most sixteen reviewed MCP tools")
    mappings = [Mapping.model_validate(value) for value in values]
    if len({m.name for m in mappings}) != len(mappings):
        raise ValueError("Duplicate MCP names")
    return mappings
