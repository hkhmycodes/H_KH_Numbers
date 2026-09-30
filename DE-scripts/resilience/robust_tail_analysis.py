import simpy
import numpy as np
import networkx as nx

def compute_khayou_framework_metrics(b_redundancy):
    """Computes graph-derived r_E and analytical H_G."""
    G = nx.DiGraph()
    G.add_edge('Source', 'Router', capacity=10.0)
    G.add_edge('Router', 'Destination', capacity=10.0, route='primary')
    
    for i in range(b_redundancy):
        backup_node = f'BackupRouter_{i}'
        G.add_edge('Router', backup_node, capacity=4.0)
        G.add_edge(backup_node, 'Destination', capacity=4.0, route='backup')
        
    pre_flow, _ = nx.maximum_flow(G, 'Source', 'Destination', capacity='capacity')
    
    G_post = G.copy()
    edges_to_remove = [(u, v) for u, v, data in G_post.edges(data=True) if data.get('route') == 'primary']
    G_post.remove_edges_from(edges_to_remove)
    
    post_flow, _ = nx.maximum_flow(G_post, 'Source', 'Destination', capacity='capacity')
    r_E = post_flow / pre_flow if pre_flow > 0 else 0.0
    
    S_G, alpha, beta, q_u = 1.0, 1.0, 1.0, 0.75
    H_G = S_G + alpha * (q_u * (1.0 - beta * r_E) + 0.5)
    
    return r_E, H_G

def run_single_simulation(b_redundancy, seed, sim_time=40.0, fault_time=15.0):
    """Runs a single simulation instance with high-resolution bins."""
    env = simpy.Environment()
    rng = np.random.default_rng(seed)
    
    time_bins = np.linspace(0, sim_time, 200)
    bin_width = time_bins[1] - time_bins[0]
    
    arrival_counts = np.zeros(len(time_bins))
    dropped_counts = np.zeros(len(time_bins))
    delivered_counts = np.zeros(len(time_bins))
    
    arrival_rate = 3.5      
    primary_capacity = 10.0
    backup_capacity = b_redundancy * 4.0
    buffer_limit = 15
    
    queue = simpy.Store(env, capacity=buffer_limit)
    primary_active = [True]
    
    def packet_generator():
        pkt_id = 0
        while env.now < sim_time:
            yield env.timeout(rng.exponential(1.0 / arrival_rate))
            pkt_id += 1
            b_idx = int(env.now / bin_width)
            if b_idx < len(arrival_counts):
                arrival_counts[b_idx] += 1
                
            if len(queue.items) >= queue.capacity:
                if b_idx < len(dropped_counts):
                    dropped_counts[b_idx] += 1
            else:
                yield queue.put((pkt_id, env.now))

    def router_processor():
        while True:
            current_rate = primary_capacity if primary_active[0] else backup_capacity
            if current_rate <= 0:
                yield env.timeout(1.0)
                continue
                
            yield env.timeout(1.0 / current_rate)
            if len(queue.items) > 0:
                yield queue.get()
                b_idx = int(env.now / bin_width)
                if b_idx < len(delivered_counts):
                    delivered_counts[b_idx] += 1

    def fault_injector():
        yield env.timeout(fault_time)
        primary_active[0] = False

    env.process(packet_generator())
    env.process(router_processor())
    env.process(fault_injector())
    env.run(until=sim_time)
    
    ploss_measured = np.zeros_like(time_bins)
    throughput_measured = delivered_counts / bin_width
    
    for i in range(len(time_bins)):
        if arrival_counts[i] > 0:
            ploss_measured[i] = (dropped_counts[i] / arrival_counts[i]) * 100.0
            
    post_fault_mask = time_bins >= fault_time
    ploss_max = np.max(ploss_measured[post_fault_mask])
    
    target_throughput = 0.95 * arrival_rate
    recovery_time = np.nan
    
    post_times = time_bins[post_fault_mask] - fault_time
    post_throughput = throughput_measured[post_fault_mask]
    
    window_size = int(1.0 / bin_width)
    if len(post_throughput) >= window_size:
        rolling_mean = np.convolve(post_throughput, np.ones(window_size)/window_size, mode='valid')
        rolling_times = post_times[window_size - 1:]
        
        for t_val, mean_val in zip(rolling_times, rolling_mean):
            if mean_val >= target_throughput:
                recovery_time = t_val
                break
                
    return ploss_max, recovery_time

def run_robust_tail_analysis(num_seeds=60):
    """Executes robust Monte Carlo analysis focusing on relative tail risk reduction."""
    b_values = [0, 1, 2]
    seeds = range(1, 1 + num_seeds)
    
    print("=" * 115)
    print(f"{'Redundancy (b)':<15} | {'r_E':<5} | {'Mean P_max':<12} | {'Mean T_rec':<12} | {'P90 T_rec':<10} | {'P(T_rec > 2s)':<14} | {'Recovery Rate'}")
    print("-" * 115)
    
    for b in b_values:
        r_E, _ = compute_khayou_framework_metrics(b)
        
        ploss_maxs, recovery_times = [], []
        recovered_count = 0
        
        for seed in seeds:
            p_max, t_rec = run_single_simulation(b, seed)
            ploss_maxs.append(p_max)
            if not np.isnan(t_rec):
                recovery_times.append(t_rec)
                recovered_count += 1
                
        mean_p_max = np.mean(ploss_maxs)
        mean_t_rec = np.mean(recovery_times) if recovery_times else np.nan
        p90_t_rec = np.percentile(recovery_times, 90) if recovery_times else np.nan
        
        # Clean exceedance calculation without magic multipliers
        exceed_2s = (sum(1 for t in recovery_times if t > 2.0) / num_seeds) * 100.0 if recovery_times else 0.0
        
        rec_rate_str = f"{recovered_count}/{num_seeds} ({(recovered_count/num_seeds)*100:.0f}%)"
        p_max_str = f"{mean_p_max:.1f}%"
        t_rec_str = f"{mean_t_rec:.2f}s" if not np.isnan(mean_t_rec) else "N/A"
        p90_str = f"{p90_t_rec:.2f}s" if not np.isnan(p90_t_rec) else "N/A"
        exceed_str = f"{exceed_2s:.1f}%"
        
        print(f"{b:<15} | {r_E:<5.2f} | {p_max_str:<12} | {t_rec_str:<12} | {p90_str:<10} | {exceed_str:<14} | {rec_rate_str}")
        
    print("=" * 115)

if __name__ == "__main__":
    run_robust_tail_analysis()