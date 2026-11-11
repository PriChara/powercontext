// Original fixture. Titles are stable IDs for the relevance judgments below.
export const sample = [
  ['Retry policy', 'For temporary HTTP failures, retry with exponential backoff and random jitter. Respect Retry-After on status 429.'],
  ['Key rotation', 'Rotate service credentials by adding the replacement key, deploying it to callers, and revoking the old key after confirming migration.'],
  ['Backup recovery', 'Restore the most recent database snapshot and replay the transaction logs to the chosen timestamp. Test the recovery process regularly.'],
  ['Cache lifetime', 'A cache entry uses a time to live (TTL) to expire stale data. Explicit invalidation is helpful after an update.'],
  ['Keyboard access', 'All controls must be reachable by keyboard. Provide a visible focus indicator and a logical tab order.'],
  ['Search ranking', 'Reciprocal rank fusion combines the positions of documents from several result lists. It does not depend on comparing raw scores.'],
  ['Large uploads', 'Split a large upload into numbered parts. Retry each part independently and verify its checksum before completing the upload.'],
  ['Alert routing', 'Send critical incidents to the on-call engineer. Group duplicate notifications and include a runbook in each alert.'],
  ['Browser privacy', 'Perform embedding inference inside the browser. Sending only the vectors and text to a local database avoids using a hosted inference endpoint.'],
  ['Index visibility', 'A successful committed write is available to later transactions. Verify the newest document with a fresh search after the import finishes.'],
  ['Rate control', 'A token bucket permits short bursts while enforcing a steady average request rate. Reject requests when no tokens remain.'],
  ['Batch inference', 'Feed several inputs to the model together to reduce per-request overhead. Group inputs by length to avoid excessive padding.'],
].map(([title, text]) => ({ title, text, part: 1 }));

export const queries = [
  ['how to wait progressively longer after transient requests fail', 'Retry policy'],
  ['move all clients to a new credential before disabling the original one', 'Key rotation'],
  ['recover the database to a point in time', 'Backup recovery'],
  ['use a form without a mouse', 'Keyboard access'],
  ['reciprocal rank fusion', 'Search ranking'],
  ['avoid wasting time on model padding when processing many texts', 'Batch inference'],
  ['limit traffic but tolerate bursts', 'Rate control'],
  ['when does a newly inserted document show up in search', 'Index visibility'],
];
