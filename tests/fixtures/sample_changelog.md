## 2.4.1 (2024-03-10)

**Bug Fixes**
- Fixed a crash when passing an empty list to `connect()`
- Fixed incorrect timezone handling in `parse_date()`

**Documentation**
- Updated the quickstart guide with new examples

## 2.4.0 (2024-02-01)

**Breaking Changes**
- Removed support for Python 3.7. The minimum supported version is now
  Python 3.8.

**New Features**
- Added support for async context managers in `Client`
- Introduced a new `retry_policy` configuration option

**Security Updates**
- Fixed a vulnerability (CVE-2024-00000) where credentials could leak
  into log output under certain error conditions

**Internal**
- Refactored the internal connection pool for maintainability
- Added more unit tests for edge cases

## 2.3.5 (2024-01-05)

**Deprecations**
- `Client.old_connect()` is deprecated and will be removed in 3.0. Use
  `Client.connect()` instead.

Thanks to @someuser for the contribution!
