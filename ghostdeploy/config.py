import re
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError, model_validator
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode

from ghostdeploy.core import GhostError

NAME = re.compile(r"[a-zA-Z_][a-zA-Z0-9_-]{0,62}")
MODULE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]{0,62}")
SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


def name(value: str) -> str:
    if not NAME.fullmatch(value):
        raise ValueError(
            "use 1-63 letters, digits, underscores or hyphens, starting with a letter or underscore"
        )
    return value


def branch(value: str) -> str:
    if (
        not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_./-]{0,179}", value)
        or ".." in value
        or value.endswith(".")
        or any(
            not part or part.startswith(".") or part.endswith(".lock") for part in value.split("/")
        )
    ):
        raise ValueError("use a regular Git branch name without glob characters")
    return value


def linux_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value.startswith("/")
        or value.startswith("//")
        or len(path.parts) < 2
        or ".." in path.parts
        or any(ord(char) < 32 or ord(char) == 127 or char in "\\," for char in value)
    ):
        raise ValueError(
            "use an absolute Linux path below / without traversal, commas or control characters"
        )
    return str(path)


Name = Annotated[str, AfterValidator(name)]
Branch = Annotated[str, AfterValidator(branch)]
LinuxPath = Annotated[str, AfterValidator(linux_path)]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class Project(Model):
    name: Name


class Odoo(Model):
    version: Literal["18.0", "19.0"]
    service: Name
    service_user: Name = "odoo"
    executable: LinuxPath
    interpreter: LinuxPath | None = None
    config_file: LinuxPath
    addons_path: list[LinuxPath] = Field(min_length=1, max_length=32)

    @property
    def unit(self) -> str:
        return f"{self.service}.service"


class Repository(Model):
    branch: Branch = "main"
    checkout_path: LinuxPath


class Backup(Model):
    enabled: bool = True
    path: LinuxPath = "/var/backups/ghostdeploy"
    min_free_mib: int = Field(default=512, ge=1, le=1_048_576)


class Health(Model):
    enabled: bool = True
    timeout_seconds: int = Field(default=60, ge=1, le=600)
    url: str | None = None

    @model_validator(mode="after")
    def local_url(self):
        if self.url is not None and not re.fullmatch(
            r"http://(?:127\.0\.0\.1|\[::1\])(?::[0-9]{1,5})?/[A-Za-z0-9_./-]*", self.url
        ):
            raise ValueError("health URL must use http, a loopback IP and a path without a query")
        if self.url:
            try:
                if urlsplit(self.url).port == 0:
                    raise ValueError
            except ValueError:
                raise ValueError("health URL port must be between 1 and 65535") from None
        return self


class Deployment(Model):
    environment: Literal["development", "staging", "production"]
    databases: list[Name] = Field(min_length=1, max_length=100)
    upgrade_policy: Literal["manual_review", "installed"] = "manual_review"
    backup: Backup = Field(default_factory=Backup)
    health_check: Health = Field(default_factory=Health)
    state_path: LinuxPath = "/var/lib/ghostdeploy"
    command_timeout_seconds: int = Field(default=3600, ge=10, le=86_400)


class Config(Model):
    schema_version: Literal[1]
    project: Project
    odoo: Odoo
    repository: Repository
    deployment: Deployment

    @model_validator(mode="after")
    def safety(self):
        deploy = self.deployment
        if self.odoo.service_user == "root":
            raise ValueError("the service user must be unprivileged")
        if len(set(deploy.databases)) != len(deploy.databases):
            raise ValueError("database allowlist contains duplicates")
        checkout = PurePosixPath(self.repository.checkout_path)
        roots = [PurePosixPath(path) for path in self.odoo.addons_path]
        if any(not path.is_relative_to(checkout) for path in roots):
            raise ValueError("custom addon roots must be inside the Git checkout")
        if any(
            first.is_relative_to(second)
            for i, first in enumerate(roots)
            for j, second in enumerate(roots)
            if i != j
        ):
            raise ValueError("custom addon roots must be distinct and may not overlap")
        external = [
            self.odoo.executable,
            self.odoo.config_file,
            deploy.backup.path,
            deploy.state_path,
        ]
        if self.odoo.interpreter:
            external.append(self.odoo.interpreter)
        if any(PurePosixPath(path).is_relative_to(checkout) for path in external):
            raise ValueError(
                "runtime, Odoo configuration, backups and state must be outside the checkout"
            )
        state = PurePosixPath(deploy.state_path)
        backups = PurePosixPath(deploy.backup.path)
        if state.is_relative_to(backups) or backups.is_relative_to(state):
            raise ValueError("backup and state directories must be separate and may not overlap")
        if checkout.is_relative_to(state) or checkout.is_relative_to(backups):
            raise ValueError("backup and state directories may not contain the checkout")
        if deploy.environment == "production" and (
            not deploy.backup.enabled
            or not deploy.health_check.enabled
            or not deploy.health_check.url
            or deploy.upgrade_policy != "manual_review"
        ):
            raise ValueError("production requires backups, an HTTP health URL and manual_review")
        return self

    def warnings(self) -> list[str]:
        deploy = self.deployment
        warnings = []
        if not deploy.backup.enabled:
            warnings.append("Database upgrades are blocked while backups are disabled.")
        if not deploy.health_check.enabled or not deploy.health_check.url:
            warnings.append("No HTTP probe will run. Service status checks remain enabled.")
        return warnings


class StrictLoader(yaml.SafeLoader):
    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise ConstructorError(
                None, None, "aliases are not supported", self.peek_event().start_mark
            )
        return super().compose_node(parent, index)

    def construct_mapping(self, node, deep=False):
        keys = set()
        if isinstance(node, MappingNode):
            for key, _ in node.value:
                if key.tag != "tag:yaml.org,2002:str" or key.value in keys:
                    raise ConstructorError(
                        None, None, "mapping keys must be unique strings", key.start_mark
                    )
                keys.add(key.value)
        return super().construct_mapping(node, deep=deep)


def validate(data) -> Config:
    if (
        type(data) is not dict
        or type(data.get("schema_version")) is not int
        or data["schema_version"] != 1
    ):
        raise GhostError("Configuration requires schema_version: 1.")
    try:
        return Config.model_validate(data)
    except ValidationError as exc:
        issues = []
        for error in exc.errors(include_input=False, include_context=False, include_url=False):
            if error["type"] == "extra_forbidden":
                issues.append(
                    "Unknown configuration field. Remove fields outside schema version 1."
                )
                continue
            field = ".".join(map(str, error["loc"])) or "configuration"
            issues.append(f"{field}: {error['msg']}")
        raise GhostError("; ".join(dict.fromkeys(issues))) from None


def load(path: Path) -> Config:
    try:
        with path.open("rb") as stream:
            content = stream.read(65_537)
        if len(content) > 65_536:
            raise GhostError("Configuration exceeds 64 KiB.")
        data = yaml.load(content, Loader=StrictLoader)
    except OSError as exc:
        raise GhostError(f"Cannot read configuration: {exc.strerror}") from None
    except (yaml.YAMLError, UnicodeError, RecursionError) as exc:
        mark = getattr(exc, "problem_mark", None)
        detail = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        raise GhostError(
            f"Invalid YAML{detail}. Check syntax, duplicate keys and unsupported tags or aliases."
        ) from None
    return validate(data)
