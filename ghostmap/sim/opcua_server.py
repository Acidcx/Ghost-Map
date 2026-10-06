"""Simulated OPC UA server shaped roughly like FactoryTalk Linx Gateway.

Two presses expose the same kind of data with drifting names, the way real
machines do: ``PRESS_01`` uses ``PressFireCount`` / ``MachineState`` /
``Faults[]`` (fault UDT variant A), ``PRESS_02`` uses ``Press_Fire_Cnt`` /
``Mach_State`` / ``Fault_Log[]`` (variant B, with an extra ``Station`` member).
Counters tick while the server runs. Everything is read-only.

This is an approximation for demos and tests, not a copy of FT Linx Gateway's
real address space.
"""

from __future__ import annotations

import asyncio
import random
from typing import Optional

from asyncua import Server, ua

NS_URI = "urn:ghostmap:sim:ftlinx"

MACHINES = {
    "PRESS_01": {
        "counters": {"PressFireCount": 1_204_331, "ShearFireCount": 98_412},
        "state": ("MachineState", {"Auto_Running": True, "Faulted": False, "Mode": 2}),
        "faults": ("Faults", ["Code", "Active", "Timestamp"]),
        "program": {"Recipe_Speed": 42.5, "Ideal_Cycle_s": 2.4},
    },
    "PRESS_02": {
        "counters": {"Press_Fire_Cnt": 877_120, "Shear_Cnt": 61_007},
        "state": ("Mach_State", {"AutoRun": True, "Fault": False, "Mode": 2}),
        "faults": ("Fault_Log", ["Code", "Active", "Time", "Station"]),
        "program": {"RecipeSpeed": 38.0, "CycleTimeIdeal": 2.6},
    },
}


def _vt(value):
    if isinstance(value, bool):
        return ua.VariantType.Boolean
    if isinstance(value, int):
        return ua.VariantType.Int32  # Logix DINT
    if isinstance(value, float):
        return ua.VariantType.Float  # Logix REAL
    return ua.VariantType.String


class SimUaServer:
    def __init__(self, host: str = "127.0.0.1", port: int = 4899):
        self.endpoint = f"opc.tcp://{host}:{port}"
        self.server = Server()
        self._task: Optional[asyncio.Task] = None
        self._counters: list[tuple] = []
        self._states: list = []

    async def _var(self, parent, ns, sid, name, value):
        node = await parent.add_variable(ua.NodeId(sid, ns), ua.QualifiedName(name, ns),
                                         ua.Variant(value, _vt(value)))
        return node  # not set_writable(): read-only like Ghost Map expects

    async def start(self) -> "SimUaServer":
        await self.server.init()
        self.server.set_endpoint(self.endpoint)
        self.server.set_server_name("Ghost Map simulated FactoryTalk Linx Gateway")
        self.server.set_security_policy([ua.SecurityPolicyType.NoSecurity])
        ns = await self.server.register_namespace(NS_URI)
        root = await self.server.nodes.objects.add_folder(ua.NodeId("FTLinxGateway", ns),
                                                          ua.QualifiedName("FactoryTalk Linx Gateway", ns))
        for shortcut, m in MACHINES.items():
            folder = await root.add_folder(ua.NodeId(f"[{shortcut}]", ns), ua.QualifiedName(shortcut, ns))
            for name, start in m["counters"].items():
                node = await self._var(folder, ns, f"[{shortcut}]{name}", name, start)
                self._counters.append((node, start))
            sname, members = m["state"]
            st = await folder.add_object(ua.NodeId(f"[{shortcut}]{sname}", ns), ua.QualifiedName(sname, ns))
            for k, v in members.items():
                node = await self._var(st, ns, f"[{shortcut}]{sname}.{k}", k, v)
                if isinstance(v, bool) and k.lower().startswith("auto"):
                    self._states.append(node)
            fname, fmembers = m["faults"]
            arr = await folder.add_object(ua.NodeId(f"[{shortcut}]{fname}", ns), ua.QualifiedName(fname, ns))
            for i in range(4):
                el = await arr.add_object(ua.NodeId(f"[{shortcut}]{fname}[{i}]", ns),
                                          ua.QualifiedName(f"{fname}[{i}]", ns))
                for mem in fmembers:
                    v = {"Code": 0 if i else 117, "Active": False, "Station": "Shear" if i == 0 else ""}.get(mem, 0)
                    await self._var(el, ns, f"[{shortcut}]{fname}[{i}].{mem}", mem, v)
            prog = await folder.add_object(ua.NodeId(f"[{shortcut}]Program:MainProgram", ns),
                                           ua.QualifiedName("Program:MainProgram", ns))
            for name, v in m["program"].items():
                await self._var(prog, ns, f"[{shortcut}]Program:MainProgram.{name}", name, v)
        await self.server.start()
        self._task = asyncio.create_task(self._tick())
        return self

    async def _tick(self):
        counts = {node: start for node, start in self._counters}
        while True:
            await asyncio.sleep(1.0)
            for node in counts:
                if random.random() < 0.8:
                    counts[node] += 1
                    await node.write_value(ua.Variant(counts[node], ua.VariantType.Int32))

    async def stop(self):
        if self._task:
            self._task.cancel()
        await self.server.stop()
