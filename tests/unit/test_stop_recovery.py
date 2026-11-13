import asyncio

import pytest

from flowengine.errors import ControllerError
from flowengine.hardware.homing.endstop import EndstopHoming
from flowengine.hardware.queue import CommandQueue


class WaitingController:
    """контроллер занят G28 и может ответить ok в момент остановки"""
    def __init__(self, ack_on_stop=False):
        self.sent = []
        self.inbox = asyncio.Queue()
        self.started = asyncio.Event()
        self.reading = 0
        self.ack_on_stop = ack_on_stop

    async def send_line(self, line):
        self.sent.append(line)
        if line.startswith('G28'):
            self.started.set()
        if line == 'M410' and self.ack_on_stop:
            await self.inbox.put('ok')

    async def read_line(self, timeout=None):
        self.reading += 1
        try:
            return await self.inbox.get()
        finally:
            self.reading -= 1


@pytest.mark.parametrize('ack_on_stop', [False, True])
async def test_stop_interrupts_homing_and_never_starts_next_axis(device_map, ack_on_stop):
    transport = WaitingController(ack_on_stop)
    queue = CommandQueue(transport)
    homing = EndstopHoming(device_map, timeout_s=60)
    task = asyncio.create_task(homing.home(queue, ['X', 'E0']))
    await asyncio.wait_for(transport.started.wait(), 1)
    await queue.abort()
    with pytest.raises(ControllerError, match='Stop'):
        await asyncio.wait_for(task, .5)
    assert queue.depth == 0
    assert transport.reading == 0
    assert transport.sent == ['G28 X', 'M410']
    await transport.inbox.put('ok')  
    with pytest.raises(ControllerError, match='aborted'):
        await queue.send('G28 E0')


async def test_stop_drops_commands_waiting_behind_homing():
    transport = WaitingController()
    queue = CommandQueue(transport)
    home = asyncio.create_task(queue.send('G28 X', timeout=60))
    await transport.started.wait()
    queued = asyncio.create_task(queue.send('G1 X10'))
    await asyncio.sleep(0)
    await queue.abort()
    results = await asyncio.wait_for(asyncio.gather(home, queued, return_exceptions=True), .5)
    assert all(isinstance(result, ControllerError) for result in results)
    assert transport.sent == ['G28 X', 'M410']
    assert queue.depth == 0
