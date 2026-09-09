"""Create and update sheet rows through a GDM session."""

from __future__ import annotations

import os

from models_task import Task

from gdm import GDM
from gdm.project import ResourceKind
from gdm.testing import memory_session


def main() -> None:
    if os.environ.get("GDM_USE_FAKE") == "1":
        with memory_session() as session:
            session.add(Task(title="Ship docs", status="open"))
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
        session.add(Task(title="Ship docs", status="open"))
        session.commit()


if __name__ == "__main__":
    main()
