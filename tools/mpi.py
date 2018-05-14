#!/usr/bin/env python

import sys
import time
import threading
import traceback
import numpy
from mpi4py import MPI
from . import mpi_pool
from .mpi_pool import MPIPool
from pyscf import lib

_registry = {}

if 'pool' not in _registry:
    import atexit
    pool = MPIPool(debug=False)
    _registry['pool'] = pool
    atexit.register(pool.close)

comm = pool.comm
rank = pool.rank
INT_MAX = 2147483647

def static_partition(tasks):
    size = len(tasks)
    segsize = (size+pool.size-1) // pool.size
    start = pool.rank * segsize
    stop = min(size, start+segsize)
    return tasks[start:stop]

def work_balanced_partition(tasks, costs=None):
    if costs is None:
        costs = numpy.ones(len(tasks))
    if rank == 0:
        cum = numpy.append(0, numpy.cumsum(costs))
        segsize = float(cum[-1]) / (pool.size-.5)
        displs = lib.misc._blocksize_partition(cum, segsize)
        if len(displs) != pool.size+1:
            displs = lib.misc._balanced_partition(cum, pool.size)
        loads = list(zip(displs[:-1], displs[1:]))
        comm.bcast(loads)
    else:
        loads = comm.bcast(None)
    if rank < len(loads):
        start, stop = loads[rank]
        return tasks[start:stop]
    else:
        return tasks[:0]

INQUIRY = 50050
TASK = 50051
def work_share_partition(tasks, interval=.02, loadmin=1):
    loadmin = max(loadmin, len(tasks)//50//pool.size)
    rest_tasks = [x for x in tasks[loadmin*pool.size:]]
    tasks = tasks[loadmin*rank:loadmin*rank+loadmin]
    def distribute_task():
        while True:
            load = len(tasks)
            if rank == 0:
                for i in range(pool.size):
                    if i != 0:
                        load = comm.recv(source=i, tag=INQUIRY)
                    if rest_tasks:
                        if load <= loadmin:
                            task = rest_tasks.pop(0)
                            comm.send(task, i, tag=TASK)
                    else:
                        comm.send('OUT_OF_TASK', i, tag=TASK)
            else:
                comm.send(load, 0, tag=INQUIRY)
            if comm.Iprobe(source=0, tag=TASK):
                tasks.append(comm.recv(source=0, tag=TASK))
                if isinstance(tasks[-1], str) and tasks[-1] == 'OUT_OF_TASK':
                    return
            time.sleep(interval)

    tasks_handler = threading.Thread(target=distribute_task)
    tasks_handler.start()

    while True:
        if tasks:
            task = tasks.pop(0)
            if isinstance(task, str) and task == 'OUT_OF_TASK':
                tasks_handler.join()
                return
            yield task

def work_stealing_partition(tasks, interval=.02):
    tasks = static_partition(tasks)
    out_of_task = [False]
    def task_daemon():
        while True:
            time.sleep(interval)
            while comm.Iprobe(source=MPI.ANY_SOURCE, tag=INQUIRY):
                src, req = comm.recv(source=MPI.ANY_SOURCE, tag=INQUIRY)
                if isinstance(req, str) and req == 'STOP_DAEMON':
                    return
                elif tasks:
                    comm.send(tasks.pop(), src, tag=TASK)
                elif src == 0 and isinstance(req, str) and req == 'ALL_DONE':
                    comm.send(out_of_task[0], src, tag=TASK)
                elif out_of_task[0]:
                    comm.send('OUT_OF_TASK', src, tag=TASK)
                else:
                    comm.send('BYPASS', src, tag=TASK)
    def prepare_to_stop():
        out_of_task[0] = True
        if rank == 0:
            while True:
                done = []
                for i in range(1, pool.size):
                    comm.send((0,'ALL_DONE'), i, tag=INQUIRY)
                    done.append(comm.recv(source=i, tag=TASK))
                if all(done):
                    break
                time.sleep(interval)
            for i in range(pool.size):
                comm.send((0,'STOP_DAEMON'), i, tag=INQUIRY)
        tasks_handler.join()

    if pool.size > 1:
        tasks_handler = threading.Thread(target=task_daemon)
        tasks_handler.start()

    while tasks:
        task = tasks.pop(0)
        yield task

    if pool.size > 1:
        def next_proc(proc):
            proc = (proc+1) % pool.size
            if proc == rank:
                proc = (proc+1) % pool.size
            return proc
        proc_last = (rank + 1) % pool.size
        proc = next_proc(proc_last)

        while True:
            comm.send((rank,None), proc, tag=INQUIRY)
            task = comm.recv(source=proc, tag=TASK)
            if isinstance(task, str) and task == 'OUT_OF_TASK':
                prepare_to_stop()
                return
            elif isinstance(task, str) and task == 'BYPASS':
                if proc == proc_last:
                    prepare_to_stop()
                    return
                else:
                    proc = next_proc(proc)
            else:
                if proc != proc_last:
                    proc_last, proc = proc, next_proc(proc)
                yield task

def _create_dtype(dat):
    mpi_dtype = MPI._typedict[dat.dtype.char]
    # the smallest power of 2 greater than dat.size/INT_MAX
    deriv_dtype_len = 1 << max(0, dat.size.bit_length()-31)
    deriv_dtype = mpi_dtype.Create_contiguous(deriv_dtype_len).Commit()
    count, rest = dat.size.__divmod__(deriv_dtype_len)
    return deriv_dtype, count, rest

def bcast_test(buf, root=0):  # To test, maybe with better performance
    buf = numpy.asarray(buf, order='C')
    shape, dtype = comm.bcast((buf.shape, buf.dtype.char))
    if rank != root:
        buf = numpy.empty(shape, dtype=dtype)

    if buf.size <= INT_MAX:
        comm.Bcast(buf, root)
    else:
        deriv_dtype, count, rest = _create_dtype(buf)
        comm.Bcast([buf, count, deriv_dtype], root)
        comm.Bcast(buf[-rest*deriv_dtype.size:], root)
    return buf

def bcast(buf, root=0):
    buf = numpy.asarray(buf, order='C')
    shape, dtype = comm.bcast((buf.shape, buf.dtype.char))
    if rank != root:
        buf = numpy.empty(shape, dtype=dtype)

    buf_seg = numpy.ndarray(buf.size, dtype=buf.dtype, buffer=buf)
    for p0, p1 in lib.prange(0, buf.size, INT_MAX-7):
        comm.Bcast(buf_seg[p0:p1], root)
    return buf

def reduce(sendbuf, op=MPI.SUM, root=0):
    sendbuf = numpy.asarray(sendbuf, order='C')
    shape, mpi_dtype = comm.bcast((sendbuf.shape, sendbuf.dtype.char))
    _assert(sendbuf.shape == shape and sendbuf.dtype.char == mpi_dtype)

    recvbuf = numpy.zeros_like(sendbuf)
    send_seg = numpy.ndarray(sendbuf.size, dtype=sendbuf.dtype, buffer=sendbuf)
    recv_seg = numpy.ndarray(recvbuf.size, dtype=recvbuf.dtype, buffer=recvbuf)
    for p0, p1 in lib.prange(0, sendbuf.size, INT_MAX-7):
        comm.Reduce(send_seg[p0:p1], recv_seg[p0:p1], op, root)

    if rank == root:
        return recvbuf
    else:
        return sendbuf

def allreduce(sendbuf, op=MPI.SUM):
    sendbuf = numpy.asarray(sendbuf, order='C')
    shape, mpi_dtype = comm.bcast((sendbuf.shape, sendbuf.dtype.char))
    _assert(sendbuf.shape == shape and sendbuf.dtype.char == mpi_dtype)

    recvbuf = numpy.zeros_like(sendbuf)
    send_seg = numpy.ndarray(sendbuf.size, dtype=sendbuf.dtype, buffer=sendbuf)
    recv_seg = numpy.ndarray(recvbuf.size, dtype=recvbuf.dtype, buffer=recvbuf)
    for p0, p1 in lib.prange(0, sendbuf.size, INT_MAX-7):
        comm.Allreduce(send_seg[p0:p1], recv_seg[p0:p1], op)
    return recvbuf

def gather(sendbuf, root=0):
#    if pool.debug:
#        if rank == 0:
#            res = [sendbuf]
#            for k in range(1, pool.size):
#                dat = comm.recv(source=k)
#                res.append(dat)
#            return numpy.vstack([x for x in res if len(x) > 0])
#        else:
#            comm.send(sendbuf, dest=0)
#            return sendbuf

    sendbuf = numpy.asarray(sendbuf, order='C')
    size_dtype = comm.allgather((sendbuf.size, sendbuf.dtype.char))
    counts = numpy.array([x[0] for x in size_dtype])

    mpi_dtype = numpy.result_type(*[x[1] for x in size_dtype]).char
    _assert(sendbuf.dtype == mpi_dtype or sendbuf.size == 0)

    if rank == root:
        #_assert(numpy.all(counts <= INT_MAX))
        _assert(all(x[1] == mpi_dtype for x in size_dtype if x[0] > 0))
        displs = numpy.append(0, numpy.cumsum(counts[:-1]))
        recvbuf = numpy.empty(sum(counts), dtype=mpi_dtype)
        #comm.Gatherv([sendbuf.ravel(), mpi_dtype],
        #             [recvbuf.ravel(), counts, displs, mpi_dtype], root)
        send_seg = numpy.ndarray(sendbuf.size, dtype=sendbuf.dtype, buffer=sendbuf)
        recv_seg = numpy.ndarray(recvbuf.size, dtype=recvbuf.dtype, buffer=recvbuf)
        for p0, p1 in lib.prange(0, numpy.max(counts), INT_MAX-7):
            counts_seg = counts - p0
            counts_seg[counts_seg<0] = 0
            comm.Gatherv([send_seg[p0:p1], mpi_dtype],
                         [recv_seg, counts_seg, displs+p0, mpi_dtype], root)
        return recvbuf.reshape((-1,) + sendbuf.shape[1:])
    else:
        send_seg = numpy.ndarray(sendbuf.size, dtype=sendbuf.dtype, buffer=sendbuf)
        for p0, p1 in lib.prange(0, numpy.max(counts), INT_MAX-7):
            counts_seg = counts - p0
            counts_seg[counts_seg<0] = 0
            comm.Gatherv([send_seg[p0:p1], mpi_dtype], None, root)
        return sendbuf


def allgather(sendbuf):
    sendbuf = numpy.asarray(sendbuf, order='C')
    attr = comm.allgather((sendbuf.size, sendbuf.dtype.char))
    counts = numpy.array([x[0] for x in attr])
    mpi_dtype = numpy.result_type(*[x[1] for x in attr]).char
    _assert(sendbuf.dtype.char == mpi_dtype or sendbuf.size == 0)
    #_assert(numpy.all(counts <= INT_MAX))
    _assert(all(x[1] == mpi_dtype for x in attr if x[0] > 0))
    displs = numpy.append(0, numpy.cumsum(counts[:-1]))
    recvbuf = numpy.empty(sum(counts), dtype=mpi_dtype)
    #comm.Allgatherv([sendbuf.ravel(), mpi_dtype],
    #                [recvbuf.ravel(), counts, displs, mpi_dtype])
    send_seg = numpy.ndarray(sendbuf.size, dtype=sendbuf.dtype, buffer=sendbuf)
    recv_seg = numpy.ndarray(recvbuf.size, dtype=recvbuf.dtype, buffer=recvbuf)
    for p0, p1 in lib.prange(0, numpy.max(counts), INT_MAX-7):
        counts_seg = counts - p0
        counts_seg[counts_seg<0] = 0
        comm.Allgatherv([send_seg[p0:p1], mpi_dtype],
                        [recv_seg, counts_seg, displs+p0, mpi_dtype])
    shape = comm.bcast(sendbuf.shape)
    return recvbuf.reshape((-1,) + shape[1:])

def alltoall(sendbuf, split_recvbuf=False):
    if isinstance(sendbuf, numpy.ndarray):
        mpi_dtype = comm.bcast(sendbuf.dtype.char)
        sendbuf = numpy.asarray(sendbuf, mpi_dtype, 'C')
        nrow = sendbuf.shape[0]
        ncol = sendbuf.size // nrow
        segsize = (nrow+pool.size-1) // pool.size * ncol
        sdispls = numpy.arange(0, pool.size*segsize, segsize)
        sdispls[sdispls>sendbuf.size] = sendbuf.size
        scounts = numpy.append(sdispls[1:]-sdispls[:-1], sendbuf.size-sdispls[-1])
    else:
        _assert(len(sendbuf) == pool.size)
        mpi_dtype = comm.bcast(sendbuf[0].dtype.char)
        sendbuf = [numpy.asarray(x, mpi_dtype).ravel() for x in sendbuf]
        scounts = numpy.asarray([x.size for x in sendbuf])
        sdispls = numpy.append(0, numpy.cumsum(scounts[:-1]))
        sendbuf = numpy.hstack(sendbuf)

    rcounts = numpy.asarray(comm.alltoall(scounts))
    rdispls = numpy.append(0, numpy.cumsum(rcounts[:-1]))

    #_assert(numpy.all(scounts <= INT_MAX))
    recvbuf = numpy.empty(sum(rcounts), dtype=mpi_dtype)
    #comm.Alltoallv([sendbuf.ravel(), scounts, sdispls, mpi_dtype],
    #               [recvbuf.ravel(), rcounts, rdispls, mpi_dtype])
    send_seg = numpy.ndarray(sendbuf.size, dtype=sendbuf.dtype, buffer=sendbuf)
    recv_seg = numpy.ndarray(recvbuf.size, dtype=recvbuf.dtype, buffer=recvbuf)
    for p0, p1 in lib.prange(0, numpy.max(scounts), INT_MAX-7):
        scounts_seg = scounts - p0
        rcounts_seg = rcounts - p0
        scounts_seg[scounts_seg<0] = 0
        rcounts_seg[rcounts_seg<0] = 0
        comm.Alltoallv([send_seg[p0:p1], scounts, sdispls+p0, mpi_dtype],
                       [recv_seg[p0:p1], rcounts, rdispls+p0, mpi_dtype])

    if split_recvbuf:
        return [recvbuf[p0:p0+c] for p0,c in zip(rdispls,rcounts)]
    else:
        return recvbuf

def sendrecv(sendbuf, source=0, dest=0):
    if source == dest:
        return sendbuf

    if rank == source:
        sendbuf = numpy.asarray(sendbuf, order='C')
        comm.send((sendbuf.shape, sendbuf.dtype), dest=dest)
        send_seg = numpy.ndarray(sendbuf.size, dtype=sendbuf.dtype, buffer=sendbuf)
        for p0, p1 in lib.prange(0, sendbuf.size, INT_MAX-7):
            comm.Send(send_seg[p0:p1], dest=dest)
        return sendbuf
    elif rank == dest:
        shape, dtype = comm.recv(source=source)
        recvbuf = numpy.empty(shape, dtype=dtype)
        recv_seg = numpy.ndarray(recvbuf.size, dtype=recvbuf.dtype, buffer=recvbuf)
        for p0, p1 in lib.prange(0, recvbuf.size, INT_MAX-7):
            comm.Recv(recv_seg[p0:p1], source=source)
        return recvbuf

def rotate(sendbuf):
    '''On every process, pass the sendbuf to the next process.'''
    if pool.size <= 1:
        return sendbuf

    if rank == 0:
        prev_node = pool.size - 1
        next_node = 1
    elif rank == pool.size - 1:
        prev_node = rank - 1
        next_node = 0
    else:
        prev_node = rank - 1
        next_node = rank + 1

    if isinstance(sendbuf, numpy.ndarray):
        if rank % 2 == 0:
            sendrecv(sendbuf, rank, next_node)
            recvbuf = sendrecv(None, prev_node, rank)
        else:
            recvbuf = sendrecv(None, prev_node, rank)
            sendrecv(sendbuf, rank, next_node)
    else:
        if rank % 2 == 0:
            comm.send(sendbuf, dest=next_node)
            recvbuf = comm.recv(source=prev_node)
        else:
            recvbuf = comm.recv(source=prev_node)
            comm.send(sendbuf, dest=next_node)
    return recvbuf

def _assert(condition):
    if not condition:
        sys.stderr.write(''.join(traceback.format_stack()[:-1]))
        comm.Abort()

def del_registry(reg_procs):
    if reg_procs:
        def f(reg_procs):
            from mpi4pyscf.tools import mpi
            mpi._registry.pop(reg_procs[mpi.rank])
        pool.apply(f, reg_procs, reg_procs)
    return []

def _init_on_workers(f_arg):
    from mpi4pyscf.tools import mpi
    module, name, args, kwargs = f_arg
    if args is None and kwargs is None:
        if module is None:  # master proccess
            obj = name
            mpi.comm.bcast(obj.pack())
        else:
            cls = getattr(importlib.import_module(module), name)
            obj = cls.__new__(cls)
            obj.unpack_(mpi.comm.bcast(None))
    elif module is None:  # master proccess
        obj = name
    else:
        from pyscf.gto import mole
        from pyscf.pbc.gto import cell
        # Guess whether the args[0] is the serialized mole or cell objects
        if isinstance(args[0], str) and args[0][0] == '{':
            if '_pseudo' in args[0]:
                c = cell.loads(args[0])
                args = (c,) + args[1:]
            elif '_bas' in args[0]:
                m = mole.loads(args[0])
                args = (m,) + args[1:]
        cls = getattr(importlib.import_module(module), name)
        obj = cls(*args, **kwargs)
    key = id(obj)
    mpi._registry[key] = obj
    regs = mpi.comm.gather(key)
    return regs

if rank == 0:
    from pyscf.gto import mole
    def _init_and_register(cls, with_init_args):
        old_init = cls.__init__
        def init(obj, *args, **kwargs):
            old_init(obj, *args, **kwargs)

# * Do not issue mpi communication inside the __init__ method
# * Avoid the distributed class being called twice from subclass  __init__ method
# * hasattr(obj, '_reg_procs') to ensure class is created only once
# * If class initialized in mpi session, bypass the distributing step
            if pool.worker_status == 'P' and not hasattr(obj, '_reg_procs'):
                cls = obj.__class__
                if len(args) > 0 and isinstance(args[0], mole.Mole):
                    regs = pool.apply(_init_on_workers, (None, obj, args, kwargs),
                                      (cls.__module__, cls.__name__,
                                       (args[0].dumps(),)+args[1:], kwargs))
                elif with_init_args:
                    regs = pool.apply(_init_on_workers, (None, obj, args, kwargs),
                                      (cls.__module__, cls.__name__, args, kwargs))
                else:
                    regs = pool.apply(_init_on_workers, (None, obj, None, None),
                                      (cls.__module__, cls.__name__, None, None))

                # Keep track of the object in a global registry.  The object can
                # be accessed from global registry on workers.
                obj._reg_procs = regs
        return init
    def _with_enter(obj):
        return obj
    def _with_exit(obj):
        obj._reg_procs = del_registry(obj._reg_procs)

    def register_class(cls, with_init_args=True):
        cls.__init__ = _init_and_register(cls, with_init_args)
        cls.__enter__ = _with_enter
        cls.__exit__ = _with_exit
        cls.close = _with_exit
        return cls
else:
    def register_class(cls, with_init_args=True):
        return cls

def _distribute_call(f_arg):
    module, name, reg_procs, args, kwargs = f_arg
    dev = reg_procs
    if module is None:  # Master process
        fn = name
    elif dev is None:
        fn = getattr(importlib.import_module(module), name)
    else:
        from mpi4pyscf.tools import mpi
        dev = mpi._registry[reg_procs[mpi.rank]]
        fn = getattr(importlib.import_module(module), name)
    return fn(dev, *args, **kwargs)

if rank == 0:
    def parallel_call(f):
        def with_mpi(dev, *args, **kwargs):
            if pool.worker_status == 'R':
# A direct call if worker is not in pending mode
                return f(dev, *args, **kwargs)
            elif hasattr(dev, '_reg_procs'):
                return pool.apply(_distribute_call, (None, f, dev, args, kwargs),
                                  (f.__module__, f.__name__, dev._reg_procs, args, kwargs))
            else:
                return pool.apply(_distribute_call, (None, f, dev, args, kwargs),
                                  (f.__module__, f.__name__, None, args, kwargs))
        return with_mpi
else:
    def parallel_call(f):
        return f


if rank == 0:
    def _merge_yield(fn):
        def main_yield(dev, *args, **kwargs):
            for x in fn(dev, *args, **kwargs):
                yield x
            for src in range(1, pool.size):
                while True:
                    dat = comm.recv(None, source=src)
                    if isinstance(dat, str) and dat == 'EOY':
                        break
                    yield dat
        return main_yield
    def reduced_yield(f):
        def with_mpi(dev, *args, **kwargs):
            return pool.apply(_distribute_call, (None, _merge_yield(f), dev, args, kwargs),
                              (f.__module__, f.__name__, dev._reg_procs, args, kwargs))
        return with_mpi
else:
    def reduced_yield(f):
        def client_yield(*args, **kwargs):
            for x in f(*args, **kwargs):
                comm.send(x, 0)
            comm.send('EOY', 0)
        return client_yield

def _reduce_call(f_arg):
    from mpi4pyscf.tools import mpi
    module, name, reg_procs, args, kwargs = f_arg
    if module is None:  # Master process
        fn = name
        dev = reg_procs
    else:
        dev = mpi._registry[reg_procs[mpi.rank]]
        fn = getattr(importlib.import_module(module), name)
    return mpi.reduce(fn(dev, *args, **kwargs))
if rank == 0:
    def call_then_reduce(f):
        def with_mpi(dev, *args, **kwargs):
            if pool.worker_status == 'R':
# A direct call if worker is not in pending mode
                return reduce(f(dev, *args, **kwargs))
            else:
                return pool.apply(_reduce_call, (None, f, dev, args, kwargs),
                                  (f.__module__, f.__name__, dev._reg_procs, args, kwargs))
        return with_mpi
else:
    def call_then_reduce(f):
        return f

