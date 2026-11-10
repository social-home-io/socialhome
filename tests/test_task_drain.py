"""Tests for :mod:`socialhome.task_drain`."""

from __future__ import annotations

import asyncio

from socialhome.task_drain import wait_tasks_done


async def test_returns_while_a_finished_task_still_waits_for_its_discard():
    """A finished task left in the set (its discard callback not run yet)
    must not make the wait spin — awaiting it never yields to the loop."""
    done: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    done.set_result(None)
    tasks = {done}  # finished, never discarded
    await asyncio.wait_for(wait_tasks_done(lambda: tasks), timeout=5)


async def test_waits_for_running_tasks_and_ones_started_meanwhile():
    tasks: set[asyncio.Task[None]] = set()
    finished: list[str] = []

    async def late() -> None:
        await asyncio.sleep(0)
        finished.append("late")

    async def first() -> None:
        await asyncio.sleep(0)
        task = asyncio.create_task(late())
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        finished.append("first")

    task = asyncio.create_task(first())
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    await wait_tasks_done(lambda: tasks)
    assert finished == ["first", "late"]


async def test_a_failing_task_does_not_raise():
    async def boom() -> None:
        raise RuntimeError("no")

    tasks = {asyncio.create_task(boom())}
    await wait_tasks_done(lambda: tasks)


async def test_dict_values_work():
    drains = {"a": asyncio.create_task(asyncio.sleep(0))}
    await wait_tasks_done(drains.values)
    assert drains["a"].done()
