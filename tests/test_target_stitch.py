import unittest
import target_stitch
import json
import io
import mock
import sys
import datetime
import dateutil
import jsonschema
from strict_rfc3339 import rfc3339_to_timestamp
from dateutil import tz

class DummyClient(target_stitch.DryRunClient):

    def __init__(self, buffer_size=100):
        super().__init__(buffer_size=buffer_size)
        self.messages = []

    def push(self, message, callback_arg=None):
        self.messages.append(message)
        super().push(message, callback_arg)


def message_lines(messages):
    return [json.dumps(m) for m in messages]


def persist_all(recs):
    with DummyClient() as client:
        target_stitch.persist_lines(client, message_lines(recs))
        return client.messages


def state(i):
    return {"type": "STATE", "value": i}
def record(i):
    return {"type": "RECORD", "stream": "foo", "record": {"i": i}}

schema = {"type": "SCHEMA",
          "stream": "foo",
          "key_properties": ["i"],
          "schema": {"properties": {"i": {"type": "integer"}}}
}

def load_sample_lines(filename):
    with open('tests/' + filename) as fp:
        return [line for line in fp]


class TestTargetStitch(unittest.TestCase):

    def test_persist_lines_fails_without_key_properties(self):
        recs = [
            {"type": "SCHEMA",
             "stream": "users",
             "schema": {
                 "properties": {
                     "id": {"type": "integer"},
                     "name": {"type": "string"}}}}]

        with DummyClient() as client:
            with self.assertRaises(Exception):
                target_stitch.persist_lines(client, message_lines(recs))

    def test_persist_lines_works_with_empty_key_properties(self):
        lines = load_sample_lines('empty_key_properties.json')
        with DummyClient() as client:
            target_stitch.persist_lines(client, lines)
            self.assertEqual(len(client.messages), 1)
            self.assertEqual(client.messages[0]['key_names'], [])


    def test_persist_lines_fails_without_keys(self):
        inputs = [
            {"type": "SCHEMA",
             "stream": "users",
             "key_properties": ["id"],
             "schema": {
                 "properties": {
                     "id": {"type": "integer"},
                     "name": {"type": "string"}}}},
            {"type": "RECORD",
             "stream": "users",
             "record": {"name": "Joshua"}}]

        with DummyClient() as client:
            with self.assertRaises(Exception):
                target_stitch.persist_lines(client, message_lines(inputs))

    def test_persist_lines_sets_key_names(self):
        inputs = [
            {"type": "SCHEMA",
             "stream": "users",
             "key_properties": ["id"],
             "schema": {
                 "properties": {
                     "id": {"type": "integer"},
                     "name": {"type": "string"}}}},
            {"type": "RECORD",
             "stream": "users",
             "record": {"id": 1, "name": "mike"}}]

        with DummyClient() as client:
            target_stitch.persist_lines(client, message_lines(inputs))
            self.assertEqual(len(client.messages), 1)
            self.assertEqual(client.messages[0]['key_names'], ['id'])

    def test_persist_lines_converts_date_time(self):
        inputs = [
            {"type": "SCHEMA",
             "stream": "users",
             "key_properties": ["id"],
             "schema": {
                 "properties": {
                     "id": {"type": "integer"},
                     "t": {"type": "string", "format": "date-time"}}}},
            {"type": "RECORD",
             "stream": "users",
             "record": {"id": 1, "t": "2017-02-27T00:00:00+00:00"}}]

        with DummyClient() as client:
            target_stitch.persist_lines(client, message_lines(inputs))
            dt = client.messages[0]['data']['t']
            self.assertEqual(datetime.datetime, type(dt))

    def test_poo(self):
        def old_parse(s):
            return datetime.datetime.utcfromtimestamp(rfc3339_to_timestamp(s)).replace(tzinfo=tz.tzutc())
        def new_parse(s):
            return dateutil.parser.parse(s)
        dt = '2017-02-27T00:00:00+00:00'
        self.assertEqual(old_parse(dt),
                         new_parse(dt))

        dt = '2017-02-27T00:00:00+04:00'
        self.assertEqual(old_parse(dt),
                         new_parse(dt))



    def test_persist_lines_fails_if_doesnt_fit_schema(self):
        inputs = [
            {"type": "SCHEMA",
             "stream": "users",
             "key_properties": ["id"],
             "schema": {
                 "properties": {
                     "id": {"type": "integer"},
                     "t": {"type": "string", "format": "date-time"}}}},
            {"type": "RECORD",
             "stream": "users",
             "record": {"id": 1, "t": "foobar"}}]


        with DummyClient() as client:
            with self.assertRaises(ValueError):
                target_stitch.persist_lines(client, message_lines(inputs))


    def test_persist_last_state_when_stream_ends_with_record(self):

        inputs = [
            schema,
            record(0), state(0), record(1), state(1), record(2),
            # flush state 1
            state(2), record(3), state(3), record(4), state(4), record(5),
            # flush state 4
            record(6),
            record(7),
            record(8),
            # flush empty states
            state(8),
            record(9),
            state(9),
            record(10)]

        buf = io.StringIO()
        with mock.patch('sys.stdout', buf):
            with DummyClient(buffer_size=3) as client:

                final_state = target_stitch.persist_lines(client, message_lines(inputs))

                self.assertEqual(
                    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
                    [m['data']['i'] for m in client.messages])
                self.assertIsNone(final_state)
            self.assertEqual('1\n4\n9\n', sys.stdout.getvalue())

    def test_persist_last_state_when_stream_ends_with_state(self):

        inputs = [
            schema,
            record(0), state(0), record(1), state(1), record(2),
            # flush state 1
            state(2), record(3), state(3), record(4), state(4), record(5),
            # flush state 4
            record(6),
            record(7),
            record(8),
            # flush empty states
            state(8),
            record(9),
            state(9),
            record(10),
            state(10)]

        buf = io.StringIO()
        with mock.patch('sys.stdout', buf):
            with DummyClient(buffer_size=3) as client:

                final_state = target_stitch.persist_lines(client, message_lines(inputs))

                self.assertEqual(
                    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
                    [m['data']['i'] for m in client.messages])
                self.assertEqual(10, final_state)
            self.assertEqual('1\n4\n9\n', sys.stdout.getvalue())

    def test_persist_lines_updates_schema(self):
        inputs = [
            {"type": "SCHEMA",
             "stream": "users",
             "key_properties": ["id"],
             "schema": {
                 "properties": {
                     "id": {"type": "integer"},
                     "name": {"type": "string"}}}},
            {"type": "RECORD",
             "stream": "users",
             "record": {"id": 1, "name": "mike"}},
            {"type": "SCHEMA",
             "stream": "users",
             "key_properties": ["id"],
             "schema": {
                 "properties": {
                     "id": {"type": "string"},
                     "name": {"type": "string"}}}},
            {"type": "RECORD",
             "stream": "users",
             "record": {"id": "1", "name": "mike"}}]

        with DummyClient() as client:
            target_stitch.persist_lines(client, message_lines(inputs))
            self.assertEqual(len(client.messages), 2)
            self.assertEqual(client.messages[0]['key_names'], ['id'])

    def test_persist_lines_updates_schema_will_error(self):
        inputs = [
            {"type": "SCHEMA",
             "stream": "users",
             "key_properties": ["id"],
             "schema": {
                 "properties": {
                     "id": {"type": "integer"},
                     "name": {"type": "string"}}}},
            {"type": "RECORD",
             "stream": "users",
             "record": {"id": 1, "name": "mike"}},
            {"type": "SCHEMA",
             "stream": "users",
             "key_properties": ["id"],
             "schema": {
                 "properties": {
                     "id": {"type": "string"},
                     "name": {"type": "string"}}}},
            {"type": "RECORD",
             "stream": "users",
             "record": {"id": 1, "name": "mike"}}]

        with DummyClient() as client:
            with self.assertRaises(Exception):
                target_stitch.persist_lines(client, message_lines(inputs))

    def test_versioned_stream(self):
        lines = load_sample_lines('versioned_stream.json')
        with DummyClient() as client:
            target_stitch.persist_lines(client, lines)
            messages = [(m['action'], m['table_version']) for m in client.messages]
            self.assertEqual(messages,
                             [('upsert', 1),
                              ('upsert', 1),
                              ('upsert', 1),
                              ('switch_view', 1),
                              ('upsert', 2),
                              ('upsert', 2),
                              ('switch_view', 2)])

    def test_decimal_handling(self):
        inputs = [
            {"type": "SCHEMA",
             "stream": "testing",
             "key_properties": ["a_float", "a_dec"],
             "schema": {
                 "properties": {
                     "a_float": {"type": ["null", "number"]},
                     "a_dec": {"type": ["null", "number"],
                               "multipleOf": 0.0001}}}},
            {"type": "RECORD",
             "stream": "testing",
             "record": {"a_float": 4.72, "a_dec": 4.72}},
            {"type": "RECORD",
             "stream": "testing",
             "record": {"a_float": 4.72, "a_dec": None}}]

        with DummyClient() as client:
            target_stitch.persist_lines(client, message_lines(inputs))
            self.assertEqual(str(sorted(client.messages[0]['data'].items())),
                             "[('a_dec', Decimal('4.7200')), ('a_float', 4.72)]")
            self.assertEqual(str(sorted(client.messages[1]['data'].items())),
                             "[('a_dec', None), ('a_float', 4.72)]")
