"""Sheet-backed Task model for tutorials."""

from gdm import Field, GDMModel


class Task(GDMModel, storage="sheet", mapping="partial"):
    id: str | None = Field(default=None, primary_key=True)
    title: str
    status: str = "todo"
    assignee: str | None = None
