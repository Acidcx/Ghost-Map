"""Simulated OPC UA server shaped roughly like FactoryTalk Linx Gateway.

Two presses expose the same kind of data with drifting names, the way real
machines do: ``PRESS_01`` uses ``PressFireCount`` / ``MachineState`` /
``Faults[]`` (fault UDT variant A), ``PRESS_02`` uses ``Press_Fire_Cnt`` /
``Mach_State`` / ``Fault_Log[]`` (variant B, with an extra ``Station`` member).
Counters tick while the server runs. Everything is read-only.

``LEVELER_01`` is shaped like a real FT Linx Gateway project: NodeIds of the
form ``::[LEVELER_01]Program:MainProgram.FAULT.<Area>.<Tag>``, alarm BOOLs
grouped by area, TON timers, a Comms area where "on" means healthy, and a
Production area with run state and counters. The tag names are invented.

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

# area -> {tag: initial value}; a dict value is a Logix TIMER (members EN/TT/DN/ACC/PRE).
_TMR = {"EN": True, "TT": False, "DN": True, "ACC": 2000, "PRE": 2000}
# A fault that brings a second one with it, 2 s later: the first is the first-out.
CASCADES = {"E_Stop_Flt": "Roll_Drive_Flt", "Control_Power_Off": "Entry_VFD_Comms_Flt",
            "HPU_Low_Oil_Level": "HPU_Low_Pressure", "Low_Air_Pressure": "Coil_Car_OverTravel"}

LEVELER_FAULTS = {
    "General": {"E_Stop_Flt": False, "Guard_Door_Open": False, "Low_Air_Pressure": False,
                "Control_Power_Off": False, "PLC_Battery_Low": False, "PLC_Minor_Flts": 0},
    "Entry": {"Uncoiler_Drive_Flt": False, "Uncoiler_MS": False, "Uncoiler_Motor_OverTemp": False,
              "Coil_Car_OverTravel": False, "Entry_VFD_Comms_Flt": False, "Uncoiler_Brake_Sts": True},
    "Leveler": {"Roll_Drive_Flt": False, "Roll_Motor_OverTemp": False, "Roll_Motor_OverTemp_Warn": False,
                "Work_Roll_Lube_Low": False, "Gap_Encoder_Flt": False, "Cool_Fan_MS": False,
                "Cool_Fan_MS_Tmr": dict(_TMR)},
    "Hydraulics": {"HPU_MS": False, "HPU_Low_Oil_Level": False, "HPU_OverTemp": False, "HPU_OverTemp_Warn": False,
                   "HPU_Filter_Plugged": False, "HPU_Low_Pressure": False, "HPU_MS_Tmr": dict(_TMR)},
    # Comms OK bits: on = healthy. The dashboard generator should suggest flipping this area.
    "Comms": {"Entry_Rack": True, "Exit_Rack": True, "Leveler_VFD": True, "Uncoiler_VFD": True,
              "Safety_PLC": True, "HMI": True},
}
# A Kinetix-style AXIS_CIP_DRIVE (a small subset of its ~600 members) at controller scope.
LEVELER_AXIS = {"CIPAxisState": 4, "AxisFault": 0, "CIPAxisFaults": 0, "CIPAxisAlarms": 0, "ModuleFaults": 0,
                "DriveEnableStatus": True, "ServoActionStatus": True, "AxisHomedStatus": True,
                "ActualPosition": 0.0, "ActualVelocity": 0.0, "CurrentFeedback": 0.0, "DCBusVoltage": 650.0,
                "MotorCapacity": 0.0, "TorqueLimitPositive": 200.0, "VelocityLoopBandwidth": 13.5,
                "BusUndervoltageFault": False, "BusUndervoltageAlarm": False, "MotorOvertemperatureFault": False,
                "ExcessivePositionErrorFault": False, "FeedbackSignalLossFLFault": False, "EnableInputDeactivatedAlarm": False}
LEVELER_PRODUCTION = {"Auto_Batch_Runout": False, "Line_Running": True, "Auto_Mode": True, "Line_Speed_FPM": 120.0,
                      "Coil_Length_Ft": 0.0, "Footage_Count": 3_481_220, "Coil_Count": 1_874}


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
        self._lev: dict = {}
        self._bit_by_name: dict = {}
        self.auto_faults = True  # tests turn the random faults off and use set_fault()

    async def _var(self, parent, ns, sid, name, value):
        node = await parent.add_variable(ua.NodeId(sid, ns), ua.QualifiedName(name, ns),
                                         ua.Variant(value, _vt(value)))
        return node  # not set_writable(): read-only like Ghost Map expects

    async def start(self) -> "SimUaServer":
        await self.server.init()
        self.server.set_endpoint(self.endpoint)
        self.server.set_server_name("Ghost Map simulated FactoryTalk Linx Gateway")
        self.server.set_security_policy([ua.SecurityPolicyType.NoSecurity])
        ns = self._ns = await self.server.register_namespace(NS_URI)
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
        await self._add_leveler(root, ns)
        await self.server.start()
        self._task = asyncio.create_task(self._tick())
        return self

    async def _add_leveler(self, root, ns):
        sc = "::[LEVELER_01]"
        folder = await root.add_folder(ua.NodeId(sc, ns), ua.QualifiedName("LEVELER_01", ns))
        prog = await folder.add_object(ua.NodeId(f"{sc}Program:MainProgram", ns),
                                       ua.QualifiedName("Program:MainProgram", ns))

        async def obj(parent, sid, name):
            return await parent.add_object(ua.NodeId(sid, ns), ua.QualifiedName(name, ns))

        base = f"{sc}Program:MainProgram"
        fault = await obj(prog, f"{base}.FAULT", "FAULT")
        bits, self._bit_by_name = [], {}
        for area, tags in LEVELER_FAULTS.items():
            a = await obj(fault, f"{base}.FAULT.{area}", area)
            for name, v in tags.items():
                sid = f"{base}.FAULT.{area}.{name}"
                if isinstance(v, dict):
                    t = await obj(a, sid, name)
                    for m, mv in v.items():
                        await self._var(t, ns, f"{sid}.{m}", m, mv)
                    continue
                node = await self._var(a, ns, sid, name, v)
                if isinstance(v, bool) and area != "Comms" and not name.endswith("_Sts"):
                    bits.append(node)
                    self._bit_by_name[name] = node
        prod = await obj(prog, f"{base}.Production", "Production")
        nodes = {}
        for name, v in LEVELER_PRODUCTION.items():
            nodes[name] = await self._var(prod, ns, f"{base}.Production.{name}", name, v)
        # A controller-scope heartbeat counter the PLC bumps every second (for the comms health check).
        hb = await self._var(folder, ns, f"{sc}Heartbeat", "Heartbeat", 0)
        # Controller-scope axis, an I/O module tag and a long recipe array (the last two should be left out).
        axis = await obj(folder, f"{sc}Ax_Leveler_Roll", "Ax_Leveler_Roll")
        axis_nodes = {}
        for name, v in LEVELER_AXIS.items():
            axis_nodes[name] = await self._var(axis, ns, f"{sc}Ax_Leveler_Roll.{name}", name, v)
        mod = await obj(folder, f"{sc}Local:1:I", "Local:1:I")
        await self._var(mod, ns, f"{sc}Local:1:I.Data", "Data", 0)
        recipe = await obj(prod, f"{base}.Production.Recipe_Length", "Recipe_Length")
        for i in range(100):
            await self._var(recipe, ns, f"{base}.Production.Recipe_Length[{i}]", f"Recipe_Length[{i}]", 0.0)
        self._lev = {"axis": axis_nodes, "bits": bits, "prod": nodes, "active": None, "ft": float(LEVELER_PRODUCTION["Footage_Count"]),
                     "coil_ft": 0.0, "coils": LEVELER_PRODUCTION["Coil_Count"], "hb": hb}

    async def _tick_leveler(self, n: int):
        lev = self._lev
        if not lev:
            return
        # Every 20 s raise one random fault for about 12 s, so the dashboard has something to show. Some
        # faults bring a second one 2 s later (an E-stop drops the roll drive), so the history has a first-out.
        if not self.auto_faults:
            pass
        elif n % 20 == 0:
            lev["active"] = random.choice(lev["bits"])
            lev["follow"] = None
            await lev["active"].write_value(ua.Variant(True, ua.VariantType.Boolean))
        elif n % 20 == 2 and lev["active"] is not None:
            name = next((k for k, v in self._bit_by_name.items() if v is lev["active"]), "")
            if name in CASCADES:
                lev["follow"] = self._bit_by_name[CASCADES[name]]
                await lev["follow"].write_value(ua.Variant(True, ua.VariantType.Boolean))
        elif n % 20 == 12 and lev["active"] is not None:
            for node in (lev["active"], lev.get("follow")):
                if node is not None:
                    await node.write_value(ua.Variant(False, ua.VariantType.Boolean))
            lev["active"] = lev["follow"] = None
        if not lev.get("hb_frozen"):
            await lev["hb"].write_value(ua.Variant(n, ua.VariantType.Int32))
        running = lev["active"] is None
        p = lev["prod"]
        await p["Line_Running"].write_value(ua.Variant(running, ua.VariantType.Boolean))
        speed = 120.0 + random.uniform(-3, 3) if running else 0.0
        await p["Line_Speed_FPM"].write_value(ua.Variant(speed, ua.VariantType.Float))
        lev["ft"] += speed / 60
        lev["coil_ft"] += speed / 60
        if lev["coil_ft"] > 1500:
            lev["coil_ft"], lev["coils"] = 0.0, lev["coils"] + 1
        await p["Footage_Count"].write_value(ua.Variant(int(lev["ft"]), ua.VariantType.Int32))
        await p["Coil_Count"].write_value(ua.Variant(lev["coils"], ua.VariantType.Int32))
        await p["Coil_Length_Ft"].write_value(ua.Variant(lev["coil_ft"], ua.VariantType.Float))
        ax = lev["axis"]
        faulted = self.auto_faults and n % 60 >= 45  # the roll axis faults on motor over-temperature for 15 s a minute
        await ax["CIPAxisState"].write_value(ua.Variant(8 if faulted else 4, ua.VariantType.Int32))
        await ax["AxisFault"].write_value(ua.Variant(1 if faulted else 0, ua.VariantType.Int32))
        await ax["MotorOvertemperatureFault"].write_value(ua.Variant(faulted, ua.VariantType.Boolean))
        vel = 0.0 if faulted or not running else speed * 0.2
        lev.setdefault("pos", 0.0)
        lev["pos"] = (lev["pos"] + vel) % 360
        await ax["ActualVelocity"].write_value(ua.Variant(vel, ua.VariantType.Float))
        await ax["ActualPosition"].write_value(ua.Variant(lev["pos"], ua.VariantType.Float))
        await ax["MotorCapacity"].write_value(ua.Variant(35.0 + random.uniform(-2, 2) if vel else 0.0, ua.VariantType.Float))

    async def _tick(self):
        counts = {node: start for node, start in self._counters}
        n = 0
        while True:
            await asyncio.sleep(1.0)
            n += 1
            await self._tick_leveler(n)
            for node in counts:
                if random.random() < 0.8:
                    counts[node] += 1
                    await node.write_value(ua.Variant(counts[node], ua.VariantType.Int32))

    async def fault_comms(self, area: str, lost: bool = True) -> int:
        """Make one LEVELER_01 fault area read as BadCommunicationError, like FT Linx when it loses the PLC
        (tests). Returns how many tags changed."""
        n = 0
        code = ua.StatusCode(ua.StatusCodes.BadCommunicationError if lost else ua.StatusCodes.Good)
        for name, v in LEVELER_FAULTS[area].items():
            if isinstance(v, bool):
                node = self.server.get_node(ua.NodeId(f"::[LEVELER_01]Program:MainProgram.FAULT.{area}.{name}", self._ns))
                await node.write_value(ua.DataValue(ua.Variant(v, ua.VariantType.Boolean), code))
                n += 1
        return n

    async def set_fault(self, name: str, on: bool = True) -> None:
        """Turn one LEVELER_01 fault bit on or off (tests; set ``auto_faults = False`` first)."""
        await self._bit_by_name[name].write_value(ua.Variant(on, ua.VariantType.Boolean))

    def freeze_heartbeat(self, frozen: bool = True) -> None:
        """Stop the heartbeat counter, like a gateway serving stale values (tests)."""
        if self._lev:
            self._lev["hb_frozen"] = frozen

    async def stop(self):
        if self._task:
            self._task.cancel()
        await self.server.stop()
