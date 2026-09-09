"""Quickstart: sheet-backed Task model via GDM client session."""

from __future__ import annotations

import os

from gdm import GDM, Field, GDMModel, select
from gdm.project import ResourceKind
from gdm.testing import memory_session


class Task(GDMModel, storage="sheet", mapping="partial"):
    id: str | None = Field(default=None, primary_key=True)
    title: str
    status: str = "todo"


def main() -> None:
    if os.environ.get("GDM_USE_FAKE") == "1":
        with memory_session() as session:
            tasks = session.exec(
                select(Task).where(Task.status == "open").limit(10)
            ).all()
            print(f"Open tasks: {len(tasks)}")
            session.add(Task(title="Write docs", status="open"))
            session.commit()
        return

    client = GDM.open(root_folder_id="YOUR_ROOT_FOLDER_ID")
    client.bind(
        Task,
        resource_id="YOUR_SPREADSHEET_ID",
        kind=ResourceKind.SPREADSHEET,
        worksheet="Tasks",
    )
    with client.session() as session:
        tasks = session.exec(select(Task).where(Task.status == "open").limit(10)).all()
        print(f"Open tasks: {len(tasks)}")
        session.add(Task(title="Write docs", status="open"))
        session.commit()


if __name__ == "__main__":
    main()
