# Miro - an RSS based video player application
# Copyright (C) 2005, 2006, 2007, 2008, 2009, 2010, 2011
# Participatory Culture Foundation
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301 USA
#
# In addition, as a special exception, the copyright holders give
# permission to link the code of portions of this program with the OpenSSL
# library.
#
# You must obey the GNU General Public License in all respects for all of
# the code used other than OpenSSL. If you modify file(s) with this
# exception, you may extend this exception to your version of the file(s),
# but you are not obligated to do so. If you do not wish to do so, delete
# this exception statement from your version. If you delete this exception
# statement from all source files in the program, then also delete it here.

"""``miro.eventloop`` -- Event loop handler.

This module handles the miro event loop which is responsible for
network requests and scheduling.

TODO: handle user setting clock back
"""

import errno
import heapq
import logging
import Queue
import select
import socket
import threading
import traceback

from miro import app
from miro import config
from miro import trapcall
from miro import signals
from miro import util
from miro.clock import clock
from miro.plat.utils import thread_body

cumulative = {}

class DelayedCall(object):
    def __init__(self, function, name, args, kwargs):
        self.function = function
        self.name = name
        self.args = args
        self.kwargs = kwargs
        self.canceled = False

    def _unlink(self):
        """Removes the references that this object has to the outside
        world, this eases the GC's work in finding cycles and fixes
        some memory leaks on windows.
        """
        self.function = self.args = self.kwargs = None

    def cancel(self):
        self.canceled = True
        self._unlink()

    def dispatch(self):
        success = True
        if not self.canceled:
            when = "While handling %s" % self.name
            start = clock()
            success = trapcall.trap_call(when, self.function, *self.args,
                    **self.kwargs)
            end = clock()
            if end-start > 0.5:
                logging.timing("%s too slow (%.3f secs)",
                               self.name, end-start)
            try:
                total = cumulative[self.name]
            except (KeyError, AttributeError):
                total = 0
            total += end - start
            cumulative[self.name] = total
            if total > 5.0:
                logging.timing("%s cumulative is too slow (%.3f secs)",
                               self.name, total)
                cumulative[self.name] = 0
        self._unlink()
        return success

class Scheduler(object):
    def __init__(self):
        self.heap = []

    def add_timeout(self, delay, function, name, args=None, kwargs=None):
        if args is None:
            args = ()
        if kwargs is None:
            kwargs = {}
        scheduled_time = clock() + delay
        dc = DelayedCall(function,  "timeout (%s)" % (name,), args, kwargs)
        heapq.heappush(self.heap, (scheduled_time, dc))
        return dc

    def next_timeout(self):
        if len(self.heap) == 0:
            return None
        else:
            return max(0, self.heap[0][0] - clock())

    def has_pending_timeout(self):
        return len(self.heap) > 0 and self.heap[0][0] < clock()

    def process_next_timeout(self):
        time, dc = heapq.heappop(self.heap)
        return dc.dispatch()

class CallQueue(object):
    def __init__(self):
        self.queue = Queue.Queue()
        self.quit_flag = False
        self.queue_size_warning_count = 0

    def add_idle(self, function, name, args=None, kwargs=None):
        if args is None:
            args = ()
        if kwargs is None:
            kwargs = {}
        dc = DelayedCall(function, "idle (%s)" % (name,), args, kwargs)
        self.queue.put(dc)

        # Check if our queue size is too big and log a warning if so.  Only do
        # this a few times.  That should be enough to track down errors, but
        # not too much to kill the log file.
        #
        # NOTE: the code below doesn't take into account that this method
        # runs on multiple threads.  However, the worst that can happen is
        # we log an extra warning or two, so this doesn't seem bad.
        if self.queue_size_warning_count < 5 and self.queue.qsize() > 1000:
            if self.queue_size_warning_count < 5:
                logging.stacktrace("Queued called size too large")
                self.queue_size_warning_count += 1

        return dc

    def process_next_idle(self):
        dc = self.queue.get()
        return dc.dispatch()

    def has_pending_idle(self):
        return not self.queue.empty()

    def process_idles(self):
        # Note: used for testing purposes
        while self.has_pending_idle() and not self.quit_flag:
            self.process_next_idle()


class ThreadPool(object):
    """The thread pool is used to handle calls like gethostbyname()
    that block and there's no asynchronous workaround.  What we do
    instead is call them in a separate thread and return the result in
    a callback that executes in the event loop.
    """
    THREADS = 4

    def __init__(self, event_loop):
        self.event_loop = event_loop
        self.queue = Queue.Queue()
        self.threads = []

    def init_threads(self):
        while len(self.threads) < ThreadPool.THREADS:
            t = threading.Thread(name='ThreadPool - %d' % len(self.threads),
                                 target=thread_body,
                                 args=[self.thread_loop])
            t.setDaemon(True)
            t.start()
            self.threads.append(t)

    def thread_loop(self):
        while True:
            next_item = self.queue.get()
            if next_item == "QUIT":
                break
            else:
                callback, errback, func, name, args, kwargs, = next_item
            try:
                result = func(*args, **kwargs)
            except KeyboardInterrupt:
                raise
            except Exception, exc:
                logging.debug(">>> thread_loop: %s %s %s %s\n%s",
                              func, name, args, kwargs,
                              "".join(traceback.format_exc()))
                func = errback
                name = 'Thread Pool Errback (%s)' % name
                args = (exc,)
            else:
                func = callback
                name = 'Thread Pool Callback (%s)' % name
                args = (result,)
            if not self.event_loop.quit_flag:
                self.event_loop.idle_queue.add_idle(func, name, args=args)
                self.event_loop.wakeup()

    def queue_call(self, callback, errback, function, name, *args, **kwargs):
        self.queue.put((callback, errback, function, name, args, kwargs))

    def close_threads(self):
        for x in xrange(len(self.threads)):
            self.queue.put("QUIT")
        # Why is there a timeout on the join() here, what's wrong?  On
        # shutdown, the system waits for the eventloop to finish using 
        # eventloop.join() but eventloop calls close_threads() which wait
        # for the threadpool threads to shutdown.  But these could be blocked
        # in a blocking operation which is exactly the point of having them
        # so eventloop.join() in turn blocks.  So if it doesn't clean up
        # in time let the daemon flag in the Thread() do its job.  See #16584.
        for t in self.threads:
            try:
                t.join(0.5)
            except StandardError:
                pass
        self.threads = []

class SimpleEventLoop(signals.SignalEmitter):
    def __init__(self):
        signals.SignalEmitter.__init__(self, 'thread-will-start',
                                       'thread-started',
                                       'thread-did-start',
                                       'begin-loop',
                                       'end-loop')
        self.quit_flag = False
        self.wake_sender, self.wake_receiver = util.make_dummy_socket_pair()
        self.loop_ready = threading.Event()

    def loop(self):
        self.loop_ready.set()
        self.emit('thread-will-start')
        self.emit('thread-started', threading.currentThread())
        self.emit('thread-did-start')

        while not self.quit_flag:
            self.emit('begin-loop')
            timeout = self.calc_timeout()
            readfds, writefds, excfds = self.calc_fds()
            readfds.append(self.wake_receiver.fileno())
            try:
                read_fds_ready, write_fds_ready, exc_fds_ready = \
                        select.select(readfds, writefds, excfds, timeout)
            except select.error, (err, detail):
                if err == errno.EINTR:
                    logging.warning ("eventloop: %s", detail)
                else:
                    self.emit('end-loop')
                    raise
            if self.quit_flag:
                self.emit('end-loop')
                break
            if self.wake_receiver.fileno() in read_fds_ready:
                self._slurp_waker_data()
            self.process_events(read_fds_ready, write_fds_ready, exc_fds_ready)
            self.emit('end-loop')

    def wakeup(self):
        try:
            self.wake_sender.send("b")
        except socket.error, e:
            logging.warn("Error waking up eventloop (%s)", e)

    def _slurp_waker_data(self):
        self.wake_receiver.recv(1024)

class EventLoop(SimpleEventLoop):
    def __init__(self):
        SimpleEventLoop.__init__(self)
        self.create_signal('event-finished')
        self.scheduler = Scheduler()
        self.idle_queue = CallQueue()
        self.urgent_queue = CallQueue()
        self.threadpool = ThreadPool(self)
        self.read_callbacks = {}
        self.write_callbacks = {}
        self.clear_removed_callbacks()
        self.idles_for_next_loop = []

    def clear_removed_callbacks(self):
        self.removed_read_callbacks = set()
        self.removed_write_callbacks = set()

    def add_read_callback(self, sock, callback):
        self.read_callbacks[sock.fileno()] = callback

    def remove_read_callback(self, sock):
        del self.read_callbacks[sock.fileno()]
        self.removed_read_callbacks.add(sock.fileno())

    def add_write_callback(self, sock, callback):
        self.write_callbacks[sock.fileno()] = callback

    def remove_write_callback(self, sock):
        del self.write_callbacks[sock.fileno()]
        self.removed_write_callbacks.add(sock.fileno())

    def call_in_thread(self, callback, errback, function, name,
                       *args, **kwargs):
        self.threadpool.queue_call(callback, errback, function, name,
                                  *args, **kwargs)

    def run_idle_next_loop(self, function, name, args=None, kwargs=None):
        """Add an idle callback to be called on the next event loop."""
        self.idles_for_next_loop.append((function, name, args, kwargs))

    def process_events(self, read_fds_ready, write_fds_ready, exc_fds_ready):
        self._process_urgent_events()
        if self.quit_flag:
            return
        for event in self.generate_events(read_fds_ready, write_fds_ready):
            success = event()
            self.emit('event-finished', success)
            if self.quit_flag:
                break
            self._process_urgent_events()
            if self.quit_flag:
                break

    def calc_fds(self):
        return (self.read_callbacks.keys(), self.write_callbacks.keys(), [])

    def calc_timeout(self):
        return self.scheduler.next_timeout()

    def do_begin_loop(self):
        self.clear_removed_callbacks()
        self._add_idles_for_next_loop()

    def _add_idles_for_next_loop(self):
        if not self.idles_for_next_loop:
            return
        for func, name, args, kwargs in self.idles_for_next_loop:
            self.idle_queue.add_idle(func, name, args, kwargs)
        self.idles_for_next_loop = []
        # call wakeup() to make sure we process the idles we just
        # added
        self.wakeup()

    def _process_urgent_events(self):
        queue = self.urgent_queue
        while queue.has_pending_idle() and not queue.quit_flag:
            success = queue.process_next_idle()
            self.emit('event-finished', success)

    def generate_events(self, read_fds_ready, write_fds_ready):
        """Generator that creates the list of events that should be
        dealt with on this iteration of the event loop.  This includes
        all socket read/write callbacks, timeouts and idle calls.

        "events" are implemented as functions that should be called
        with no arguments.
        """
        for callback in self.generate_callbacks(write_fds_ready,
                                               self.write_callbacks,
                                               self.removed_write_callbacks):
            yield callback
        for callback in self.generate_callbacks(read_fds_ready,
                                               self.read_callbacks,
                                               self.removed_read_callbacks):
            yield callback
        while self.scheduler.has_pending_timeout():
            yield self.scheduler.process_next_timeout
        while self.idle_queue.has_pending_idle():
            yield self.idle_queue.process_next_idle

    def generate_callbacks(self, ready_list, map_, removed):
        for fd in ready_list:
            try:
                function = map_[fd]
            except KeyError:
                # this can happen the write callback removes the read
                # callback or vise versa
                pass
            else:
                if fd in removed:
                    continue
                when = "While talking to the network"
                def callback_event():
                    success = trapcall.trap_call(when, function)
                    if not success:
                        del map_[fd]
                    return success
                yield callback_event

    def quit(self):
        self.quit_flag = True
        self.idle_queue.quit_flag = True
        self.urgent_queue.quit_flag = True

_eventloop = EventLoop()

def add_read_callback(sock, callback):
    """Add a read callback.  When socket is ready for reading,
    callback will be called.  If there is already a read callback
    installed, it will be replaced.
    """
    _eventloop.add_read_callback(sock, callback)

def remove_read_callback(sock):
    """Remove a read callback.  If there is not a read callback
    installed for socket, a KeyError will be thrown.
    """
    _eventloop.remove_read_callback(sock)

def add_write_callback(sock, callback):
    """Add a write callback.  When socket is ready for writing,
    callback will be called.  If there is already a write callback
    installed, it will be replaced.
    """
    _eventloop.add_write_callback(sock, callback)

def remove_write_callback(sock):
    """Remove a write callback.  If there is not a write callback
    installed for socket, a KeyError will be thrown.
    """
    _eventloop.remove_write_callback(sock)

def stop_handling_socket(sock):
    """Convience function to that removes both the read and write
    callback for a socket if they exist.
    """
    try:
        remove_read_callback(sock)
    except KeyError:
        pass
    try:
        remove_write_callback(sock)
    except KeyError:
        pass

def add_timeout(delay, function, name, args=None, kwargs=None):
    """Schedule a function to be called at some point in the future.
    Returns a ``DelayedCall`` object that can be used to cancel the
    call.
    """
    dc = _eventloop.scheduler.add_timeout(delay, function, name, args, kwargs)
    _eventloop.wakeup()
    return dc

def add_idle(function, name, args=None, kwargs=None):
    """Schedule a function to be called when we get some spare time.
    Returns a ``DelayedCall`` object that can be used to cancel the
    call.
    """
    dc = _eventloop.idle_queue.add_idle(function, name, args, kwargs)
    _eventloop.wakeup()
    return dc

def add_urgent_call(function, name, args=None, kwargs=None):
    """Schedule a function to be called as soon as possible.  This
    method should be used for things like GUI actions, where the user
    is waiting on us.
    """
    dc = _eventloop.urgent_queue.add_idle(function, name, args, kwargs)
    _eventloop.wakeup()
    return dc

def call_in_thread(callback, errback, function, name, *args, **kwargs):
    """Schedule a function to be called in a separate thread.

    .. Warning::

       Do not put code that accesses the database or the UI here!
    """
    _eventloop.call_in_thread(
        callback, errback, function, name, *args, **kwargs)

lt = None

profile_file = None

def startup():
    thread_pool_init()

    def profile_startup():
        import profile
        profile.runctx('_eventloop.loop()', globals(), locals(),
                       profile_file + ".event_loop")

    global lt
    if profile_file:
        lt = threading.Thread(target=profile_startup, name="Event Loop")
    else:
        lt = threading.Thread(target=_eventloop.loop, name="Event Loop")
    lt.setDaemon(False)
    lt.start()
    _eventloop.loop_ready.wait()

def setup_config_watcher():
    app.backend_config_watcher = config.ConfigWatcher(
            lambda func, *args: add_idle(func, "config callback", args=args))

def join():
    if lt is not None:
        lt.join()

def shutdown():
    """Shuts down the thread pool and eventloop.
    """
    thread_pool_quit()
    _eventloop.quit()
    _eventloop.wakeup()

def connect(signal, callback):
    _eventloop.connect(signal, callback)

def connect_after(signal, callback):
    _eventloop.connect_after(signal, callback)

def disconnect(signal, callback):
    _eventloop.disconnect(signal, callback)

def thread_pool_quit():
    _eventloop.threadpool.close_threads()

def thread_pool_init():
    _eventloop.threadpool.init_threads()

def as_idle(func):
    """Decorator to make a methods run as an idle function

    Suppose you have 2 methods, foo and bar::

        @as_idle
        def foo():
            # database operations

        def bar():
            # same database operations as foo

    Then calling ``foo()`` is exactly the same as calling
    ``add_idle(bar)``.
    """
    def queuer(*args, **kwargs):
        return add_idle(func, "%s() (using as_idle)" % func.__name__,
                       args=args, kwargs=kwargs)
    return queuer

def as_urgent(func):
    """Like ``as_idle``, but uses ``add_urgent_call()`` instead of
    ``add_idle()``.
    """
    def queuer(*args, **kwargs):
        return add_urgent_call(func, "%s() (using as_urgent)" % func.__name__,
                               args=args, kwargs=kwargs)
    return queuer

def idle_iterate(func, name, args=None, kwargs=None):
    """Iterate over a generator function using add_idle for each
    iteration.

    This allows long running functions to be split up into distinct
    steps, after each step other idle functions will have a chance to
    run.

    For example::

        def foo(x, y, z):
            # do some computation
            yield
            # more computation
            yield
            # more computation
            yield

        eventloop.idle_iterate(foo, 'Foo', args=(1, 2, 3))
    """
    if args is None:
        args = ()
    if kwargs is None:
        kwargs = {}
    iterator = func(*args, **kwargs)
    add_idle(_idle_iterate_step, name, args=(iterator, name))

def _idle_iterate_step(iterator, name):
    try:
        retval = iterator.next()
    except StopIteration:
        return
    else:
        if retval is not None:
            logging.warn("idle_iterate yield value ignored: %s (%s)",
                         retval, name)
        _eventloop.run_idle_next_loop(_idle_iterate_step, name,
                args=(iterator, name))

def idle_iterator(func):
    """Decorator to wrap a generator function in a ``idle_iterate()``
    call.
    """
    def queuer(*args, **kwargs):
        return idle_iterate(func, "%s() (using idle_iterator)" % func.__name__, 
                            args=args, kwargs=kwargs)
    return queuer

class DelayedFunctionCaller(object):
    """Call a function sometime in the future using add_idle()/add_timeout()

    This class also tracks whether a function has been scheduled and avoids
    scheduling it twice.
    """
    def __init__(self, func):
        """Create a DelayedFunctionCaller

        :param func: function to call.
        """
        self.dc = None
        self.func = func
        self.name = 'delayed call to %s' % func

    def call_when_idle(self, *args, **kwargs):
        """Call our function when we're idle."""
        if self.dc is None:
            self.dc = add_idle(self.call_now, self.name, args=args,
                               kwargs=kwargs)

    def call_after_timeout(self, timeout, *args, **kwargs):
        """Call our function after a timeout."""
        if self.dc is None:
            self.dc = add_timeout(timeout, self.call_now, self.name,
                                  args=args, kwargs=kwargs)

    def call_now(self, *args, **kwargs):
        """Call our function immediately."""
        self.cancel_call()
        self.func(*args, **kwargs)

    def cancel_call(self):
        """Cancel a timeout/idle callback, if scheduled."""
        if self.dc is not None:
            self.dc.cancel()
            self.dc = None
