# This module used to create multi worker processes shipped with a manager
# And it's only a framework which will manager the process create, quit, etc.
# It'll be used in daemon.

from __future__ import print_function, absolute_import

import os
import sys
import time
import signal
import logging
import psutil

default_log = logging.getLogger(__name__)

is_exit = False

class TimeoutException(Exception):
    pass

class Timeout():
    """Timeout class using ALARM signal."""

    def __init__(self, sec):
        self.sec = sec

    def __enter__(self):
        signal.signal(signal.SIGALRM, self.raise_timeout)
        signal.alarm(self.sec)

    def __exit__(self, *args):
        signal.alarm(0)    # disable alarm

    def raise_timeout(self, *args):
        raise TimeoutException()

def get_memory(pid):
    # return the memory usage in MB
    _pid, status = os.waitpid(pid, os.WNOHANG)
    if _pid == 0:
        process = psutil.Process(pid)
        mem = process.memory_info().rss / float(1024*1024)
        return mem
    return 0

class Worker(object):
    _id = 1

    def __init__(self, log=None,
                 max_requests=None,
                 soft_memory_limit=200, #memory limit MB
                 hard_memory_limit=300, #memory limit MB
                 timeout=None,
                 check_point=None,
                 name=None,
                 *args, **kwargs):
        self.log = log or default_log
        self.max_requests = max_requests
        self.soft_memory_limit = soft_memory_limit
        self.hard_memory_limit = hard_memory_limit
        self.timeout = timeout
        self.args = args
        self.kwargs = kwargs
        self.is_exit = None
        self.count = 0
        self.check_point = check_point
        self.name = "%s-%d" % ((name or 'Process'), self._id)
        self.__class__._id += 1

    def start(self):
        self.pid = os.getpid()

        signal.signal(signal.SIGINT, self.signal_handler)
        signal.signal(signal.SIGTERM, self.signal_handler)

        self.init()
        self._run()
        self.after_run()

    def init(self):
        self.log.info('%s %d creating' % (self.name, self.pid))

    def run(self):
        self.log.info('%s %d running' % (self.name, self.pid))
        time.sleep(1)
        return True

    def on_success(self, result):
        pass

    def on_failure(self):
        pass

    def _run(self):
        while (not self.max_requests or
                   (self.max_requests and self.count < self.max_requests)) and \
                not self.is_exit:
            try:
                if self.timeout:
                    with Timeout(self.timeout):
                        ret = self.run()
                else:
                    ret = self.run()
                if ret:
                    self.count += 1
                if self.check_point:
                    time.sleep(self.check_point)
            except TimeoutException as e:
                self.is_exit = 'timeout'
                return
            except Exception as e:
                self.log.exception(e)

    def after_run(self):
        if self.is_exit == 'signal':
            self.log.info('%s %d cancelled by signal.' % (self.name, self.pid))
        elif self.is_exit == 'timeout':
            self.log.info("%s %d cancelled by reaching timeout %ds" %
                          (self.name, self.pid, self.timeout))
        else:
            self.log.info('%s %d cancelled by reaching max requests count [%d]' % (
                self.name, self.pid, self.max_requests))

    def signal_handler(self, signum, frame):
        self.is_exit = 'signal'
        self.log.info ("%s %d received a signal %d" % (self.name, self.pid, signum))

    def reached_soft_memory_limit(self, mem):
        if mem >= self.soft_memory_limit:
            return True
        else:
            return False

    def reached_hard_memory_limit(self, mem):
        if mem >= self.hard_memory_limit:
            return True
        else:
            return False

class Manager(object):
    def __init__(self, workers, log=None, check_point=10,
                 title='Workers Daemon', wait_time=3):
        """
        :param workers: a list of workers
        :param log: log object
        :param check_point: time interval to check sub process status
        :return:
        """
        if not workers:
            log.info('No workers need to run.')
            sys.exit(0)

        self.log = log or logging.getLogger(__name__)
        self.workers = workers
        self.is_exit = False
        self.check_point = check_point
        self.title = title
        self.wait_time = wait_time

    def init(self):
        self.log.info('=============================')
        self.log.info('%s Starting' % self.title)
        self.log.info('=============================')
        self.log.info('Daemon process %d' % self.pid)
        self.log.info('Check point %ds' % self.check_point)

    def start(self):
        self.pid = os.getpid()

        signal.signal(signal.SIGINT, self.signal_handler)
        signal.signal(signal.SIGTERM, self.signal_handler)

        self.init()
        self.run()
        self.after_run()

    def run(self):
        pids = {}

        try:
            while not self.is_exit:
                for i, worker in enumerate(self.workers):
                    pid = pids.get(i)
                    create = False
                    if not pid:
                        create = True
                    else:
                        if not psutil.pid_exists(pid):
                            self.log.info('%s %d is not existed any more.' % (worker.name, pid))
                            create = True

                    if create:
                        pid = os.fork()
                        #main
                        if pid:
                            pids[i] = pid
                        #child
                        else:
                            try:
                                worker.start()
                            except Exception as e:
                                self.log.exception(e)
                            finally:
                                sys.exit(0)
                    else:
                        try:
                            mem = get_memory(pid)
                            if worker.reached_hard_memory_limit(mem):
                                self.log.info('%s %d memory is %dM reaches hard memory limit %dM will be killed.' % (
                                    worker.name, pid, mem, worker.hard_memory_limit))
                                self.kill_child(pid, signal.SIGKILL)
                            elif worker.reached_soft_memory_limit(mem):
                                self.kill_child(pid, signal.SIGTERM)
                                self.log.info('%s %d memory is %dM reaches soft memory limit %dM will be cannelled.' % (
                                    worker.name, pid, mem, worker.soft_memory_limit))
                        except Exception as e:
                            self.log.info(e)

                time.sleep(self.check_point)

            for i, pid in pids.items():
                self.kill_child(pid)
            time.sleep(self.wait_time)
            self.log.info('Main process %s quit.' % self.pid)

        except KeyboardInterrupt:
            self.log.info ('Main process %d received Ctrl+C, quit' % os.getpid())

    def after_run(self):
        pass

    def kill_child(self, pid, sig=signal.SIGTERM):
        if psutil.pid_exists(pid):
            os.kill(pid, sig)

    def signal_handler(self, signum, frame):
        self.is_exit = True
        self.log.info ("Process %d received a signal %d" % (self.pid, signum))


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)

    class NewWorker(Worker):
        def run(self):
            s = []
            for i in range(50000):
                s.append(str(i))
            print ('result=', len(s))
            time.sleep(1)
            return True

    workers = [Worker(max_requests=2),
               NewWorker(max_requests=2, timeout=5, name='NewWorker',
                         soft_memory_limit=5, hard_memory_limit=10)]
    manager = Manager(workers, check_point=1)
    manager.start()