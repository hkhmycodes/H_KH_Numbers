#!/usr/bin/env python3
"""
Comparative analysis for the Khayou-number paper (corrected).

This version:
  * Implements H_G = S_G + alpha * T_G exactly as defined in the paper
    (SCC condensation, DAG dynamic programming, NO per-metric normalisation).
  * Evaluates each metric on TWO damage models:
      - "topological":  classical max-flow after node removal.  Favours
                        structural centralities (PageRank, betweenness) by
                        design and is reported for completeness.
      - "operational":  load-aware effective throughput after node removal
                        with per-node congestion penalties.  This is the
                        benchmark H_G is designed for.
  * Ablation S_G vs H_G is run on BOTH damage models.
  * Optional alpha-sweep exposes the structural/operational trade-off.
  * Real-network GSCC dismantling is unchanged (purely structural).

Usage
  python khayou_comparison.py --smoke
  python khayou_comparison.py --n-graphs 50 --alpha-sweep
  python khayou_comparison.py --real net1.txt net2.txt net3.txt

Requires: numpy, scipy, networkx, matplotlib
"""
import argparse
import csv
import warnings
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import networkx as nx
import numpy as np
import scipy.sparse as sp
from scipy.sparse.csgraph import connected_components
from scipy.sparse.linalg import lsqr
from scipy.stats import rankdata, spearmanr

trapz = getattr(np, "trapezoid", None) or np.trapz
SRC = -1                                    # super-source id for max-flow
ROOT_DEFAULT = 0

SYN_METHODS   = ["H_G", "S_G", "PageRank", "Betweenness"]
REAL_METHODS  = ["S_G", "PageRank", "Betweenness", "TAD"]


# --------------------------------------------------------------------------- #
# Structural scores
# --------------------------------------------------------------------------- #
def strahler_scc(G):
    """Horton-Strahler order on the SCC condensation (paper Def. 5.1)."""
    C = nx.condensation(G)
    order = {}
    for c in nx.topological_sort(C):
        preds = [order[p] for p in C.predecessors(c)]
        if not preds:
            order[c] = 1
        else:
            m = max(preds)
            order[c] = m + 1 if preds.count(m) >= 2 else m
    mapping = C.graph["mapping"]
    return {v: float(order[mapping[v]]) for v in G}


def _primary_path_edges(G, v, root):
    try:
        path = nx.shortest_path(G, v, root)
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        return []
    return list(zip(path[:-1], path[1:]))


def local_stress(G, beta=1.0, root=ROOT_DEFAULT):
    """g(u) = q(u) * (1 - beta * r_E(u)),  q = load/cap, r_E = b/(1+b)."""
    g = {}
    for v in G:
        if v == root:
            g[v] = 0.0
            continue
        load = float(G.nodes[v].get("load", 1.0))
        cap  = float(G.nodes[v].get("capacity", 1.0))
        q    = load / cap if cap > 0 else 0.0
        P    = _primary_path_edges(G, v, root)
        H    = G.copy()
        H.remove_edges_from(P)
        try:
            b = int(nx.edge_connectivity(H, v, root))
            b = max(0, b)
        except (nx.NetworkXError, nx.NetworkXNoPath, nx.NodeNotFound):
            b = 0
        r = b / (1.0 + b)
        g[v] = q * (1.0 - beta * r)
    return g


def cumulative_stress(G, g, root=ROOT_DEFAULT):
    """T_G(C) = max_{u in C} g(u) + max_{D in Pred(C)} T_G(D)  on the DAG."""
    C = nx.condensation(G)
    mapping = C.graph["mapping"]
    gC = {}
    for c in C:
        members = C.nodes[c].get("members", [c])
        gC[c] = max((g[v] for v in members), default=0.0)
    T = {}
    for c in nx.topological_sort(C):
        preds = list(C.predecessors(c))
        T[c] = gC[c] + (max(T[p] for p in preds) if preds else 0.0)
    return {v: T[mapping[v]] for v in G}


def h_score(G, alpha, S=None, beta=1.0, root=ROOT_DEFAULT):
    """H_G(u) = S_G(u) + alpha * T_G(u)   (paper Def. 8.1) -- no normalisation."""
    S = strahler_scc(G) if S is None else S
    g = local_stress(G, beta=beta, root=root)
    T = cumulative_stress(G, g, root=root)
    return {v: S[v] + alpha * T[v] for v in G}


# --------------------------------------------------------------------------- #
# Baselines
# --------------------------------------------------------------------------- #
def trophic_levels(G):
    nodes = list(G.nodes)
    idx = {v: i for i, v in enumerate(nodes)}
    n = len(nodes)
    r = [idx[u] for u, _ in G.edges]
    c = [idx[v] for _, v in G.edges]
    A = sp.csr_matrix((np.ones(len(r)), (r, c)), shape=(n, n))
    kin  = np.asarray(A.sum(0)).ravel()
    kout = np.asarray(A.sum(1)).ravel()
    L = (sp.diags(kin + kout) - A - A.T).tocsr()
    b = kin - kout
    h = np.zeros(n)
    ncomp, lab = connected_components(A, directed=False)
    for comp in range(ncomp):
        m = np.where(lab == comp)[0]
        if len(m) < 2:
            continue
        x = lsqr(L[m][:, m], b[m], atol=1e-12, btol=1e-12, iter_lim=50000)[0]
        h[m] = x - x.min()
    return {v: float(h[idx[v]]) for v in nodes}


def tad_scores(G, h=None):
    h = trophic_levels(G) if h is None else h
    F = dict.fromkeys(G, 0.0)
    for i, j in G.edges:
        d = h[j] - h[i]
        if d < 0:
            w = (abs(d) + 1.0) ** 2
            F[i] += w
            F[j] += w
    return F


def baseline_scores(G, bc_k=None, seed=0):
    k = None if bc_k is None or bc_k >= len(G) else bc_k
    return {
        "S_G":         strahler_scc(G),
        "PageRank":    nx.pagerank(G),
        "Betweenness": nx.betweenness_centrality(G, k=k, seed=seed),
    }


def order_from_scores(scores, rng, exclude=()):
    nodes = [v for v in scores if v not in exclude]
    s = np.array([scores[v] for v in nodes], dtype=float)
    idx = np.lexsort((rng.random(len(nodes)), -s))
    return [nodes[i] for i in idx]


# --------------------------------------------------------------------------- #
# Damage models
# --------------------------------------------------------------------------- #
def delivered(G, removed=(), root=ROOT_DEFAULT):
    """Classical max-flow delivered to root after node failures (edge caps)."""
    gone = set(removed)
    if root in gone:
        return 0.0
    D = nx.DiGraph()
    for u, v, d in G.edges(data=True):
        if u not in gone and v not in gone:
            D.add_edge(u, v, capacity=float(d.get("capacity", 1.0)))
    for v, data in G.nodes(data=True):
        if v == root or v in gone:
            continue
        demand = float(data.get("load", 0.0))
        if demand <= 0:
            continue
        dv = ("demand", v)
        D.add_edge(SRC, dv, capacity=demand)
        D.add_edge(dv, v, capacity=demand)
    if SRC not in D or root not in D:
        return 0.0
    return nx.maximum_flow_value(D, SRC, root)


def operational_damage(G, removed=(), root=ROOT_DEFAULT, alpha_cong=1.0):
    """Load-aware effective throughput after node removal.

    Each surviving source routes its full demand along a shortest surviving
    path to root.  Every node's contribution is its delivered demand scaled
    by a per-node congestion penalty 1 / (1 + alpha_cong * max(0, u - 1))
    with u = routed/cap.  Higher u -> lower effective throughput.
    """
    gone = set(removed)
    if root in gone:
        return 0.0
    alive = [v for v in G if v not in gone]
    if root not in alive:
        return 0.0
    H = G.subgraph(alive)

    routed = {v: 0.0 for v in alive}
    src_paths = {}
    for s in alive:
        if s == root:
            continue
        dem = float(G.nodes[s].get("load", 1.0))
        if dem <= 0:
            continue
        try:
            path = nx.shortest_path(H, s, root)
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            continue
        src_paths[s] = path
        for v in path[1:]:                 # don't charge the source itself
            routed[v] += dem

    pen = {}
    for v in alive:
        if v == root:
            continue
        cap = float(G.nodes[v].get("capacity", 1.0))
        u   = routed[v] / cap if cap > 0 else 1.0
        pen[v] = 1.0 / (1.0 + alpha_cong * max(0.0, u - 1.0))

    eff = 0.0
    for s, path in src_paths.items():
        dem = float(G.nodes[s].get("load", 1.0))
        worst = min((pen.get(v, 1.0) for v in path[1:]), default=1.0)
        eff += dem * worst
    return eff


def functional_curve(G, order, k_max, root=ROOT_DEFAULT, model="topological", **kw):
    """Q(k) = damage metric after removing top-k nodes / metric on intact graph."""
    if model == "topological":
        fn, kw_fn = delivered, {"root": root}
    else:
        fn, kw_fn = operational_damage, {"root": root, **kw}

    base = fn(G, (), **kw_fn)
    if base <= 0:
        base = 1.0
    Q = [1.0]
    for k in range(1, k_max + 1):
        Q.append(fn(G, order[:k], **kw_fn) / base)
    x = np.arange(k_max + 1) / len(G)
    return x, np.array(Q)


def functional_auc(x, Q):
    return float(trapz(Q, x) / x[-1])


# --------------------------------------------------------------------------- #
# Synthetic networks
# --------------------------------------------------------------------------- #
def make_network(n, sigma, headroom, rng, window=12, p_extra=0.5, p_cycle=0.2):
    """Directed network, edges point toward root 0.  Node- and edge-level attributes."""
    G = nx.DiGraph()
    G.add_nodes_from(range(n))
    for v in range(1, n):
        G.add_edge(v, int(rng.integers(max(0, v - window), v)))
    for v in range(2, n):
        if rng.random() < p_extra:
            G.add_edge(v, int(rng.integers(max(0, v - window), v)))
    for v in range(1, n):
        if rng.random() < p_cycle:
            down = sorted(nx.descendants(G, v) - {0})
            if down:
                G.add_edge(int(rng.choice(down)), v)
    # Node-level load and capacity (paper's model)
    for v in G:
        G.nodes[v]["load"]     = 0.0 if v == 0 else float(np.exp(rng.normal(0, sigma)))
        G.nodes[v]["capacity"] = float(np.exp(rng.normal(0, sigma)))
    # Edge capacities (topological benchmark)
    for u, v in G.edges:
        G[u][v]["capacity"] = float(np.exp(rng.normal(0, sigma)))
    # Scale edge capacities to give root inflow headroom
    tot_load = sum(G.nodes[v]["load"] for v in G)
    in_cap   = sum(d["capacity"] for _, _, d in G.in_edges(0, data=True))
    scale    = headroom * tot_load / in_cap if in_cap > 0 else 1.0
    for u, v in G.edges:
        G[u][v]["capacity"] *= scale
    return G


def run_synthetic(args, rng):
    grid   = np.linspace(0, args.max_frac, 51)
    methods = SYN_METHODS + ["Random"]
    # two benchmarks
    curves = {"topological": {m: [] for m in methods},
              "operational": {m: [] for m in methods}}
    aucs   = {"topological": {m: [] for m in methods},
              "operational": {m: [] for m in methods}}
    rhos_t = {m: [] for m in SYN_METHODS}   # Spearman vs topological single-node impact
    rhos_o = {m: [] for m in SYN_METHODS}   # Spearman vs operational single-node impact
    scatter = {m: [] for m in SYN_METHODS}

    for g in range(args.n_graphs):
        n = int(rng.integers(args.n_min, args.n_max + 1))
        G = make_network(n, args.sigma, args.headroom, rng)
        scores = baseline_scores(G, seed=int(rng.integers(1 << 30)))
        scores["H_G"] = h_score(G, args.alpha, S=scores["S_G"])
        cand = [v for v in G if v != 0]

        base_t = delivered(G)
        base_o = operational_damage(G)
        impact_t = {v: 1 - delivered(G, [v]) / (base_t if base_t > 0 else 1.0)
                    for v in cand}
        impact_o = {v: 1 - operational_damage(G, [v]) / (base_o if base_o > 0 else 1.0)
                    for v in cand}

        k_max = max(1, int(round(args.max_frac * n)))
        orders = {m: order_from_scores(scores[m], rng, exclude={0}) for m in SYN_METHODS}
        orders["Random"] = list(rng.permutation(cand))

        for bench, fn in (("topological", functional_curve), ("operational", functional_curve)):
            kw = {} if bench == "topological" else {"alpha_cong": args.alpha_cong}
            for m in methods:
                x, Q = functional_curve(G, orders[m], k_max, model=bench, **kw)
                curves[bench][m].append(np.interp(grid, x, Q))
                aucs[bench][m].append(functional_auc(x, Q))

        for m in SYN_METHODS:
            s = np.array([scores[m][v] for v in cand])
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                rhos_t[m].append(spearmanr(s, np.array([impact_t[v] for v in cand]))[0])
                rhos_o[m].append(spearmanr(s, np.array([impact_o[v] for v in cand]))[0])
            scatter[m].append(np.c_[rankdata(s) / len(s), [impact_o[v] for v in cand]])

        print(f"  synthetic graph {g + 1}/{args.n_graphs} (n={n})", end="\r")
    print()
    return dict(grid=grid, curves=curves, aucs=aucs,
                rhos_top=rhos_t, rhos_op=rhos_o, scatter=scatter)


def run_ablation(args, rng):
    """S_G vs H_G on BOTH damage models across matched heterogeneous topologies."""
    rows = []
    for sigma in args.ablation_sigmas:
        aS_t, aH_t, aS_o, aH_o = [], [], [], []
        for g in range(args.n_graphs):
            local_rng = np.random.default_rng(args.seed + 1000 * g)
            n = int(local_rng.integers(args.n_min, args.n_max + 1))
            G = make_network(n, sigma, args.headroom, local_rng)
            S = strahler_scc(G)
            H = h_score(G, args.alpha, S=S)
            k_max = max(1, int(round(args.max_frac * n)))
            for sc, storeS, storeH in ((S, aS_t, aH_t), (H, None, None)):
                pass  # placeholder to keep linters quiet
            for sc, storeS, storeH in [(S, aS_t, aH_t), (H, None, None)]:
                pass
            for sc, storeS, storeH in ((S, aS_t, aH_t),):
                x, Q = functional_curve(G, order_from_scores(S, local_rng, {0}),
                                        k_max, model="topological")
                storeS.append(functional_auc(x, Q))
            for sc, storeS, storeH in ((H, aS_t, aH_t),):
                x, Q = functional_curve(G, order_from_scores(H, local_rng, {0}),
                                        k_max, model="topological")
                storeH.append(functional_auc(x, Q))
            x, Q = functional_curve(G, order_from_scores(S, local_rng, {0}),
                                    k_max, model="operational",
                                    alpha_cong=args.alpha_cong)
            aS_o.append(functional_auc(x, Q))
            x, Q = functional_curve(G, order_from_scores(H, local_rng, {0}),
                                    k_max, model="operational",
                                    alpha_cong=args.alpha_cong)
            aH_o.append(functional_auc(x, Q))

        d_t = np.array(aS_t) - np.array(aH_t)
        d_o = np.array(aS_o) - np.array(aH_o)
        rows.append(dict(
            sigma=sigma,
            auc_S_top=float(np.mean(aS_t)), auc_H_top=float(np.mean(aH_t)),
            diff_top=float(d_t.mean()),
            diff_top_sem=float(d_t.std(ddof=1) / np.sqrt(len(d_t))),
            auc_S_op=float(np.mean(aS_o)),  auc_H_op=float(np.mean(aH_o)),
            diff_op=float(d_o.mean()),
            diff_op_sem=float(d_o.std(ddof=1) / np.sqrt(len(d_o))),
        ))
        print(f"  ablation sigma={sigma}: "
              f"topo S={rows[-1]['auc_S_top']:.3f} H={rows[-1]['auc_H_top']:.3f} | "
              f"op   S={rows[-1]['auc_S_op']:.3f} H={rows[-1]['auc_H_op']:.3f}")
    return rows


def run_alpha_sweep(args, rng):
    """Sweep alpha in H_G = S_G + alpha*T_G; report AUC on both benchmarks."""
    alphas = args.alpha_grid
    topo_auc = {a: [] for a in alphas}
    op_auc   = {a: [] for a in alphas}
    for g in range(args.n_graphs):
        local_rng = np.random.default_rng(args.seed + 5000 * g)
        n = int(local_rng.integers(args.n_min, args.n_max + 1))
        G = make_network(n, args.sigma, args.headroom, local_rng)
        S = strahler_scc(G)
        k_max = max(1, int(round(args.max_frac * n)))
        for a in alphas:
            H = h_score(G, a, S=S)
            order = order_from_scores(H, local_rng, {0})
            x, Q = functional_curve(G, order, k_max, model="topological")
            topo_auc[a].append(functional_auc(x, Q))
            x, Q = functional_curve(G, order, k_max, model="operational",
                                    alpha_cong=args.alpha_cong)
            op_auc[a].append(functional_auc(x, Q))
    return {a: (float(np.mean(topo_auc[a])), float(np.mean(op_auc[a]))) for a in alphas}


# --------------------------------------------------------------------------- #
# Real networks (structural GSCC dismantling)
# --------------------------------------------------------------------------- #
def load_edgelist(path):
    G = nx.DiGraph()
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line[0] in "#%":
                continue
            p = line.replace(",", " ").split()
            if len(p) >= 2 and p[0] != p[1]:
                G.add_edge(p[0], p[1])
    return G


def _gscc_size(A):
    if A.shape[0] == 0:
        return 0
    _, lab = connected_components(A, directed=True, connection="strong")
    return int(np.bincount(lab).max())


def gscc_curve(G, order, n_points=200):
    nodes = list(G.nodes)
    idx = {v: i for i, v in enumerate(nodes)}
    N = len(nodes)
    A = nx.to_scipy_sparse_array(G, nodelist=nodes, weight=None, format="csr")
    ord_idx = np.array([idx[v] for v in order])
    step = max(1, N // n_points)
    mask = np.ones(N, bool)
    ks, fs = [0], [_gscc_size(A) / N]
    k, auc = 0, 0.0
    while k < N:
        kk = min(N, k + step)
        mask[ord_idx[k:kk]] = False
        size = _gscc_size(A[mask][:, mask])
        auc += (size / N) * (kk - k) / N
        k = kk
        ks.append(k)
        fs.append(size / N)
        if size <= 1:
            break
    return np.array(ks) / N, np.array(fs), auc


def run_real(nets, args, rng):
    out = {}
    for name, G in nets.items():
        G = nx.DiGraph(G)
        G.remove_edges_from(nx.selfloop_edges(G))
        N = len(G)
        print(f"  {name}: N={N}, L={G.number_of_edges()}")
        bc_k = args.bc_k if N > args.bc_exact_max else None
        scores = baseline_scores(G, bc_k=bc_k, seed=args.seed)
        scores["TAD"] = tad_scores(G, h=trophic_levels(G))
        out[name] = {}
        for m in REAL_METHODS:
            x, f, auc = gscc_curve(G, order_from_scores(scores[m], rng))
            out[name][m] = dict(x=x, f=f, auc=auc)
    return out


# --------------------------------------------------------------------------- #
# Figures + tables
# --------------------------------------------------------------------------- #
STYLE = {"H_G": ("C3", "-"), "S_G": ("C1", "-"), "PageRank": ("C0", "-"),
         "Betweenness": ("C2", "-"), "TAD": ("k", "-"), "Random": ("0.6", "--")}


def save_fig(fig, outdir, stem):
    fig.tight_layout()
    fig.savefig(outdir / f"{stem}.pdf")
    fig.savefig(outdir / f"{stem}.png", dpi=200)
    plt.close(fig)


def fig_functional(syn, outdir):
    fig, axes = plt.subplots(1, 2, figsize=(11, 3.8), sharey=True)
    titles = {"topological": "(a) Topological benchmark",
              "operational": "(b) Load-aware operational benchmark"}
    for ax, bench in zip(axes, ("topological", "operational")):
        for m in SYN_METHODS + ["Random"]:
            C = np.array(syn["curves"][bench][m])
            mu, se = C.mean(0), C.std(0, ddof=1) / np.sqrt(len(C))
            col, ls = STYLE[m]
            ax.plot(syn["grid"], mu, color=col, ls=ls,
                    lw=1.8 if m == "H_G" else 1.2,
                    label=m.replace("_G", "$_G$"))
            ax.fill_between(syn["grid"], mu - se, mu + se, color=col, alpha=0.12, lw=0)
        ax.set_xlabel("Fraction of nodes removed")
        ax.set_title(titles[bench], fontsize=10)
        ax.set_ylim(0, 1.02)
    axes[0].set_ylabel("Remaining capacity $Q$")
    axes[1].legend(frameon=False, fontsize=8)
    save_fig(fig, outdir, "fig1_functional_degradation")


def fig_score_vs_impact(syn, outdir):
    fig, axes = plt.subplots(1, len(SYN_METHODS), figsize=(11, 2.9), sharey=True)
    for ax, m in zip(axes, SYN_METHODS):
        P = np.vstack(syn["scatter"][m])
        ax.scatter(P[:, 0], P[:, 1], s=5, alpha=0.25, color=STYLE[m][0], lw=0)
        rho = np.nanmean(syn["rhos_op"][m])
        ax.set_title(f"{m.replace('_G', '$_G$')}  ($\\rho_o$={rho:.2f})", fontsize=9)
        ax.set_xlabel("Score percentile (within graph)")
    axes[0].set_ylabel("Single-node operational impact")
    save_fig(fig, outdir, "fig2_score_vs_impact")


def fig_real(real, outdir):
    n = len(real)
    fig, axes = plt.subplots(1, n, figsize=(3.6 * n, 3.2), squeeze=False)
    for ax, (name, res) in zip(axes[0], real.items()):
        for m in REAL_METHODS:
            col, ls = STYLE[m]
            ax.plot(res[m]["x"], res[m]["f"], color=col, ls=ls,
                    lw=1.8 if m == "S_G" else 1.2, label=m.replace("_G", "$_G$"))
        ax.set_title(name, fontsize=9)
        ax.set_xlabel("Fraction of nodes removed")
    axes[0][0].set_ylabel("$|GSCC_{max}|/|V|$")
    axes[0][-1].legend(frameon=False, fontsize=8)
    save_fig(fig, outdir, "fig3_real_gscc_dismantling")


def fig_alpha_sweep(sweep, outdir):
    alphas = sorted(sweep)
    topo = [sweep[a][0] for a in alphas]
    op   = [sweep[a][1] for a in alphas]
    fig, ax1 = plt.subplots(figsize=(5.2, 3.4))
    ax1.plot(alphas, topo, "o-", color="C0", label="topological AUC")
    ax1.plot(alphas, op,   "s-", color="C3", label="operational AUC")
    ax1.set_xlabel(r"$\alpha$")
    ax1.set_ylabel("Functional AUC")
    ax1.legend(frameon=False, fontsize=8)
    ax1.set_title(r"$H_G=S_G+\alpha\,T_G$ vs. both benchmarks", fontsize=10)
    save_fig(fig, outdir, "fig4_alpha_sweep")


def write_tables(syn, real, abl, sweep, outdir):
    def ms(a):
        a = np.asarray(a, float)
        a = a[~np.isnan(a)]
        if len(a) == 0:
            return (np.nan, np.nan)
        return (a.mean(), a.std(ddof=1) if len(a) > 1 else np.nan)

    def f(v):
        if v is None:
            return "--"
        return f"{v[0]:.3f}" if np.isnan(v[1]) else f"{v[0]:.3f} $\\pm$ {v[1]:.3f}"

    # Main comparison table: two AUC columns
    rows = []
    for m in ["H_G", "S_G", "PageRank", "Betweenness"]:
        rho_t = ms(syn["rhos_top"][m])
        rho_o = ms(syn["rhos_op"][m])
        auc_t = ms(syn["aucs"]["topological"][m])
        auc_o = ms(syn["aucs"]["operational"][m])
        rows.append((m, rho_t, rho_o, auc_t, auc_o))

    with open(outdir / "table_comparison.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["Method", "rho_top", "rho_op", "AUC_top", "AUC_op"])
        for m, rt, ro, at, ao in rows:
            w.writerow([m, f(rt), f(ro), f(at), f(ao)])

    tex = ["% Requires \\usepackage[utf8]{inputenc}",
           "\\begin{tabular}{lcccc}", "\\toprule",
           "Method & $\\rho_{\\mathrm{top}}$ & $\\rho_{\\mathrm{op}}$ & "
           "Topological AUC & Operational AUC \\\\", "\\midrule"]
    for m, rt, ro, at, ao in rows:
        name = m.replace("_G", "$_G$")
        tex.append(f"{name} & " + " & ".join(f(v) for v in (rt, ro, at, ao)) + " \\\\")
    tex += ["\\bottomrule", "\\end{tabular}"]
    (outdir / "table_comparison.tex").write_text("\n".join(tex))

    # Ablation
    with open(outdir / "table_ablation.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(abl[0].keys()))
        w.writeheader(); w.writerows(abl)

    # Alpha sweep
    with open(outdir / "table_alpha_sweep.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["alpha", "AUC_topological", "AUC_operational"])
        for a in sorted(sweep):
            w.writerow([a, f"{sweep[a][0]:.4f}", f"{sweep[a][1]:.4f}"])

    # Real networks
    if real:
        with open(outdir / "real_auc_per_network.csv", "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["network"] + REAL_METHODS)
            for n, res in real.items():
                w.writerow([n] + [f"{res[m]['auc']:.4f}" for m in REAL_METHODS])

    # Console summary
    print("\n" + "Method".ljust(14) + "rho_top".ljust(16) + "rho_op".ljust(16)
          + "AUC_top".ljust(16) + "AUC_op")
    for m, rt, ro, at, ao in rows:
        print(m.ljust(14) + f(rt).ljust(16) + f(ro).ljust(16)
              + f(at).ljust(16) + f(ao))


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n-graphs", type=int, default=30)
    ap.add_argument("--n-min", type=int, default=30)
    ap.add_argument("--n-max", type=int, default=100)
    ap.add_argument("--alpha", type=float, default=1.0,
                    help="weight of T_G in H_G = S_G + alpha*T_G")
    ap.add_argument("--beta", type=float, default=1.0,
                    help="resilience attenuation factor")
    ap.add_argument("--alpha-cong", type=float, default=1.0,
                    help="congestion penalty strength in operational damage")
    ap.add_argument("--sigma", type=float, default=1.0,
                    help="lognormal sigma for node loads/capacities")
    ap.add_argument("--headroom", type=float, default=1.5,
                    help="root inflow edge capacity / total load")
    ap.add_argument("--max-frac", type=float, default=0.5,
                    help="max fraction of nodes removed")
    ap.add_argument("--ablation-sigmas", type=float, nargs="+",
                    default=[0.0, 0.5, 1.0, 1.5])
    ap.add_argument("--alpha-grid", type=float, nargs="+",
                    default=[0.0, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0])
    ap.add_argument("--alpha-sweep", action="store_true",
                    help="run the alpha sweep (adds Fig. 4)")
    ap.add_argument("--real", nargs="*", default=[],
                    help="edge-list files for GSCC dismantling")
    ap.add_argument("--bc-k", type=int, default=300)
    ap.add_argument("--bc-exact-max", type=int, default=1500)
    ap.add_argument("--outdir", default="results")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--smoke", action="store_true")
    args = ap.parse_args()

    if args.smoke:
        args.n_graphs, args.n_min, args.n_max = 3, 30, 40
        args.ablation_sigmas = [0.0, 1.0]
        args.alpha_grid = [0.0, 1.0, 5.0]

    rng = np.random.default_rng(args.seed)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    print("[1/4] synthetic benchmark (topological + operational)")
    syn = run_synthetic(args, rng)

    print("[2/4] ablation S_G vs H_G (both benchmarks)")
    abl = run_ablation(args, rng)

    sweep = {}
    if args.alpha_sweep or args.smoke:
        print("[3/4] alpha sweep")
        sweep = run_alpha_sweep(args, rng)
    else:
        print("[3/4] alpha sweep skipped (--alpha-sweep to enable)")

    print("[4/4] real networks")
    nets = {Path(p).stem: load_edgelist(p) for p in args.real}
    if args.smoke:
        sf = nx.scale_free_graph(300, seed=args.seed)
        nets["SMOKE-surrogate"] = nx.DiGraph(sf)
    real = run_real(nets, args, rng) if nets else {}
    if not real:
        print("  no --real files given: skipping Fig. 3 and structural-AUC column")

    fig_functional(syn, outdir)
    fig_score_vs_impact(syn, outdir)
    if real:
        fig_real(real, outdir)
    if sweep:
        fig_alpha_sweep(sweep, outdir)

    write_tables(syn, real, abl, sweep, outdir)
    print(f"\nOutputs written to {outdir.resolve()}")


if __name__ == "__main__":
    main()