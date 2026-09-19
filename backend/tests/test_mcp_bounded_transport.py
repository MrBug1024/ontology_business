"""Reject oversized MCP wire messages before consumers deserialize them."""
import asyncio

import httpx
import pytest

from app.services.mcp_bounded_transport_service import BoundedTransport, MCPResponseLimitError, _Budget


class Chunks(httpx.AsyncByteStream):
    def __init__(self, *chunks):
        self.chunks, self.reads, self.closed = chunks, 0, False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk

    async def aclose(self):
        self.closed = True


def read(stream, headers, *, limit=16, frame_limit=12):
    async def run():
        def respond(request):
            assert request.headers['Accept-Encoding'] == 'identity'
            return httpx.Response(200, headers=headers, stream=stream)
        transport = BoundedTransport(httpx.MockTransport(respond), _Budget(limit), frame_limit)
        async with httpx.AsyncClient(transport=transport) as client:
            async with client.stream('GET', 'https://synthetic.invalid/mcp') as response:
                return b''.join([chunk async for chunk in response.aiter_bytes()])
    return asyncio.run(run())


def test_chunked_json_is_rejected_before_reading_remaining_payload_and_is_closed():
    stream = Chunks(b'{"text":"', b'x' * 10, b'y' * 10, b'"}')
    with pytest.raises(MCPResponseLimitError):
        read(stream, {'content-type': 'application/json'})
    assert stream.reads == 2
    assert stream.closed


@pytest.mark.parametrize('headers', [{'content-length': '100'}, {'content-encoding': 'gzip'}, {'content-length': '-1'}])
def test_declared_oversize_or_compressed_responses_are_closed_without_reading(headers):
    stream = Chunks(b'Never read')
    with pytest.raises(MCPResponseLimitError):
        read(stream, headers)
    assert stream.reads == 0
    assert stream.closed


def test_sse_delimiters_cross_network_chunks_without_losing_the_per_event_limit():
    stream = Chunks(b'data: a\r\n\r', b'\ndata: b\n', b'\n')
    assert read(stream, {'content-type': 'text/event-stream'}, limit=32) == b'data: a\r\n\r\ndata: b\n\n'
    oversized = Chunks(b'data: ', b'x' * 8, b'\n\n')
    with pytest.raises(MCPResponseLimitError, match='事件'):
        read(oversized, {'content-type': 'text/event-stream'}, limit=32)
    assert oversized.reads == 2
    assert oversized.closed


def test_session_budget_counts_multiple_responses_instead_of_resetting_per_request():
    async def run():
        transport = BoundedTransport(httpx.MockTransport(lambda request: httpx.Response(200, stream=Chunks(b'123456'))), _Budget(10), 12)
        async with httpx.AsyncClient(transport=transport) as client:
            assert (await client.get('https://synthetic.invalid/one')).text == '123456'
            with pytest.raises(MCPResponseLimitError):
                await client.get('https://synthetic.invalid/two')
    asyncio.run(run())
