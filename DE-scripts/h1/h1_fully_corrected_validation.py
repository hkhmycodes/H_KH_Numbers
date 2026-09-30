import simpy
import networkx as nx
import numpy as np
import matplotlib.pyplot as plt
from scipy.stats import spearmanr
from networkx.algorithms.connectivity import local_edge_connectivity

class NetworkSimulation:
    def __init__(self, G, root, capacities, arrival_rates, load_mult, seed):
        self.env = simpy.Environment()
        self.G = G
        self.root = root
        self.capacities = capacities
        self.arrival_rates = {n: r * load_mult for n, r in arrival_rates.items()}
        
        np.random.seed(seed)
        
        # Precompute primary paths to root
        self.primary_paths = {}
        for node in G.nodes():
            if node != root:
                try:
                    self.primary_paths[node] = nx.shortest_path(G, source=node, target=root)
                except nx.NetworkXNoPath:
                    self.primary_paths[node] = [node, root]
                    
        # Single-server queue per node matching analytical service capacity c(u)
        self.node_resources = {
            node: simpy.Resource(self.env, capacity=1) 
            for node in G.nodes()
        }
        
        self.latencies = []
        self.total_generated = 0
        self.total_dropped = 0
        self.measurement_active = False
        self.generating_active = True
        self.active_packets = 0

    def packet_flow(self, packet_id, source, counts_toward_measurement):
        self.active_packets += 1
        path = self.primary_paths.get(source, [source, self.root])
        start_time = self.env.now
        
        try:
            for i in range(len(path) - 1):
                curr_node = path[i]
                resource = self.node_resources[curr_node]
                
                # Drop-tail buffer limit (max 10 queued packets)
                if len(resource.queue) >= 10:
                    if counts_toward_measurement:
                        self.total_dropped += 1
                    return 
                    
                with resource.request() as req:
                    yield req
                    # Service time strictly governed by 1 / c(u)
                    service_time = np.random.exponential(1.0 / self.capacities[curr_node])
                    yield self.env.timeout(service_time)
                    
            end_time = self.env.now
            if counts_toward_measurement:
                self.latencies.append(end_time - start_time)
        finally:
            self.active_packets -= 1

    def source_generator(self, source, rate):
        packet_id = 0
        while self.generating_active:
            if rate <= 0:
                break
            interarrival = np.random.exponential(1.0 / rate)
            yield self.env.timeout(interarrival)
            
            if not self.generating_active:
                break
                
            counts = self.measurement_active
            if counts:
                self.total_generated += 1
                
            self.env.process(self.packet_flow(f"{source}-{packet_id}", source, counts))
            packet_id += 1

    def run(self):
        for source, rate in self.arrival_rates.items():
            if source != self.root:
                self.env.process(self.source_generator(source, rate))
                
        yield self.env.timeout(15.0)  # Warm-up Period
        
        self.measurement_active = True
        self.latencies = []
        self.total_generated = 0
        self.total_dropped = 0
        
        yield self.env.timeout(30.0)  # Measurement Interval
        
        self.measurement_active = False
        self.generating_active = False
        
        # Unconstrained drain phase ensuring cohort resolution
        while self.active_packets > 0:
            yield self.env.timeout(0.5)


def generate_multilayer_network(n_nodes=30, seed=42):
    np.random.seed(seed)
    G = nx.DiGraph()
    tier_size = n_nodes // 3
    tiers = [
        list(range(0, tier_size)),
        list(range(tier_size, 2 * tier_size)),
        list(range(2 * tier_size, n_nodes - 1)),
        [n_nodes - 1]
    ]
    root = n_nodes - 1
    
    for i in range(len(tiers) - 1):
        for u in tiers[i]:
            targets = np.random.choice(tiers[i+1], size=min(2, len(tiers[i+1])), replace=False)
            for v in targets: G.add_edge(u, v)
                
    for _ in range(5):
        u, v = np.random.randint(0, n_nodes - 2), np.random.randint(0, n_nodes - 2)
        if u != v: G.add_edge(u, v)
            
    for node in range(n_nodes - 1):
        if not nx.has_path(G, node, root): G.add_edge(node, root)
    return G, root


# --- Rigorous Analytical Framework with True Route-Specific Resilience ---
def compute_structural_order(condensed_dag):
    S_G = {}
    for node in nx.topological_sort(condensed_dag):
        preds = list(condensed_dag.predecessors(node))
        base_val = 1
        if preds:
            max_pred = max(S_G[p] for p in preds)
            max_preds_count = sum(1 for p in preds if S_G[p] == max_pred)
            S_G[node] = max_pred + (1 if max_preds_count > 1 else 0)
        else:
            S_G[node] = base_val
    root_sccs = [n for n, out_deg in condensed_dag.out_degree() if out_deg == 0]
    return max(S_G[n] for n in root_sccs) if root_sccs else 1

def compute_analytical_metrics(G, root, capacities, arrival_rates, beta=1.0, alpha=1.0):
    condensed = nx.condensation(G)
    S_G_val = compute_structural_order(condensed)
    
    # Precompute true route-specific resilience r_E(u) based on graph cuts
    r_E_vals = {}
    for node in G.nodes():
        if node == root:
            r_E_vals[node] = 1.0
            continue
        try:
            path = nx.shortest_path(G, source=node, target=root)
            path_edges = list(zip(path[:-1], path[1:]))
            G_sub = G.copy()
            G_sub.remove_edges_from(path_edges)
            if nx.has_path(G_sub, node, root):
                b_u = local_edge_connectivity(G_sub, node, root)
            else:
                b_u = 0
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            b_u = 0
        r_E_vals[node] = b_u / (1.0 + b_u)

    g_vals = {}
    for node in G.nodes():
        l_u = arrival_rates.get(node, 0.0)
        c_u = capacities.get(node, 5.0)
        q_u = l_u / c_u
        r_E = r_E_vals[node]
        g_vals[node] = q_u * (1.0 - beta * r_E)
        
    g_G = {}
    for scc_id, data in condensed.nodes(data=True):
        members = data['members']
        g_G[scc_id] = max(g_vals[u] for u in members)
        
    T_G = {}
    for scc_id in nx.topological_sort(condensed):
        preds = list(condensed.predecessors(scc_id))
        max_pred_T = max((T_G[p] for p in preds), default=0.0)
        T_G[scc_id] = g_G[scc_id] + max_pred_T
        
    root_sccs = [n for n, out_deg in condensed.out_degree() if out_deg == 0]
    T_G_val = max(T_G[n] for n in root_sccs) if root_sccs else 0.0
    H_G_val = S_G_val + alpha * T_G_val
    
    return S_G_val, T_G_val, H_G_val


# Base setup
G, root = generate_multilayer_network(n_nodes=30, seed=42)
capacities = {node: np.random.uniform(4.0, 10.0) for node in G.nodes()}

load_multipliers = np.linspace(0.5, 4.0, 8)
seeds = [42, 123, 456, 789, 1024]

mean_L95, std_L95 = [], []
mean_loss, std_loss = [], []
hg_values = []

base_arrival_rates = {node: 1.2 * (1.0 + 0.1 * (node % 3)) for node in G.nodes() if node != root}

print("Running fully corrected simulation and analytical metric sweep...")
for mult in load_multipliers:
    seed_l95s = []
    seed_losses = []
    
    # Correctly scale arrival rates for analytical evaluation without distorting S_G
    scaled_arrival_rates = {node: rate * mult for node, rate in base_arrival_rates.items()}
    _, _, hg_val = compute_analytical_metrics(G, root, capacities, scaled_arrival_rates, beta=1.0, alpha=1.0)
    hg_values.append(hg_val)
    
    for s in seeds:
        sim = NetworkSimulation(G, root, capacities, base_arrival_rates, load_mult=mult, seed=s)
        sim.env.process(sim.run())
        sim.env.run(until=120.0)
        
        if len(sim.latencies) > 0:
            l95 = np.percentile(sim.latencies, 95)
            loss_prob = sim.total_dropped / max(1, sim.total_generated)
        else:
            l95 = 0.0
            loss_prob = 1.0
            
        seed_l95s.append(l95)
        seed_losses.append(loss_prob)
        
    mean_L95.append(np.mean(seed_l95s))
    std_L95.append(np.std(seed_l95s))
    mean_loss.append(np.mean(seed_losses))
    std_loss.append(np.std(seed_losses))
    print(f"  gamma={mult:.2f} | H_G={hg_values[-1]:.3f} | L95={mean_L95[-1]:.2f} (±{std_L95[-1]:.2f}) | LossProb={mean_loss[-1]:.3f} (±{std_loss[-1]:.3f})")

# Compute Spearman rank correlations with observed congestion metrics
corr_l95, _ = spearmanr(hg_values, mean_L95)
corr_loss, _ = spearmanr(hg_values, mean_loss)
print(f"\nSpearman Correlation Analysis (n=8):")
print(f"  rho(H_G, L_95)  = {corr_l95:.3f}")
print(f"  rho(H_G, P_loss) = {corr_loss:.3f}")

# --- Publication-Grade Validation Plotting ---
fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 5), dpi=300)

ax1.plot(load_multipliers, hg_values, marker='o', linestyle='-', color='tab:purple', linewidth=2)
ax1.set_xlabel(r'Traffic Load Multiplier ($\gamma$)', fontsize=11)
ax1.set_ylabel(r'Integrated Metric $H_G(\gamma)$', fontsize=11)
ax1.set_title(r'H1 Model Response: $H_G(\gamma)$ vs. $\gamma$', fontsize=12, fontweight='bold')
ax1.grid(True, linestyle='--', alpha=0.4)

color = 'tab:red'
ax2.set_xlabel(r'Traffic Load Multiplier ($\gamma$)', fontsize=11)
ax2.set_ylabel(r'95th-Percentile Latency $L_{95}$ (t.u.)', color=color, fontsize=11)
ax2.errorbar(load_multipliers, mean_L95, yerr=std_L95, marker='o', linestyle='-', color=color, linewidth=2, capsize=4, label=r'$L_{95}$')
ax2.tick_params(axis='y', labelcolor=color)
ax2.grid(True, linestyle='--', alpha=0.4)

ax_twin = ax2.twinx()
color = 'tab:blue'
ax_twin.set_ylabel(r'Buffer Drop Probability ($P_{\text{drop}}$)', color=color, fontsize=11)
ax_twin.errorbar(load_multipliers, mean_loss, yerr=std_loss, marker='s', linestyle='--', color=color, linewidth=2, capsize=4, label=r'$P_{\text{drop}}$')
ax_twin.tick_params(axis='y', labelcolor=color)

ax2.set_title(r'Empirical Congestion: $L_{95}$ and $P_{\text{drop}}$ Scaling', fontsize=12, fontweight='bold')

fig.tight_layout()
plt.savefig('h1_fully_corrected_validation.png')
plt.show()
print("Execution complete. Fully corrected framework saved as 'h1_fully_corrected_validation.png'.")