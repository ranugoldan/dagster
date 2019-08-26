import glob
import os
import pickle
from abc import ABCMeta, abstractmethod
from collections import defaultdict

import gevent.lock
import pyrsistent
import six
from watchdog.events import PatternMatchingEventHandler
from watchdog.observers import Observer

from dagster import check
from dagster.core.events.log import EventRecord
from dagster.utils import mkdir_p

from .config import base_runs_directory
from .pipeline_run import PipelineRun, PipelineRunStatus


class EventLogSequence(pyrsistent.CheckedPVector):
    __type__ = EventRecord


class EventLogStorage(six.with_metaclass(ABCMeta)):  # pylint: disable=no-init
    @abstractmethod
    def get_logs_for_run(self, run_id, cursor=-1):
        '''Get all of the logs corresponding to a run.

        Args:
            run_id (str): The id of the run for which to fetch logs.
            cursor (Optional[int]): Zero-indexed logs will be returned starting from cursor + 1,
                i.e., if cursor is -1, all logs will be returned. (default: -1)
        '''

    @abstractmethod
    def store_event(self, run_id, event):
        '''Store an event corresponding to a pipeline run.

        Args:
            run_id (str): The id of the run that generated the event.
            event (EventRecord): The event to store.
        '''

    @property
    @abstractmethod
    def is_persistent(self):
        '''(bool) Whether the log storage persists after the process that
        created it dies.'''

    @property
    def event_handler(self):
        def _make_handler_class(event_log_storage):
            class _LogStorageEventHandler(object):
                def __init__(self, pipeline_run):
                    check.inst_param(pipeline_run, 'pipeline_run', PipelineRun)
                    self._run_id = pipeline_run.run_id
                    self._log_storage = event_log_storage

                def handle_new_event(self, event):
                    check.inst_param(event, 'new_event', EventRecord)

                    return self._log_storage.store_event(self._run_id, event)

            return _LogStorageEventHandler

        return _make_handler_class(self)

    @abstractmethod
    def wipe(self):
        '''Clear the log storage.'''


class InMemoryEventLogStorage(EventLogStorage):
    def __init__(self):
        self._logs = defaultdict(EventLogSequence)
        self._lock = defaultdict(gevent.lock.Semaphore)

    def get_logs_for_run(self, run_id, cursor=-1):
        cursor = int(cursor) + 1
        with self._lock[run_id]:
            return self._logs[run_id][cursor:]

    @property
    def is_persistent(self):
        return False

    def store_event(self, run_id, event):
        with self._lock[run_id]:
            self._logs[run_id] = self._logs[run_id].append(event)

    def wipe(self):
        self._logs = defaultdict(EventLogSequence)
        self._lock = defaultdict(gevent.lock.Semaphore)


class FilesystemEventLogStorage(EventLogStorage):
    def __init__(self, base_dir=None):
        self._base_dir = check.opt_str_param(base_dir, 'base_dir', base_runs_directory())
        mkdir_p(self._base_dir)
        self.file_cursors = defaultdict(lambda: (0, 0))
        # Swap these out to use lockfiles
        self.file_lock = defaultdict(gevent.lock.Semaphore)
        self._watchers = {}
        self._obs = Observer()
        self._obs.start()

    def filepath_for_run_id(self, run_id):
        return os.path.join(self._base_dir, '{run_id}.log'.format(run_id=run_id))

    def store_event(self, run_id, event):
        with self.file_lock[run_id]:
            # Going to do the less error-prone, simpler, but slower strategy:
            # open, append, close for every log message for now.
            # Open the file for binary content and create if it doesn't exist.
            with open(self.filepath_for_run_id(run_id), 'ab') as file_handle:
                file_handle.seek(0, os.SEEK_END)
                pickle.dump(event, file_handle)

    def wipe(self):
        for filename in glob.glob(os.path.join(self._base_dir, '*.log')):
            os.unlink(filename)

        self.file_lock = defaultdict(gevent.lock.Semaphore)
        self.file_cursors = defaultdict(lambda: (0, 0))

    @property
    def is_persistent(self):
        return True

    def logs_ready(self, run_id):
        return os.path.exists(self.filepath_for_run_id(run_id))

    def get_logs_for_run(self, run_id, cursor=0):
        events = []
        with self.file_lock[run_id]:
            with open(self.filepath_for_run_id(run_id), 'rb') as fd:
                # There might be a path to make this more performant, at the expense of interop,
                # by using a modified file format: https://stackoverflow.com/a/8936927/324449
                # Alternatively, we could use .jsonl and linecache instead of pickle
                if cursor == self.file_cursors[run_id][0]:
                    fd.seek(self.file_cursors[run_id][1])
                else:
                    i = 0
                    while i < cursor:
                        pickle.load(fd)
                        i += 1
                    self.file_cursors[run_id] = (cursor, fd.tell())
                try:
                    while True:
                        events.append(pickle.load(fd))
                except EOFError:
                    pass
        return events

    def watch(self, run_id, start_cursor, callback):
        watchdog = EventLogStorageWatchdog(self, run_id, callback, start_cursor)
        self._watchers[run_id] = self._obs.schedule(watchdog, self._base_dir, True)

    def end_watch(self, run_id, handler):
        self._obs.remove_handler_for_watch(handler, self._watchers[run_id])
        del self._watchers[run_id]


class EventLogStorageWatchdog(PatternMatchingEventHandler):
    def __init__(self, event_log_storage, run_id, callback, start_cursor, **kwargs):
        self._event_log_storage = event_log_storage
        self._run_id = run_id
        self._cb = callback
        self._log_path = event_log_storage.filepath_for_run_id(run_id)
        self._cursor = start_cursor
        super(EventLogStorageWatchdog, self).__init__(patterns=[self._log_path], **kwargs)

    def _process_log(self):
        events = self._event_log_storage.get_logs_for_run(self._run_id, self._cursor)
        self._cursor += len(events)
        for event in events:
            status = self._cb(event)

            if status == PipelineRunStatus.SUCCESS or status == PipelineRunStatus.FAILURE:
                self._event_log_storage.end_watch(self._run_id, self)

    def on_created(self, event):
        check.invariant(event.src_path == self._log_path)
        self._process_log()

    def on_modified(self, event):
        check.invariant(event.src_path == self._log_path)
        self._process_log()