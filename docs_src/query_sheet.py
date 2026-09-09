"""Query sheet rows through a session."""

from __future__ import annotations

import os

from models_task import Task

from gdm import GDM, select
from gdm.project import ResourceKind
from gdm.testing import memory_session


def main() -> None:
    if os.environ.get("GDM_USE_FAKE") == "1":
        with memory_session() as session:
            rows = session.exec(
                select(Task)
                .where(Task.status == "open")
                .where(Task.assignee != None)  # noqa: E711
                .order_by(Task.title)
                .limit(50)
            ).all()
            print(f"Open assigned tasks: {len(rows)}")
        return

    client = GDM.open(root_folder_id="YOUR_ROOT_FOLDER_ID")
    client.bind(
        Task,
        resource_id="YOUR_SPREADSHEET_ID",
        kind=ResourceKind.SPREADSHEET,
        worksheet="Tasks",
    )
    with client.session(scan_limit=500) as session:
        rows = session.exec(
            select(Task)
            .where(Task.status == "open")
            .where(Task.assignee != None)  # noqa: E711
            .order_by(Task.title)
            .limit(50)
        ).all()
        print(f"Open assigned tasks: {len(rows)}")


if __name__ == "__main__":
    main()
