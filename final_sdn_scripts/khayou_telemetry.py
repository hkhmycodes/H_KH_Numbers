"""
Drop-in additions for khayou_controller.py, packaged as a mixin.

Integration (three edits in khayou_controller.py):

    from khayou_telemetry import KhayouTelemetryMixin

    class KhayouController(KhayouTelemetryMixin, app_manager.RyuApp):   # mixin FIRST
        ...

    # at the end of your graph-skeleton setup (once G is built):
        self._dump_graph_edges()

Contract:
  - This mixin intentionally OVERRIDES _compute_and_log_khayou_metrics.
    Any future change to the base method must be ported here too.
  - It assumes self.G (nx.DiGraph, node attrs c and l, root node named "rho"),
    self.datapaths, self._controller_start_time, and (optionally) self._stopping
    already exist on the host class.
  - It does NOT reroute data-plane flows after a link failure. That depends on
    how the host controller installs forwarding rules.
"""
import json
import os
import resource
import time

from ryu.controller import ofp_event
from ryu.controller.handler import MAIN_DISPATCHER, set_ev_cls

from khayou_metrics import compute_khayou

ROOT = "rho"


class KhayouTelemetryMixin:
    # Initialized lazily. All mutations assign an instance attribute first
    # (self._x = ...), never mutate the class-level object in place.
    _port_map = None
    _removed_edges = None

    # ---------------------------------------------------------------- metrics
    def _compute_and_log_khayou_metrics(self):
        if getattr(self, "_stopping", False) or not getattr(self, "datapaths", None):
            return
        t0 = time.monotonic()
        res = compute_khayou(self.G, root=ROOT)
        cycle_ms = (time.monotonic() - t0) * 1000.0
        # ru_maxrss is the PEAK resident set (KiB on Linux).
        rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
        elapsed = time.monotonic() - self._controller_start_time
        removed = sum(len(v) for v in (self._removed_edges or {}).values())

        s_g = int(res.pop("S_G"))
        parts = [
            f"ts={time.time():.3f}",
            f"elapsed={elapsed:.3f}",
            f"S_G={s_g}",
        ]
        parts += [f"{k}={v:.6f}" for k, v in res.items()]
        parts += [
            f"cycle_ms={cycle_ms:.2f}",
            f"rss_mb={rss_mb:.1f}",
            f"edges_removed={removed}",
        ]
        print("METRIC_SAMPLE " + " ".join(parts), flush=True)

    # ------------------------------------------------------------ graph dump
    def _dump_graph_edges(self, path=None):
        """Write the controller's graph to JSON so scc_table.py uses the SAME graph."""
        path = path or os.environ.get(
            "KHAYOU_GRAPH_DUMP", "results/graph_edges.json"
        )
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as fh:
            json.dump(
                {
                    "nodes": list(self.G.nodes()),
                    "edges": [list(e) for e in self.G.edges()],
                },
                fh,
                indent=1,
            )

    # ------------------------------------------------------- link fail / heal
    def _load_port_map(self):
        """Written by run_emulation.py after net.start(); loaded lazily."""
        if self._port_map is not None:
            return self._port_map
        path = os.environ.get("KHAYOU_PORT_MAP", "")
        if not path or not os.path.exists(path):
            return {}
        try:
            with open(path) as fh:
                rows = json.load(fh)
        except Exception:
            return {}
        self._port_map = {
            (int(r["dpid"]), int(r["port_no"])): (r["sw"], r["neighbor"])
            for r in rows
        }
        return self._port_map

    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
    def _port_status_handler(self, ev):
        msg = ev.msg
        dp = msg.datapath
        ofp = dp.ofproto
        key = (int(dp.id), int(msg.desc.port_no))
        link = self._load_port_map().get(key)
        if link is None:
            return
        sw, nbr = link
        down = bool(msg.desc.state & ofp.OFPPS_LINK_DOWN) or bool(
            msg.desc.config & ofp.OFPPC_PORT_DOWN
        )
        if self._removed_edges is None:
            self._removed_edges = {}

        if down and key not in self._removed_edges:
            gone = []
            for u, v in ((sw, nbr), (nbr, sw)):
                if self.G.has_edge(u, v):
                    attrs = dict(self.G.edges[u, v])
                    self.G.remove_edge(u, v)
                    gone.append((u, v, attrs))
            self._removed_edges[key] = gone
            print(
                f"FAULT_EVENT ts={time.time():.3f} state=down "
                f"sw={sw} nbr={nbr} edges_removed={len(gone)}",
                flush=True,
            )
        elif not down and key in self._removed_edges:
            for u, v, attrs in self._removed_edges.pop(key):
                self.G.add_edge(u, v, **attrs)
            print(
                f"FAULT_EVENT ts={time.time():.3f} state=up "
                f"sw={sw} nbr={nbr}",
                flush=True,
            )