// k6 owns scheduling and latency histograms; this file defines DiOS API traffic.
import http from 'k6/http';
import exec from 'k6/execution';
import { Counter, Rate, Trend } from 'k6/metrics';

const config = JSON.parse(open('/input/client.json'));
const semanticErrors = new Counter('semantic_errors');
const cycles = new Counter('completed_write_cycles');
const availability = new Rate('operation_success');
const latency = new Trend('operation_ms', true);
const stages = Object.fromEntries(['detail','list','deny','submit','replay','revoke']
  .map(name => [name, new Trend(`${name}_ms`, true)]));
const headers = {Authorization: `Bearer ${config.token}`, 'Content-Type': 'application/json'};
const concurrency = Number(__ENV.CONCURRENCY || 1);
const arrival = Number(__ENV.ARRIVAL_RATE || 0);
export const options = {
  scenarios: {mixed: arrival ? {
    executor: 'constant-arrival-rate', rate: arrival, timeUnit: '1s',
    duration: `${__ENV.DURATION || 10}s`, preAllocatedVUs: concurrency, maxVUs: concurrency,
    gracefulStop: '30s',
  } : {
    executor: 'shared-iterations', vus: concurrency,
    iterations: Number(__ENV.ITERATIONS || 256), maxDuration: '60s', gracefulStop: '30s',
  }},
  summaryTrendStats: ['avg', 'min', 'med', 'max', 'p(95)', 'p(99)'],
  thresholds: {semantic_errors: ['count==0']},
};

function request(method, path, value, operation, expected) {
  const response = http.request(method, config.base + path,
    value === null ? null : JSON.stringify(value),
    {headers, timeout: '15s', tags: {name: operation, operation},
      responseCallback: http.expectedStatuses(expected)});
  latency.add(response.timings.duration, {operation});
  stages[operation].add(response.timings.duration);
  availability.add(response.status === expected, {operation});
  // Capacity/transport failures remain availability failures; wrong authorization
  // responses and malformed successful results are semantic failures too.
  if (response.status !== expected) {
    if (operation === 'deny' && response.status >= 200 && response.status < 300) semanticErrors.add(1);
    return null;
  }
  try { return response.json(); }
  catch (_) { semanticErrors.add(1); return null; }
}

export default function () {
  const index = exec.scenario.iterationInTest;
  semanticErrors.add(0); cycles.add(0);
  if (index % 10 < 7) {
    const detail = index % 2 === 0;
    const result = request('GET', detail ? `/runs/${config.own}` : '/runs?limit=20',
      null, detail ? 'detail' : 'list', 200);
    if (result && (detail ? result.id !== config.own :
      !Array.isArray(result.items) || result.items.some(r => r.id === config.other))) semanticErrors.add(1);
  } else if (index % 10 < 9) {
    request('GET', `/runs/${config.other}`, null, 'deny', 404);
  } else {
    const body = {profile_id: 'review', sample_id: 'first-item', goal: 'synthetic capacity input',
      idempotency_key: `load-${index}`};
    const first = request('POST', '/runs', body, 'submit', 202);
    if (!first) return;
    if (!first.authorization || first.attempts.length !== 0) { semanticErrors.add(1); return; }
    const replay = request('POST', '/runs', body, 'replay', 202);
    if (!replay) return;
    if (replay.id !== first.id) { semanticErrors.add(1); return; }
    const revoked = request('POST', `/runs/${first.id}/revoke`,
      {expected_version: replay.version}, 'revoke', 200);
    if (!revoked) return;
    if (revoked.attempts.length !== 0 || revoked.authorization.state !== 'revoked') {
      semanticErrors.add(1); return;
    }
    cycles.add(1);
  }
}

export function handleSummary(data) {
  return {'/output/k6-summary.json': JSON.stringify(data, null, 2)};
}
