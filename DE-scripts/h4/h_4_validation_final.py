import simpy
import random
import numpy as np
import networkx as nx
from scipy.stats import spearmanr

class Packet:
    def __init__(self, path, birth_time):
        self.path = path
        self.hop_idx = 0
        self.birth_time = birth_time

class NetworkNode:
    def __init__(self, env, node_id, capacity):
        self.env = env
        self.node_id = node_id
        self.capacity = capacity
        self.buffer_limit = 25
        self.store = simpy.Store(env)

def traffic_generator(env, source, path_list, router_dict, arrival_rate, path_metrics):
    """Generates independent Poisson traffic and tracks path-specific sent/dropped counts."""
    while True:
        interarrival = random.expovariate(arrival_rate)
        yield env.timeout(interarrival)
        
        valid_paths = [p for p in path_list if p[0] == source]
        if not valid_paths:
            return
        chosen_path = random.choice(valid_paths)
        path_tuple = tuple(chosen_path)
        
        path_metrics[path_tuple]["sent"] += 1
        packet = Packet(chosen_path, env.now)
        first_node = router_dict[chosen_path[0]]
        
        if len(first_node.store.items) < first_node.buffer_limit:
            first_node.store.put(packet)
        else:
            path_metrics[path_tuple]["dropped"] += 1

def node_server(env, node, router_dict, path_metrics):
    """Processes packets at each node with service time inversely proportional to capacity c(u)."""
    while True:
        packet = yield node.store.get()
        
        service_rate = max(0.5, node.capacity)
        service_time = random.expovariate(service_rate)
        yield env.timeout(service_time)
        
        path = packet.path
        curr_idx = packet.hop_idx
        path_tuple = tuple(path)
        
        if curr_idx + 1 < len(path):
            next_node_id = path[curr_idx + 1]
            next_node = router_dict[next_node_id]
            packet.hop_idx += 1
            
            if len(next_node.store.items) < next_node.buffer_limit:
                next_node.store.put(packet)
            else:
                path_metrics[path_tuple]["dropped"] += 1
        else:
            latency = env.now - packet.birth_time
            path_metrics[path_tuple]["latencies"].append(latency)

def run_single_monte_carlo_trial(num_nodes=14, root=0):
    """Runs one independent network trial with decoupled traffic and path-specific drop tracking."""
    G = nx.DiGraph()
    edges = [
        (1, 0), (2, 0),
        (3, 1), (4, 1), (5, 2),
        (6, 3), (7, 3), (8, 4), (9, 5),
        (10, 6), (11, 7), (12, 8), (13, 9),
        (3, 2), (5, 1), (8, 2)
    ]
    G.add_edges_from(edges)
    
    nodes = list(G.nodes())
    sources = [u for u in nodes if G.in_degree(u) == 0]
    
    all_paths = []
    for src in sources:
        for path in nx.all_simple_paths(G, source=src, target=root):
            all_paths.append(path)
            
    if not all_paths:
        return None
        
    # Analytical parameters (used solely for theoretical metrics)
    loads = {u: random.uniform(1.0, 5.0) for u in nodes}
    capacities = {u: random.uniform(3.0, 9.0) for u in nodes}
    resilience = {u: random.uniform(0.1, 0.7) for u in nodes}
    
    loads[root] = 0.0
    capacities[root] = 30.0
    resilience[root] = 0.95
    beta = 1.0
    
    # Independent simulation workload generation (decoupled from analytical loads)
    source_arrival_rates = {src: random.uniform(1.5, 4.5) for src in sources}
    
    # 1. Analytical Metrics Setup
    local_stress, util_sum, util_max = {}, {}, {}
    for u in nodes:
        if u == root:
            local_stress[u] = 0.0
            util_sum[u] = 0.0
            util_max[u] = 0.0
        else:
            q = loads[u] / capacities[u]
            local_stress[u] = q * (1.0 - beta * resilience[u])
            util_sum[u] = q
            util_max[u] = q
            
    path_scores = {}
    for p in all_paths:
        t_g = sum(local_stress[n] for n in p)
        u_cum = sum(util_sum[n] for n in p)
        u_mx = max(util_max[n] for n in p)
        path_scores[tuple(p)] = {
            "T_G": t_g,
            "U_cum": u_cum,
            "U_max": u_mx,
            "hop": len(p)
        }
        
    p_star = max(path_scores, key=lambda k: path_scores[k]["T_G"])
    p_ucum = max(path_scores, key=lambda k: path_scores[k]["U_cum"])
    p_umax = max(path_scores, key=lambda k: path_scores[k]["U_max"])
    p_hop = max(all_paths, key=len)
    p_rand = random.choice(all_paths)
    
    # 2. Independent Queueing Simulation Stage
    env = simpy.Environment()
    router_dict = {u: NetworkNode(env, u, capacities[u]) for u in nodes}
    path_metrics = {tuple(p): {"latencies": [], "sent": 0, "dropped": 0} for p in all_paths}
    
    for u in nodes:
        env.process(node_server(env, router_dict[u], router_dict, path_metrics))
        
    for src in sources:
        arr_rate = source_arrival_rates[src]
        env.process(traffic_generator(env, src, all_paths, router_dict, arr_rate, path_metrics))
        
    env.run(until=100.0)
    
    # 3. Empirical Performance Extraction
    empirical_data = {}
    for path_tuple, data in path_metrics.items():
        lats = data["latencies"]
        if len(lats) >= 10:
            mean_lat = np.mean(lats)
            p95_lat = np.percentile(lats, 95)
            drop_rate = (data["dropped"] / data["sent"]) if data["sent"] > 0 else 0.0
            
            empirical_data[path_tuple] = {
                "L_mean": mean_lat,
                "L_95": p95_lat,
                "Drop_Rate": drop_rate
            }
            
    common_keys = [k for k in path_scores.keys() if k in empirical_data]
    if len(common_keys) < 4:
        return None
        
    # Sort empirical paths by L_95 descending (worst bottleneck first)
    sorted_empirical_l95 = sorted(common_keys, key=lambda k: empirical_data[k]["L_95"], reverse=True)
    p_hat_l95 = sorted_empirical_l95[0]
    top3_empirical = set(sorted_empirical_l95[:3])
    
    # Top-k localization checks for T_G
    hit_star_top1 = 1 if p_star == p_hat_l95 else 0
    hit_star_top3 = 1 if p_star in top3_empirical else 0
    
    hit_ucum_top1 = 1 if p_ucum == p_hat_l95 else 0
    hit_umax_top1 = 1 if p_umax == p_hat_l95 else 0
    hit_hop_top1 = 1 if tuple(p_hop) == p_hat_l95 else 0
    hit_rand_top1 = 1 if tuple(p_rand) == p_hat_l95 else 0
    
    # Rank correlations
    tg_vals = [path_scores[k]["T_G"] for k in common_keys]
    l95_vals = [empirical_data[k]["L_95"] for k in common_keys]
    lmean_vals = [empirical_data[k]["L_mean"] for k in common_keys]
    drop_vals = [empirical_data[k]["Drop_Rate"] for k in common_keys]
    
    rho_l95, _ = spearmanr(tg_vals, l95_vals)
    rho_lmean, _ = spearmanr(tg_vals, lmean_vals)
    rho_drop, _ = spearmanr(tg_vals, drop_vals)
    
    return {
        "hit_star_top1": hit_star_top1,
        "hit_star_top3": hit_star_top3,
        "hit_ucum_top1": hit_ucum_top1,
        "hit_umax_top1": hit_umax_top1,
        "hit_hop_top1": hit_hop_top1,
        "hit_rand_top1": hit_rand_top1,
        "rho_l95": rho_l95 if not np.isnan(rho_l95) else 0.0,
        "rho_lmean": rho_lmean if not np.isnan(rho_lmean) else 0.0,
        "rho_drop": rho_drop if not np.isnan(rho_drop) else 0.0
    }

def run_decoupled_experiment(num_trials=300):
    print(f"Running {num_trials}-trial decoupled H4 validation study...")
    results = {
        "star_top1": [], "star_top3": [], "ucum_top1": [], "umax_top1": [],
        "hop_top1": [], "rand_top1": [],
        "rho_l95": [], "rho_lmean": [], "rho_drop": []
    }
    
    random.seed(42)
    np.random.seed(42)
    
    for _ in range(num_trials):
        res = run_single_monte_carlo_trial()
        if res is not None:
            results["star_top1"].append(res["hit_star_top1"])
            results["star_top3"].append(res["hit_star_top3"])
            results["ucum_top1"].append(res["hit_ucum_top1"])
            results["umax_top1"].append(res["hit_umax_top1"])
            results["hop_top1"].append(res["hit_hop_top1"])
            results["rand_top1"].append(res["hit_rand_top1"])
            results["rho_l95"].append(res["rho_l95"])
            results["rho_lmean"].append(res["rho_lmean"])
            results["rho_drop"].append(res["rho_drop"])
            
    n_valid = len(results["rho_l95"])
    print("\n========================================================")
    print("      DECOUPLED H4 VALIDATION RESULTS (300 TRIALS)      ")
    print("========================================================")
    print(f"Total Valid Trials: {n_valid}")
    print(f"\n--- 1. BOTTLENECK LOCALIZATION HIT RATES ---")
    print(f"Analytical Stress ($T_G$) Top-1 Match:      {np.mean(results['star_top1'])*100:.1f}%")
    print(f"Analytical Stress ($T_G$) Top-3 Inclusion:  {np.mean(results['star_top3'])*100:.1f}%")
    print(f"Cumulative Utilization ($U_{{cum}}$) Top-1: {np.mean(results['ucum_top1'])*100:.1f}%")
    print(f"Max Utilization ($U_{{max}}$) Top-1:      {np.mean(results['umax_top1'])*100:.1f}%")
    print(f"Longest Hop-Count Top-1:                {np.mean(results['hop_top1'])*100:.1f}%")
    print(f"Random Path Selection Top-1:            {np.mean(results['rand_top1'])*100:.1f}%")
    
    print(f"\n--- 2. RANK CORRELATION ASSOCIATIONS ($\rho$) ---")
    for metric_name, key in [("95th-Percentile Latency ($L_{95}$)", "rho_l95"),
                             ("Mean Latency ($L_{mean}$)", "rho_lmean"),
                             ("True Path Drop Rate ($D_{loss}$)", "rho_drop")]:
        vals = results[key]
        mean_r = np.mean(vals)
        boot = [np.mean(np.random.choice(vals, size=len(vals), replace=True)) for _ in range(1000)]
        ci_l, ci_u = np.percentile(boot, [2.5, 97.5])
        print(f"T_G vs {metric_name}: Mean Rho = {mean_r:.3f} (95% CI: [{ci_l:.3f}, {ci_u:.3f}])")
    print("========================================================")

if __name__ == "__main__":
    run_decoupled_experiment(num_trials=300)