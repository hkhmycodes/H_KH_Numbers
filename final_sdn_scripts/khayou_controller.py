#!/usr/bin/env python3
"""
Khayou SDN controller for the Mininet validation experiment.

"""

import time

import networkx as nx

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import (
    CONFIG_DISPATCHER,
    MAIN_DISPATCHER,
    DEAD_DISPATCHER,
    set_ev_cls,
)
from ryu.ofproto import ofproto_v1_3
from ryu.lib import hub
from ryu.lib.packet import packet, ethernet, arp, ether_types

from khayou_telemetry import KhayouTelemetryMixin


class KhayouProductionController(KhayouTelemetryMixin, app_manager.RyuApp):
    """
    Mixin FIRST: MRO is
        KhayouProductionController
          -> KhayouTelemetryMixin
          -> RyuApp
          -> RyuApp base classes
    so the mixin's _compute_and_log_khayou_metrics and _port_status_handler
    shadow anything in RyuApp and the concrete class below.
    """
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    METRIC_INTERVAL = 3.0
    INITIAL_METRIC_DELAY = 5.0

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.datapaths = {}
        self.prev_port_stats = {}
        self._stopping = False
        self._closed = False
        self._controller_start_time = time.monotonic()
        self._metrics_logged_once = False
        self._last_disconnected_log = 0.0

        # Fixed experiment topology model.
        # Node attributes: c (service capacity), l (measured offered load).
        self.G = nx.DiGraph()
        self._setup_graph_skeleton()

        # Per-switch port-rate bookkeeping used to build the node loads.
        self._source_rates = {"u1": 0.1, "u2": 0.1, "u3": 0.1}
        self._core_rates = {"s4": 0.1, "s5": 0.1, "s6": 0.1}
        self._root_rate = 0.1

        # Start the controller-owned telemetry thread.
        self.monitor_thread = hub.spawn(self._metric_evaluation_loop)

        self.logger.info(
            "Khayou controller initialized: metric thread started "
            "(initial delay=%ss, interval=%ss).",
            self.INITIAL_METRIC_DELAY,
            self.METRIC_INTERVAL,
        )

    def _setup_graph_skeleton(self):
        nodes = ["u1", "u2", "u3", "s1", "s2", "s3", "s4", "s5", "s6", "rho"]

        # c is the analytical service capacity. l is measured offered load.
        for node in nodes:
            self.G.add_node(node, c=10.0, l=0.1)

        edges = [
            ("u1", "s1"),
            ("u2", "s2"),
            ("u3", "s3"),
            ("s1", "s4"),
            ("s2", "s4"),
            ("s3", "s5"),
            # Directed 2-cycle: SCC structure.
            ("s4", "s5"),
            ("s5", "s4"),
            ("s4", "s6"),
            ("s5", "s6"),
            ("s6", "rho"),
            # Backup collector link represented in the structural graph.
            ("s5", "rho"),
        ]
        self.G.add_edges_from(edges)

        # Emit the graph so scc_table.py uses the SAME edge set the controller
        # uses at runtime. Path is taken from KHAYOU_GRAPH_DUMP (set by
        # run_pipeline.sh) or defaults to results/graph_edges.json.
        self._dump_graph_edges()

    # ------------------------------------------------------------------
    # Datapath lifecycle
    # ------------------------------------------------------------------

    @set_ev_cls(
        ofp_event.EventOFPStateChange,
        [MAIN_DISPATCHER, DEAD_DISPATCHER],
    )
    def state_change_handler(self, ev):
        datapath = ev.datapath
        dpid = datapath.id

        if ev.state == MAIN_DISPATCHER:
            self.datapaths[dpid] = datapath
            self.logger.info("Datapath %016x connected.", dpid)

        elif ev.state == DEAD_DISPATCHER:
            if dpid in self.datapaths:
                self.logger.info(
                    "Datapath %016x disconnected (DEAD_DISPATCHER).",
                    dpid,
                )
                del self.datapaths[dpid]

            # Clear stale port-rate samples for this datapath.
            stale = [key for key in self.prev_port_stats if key[0] == dpid]
            for key in stale:
                self.prev_port_stats.pop(key, None)

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        dpid = datapath.id

        self.datapaths[dpid] = datapath

        # Table-miss -> controller, for ARP/control-plane handling.
        match = parser.OFPMatch()
        actions = [
            parser.OFPActionOutput(
                ofproto.OFPP_CONTROLLER,
                ofproto.OFPCML_NO_BUFFER,
            )
        ]
        self.add_flow(datapath, 0, match, actions)

        self._install_deterministic_routes(
            datapath, dpid, parser, ofproto
        )

        self.logger.info("Installed deterministic routes on datapath %d.", dpid)

    def add_flow(self, datapath, priority, match, actions):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        inst = [
            parser.OFPInstructionActions(
                ofproto.OFPIT_APPLY_ACTIONS,
                actions,
            )
        ]

        mod = parser.OFPFlowMod(
            datapath=datapath,
            priority=priority,
            match=match,
            instructions=inst,
        )
        datapath.send_msg(mod)

    def _install_deterministic_routes(self, datapath, dpid, parser, ofproto):
        # Forward direction: all source traffic goes toward rho.
        if dpid == 1:
            self.add_flow(
                datapath, 10,
                parser.OFPMatch(
                    eth_type=ether_types.ETH_TYPE_IP,
                    ipv4_dst="10.0.0.10",
                ),
                [parser.OFPActionOutput(2)],
            )
        elif dpid == 2:
            self.add_flow(
                datapath, 10,
                parser.OFPMatch(
                    eth_type=ether_types.ETH_TYPE_IP,
                    ipv4_dst="10.0.0.10",
                ),
                [parser.OFPActionOutput(2)],
            )
        elif dpid == 3:
            self.add_flow(
                datapath, 10,
                parser.OFPMatch(
                    eth_type=ether_types.ETH_TYPE_IP,
                    ipv4_dst="10.0.0.10",
                ),
                [parser.OFPActionOutput(2)],
            )
        elif dpid == 4:
            self.add_flow(
                datapath, 10,
                parser.OFPMatch(
                    eth_type=ether_types.ETH_TYPE_IP,
                    ipv4_dst="10.0.0.10",
                ),
                [parser.OFPActionOutput(4)],
            )
        elif dpid == 5:
            # Primary data path toward rho through s6.
            self.add_flow(
                datapath, 10,
                parser.OFPMatch(
                    eth_type=ether_types.ETH_TYPE_IP,
                    ipv4_dst="10.0.0.10",
                ),
                [parser.OFPActionOutput(3)],
            )
        elif dpid == 6:
            self.add_flow(
                datapath, 10,
                parser.OFPMatch(
                    eth_type=ether_types.ETH_TYPE_IP,
                    ipv4_dst="10.0.0.10",
                ),
                [parser.OFPActionOutput(3)],
            )

        # Reverse direction from rho toward the three sources.
        if dpid == 6:
            self.add_flow(
                datapath, 10,
                parser.OFPMatch(
                    eth_type=ether_types.ETH_TYPE_IP,
                    ipv4_dst="10.0.0.1",
                ),
                [parser.OFPActionOutput(1)],
            )
            self.add_flow(
                datapath, 10,
                parser.OFPMatch(
                    eth_type=ether_types.ETH_TYPE_IP,
                    ipv4_dst="10.0.0.2",
                ),
                [parser.OFPActionOutput(1)],
            )
            self.add_flow(
                datapath, 10,
                parser.OFPMatch(
                    eth_type=ether_types.ETH_TYPE_IP,
                    ipv4_dst="10.0.0.3",
                ),
                [parser.OFPActionOutput(2)],
            )
        elif dpid == 4:
            self.add_flow(
                datapath, 10,
                parser.OFPMatch(
                    eth_type=ether_types.ETH_TYPE_IP,
                    ipv4_dst="10.0.0.1",
                ),
                [parser.OFPActionOutput(1)],
            )
            self.add_flow(
                datapath, 10,
                parser.OFPMatch(
                    eth_type=ether_types.ETH_TYPE_IP,
                    ipv4_dst="10.0.0.2",
                ),
                [parser.OFPActionOutput(2)],
            )
        elif dpid == 5:
            self.add_flow(
                datapath, 10,
                parser.OFPMatch(
                    eth_type=ether_types.ETH_TYPE_IP,
                    ipv4_dst="10.0.0.3",
                ),
                [parser.OFPActionOutput(1)],
            )
        elif dpid == 1:
            self.add_flow(
                datapath, 10,
                parser.OFPMatch(
                    eth_type=ether_types.ETH_TYPE_IP,
                    ipv4_dst="10.0.0.1",
                ),
                [parser.OFPActionOutput(1)],
            )
        elif dpid == 2:
            self.add_flow(
                datapath, 10,
                parser.OFPMatch(
                    eth_type=ether_types.ETH_TYPE_IP,
                    ipv4_dst="10.0.0.2",
                ),
                [parser.OFPActionOutput(1)],
            )
        elif dpid == 3:
            self.add_flow(
                datapath, 10,
                parser.OFPMatch(
                    eth_type=ether_types.ETH_TYPE_IP,
                    ipv4_dst="10.0.0.3",
                ),
                [parser.OFPActionOutput(1)],
            )

    # ------------------------------------------------------------------
    # Bounded telemetry loop
    # ------------------------------------------------------------------
    #
    # NOTE: _compute_and_log_khayou_metrics is provided by
    # KhayouTelemetryMixin via khayou_metrics.compute_khayou. It reads
    # self.G, self._controller_start_time, and (if present) self._stopping,
    # and emits one 'METRIC_SAMPLE ts=... S_G=... T_G_b*... H_G_a*...'
    # line per cycle. Do not re-implement it here: the mixin's version
    # must stay authoritative, and any future change to the base method
    # must be ported into khayou_telemetry.py.
    #

    def _metric_evaluation_loop(self):
        try:
            # Bounded startup delay rather than sleeping forever.
            if self._stopping:
                return
            hub.sleep(self.INITIAL_METRIC_DELAY)

            while not self._stopping:
                if self.datapaths:
                    for datapath in list(self.datapaths.values()):
                        if self._stopping:
                            break
                        if getattr(datapath, "is_active", True):
                            self._request_stats(datapath)
                else:
                    # Avoid log spam while the network is absent.
                    now = time.monotonic()
                    if now - self._last_disconnected_log >= 10.0:
                        self.logger.info(
                            "No active datapaths; metric polling is idle."
                        )
                        self._last_disconnected_log = now

                if self._stopping:
                    break

                hub.sleep(self.METRIC_INTERVAL)

                if self._stopping:
                    break

                if self.datapaths:
                    # Dispatch to the mixin's override.
                    self._compute_and_log_khayou_metrics()

        except Exception:
            if not self._stopping:
                self.logger.exception("Telemetry loop terminated unexpectedly.")
        finally:
            self.logger.info("Khayou telemetry loop stopped.")

    def _request_stats(self, datapath):
        if self._stopping or not getattr(datapath, "is_active", True):
            return

        try:
            parser = datapath.ofproto_parser
            req = parser.OFPPortStatsRequest(
                datapath,
                0,
                datapath.ofproto.OFPP_ANY,
            )
            datapath.send_msg(req)
        except Exception as exc:
            if not self._stopping:
                self.logger.debug(
                    "Unable to request stats from dpid=%s: %s",
                    getattr(datapath, "id", "?"),
                    exc,
                )

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def port_stats_reply_handler(self, ev):
        if self._stopping:
            return

        body = ev.msg.body
        dpid = ev.msg.datapath.id
        current_time = time.monotonic()

        for stat in body:
            port_no = stat.port_no
            tx_bytes = stat.tx_bytes
            key = (dpid, port_no)

            if key in self.prev_port_stats:
                prev_bytes, prev_time = self.prev_port_stats[key]
                dt = current_time - prev_time

                if dt > 0:
                    byte_delta = max(0, tx_bytes - prev_bytes)
                    bit_rate_mbps = (
                        byte_delta * 8.0
                    ) / (dt * 1_000_000.0)
                    rate = max(0.1, bit_rate_mbps)

                    if dpid == 1 and port_no == 2:
                        self._source_rates["u1"] = rate
                        self.G.nodes["u1"]["l"] = rate

                    elif dpid == 2 and port_no == 2:
                        self._source_rates["u2"] = rate
                        self.G.nodes["u2"]["l"] = rate

                    elif dpid == 3 and port_no == 2:
                        self._source_rates["u3"] = rate
                        self.G.nodes["u3"]["l"] = rate

                    elif dpid == 4 and port_no == 4:
                        self._core_rates["s4"] = rate
                        self.G.nodes["s4"]["l"] = rate

                    elif dpid == 5 and port_no == 3:
                        self._core_rates["s5"] = rate
                        self.G.nodes["s5"]["l"] = rate

                    elif dpid == 6 and port_no == 3:
                        self._core_rates["s6"] = rate
                        self.G.nodes["s6"]["l"] = rate

                    # The root's observed offered load tracks the primary
                    # collector's transmit rate.
                    if dpid == 6 and port_no == 3:
                        self._root_rate = max(0.1, self._core_rates["s6"])
                        self.G.nodes["rho"]["l"] = self._root_rate

            self.prev_port_stats[key] = (tx_bytes, current_time)

    # ------------------------------------------------------------------
    # ARP handling
    # ------------------------------------------------------------------

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        if self._stopping:
            return

        msg = ev.msg
        datapath = msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        in_port = msg.match["in_port"]

        pkt = packet.Packet(msg.data)
        eth_list = pkt.get_protocols(ethernet.ethernet)
        if not eth_list:
            return

        eth = eth_list[0]

        if eth.ethertype != ether_types.ETH_TYPE_ARP:
            return

        arp_pkt = pkt.get_protocol(arp.arp)
        if not arp_pkt or arp_pkt.opcode != arp.ARP_REQUEST:
            return

        target_mac = {
            "10.0.0.1": "00:00:00:00:00:01",
            "10.0.0.2": "00:00:00:00:00:02",
            "10.0.0.3": "00:00:00:00:00:03",
            "10.0.0.10": "00:00:00:00:00:10",
        }.get(arp_pkt.dst_ip)

        if target_mac is None:
            return

        reply = packet.Packet()
        reply.add_protocol(
            ethernet.ethernet(
                ethertype=ether_types.ETH_TYPE_ARP,
                dst=eth.src,
                src=target_mac,
            )
        )
        reply.add_protocol(
            arp.arp(
                opcode=arp.ARP_REPLY,
                src_mac=target_mac,
                src_ip=arp_pkt.dst_ip,
                dst_mac=arp_pkt.src_mac,
                dst_ip=arp_pkt.src_ip,
            )
        )
        reply.serialize()

        actions = [parser.OFPActionOutput(in_port)]
        out = parser.OFPPacketOut(
            datapath=datapath,
            buffer_id=ofproto.OFP_NO_BUFFER,
            in_port=ofproto.OFPP_CONTROLLER,
            actions=actions,
            data=reply.data,
        )
        datapath.send_msg(out)

    # ------------------------------------------------------------------
    # Ryu lifecycle
    # ------------------------------------------------------------------

    def close(self):
        if self._closed:
            return

        self._closed = True
        self._stopping = True

        self.logger.info("Closing Khayou controller.")

        # Stop the controller-owned green thread immediately.
        thread = getattr(self, "monitor_thread", None)
        if thread is not None:
            try:
                hub.kill(thread)
            except Exception:
                pass
            try:
                thread.wait()
            except Exception:
                pass

        self.monitor_thread = None
        self.datapaths.clear()
        self.prev_port_stats.clear()

        self.logger.info("Khayou controller closed cleanly.")

        # Let Ryu complete its own application cleanup.
        try:
            super().close()
        except AttributeError:
            # Defensive for older/incompatible Ryu releases.
            pass


if __name__ == "__main__":
    # Ryu loads this module through ryu-manager; direct execution is not needed.
    print("Use: ryu-manager khayou_controller.py")