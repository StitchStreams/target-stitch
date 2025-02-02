# Changelog

## 4.0.1
  * Bump aiohttp from 3.8.5 to 3.11.9
  * Bump requests from 2.31.0 to 3.32.3 [#112] (https://github.com/singer-io/target-stitch/pull/112)

## 4.0.0
  * Bump singer-python to version `6.0.0`, which adds support for python `3.10+` but is no longer compatible with python `3.5` 
  * Bumps requests and aiohttp libraries to more secure versions [#108](https://github.com/singer-io/target-stitch/pull/108)

## 3.2.2
  * Remove unused dependencies [#107](https://github.com/singer-io/target-stitch/pull/107)

## 3.2.1
  * Updated dependencies to support Python 3.9.6, deprecated support for Python 3.5.X. [#104](https://github.com/singer-io/target-stitch/pull/104)

## 3.2.0
  * Log how many records appear in a batch and note the number of bytes [#98](https://github.com/singer-io/target-stitch/pull/98)

## 3.1.1
  * Fix a bug related to buffering records per stream that would cause state to not be emitted during certain edge conditions [#96](https://github.com/singer-io/target-stitch/pull/96)

## 3.1.0
  * Buffer records per stream so that changing streams does not flush records [#94](https://github.com/singer-io/target-stitch/pull/94)

## 3.0.3
  * Generates sequence numbers based on nanosecond time to avoid collisions with small, async batches [#90](https://github.com/singer-io/target-stitch/pull/90)

## 3.0.1
  * Removes requirement for `connection_ns` property.

## 3.0.0
  * Adds new configuration properties - `small_batch_url`, `big_batch_url` and `batch_size_preferences` - for internal Stitch use.

## 2.0.7
  * Any exception in flush_state callback will set SEND_EXCEPTION resulting in the termination of the main thread and process.

## 2.0.5
  * Emits final state after all records have been pushed to Stitch, before exit [#71](https://github.com/singer-io/target-stitch/pull/71)

## 1.8.1
  * Updates `requests` to version `2.20.0` in response to CVE 2018-18074

## 1.7.6
  * Flush buffer if enough time has passed when state message is received [#57](https://github.com/singer-io/target-stitch/pull/57)

## 1.7.5
  * Throw an error in the ValidationHandler if schema validation fails.

## 1.7.4
  * Generate unique sequence numbers based on the current time millis with an appended zero-padded message number

## 1.7.3
  * Update to singer-python==5.0.15 to use the change to `RecordMessage.asdict` for serialization of `time_extracted`

## 1.7.2
  * Updates serialize to format `time_extracted` in a cross platform way, using `singer.utils.strftime`

## 1.7.1
  * Allows the push to the Stitch API to bypass SSL verification if an env variable is set [#45](https://github.com/singer-io/target-stitch/pull/45)
  * Updates error message to clarify when a message is too large for the Stitch API [#47](https://github.com/singer-io/target-stitch/pull/47)
