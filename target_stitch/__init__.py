#!/usr/bin/env python3

'''
Target for Stitch API.
'''

import argparse
import copy
import gzip
import http.client
import io
import json
import os
import re
import sys
import time
import urllib

from threading import Thread
from contextlib import contextmanager
from collections import namedtuple
from datetime import datetime, timezone
from decimal import Decimal
import psutil

import backoff
import requests
from requests.exceptions import RequestException, HTTPError
import pkg_resources
import singer

from jsonschema import ValidationError, Draft4Validator, FormatChecker

LOGGER = singer.get_logger()

# We use this to store schema and key properties from SCHEMA messages
StreamMeta = namedtuple('StreamMeta', ['schema', 'key_properties'])

DEFAULT_STITCH_URL = 'https://api.stitchdata.com/v2/import/batch'

class TargetStitchException(Exception):
    '''A known exception for which we don't need to print a stack trace'''
    pass

class MemoryReporter(Thread):
    '''Logs memory usage every 30 seconds'''

    def __init__(self):
        self.process = psutil.Process()
        super().__init__(name='memory_reporter', daemon=True)

    def run(self):
        while True:
            LOGGER.info('Virtual memory usage: %.2f%% of total: %s',
                        self.process.memory_percent(),
                        self.process.memory_info())
            time.sleep(30.0)


class Timings(object):
    '''Gathers timing information for the three main steps of the Tap.'''
    def __init__(self):
        self.last_time = time.time()
        self.timings = {
            'serializing': 0.0,
            'posting': 0.0,
            None: 0.0
        }

    @contextmanager
    def mode(self, mode):
        '''We wrap the big steps of the Tap in this context manager to accumulate
        timing info.'''

        start = time.time()
        yield
        end = time.time()
        self.timings[None] += start - self.last_time
        self.timings[mode] += end - start
        self.last_time = end


    def log_timings(self):
        '''We call this with every flush to print out the accumulated timings'''
        LOGGER.info('Timings: unspecified: %.3f; serializing: %.3f; posting: %.3f;',
                    self.timings[None],
                    self.timings['serializing'],
                    self.timings['posting'])

TIMINGS = Timings()


def float_to_decimal(value):
    '''Walk the given data structure and turn all instances of float into
    double.'''
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, list):
        return [float_to_decimal(child) for child in value]
    if isinstance(value, dict):
        return {k: float_to_decimal(v) for k, v in value.items()}
    return value

class BatchTooLargeException(TargetStitchException):
    '''Exception for when the records and schema are so large that we can't
    create a batch with even one record.'''
    pass


def _log_backoff(details):
    (_, exc, _) = sys.exc_info()
    LOGGER.info(
        'Error sending data to Stitch. Sleeping %d seconds before trying again: %s',
        details['wait'], exc)


class StitchHandler(object): # pylint: disable=too-few-public-methods
    '''Sends messages to Stitch.'''

    def __init__(self, token, stitch_url, max_batch_bytes):
        self.token = token
        self.stitch_url = stitch_url
        self.session = requests.Session()
        self.max_batch_bytes = max_batch_bytes

    def headers(self):
        '''Return the headers based on the token'''
        return {
            'Authorization': 'Bearer {}'.format(self.token),
            'Content-Type': 'application/json'
        }

    @backoff.on_exception(backoff.expo,
                          RequestException,
                          giveup=singer.utils.exception_is_4xx,
                          max_tries=8,
                          on_backoff=_log_backoff)
    def send(self, data):
        '''Send the given data to Stitch, retrying on exceptions'''
        url = self.stitch_url
        headers = self.headers()
        response = self.session.post(url, headers=headers, data=data)
        response.raise_for_status()
        return response


    def handle_batch(self, messages, schema, key_names):
        '''Handle messages by sending them to Stitch.

        If the serialized form of the messages is too large to fit into a
        single request this will break them up into multiple smaller
        requests.

        '''

        LOGGER.info("Sending batch with %d messages for table %s to %s",
                    len(messages), messages[0].stream, self.stitch_url)
        with TIMINGS.mode('serializing'):
            bodies = serialize(messages, schema, key_names, self.max_batch_bytes)

        LOGGER.info('Split batch into %d requests', len(bodies))
        for i, body in enumerate(bodies):
            with TIMINGS.mode('posting'):
                LOGGER.debug('Request %d of %d is %d bytes', i + 1, len(bodies), len(body))
                try:
                    self.send(body)

                # An HTTPError means we got an HTTP response but it was a
                # bad status code. Try to parse the "message" from the
                # json body of the response, since Stitch should include
                # the human-oriented message in that field. If there are
                # any errors parsing the message, just include the
                # stringified response.
                except HTTPError as exc:
                    try:
                        msg = json.loads(exc.response.json())['message']
                    except: # pylint: disable=bare-except
                        msg = '{}: {}'.format(exc.response, exc.response.content)
                    raise TargetStitchException('Error sending data to Stitch: ' + msg)

                # A RequestException other than HTTPError means we
                # couldn't even connect to stitch. The exception is likely
                # to be very long and gross. Log the full details but just
                # include the summary in the critical error message. TODO:
                # When we expose logs to Stitch users, modify this to
                # suggest looking at the logs for details.
                except RequestException as exc:
                    LOGGER.exception(exc)
                    raise TargetStitchException('Error connecting to Stitch')


class LoggingHandler(object):  # pylint: disable=too-few-public-methods
    '''Logs records to a local output file.'''
    def __init__(self, output_file, max_batch_bytes):
        self.output_file = output_file
        self.max_batch_bytes = max_batch_bytes

    def handle_batch(self, messages, schema, key_names):
        '''Handles a batch of messages by saving them to a local output file.

        Serializes records in the same way StitchHandler does, so the
        output file should contain the exact request bodies that we would
        send to Stitch.

        '''
        LOGGER.info("Saving batch with %d messages for table %s to %s",
                    len(messages), messages[0].stream, self.output_file.name)
        for i, body in enumerate(serialize(messages, schema, key_names, self.max_batch_bytes)):
            LOGGER.debug("Request body %d is %d bytes", i, len(body))
            self.output_file.write(body)
            self.output_file.write('\n')


class ValidatingHandler(object): # pylint: disable=too-few-public-methods
    '''Validates input messages against their schema.'''

    def handle_batch(self, messages, schema, key_names): # pylint: disable=no-self-use
        '''Handles messages by validating them against schema.'''
        schema = float_to_decimal(schema)
        validator = Draft4Validator(schema, format_checker=FormatChecker())
        for i, message in enumerate(messages):
            if isinstance(message, singer.RecordMessage):
                data = float_to_decimal(message.record)
                validator.validate(data)
                if key_names:
                    for k in key_names:
                        if k not in data:
                            raise TargetStitchException(
                                'Message {} is missing key property {}'.format(
                                    i, k))
        LOGGER.info('Batch is valid')


def serialize(messages, schema, key_names, max_bytes):
    '''Produces request bodies for Stitch.

    Builds a request body consisting of all the messages. Serializes it as
    JSON. If the result exceeds the request size limit, splits the batch
    in half and recurs.

    '''
    serialized_messages = []
    for message in messages:
        if isinstance(message, singer.RecordMessage):
            serialized_messages.append({
                'action': 'upsert',
                'data': message.record,
                'vintage': datetime.now(timezone.utc).isoformat(),
                'sequence': int(time.time() * 1000)})
        elif isinstance(message, singer.ActivateVersionMessage):
            serialized_messages.append({
                'action': 'activate_version',
                'sequence': int(time.time() * 1000)})

    body = {
        'table_name': messages[0].stream,
        'schema': schema,
        'key_names': key_names,
        'messages': serialized_messages
    }
    if messages[0].version is not None:
        body['table_version'] = messages[0].version

    serialized = json.dumps(body)
    LOGGER.debug('Serialized %d messages into %d bytes', len(messages), len(serialized))

    if len(serialized) < max_bytes:
        return [serialized]

    if len(messages) <= 1:
        raise BatchTooLargeException("A single record is larger than batch size limit of %d")

    pivot = len(messages) // 2
    l_half = serialize(messages[:pivot], schema, key_names, max_bytes)
    r_half = serialize(messages[pivot:], schema, key_names, max_bytes)
    return l_half + r_half


class TargetStitch(object):
    '''Encapsulates most of the logic of target-stitch.

    Useful for unit testing.

    '''

    # pylint: disable=too-many-instance-attributes
    def __init__(self, # pylint: disable=too-many-arguments
                 handlers,
                 state_writer,
                 max_batch_bytes,
                 max_batch_records,
                 batch_delay_seconds):
        self.messages = []
        self.buffer_size_bytes = 0
        self.state = None

        # Mapping from stream name to {'schema': ..., 'key_names': ...}
        self.stream_meta = {}

        # Instance of StitchHandler
        self.handlers = handlers

        # Writer that we write state records to
        self.state_writer = state_writer

        # Batch size limits. Stored as properties here so we can easily
        # change for testing.
        self.max_batch_bytes = max_batch_bytes
        self.max_batch_records = max_batch_records

        # Minimum frequency to send a batch, used with self.time_last_batch_sent
        self.batch_delay_seconds = batch_delay_seconds

        # Time that the last batch was sent
        self.time_last_batch_sent = time.time()



    def flush(self):
        '''Send all the buffered messages to Stitch.'''

        if self.messages:
            stream_meta = self.stream_meta[self.messages[0].stream]
            for handler in self.handlers:
                handler.handle_batch(self.messages, stream_meta.schema, stream_meta.key_properties)
            self.time_last_batch_sent = time.time()
            self.messages = []
            self.buffer_size_bytes = 0

        if self.state:
            line = json.dumps(self.state)
            LOGGER.debug('Emitting state %s', line)
            self.state_writer.write("{}\n".format(line))
            self.state_writer.flush()
            self.state = None
            TIMINGS.log_timings()

    def handle_line(self, line):

        '''Takes a raw line from stdin and handles it, updating state and possibly
        flushing the batch to the Gate and the state to the output
        stream.

        '''

        message = singer.parse_message(line)

        # If we got a Schema, set the schema and key properties for this
        # stream. Flush the batch, if there is one, in case the schema is
        # different.
        if isinstance(message, singer.SchemaMessage):
            self.flush()
            self.stream_meta[message.stream] = StreamMeta(
                message.schema, message.key_properties)

        elif isinstance(message, (singer.RecordMessage, singer.ActivateVersionMessage)):
            if self.messages and (
                    message.stream != self.messages[0].stream or
                    message.version != self.messages[0].version):
                self.flush()
            self.messages.append(message)
            self.buffer_size_bytes += len(line)

            num_bytes = self.buffer_size_bytes
            num_messages = len(self.messages)
            num_seconds = time.time() - self.time_last_batch_sent

            enough_bytes = num_bytes >= self.max_batch_bytes
            enough_messages = num_messages >= self.max_batch_records
            enough_time = num_seconds >= self.batch_delay_seconds
            if enough_bytes or enough_messages or enough_time:
                LOGGER.info('Flushing %d bytes, %d messages, after %.2f seconds',
                            num_bytes, num_messages, num_seconds)
                self.flush()

        elif isinstance(message, singer.StateMessage):
            self.state = message.value


    def consume(self, reader):
        '''Consume all the lines from the queue, flushing when done.'''
        for line in reader:
            self.handle_line(line)
        self.flush()


def collect():
    '''Send usage info to Stitch.'''

    try:
        version = pkg_resources.get_distribution('target-stitch').version
        conn = http.client.HTTPSConnection('collector.stitchdata.com', timeout=10)
        conn.connect()
        params = {
            'e': 'se',
            'aid': 'singer',
            'se_ca': 'target-stitch',
            'se_ac': 'open',
            'se_la': version,
        }
        conn.request('GET', '/i?' + urllib.parse.urlencode(params))
        conn.getresponse()
        conn.close()
    except: # pylint: disable=bare-except
        LOGGER.debug('Collection request failed')


def use_batch_url(url):
    '''Replace /import/push with /import/batch in URL'''
    result = url
    if url.endswith('/import/push'):
        result = url.replace('/import/push', '/import/batch')
        LOGGER.info("I can't hit Stitch's /push endpoint, using " +
                    "/batch endpoint instead. Changed %s to %s", url, result)
    return result

def main_impl():
    '''We wrap this function in main() to add exception handling'''
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '-c', '--config',
        help='Config file',
        type=argparse.FileType('r'))
    parser.add_argument(
        '-n', '--dry-run',
        help='Dry run - Do not push data to Stitch',
        action='store_true')
    parser.add_argument(
        '-o', '--output-file',
        help='Save requests to this output file',
        type=argparse.FileType('w'))
    parser.add_argument('--max-batch-records', type=int, default=20000)
    parser.add_argument('--max-batch-bytes', type=int, default=4000000)
    parser.add_argument('--batch-delay-seconds', type=float, default=300.0)
    args = parser.parse_args()

    handlers = []
    if args.output_file:
        handlers.append(LoggingHandler(args.output_file, args.max_batch_bytes))
    if args.dry_run:
        handlers.append(ValidatingHandler())
    elif not args.config:
        parser.error("config file required if not in dry run mode")
    else:
        config = json.load(args.config)
        token = config.get('token')
        stitch_url = use_batch_url(config.get('stitch_url', DEFAULT_STITCH_URL))

        if not token:
            raise Exception('Configuration is missing required "token" field')

        if not config.get('disable_collection'):
            LOGGER.info('Sending version information to stitchdata.com. ' +
                        'To disable sending anonymous usage data, set ' +
                        'the config parameter "disable_collection" to true')
            Thread(target=collect).start()
        handlers.append(StitchHandler(token, stitch_url, args.max_batch_bytes))

    # queue = Queue(args.max_batch_records)
    reader = io.TextIOWrapper(sys.stdin.buffer, encoding='utf-8')
    TargetStitch(handlers,
                 sys.stdout,
                 args.max_batch_bytes,
                 args.max_batch_records,
                 args.batch_delay_seconds).consume(reader)
    LOGGER.info("Exiting normally")

def main():
    '''Main entry point'''
    try:
        MemoryReporter().start()
        main_impl()

    # If we catch an exception at the top level we want to log a CRITICAL
    # line to indicate the reason why we're terminating. Sometimes the
    # extended stack traces can be confusing and this provides a clear way
    # to call out the root cause. If it's a known TargetStitchException we
    # can suppress the stack trace, otherwise we should include the stack
    # trace for debugging purposes, so re-raise the exception.
    except TargetStitchException as exc:
        LOGGER.critical(exc)
        sys.exit(1)
    except Exception as exc:
        LOGGER.critical(exc)
        raise exc

if __name__ == '__main__':
    main()
