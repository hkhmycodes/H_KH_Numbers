#!/usr/bin/env python3
"""
Mininet topology for the Khayou SDN emulation experiment.
"""

from mininet.topo import Topo


class KhayouSDNTopo(Topo):
    def build(self):
        # End hosts.
        u1 = self.addHost(
            "u1",
            ip="10.0.0.1",
            mac="00:00:00:00:00:01",
        )
        u2 = self.addHost(
            "u2",
            ip="10.0.0.2",
            mac="00:00:00:00:00:02",
        )
        u3 = self.addHost(
            "u3",
            ip="10.0.0.3",
            mac="00:00:00:00:03",
        )
        rho = self.addHost(
            "rho",
            ip="10.0.0.10",
            mac="00:00:00:00:00:10",
        )

        # OpenFlow 1.3 switches.
        for idx in range(1, 7):
            self.addSwitch(
                f"s{idx}",
                dpid=f"{idx:016x}",
                protocols="OpenFlow13",
            )

        # Host ingress.
        self.addLink(
            u1, "s1",
            bw=10, delay="2ms",
            max_queue_size=25,
            use_tbf=True,
            port1=1, port2=1,
        )
        self.addLink(
            u2, "s2",
            bw=10, delay="2ms",
            max_queue_size=25,
            use_tbf=True,
            port1=1, port2=1,
        )
        self.addLink(
            u3, "s3",
            bw=10, delay="2ms",
            max_queue_size=25,
            use_tbf=True,
            port1=1, port2=1,
        )

        # Aggregation.
        self.addLink(
            "s1", "s4",
            bw=15, delay="5ms",
            max_queue_size=20,
            use_tbf=True,
            port1=2, port2=1,
        )
        self.addLink(
            "s2", "s4",
            bw=15, delay="5ms",
            max_queue_size=20,
            use_tbf=True,
            port1=2, port2=2,
        )
        self.addLink(
            "s3", "s5",
            bw=15, delay="5ms",
            max_queue_size=20,
            use_tbf=True,
            port1=2, port2=1,
        )

        # Directed-cycle region in the analytical graph.
        # The Linux/OVS topology is physically bidirectional; the controller's
        # structural model represents both directions explicitly.
        self.addLink(
            "s4", "s5",
            bw=20, delay="1ms",
            max_queue_size=15,
            use_tbf=True,
            port1=3, port2=2,
        )

        # Core.
        self.addLink(
            "s4", "s6",
            bw=25, delay="3ms",
            max_queue_size=30,
            use_tbf=True,
            port1=4, port2=1,
        )
        self.addLink(
            "s5", "s6",
            bw=25, delay="3ms",
            max_queue_size=30,
            use_tbf=True,
            port1=3, port2=2,
        )

        # Primary collector link.
        self.addLink(
            "s6", rho,
            bw=10, delay="5ms",
            max_queue_size=15,
            use_tbf=True,
            port1=3, port2=1,
        )

        # Backup collector link.
        self.addLink(
            "s5", rho,
            bw=8, delay="4ms",
            max_queue_size=15,
            use_tbf=True,
            port1=4, port2=2,
        )


topos = {"khayou": KhayouSDNTopo}
