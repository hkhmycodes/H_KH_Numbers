import networkx as nx
import numpy as np
import matplotlib.pyplot as plt

# Set professional plotting standards
plt.rcParams['font.family'] = 'sans-serif'
plt.rcParams['font.sans-serif'] = ['DejaVu Sans', 'Arial', 'Helvetica']

def compute_framework_metrics(G, root, primary_path, alpha=1.0, beta=1.0, q_u=0.75):
    """
    Computes structural order S_G, residual directed edge connectivity b, 
    operational stress T_G, and the integrated Khayou metric H_G.
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
        
    # Integrated Khayou Metric H_G evaluated at source u_0
    r_E = b_val / (1.0 + b_val)
    g_u = q_u * (1.0 - beta * r_E)
    T_G_val = g_u  # Removed + 0.5 offset to align with source node formulation
    H_G_val = S_G_val + alpha * T_G_val
    
    return S_G_val, b_val, H_G_val

def run_deterministic_h2_verification():
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
    
    b_variants = []
    h_g_variants = []
    s_g_variants = []
    
    for pool in aux_nodes_pools:
        # Build a base graph containing the primary path
        G = nx.DiGraph()
        for i in range(len(primary_path) - 1):
            G.add_edge(primary_path[i], primary_path[i+1])
            
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
                
        S_G, b_val, H_G = compute_framework_metrics(G, root, primary_path)
        
        b_variants.append(int(b_val))
        h_g_variants.append(H_G)
        s_g_variants.append(S_G)
        
    # Assertions confirming implementation correctness and strict monotonicity
    assert b_variants == [0, 1, 2, 3, 4], f"Unexpected b values: {b_variants}"
    assert all(np.diff(h_g_variants) < 0), "H_G values are not strictly monotonic decreasing!"
        
    return s_g_variants, b_variants, h_g_variants

def plot_deterministic_verification():
    s_g, b_vals, h_g_vals = run_deterministic_h2_verification()
    
    print("=== Controlled Sweep for H2: Consistency Check ===")
    for s, b, h in zip(s_g, b_vals, h_g_vals):
        print(f"Structural Order S_G(u_0) = {s} | Backup Connectivity b = {b} | Metric H_G(u_0) = {h:.4f}")
        
    fig, ax = plt.subplots(figsize=(8, 5), dpi=300)
    
    ax.plot(b_vals, h_g_vals, marker='s', color='darkcyan', linewidth=2.2, markersize=8,
            label='Analytical Metric ($H_G(u_0)$)')
            
    ax.set_xlabel('Directed Backup Edge-Connectivity ($b$)', fontsize=12, fontweight='bold')
    ax.set_ylabel('Integrated Khayou Metric ($H_G(u_0)$)', fontsize=12, fontweight='bold')
    ax.set_title('Controlled Sweep for H2: Decreasing $H_G(u_0)$ with $b$ ($S_G(u_0) = 1$)', fontsize=12, pad=15, fontweight='bold')
    
    ax.set_xticks([0, 1, 2, 3, 4])
    ax.grid(True, linestyle='--', alpha=0.6)
    ax.legend(loc='upper right', frameon=True, facecolor='white')
    
    plt.tight_layout()
    plt.savefig('hypothesis_h2_controlled_verification.png', dpi=300)
    plt.show()

if __name__ == '__main__':
    plot_deterministic_verification()