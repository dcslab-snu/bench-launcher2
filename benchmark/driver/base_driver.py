# coding: UTF-8

import asyncio
import functools
import json
from abc import ABCMeta, abstractmethod
from itertools import chain
from signal import SIGCONT, SIGSTOP
from typing import Any, Callable, Coroutine, Iterable, List, Optional, Set, Tuple, Type

import psutil

from ..utils.cgroup_cpuset import CgroupCpuset
from ..utils.dvfs import DVFS
from ..utils.hyphen import convert_to_hyphen, convert_to_set
from ..utils.numa_topology import NumaTopology
from ..utils.resctrl import ResCtrl


class BenchDriver(metaclass=ABCMeta):
    class _Decorators:
        @staticmethod
        def ensure_running(func: Callable[['BenchDriver', Any], Any]):
            @functools.wraps(func)
            def decorator(self: 'BenchDriver', *args, **kwargs):
                if not self.is_running:
                    raise RuntimeError(f'The benchmark ({self._name}) has already ended or never been invoked.'
                                       ' Run benchmark first via invoking `run()`!')
                return func(self, *args, **kwargs)

            return decorator

        @staticmethod
        def ensure_not_running(func: Callable[['BenchDriver', Any], Any]):
            @functools.wraps(func)
            def decorator(self: 'BenchDriver', *args, **kwargs):
                if self.is_running:
                    raise RuntimeError(f'The benchmark ({self._name}) has already ended or never been invoked.'
                                       ' Run benchmark first via invoking `run()`!')
                return func(self, *args, **kwargs)

            return decorator

        @staticmethod
        def ensure_invoked(func: Callable[['BenchDriver', Any], Any]):
            @functools.wraps(func)
            def decorator(self: 'BenchDriver', *args, **kwargs):
                if not self.has_invoked:
                    raise RuntimeError(f'The benchmark ({self._name}) has never been invoked.'
                                       ' Run benchmark first via invoking `run()`!')
                return func(self, *args, **kwargs)

            return decorator

    _benches: Set[str] = None
    _bench_home: str = None
    bench_name: str = None

    def __init__(self, name: str, workload_type: str, identifier: str, num_threads: int, binding_cores: str, numa_mem_nodes: Optional[str],
                 cpu_freq: float, cbm_ranges: str):
        self._name: str = name
        self._type: str = workload_type
        self._identifier: str = identifier
        self._num_threads: int = num_threads
        self._binding_cores: str = binding_cores
        self._cpu_freq: float = cpu_freq
        self._cbm_ranges: str = cbm_ranges
        self._numa_mem_nodes: Optional[str] = numa_mem_nodes

        self._bench_proc_info: Optional[psutil.Process] = None
        self._async_proc: Optional[asyncio.subprocess.Process] = None
        self._async_proc_info: Optional[psutil.Process] = None

        self._group_name = identifier
        self._cgroup: CgroupCpuset = CgroupCpuset(identifier)
        self._resctrl_group: ResCtrl = ResCtrl()

    def __del__(self):
        try:
            self.stop()
        except (psutil.NoSuchProcess, ProcessLookupError):
            pass

    @staticmethod
    @abstractmethod
    def has(bench_name: str) -> bool:
        pass

    @staticmethod
    def get_bench_home(bench_name: str) -> str:
        from benchmark_launcher import GLOBAL_CFG_PATH
        with GLOBAL_CFG_PATH.open() as fp:
            return json.load(fp)['benchmark'][bench_name]

    @property
    def name(self) -> str:
        return self._name

    @property
    def wl_type(self) -> str:
        return self._type

    @property
    @_Decorators.ensure_invoked
    def created_time(self) -> float:
        return self._bench_proc_info.create_time()

    @property
    def _is_running(self) -> bool:
        """
        Check if this benchmark is running.

        The difference with :func:`benchmark.driver.base_driver.BenchDriver.is_running` is this method is more precise,
        but has more cost.

        :return: True if running
        """
        return self.is_running and self._find_bench_proc() is not None

    @property
    def has_invoked(self):
        return self._bench_proc_info is not None and \
               self._async_proc is not None and \
               self._async_proc_info is not None

    @property
    def is_running(self):
        return self.has_invoked and self._bench_proc_info.is_running()

    @property
    def pid(self) -> Optional[int]:
        """
        Get effective pid of this benchmark (not python pid) without blocking.
        :return: actual pid of benchmark process
        """
        if self._bench_proc_info is None:
            return None
        else:
            return self._bench_proc_info.pid

    @abstractmethod
    async def _launch_bench(self) -> asyncio.subprocess.Process:
        pass

    @abstractmethod
    def _find_bench_proc(self) -> Optional[psutil.Process]:
        pass

    @_Decorators.ensure_not_running
    async def run(self) -> None:
        self._bench_proc_info = None

        await self._cgroup.create_group()
        await self._cgroup.assign_cpus(self._binding_cores)
        mem_sockets: str = await self.__get_effective_mem_nodes()
        await self._cgroup.assign_mems(mem_sockets)

        self._async_proc = await self._launch_bench()
        self._async_proc_info = psutil.Process(self._async_proc.pid)

        nodes = await NumaTopology.get_node_topo()

        # Masks for cbm_mask
        masks = ['1'] * (max(nodes) + 1)

        # change the cbm_ranges to cbm_ranges_list
        cbm_ranges_list: List[List[str]] = ResCtrl.cbm_ranges_to_list(self._cbm_ranges)
        for socket_id, cbm_range in enumerate(cbm_ranges_list):
            start, end = cbm_range
            cbm_mask = ResCtrl.gen_mask(int(start), int(end))
            masks[socket_id] = cbm_mask

        # setting freq to local config
        core_set = convert_to_set(self._binding_cores)
        cpufreq_khz = int(self._cpu_freq * 1000000)
        DVFS.set_freq(cpufreq_khz, core_set)

        while True:
            self._bench_proc_info = self._find_bench_proc()
            if self._bench_proc_info is not None:
                await self._rename_group(f'{self._name}_{self._bench_proc_info.pid}')
                await self._resctrl_group.create_group()
                await self._resctrl_group.assign_llc(*masks)
                await self._resctrl_group.add_tasks(self.all_child_tid())
                return
            await asyncio.sleep(0.1)

    @_Decorators.ensure_running
    async def join(self) -> None:
        await self._async_proc.wait()

    def stop(self) -> None:
        self._async_proc.kill()
        self._bench_proc_info.kill()
        self._async_proc_info.kill()

    @_Decorators.ensure_running
    def pause(self) -> None:
        self._async_proc.send_signal(SIGSTOP)
        self._bench_proc_info.suspend()

    @_Decorators.ensure_running
    def resume(self) -> None:
        self._async_proc.send_signal(SIGCONT)
        self._bench_proc_info.resume()

    @_Decorators.ensure_running
    def all_child_tid(self) -> Iterable[int]:
        return chain(
                (t.id for t in self._bench_proc_info.threads()),
                *((t.id for t in proc.threads()) for proc in self._bench_proc_info.children(recursive=True))
        )

    async def __get_effective_mem_nodes(self) -> str:
        # Explicit Mem Node Alloc
        if self._numa_mem_nodes is not None:
            return self._numa_mem_nodes

        # Local Alloc Case
        cpu_topo, mem_topo = await NumaTopology.get_numa_info()
        bound_cores = convert_to_set(self._binding_cores)

        belonged_sockets: List[int] = list()

        for socket_id, core_ids in cpu_topo.items():
            if len(core_ids & bound_cores) is not 0:
                belonged_sockets.append(socket_id)

        return convert_to_hyphen(belonged_sockets)

    @_Decorators.ensure_running
    async def _rename_group(self, new_name: str) -> None:
        self._group_name = new_name

        await self._cgroup.rename(new_name)
        self._resctrl_group.group_name = self._group_name

    async def cleanup(self) -> None:
        await self._cgroup.delete()
        await self._resctrl_group.delete()

    def read_resctrl(self) -> Coroutine[None, None, Tuple[int, int, int]]:
        return self._resctrl_group.read()


def find_driver(workload_name) -> Type[BenchDriver]:
    from benchmark.driver.spec_driver import SpecDriver
    from benchmark.driver.parsec_driver import ParsecDriver
    from benchmark.driver.rodinia_driver import RodiniaDriver
    from benchmark.driver.npb_driver import NPBDriver

    bench_drivers = (SpecDriver, ParsecDriver, RodiniaDriver, NPBDriver)

    for _bench_driver in bench_drivers:
        if _bench_driver.has(workload_name):
            return _bench_driver

    raise ValueError(f'Can not find appropriate driver for workload : {workload_name}')


def bench_driver(workload_name: str, workload_type: str, identifier: str, num_threads: int, binding_cores: str,
                 numa_mem_nodes: Optional[str], cpu_freq: float, cbm_ranges: str) -> BenchDriver:
    _bench_driver = find_driver(workload_name)

    return _bench_driver(workload_name, workload_type, identifier, num_threads, binding_cores, numa_mem_nodes,
                         cpu_freq, cbm_ranges)
