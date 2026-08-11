1.9.2 (2024-04-01)
------------------

**Bug Fixes**

- Fixed a memory leak in the connection pool that occurred when a
  request timed out before completing.
- Fixed incorrect handling of redirects for HEAD requests.

1.9.1 (2024-03-15)
------------------

**Performance Improvements**

- Reduced memory usage of the response cache by roughly 30% for large
  payloads.

**Other**

- Various internal cleanups and CI improvements.

1.9.0 (2024-02-20)
------------------

**Breaking Changes**

- The `timeout` parameter now defaults to 30 seconds instead of None
  (no timeout). Existing callers relying on indefinite waits must set
  `timeout=None` explicitly.

**New Features**

- Added a `Session.stream()` method for streaming large responses.
