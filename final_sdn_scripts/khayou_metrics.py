#!/usr/bin/env python3
"""
Pure (Ryu-free) implementation of the Khayou structural/operational metric.

The controller calls compute_khayou(G) every telemetry cycle. Keeping it free
of Ryu/Mininet lets you unit-test it and regression-check it against your old
T_G values offline.

G: nx.DiGraph. Node attributes: c (capacity), l (measured load).
Definitions (as used in the experiment):
  q_u = l_u / c_u
  b_u = edge connectivity u -> root after removing the edges of one shortest path
  r_u = b_u / (1 + b_u)
  g_u(beta) = q_u * (1 - beta * r_u)
  T(component) = max_{u in component} g_u + max_{pred} T(pred)   (0 if no preds)
  S order: Horton-Strahler on the SCC condensation
  H_G = S_G + alpha * T_G(beta = 1)
Baselines (network-wide, NOT path-based): U_max_net, U_mean_net over non-root nodes.
T_G at beta = 0 is the path-aware utilization-only baseline.
"""
import networkx as nx

BETAS = (0.0, 0.5, 1.0)
ALPHAS = (0.0, 0.5, 1.0, 2.0)


def tag(x):
    """0 -> '0', 0.5 -> '05', 1.0 -> '1', 2.0 -> '2'"""
    return f"{x:g}".replace(".", "")


def structural_orders(cond):
    order = list(nx.topological_sort(cond))
    s = {}
    for c in order:
        preds = list(cond.predecessors(c))
        if not preds:
            s[c] = 1
        else:
            o = [s[p] for p in preds]
            m = max(o)
            s[c] = m + 1 if o.count(m) >= 2 else m
    return order, s


def residual_redundancy(G, node, root):
    if node == root or node not in G or root not in G:
        return 0
    try:
        path = nx.shortest_path(G, node, root)
        R = G.copy()
        R.remove_edges_from(zip(path[:-1], path[1:]))
        return nx.edge_connectivity(R, node, root)
    except (nx.NetworkXNoPath, nx.NetworkXError):
        return 0


def compute_khayou(G, root="rho", betas=BETAS, alphas=ALPHAS):
    cond = nx.condensation(G)
    order, s_map = structural_orders(cond)
    comp_of = {n: c for c in cond for n in cond.nodes[c]["members"]}

    q, r = {}, {}
    for n, d in G.nodes(data=True):
        cap = float(d.get("c", 0.0))
        q[n] = float(d.get("l", 0.0)) / cap if cap > 0 else 0.0
        b = residual_redundancy(G, n, root)
        r[n] = b / (1.0 + b)

    out = {"S_G": s_map[comp_of[root]]}
    t_beta = {}
    for beta in betas:
        g = {n: q[n] * (1.0 - beta * r[n]) for n in G.nodes()}
        t = {}
        for c in order:
            preds = list(cond.predecessors(c))
            g_c = max(g[n] for n in cond.nodes[c]["members"])
            t[c] = g_c + (max(t[p] for p in preds) if preds else 0.0)
        t_beta[beta] = t[comp_of[root]]
        out[f"T_G_b{tag(beta)}"] = t_beta[beta]

    t1 = t_beta.get(1.0, 0.0)
    for a in alphas:
        out[f"H_G_a{tag(a)}"] = out["S_G"] + a * t1

    others = [q[n] for n in G.nodes() if n != root]
    out["U_max_net"] = max(others) if others else 0.0
    out["U_mean_net"] = sum(others) / len(others) if others else 0.0
    return out


if __name__ == "__main__":
    # Self-test on the 10-node configuration B (2-cycle s4<->s5).
    G = nx.DiGraph()
    E = [("u1","s1"),("u2","s2"),("u3","s3"),("s1","s4"),("s2","s4"),("s3","s5"),
         ("s4","s5"),("s5","s4"),("s4","s6"),("s5","s6"),("s6","rho"),("s5","rho")]
    G.add_edges_from(E)
    for n in G:
        G.nodes[n]["c"], G.nodes[n]["l"] = 10.0, 0.0
    idle = compute_khayou(G)
    for n in ("u1", "u2", "u3", "s1", "s2", "s3", "s4", "s5", "s6"):
        G.nodes[n]["l"] = 9.0
    loaded = compute_khayou(G)
    print("idle  :", {k: round(v, 4) for k, v in idle.items()})
    print("loaded:", {k: round(v, 4) for k, v in loaded.items()})
    assert idle["S_G"] == loaded["S_G"] == 3
    assert loaded["T_G_b1"] > idle["T_G_b1"]
    assert loaded["T_G_b0"] >= loaded["T_G_b1"]
    print("self-test OK")
