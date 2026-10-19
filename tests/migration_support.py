"""Помощники тестов миграции (блок 9)."""

from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.migration._runs import apply_step, plan_run, read_panel
from remnabay.queue import TaskContext, TaskStatus
from tests.guide_support import queued


async def run_chain(session: AsyncSession) -> None:
    """Выполняет задачи прогона по цепочке, пока они ставятся (окружение — `fake_runtime`)."""
    while True:
        rows = [
            (definition, row)
            for definition in (read_panel, plan_run, apply_step)
            for row in await queued(session, definition)
        ]
        if not rows:
            return
        for definition, row in rows:
            context = TaskContext(session=session, task_id=row.id, attempt_number=1)
            await definition.run(context, row.args)
            row.status = TaskStatus.DONE
        await session.flush()
