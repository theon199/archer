"""Strict shared schemas: model-proposed arguments cannot supply authority."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class NotesArguments(Arguments):
    action: Literal["list", "get", "history", "create", "update", "delete"]
    id: str | None = Field(default=None, min_length=1, max_length=128)
    title: str | None = Field(default=None, max_length=300)
    content: str | None = Field(default=None, max_length=64000)
    tags: list[str] | None = Field(default=None, max_length=32)
    expected_revision: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def required_fields(self):
        allowed = {
            "list": {"action"},
            "get": {"action", "id"},
            "history": {"action", "id"},
            "create": {"action", "title", "content", "tags"},
            "update": {"action", "id", "expected_revision", "title", "content", "tags"},
            "delete": {"action", "id", "expected_revision"},
        }
        present = {key for key in self.model_fields_set if getattr(self, key) is not None}
        if present - allowed[self.action]:
            raise ValueError("Fields are not valid for this action")
        if self.action == "update" and not present.intersection({"title", "content", "tags"}):
            raise ValueError("Update requires a changed field")
        if self.action in {"get", "history", "update", "delete"} and not self.id:
            raise ValueError("Note ID is required")
        if self.action in {"update", "delete"} and self.expected_revision is None:
            raise ValueError("Expected revision is required")
        if self.tags and any(len(tag) > 100 for tag in self.tags):
            raise ValueError("Tag is too long")
        return self


class FilesArguments(Arguments):
    action: Literal["list", "read"]
    path: str = Field(default="", max_length=2048)


class SearchArguments(Arguments):
    query: str = Field(min_length=1, max_length=1000)
    count: int = Field(default=3, ge=1, le=5)


class FetchArguments(Arguments):
    url: str = Field(min_length=1, max_length=2048)


SCHEMAS = {
    "personal_notes": NotesArguments,
    "read_approved_file": FilesArguments,
    "web_search": SearchArguments,
    "web_fetch": FetchArguments,
}
