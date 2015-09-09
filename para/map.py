import logging
import time
import traceback
from multiprocessing import Process, Queue, cpu_count
from queue import Empty
from threading import Thread

logger = logging.getLogger(__name__)

OUTPUT_QUEUE_TIMEOUT = 0.1
"""
This is how long an output queue will block while waiting for output to process
"""


def map(process, items, mappers=None):
    """
    Implements a distributed stategy for processing XML files.  This
    function constructs a set of py:mod:`multiprocessing` threads (spread over
    multiple cores) and uses an internal queue to aggregate outputs.  To use
    this function, implement a `process()` function that takes one argument --
    a serializable job.  Anything that this function ``yield``s will be
    `yielded` in turn from the :func:`para.map` function.

    :Parameters:
        process : `func`
            A function that takes an item as a parameter and returns a
            generator of output values.
        items : `iterable` ( `picklable` )
            :mod:`pickle`-able items to process.  Note that this must fit in
            memory.
        mappers : int
            the number of parallel mappers to spool up

    :Example:

        >>> import para
        >>> files = ["examples/dump.xml", "examples/dump2.xml"]
        >>>
        >>> def filter_long_lines(path):
        ...     with open(path) as f:
        ...         for line in f:
        ...             if len(line) > 100:
        ...                 yield (path, line)
        ...
        >>> for path, line in para.map(filter_long_lines, files):
        ...     print(path, line)
        ...
    """

    # Load paths into the queue
    item_queue = Queue()
    for item in items:
        item_queue.put(item)

    # How many mappers are we going to have?
    mappers = min(max(1, mappers or cpu_count()), len(items))

    # Prepare the output queue
    output = Queue()

    # Prepare the logs queue
    qlogger = QueueLogger()
    qlogger.start()

    # Prepare the mappers and start them
    mapper_processes = [Mapper(process, item_queue, output, qlogger,
                               name=str(i))
                        for i in range(mappers)]
    for mapper_process in mapper_processes:
        mapper_process.start()

    # Read from the output queue while there's still a mapper alive or
    # something in the queue to read.
    while sum(m.is_alive() for m in mapper_processes) > 0 or not output.empty():
        try:
            # if we timeout, the loop will check to see if we are done
            error, value = output.get(timeout=OUTPUT_QUEUE_TIMEOUT)

            if error is None:
                yield value
            else:
                raise error

        except Empty:
            # This can happen when mappers aren't adding values to the
            # queue fast enough *or* if we're done processing.  Let the while
            # condition determine if we are done or not.
            continue


class Mapper(Process):
    """
    Implements a mapper process worker.  Instances of this class will
    continually try to read from an `item_queue` and execute it's `process()`
    function until there is nothing left to read from the `item_queue`.
    """
    def __init__(self, process, item_queue, output, logger, name=None):
        super().__init__(name="Mapper {0}".format(name), daemon=True)
        self.process = process
        self.item_queue = item_queue
        self.output = output
        self.logger = logger
        self.stats = []

    def run(self):
        logger.info("{0}: Starting up.".format(self.name))
        try:
            while True:
                # Get an item to process
                item = self.item_queue.get(timeout=0.05)
                self.logger.info("{0}: Processing {1}"
                                 .format(self.name, str(item)[:50]))

                try:
                    start_time = time.time()
                    count = 0
                    # For each value that is yielded, add it to the output
                    # queue
                    for value in self.process(item):
                        self.output.put((None, value))
                        count += 1
                    self.stats.append((item, count, time.time() - start_time))
                except Exception as e:
                    self.logger.error(
                        "{0}: An error occured while processing {1}"
                        .format(self.name, str(item)[:50])
                    )
                    formatted = traceback.format_exc(chain=False)
                    self.logger.error("{0}: {1}".format(self.name, formatted))
                    self.output.put((e, None))
                    return  # Exits without polluting stderr

        except Empty:
            self.logger.info("{0}: No more items to process".format(self.name))
            self.logger.info("\n" + "\n".join(self.format_stats()))

    def format_stats(self):
        for path, outputs, duration in self.stats:
            yield "{0}: - Extracted {1} values from {2} in {3} seconds" \
                  .format(self.name, outputs, path, duration)


class QueueLogger(Thread):

    def __init__(self, logger=None):
        super().__init__(daemon=True)
        self.queue = Queue()

    def debug(self, message):
        self.queue.put((logging.DEBUG, message))

    def info(self, message):
        self.queue.put((logging.INFO, message))

    def warning(self, message):
        self.queue.put((logging.WARNING, message))

    def error(self, message):
        self.queue.put((logging.ERROR, message))

    def run(self):
        while True:
            try:
                level, message = self.queue.get(timeout=OUTPUT_QUEUE_TIMEOUT)
                logger.log(level, message)
            except Empty:
                continue