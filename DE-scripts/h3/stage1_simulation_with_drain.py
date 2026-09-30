import random
import networkx as nx
import numpy as np
import scipy.stats as stats
import simpy

# ==========================================
# 1. ROOTED NETWORK GENERATION
# ==========================================
def generate_rooted_network(n_nodes=30, p_edge=0.15):
    G = nx.gnp_random_graph(n_nodes, p_edge, directed=True)
    while G.number_of_edges() == 0:
        G = nx.gnp_random_graph(n_nodes, p_edge, directed=True)
        
    sinks = [u for u in G.nodes() if G.out_degree(u) == 0]
    rho = sinks[0] if sinks else max(G.nodes(), key=lambda n: G.in_degree(n))
    
    for u in list(G.nodes()):
        if u != rho and not nx.has_path(G, u, rho):
            G.add_edge(u, rho)
            
    return G, rho

# ==========================================
# 2. SCC CONDENSATION & STRUCTURAL ORDER (S_G)
# ==========================================
def compute_structural_strahler(G):
    sccs = list(nx.strongly_connected_components(G))
    condensation = nx.condensation(G, scc=sccs)
    
    try:
        topo_order = list(nx.topological_sort(condensation))
    except nx.NetworkXUnfeasible:
        return None, None, None

    s_values = {}
    for c in topo_order:
        preds = list(condensation.predecessors(c))
        if not preds:
            s_values[c] = 1
        else:
            pred_orders = [s_values[p] for p in preds]
            max_m = max(pred_orders)
            count_max = pred_orders.count(max_m)
            s_values[c] = max_m + 1 if count_max >= 2 else max_m
            
    mapping = condensation.graph['mapping']
    vertex_s = {u: s_values[mapping[u]] for u in G.nodes()}
    
    return condensation, s_values, vertex_s

# ==========================================
# 3. OFFERED LOAD & KHAYOU METRIC (T_G & H_G)
# ==========================================
def compute_khayou_metric_unified(G, condensation, s_values, capacity, source_rates, rho, alpha=1.0, beta=0.0):
    mapping = condensation.graph['mapping']
    
    offered_load = {u: 0.0 for u in G.nodes()}
    source_paths = {}
    
    sources = [u for u in G.nodes() if u != rho and u in source_rates]
    for src in sources:
        try:
            path = nx.shortest_path(G, source=src, target=rho)
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            path = [src, rho] if rho in G[src] else [src]
        source_paths[src] = path
        
        rate = source_rates[src]
        for node in path:
            offered_load[node] += rate
            
    for u in G.nodes():
        if offered_load[u] == 0.0:
            offered_load[u] = 0.05
            
    utilization = {u: offered_load[u] / capacity[u] for u in G.nodes()}
    local_stress = {u: utilization[u] * (1.0 - beta) for u in G.nodes()}
    
    comp_nodes = {}
    for u, c_idx in mapping.items():
        comp_nodes.setdefault(c_idx, []).append(u)
        
    comp_g = {c: max(local_stress[u] for u in nodes) for c, nodes in comp_nodes.items()}
    
    topo_order = list(nx.topological_sort(condensation))
    t_values = {}
    for c in topo_order:
        preds = list(condensation.predecessors(c))
        if not preds:
            t_values[c] = comp_g[c]
        else:
            max_pred_t = max(t_values[p] for p in preds)
            t_values[c] = comp_g[c] + max_pred_t
            
    h_values = {}
    t_node_values = {}
    for u in G.nodes():
        c_idx = mapping[u]
        h_values[u] = s_values[c_idx] + alpha * t_values[c_idx]
        t_node_values[u] = t_values[c_idx]
        
    return h_values, t_node_values, local_stress, source_paths

# ==========================================
# 4. SIMPY QUEUEING WITH WARM-UP & DRAIN PHASE
# ==========================================
class SequentialNodeQueue:
    def __init__(self, env, name, capacity, max_buffer=40):
        self.env = env
        self.name = name
        self.capacity = max(capacity, 0.1)
        self.max_buffer = max_buffer
        self.store = simpy.Store(env, capacity=max_buffer)

def run_drain_simulation(G, rho, capacity, source_rates, source_paths, warmup_time=15.0, measurement_time=30.0):
    env = simpy.Environment()
    nodes = {u: SequentialNodeQueue(env, u, capacity[u], max_buffer=35) for u in G.nodes()}
    
    stats = {
        "N_generated": 0,
        "N_delivered": 0,
        "N_dropped": 0,
        "root_latencies": []
    }
    
    def node_server(u):
        node = nodes[u]
        while True:
            packet = yield node.store.get()
            
            service_time = random.expovariate(node.capacity)
            yield env.timeout(service_time)
            
            path = packet["path"]
            hop_idx = packet["hop_index"]
            
            if hop_idx + 1 < len(path):
                next_node = path[hop_idx + 1]
                packet["hop_index"] += 1
                if next_node in nodes:
                    next_store = nodes[next_node].store
                    if len(next_store.items) < next_store.capacity:
                        yield next_store.put(packet)
                    else:
                        if packet["is_measured"]:
                            stats["N_dropped"] += 1
            else:
                if packet["is_measured"]:
                    e2e_latency = env.now - packet["departure_time"]
                    stats["root_latencies"].append(e2e_latency)
                    stats["N_delivered"] += 1

    for u in G.nodes():
        env.process(node_server(u))
        
    def source_traffic_generator(src, rate, path):
        pkt_counter = 0
        stop_generation_time = warmup_time + measurement_time
        while env.now < stop_generation_time:
            yield env.timeout(random.expovariate(rate))
            
            is_measured = (env.now >= warmup_time)
            if is_measured:
                stats["N_generated"] += 1
                
            pkt_counter += 1
            packet = {
                "id": f"pkt_{src}_{pkt_counter}",
                "source": src,
                "departure_time": env.now,
                "path": path,
                "hop_index": 0,
                "is_measured": is_measured
            }
            
            if path and path[0] in nodes:
                first_store = nodes[path[0]].store
                if len(first_store.items) < first_store.capacity:
                    yield first_store.put(packet)
                else:
                    if is_measured:
                        stats["N_dropped"] += 1

    for src, rate in source_rates.items():
        if src in source_paths:
            env.process(source_traffic_generator(src, rate, source_paths[src]))
            
    # Run simulation through generation phase, then allow unconstrained drain until queues empty
    generation_end = warmup_time + measurement_time
    env.run(until=generation_end)
    
    # Drain phase: Continue running until no measured packets remain in any node buffer
    while any(len(nodes[u].store.items) > 0 for u in G.nodes()) and env.now < generation_end + 50.0:
        env.step()
        
    return stats

# ==========================================
# 5. MONTE-CARLO EXPERIMENT WITH BOOTSTRAP TEST
# ==========================================
def main():
    print("Executing Drain-Corrected H3 Monte-Carlo Simulation Sweep...")
    iterations = 100  # Scaled up for robustness
    
    results_s = []
    results_t = []
    results_h = []
    results_l95 = []
    results_ploss = []
    
    random.seed(303)
    np.random.seed(303)
    
    for i in range(iterations):
        G, rho = generate_rooted_network(n_nodes=25, p_edge=0.15)
        condensation, s_values, vertex_s = compute_structural_strahler(G)
        if condensation is None:
            continue
            
        capacity = {u: random.uniform(30.0, 70.0) for u in G.nodes()}
        
        sources = [u for u in G.nodes() if G.in_degree(u) == 0 and u != rho]
        if not sources:
            sources = [u for u in G.nodes() if u != rho][:4]
            if not sources:
                sources = [rho]
                
        source_rates = {src: random.uniform(5.0, 18.0) for src in sources}
        
        alpha = 1.0
        beta = 0.0
        
        h_values, t_values_map, local_stress, source_paths = compute_khayou_metric_unified(
            G, condensation, s_values, capacity, source_rates, rho, alpha=alpha, beta=beta
        )
        
        sim_stats = run_drain_simulation(
            G, rho, capacity, source_rates, source_paths, warmup_time=15.0, measurement_time=30.0
        )
        
        p_loss = (sim_stats["N_dropped"] / sim_stats["N_generated"]) if sim_stats["N_generated"] > 0 else 0.0
        l95 = np.percentile(sim_stats["root_latencies"], 95) if sim_stats["root_latencies"] else 0.0
        
        results_s.append(vertex_s[rho])
        results_t.append(t_values_map[rho])
        results_h.append(h_values[rho])
        results_l95.append(l95)
        results_ploss.append(p_loss)
        
    # Compute Spearman Rank Correlations
    rho_s_l95, p_s_l95 = stats.spearmanr(results_s, results_l95)
    rho_t_l95, p_t_l95 = stats.spearmanr(results_t, results_l95)
    rho_h_l95, p_h_l95 = stats.spearmanr(results_h, results_l95)
    rho_h_loss, p_h_loss = stats.spearmanr(results_h, results_ploss)
    
    # Bootstrap test for dependent correlations: rho_H - rho_S
    n_boot = 2000
    boot_diffs = []
    arr_s = np.array(results_s)
    arr_h = np.array(results_h)
    arr_l95 = np.array(results_l95)
    n_samples = len(arr_l95)
    
    for _ in range(n_boot):
        idx = np.random.choice(n_samples, n_samples, replace=True)
        r_h, _ = stats.spearmanr(arr_h[idx], arr_l95[idx])
        r_s, _ = stats.spearmanr(arr_s[idx], arr_l95[idx])
        boot_diffs.append(r_h - r_s)
        
    ci_lower = np.percentile(boot_diffs, 2.5)
    ci_upper = np.percentile(boot_diffs, 97.5)
    p_val_diff = np.mean(np.array(boot_diffs) <= 0) # Empirical p-value for H_H > H_S
    
    print("\n" + "="*65)
    print("DRAIN-CORRECTED H3 EXPERIMENTAL RESULTS & BOOTSTRAP TEST")
    print("="*65)
    print(f"Total Validated Network Iterations: {n_samples}")
    print(f"Spearman (S_G(rho) vs. L95): rho = {rho_s_l95:.4f} (p = {p_s_l95:.4e})")
    print(f"Spearman (T_G(rho) vs. L95): rho = {rho_t_l95:.4f} (p = {p_t_l95:.4e})")
    print(f"Spearman (H_G(rho) vs. L95): rho = {rho_h_l95:.4f} (p = {p_h_l95:.4e})")
    print(f"Spearman (H_G(rho) vs. P_loss): rho = {rho_h_loss:.4f} (p = {p_h_loss:.4e})")
    print("-"*65)
    print(f"Bootstrap Δρ (ρ_H - ρ_S) 95% CI: [{ci_lower:.4f}, {ci_upper:.4f}]")
    print(f"Empirical Bootstrap p-value (Δρ > 0): p = {p_val_diff:.4f}")
    print("="*65)

if __name__ == "__main__":
    main()