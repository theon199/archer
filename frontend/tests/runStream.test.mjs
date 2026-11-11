import assert from 'node:assert/strict';
import test from 'node:test';
import { runEvents } from '../src/runStream.ts';

function fragmentedResponse(text) {
  const bytes = new TextEncoder().encode(text);
  let index = 0;
  return new Response(new ReadableStream({
    pull(controller) {
      // Splits CRLF, field names, JSON, and multibyte Unicode boundaries.
      if (index < bytes.length) controller.enqueue(bytes.slice(index, ++index));
      else controller.close();
    },
  }));
}

test('fragmented SSE preserves Unicode, skips duplicates and carries resume cursor', async () => {
  const token = 'Plan 🌍\nnext line';
  const text = ': heartbeat\r\n\r\nid: 1\r\nevent: token\r\ndata: {"token":"already seen"}\r\n\r\n'
    + `id: 2\r\nevent: token\r\ndata: ${JSON.stringify({ token })}\r\n\r\n`
    + 'id: 2\r\nevent: token\r\ndata: {"token":"duplicate"}\r\n\r\n'
    + 'id: 3\r\nevent: done\r\ndata: {"status":"succeeded"}\r\n\r\n';
  const events = [];
  const fetcher = async (url, options) => {
    assert.equal(url, '/api/v1/runs/run-test/events');
    assert.equal(options.headers['Last-Event-ID'], '1');
    return fragmentedResponse(text);
  };
  for await (const event of runEvents(fetcher, 'run-test', 1)) events.push(event);
  assert.deepEqual(events.map(e => e.id), [2, 3]);
  assert.equal(events[0].data.token, token);
});

test('revoked session and unavailable stream are errors', async () => {
  await assert.rejects(async () => {
    for await (const _ of runEvents(async () => fragmentedResponse('event: auth_expired\ndata: {}\n\n'), 'run', 0)) {}
  }, /Session expired/);
  await assert.rejects(async () => {
    for await (const _ of runEvents(async () => new Response('', { status: 401 }), 'run', 0)) {}
  }, /unavailable/);
});

test('closing the reader cancels its stream', async () => {
  let canceled = false;
  const response = new Response(new ReadableStream({
    start(controller) { controller.enqueue(new TextEncoder().encode('id: 1\nevent: state\ndata: {"status":"queued"}\n\n')); },
    cancel() { canceled = true; },
  }));
  const iterator = runEvents(async () => response, 'run', 0);
  assert.equal((await iterator.next()).value.type, 'state');
  await iterator.return();
  assert.equal(canceled, true);
});
