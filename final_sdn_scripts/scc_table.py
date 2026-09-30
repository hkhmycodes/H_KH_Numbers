#!/usr/bin/env python3
"""
Offline structural table for the four SDN topology configurations.
Writes results/scc_table.csv with one row per (topology, node, S_G).
"""
import argparse, csv, os
import networkx as nx

TOPOS = {
    "A_dag": [
        ("u1","s1"),("u2","s2"),("u3","s3"),
        ("s1","s4"),("s2","s4"),("s3","s5"),
        ("s4","s6"),("s5","s6"),
        ("s6","rho"),("s5","rho"),
    ],
    "B_2cycle": [
        ("u1","s1"),("u2","s2"),("u3","s3"),
        ("s1","s4"),("s2","s4"),("s3","s5"),
        ("s4","s5"),("s5","s4"),
        ("s4","s6"),("s5","s6"),
        ("s6","rho"),("s5","rho"),
    ],
    "C_3cycle": [
        ("u1","s1"),("u2","s2"),("u3","s3"),
        ("s1","s4"),("s2","s4"),("s3","s5"),
        ("s4","s5"),("s5","s6"),("s6","s4"),
        ("s6","rho"),("s5","rho"),
    ],
    "D_nested": [
        ("u1","s1"),("u2","s2"),("u3","s3"),
        ("s1","s4"),("s2","s4"),("s3","s5"),
        ("s4","s5"),("s5","s4"),
        ("s5","s6"),("s6","s5"),
        ("s4","s6"),
        ("s6","rho"),("s5","rho"),
    ],
}

NODES = ["u1","u2","u3","s1","s2","s3","s4","s5","s6","rho"]


def scc_table(edges):
    G = nx.DiGraph()
    G.add_nodes_from(NODES)
    G.add_edges_from(edges)
    cond = nx.condensation(G)
    topo = list(nx.topological_sort(cond))

    s_g = {}
    for c_id in topo:
        preds = list(cond.predecessors(c_id))
        if not preds:
            s_g[c_id] = 1
        else:
            orders = [s_g[p] for p in preds]
            m = max(orders)
            s_g[c_id] = m + 1 if orders.count(m) >= 2 else m

    rows = []
    for c_id in cond.nodes():
        members = sorted(cond.nodes[c_id]["members"])
        s_val = s_g[c_id]
        chi = _cycle_rank(G.subgraph(members))
        for node in members:
            rows.append((node, s_val, c_id, ",".join(members), chi))
    return rows


def _cycle_rank(sub):
    if len(sub) <= 1:
        return 0
    return sub.number_of_edges() - sub.number_of_nodes() + 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="results/scc_table.csv")
    args = ap.parse_args()
    os.makedirs(os.path.dirname(args.out), exist_ok=True)

    with open(args.out, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["topology", "node", "S_G", "component_id",
                    "component_members", "chi"])
        for name, edges in TOPOS.items():
            for row in scc_table(edges):
                w.writerow([name, *row])
    print(f"[+] wrote {args.out}")


if __name__ == "__main__":
    main()