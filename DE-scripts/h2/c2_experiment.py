import csv
import os
import networkx as nx
import numpy as np
import matplotlib.pyplot as plt

# Set professional plotting standards
plt.rcParams['font.family'] = 'sans-serif'
plt.rcParams['font.sans-serif'] = ['DejaVu Sans', 'Arial', 'Helvetica']


def compute_framework_metrics(G, root, primary_path, alpha=1.0, beta=1.0, q_u=0.75):
    """
    Computes structural order S_G, residual directed edge connectivity b,
    operational stress T_G, and the integrated metric H_G.
    """
    sccs = list(nx.strongly_connected_components(G))
    cond_dag = nx.condensation(G, scc=sccs)

    node_to_scc = {}
    for idx, scc in enumerate(sccs):
        for node in scc:
            node_to_scc[node] = idx

    s_g_comp = {}
    try:
        topo_order = list(nx.topological_sort(cond_dag))
    except nx.NetworkXUnfeasible:
        topo_order = list(cond_dag.nodes())

    for comp in topo_order:
        preds = list(cond_dag.predecessors(comp))
        if not preds:
            s_g_comp[comp] = 1.0
        else:
            max_pred_order = max(s_g_comp[p] for p in preds)
            count_max = sum(1 for p in preds if s_g_comp[p] == max_pred_order)
            s_g_comp[comp] = max_pred_order + 1 if count_max >= 2 else max_pred_order

    source_node = primary_path[0]
    comp_idx = node_to_scc[source_node]
    S_G_val = s_g_comp[comp_idx]

    # Directed Residual Edge Connectivity b(u; P_u)
    G_res = G.copy()
    primary_edges = list(zip(primary_path[:-1], primary_path[1:]))
    for edge in primary_edges:
        if G_res.has_edge(*edge):
            G_res.remove_edge(*edge)

    try:
        b_val = float(nx.edge_connectivity(G_res, source_node, root))
    except (nx.NetworkXError, nx.NetworkXUnfeasible):
        b_val = 0.0

    # Integrated metric H_G evaluated at source u_0
    r_E = b_val / (1.0 + b_val)
    g_u = q_u * (1.0 - beta * r_E)
    T_G_val = g_u  # source-node formulation, no offset
    H_G_val = S_G_val + alpha * T_G_val

    return S_G_val, b_val, H_G_val, r_E, g_u, T_G_val


def run_deterministic_h2_verification(alpha=1.0, beta=1.0, q_u=0.75):
    """
    Constructs a controlled topological scaffold where backup edge connectivity b
    is systematically swept from 0 to 4 while maintaining a fixed structural order S_G(u_0).
    """
    n_nodes = 10
    root = n_nodes - 1
    source = 0

    # Define a rigid primary path: 0 -> 1 -> 2 -> root
    primary_path = [0, 1, 2, root]

    # Pool of auxiliary nodes available to construct independent disjoint backup paths
    aux_nodes_pools = [
        [],                 # For b = 0 (no bypasses)
        [3],                # For b = 1
        [3, 4],             # For b = 2
        [3, 4, 5],          # For b = 3
        [3, 4, 5, 6]        # For b = 4
    ]

    rows = []

    for pool in aux_nodes_pools:
        # Build a base graph containing the primary path
        G = nx.DiGraph()
        for i in range(len(primary_path) - 1):
            G.add_edge(primary_path[i], primary_path[i + 1])

        # Add background nodes ensuring they connect to root to keep base S_G stable
        for n in range(n_nodes):
            if n not in primary_path and n not in pool:
                G.add_edge(n, root)

        # Construct independent auxiliary bypass paths from source to root via pool nodes
        for aux in pool:
            G.add_edge(source, aux)
            G.add_edge(aux, root)

        # Ensure overall connectivity
        for node in range(n_nodes):
            if not nx.has_path(G, node, root):
                G.add_edge(node, root)

        S_G, b_val, H_G, r_E, g_u, T_G_val = compute_framework_metrics(
            G, root, primary_path, alpha=alpha, beta=beta, q_u=q_u
        )

        rows.append({
            "b": int(b_val),
            "S_G": float(S_G),
            "q_u": float(q_u),
            "r_E": float(r_E),
            "g_u": float(g_u),
            "T_G": float(T_G_val),
            "alpha": float(alpha),
            "beta": float(beta),
            "H_G": float(H_G),
            "pool_size": len(pool),
        })

    b_variants = [r["b"] for r in rows]
    h_g_variants = [r["H_G"] for r in rows]
    s_g_variants = [r["S_G"] for r in rows]

    # Assertions confirming implementation correctness and strict monotonicity
    assert b_variants == [0, 1, 2, 3, 4], f"Unexpected b values: {b_variants}"
    assert all(np.diff(h_g_variants) < 0), "H_G values are not strictly monotonic decreasing!"

    return s_g_variants, b_variants, h_g_variants, rows


def plot_deterministic_verification():
    s_g, b_vals, h_g_vals, rows = run_deterministic_h2_verification()

    # --- Output paths ---
    output_dir = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else "."
    csv_path = os.path.join(output_dir, "hypothesis_h2_controlled_verification.csv")
    pdf_path = os.path.join(output_dir, "hypothesis_h2_controlled_verification.pdf")

    print("=== Controlled resilience sweep (consistency check C2) ===")
    for r in rows:
        print(
            f"S_G(u0)={r['S_G']:.4f} | b={r['b']:d} | r_E={r['r_E']:.6f} | "
            f"g(u0)={r['g_u']:.6f} | T_G(u0)={r['T_G']:.6f} | "
            f"H_G(u0)={r['H_G']:.12f}"
        )

    # --- CSV output ---
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "b", "S_G", "q_u", "r_E", "g_u", "T_G",
                "alpha", "beta", "H_G", "pool_size",
            ],
        )
        writer.writeheader()
        for r in rows:
            writer.writerow({
                "b": r["b"],
                "S_G": f"{r['S_G']:.6f}",
                "q_u": f"{r['q_u']:.6f}",
                "r_E": f"{r['r_E']:.12f}",
                "g_u": f"{r['g_u']:.12f}",
                "T_G": f"{r['T_G']:.12f}",
                "alpha": f"{r['alpha']:.6f}",
                "beta": f"{r['beta']:.6f}",
                "H_G": f"{r['H_G']:.12f}",
                "pool_size": r["pool_size"],
            })
    print(f"Wrote CSV: {csv_path}")

    # --- Publication-Grade Plotting (PDF) ---
    fig, ax = plt.subplots(figsize=(8, 5), dpi=300)

    ax.plot(
        b_vals, h_g_vals,
        marker='s', color='darkcyan', linewidth=2.2, markersize=8,
        label=r'Analytical metric ($H_G(u_0)$)',
    )

    ax.set_xlabel(r'Directed Backup Edge-Connectivity ($b$)',
                  fontsize=12, fontweight='bold')
    ax.set_ylabel(r'Generalized Metric $H_G(u_0)$',
                  fontsize=12, fontweight='bold')
    ax.set_title(
        r'Controlled resilience sweep (consistency check C2): '
        r'decreasing $H_G(u_0)$ with $b$ ($S_G(u_0) = 1$)',
        fontsize=12, pad=15, fontweight='bold',
    )

    ax.set_xticks([0, 1, 2, 3, 4])
    ax.grid(True, linestyle='--', alpha=0.6)
    ax.legend(loc='upper right', frameon=True, facecolor='white')

    plt.tight_layout()
    plt.savefig(pdf_path, format='pdf', bbox_inches='tight')
    plt.show()
    print(f"Figure saved as '{pdf_path}'.")


if __name__ == '__main__':
    plot_deterministic_verification()